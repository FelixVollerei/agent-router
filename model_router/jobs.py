"""Headless durable execution adapter. Does not use the desktop HTTP service."""
from dataclasses import asdict
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

from . import __version__
from .catalog import configured_catalog
from .job_contract import (APPROVAL_FIELD, MANIFEST_VERSION, PROTOCOL, PROTOCOL_V2, PROTOCOLS,
    SETTLEMENT_SCHEMA_VERSION, TERMINAL, WORK_REVISION_FIELD, canonical, fingerprint, identity,
    payload_digest, request_hash, stop_acknowledgement, validate_submit)
from .job_process import ProcessContainment, ServiceLock
from .job_store import JobStore
from .process import LineProcess
from .workspace import inventory, safe_path


class JobService:
    def __init__(self, config, client_id, worker_command=None):
        config.validate()
        self.config = copy.deepcopy(config)
        self.client = identity(client_id, "client")
        self.state = config.state_dir.resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.owner = ServiceLock(self.state / "jobs.lock")
        self.store = JobStore(self.state / "jobs.sqlite3")
        self.store.recover()
        self.lock = threading.RLock()
        self.active, self.thread, self.cancel_event = None, None, None
        self.closed = False
        # Injectable only in Python tests, never selected from RPC or model input.
        self.worker_command = worker_command or [sys.executable, "-B", "-X", "utf8", "-m", "model_router.job_worker"]

    def describe(self, protocol=PROTOCOL):
        return {"protocol": protocol, "protocol_versions": list(PROTOCOLS),
            "server_version": __version__, "execution_available": os.name == "nt",
            "methods": ["catalog.list", "job.submit", "job.lookup", "job.get", "job.events", "job.cancel", "job.artifacts"],
            "features": {"durable_idempotency": True, "event_replay": True, "single_slot": True, "resume_execution": False,
                "steering": False, "promotion": False, "dsh_standard_conformance": False, "autonomous": False,
                "lookup_by_parent": True, "stop_acknowledgement": True, "protocol_upgrade": True},
            "budgets": {"max_runtime": "local_process_supervision", "max_attempts": "enforced",
                "max_provider_calls": "provider_invocations_including_review", "tokens": "observation_only", "api_cost": "unknown"},
            "cancellation": {"local": "windows_job_object", "remote_provider": "not_observable_in_v2"},
            "scope": {"input_packaging": "explicit_files_only", "managed_tools": "package_only",
                "codex_read_isolation": "depends_on_cli_os_sandbox_not_proven_by_packaging",
                "codex_write_scope": "sandbox_plus_post_verification", "trusted_checks": True},
            "limits": {"frame_bytes": 200000, "events_per_page": 100, "jobs": 4096, "events_per_job": 4096},
            "manifest_version": MANIFEST_VERSION}

    def submit(self, request, *, protocol=PROTOCOL):
        with self.lock:
            if self.closed:
                raise ValueError("connection_closed")
            normalized, task, settings = validate_submit(self.config, request, protocol=protocol)
            digest = request_hash(normalized, protocol)
            # Return a duplicate before consulting the transient slot; terminal state may
            # have committed just before the supervisor releases its process ownership.
            with self.store.lock:
                previous = self.store.db.execute("SELECT id,request_hash FROM jobs WHERE client=? AND (task_id=? OR idem=?)",
                    (self.client, normalized["client_task_id"], normalized["idempotency_key"])).fetchall()
            if previous:
                if len(previous) != 1 or previous[0]["request_hash"] != digest:
                    raise ValueError("idempotency_conflict")
                return {**self.get(previous[0]["id"]), "duplicate": True}
            if self.active is not None:
                raise ValueError("busy_or_quarantined_unknown")
            # v2 carries facts the idempotency digest deliberately ignores, so it also records an
            # independent binding over the whole payload. v1 has no such evidence and keeps its
            # single digest, which is what makes an old retry stay byte-identical.
            binding = payload_digest(normalized, protocol) if protocol != PROTOCOL else None
            identifier, duplicate = self.store.admit(self.client, normalized, digest, protocol=protocol,
                work_revision=normalized.get(WORK_REVISION_FIELD), approval=normalized.get(APPROVAL_FIELD),
                payload_digest=binding)
            if duplicate:
                return {**self.get(identifier), "duplicate": True}
            self.active = identifier
            self.cancel_event = threading.Event()
            payload = {"request": normalized, "task": asdict(task), "workspace": settings,
                "package": str(self.state / "packages" / identifier / "input"), "config": asdict(self.config)}
            payload["config"]["state_dir"] = str(self.state / "execution")
            self.thread = threading.Thread(target=self._execute, args=(identifier, payload, self.cancel_event), daemon=True, name="router-job-supervisor")
            self.thread.start()
            return {**self.get(identifier), "duplicate": False}

    def _execute(self, identifier, payload, cancelled):
        start = time.monotonic()
        child, containment, run_id = None, None, None
        final, reason, worker_result = "unknown", "worker_did_not_settle", None
        try:
            child = LineProcess(self.worker_command, Path(__file__).resolve().parent.parent)
            containment = ProcessContainment(child.proc)
            self.store.update(identifier, "running")
            child.write_input(canonical(payload))
            while True:
                if cancelled.is_set():
                    final, reason = "cancelled", "host_requested_stop"
                    break
                if time.monotonic() - start >= payload["request"]["limits"]["max_runtime"]:
                    final, reason = "timed_out", "parent_runtime_exhausted"
                    break
                item = child.poll(.05)
                if item and item[0] == "overflow":
                    raise ValueError("worker_frame_limit")
                if item and item[0] == "stdout":
                    value = json.loads(item[1])
                    kind, data = value.get("kind"), value.get("data")
                    if not isinstance(data, dict):
                        raise ValueError("invalid_worker_event")
                    if kind == "workspace":
                        if not re.fullmatch(r"[a-f0-9]{32}", data.get("run_id", "")):
                            raise ValueError("invalid_run_identity")
                        run_id = data["run_id"]
                        self.store.update(identifier, "running", {"run_id": run_id}, run_id)
                    elif kind == "result":
                        if data.get("state") not in {"verified", "needs_human", "blocked", "cancelled"} or data.get("run_id") != run_id or run_id is None:
                            raise ValueError("invalid_worker_result")
                        worker_result = data
                    elif kind == "error":
                        final, reason = "blocked", str(data.get("reason", "worker_error"))[:500]
                    elif kind in {"progress", "provider_call", "route"}:
                        self.store.event(identifier, kind, data)
                    else:
                        raise ValueError("unknown_worker_event")
                if child.finished():
                    if worker_result and child.proc.returncode == 0:
                        final, reason = worker_result["state"], worker_result["reason"]
                    elif final != "blocked":
                        final, reason = "unknown", "worker_exited_without_valid_result"
                    break
        except Exception as exc:
            final = "blocked" if containment is None else "unknown"
            reason = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            stopped = False
            try:
                if containment:
                    containment.close()  # Kill descendants before settling the durable job.
                if child:
                    child.close()
                    stopped = child.proc.poll() is not None
            except (OSError, ValueError, TimeoutError):
                final, reason = "unknown", "local_cleanup_unconfirmed"
            result = {**(worker_result or {}), "reason": reason, "local_stopped": stopped,
                "remote_stopped": None, "applied": False, "runtime": time.monotonic() - start,
                "stop_acknowledgement": stop_acknowledgement(final, {"local_stopped": stopped})}
            result.pop("state", None)
            job_protocol = self.store.get(self.client, identifier)["protocol"]
            self.store.update(identifier, final, result, run_id,
                settlement_schema_version=SETTLEMENT_SCHEMA_VERSION.get(job_protocol, 1))
            with self.lock:
                self.active = None

    def get(self, identifier):
        row = self.store.get(self.client, identifier)
        req = row["request"]
        value = {"job_id": row["id"], "client_task_id": row["task_id"], "session_id": req["session_id"],
            "state": row["state"], "run_id": row["run_id"], "result": row["result"], "applied": False}
        # Only a v2 job has evidence that needs its own binding, so only a v2 reply carries it and
        # a v1 reply keeps exactly the keys it had.
        if row.get("payload_digest"):
            value["payload_digest"] = row["payload_digest"]
        return value

    def lookup(self, client_task_id):
        identity(client_task_id,"client_task_id")
        with self.store.lock:
            row = self.store.db.execute("SELECT id FROM jobs WHERE client=? AND task_id=?",
                (self.client,client_task_id)).fetchone()
        return {"found":row is not None,"job":self.get(row["id"]) if row else None}

    def cancel(self, identifier):
        with self.lock:
            row = self.get(identifier)
            requested = row["state"] not in TERMINAL
            if requested:
                if identifier != self.active or self.cancel_event is None:
                    raise ValueError("active_worker_not_owned")
                self.store.update(identifier, "cancel_requested")
                self.cancel_event.set()
            settled = self.get(identifier)
            # The acknowledgement states what this peer can prove right now: a request that is
            # pending, an already-settled job, or a stop that was actually confirmed. The remote
            # half is always "unknown" because nothing here observes the provider.
            return {**settled, "cancel_requested": requested,
                "stop_acknowledgement": stop_acknowledgement(settled["state"], settled["result"],
                    requested=requested)}

    def artifacts(self, identifier):
        row = self.get(identifier)
        if row["state"] not in TERMINAL or not row["run_id"]:
            # The empty branch carries the manifest version too: a caller classifies the manifest
            # before it reads a row, so "no artifacts" must not look like "no declaration".
            return {"artifacts": [], "applied": False, "manifest_version": MANIFEST_VERSION}
        root = self.state / "execution" / "runs" / row["run_id"]
        result = []
        candidates = [p.name for p in root.iterdir() if p.is_file() and (p.name in {"changes.diff", "result.json", "verified-hashes.json"} or re.fullmatch(r"verification-\d+\.json", p.name))]
        work = root / "work"
        actual = inventory(work)
        expected = json.loads((root / "verified-hashes.json").read_text(encoding="utf-8")) if row["state"] == "verified" else None
        if expected is not None and actual != expected:
            raise ValueError("artifact_changed_after_verification")
        candidates.extend("work/" + p for p in actual)
        if len(candidates) > 2048:
            raise ValueError("artifact_count_limit")
        for relative in sorted(candidates):
            path = safe_path(root, relative)
            size = path.stat().st_size
            if size > 32 * 1024 * 1024:
                raise ValueError("artifact_size_limit")
            result.append({"ref": f"router:{identifier}:{relative}", "relative_path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": size})
        return {"artifacts": result, "run_root": str(root), "verified_candidate": row["state"] == "verified",
            "applied": False,
            # The row schema this manifest was built under, so a stored manifest stays readable
            # after the peer moves on to another version.
            "manifest_version": MANIFEST_VERSION}

    def close(self):
        with self.lock:
            self.closed = True
            if self.active and self.cancel_event:
                self.cancel_event.set()
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                # Do not unlock a state directory while an owned supervisor may still write it.
                raise RuntimeError("job_supervisor_did_not_stop")
        self.store.close()
        self.owner.close()


def serve(config, client_id, stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    service, initialized, protocol = JobService(config, client_id), False, None
    params_by_method = {"catalog.list": set(), "job.submit": None, "job.lookup": {"client_task_id"}, "job.get": {"job_id"},
        "job.cancel": {"job_id"}, "job.artifacts": {"job_id"}, "job.events": {"job_id", "after", "limit"}}
    def reject_constants(value):
        raise ValueError("non_finite_json")
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    try:
        while True:
            raw = stdin.readline(200001)
            if not raw:
                break
            identifier = None
            try:
                if len(raw) > 200000 or not raw.endswith(b"\n"):
                    raise ValueError("frame_limit_or_unterminated_frame")
                request = json.loads(raw, parse_constant=reject_constants, object_pairs_hook=unique_pairs)
                if not isinstance(request, dict) or set(request) - {"jsonrpc", "id", "method", "params"} or request.get("jsonrpc") != "2.0":
                    raise ValueError("invalid_jsonrpc_request")
                candidate_id = request.get("id")
                if type(candidate_id) not in {int, str} or len(str(candidate_id)) > 128:
                    raise ValueError("request_id_required")
                identifier = candidate_id
                method, params = request.get("method"), request.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("object_params_required")
                if method == "initialize":
                    # Every version this peer implements is accepted, and a second initialize is
                    # the documented upgrade step rather than an error: a caller confirms the
                    # legacy version first, reads `protocol_versions`, then asks for the newer one.
                    if set(params) != {"protocol"} or params["protocol"] not in PROTOCOLS:
                        raise ValueError("incompatible_protocol")
                    initialized, protocol = True, params["protocol"]
                    result = service.describe(protocol)
                elif not initialized:
                    raise ValueError("initialize_required")
                elif method not in params_by_method:
                    raise ValueError("method_not_supported")
                else:
                    fields = params_by_method[method]
                    if fields is not None and set(params) != fields:
                        raise ValueError("method_fields_must_match_v1")
                    if method == "catalog.list":
                        result = configured_catalog(config)
                    elif method == "job.submit":
                        result = service.submit(params, protocol=protocol)
                    elif method == "job.lookup":
                        result = service.lookup(params["client_task_id"])
                    elif method == "job.events":
                        result = service.store.events(service.client, params["job_id"], params["after"], params["limit"])
                    else:
                        result = getattr(service, method.split(".")[1])(params["job_id"])
                response = {"jsonrpc": "2.0", "id": identifier, "result": result}
                if len(canonical(response).encode("utf-8")) > 200000:
                    raise ValueError("response_limit")
            except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
                response = {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": str(exc)[:300]}}
            stdout.write(canonical(response) + "\n")
            stdout.flush()
            if len(raw) > 200000:
                break
    finally:
        service.close()
