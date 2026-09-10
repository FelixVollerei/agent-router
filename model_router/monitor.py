from collections import Counter, defaultdict
import hashlib
import json
import time


class BudgetExceeded(RuntimeError):
    pass


class RunCancelled(BudgetExceeded):
    pass


class Monitor:
    def __init__(self, budget, clock=time.monotonic, cancel_event=None):
        self.budget, self.clock = budget, clock
        self.cancel_event = cancel_event
        self.started = clock()
        self.steps = self.no_progress = self.failed_tests = self.debug_loops = self.bytes = 0
        self.seen = set()
        self.signatures = Counter()
        self.file_versions = defaultdict(set)

    def remaining(self):
        return max(0, self.budget.max_runtime - (self.clock() - self.started))

    def check(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise RunCancelled("User requested stop")
        if self.remaining() <= 0:
            raise BudgetExceeded("max_runtime")

    def consume_context(self, size):
        self.bytes += size
        if self.bytes >= self.budget.max_context_growth:
            raise BudgetExceeded("max_context_growth")

    def observe(self, event):
        self.check()
        kind, data = event.kind, event.data
        self.consume_context(len(json.dumps(data, ensure_ascii=False).encode("utf-8")))
        if kind == "test":
            self.failed_tests = 0 if data.get("passed") else self.failed_tests + 1
            self.debug_loops += int(not data.get("passed", False))
            if self.failed_tests >= self.budget.max_failed_tests:
                raise BudgetExceeded("max_failed_tests")
            if self.debug_loops >= self.budget.max_debug_loops:
                raise BudgetExceeded("max_debug_loops")
        if kind in {"tool", "test", "hypothesis", "file_read", "file_write"}:
            self.steps += 1
            # Evidence is a host-generated output/content hash, never model confidence/progress.
            evidence = data.get("evidence_hash")
            signature = data.get("signature", "")
            self.signatures[signature] += 1
            new = bool(evidence and evidence not in self.seen)
            if evidence:
                self.seen.add(evidence)
            if kind == "file_write":
                path = data.get("path", "")
                versions = self.file_versions[path]
                if evidence in versions:
                    new = False  # A -> B -> A oscillation
                versions.add(evidence)
            # Repeating the same action is not convergence, even when stdout timestamps change.
            if self.signatures[signature] >= 3:
                new = False
            self.no_progress = 0 if new else self.no_progress + 1
            if self.no_progress >= self.budget.max_no_progress_tool_calls:
                raise BudgetExceeded("no_progress")
            if self.steps >= self.budget.max_exploration_steps:
                raise BudgetExceeded("max_exploration_steps")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
