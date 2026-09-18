"""Router-owned v1 domain contract; independent of carrier and DSH implementation."""
from dataclasses import asdict
import copy
import hashlib
import json
import math
from pathlib import Path
import re

from .models import Task
from .workspace import is_link, permitted, safe_path

PROTOCOL = "router.jobs/v1"
TERMINAL = {"verified", "needs_human", "blocked", "failed", "cancelled", "timed_out", "unknown"}
FIELDS = {"client_task_id", "idempotency_key", "session_id", "workspace_id", "task", "limits", "allowed_models", "upload_allowed"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("invalid_" + label)
    return value


def validate_submit(config, value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("submit_fields_must_match_v1")
    value = copy.deepcopy(value)
    for name in ("client_task_id", "idempotency_key", "workspace_id"):
        identity(value[name], name)
    if value["session_id"] is not None:
        identity(value["session_id"], "session_id")
    settings = config.workspaces.get(value["workspace_id"])
    required = {"path", "read_files", "write_files", "checks", "allowed_models", "upload_allowed"}
    if not isinstance(settings, dict) or required - set(settings) or set(settings) - required - {"protected_files"}:
        raise ValueError("workspace_not_registered_or_incomplete")
    source = Path(settings["path"])
    if not source.is_absolute() or not source.is_dir() or any(is_link(p) for p in [source, *source.parents]):
        raise ValueError("invalid_workspace")
    source = source.resolve()
    if source == config.state_dir or source in config.state_dir.resolve().parents:
        raise ValueError("state_must_be_outside_workspace")
    for field in ("read_files", "write_files", "checks", "allowed_models", "protected_files"):
        items = settings.get(field, [])
        if not isinstance(items, list) or len(items) > 2048 or not all(isinstance(x, str) and x for x in items):
            raise ValueError("invalid_workspace_" + field)
    for name in settings["read_files"]:
        if any(c in name for c in "*?[") or not safe_path(source, name).is_file():
            raise ValueError("read_files_requires_existing_explicit_files")
    if len(set(settings["read_files"])) != len(settings["read_files"]):
        raise ValueError("duplicate_input_file")
    if type(value["upload_allowed"]) is not bool or type(settings["upload_allowed"]) is not bool:
        raise ValueError("invalid_upload_permission")
    if not value["upload_allowed"] or not settings["upload_allowed"]:
        raise ValueError("model_upload_not_authorized")
    task = Task.from_dict(value["task"])
    if not task.allowed_files:
        raise ValueError("write_scope_required")
    for name in task.allowed_files:
        safe_path(source, name)
        if name not in settings["write_files"] and (any(c in name for c in "*?[") or not permitted(name, settings["write_files"], [])):
            raise ValueError("write_scope_expanded")
    if not set(task.checks) <= set(settings["checks"]) or not set(task.checks) <= config.commands.keys():
        raise ValueError("check_not_authorized")
    protected = set(settings.get("protected_files", []))
    # A check script passed as argv is immutable; more complex suites register their oracle files.
    for name in task.checks:
        protected.update(arg for arg in config.commands[name][1:] if arg in settings["read_files"])
    if not protected <= set(settings["read_files"]):
        raise ValueError("protected_file_not_in_package")
    task.forbidden_files = sorted(set(task.forbidden_files) | protected)
    allowed = value["allowed_models"]
    if not isinstance(allowed, list) or not allowed or len(allowed) > 100 or not all(isinstance(m, str) for m in allowed):
        raise ValueError("allowed_models_required")
    if len(set(allowed)) != len(allowed) or not set(allowed) <= set(settings["allowed_models"]) or not set(allowed) <= {m.id for m in config.models}:
        raise ValueError("model_scope_expanded")
    if task.selected_executor is not None and task.selected_executor not in allowed:
        raise ValueError("selected_executor_not_authorized")
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != {"max_runtime", "max_attempts", "max_provider_calls"}:
        raise ValueError("unsupported_or_missing_budget")
    for key, number in limits.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise ValueError("invalid_budget")
        if key != "max_runtime" and type(number) is not int:
            raise ValueError("budget_count_requires_integer")
    if limits["max_runtime"] > config.max_total_runtime or limits["max_attempts"] > config.max_attempts or limits["max_provider_calls"] > 2 * config.max_attempts:
        raise ValueError("budget_expanded")
    if task.profile.hours_left() <= 0:
        raise ValueError("deadline_expired")
    # Canonical request stays distinct from server-enforced derived restrictions.
    normalized = {**value, "task": asdict(Task.from_dict(value["task"])), "allowed_models": sorted(allowed)}
    return normalized, task, copy.deepcopy(settings)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
