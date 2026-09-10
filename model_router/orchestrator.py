from __future__ import annotations

from dataclasses import asdict
import copy
import json
from pathlib import Path
import time
import uuid

from .experience import ExperienceStore
from .handoff import build_handoff, build_prompt
from .models import Event, Outcome
from .monitor import BudgetExceeded, Monitor, RunCancelled
from .policy import NoSafeRoute, Policy
from .providers import ProviderUnavailable, get_provider
from .usage import read_usage, remaining_percent
from .verifier import Verifier
from .workspace import Workspace, inventory


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Orchestrator:
    def __init__(self, config, provider_factory=get_provider, verifier=None, on_route=None,
                 on_event=None, on_workspace=None, cancel_event=None):
        self.config = config
        self.provider_factory = provider_factory
        self.verifier = verifier or Verifier(config)
        self.on_route = on_route
        self.on_event, self.on_workspace, self.cancel_event = on_event, on_workspace, cancel_event

    def run(self, task, repo: Path):
        cfg = self.config
        cfg.validate()
        started = time.monotonic()
        if not task.allowed_files:
            raise ValueError("Execution requires explicit allowed_files; edit the analyzed task before running")
        unknown_checks = set(task.checks) - cfg.commands.keys()
        if unknown_checks:
            raise ValueError(f"Unconfigured checks: {sorted(unknown_checks)}")
        # No invented automatic oracle. Lower the profile before choosing a model.
        if not task.checks or not task.acceptance_criteria or task.manual_verification:
            task.profile.automatic_verifiability = min(task.profile.automatic_verifiability, 1)
        run_id = uuid.uuid4().hex
        run_dir = cfg.state_dir / "runs" / run_id
        workspace = Workspace(repo, run_dir)
        workspace.prepare()
        if self.on_workspace:
            self.on_workspace(run_dir)
        save_json(run_dir / "task.json", asdict(task))
        store = ExperienceStore(cfg.state_dir / "experience.sqlite3")
        policy = Policy(cfg, store)
        all_events, handoff = [], ""
        status, failure, executor, attempt = "blocked", "No attempt", "", 0
        executed = 0
        try:
            for attempt in range(1, cfg.max_attempts + 1):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    status, failure = "cancelled", "User requested stop"
                    break
                remaining = cfg.max_total_runtime - (time.monotonic() - started)
                deadline_seconds = task.profile.hours_left() * 3600
                if remaining <= 0 or deadline_seconds <= 0:
                    failure = "Task total runtime/deadline exhausted"
                    break
                if cfg.auto_usage and any(p.get("kind") == "codex_cli" for p in cfg.providers.values()):
                    try:
                        command = next(p.get("command", "codex") for p in cfg.providers.values() if p.get("kind") == "codex_cli")
                        usage = read_usage(command, timeout=min(8, remaining, deadline_seconds))
                        value = remaining_percent(usage, cfg.codex_limit_id)
                        if value is not None:
                            cfg.codex_budget_remaining = value
                        save_json(run_dir / "quota.json", {"remaining_percent": value, "source": "app-server", "snapshot": usage})
                    except (OSError, RuntimeError, TimeoutError) as exc:
                        save_json(run_dir / "quota.json", {"remaining_percent": cfg.codex_budget_remaining,
                                                          "source": "manual_or_unknown", "error": str(exc)[:500]})
                try:
                    route = policy.decide(task)
                    if task.selected_executor:
                        chosen = cfg.model(task.selected_executor)
                        route.executor = chosen.id
                        route.budget = policy.budget(task, chosen)
                        route.next_executor = None
                        route.reasons.append("使用审阅方案中明确选定的执行模型")
                except NoSafeRoute as exc:
                    failure = str(exc)
                    break
                executor = route.executor
                model = cfg.model(executor)
                remaining = cfg.max_total_runtime - (time.monotonic() - started)
                if remaining <= 0 or task.profile.hours_left() <= 0:
                    failure = "Task total runtime/deadline exhausted during routing"
                    break
                route.budget.max_runtime = min(route.budget.max_runtime, remaining, max(.1, task.profile.hours_left() * 3600))
                save_json(run_dir / f"route-{attempt}.json", asdict(route))
                if self.on_route:
                    self.on_route(route)
                workspace.checkpoint(f"checkpoint-{attempt}")
                remaining = min(cfg.max_total_runtime - (time.monotonic() - started), task.profile.hours_left() * 3600)
                if remaining <= 0:
                    failure = "Task total runtime/deadline exhausted during checkpoint"
                    break
                route.budget.max_runtime = min(route.budget.max_runtime, remaining)
                prompt = build_prompt(task, route, handoff, cfg.commands)
                (run_dir / f"prompt-{attempt}.md").write_text(prompt, encoding="utf-8")
                monitor = Monitor(route.budget, cancel_event=self.cancel_event)
                events, verification, infra = [], None, False
                attempt_start = time.monotonic()
                executed = attempt

                def emit(event):
                    events.append(event)
                    all_events.append(event)
                    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as log:
                        log.write(json.dumps({"attempt": attempt, **asdict(event)}, ensure_ascii=False) + "\n")
                    if self.on_event:
                        self.on_event(attempt, event)
                    monitor.observe(event)

                try:
                    provider = self.provider_factory(cfg, model)
                    provider.run(task=task, model=model, prompt=prompt, work=workspace.work, monitor=monitor, emit=emit)
                    monitor.check()
                    remaining = min(cfg.max_total_runtime - (time.monotonic() - started),
                                    task.profile.hours_left() * 3600, monitor.remaining())
                    if remaining <= 0:
                        raise BudgetExceeded("verification runtime exhausted")
                    verification = self.verifier.verify(task, workspace, timeout=min(120, remaining))
                    save_json(run_dir / f"verification-{attempt}.json", asdict(verification))
                    failure = "Independent verifier: " + verification.status
                    if verification.status == "passed" and route.reviewer:
                        approved = self._review(task, route, workspace, attempt, store, run_id,
                                                min(cfg.max_total_runtime - (time.monotonic() - started),
                                                    task.profile.hours_left() * 3600))
                        if not approved:
                            verification.status = "failed"
                            failure = "Required independent review rejected or returned no structured approval"
                            save_json(run_dir / f"verification-{attempt}.json", asdict(verification))
                    monitor.check()
                    if time.monotonic() - started >= cfg.max_total_runtime or task.profile.hours_left() <= 0:
                        raise BudgetExceeded("Task runtime/deadline exhausted")
                    if verification.status == "passed":
                        status = "verified"
                        save_json(run_dir / "verified-hashes.json", inventory(workspace.work))
                    elif verification.status == "needs_human":
                        status = "needs_human"
                except RunCancelled as exc:
                    status, failure = "cancelled", str(exc)
                except BudgetExceeded as exc:
                    failure = "Stopped by controller: " + str(exc)
                except ProviderUnavailable as exc:
                    failure, infra = str(exc), True
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    failure, infra = f"Adapter/verification error: {type(exc).__name__}: {exc}", True
                try:
                    changed = workspace.diff()
                except (OSError, ValueError) as exc:
                    # A compromised/unsupported candidate is not safe input for the next model.
                    status, infra, changed = "blocked", True, []
                    failure = f"Workspace safety check failed: {exc}"
                stop = status in {"verified", "needs_human", "cancelled"} or infra or task.no_escalation or task.force == "deepseek"
                can_escalate = not stop and attempt < cfg.max_attempts
                next_task = copy.deepcopy(task)
                if not stop:
                    next_task.profile.previous_attempt_count += 1
                    if executor not in next_task.profile.failed_executors:
                        next_task.profile.failed_executors.append(executor)
                    if can_escalate:
                        try:
                            policy.decide(next_task)
                        except NoSafeRoute:
                            can_escalate = False
                            failure += "; no higher safe executor available"
                usage_events = [e.data for e in events if e.kind == "usage"]
                usage = usage_events[-1] if usage_events else {}
                store.record(run_id=run_id, attempt=attempt, profile=task.profile, model=model,
                             strategy="execute_review" if route.reviewer else "execute", success=status == "verified", runtime=time.monotonic() - attempt_start,
                             escalation=can_escalate, verifier=verification.status if verification else "not_run",
                             human_intervention=status == "needs_human", tokens=usage.get("tokens"),
                             api_cost=usage.get("api_cost"), codex_usage=usage.get("codex_usage"),
                             outcome_kind="cancelled" if status == "cancelled" else "infrastructure" if infra else "unverified" if status == "needs_human" else "engineering")
                handoff = build_handoff(task, all_events, verification, failure, changed)
                (run_dir / "ESCALATION.md").write_text(handoff, encoding="utf-8")
                (run_dir / f"handoff-{attempt}.md").write_text(handoff, encoding="utf-8")
                if stop:
                    break
                task = next_task
                if not can_escalate:
                    if attempt >= cfg.max_attempts:
                        failure += "; max_attempts exhausted"
                    break
            result = Outcome(status, str(run_dir), executor, failure, executed)
            save_json(run_dir / "result.json", asdict(result))
            return result
        finally:
            store.close()

    def _review(self, task, route, workspace, attempt, store, run_id, remaining):
        if remaining <= 0:
            raise BudgetExceeded("No time remaining for mandatory review")
        model = self.config.model(route.reviewer)
        budget = Policy(self.config).budget(task, model)
        budget.max_runtime = min(budget.max_runtime, remaining, 300)
        monitor = Monitor(budget, cancel_event=self.cancel_event)
        events = []
        baseline = inventory(workspace.work)
        diff = (workspace.run_dir / "changes.diff").read_text(encoding="utf-8")
        prompt = ("Perform a read-only engineering review. Do not implement or change any files. "
                  "Inspect the full candidate against acceptance criteria and the diff. "
                  "Final response MUST be JSON: {\"approved\": true/false, \"findings\": [strings]}. "
                  "Approve only when no blocking issues remain.\n" +
                  build_prompt(task, route, commands=self.config.commands) + "\n## Candidate diff\n" + diff[:30000])
        def emit(event):
            events.append(event)
            monitor.observe(event)
        approved, infrastructure, cancelled = False, False, False
        try:
            self.provider_factory(self.config, model).run(task=task, model=model, prompt=prompt,
                                                         work=workspace.work, monitor=monitor, emit=emit, review=True)
            summaries = [e.data.get("text", "") for e in events if e.kind == "summary"]
            answer = json.loads(summaries[-1]) if summaries else {}
            approved = answer.get("approved") is True and answer.get("findings") == []
            monitor.check()
        except RunCancelled:
            cancelled = True
            raise
        except (ValueError, BudgetExceeded):
            approved = False
        except ProviderUnavailable:
            infrastructure = True
            raise
        finally:
            if inventory(workspace.work) != baseline:
                approved = False
            save_json(workspace.run_dir / f"review-{attempt}.json", {"approved": approved, "events": [asdict(e) for e in events]})
            store.record(run_id=run_id, attempt=attempt, profile=task.profile, model=model, strategy="review",
                         success=approved, runtime=monitor.clock() - monitor.started, escalation=False,
                         verifier="passed" if approved else "failed", outcome_kind="cancelled" if cancelled else "infrastructure" if infrastructure else "engineering")
        return approved
