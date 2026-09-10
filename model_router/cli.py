from dataclasses import asdict
import argparse
import json
from pathlib import Path
import shutil
import sys

from .analyzer import Analyzer, parse_overrides
from .config import Config
from .experience import ExperienceStore
from .handoff import build_prompt
from .models import Task
from .orchestrator import Orchestrator
from .policy import Policy
from .usage import read_usage, remaining_percent
from .workspace import promote


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Engineering Model Router — cheapest safe model first")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    analyze = sub.add_parser("analyze", help="Read-only LLM Task Analyzer; write a task for inspection")
    analyze.add_argument("request")
    analyze.add_argument("--repo", type=Path, default=Path.cwd())
    analyze.add_argument("--offline", action="store_true", help="Conservative unknown-facts profile, no LLM")
    analyze.add_argument("--out", type=Path)
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("--task", type=Path, required=True)
        p.add_argument("--override", default="", help="Leading slash overrides, e.g. /deadline /no-escalation")
        if name == "run":
            p.add_argument("--repo", type=Path, required=True)
        else:
            p.add_argument("--prompt", action="store_true")
    sub.add_parser("doctor", help="Read-only local provider/config capability report")
    sub.add_parser("usage", help="Read Codex app-server quota; never consumes a reset")
    sub.add_parser("history", help="Export abstract experience rows")
    p = sub.add_parser("promote", help="Explicitly copy verified result to unchanged original workspace")
    p.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        cfg = Config.load(args.config)
        if args.state_dir:
            cfg.state_dir = args.state_dir.resolve()
        if args.command == "analyze":
            task = Analyzer(cfg).analyze(args.request, args.repo.resolve(), not args.offline)
            if args.out:
                args.out.write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8")
            output(asdict(task))
        elif args.command in {"plan", "run"}:
            task = Task.from_dict(json.loads(args.task.read_text(encoding="utf-8-sig")))
            request, options = parse_overrides(args.override + " " + task.request)
            task.request = request.strip()
            for k, v in options.items():
                setattr(task, k, v)
            if not task.checks or not task.acceptance_criteria or task.manual_verification:
                task.profile.automatic_verifiability = min(task.profile.automatic_verifiability, 1)
            if args.command == "run":
                def explain(route):
                    label = route.executor + (" + " + route.reviewer + " review" if route.reviewer else "")
                    print("Routing: " + label, file=sys.stderr)
                    for reason in route.reasons:
                        print("  - " + reason, file=sys.stderr)
                    b = route.budget
                    print(f"Budget: {b.max_runtime:g}s; {b.max_failed_tests} failed tests; {b.max_no_progress_tool_calls} no-progress calls", file=sys.stderr)
                    print(f"Escalation: {route.next_executor or 'stop / needs explicit action'}", file=sys.stderr)
                result = Orchestrator(cfg, on_route=explain).run(task, args.repo)
                output(asdict(result))
                return 0 if result.status == "verified" else 2
            store = ExperienceStore(cfg.state_dir / "experience.sqlite3")
            try:
                route = Policy(cfg, store).decide(task)
                output({"profile": asdict(task.profile), "route": asdict(route)})
                if args.prompt:
                    print(build_prompt(task, route, commands=cfg.commands))
            finally:
                store.close()
        elif args.command == "usage":
            result = read_usage()
            output({"remaining_percent": remaining_percent(result, cfg.codex_limit_id), "raw": result})
        elif args.command == "doctor":
            import os
            output({"python": sys.version.split()[0], "state_dir": str(cfg.state_dir),
                    "providers": {name: {"kind": p.get("kind"),
                                         "executable_found": bool(shutil.which(p.get("command", "codex"))) if p.get("kind") == "codex_cli" else None,
                                         "api_key_present": bool(os.environ.get(p.get("api_key_env", "DEEPSEEK_API_KEY"))) if p.get("kind") == "managed_chat" else None}
                                  for name, p in cfg.providers.items()},
                    "models": [asdict(m) for m in cfg.models],
                    "note": "Model IDs are configurable; local CLI presence is not proof of account model entitlement."})
        elif args.command == "history":
            store = ExperienceStore(cfg.state_dir / "experience.sqlite3")
            try:
                output(store.export())
            finally:
                store.close()
        else:
            output({"promoted_files": promote(args.run_dir)})
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, StopIteration) as exc:
        print(f"model-router: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
