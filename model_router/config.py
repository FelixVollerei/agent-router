from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
import math

from .models import ModelSpec


DEFAULT_MODELS = [
    ModelSpec("deepseek", "deepseek", "deepseek-v4-pro", "", 0, .2, .94, 8),
    ModelSpec("sol_medium", "codex", "gpt-5.6-sol", "medium", 2, 2, .965, 7, True),
    ModelSpec("sol_high", "codex", "gpt-5.6-sol", "high", 3, 4, .98, 10, True),
    ModelSpec("astra", "codex", "gpt-6-astra", "high", 4, 9, .99, 12, True),
    ModelSpec("astra_ultra", "codex", "gpt-6-astra", "ultra", 5, 20, .995, 20, True),
]


@dataclass
class Config:
    state_dir: Path
    models: list[ModelSpec] = field(default_factory=lambda: list(DEFAULT_MODELS))
    providers: dict = field(default_factory=lambda: {
        "deepseek": {"kind": "managed_chat", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY"},
        "codex": {"kind": "codex_cli", "command": "codex"},
    })
    commands: dict[str, list[str]] = field(default_factory=dict)
    codex_budget_remaining: float | None = None
    auto_usage: bool = True
    codex_limit_id: str = "codex"
    max_attempts: int = 5
    max_total_runtime: float = 2400
    budget_overrides: dict = field(default_factory=dict)
    analyzer_provider: str = "deepseek"
    analyzer_model: str = "deepseek-v4-pro"
    analyzer_timeout: float = 30
    history_min_samples: int = 5
    time_weight: float = .2
    human_weight: float = 3
    failure_weight: float = 15
    review_cost_fraction: float = .35
    review_residual_risk: float = .5
    research: dict = field(default_factory=dict)
    evaluation_records: list[dict] = field(default_factory=list)
    workspaces: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        data = {}
        base = Path.cwd()
        if path:
            path = Path(path).resolve()
            base = path.parent
            with path.open("rb") as f:
                data = tomllib.load(f)
        settings = data.pop("router", {})
        state = Path(settings.pop("state_dir", ".router-state"))
        cfg = cls(state_dir=(base / state).resolve(), **settings)
        if "models" in data:
            cfg.models = [ModelSpec(**x) for x in data["models"]]
        cfg.providers.update(data.get("providers", {}))
        cfg.commands = data.get("commands", {})
        cfg.budget_overrides = data.get("budgets", {})
        cfg.research = data.get("research", {})
        cfg.evaluation_records = data.get("evaluations", [])
        cfg.workspaces = data.get("workspaces", {})
        for settings in cfg.workspaces.values():
            if "path" in settings:
                settings["path"] = str((base / settings["path"]).resolve())
        cfg.validate()
        return cfg

    def validate(self):
        from .catalog import validate_evaluations
        from .network import validate_url
        import re
        validate_evaluations(self.evaluation_records)
        if not isinstance(self.research, dict) or set(self.research) - {"api_key_env"}:
            raise ValueError("Invalid research settings")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.research.get("api_key_env", "BRAVE_SEARCH_API_KEY")):
            raise ValueError("Invalid search credential reference")
        for name, provider in self.providers.items():
            if not isinstance(provider, dict):
                raise ValueError("Provider settings must be objects")
            if set(provider) - {"kind", "base_url", "api_key_env", "command", "windows_sandbox", "thinking", "reasoning_effort", "max_output_tokens"}:
                raise ValueError("Provider accepts credential environment references, never inline secrets or arbitrary settings")
            if provider.get("kind") == "managed_chat":
                if validate_url(provider.get("base_url", ""), True).query:
                    raise ValueError("Provider base URL cannot carry query credentials")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", provider.get("api_key_env", "DEEPSEEK_API_KEY")):
                    raise ValueError("Invalid provider credential reference")
        for key in ("max_total_runtime", "analyzer_timeout", "time_weight", "human_weight", "failure_weight", "review_cost_fraction", "review_residual_risk"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be finite and positive")
        if self.review_residual_risk > 1 or self.review_cost_fraction > 1:
            raise ValueError("Review fractions must be in (0, 1]")
        if type(self.max_attempts) is not int or type(self.history_min_samples) is not int or self.history_min_samples < 1:
            raise ValueError("Attempt and sample counts must be positive integers")
        if self.codex_budget_remaining is not None and not 0 <= self.codex_budget_remaining <= 100:
            raise ValueError("codex_budget_remaining must be 0..100")
        if self.max_attempts < 1 or self.max_total_runtime <= 0:
            raise ValueError("Run budgets must be positive")
        ids = [m.id for m in self.models]
        if len(set(ids)) != len(ids) or not self.models:
            raise ValueError("Model IDs must be unique")
        for m in self.models:
            if not all(isinstance(s, str) and s.strip() for s in (m.id, m.provider, m.model)) or not isinstance(m.reasoning, str):
                raise ValueError("Invalid model identity")
            if type(m.scarce) is not bool or any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) for n in (m.prior_success, m.resource_cost, m.expected_minutes)):
                raise ValueError("Invalid model numeric fields")
            if m.provider not in self.providers or not 0 < m.prior_success < 1 or type(m.rank) is not int or m.rank < 0 or not math.isfinite(m.resource_cost) or m.resource_cost < 0 or not math.isfinite(m.expected_minutes) or m.expected_minutes <= 0:
                raise ValueError(f"Invalid model spec: {m.id}")
        for name, argv in self.commands.items():
            if not isinstance(argv, list) or not argv or not all(isinstance(s, str) for s in argv):
                raise ValueError(f"Command {name} must be an argv array, never a shell string")
        from .models import Budget
        for overrides in self.budget_overrides.values():
            Budget(**overrides)

    def model(self, key: str) -> ModelSpec:
        return next(m for m in self.models if m.id == key)
