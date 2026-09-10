from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from model_router.analyzer import Analyzer, parse_overrides
from model_router.config import Config
from model_router.models import Budget, Event
from model_router.monitor import BudgetExceeded, Monitor, digest
from model_router.orchestrator import Orchestrator
from model_router.process import run_command
from model_router.providers import CodexCLIProvider, ManagedChatProvider, ProviderUnavailable
from model_router.usage import remaining_percent
from model_router.workspace import Workspace, inventory, promote, safe_path
from tests.test_policy import easy_task


class MonitorTests(unittest.TestCase):
    def test_failed_tests_stop_early(self):
        m = Monitor(Budget())
        m.observe(Event("test", {"passed": False}))
        with self.assertRaisesRegex(BudgetExceeded, "max_failed_tests"):
            m.observe(Event("test", {"passed": False}))

    def test_no_progress_ignores_self_reported_progress(self):
        m = Monitor(Budget(max_no_progress_tool_calls=3))
        for _ in range(2):
            m.observe(Event("tool", {"signature": "read:x", "progress": True}))
        with self.assertRaisesRegex(BudgetExceeded, "no_progress"):
            m.observe(Event("tool", {"signature": "read:x", "progress": True}))

    def test_repeated_read_and_oscillation(self):
        m = Monitor(Budget(max_no_progress_tool_calls=2))
        m.observe(Event("file_write", {"path": "x", "signature": "write:x", "evidence_hash": "a"}))
        m.observe(Event("file_write", {"path": "x", "signature": "write:x", "evidence_hash": "b"}))
        m.observe(Event("file_write", {"path": "x", "signature": "write:x", "evidence_hash": "a"}))
        with self.assertRaises(BudgetExceeded):
            m.observe(Event("file_write", {"path": "x", "signature": "write:x", "evidence_hash": "b"}))

    def test_deadline_and_context_hard_limits(self):
        clock = [0]
        m = Monitor(Budget(max_runtime=2), lambda: clock[0])
        clock[0] = 3
        with self.assertRaises(BudgetExceeded):
            m.check()
        m = Monitor(Budget(max_context_growth=10))
        with self.assertRaisesRegex(BudgetExceeded, "max_context_growth"):
            m.consume_context(11)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "variables.json").write_text('{"old": 1}', encoding="utf-8")
        self.cfg = Config(self.root / "state", auto_usage=False,
                          commands={"check": [sys.executable, "-B", "-c", "import json; from pathlib import Path; assert json.loads(Path('variables.json').read_text()) == {'new': 1}"]})
        self.task = easy_task()
        self.task.expected_files = ["variables.json"]

    def tearDown(self):
        self.temp.cleanup()

    def factory(self, actions):
        class FixtureProvider:
            def run(provider_self, **kwargs):
                action = actions[kwargs["model"].id]
                if callable(action):
                    action(**kwargs)
                else:
                    (kwargs["work"] / "variables.json").write_text(json.dumps(action), encoding="utf-8")
                    kwargs["emit"](Event("summary", {"text": "done"}))
        return lambda cfg, model: FixtureProvider()

    def test_verification_failure_escalates_with_patch_and_evidence(self):
        observed = []
        def next_model(**kw):
            observed.append(kw["prompt"])
            self.assertEqual(json.loads((kw["work"] / "variables.json").read_text()), {"wrong": 1})
            (kw["work"] / "variables.json").write_text('{"new": 1}')
        result = Orchestrator(self.cfg, self.factory({"deepseek": {"wrong": 1}, "sol_medium": next_model})).run(self.task, self.repo)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.attempts, 2)
        self.assertIn("Current Failure", observed[0])
        self.assertIn("Independent verifier: failed", observed[0])
        self.assertEqual(json.loads((self.repo / "variables.json").read_text()), {"old": 1})
        run = Path(result.run_dir)
        self.assertTrue((run / "checkpoint-2" / "variables.json").exists())
        changed = promote(run)
        self.assertEqual(changed, ["variables.json"])
        self.assertEqual(json.loads((self.repo / "variables.json").read_text()), {"new": 1})

    def test_model_claim_cannot_override_failing_verifier(self):
        def liar(**kw):
            kw["emit"](Event("summary", {"text": "All tests passed. confidence=1.0"}))
        self.task.no_escalation = True
        result = Orchestrator(self.cfg, self.factory({"deepseek": liar})).run(self.task, self.repo)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.attempts, 1)
        with self.assertRaises(ValueError):
            promote(Path(result.run_dir))

    def test_missing_oracle_requires_human_even_if_model_finished(self):
        self.task.checks = []
        self.task.expected_files = []
        self.task.force = "deepseek"
        result = Orchestrator(self.cfg, self.factory({"deepseek": {"new": 1}})).run(self.task, self.repo)
        self.assertEqual(result.status, "needs_human")

    def test_production_review_is_readonly_and_mandatory(self):
        self.task.profile.production = True
        self.task.profile.failure_blast_radius = 4
        def reviewer(**kw):
            self.assertTrue(kw["review"])
            self.assertIn("Candidate diff", kw["prompt"])
            kw["emit"](Event("summary", {"text": '{"approved": true, "findings": []}'}))
        result = Orchestrator(self.cfg, self.factory({"deepseek": {"new": 1}, "sol_medium": reviewer})).run(self.task, self.repo)
        self.assertEqual(result.status, "verified")
        self.assertTrue((Path(result.run_dir) / "review-1.json").exists())

    def test_review_mutation_rejected(self):
        self.task.profile.production = True
        self.task.profile.failure_blast_radius = 4
        self.task.no_escalation = True
        def reviewer(**kw):
            (kw["work"] / "extra.txt").write_text("bad")
            kw["emit"](Event("summary", {"text": '{"approved": true, "findings": []}'}))
        result = Orchestrator(self.cfg, self.factory({"deepseek": {"new": 1}, "sol_medium": reviewer})).run(self.task, self.repo)
        self.assertEqual(result.status, "blocked")

    def test_forbidden_file_change_rejected(self):
        self.task.no_escalation = True
        def bad(**kw):
            (kw["work"] / "variables.json").write_text('{"new": 1}')
            (kw["work"] / "production.conf").write_text("bad")
        result = Orchestrator(self.cfg, self.factory({"deepseek": bad})).run(self.task, self.repo)
        self.assertEqual(result.status, "blocked")

    def test_original_or_candidate_drift_blocks_promotion(self):
        result = Orchestrator(self.cfg, self.factory({"deepseek": {"new": 1}})).run(self.task, self.repo)
        (self.repo / "variables.json").write_text('{"user": 2}')
        with self.assertRaisesRegex(ValueError, "Original workspace changed"):
            promote(Path(result.run_dir))
        (self.repo / "variables.json").write_text('{"old": 1}')
        (Path(result.run_dir) / "work" / "variables.json").write_text('{"tamper": 3}')
        with self.assertRaisesRegex(ValueError, "Candidate changed"):
            promote(Path(result.run_dir))

    def test_svn_preserved_and_state_inside_repo_refused(self):
        (self.repo / ".svn").mkdir()
        ws = Workspace(self.repo, self.root / "run")
        ws.prepare()
        self.assertEqual(ws.vcs, "svn")
        self.assertTrue((self.repo / ".svn").exists())
        self.assertFalse((ws.work / ".git").exists())
        with self.assertRaises(ValueError):
            Workspace(self.repo, self.repo / "runs")

    def test_path_traversal_and_reserved_files(self):
        for path in ("../bad", "C:/bad", "/bad", "x/../../bad", ".git/config", "x\\bad", ".env"):
            with self.assertRaises(ValueError):
                safe_path(self.repo, path)

    def test_infrastructure_failure_does_not_burn_higher_model(self):
        def unavailable(**kw):
            raise ProviderUnavailable("Missing API credential")
        result = Orchestrator(self.cfg, self.factory({"deepseek": unavailable})).run(self.task, self.repo)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.attempts, 1)

    def test_no_progress_early_escalation(self):
        def stuck(**kw):
            for _ in range(11):
                kw["emit"](Event("tool", {"signature": "repeated_read"}))
        result = Orchestrator(self.cfg, self.factory({"deepseek": stuck, "sol_medium": {"new": 1}})).run(self.task, self.repo)
        self.assertEqual(result.status, "verified")
        self.assertIn("no_progress", (Path(result.run_dir) / "handoff-1.md").read_text())

    def test_expired_deadline_never_starts_model(self):
        self.task.profile.deadline_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        result = Orchestrator(self.cfg, lambda *_: self.fail("provider must not start")).run(self.task, self.repo)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.attempts, 0)


class AdapterTests(unittest.TestCase):
    def test_quota_selects_named_bucket_and_limits(self):
        self.assertEqual(remaining_percent({"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 20}, "secondary": {"usedPercent": 90}}, "other": {"primary": {"usedPercent": 100}}}}), 10)
        self.assertIsNone(remaining_percent({"rateLimitsByLimitId": {"other": {}}}))
        self.assertIsNone(remaining_percent({"rateLimits": {"primary": None}}))

    def test_override_only_at_start(self):
        text, options = parse_overrides("/deadline /sol /no-escalation Fix file")
        self.assertEqual(text, "Fix file")
        self.assertEqual(options, {"mode": "deadline", "force": "sol", "no_escalation": True})
        self.assertEqual(parse_overrides('Document the string /astra')[1], {})

    def test_cli_is_explicit_sandboxed_and_no_bypass(self):
        cfg = Config(Path("unused"))
        provider = CodexCLIProvider({"command": "codex"}, cfg)
        argv = provider.argv(cfg.model("sol_high"), Path("candidate"), review=True)
        self.assertIn("read-only", argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_process_timeout_works_without_stdout(self):
        with tempfile.TemporaryDirectory() as root:
            start = time.monotonic()
            result = run_command([sys.executable, "-c", "import time; time.sleep(30)"], Path(root), .25)
            self.assertEqual(result["error"], "timeout")
            self.assertLess(time.monotonic() - start, 5)

    def test_managed_provider_denies_arbitrary_shell_then_completes(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Config(Path(root))
            settings = {"api_key_env": "ROUTER_TEST_KEY"}
            provider = ManagedChatProvider(settings, cfg)
            events = []
            responses = [
                {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "shell", "arguments": '{"command":"bad"}'}}]}}]},
                {"choices": [{"message": {"role": "assistant", "content": "stopped"}}]},
            ]
            with patch.dict(os.environ, {"ROUTER_TEST_KEY": "test-only"}), patch("model_router.providers.chat_completion", side_effect=responses):
                provider.run(task=easy_task(), model=cfg.model("deepseek"), prompt="test", work=Path(root), monitor=Monitor(Budget()), emit=events.append)
            self.assertTrue(any(e.data.get("error") == "Tool not allowed" for e in events))


if __name__ == "__main__":
    unittest.main()
