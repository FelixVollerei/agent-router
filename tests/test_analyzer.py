from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from model_router.analyzer import Analyzer
from model_router.config import Config
from model_router.models import TaskProfile


class AnalyzerTests(unittest.TestCase):
    def test_uncited_features_and_structured_confidence_do_not_lower_risk(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = Config(Path(temp))
            profile = asdict(TaskProfile())
            profile.update(task_type="batch_edit", automatic_verifiability=5, reversibility=5,
                           evidence={"task_type": "batch rename", "automatic_verifiability": None,
                                     "reversibility": {"confidence": .99}})
            response = {"choices": [{"message": {"content": json.dumps({"profile": profile, "context": {"reference": "rules"}, "goal": "rename"})}}]}
            with patch("model_router.analyzer.chat_completion", return_value=response):
                task = Analyzer(cfg).analyze("batch rename", Path(temp))
            self.assertEqual(task.profile.task_type, "batch_edit")
            self.assertEqual(task.profile.automatic_verifiability, 1)
            self.assertEqual(task.profile.reversibility, 2)
            self.assertIsInstance(task.context, list)

    def test_offline_never_invents_verifier_or_allowed_files(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Analyzer(Config(Path(temp))).analyze("/cheap fix production bug", Path(temp), False)
            self.assertEqual(task.mode, "cheap")
            self.assertEqual(task.allowed_files, [])
            self.assertEqual(task.checks, [])
            self.assertLessEqual(task.profile.automatic_verifiability, 1)
