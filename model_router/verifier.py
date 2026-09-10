from dataclasses import asdict

from .models import Verification
from .process import run_command
from .workspace import forbidden_artifacts, inventory, matches, permitted


class Verifier:
    def __init__(self, config):
        self.config = config

    def verify(self, task, workspace, timeout=120):
        reports = []
        for name in task.checks:
            argv = self.config.commands.get(name)
            if not argv:
                reports.append({"name": name, "passed": False, "error": "Check not configured"})
                continue
            result = run_command(argv, workspace.work, max(.1, timeout / max(1, len(task.checks))))
            reports.append({"name": name, "passed": result["returncode"] == 0 and not result["error"], **result})
        # Inspect AFTER running checks, so check-induced mutations are also validated.
        changed = workspace.diff()
        illegal = [p for p in changed if not permitted(p, task.allowed_files, task.forbidden_files)]
        reports.append({"name": "scope", "passed": not illegal, "unexpected_files": illegal})
        artifacts = forbidden_artifacts(workspace.work)
        reports.append({"name": "reserved_artifacts", "passed": not artifacts, "files": artifacts})
        missing = [pattern for pattern in task.expected_files if not matches_any(pattern, changed)]
        reports.append({"name": "expected_changes", "passed": not missing, "missing": missing})
        if any(not r["passed"] for r in reports):
            return Verification("failed", reports, changed)
        if task.manual_verification or not task.checks or not task.acceptance_criteria:
            return Verification("needs_human", reports, changed)
        return Verification("passed", reports, changed)


def matches_any(pattern, paths):
    return any(matches(path, [pattern]) for path in paths)
