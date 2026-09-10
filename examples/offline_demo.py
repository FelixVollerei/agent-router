"""Explicit deterministic test fixture. It does not call or impersonate real models."""
from dataclasses import asdict
import json
from pathlib import Path

from model_router.config import Config
from model_router.models import Event, Task
from model_router.orchestrator import Orchestrator


class DemoFixture:
    def run(self, **kw):
        path = kw["work"] / "variables.json"
        if kw["model"].rank == 0:
            # Demonstrate one credible failure, preserving the first 50 entries as information.
            path.write_text(json.dumps({f"NEW_{i:03d}": i for i in range(50)}), encoding="utf-8")
            kw["emit"](Event("hypothesis", {"category": "known_fact", "text": "Source keys and values follow a 0..99 integer sequence", "signature": "source-sequence"}))
        else:
            assert "Current Failure" in kw["prompt"]
            path.write_text(json.dumps({f"NEW_{i:03d}": i for i in range(100)}, indent=2), encoding="utf-8")
        kw["emit"](Event("summary", {"text": "Deterministic offline fixture finished; host must verify"}))


def main():
    root = Path(__file__).resolve().parent.parent
    cfg = Config.load(root / "router.example.toml")
    cfg.state_dir = root / ".router-dev" / "offline-demo"
    cfg.auto_usage = False
    cfg.codex_budget_remaining = 100
    task = Task.from_dict(json.loads((root / "examples" / "batch-task.json").read_text(encoding="utf-8")))
    result = Orchestrator(cfg, lambda *_: DemoFixture()).run(task, root / "examples" / "demo_repo")
    print(json.dumps({"fixture_only": True, **asdict(result)}, ensure_ascii=True))
    return 0 if result.status == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
