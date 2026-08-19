#!/usr/bin/env python3
"""Adversarial regression tests for trial evidence validation and summary."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from route_model import relative_quota, route as route_task  # noqa: E402
from summarize_trial import (  # noqa: E402
    TrialDataError,
    _summarize_at_observed_time,
    canonical_task_class,
    summarize as summarize_live,
)


BASE_TIME = datetime.fromisoformat("2026-08-19T10:00:00+08:00")
TARGETS = {
    "review_eligibility": {"elapsed_calendar_days_min": 7, "completed_tasks_min": 10, "operator": "OR"},
    "route_acceptance_rate_min": 0.8,
    "high_risk_under_routing_max": 0,
    "same_class_unexplained_route_changes_max": 0,
    "duplicate_prompts_max": 0,
    "verified_efficiency_signals_min": 1,
}
ROUTING_INPUT_FIELDS = (
    "stage",
    "action_scope",
    "task_kind",
    "preference",
    "complexity",
    "ambiguity",
    "context_load",
    "tool_load",
    "error_cost",
    "latency_need",
    "repeatability",
    "parallelizable",
    "rapid_coding_iteration",
    "text_only",
    "risk_flags",
)


def event_time(offset_minutes: int) -> str:
    return (BASE_TIME + timedelta(minutes=offset_minutes)).isoformat()


def summarize(state_input: dict, events_input: list[dict], as_of: datetime) -> dict:
    return _summarize_at_observed_time(state_input, events_input, as_of, observed_now=as_of)


def state() -> dict:
    return {
        "schema_version": 1,
        "trial_id": "trial-test",
        "status": "active",
        "started_at": event_time(0),
        "source_commit": "abc123",
        "targets": TARGETS,
        "global_gate_enabled": False,
        "log_path": "evidence/trial-events.jsonl",
    }


def started() -> dict:
    return {
        "schema_version": 1,
        "event_id": "start",
        "event_type": "trial_started",
        "trial_id": "trial-test",
        "occurred_at": event_time(0),
        "task_id": None,
        "source_commit": "abc123",
        "global_gate_enabled": False,
        "targets": TARGETS,
    }


def route_event(
    task_id: str,
    offset: int,
    *,
    risk_floor: str = "low",
    user_choice: str = "accept_recommended",
) -> dict:
    route_input = {
        "stage": "execute",
        "action_scope": "local_write",
        "task_kind": "coding",
        "preference": "balanced",
        "complexity": 2,
        "ambiguity": 2,
        "context_load": 2,
        "tool_load": 2,
        "error_cost": 2,
        "latency_need": 3,
        "repeatability": 3,
        "parallelizable": False,
        "rapid_coding_iteration": False,
        "text_only": True,
        "risk_flags": [],
    }
    if risk_floor == "medium":
        route_input["complexity"] = 4
    elif risk_floor == "high":
        route_input["error_cost"] = 4
    elif risk_floor == "critical":
        route_input["risk_flags"] = ["production"]
    elif risk_floor != "low":
        raise ValueError("测试夹具的 risk_floor 非法")

    expected = route_task(route_input)
    model = expected["model"]
    effort = expected["effort"]
    event = {
        "schema_version": 1,
        "event_id": f"route-{task_id}",
        "event_type": "route_decision",
        "trial_id": "trial-test",
        "occurred_at": event_time(offset),
        "task_id": task_id,
        "project": "测试项目",
        "task_summary": "测试任务",
        **route_input,
        "router_version": expected["router_version"],
        "risk_floor": expected["risk_floor"],
        "route_trigger": "new_project",
        "recommended_model": model,
        "recommended_effort": effort,
        "relative_quota": expected["relative_quota"],
        "reason_codes": expected["reason_codes"],
        "user_choice": user_choice,
        "selected_model": model,
        "selected_effort": effort,
        "choice_reason": None if user_choice == "accept_recommended" else "测试改选",
        "duplicate_prompt": False,
    }
    event["task_class"] = canonical_task_class(event)
    return event


def rebind_route(event: dict) -> dict:
    expected = route_task({field: event[field] for field in ROUTING_INPUT_FIELDS})
    event.update(
        {
            "router_version": expected["router_version"],
            "risk_floor": expected["risk_floor"],
            "recommended_model": expected["model"],
            "recommended_effort": expected["effort"],
            "relative_quota": expected["relative_quota"],
            "reason_codes": expected["reason_codes"],
        }
    )
    if event["user_choice"] == "accept_recommended":
        event["selected_model"] = expected["model"]
        event["selected_effort"] = expected["effort"]
    event["task_class"] = canonical_task_class(event)
    return event


def quota_observation(*, before: float, after: float, reset: bool = False, window: str = "weekly-1") -> dict:
    return {
        "pool": "通用池",
        "window_id": window,
        "before_remaining_percent": before,
        "after_remaining_percent": after,
        "source": "Codex 用量界面",
        "reset_observed": reset,
        "note": "测试观测",
    }


def outcome_event(
    task_id: str,
    offset: int,
    *,
    model: str = "gpt-5.6-terra",
    effort: str = "medium",
    rework: int | None = 1,
) -> dict:
    return {
        "schema_version": 1,
        "event_id": f"outcome-{task_id}",
        "event_type": "task_outcome",
        "trial_id": "trial-test",
        "occurred_at": event_time(offset),
        "task_id": task_id,
        "status": "completed",
        "actual_model": model,
        "actual_effort": effort,
        "quality_score": 4,
        "rework_count": rework,
        "elapsed_minutes": 19,
        "quota_observation": None,
        "baseline": None,
        "acceptance_evidence": "测试通过",
    }


def ten_task_evidence() -> list[dict]:
    events = [started()]
    for index in range(10):
        task_id = f"task-{index}"
        events.append(route_event(task_id, 1 + index * 2))
        rework = 2 if index == 0 else 1
        outcome = outcome_event(task_id, 2 + index * 2, rework=rework)
        if index == 1:
            outcome["baseline"] = {
                "task_id": "task-0",
                "task_class": events[1]["task_class"],
                "rework_count": 2,
            }
        events.append(outcome)
    return events


class TrialSummaryTests(unittest.TestCase):
    def test_new_trial_has_zero_tasks_and_is_not_due(self) -> None:
        result = summarize(state(), [started()], datetime.fromisoformat(event_time(60)))
        self.assertEqual(result["route_decision_count"], 0)
        self.assertIsNone(result["acceptance"]["rate"])
        self.assertEqual(result["review_status"], "not_due")

    def test_ten_closed_tasks_with_real_prior_baseline_are_eligible(self) -> None:
        result = summarize(state(), ten_task_evidence(), datetime.fromisoformat(event_time(30)))
        self.assertTrue(result["review_due"])
        self.assertEqual(result["acceptance"]["rate"], 1.0)
        self.assertEqual(result["outcomes"]["verified_efficiency_signal_count"], 1)
        self.assertEqual(result["review_status"], "eligible_for_user_review")

    def test_declared_low_risk_cannot_hide_production_flag(self) -> None:
        decision = route_event("risk-lie", 1, risk_floor="critical")
        decision["risk_floor"] = "low"
        decision["task_class"] = canonical_task_class(decision)
        with self.assertRaises(TrialDataError):
            summarize(state(), [started(), decision], datetime.fromisoformat(event_time(2)))

    def test_accept_recommended_requires_selected_configuration_to_match(self) -> None:
        decision = route_event("choice-lie", 1)
        decision["selected_model"] = "gpt-5.6-luna"
        with self.assertRaises(TrialDataError):
            summarize(state(), [started(), decision], datetime.fromisoformat(event_time(2)))

    def test_cross_class_baseline_is_rejected(self) -> None:
        low_route = route_event("low", 1)
        low_outcome = outcome_event("low", 2, rework=2)
        high_route = route_event("high", 3, risk_floor="high")
        high_outcome = outcome_event("high", 4, model="gpt-5.6-sol", effort="high")
        high_outcome["baseline"] = {
            "task_id": "low",
            "task_class": low_route["task_class"],
            "rework_count": 2,
        }
        with self.assertRaises(TrialDataError):
            summarize(
                state(),
                [started(), low_route, low_outcome, high_route, high_outcome],
                datetime.fromisoformat(event_time(5)),
            )

    def test_baseline_rework_must_match_recorded_prior_outcome(self) -> None:
        first_route = route_event("first", 1)
        first_outcome = outcome_event("first", 2, rework=2)
        second_route = route_event("second", 3)
        second_outcome = outcome_event("second", 4)
        second_outcome["baseline"] = {
            "task_id": "first",
            "task_class": first_route["task_class"],
            "rework_count": 99,
        }
        with self.assertRaises(TrialDataError):
            summarize(
                state(),
                [started(), first_route, first_outcome, second_route, second_outcome],
                datetime.fromisoformat(event_time(5)),
            )

    def test_as_of_excludes_future_correction_from_historical_metrics(self) -> None:
        original = route_event("corrected", 1, user_choice="custom")
        corrected = dict(original)
        corrected.update(
            {
                "event_id": "route-corrected-v2",
                "occurred_at": event_time(24 * 60),
                "supersedes_event_id": original["event_id"],
                "user_choice": "accept_recommended",
                "choice_reason": None,
            }
        )
        historical = summarize(
            state(),
            [started(), original, corrected],
            datetime.fromisoformat(event_time(60)),
        )
        current = summarize(
            state(),
            [started(), original, corrected],
            datetime.fromisoformat(event_time(24 * 60 + 1)),
        )
        self.assertEqual(historical["acceptance"]["rate"], 0.0)
        self.assertEqual(current["acceptance"]["rate"], 1.0)

    def test_outcome_correction_replaces_old_status_and_metrics(self) -> None:
        decision = route_event("outcome-correction", 1)
        original = outcome_event("outcome-correction", 2)
        original.update({"status": "incomplete", "actual_model": None, "actual_effort": None})
        corrected = outcome_event("outcome-correction", 3)
        corrected.update(
            {
                "event_id": "outcome-correction-v2",
                "supersedes_event_id": original["event_id"],
            }
        )
        result = summarize(
            state(),
            [started(), decision, original, corrected],
            datetime.fromisoformat(event_time(4)),
        )
        self.assertEqual(result["completed_task_count"], 1)
        self.assertEqual(result["outcomes"]["quality_score_count"], 1)

    def test_late_route_correction_keeps_original_logical_position(self) -> None:
        original = route_event("late-route-correction", 1, user_choice="custom")
        outcome = outcome_event("late-route-correction", 2)
        correction = dict(original)
        correction.update(
            {
                "event_id": "late-route-correction-v2",
                "occurred_at": event_time(3),
                "supersedes_event_id": original["event_id"],
                "user_choice": "accept_recommended",
                "choice_reason": None,
            }
        )
        final_correction = dict(correction)
        final_correction.update(
            {
                "event_id": "late-route-correction-v3",
                "occurred_at": event_time(4),
                "supersedes_event_id": correction["event_id"],
                "evidence_note": "二次核对",
            }
        )
        result = summarize(
            state(),
            [started(), original, outcome, correction, final_correction],
            datetime.fromisoformat(event_time(5)),
        )
        self.assertEqual(result["route_decision_count"], 1)
        self.assertEqual(result["completed_task_count"], 1)
        self.assertEqual(result["acceptance"]["rate"], 1.0)

    def test_late_baseline_correction_keeps_original_logical_position(self) -> None:
        baseline_route = route_event("baseline-corrected", 1)
        baseline_outcome = outcome_event("baseline-corrected", 2, rework=2)
        current_route = route_event("uses-baseline", 3)
        current_outcome = outcome_event("uses-baseline", 4, rework=1)
        current_outcome["baseline"] = {
            "task_id": "baseline-corrected",
            "task_class": baseline_route["task_class"],
            "rework_count": 2,
        }
        baseline_correction = dict(baseline_outcome)
        baseline_correction.update(
            {
                "event_id": "baseline-corrected-outcome-v2",
                "occurred_at": event_time(5),
                "supersedes_event_id": baseline_outcome["event_id"],
                "quality_score": 5,
            }
        )
        result = summarize(
            state(),
            [
                started(),
                baseline_route,
                baseline_outcome,
                current_route,
                current_outcome,
                baseline_correction,
            ],
            datetime.fromisoformat(event_time(6)),
        )
        self.assertEqual(result["completed_task_count"], 2)
        self.assertEqual(result["outcomes"]["verified_efficiency_signal_count"], 1)

        baseline_correction["rework_count"] = 3
        current_correction = dict(current_outcome)
        current_correction.update(
            {
                "event_id": "uses-baseline-outcome-v2",
                "occurred_at": event_time(6),
                "supersedes_event_id": current_outcome["event_id"],
                "baseline": {
                    "task_id": "baseline-corrected",
                    "task_class": baseline_route["task_class"],
                    "rework_count": 3,
                },
            }
        )
        repaired = summarize(
            state(),
            [
                started(),
                baseline_route,
                baseline_outcome,
                current_route,
                current_outcome,
                baseline_correction,
                current_correction,
            ],
            datetime.fromisoformat(event_time(7)),
        )
        self.assertEqual(repaired["outcomes"]["mean_rework_delta"], 2.0)

    def test_state_start_must_exactly_match_first_event(self) -> None:
        invalid_state = state()
        invalid_state["started_at"] = event_time(1)
        with self.assertRaises(TrialDataError):
            summarize(invalid_state, [started()], datetime.fromisoformat(event_time(2)))

    def test_correction_cannot_branch_from_superseded_event(self) -> None:
        original = route_event("branch", 1, user_choice="custom")
        correction = dict(original)
        correction.update(
            {
                "event_id": "route-branch-v2",
                "occurred_at": event_time(2),
                "supersedes_event_id": original["event_id"],
                "user_choice": "accept_recommended",
                "choice_reason": None,
            }
        )
        branch = dict(correction)
        branch.update(
            {
                "event_id": "route-branch-v3",
                "occurred_at": event_time(3),
                "supersedes_event_id": original["event_id"],
            }
        )
        with self.assertRaises(TrialDataError):
            summarize(state(), [started(), original, correction, branch], datetime.fromisoformat(event_time(4)))

    def test_outcome_must_be_appended_after_route(self) -> None:
        with self.assertRaises(TrialDataError):
            summarize(
                state(),
                [started(), outcome_event("late-route", 1), route_event("late-route", 2)],
                datetime.fromisoformat(event_time(3)),
            )

    def test_log_timestamps_must_be_non_decreasing(self) -> None:
        with self.assertRaises(TrialDataError):
            summarize(
                state(),
                [started(), route_event("time-order", 2), outcome_event("time-order", 1)],
                datetime.fromisoformat(event_time(3)),
            )

    def test_seven_day_and_ten_task_review_boundaries_are_exact(self) -> None:
        day_six = summarize(
            state(),
            [started()],
            datetime.fromisoformat(event_time(5 * 24 * 60 + 1)),
        )
        day_seven = summarize(
            state(),
            [started()],
            datetime.fromisoformat(event_time(6 * 24 * 60 + 1)),
        )
        self.assertFalse(day_six["review_due"])
        self.assertTrue(day_seven["review_due"])

        nine_tasks = ten_task_evidence()[:-2]
        nine = summarize(state(), nine_tasks, datetime.fromisoformat(event_time(30)))
        ten = summarize(state(), ten_task_evidence(), datetime.fromisoformat(event_time(30)))
        self.assertEqual(nine["completed_task_count"], 9)
        self.assertFalse(nine["review_due"])
        self.assertEqual(ten["completed_task_count"], 10)
        self.assertTrue(ten["review_due"])

    def test_stability_compares_only_identical_routing_inputs(self) -> None:
        first = route_event("stable-first", 1)
        changed_input = route_event("stable-second", 2)
        changed_input["stage"] = "plan"
        changed_input["repeatability"] = 4
        rebind_route(changed_input)
        changed = summarize(
            state(),
            [started(), first, changed_input],
            datetime.fromisoformat(event_time(3)),
        )
        self.assertEqual(changed["stability"]["comparable_pairs"], 0)
        self.assertEqual(changed["stability"]["potential_unexplained_route_change_count"], 0)

        same_input = route_event("stable-third", 2)
        stable = summarize(
            state(),
            [started(), first, same_input],
            datetime.fromisoformat(event_time(3)),
        )
        self.assertEqual(stable["stability"]["comparable_pairs"], 1)
        self.assertEqual(stable["stability"]["potential_unexplained_route_change_count"], 0)

        same_input["recommended_model"] = "gpt-5.6-luna"
        same_input["selected_model"] = "gpt-5.6-luna"
        same_input["relative_quota"] = relative_quota("gpt-5.6-luna", "medium")
        with self.assertRaises(TrialDataError):
            summarize(state(), [started(), first, same_input], datetime.fromisoformat(event_time(3)))

    def test_high_risk_unknown_actual_blocks_review(self) -> None:
        events = [started(), route_event("high-pending", 1, risk_floor="critical")]
        result = summarize(state(), events, datetime.fromisoformat(event_time(6 * 24 * 60 + 1)))
        self.assertTrue(result["review_due"])
        self.assertEqual(result["high_risk"]["actual_configuration_pending_count"], 1)
        self.assertEqual(result["review_status"], "continue_trial_or_insufficient_evidence")

    def test_cancelled_high_risk_with_unknown_actual_stays_pending(self) -> None:
        decision = route_event("cancelled-high", 1, risk_floor="critical")
        outcome = outcome_event("cancelled-high", 2, model="gpt-5.6-sol", effort="high")
        outcome.update({"status": "cancelled", "actual_model": None, "actual_effort": None})
        result = summarize(
            state(),
            [started(), decision, outcome],
            datetime.fromisoformat(event_time(6 * 24 * 60 + 1)),
        )
        self.assertEqual(result["high_risk"]["actual_configuration_pending_count"], 1)
        self.assertEqual(result["review_status"], "continue_trial_or_insufficient_evidence")

    def test_future_as_of_is_rejected(self) -> None:
        future = datetime.now().astimezone() + timedelta(days=1)
        with self.assertRaises(TrialDataError):
            summarize_live(state(), [started()], future)

    def test_selected_under_routing_is_warning_not_advisor_error(self) -> None:
        events = ten_task_evidence()
        decision = route_event("task-2", 5, risk_floor="high", user_choice="custom")
        decision["user_choice"] = "custom"
        decision["selected_model"] = "gpt-5.6-luna"
        decision["selected_effort"] = "medium"
        decision["choice_reason"] = "主动选择低额度配置"
        events[5] = decision
        events[6] = outcome_event("task-2", 6, model="gpt-5.6-sol", effort="high")
        result = summarize(state(), events, datetime.fromisoformat(event_time(30)))
        self.assertEqual(result["high_risk"]["selected_under_routing_warning_count"], 1)
        self.assertEqual(result["acceptance"]["rate"], 0.9)
        self.assertEqual(result["review_status"], "eligible_for_user_review")

    def test_user_selected_gpt_55_can_be_recorded_but_is_not_auto_recommended(self) -> None:
        decision = route_event("legacy-model", 1, user_choice="custom")
        self.assertNotEqual(decision["recommended_model"], "gpt-5.5")
        decision.update(
            {
                "selected_model": "gpt-5.5",
                "selected_effort": "medium",
                "choice_reason": "已验证旧流程兼容性",
            }
        )
        outcome = outcome_event("legacy-model", 2, model="gpt-5.5", effort="medium")
        result = summarize(state(), [started(), decision, outcome], datetime.fromisoformat(event_time(3)))
        self.assertEqual(result["route_decision_count"], 1)
        self.assertEqual(result["completed_task_count"], 1)

    def test_high_risk_actual_under_routing_is_detected(self) -> None:
        decision = route_event("high-under", 1, risk_floor="critical", user_choice="custom")
        decision.update(
            {
                "selected_model": "gpt-5.6-luna",
                "selected_effort": "medium",
                "choice_reason": "测试低配",
            }
        )
        outcome = outcome_event("high-under", 2, model="gpt-5.6-luna", effort="medium")
        result = summarize(state(), [started(), decision, outcome], datetime.fromisoformat(event_time(3)))
        self.assertEqual(result["high_risk"]["advisor_under_routing_count"], 0)
        self.assertEqual(result["high_risk"]["selected_under_routing_warning_count"], 1)
        self.assertEqual(result["high_risk"]["actual_under_routing_count"], 1)
        self.assertEqual(result["high_risk"]["under_routing_task_count"], 1)

    def test_external_deploy_cannot_be_recorded_as_ultra(self) -> None:
        decision = route_event("deploy-ultra", 1, risk_floor="critical")
        decision["stage"] = "deploy"
        decision["action_scope"] = "external_effect"
        decision["recommended_effort"] = "ultra"
        decision["selected_effort"] = "ultra"
        decision["relative_quota"] = relative_quota("gpt-5.6-sol", "ultra")
        decision["task_class"] = canonical_task_class(decision)
        with self.assertRaises(TrialDataError):
            summarize(state(), [started(), decision], datetime.fromisoformat(event_time(2)))

    def test_quota_improvement_requires_same_observation_window(self) -> None:
        first_route = route_event("quota-first", 1)
        first_outcome = outcome_event("quota-first", 2, rework=1)
        first_outcome["quota_observation"] = quota_observation(before=100, after=95)
        second_route = route_event("quota-second", 3)
        second_outcome = outcome_event("quota-second", 4, rework=1)
        second_outcome["quota_observation"] = quota_observation(before=95, after=93)
        second_outcome["baseline"] = {
            "task_id": "quota-first",
            "task_class": first_route["task_class"],
            "rework_count": 1,
        }
        result = summarize(
            state(),
            [started(), first_route, first_outcome, second_route, second_outcome],
            datetime.fromisoformat(event_time(5)),
        )
        self.assertEqual(result["outcomes"]["quota_improvement_signal_count"], 1)
        self.assertEqual(result["outcomes"]["verified_efficiency_signal_count"], 1)

        second_outcome["quota_observation"]["window_id"] = "weekly-2"
        result = summarize(
            state(),
            [started(), first_route, first_outcome, second_route, second_outcome],
            datetime.fromisoformat(event_time(5)),
        )
        self.assertEqual(result["outcomes"]["quota_improvement_signal_count"], 0)

        second_outcome["quota_observation"].update(
            {
                "window_id": "weekly-1",
                "before_remaining_percent": 96,
                "after_remaining_percent": 94,
            }
        )
        impossible_sequence = summarize(
            state(),
            [started(), first_route, first_outcome, second_route, second_outcome],
            datetime.fromisoformat(event_time(5)),
        )
        self.assertEqual(impossible_sequence["outcomes"]["comparable_quota_count"], 0)
        self.assertEqual(impossible_sequence["outcomes"]["quota_improvement_signal_count"], 0)

    def test_rework_without_evidence_notes_does_not_count_as_efficiency(self) -> None:
        events = ten_task_evidence()
        events[2]["acceptance_evidence"] = None
        events[4]["acceptance_evidence"] = None
        result = summarize(state(), events, datetime.fromisoformat(event_time(30)))
        self.assertEqual(result["outcomes"]["comparable_rework_count"], 0)
        self.assertEqual(result["outcomes"]["verified_efficiency_signal_count"], 0)
        self.assertEqual(result["review_status"], "continue_trial_or_insufficient_evidence")

    def test_rolled_back_state_never_returns_eligible(self) -> None:
        rolled_back = state()
        rolled_back["status"] = "rolled_back"
        result = summarize(rolled_back, ten_task_evidence(), datetime.fromisoformat(event_time(30)))
        self.assertEqual(result["review_status"], "rolled_back")

    def test_target_types_and_supported_efforts_are_validated(self) -> None:
        invalid_state = state()
        invalid_state["targets"] = dict(TARGETS)
        invalid_state["targets"]["duplicate_prompts_max"] = False
        with self.assertRaises(TrialDataError):
            summarize(invalid_state, [started()], datetime.fromisoformat(event_time(1)))

        decision = route_event("effort", 1)
        decision["recommended_model"] = "gpt-5.3-codex-spark"
        decision["recommended_effort"] = "ultra"
        decision["relative_quota"] = "低（独立额度池，需实测）"
        decision["selected_model"] = "gpt-5.3-codex-spark"
        decision["selected_effort"] = "ultra"
        with self.assertRaises(TrialDataError):
            summarize(state(), [started(), decision], datetime.fromisoformat(event_time(2)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
