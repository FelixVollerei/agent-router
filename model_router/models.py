from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
import math

TASK_TYPES = {"mechanical_edit", "batch_edit", "feature", "refactor", "debugging",
              "performance_debugging", "architecture", "research", "reverse_engineering",
              "data_processing", "ui_asset", "test", "review", "other"}


@dataclass
class TaskProfile:
    task_type: str = "other"
    domain: str = "general"
    environment: str = "default"
    requirement_clarity: int = 2
    automatic_verifiability: int = 1
    human_verification_cost: int = 3
    failure_blast_radius: int = 3
    tool_complexity: int = 2
    repo_scope: int = 2
    domain_uncertainty: int = 3
    root_cause_uncertainty: int = 2
    reversibility: int = 2
    deadline_urgency: int = 0
    deadline_at: str | None = None
    production_blocker: bool = False
    production: bool = False
    critical: bool = False
    onsite: bool = False
    high_value: bool = False
    existing_reference_implementation: bool = False
    known_acceptance_criteria: bool = False
    previous_attempt_count: int = 0
    failed_executors: list[str] = field(default_factory=list)
    estimated_context_size: int = 0
    evidence: dict[str, str] = field(default_factory=dict)
    unresolved_questions: list[str] = field(default_factory=list)
    suggested_executor: str | None = None
    escalation_conditions: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"Unknown task_type: {self.task_type}")
        scales = ("requirement_clarity", "automatic_verifiability", "human_verification_cost",
                  "failure_blast_radius", "tool_complexity", "repo_scope", "domain_uncertainty",
                  "root_cause_uncertainty", "reversibility", "deadline_urgency")
        for key in scales:
            value = getattr(self, key)
            if type(value) is not int or not 0 <= value <= 5:
                raise ValueError(f"{key} must be an integer in 0..5")
        for key in ("production_blocker", "production", "critical", "onsite", "high_value",
                    "existing_reference_implementation", "known_acceptance_criteria"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} must be boolean")
        for key in ("previous_attempt_count", "estimated_context_size"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError(f"{key} must be nonnegative integer")
        if not isinstance(self.domain, str) or not self.domain.strip():
            raise ValueError("domain must be a nonempty string")
        if not isinstance(self.environment, str) or not self.environment.strip():
            raise ValueError("environment must be a nonempty string (e.g. scada-2023/harness-v1)")
        for key in ("failed_executors", "unresolved_questions", "escalation_conditions"):
            if not isinstance(getattr(self, key), list) or not all(isinstance(x, str) for x in getattr(self, key)):
                raise ValueError(f"{key} must be a string list")
        if not isinstance(self.evidence, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in self.evidence.items()):
            raise ValueError("evidence must map strings to strings")
        if self.deadline_at:
            deadline = datetime.fromisoformat(self.deadline_at.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                raise ValueError("deadline_at requires timezone, e.g. +07:00")

    def hours_left(self, now: datetime | None = None) -> float:
        if not self.deadline_at:
            return {0: math.inf, 1: 96, 2: 48, 3: 12, 4: 2, 5: .5}[self.deadline_urgency]
        return (datetime.fromisoformat(self.deadline_at.replace("Z", "+00:00")) -
                (now or datetime.now(timezone.utc))).total_seconds() / 3600

    def deadline_class(self) -> str:
        h = self.hours_left()
        return "<1h" if h < 1 else "<4h" if h < 4 else "4-24h" if h < 24 else "24-72h" if h <= 72 else ">72h"


@dataclass
class Task:
    request: str
    profile: TaskProfile
    goal: str = ""
    context: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)
    forbidden_files: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    manual_verification: bool = False
    mode: str = "balanced"
    force: str | None = None
    no_escalation: bool = False
    execution_prompt: str = ""
    selected_executor: str | None = None

    def __post_init__(self):
        if not isinstance(self.request, str) or not self.request.strip():
            raise ValueError("request must be nonempty")
        if self.mode not in {"cheap", "balanced", "deadline", "critical"}:
            raise ValueError("Invalid mode")
        if self.force not in {None, "deepseek", "sol", "astra", "ultra"}:
            raise ValueError("Invalid executor override")
        if not isinstance(self.execution_prompt, str) or len(self.execution_prompt) > 60000:
            raise ValueError("Invalid execution prompt")
        if self.selected_executor is not None and not isinstance(self.selected_executor, str):
            raise ValueError("Invalid selected executor")
        for key in ("context", "constraints", "acceptance_criteria", "allowed_files", "forbidden_files", "expected_files", "checks"):
            if not isinstance(getattr(self, key), list) or not all(isinstance(x, str) for x in getattr(self, key)):
                raise ValueError(f"{key} must be a string list")
        for key in ("manual_verification", "no_escalation"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} must be boolean")

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        data = dict(data)
        data["profile"] = TaskProfile(**data.get("profile", {}))
        return cls(**data)


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    model: str
    reasoning: str
    rank: int
    resource_cost: float
    prior_success: float
    expected_minutes: float
    scarce: bool = False


@dataclass
class Budget:
    max_debug_loops: int = 2
    max_failed_tests: int = 2
    max_no_progress_tool_calls: int = 10
    max_exploration_steps: int = 30
    max_runtime: float = 600
    max_context_growth: int = 64000  # UTF-8 bytes, not provider token counts

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Budget {key} must be finite and positive")


@dataclass
class Route:
    executor: str
    reviewer: str | None
    reasons: list[str]
    risk_score: float
    candidates: list[dict[str, Any]]
    budget: Budget
    next_executor: str | None


@dataclass
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verification:
    status: str  # passed, failed, needs_human
    checks: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    status: str
    run_dir: str
    executor: str
    reason: str
    attempts: int


def jsonable(value):
    return asdict(value)
