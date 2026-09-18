"""Small synchronous host client. The caller owns authorization and parent budgets."""
import json
import threading
import time

from .job_contract import PROTOCOL, canonical
from .process import LineProcess


class JobClient:
    def __init__(self, argv, cwd=None, timeout=15):
        self.child = LineProcess(argv, cwd)
        self.timeout, self.sequence = timeout, 0
        self.lock = threading.Lock()
        try:
            self.description = self.call("initialize", {"protocol": PROTOCOL})
            if self.description.get("protocol") != PROTOCOL:
                raise ValueError("incompatible_protocol")
        except BaseException:
            self.close()
            raise

    def call(self, method, params=None):
        with self.lock:
            self.sequence += 1
            identifier = self.sequence
            value = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}
            if len(canonical(value).encode("utf-8")) + 1 > 200000:
                raise ValueError("frame_limit")
            self.child.proc.stdin.write(canonical(value) + "\n")
            self.child.proc.stdin.flush()
            end = time.monotonic() + self.timeout
            while time.monotonic() < end:
                item = self.child.poll(.05)
                if item and item[0] == "overflow":
                    raise ValueError("response_frame_limit")
                if item and item[0] == "stdout":
                    response = json.loads(item[1])
                    # A response from a previously timed-out request is not a new admission.
                    if response.get("id") != identifier:
                        continue
                    if "error" in response:
                        raise ValueError(response["error"]["message"])
                    return response["result"]
                if self.child.finished():
                    raise RuntimeError("router_connection_closed; execution state may be unknown")
            raise TimeoutError("router_response_timeout; query/retry using the original task identity")

    def close(self):
        if not self.child.proc.stdin.closed:
            self.child.proc.stdin.close()
        try:
            self.child.proc.wait(timeout=20)
        finally:
            self.child.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
