"""Private worker entry point. No network or work before trusted stdin admission."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

from .config import Config
from .job_contract import canonical
from .models import ModelSpec, Task
from .monitor import BudgetExceeded
from .orchestrator import Orchestrator
from .providers import get_provider
from .workspace import safe_path


def send(kind, data):
    # Stream only bounded host-normalized fields; private prompts/reasoning stay local.
    value = canonical({"kind": kind, "data": data})
    if len(value.encode("utf-8")) > 24000:
        value = canonical({"kind": "progress", "data": {"event": kind, "detail": "large_detail_retained_in_run_artifacts"}})
    print(value, flush=True)


def execute(payload):
    started = time.monotonic()
    settings = payload["config"]
    settings["state_dir"] = Path(settings["state_dir"])
    settings["models"] = [ModelSpec(**row) for row in settings["models"]]
    cfg = Config(**settings)
    cfg.validate()
    request = payload["request"]
    task = Task.from_dict(payload["task"])
    root = Path(payload["package"])
    root.mkdir(parents=True, exist_ok=False)
    source = Path(payload["workspace"]["path"])
    manifest, total = {}, 0
    for relative in payload["workspace"]["read_files"]:
        src = safe_path(source, relative)
        size = src.stat().st_size
        total += size
        if total > 32 * 1024 * 1024:
            raise ValueError("input_package_size_limit")
        with src.open("rb") as stream:
            raw = stream.read(min(size + 1, 32 * 1024 * 1024 + 1))
        if len(raw) != size:
            raise ValueError("source_changed_during_package")
        dst = safe_path(root, relative)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        manifest[relative] = hashlib.sha256(raw).hexdigest()
    for relative, digest in manifest.items():
        if hashlib.sha256(safe_path(source, relative).read_bytes()).hexdigest() != digest:
            raise ValueError("source_changed_during_package")
    (root.parent / "input-manifest.json").write_text(canonical(manifest), encoding="utf-8")
    remaining = request["limits"]["max_runtime"] - (time.monotonic() - started)
    if remaining <= 0:
        raise BudgetExceeded("package_deadline")
    cfg.max_total_runtime = min(cfg.max_total_runtime, remaining)
    cfg.max_attempts = request["limits"]["max_attempts"]
    cfg.models = [m for m in cfg.models if m.id in request["allowed_models"]]
    cfg.auto_usage = False  # Do not add an unrequested external query to a host task.
    calls, run_id, observed_tokens = 0, None, 0
    known_usage = False
    def factory(config, model):
        nonlocal calls
        if model.id not in request["allowed_models"]:
            raise ValueError("model_not_authorized")
        if calls >= request["limits"]["max_provider_calls"]:
            raise BudgetExceeded("parent_provider_calls_exhausted")
        calls += 1
        send("provider_call", {"count": calls, "model_id": model.id})
        return get_provider(config, model)
    def workspace(path):
        nonlocal run_id
        run_id = path.name
        send("workspace", {"run_id": run_id})
    def event(attempt, value):
        nonlocal observed_tokens, known_usage
        if value.kind == "usage" and type(value.data.get("tokens")) is int:
            observed_tokens += value.data["tokens"]
            known_usage = True
        phase = ("checking" if value.kind == "test" else "editing" if value.kind == "file_write" else
                 "reading" if value.kind == "file_read" else "tooling" if value.kind == "tool" else
                 "summarizing" if value.kind == "summary" else "executing")
        data = {"attempt": attempt, "event": value.kind, "phase": phase}
        if value.kind == "summary":
            data["text"] = str(value.data.get("text", ""))[:1600]
        elif value.kind in {"file_read", "file_write"}:
            data.update(path=str(value.data.get("path", ""))[:500])
            if value.kind == "file_write":
                data["change"] = value.data.get("change", "modified")
        elif value.kind == "test":
            data.update(name=str(value.data.get("name", ""))[:200], passed=value.data.get("passed") is True,
                        returncode=value.data.get("returncode"))
        elif value.kind == "tool":
            data.update(tool=str(value.data.get("command") or value.data.get("signature") or "tool")[:500],
                        returncode=value.data.get("returncode"), error=str(value.data.get("error", ""))[:500])
        elif value.kind == "usage":
            data["tokens"] = value.data.get("tokens")
        send("progress", data)
    outcome = Orchestrator(cfg, provider_factory=factory, on_workspace=workspace, on_event=event,
        on_route=lambda r: send("route", {"executor": r.executor, "reviewer": r.reviewer})).run(task, root)
    send("result", {"state": outcome.status, "run_id": run_id, "reason": outcome.reason[:500], "attempts": outcome.attempts,
        "provider_calls": calls, "tokens_observed": observed_tokens if known_usage else None,
        "tokens_complete": False, "api_cost": None, "applied": False})


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    try:
        raw = sys.stdin.buffer.read(1000001)
        if len(raw) > 1000000:
            raise ValueError("worker_request_size_limit")
        execute(json.loads(raw))
    except Exception as exc:
        send("error", {"reason": f"{type(exc).__name__}: {str(exc)[:300]}"})
        raise SystemExit(2)
