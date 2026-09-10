"""Opt-in real provider smoke test on the disposable demo only. No keys in files.

python -B -m examples.smoke deepseek
python -B -m examples.smoke codex
"""
from dataclasses import asdict
import json
from pathlib import Path
import sys

from model_router.analyzer import Analyzer
from model_router.config import Config
from model_router.models import Task
from model_router.orchestrator import Orchestrator


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    provider = sys.argv[1]
    if provider not in {"deepseek", "codex"}:
        raise SystemExit("Choose deepseek or codex")
    root = Path(__file__).resolve().parent.parent
    cfg = Config.load(root / "router.example.toml")
    cfg.state_dir = root / ".router-dev" / ("live-" + provider)
    cfg.max_total_runtime = 180
    cfg.max_attempts = 1
    cfg.budget_overrides["default"] = {"max_runtime": 120}
    task = Task.from_dict(json.loads((root / "examples" / "batch-task.json").read_text(encoding="utf-8")))
    task.force = "deepseek" if provider == "deepseek" else "sol"
    task.no_escalation = True
    if provider == "deepseek":
        analyzed = Analyzer(cfg).analyze(task.request + " 验收方式：check.py自动核对全部100个变量名和值。仅修改variables.json，结果在隔离副本中可恢复。有明确参考规则。", root / "examples" / "demo_repo")
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / "analyzed-task.json").write_text(json.dumps(asdict(analyzed), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"analyzer": "passed", "task_type": analyzed.profile.task_type}, ensure_ascii=False), flush=True)
    result = Orchestrator(cfg).run(task, root / "examples" / "demo_repo")
    print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
    return 0 if result.status == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
