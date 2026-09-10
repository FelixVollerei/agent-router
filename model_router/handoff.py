from dataclasses import asdict
import json


def build_prompt(task, route, handoff="", commands=None):
    sections = {
        "Approved Execution Prompt": task.execution_prompt or task.goal or task.request,
        "Goal": task.goal or task.request,
        "Original Request": task.request,
        "Relevant Context": task.context + ["You are in an isolated copy; VCS metadata is intentionally excluded. Do not initialize a repository or spend time discovering git history."],
        "Exact Scope / Allowed Files": task.allowed_files,
        "Constraints": task.constraints,
        "Acceptance Criteria": task.acceptance_criteria,
        "Forbidden Changes": task.forbidden_files + ["No changes outside isolated workspace", "No VCS metadata modifications", "No deployment, publishing or external side effects"],
        "Verification Method": {name: (commands or {}).get(name, "Host-defined check; use run_check by name") for name in task.checks},
        "Manual Verification Required": task.manual_verification,
        "Execution Budget": asdict(route.budget),
        "Escalation Conditions": ["Stop at any budget limit", "Stop repeated hypotheses/reads/patch oscillation without new evidence", "Executor completion is not verifier success"],
        "Handoff Evidence": handoff or "No previous attempt",
    }
    return "\n\n".join(f"## {key}\n" + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
                         for key, value in sections.items())


def build_handoff(task, events, verification, failure, changed):
    def texts(kind, key):
        return list(dict.fromkeys(str(e.data.get(key, "")) for e in events if e.kind == kind and e.data.get(key)))
    def hypotheses(category):
        return [e.data.get("text") for e in events if e.kind == "hypothesis" and e.data.get("category") == category]
    sections = {
        "Original Request": task.request,
        "Task Profile": asdict(task.profile),
        "Current Goal": task.goal or task.request,
        "Files Inspected": texts("file_read", "path"),
        "Files Modified": changed,
        "Commands Executed": texts("tool", "command") + texts("test", "argv"),
        "Tests Executed": verification.checks if verification else [],
        "Observed Results": [e.data for e in events if e.kind in {"test", "tool"}][-12:],
        "Known Good Facts (executor-reported; confirm citations)": hypotheses("known_fact"),
        "Hypotheses Tested (executor-reported)": hypotheses("hypothesis_tested"),
        "Hypotheses Rejected (executor-reported)": hypotheses("hypothesis_rejected"),
        "Current Failure": failure,
        "Unresolved Questions": task.profile.unresolved_questions + hypotheses("question"),
        "Most Promising Next Steps": hypotheses("next_step") or ["Use recorded evidence and verifier failures; do not repeat disproved hypotheses without new evidence"],
        "Important Constraints": task.constraints,
        "Git Diff / Patch Summary": {"changed_files": changed, "patch": "../changes.diff (relative to isolated work)",
                                     "note": "Candidate retains prior edits; baseline and per-attempt checkpoints are outside work"},
    }
    # Bound EACH section so the failure/constraints never disappear due to an early long log.
    parts = []
    for key, value in sections.items():
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        limit = 4000 if key == "Observed Results" else 1800
        if len(rendered) > limit:
            rendered = rendered[:limit] + "\n[truncated; full local evidence in events.jsonl]"
        parts.append(f"## {key}\n{rendered}")
    return "# ESCALATION\n\n" + "\n\n".join(parts)
