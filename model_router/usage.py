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
