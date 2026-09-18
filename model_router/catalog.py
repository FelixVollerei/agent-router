"""Provider facts and reviewed evaluation evidence, separate from routing permission."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
import re

from .network import fetch, validate_url
from .usage import read_models


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def configured_catalog(config):
    return {"observed_at": utc_now(), "source": "host_configuration", "models": [
        {**asdict(m), "availability": "unverified", "selection": "configured",
         "tools": "managed_allowlist" if config.providers[m.provider].get("kind") == "managed_chat" else "codex_sandbox"}
        for m in config.models]}


def discover(config, provider, cancel_event=None):
    if provider not in config.providers:
        raise ValueError("unknown_provider")
    settings = config.providers[provider]
    kind = settings.get("kind")
    result = {"provider": provider, "observed_at": utc_now(), "models": [], "status": "unavailable"}
    try:
        if kind == "codex_cli":
            result["source"] = "codex:model/list"
            rows = read_models(settings.get("command", "codex"), timeout=15, cancel_event=cancel_event)
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("model"), str):
                    raise ValueError("invalid_model_catalog")
                efforts = row.get("supportedReasoningEfforts", [])
                result["models"].append({"model": row["model"], "name": str(row.get("displayName", row["model"]))[:200],
                    "reasoning_efforts": [e["reasoningEffort"] for e in efforts if isinstance(e, dict) and isinstance(e.get("reasoningEffort"), str)],
                    "selectable": row.get("hidden") is not True})
        elif kind == "managed_chat":
            url = settings.get("base_url", "").rstrip("/") + "/models"
            validate_url(url, True)
            result["source"] = url
            value = json.loads(fetch({"url": url, "allow_loopback": True,
                "api_key_env": settings.get("api_key_env", "DEEPSEEK_API_KEY")}, timeout=15, cancel_event=cancel_event)["text"])
            rows = value.get("data")
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ValueError("invalid_model_catalog")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                    raise ValueError("invalid_model_catalog")
                result["models"].append({"model": row["id"], "name": row["id"], "reasoning_efforts": None, "selectable": None})
        else:
            raise ValueError("discovery_unsupported")
        result["status"] = "observed"
        result["note"] = "Discovery is not execution authorization, pricing, capability rank or successful inference."
    except (ValueError, OSError, RuntimeError) as exc:
        result["error"] = str(exc)[:300]
        result["models"] = []
    return result


EVIDENCE_FIELDS = {"benchmark", "version", "task_type", "metric", "score", "min", "max", "higher_is_better",
                   "provider", "model", "reasoning", "source_url", "evaluated_at", "reviewed"}


def validate_evaluations(rows):
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("evaluation_records must be a list of at most 500 records")
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != EVIDENCE_FIELDS:
            raise ValueError("Evaluation record fields do not match v1")
        for k in EVIDENCE_FIELDS - {"score", "min", "max", "reviewed", "higher_is_better"}:
            if not isinstance(row[k], str) or len(row[k]) > 4096 or (k != "reasoning" and not row[k]):
                raise ValueError("Invalid evaluation string: " + k)
        for k in ("score", "min", "max"):
            if isinstance(row[k], bool) or not isinstance(row[k], (int, float)) or not math.isfinite(row[k]):
                raise ValueError("Invalid evaluation score")
        if not row["min"] <= row["score"] <= row["max"] or row["max"] <= row["min"]:
            raise ValueError("Invalid evaluation scale")
        if type(row["reviewed"]) is not bool or type(row["higher_is_better"]) is not bool:
            raise ValueError("Evaluation flags must be booleans")
        validate_url(row["source_url"])
        date = datetime.fromisoformat(row["evaluated_at"].replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("Evaluation date requires timezone")
        key = tuple(row[k] for k in ("benchmark", "version", "task_type", "metric", "provider", "model", "reasoning"))
        if key in identities:
            raise ValueError("Duplicate evaluation identity; replace the prior record explicitly")
        identities.add(key)


def evaluation_context(config, task, now=None):
    now = now or datetime.now(timezone.utc)
    groups, ignored = {}, []
    models = {(m.provider, m.model, m.reasoning): m.id for m in config.models}
    for r in config.evaluation_records:
        model_id = models.get((r["provider"], r["model"], r["reasoning"]))
        age = (now - datetime.fromisoformat(r["evaluated_at"].replace("Z", "+00:00"))).total_seconds()
        reason = ("not_reviewed" if not r["reviewed"] else "model_mismatch" if not model_id else
                  "task_mismatch" if r["task_type"] != task.profile.task_type else
                  "stale_or_future" if age < -300 or age > 90 * 86400 else None)
        if reason:
            ignored.append({"benchmark": r["benchmark"], "model": r["model"], "reason": reason})
            continue
        group = tuple(r[k] for k in ("benchmark", "version", "task_type", "metric", "min", "max", "higher_is_better"))
        groups.setdefault(group, []).append((model_id, r))
    usable = {}
    for members in groups.values():
        if len({mid for mid, r in members}) < 2:
            ignored.extend({"benchmark": r["benchmark"], "model": r["model"], "reason": "no_comparable_peer"} for _, r in members)
            continue
        for model_id, r in members:
            score = (r["score"] - r["min"]) / (r["max"] - r["min"])
            if not r["higher_is_better"]:
                score = 1 - score
            usable.setdefault(model_id, []).append({"quality": score, "record": r})
    return usable, ignored
