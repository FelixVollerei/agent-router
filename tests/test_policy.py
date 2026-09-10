from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from model_router.config import Config
from model_router.experience import ExperienceStore
from model_router.models import Task, TaskProfile
from model_router.policy import NoSafeRoute, Policy


def easy_task(**changes):
    profile = TaskProfile(task_type="batch_edit", domain="plant_scada", requirement_clarity=5,
                          automatic_verifiability=5, human_verification_cost=1, failure_blast_radius=1,
                          tool_complexity=1, repo_scope=1, domain_uncertainty=0, root_cause_uncertainty=0,
                          reversibility=5, existing_reference_implementation=True, known_acceptance_criteria=True,
                          **changes)
    return Task("Rename variables", profile, acceptance_criteria=["Exact names and values"],
                allowed_files=["variables.json"], checks=["check"])


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(Path("unused"), auto_usage=False)
        self.policy = Policy(self.cfg)

    def scada(self):
        return Task("Unknown loading regression", TaskProfile(task_type="performance_debugging", domain="plant_scada",
                    root_cause_uncertainty=5, domain_uncertainty=4, automatic_verifiability=1,
                    production=True, production_blocker=True, deadline_urgency=3))

    def test_case_1_clear_batch_three_days(self):
        self.assertEqual(self.policy.decide(easy_task(deadline_urgency=1)).executor, "deepseek")

    def test_case_2_scada_unknown_root_8h(self):
        self.assertEqual(self.policy.decide(self.scada()).executor, "sol_high")

    def test_case_3_game_spec_feature(self):
        task = easy_task()
        task.profile.domain, task.profile.task_type = "game", "feature"
        self.assertEqual(self.policy.decide(task).executor, "deepseek")

    def test_case_4_large_production_refactor(self):
        task = easy_task(production=True)
        task.profile.task_type = "refactor"
        task.profile.failure_blast_radius = task.profile.repo_scope = task.profile.human_verification_cost = 4
        route = self.policy.decide(task)
        self.assertIn(route.executor, {"deepseek", "sol_medium"})
        if route.executor == "deepseek":
            self.assertEqual(route.reviewer, "sol_medium")

    def test_case_5_sol_failed_onsite(self):
        task = self.scada()
        task.profile.failed_executors, task.profile.onsite = ["sol_high"], True
        self.assertEqual(self.policy.decide(task).executor, "astra")

    def test_case_6_astra_failed_critical(self):
        task = self.scada()
        task.profile.failed_executors, task.profile.critical = ["astra"], True
        self.assertEqual(self.policy.decide(task).executor, "astra_ultra")

    def test_case_7_low_quota_game(self):
        self.cfg.codex_budget_remaining = 10
        task = easy_task()
        task.profile.domain = "game"
        self.assertEqual(self.policy.decide(task).executor, "deepseek")

    def test_case_8_low_quota_critical_onsite(self):
        self.cfg.codex_budget_remaining = 10
        task = self.scada()
        task.profile.critical, task.profile.onsite = True, True
        self.assertGreaterEqual(self.cfg.model(self.policy.decide(task).executor).rank, 3)

    def test_ultra_gated_and_no_route_after_ultra_failure(self):
        route = self.policy.decide(self.scada())
        self.assertNotIn("astra_ultra", [x["executor"] for x in route.candidates])
        task = self.scada()
        task.profile.failed_executors = ["astra_ultra"]
        with self.assertRaises(NoSafeRoute):
            self.policy.decide(task)

    def test_force_no_escalation_and_cheap_keeps_safety(self):
        task = self.scada()
        task.mode = "cheap"
        self.assertGreaterEqual(self.cfg.model(self.policy.decide(task).executor).rank, 3)
        task.force = "deepseek"
        route = self.policy.decide(task)
        self.assertEqual(route.executor, "deepseek")
        self.assertIsNone(route.next_executor)

    def test_deadline_changes_route_and_budget(self):
        task = easy_task(production=True)
        task.profile.task_type = "feature"
        self.assertEqual(self.policy.decide(task).executor, "deepseek")
        task.profile.deadline_urgency = 3
        route = self.policy.decide(task)
        self.assertGreaterEqual(self.cfg.model(route.executor).rank, 2)
        self.assertLessEqual(route.budget.max_runtime, 600)

    def test_history_excludes_failed_low_model_and_infrastructure(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ExperienceStore(Path(temp) / "history.sqlite3")
            task = easy_task()
            model = self.cfg.model("deepseek")
            for i in range(12):
                store.record(run_id=str(i), attempt=1, profile=task.profile, model=model, strategy="execute",
                             success=False, runtime=10, escalation=True, verifier="failed")
            policy = Policy(self.cfg, store)
            self.assertNotEqual(policy.decide(task).executor, "deepseek")
            stats = store.stats(task.profile, model)
            self.assertEqual(stats["attempts"], 12)
            store.record(run_id="infra", attempt=1, profile=task.profile, model=model, strategy="execute",
                         success=False, runtime=1, escalation=False, verifier="not_run", outcome_kind="infrastructure")
            self.assertEqual(store.stats(task.profile, model)["attempts"], 12)
            store.close()

    def test_invalid_profile_rejected(self):
        for kwargs in ({"reversibility": 6}, {"production": "false"}, {"deadline_at": "2026-09-09T12:00:00"}, {"confidence": .99}):
            with self.assertRaises((ValueError, TypeError)):
                TaskProfile(**kwargs)


if __name__ == "__main__":
    unittest.main()
