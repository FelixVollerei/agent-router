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
# The v2 wire contract is a separate protocol id, not a flag on v1: a connection speaks exactly
# one closed schema, and which one is decided by the protocol it negotiated at `initialize`.
PROTOCOL_V2 = "router.jobs/v2"
# Newest first. This is the order a caller reads from `describe()` to decide what to ask for.
PROTOCOLS = (PROTOCOL_V2, PROTOCOL)
TERMINAL = {"verified", "needs_human", "blocked", "failed", "cancelled", "timed_out", "unknown"}
# The v1 closed payload set. It is frozen: v1 requests must keep validating exactly as before.
FIELDS = {"client_task_id", "idempotency_key", "session_id", "workspace_id", "task", "limits", "allowed_models", "upload_allowed"}
# v2 adds the two facts the host needs to make approval and revision visible across the boundary.
# They are evidence the host supplies and the peer records; neither carries authorization, and
# the peer never interprets them as permission.
WORK_REVISION_FIELD = "work_revision"
APPROVAL_FIELD = "approval"
APPROVAL_FIELDS = {"revision", "approval_id", "granted_at"}
FIELDS_V2 = FIELDS | {WORK_REVISION_FIELD, APPROVAL_FIELD}
SCHEMAS = {PROTOCOL: FIELDS, PROTOCOL_V2: FIELDS_V2}
# Artifact-manifest row schema the peer emits. Both artifact reply branches carry it, including
# the empty one, so a caller can classify the manifest before it reads a single row.
MANIFEST_VERSION = 1
# Diagnostic artifacts the peer declares in its own artifact manifest when this run produced them.
# They are named here so the declaration is data rather than a convention the caller has to guess:
# before this, a caller found them by probing these three file names inside the run directory.
DIAGNOSTIC_ARTIFACTS = ("events.jsonl", "task.json", "ESCALATION.md")
SETTLEMENT_SCHEMA_VERSION = {PROTOCOL: 1, PROTOCOL_V2: 2}
# What the peer can prove about a stop. `remote` is deliberately limited to "unknown": nothing in
# this service observes the remote provider, so claiming more would be inventing authority.
STOP_ACKNOWLEDGEMENTS = {"local": ("pending", "confirmed", "unconfirmed", "not_requested"),
    "remote": ("unknown",)}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("invalid_" + label)
    return value


def validate_submit(config, value, *, protocol=PROTOCOL):
    """Validate one `job.submit` payload against the schema of the negotiated protocol.

    The key set stays *closed* in both versions: a v1 connection cannot smuggle a v2 key in, and
    a v2 connection cannot fall back to the v1 shape. The default keeps every existing caller -
    including direct unit callers - on the v1 contract it already had.
    """
    fields = SCHEMAS.get(protocol)
    if fields is None:
        raise ValueError("unsupported_protocol")
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("submit_fields_must_match_v2" if protocol == PROTOCOL_V2 else "submit_fields_must_match_v1")
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
    if protocol == PROTOCOL_V2:
        # Recorded facts, checked for shape only. `revision` binds the approval to the exact work
        # package; the peer stores both and decides nothing about whether the approval is valid.
        identity(value[WORK_REVISION_FIELD], WORK_REVISION_FIELD)
        approval = value[APPROVAL_FIELD]
        if not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS:
            raise ValueError("invalid_approval")
        if approval["revision"] != value[WORK_REVISION_FIELD]:
            raise ValueError("approval_revision_mismatch")
        identity(approval["approval_id"], "approval_id")
        if not isinstance(approval["granted_at"], str) or not approval["granted_at"]:
            raise ValueError("invalid_approval_granted_at")
    # Canonical request stays distinct from server-enforced derived restrictions.
    normalized = {**value, "task": asdict(Task.from_dict(value["task"])), "allowed_models": sorted(allowed)}
    return normalized, task, copy.deepcopy(settings)


def request_hash(normalized, protocol=PROTOCOL):
    """The durable idempotency digest for one normalized request.

    v1 must stay byte-identical to the digest already stored for pre-upgrade jobs: otherwise a
    legitimate retry of an existing job would be reported as `idempotency_conflict`. The v2 digest
    prefixes the protocol and appends the work revision, so the two version spaces cannot collide,
    a different revision on the same identity is a conflict rather than a silent replay, and
    re-approval metadata (which legitimately changes) does not change the digest.
    """
    payload = {key: item for key, item in normalized.items() if key != APPROVAL_FIELD}
    if protocol == PROTOCOL:
        return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{protocol}|{canonical(payload)}|{payload.get(WORK_REVISION_FIELD) or ''}".encode("utf-8")).hexdigest()


def payload_digest(normalized, protocol=PROTOCOL):
    """The integrity binding for the whole payload of one version.

    This is deliberately *not* the idempotency digest: `request_hash` must keep answering only
    "same request?", which is why it ignores approval metadata. This digest covers the canonical
    payload including that metadata, so no v2 evidence sits outside an integrity binding, and it is
    recorded beside the idempotency hash rather than replacing it.
    """
    return hashlib.sha256(f"{protocol}|payload|{canonical(normalized)}".encode("utf-8")).hexdigest()


def stop_acknowledgement(state, result, *, requested=False):
    """What this peer can prove about a stop, in a shape a caller classifies instead of guessing."""
    result = result or {}
    if state in TERMINAL:
        local = "confirmed" if result.get("local_stopped") is True else "unconfirmed"
    else:
        local = "pending" if requested else "not_requested"
    return {"local": local, "remote": "unknown"}


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
