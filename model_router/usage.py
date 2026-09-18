"""Read-only Codex app-server JSON-RPC quota lookup; never redeems quota."""
import json
import time

from .process import LineProcess


def remaining_percent(result, limit_id="codex"):
    buckets = result.get("rateLimitsByLimitId")
    if buckets is not None:
        bucket = buckets.get(limit_id)
    else:
        bucket = result.get("rateLimits")
    if not bucket:
        return None
    percentages = [100 - w["usedPercent"] for key in ("primary", "secondary")
                   if isinstance((w := bucket.get(key)), dict) and isinstance(w.get("usedPercent"), (int, float))]
    return max(0, min(100, min(percentages))) if percentages else None


def read_usage(command="codex", timeout=8):
    child = LineProcess([command, "app-server", "--listen", "stdio://"])
    start = time.monotonic()
    def response(request_id):
        while time.monotonic() - start < timeout:
            line = child.poll()
            if line and line[0] == "stdout":
                try:
                    value = json.loads(line[1])
                except json.JSONDecodeError:
                    continue
                if value.get("id") == request_id and ("result" in value or "error" in value):
                    if "error" in value:
                        raise RuntimeError(str(value["error"])[:500])
                    return value["result"]
            if child.finished():
                raise RuntimeError("Codex app server exited")
        raise TimeoutError("Codex quota read timed out")
    try:
        child.send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "engineering_model_router", "version": "0.1.0"}}})
        response(1)
        child.send({"method": "initialized"})
        child.send({"id": 2, "method": "account/rateLimits/read"})
        return response(2)
    finally:
        child.close()


def read_models(command="codex", timeout=15, cancel_event=None):
    from .monitor import RunCancelled
    child = LineProcess([command, "app-server", "--listen", "stdio://"])
    start = time.monotonic()
    def response(identifier):
        while time.monotonic() - start < timeout:
            if cancel_event is not None and cancel_event.is_set():
                raise RunCancelled("Catalog read cancelled")
            item = child.poll(.05)
            if item and item[0] == "overflow":
                raise ValueError("Model catalog frame limit")
            if item and item[0] == "stdout":
                data = json.loads(item[1])
                if data.get("id") == identifier:
                    if "error" in data:
                        raise RuntimeError("Codex model/list failed")
                    return data["result"]
            if child.finished():
                raise RuntimeError("Codex app server exited")
        raise TimeoutError("Codex model/list timed out")
    try:
        child.send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "engineering_model_router", "version": "0.4.0"}}})
        response(1)
        child.send({"method": "initialized"})
        rows, cursor, seen = [], None, set()
        for identifier in range(2, 22):
            params = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            child.send({"id": identifier, "method": "model/list", "params": params})
            page = response(identifier)
            if not isinstance(page.get("data"), list):
                raise ValueError("Invalid model/list response")
            rows.extend(page["data"])
            if len(rows) > 1000:
                raise ValueError("Model catalog size limit")
            cursor = page.get("nextCursor")
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("Invalid/repeated model catalog cursor")
            seen.add(cursor)
        raise ValueError("Model catalog page limit")
    finally:
        child.close()
