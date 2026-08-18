#!/usr/bin/env python3
"""Offline regression tests for the deterministic model router."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
FIXTURES_PATH = SKILL_DIR / "references" / "evaluation-fixtures.json"
sys.path.insert(0, str(SCRIPT_DIR))

from route_model import (  # noqa: E402
    InputError,
    SUPPORTED_EFFORTS,
    determine_risk_floor,
    route,
    validate_task,
)


def load_fixtures() -> list[dict]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


class RoutingFixtureTests(unittest.TestCase):
    def test_all_twelve_realistic_fixtures_match(self) -> None:
        fixtures = load_fixtures()
        self.assertEqual(len(fixtures), 12)
        for fixture in fixtures:
            with self.subTest(fixture=fixture["id"]):
                actual = route(fixture["task"])
                expected = fixture["expected"]
                self.assertEqual(actual["model"], expected["model"])
                self.assertEqual(actual["effort"], expected["effort"])
                self.assertEqual(actual["risk_floor"], expected["risk_floor"])
                self.assertTrue(actual["requires_user_confirmation"])

    def test_every_route_uses_a_supported_model_effort_pair(self) -> None:
        for fixture in load_fixtures():
            with self.subTest(fixture=fixture["id"]):
                actual = route(fixture["task"])
                self.assertIn(actual["model"], SUPPORTED_EFFORTS)
                self.assertIn(actual["effort"], SUPPORTED_EFFORTS[actual["model"]])

    def test_high_risk_never_routes_below_sol_high(self) -> None:
        allowed_efforts = {"high", "xhigh", "max", "ultra"}
        for fixture in load_fixtures():
            actual = route(fixture["task"])
            if actual["risk_floor"] in {"high", "critical"}:
                with self.subTest(fixture=fixture["id"]):
                    self.assertEqual(actual["model"], "gpt-5.6-sol")
                    self.assertIn(actual["effort"], allowed_efforts)

    def test_external_deploy_never_auto_routes_ultra(self) -> None:
        task = dict(load_fixtures()[11]["task"])
        task.update(
            {
                "stage": "deploy",
                "action_scope": "external_effect",
                "error_cost": 5,
                "risk_flags": ["production", "irreversible"],
            }
        )
        actual = route(task)
        self.assertEqual(actual["model"], "gpt-5.6-sol")
        self.assertEqual(actual["effort"], "xhigh")

    def test_spark_is_only_used_for_eligible_text_coding(self) -> None:
        for fixture in load_fixtures():
            actual = route(fixture["task"])
            if actual["model"] == "gpt-5.3-codex-spark":
                task = fixture["task"]
                with self.subTest(fixture=fixture["id"]):
                    self.assertEqual(task["task_kind"], "coding")
                    self.assertTrue(task["text_only"])
                    self.assertTrue(task["rapid_coding_iteration"])
                    self.assertFalse(task["risk_flags"])
                    self.assertNotEqual(task["action_scope"], "external_effect")

    def test_gpt_55_is_never_automatic(self) -> None:
        for fixture in load_fixtures():
            self.assertNotEqual(route(fixture["task"])["model"], "gpt-5.5")

    def test_routing_is_deterministic(self) -> None:
        task = load_fixtures()[4]["task"]
        self.assertEqual(route(task), route(task))

    def test_router_matrix_matches_local_model_cache_when_available(self) -> None:
        cache_path = Path.home() / ".codex" / "models_cache.json"
        if not cache_path.exists():
            self.skipTest("本机没有 Codex 模型缓存")

        cached_models = {
            model["slug"]: {level["effort"] for level in model["supported_reasoning_levels"]}
            for model in json.loads(cache_path.read_text(encoding="utf-8"))["models"]
        }
        for model, efforts in SUPPORTED_EFFORTS.items():
            with self.subTest(model=model):
                self.assertIn(model, cached_models)
                self.assertEqual(efforts, cached_models[model])


class InputValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid = load_fixtures()[0]["task"]

    def assert_invalid(self, mutation) -> None:
        task = dict(self.valid)
        mutation(task)
        with self.assertRaises(InputError):
            validate_task(task)

    def test_missing_field_is_rejected(self) -> None:
        self.assert_invalid(lambda task: task.pop("stage"))

    def test_unknown_field_is_rejected(self) -> None:
        self.assert_invalid(lambda task: task.update({"project_name": "示例"}))

    def test_out_of_range_score_is_rejected(self) -> None:
        self.assert_invalid(lambda task: task.update({"complexity": 6}))

    def test_boolean_is_not_accepted_as_score(self) -> None:
        self.assert_invalid(lambda task: task.update({"complexity": True}))

    def test_invalid_boolean_is_rejected(self) -> None:
        self.assert_invalid(lambda task: task.update({"text_only": "true"}))

    def test_unknown_risk_flag_is_rejected(self) -> None:
        self.assert_invalid(lambda task: task.update({"risk_flags": ["unknown"]}))

    def test_duplicate_risk_flag_is_rejected(self) -> None:
        self.assert_invalid(
            lambda task: task.update({"risk_flags": ["security", "security"]})
        )

    def test_deploy_is_critical_even_without_flags(self) -> None:
        task = dict(self.valid)
        task["stage"] = "deploy"
        self.assertEqual(determine_risk_floor(validate_task(task)), "critical")


if __name__ == "__main__":
    unittest.main(verbosity=2)
