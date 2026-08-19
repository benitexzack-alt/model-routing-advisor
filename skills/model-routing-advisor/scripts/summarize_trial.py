#!/usr/bin/env python3
"""Validate and summarize append-only model-routing trial evidence."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from route_model import (
    InputError,
    ROUTE_VERSION,
    SUPPORTED_EFFORTS,
    determine_risk_floor,
    relative_quota,
    route as route_task,
    validate_task,
)


EVENT_TYPES = {"trial_started", "route_decision", "task_outcome"}
USER_CHOICES = {"accept_recommended", "prefer_quota", "prefer_quality", "custom"}
OUTCOME_STATUSES = {"completed", "blocked", "cancelled", "incomplete"}
ROUTE_TRIGGERS = {
    "new_project",
    "stage_change",
    "risk_change",
    "scope_change",
    "resume_significant_work",
    "user_requested",
}
SAFE_HIGH_RISK_EFFORTS = {"high", "xhigh", "max", "ultra"}
ROUTING_SCORE_FIELDS = (
    "complexity",
    "ambiguity",
    "context_load",
    "tool_load",
    "error_cost",
    "latency_need",
    "repeatability",
)
ROUTING_BOOLEAN_FIELDS = ("parallelizable", "rapid_coding_iteration", "text_only")
ROUTING_ENUM_FIELDS = ("stage", "action_scope", "task_kind", "preference")
ROUTING_INPUT_FIELDS = ROUTING_ENUM_FIELDS + ROUTING_SCORE_FIELDS + ROUTING_BOOLEAN_FIELDS + ("risk_flags",)


class TrialDataError(ValueError):
    """Raised when trial state or an event violates the protocol."""


def parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise TrialDataError(f"{field} 必须是 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TrialDataError(f"{field} 不是有效 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise TrialDataError(f"{field} 必须包含时区")
    if parsed.utcoffset() != timedelta(hours=8):
        raise TrialDataError(f"{field} 必须使用 Asia/Shanghai 的 +08:00 时区")
    return parsed


def require(value: dict[str, Any], fields: set[str], label: str) -> None:
    missing = sorted(fields - value.keys())
    if missing:
        raise TrialDataError(f"{label} 缺少字段: {', '.join(missing)}")


def _require_non_empty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TrialDataError(f"{field} 必须是非空字符串")


def _require_non_negative_int(value: Any, field: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise TrialDataError(f"{field} 必须是大于等于 {minimum} 的整数")


def _is_number(value: Any) -> bool:
    return type(value) in {int, float}


def validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise TrialDataError("trial-state 必须是 JSON 对象")
    require(
        state,
        {
            "schema_version",
            "trial_id",
            "status",
            "started_at",
            "source_commit",
            "targets",
            "global_gate_enabled",
            "log_path",
        },
        "trial-state",
    )
    if state["schema_version"] != 1:
        raise TrialDataError("trial-state schema_version 必须为 1")
    if state["status"] not in {"active", "completed", "rolled_back"}:
        raise TrialDataError("trial-state status 非法")
    if state["global_gate_enabled"] is not False:
        raise TrialDataError("试运行阶段 global_gate_enabled 必须为 false")
    for field in ("trial_id", "source_commit", "log_path"):
        _require_non_empty_string(state[field], f"trial-state.{field}")
    if state["log_path"] != "evidence/trial-events.jsonl":
        raise TrialDataError("trial-state.log_path 必须指向 evidence/trial-events.jsonl")
    parse_time(state["started_at"], "trial-state.started_at")

    targets = state["targets"]
    if not isinstance(targets, dict):
        raise TrialDataError("trial-state.targets 必须是对象")
    require(
        targets,
        {
            "review_eligibility",
            "route_acceptance_rate_min",
            "high_risk_under_routing_max",
            "same_class_unexplained_route_changes_max",
            "duplicate_prompts_max",
            "verified_efficiency_signals_min",
        },
        "trial-state.targets",
    )
    eligibility = targets["review_eligibility"]
    if not isinstance(eligibility, dict):
        raise TrialDataError("trial-state.targets.review_eligibility 必须是对象")
    require(
        eligibility,
        {"elapsed_calendar_days_min", "completed_tasks_min", "operator"},
        "trial-state.targets.review_eligibility",
    )
    if eligibility["operator"] != "OR":
        raise TrialDataError("review_eligibility.operator 必须为 OR")
    _require_non_negative_int(
        eligibility["elapsed_calendar_days_min"],
        "review_eligibility.elapsed_calendar_days_min",
        minimum=1,
    )
    _require_non_negative_int(
        eligibility["completed_tasks_min"],
        "review_eligibility.completed_tasks_min",
        minimum=1,
    )
    rate = targets["route_acceptance_rate_min"]
    if not _is_number(rate) or not 0 <= rate <= 1:
        raise TrialDataError("route_acceptance_rate_min 必须是 0 到 1 的数字")
    for field in (
        "high_risk_under_routing_max",
        "same_class_unexplained_route_changes_max",
        "duplicate_prompts_max",
    ):
        _require_non_negative_int(targets[field], f"trial-state.targets.{field}")
    _require_non_negative_int(
        targets["verified_efficiency_signals_min"],
        "trial-state.targets.verified_efficiency_signals_min",
        minimum=1,
    )
    return state


def _routing_input(event: dict[str, Any]) -> dict[str, Any]:
    return {field: event[field] for field in ROUTING_INPUT_FIELDS}


def canonical_task_class(event: dict[str, Any]) -> str:
    return "/".join(
        (
            event["task_kind"],
            event["stage"],
            event["action_scope"],
            event["risk_floor"],
            event["preference"],
        )
    )


def comparison_signature(event: dict[str, Any]) -> tuple[Any, ...]:
    values: list[Any] = []
    for field in ROUTING_INPUT_FIELDS:
        value = event[field]
        values.append(tuple(sorted(value)) if field == "risk_flags" else value)
    return tuple(values)


def _validate_model_effort(model: Any, effort: Any, label: str) -> None:
    _require_non_empty_string(model, f"{label}.model")
    _require_non_empty_string(effort, f"{label}.effort")
    if model not in SUPPORTED_EFFORTS:
        raise TrialDataError(f"{label} 使用了不支持的模型")
    if effort not in SUPPORTED_EFFORTS[model]:
        raise TrialDataError(f"{label} 使用了模型不支持的档位")


def _validate_common(
    event: Any,
    index: int,
    state: dict[str, Any],
    seen: dict[str, dict[str, Any]],
    superseded_ids: set[str],
) -> dict[str, Any]:
    label = f"第 {index} 条事件"
    if not isinstance(event, dict):
        raise TrialDataError(f"{label} 必须是 JSON 对象")
    require(event, {"schema_version", "event_id", "event_type", "trial_id", "occurred_at", "task_id"}, label)
    if event["schema_version"] != 1:
        raise TrialDataError(f"{label} schema_version 必须为 1")
    if event["event_type"] not in EVENT_TYPES:
        raise TrialDataError(f"{label} event_type 非法")
    if event["trial_id"] != state["trial_id"]:
        raise TrialDataError(f"{label} trial_id 与状态文件不一致")
    _require_non_empty_string(event["event_id"], f"{label}.event_id")
    if event["event_id"] in seen:
        raise TrialDataError(f"{label} event_id 重复")
    parse_time(event["occurred_at"], f"{label}.occurred_at")
    note = event.get("evidence_note")
    if note is not None and not isinstance(note, str):
        raise TrialDataError(f"{label}.evidence_note 必须是字符串或 null")

    supersedes = event.get("supersedes_event_id")
    if supersedes is not None:
        if event["event_type"] == "trial_started":
            raise TrialDataError(f"{label} trial_started 不允许更正")
        if supersedes not in seen:
            raise TrialDataError(f"{label} supersedes_event_id 必须指向更早事件")
        if supersedes in superseded_ids:
            raise TrialDataError(f"{label} supersedes_event_id 已被更正；必须指向当前有效叶节点")
        prior = seen[supersedes]
        if prior["event_type"] != event["event_type"] or prior.get("task_id") != event.get("task_id"):
            raise TrialDataError(f"{label} 只能更正同类型、同 task_id 的事件")
    return event


def _validate_route(event: dict[str, Any], label: str) -> None:
    fields = {
        "project",
        "task_summary",
        "task_class",
        "router_version",
        "risk_floor",
        "route_trigger",
        "recommended_model",
        "recommended_effort",
        "relative_quota",
        "reason_codes",
        "user_choice",
        "selected_model",
        "selected_effort",
        "choice_reason",
        "duplicate_prompt",
    } | set(ROUTING_INPUT_FIELDS)
    require(event, fields, label)
    _require_non_empty_string(event["task_id"], f"{label}.task_id")
    _require_non_empty_string(event["task_summary"], f"{label}.task_summary")
    if event["project"] is not None:
        _require_non_empty_string(event["project"], f"{label}.project")
    try:
        task_input = validate_task(_routing_input(event))
    except InputError as exc:
        raise TrialDataError(f"{label} 原始路由输入非法: {exc}") from exc
    derived_floor = determine_risk_floor(task_input)
    if event["risk_floor"] != derived_floor:
        raise TrialDataError(f"{label} risk_floor 与原始输入计算结果不一致；应为 {derived_floor}")
    expected_route = route_task(task_input)
    if event["router_version"] != ROUTE_VERSION or event["router_version"] != expected_route["router_version"]:
        raise TrialDataError(f"{label} router_version 与锁定路由器不一致；应为 {ROUTE_VERSION}")
    expected_fields = {
        "risk_floor": expected_route["risk_floor"],
        "recommended_model": expected_route["model"],
        "recommended_effort": expected_route["effort"],
        "relative_quota": expected_route["relative_quota"],
        "reason_codes": expected_route["reason_codes"],
    }
    for field, expected_value in expected_fields.items():
        if event[field] != expected_value:
            raise TrialDataError(f"{label} {field} 与锁定路由器输出不一致；应为 {expected_value}")
    expected_class = canonical_task_class(event)
    if event["task_class"] != expected_class:
        raise TrialDataError(f"{label} task_class 必须为 {expected_class}")
    if event["route_trigger"] not in ROUTE_TRIGGERS:
        raise TrialDataError(f"{label} route_trigger 非法")
    _validate_model_effort(event["recommended_model"], event["recommended_effort"], f"{label}.recommended")
    _validate_model_effort(event["selected_model"], event["selected_effort"], f"{label}.selected")
    expected_quota = relative_quota(event["recommended_model"], event["recommended_effort"])
    if event["relative_quota"] != expected_quota:
        raise TrialDataError(f"{label} relative_quota 与推荐配置不一致；应为 {expected_quota}")
    if not isinstance(event["reason_codes"], list) or not event["reason_codes"]:
        raise TrialDataError(f"{label} reason_codes 必须是非空字符串数组")
    if any(not isinstance(item, str) or not item for item in event["reason_codes"]):
        raise TrialDataError(f"{label} reason_codes 必须是非空字符串数组")
    if event["user_choice"] not in USER_CHOICES:
        raise TrialDataError(f"{label} user_choice 非法")
    selected = (event["selected_model"], event["selected_effort"])
    recommended = (event["recommended_model"], event["recommended_effort"])
    if event["user_choice"] == "accept_recommended" and selected != recommended:
        raise TrialDataError(f"{label} 接受推荐时所选配置必须与推荐一致")
    if event["user_choice"] != "accept_recommended":
        _require_non_empty_string(event["choice_reason"], f"{label}.choice_reason")
    elif event["choice_reason"] is not None and not isinstance(event["choice_reason"], str):
        raise TrialDataError(f"{label}.choice_reason 必须是字符串或 null")
    if type(event["duplicate_prompt"]) is not bool:
        raise TrialDataError(f"{label} duplicate_prompt 必须是布尔值")


def _validate_quota_observation(quota: Any, label: str) -> None:
    if not isinstance(quota, dict):
        raise TrialDataError(f"{label} 必须是对象或 null")
    require(
        quota,
        {
            "pool",
            "window_id",
            "before_remaining_percent",
            "after_remaining_percent",
            "source",
            "reset_observed",
            "note",
        },
        label,
    )
    for field in ("pool", "window_id", "source"):
        _require_non_empty_string(quota[field], f"{label}.{field}")
    for field in ("before_remaining_percent", "after_remaining_percent"):
        value = quota[field]
        if not _is_number(value) or not 0 <= value <= 100:
            raise TrialDataError(f"{label}.{field} 必须为 0 到 100 的数字")
    if type(quota["reset_observed"]) is not bool:
        raise TrialDataError(f"{label}.reset_observed 必须是布尔值")
    if quota["note"] is not None and not isinstance(quota["note"], str):
        raise TrialDataError(f"{label}.note 必须是字符串或 null")
    if not quota["reset_observed"] and quota["after_remaining_percent"] > quota["before_remaining_percent"]:
        raise TrialDataError(f"{label} 未观察到重置时，剩余额度不能倒增")


def _validate_outcome(event: dict[str, Any], label: str) -> None:
    fields = {
        "status",
        "actual_model",
        "actual_effort",
        "quality_score",
        "rework_count",
        "elapsed_minutes",
        "quota_observation",
        "baseline",
        "acceptance_evidence",
    }
    require(event, fields, label)
    _require_non_empty_string(event["task_id"], f"{label}.task_id")
    if event["status"] not in OUTCOME_STATUSES:
        raise TrialDataError(f"{label} status 非法")
    actual_model = event["actual_model"]
    actual_effort = event["actual_effort"]
    if (actual_model is None) != (actual_effort is None):
        raise TrialDataError(f"{label} actual_model 与 actual_effort 必须同时填写或同时为 null")
    if event["status"] == "completed" and actual_model is None:
        raise TrialDataError(f"{label} completed 必须记录实际模型和档位")
    if actual_model is not None:
        _validate_model_effort(actual_model, actual_effort, f"{label}.actual")
    score = event["quality_score"]
    if score is not None and (not _is_number(score) or not 1 <= score <= 5):
        raise TrialDataError(f"{label} quality_score 必须为 1 到 5 或 null")
    rework = event["rework_count"]
    if rework is not None and (type(rework) is not int or rework < 0):
        raise TrialDataError(f"{label} rework_count 必须为非负整数或 null")
    elapsed = event["elapsed_minutes"]
    if elapsed is not None and (not _is_number(elapsed) or elapsed < 0):
        raise TrialDataError(f"{label} elapsed_minutes 必须为非负数字或 null")
    quota = event["quota_observation"]
    if quota is not None:
        _validate_quota_observation(quota, f"{label}.quota_observation")
    baseline = event["baseline"]
    if baseline is not None:
        if not isinstance(baseline, dict):
            raise TrialDataError(f"{label} baseline 必须是对象或 null")
        require(baseline, {"task_id", "task_class", "rework_count"}, f"{label}.baseline")
        _require_non_empty_string(baseline["task_id"], f"{label}.baseline.task_id")
        _require_non_empty_string(baseline["task_class"], f"{label}.baseline.task_class")
        baseline_rework = baseline["rework_count"]
        if baseline_rework is not None and (type(baseline_rework) is not int or baseline_rework < 0):
            raise TrialDataError(f"{label}.baseline.rework_count 必须为非负整数或 null")
    evidence = event["acceptance_evidence"]
    if evidence is not None and not isinstance(evidence, str):
        raise TrialDataError(f"{label}.acceptance_evidence 必须是字符串或 null")


def validate_event_log(state: dict[str, Any], events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list) or not events:
        raise TrialDataError("事件日志必须至少包含 trial_started")
    if not isinstance(events[0], dict) or events[0].get("event_type") != "trial_started":
        raise TrialDataError("事件日志第一条必须是 trial_started")

    seen: dict[str, dict[str, Any]] = {}
    superseded_ids: set[str] = set()
    active_leaf: dict[tuple[str, Any], str] = {}
    started_at = parse_time(state["started_at"], "trial-state.started_at")
    previous_time: datetime | None = None
    trial_started_count = 0

    for index, raw in enumerate(events, start=1):
        event = _validate_common(raw, index, state, seen, superseded_ids)
        label = f"第 {index} 条事件"
        occurred_at = parse_time(event["occurred_at"], f"{label}.occurred_at")
        if occurred_at < started_at:
            raise TrialDataError(f"{label} 不能早于试运行开始时间")
        if previous_time is not None and occurred_at < previous_time:
            raise TrialDataError(f"{label} occurred_at 不能早于前一条事件；日志必须按写入时间追加")

        if event["event_type"] == "trial_started":
            trial_started_count += 1
            if index != 1 or trial_started_count != 1:
                raise TrialDataError("日志只能有一个 trial_started，且必须是第一条")
            require(event, {"source_commit", "global_gate_enabled", "targets"}, label)
            if event["task_id"] is not None:
                raise TrialDataError(f"{label} trial_started.task_id 必须为 null")
            if event["source_commit"] != state["source_commit"] or event["targets"] != state["targets"]:
                raise TrialDataError(f"{label} 与状态基线不一致")
            if event["global_gate_enabled"] is not False:
                raise TrialDataError(f"{label} global_gate_enabled 必须为 false")
            if event["occurred_at"] != state["started_at"]:
                raise TrialDataError("trial-state.started_at 必须与首个 trial_started.occurred_at 完全一致")
        elif event["event_type"] == "route_decision":
            _validate_route(event, label)
        else:
            _validate_outcome(event, label)

        key = (event["event_type"], event.get("task_id"))
        supersedes = event.get("supersedes_event_id")
        if event["event_type"] != "trial_started":
            if supersedes is None and key in active_leaf:
                raise TrialDataError(f"{label} 同一 task_id 已有有效 {event['event_type']}；更正必须使用 supersedes_event_id")
            if supersedes is not None and active_leaf.get(key) != supersedes:
                raise TrialDataError(f"{label} 更正目标不是该任务当前有效叶节点")
            if event["event_type"] == "task_outcome" and ("route_decision", event["task_id"]) not in active_leaf:
                raise TrialDataError(f"{label} task_outcome 必须在对应 route_decision 之后追加")
            active_leaf[key] = event["event_id"]

        seen[event["event_id"]] = event
        if supersedes is not None:
            superseded_ids.add(supersedes)
        previous_time = occurred_at

    if trial_started_count != 1:
        raise TrialDataError("必须恰好有一个 trial_started 事件")
    return events


def _validate_active_baselines(active: list[dict[str, Any]], logical_indexes: dict[str, int]) -> None:
    decisions = {event["task_id"]: event for event in active if event["event_type"] == "route_decision"}
    outcomes = {event["task_id"]: event for event in active if event["event_type"] == "task_outcome"}
    for outcome in outcomes.values():
        baseline = outcome["baseline"]
        if baseline is None:
            continue
        task_id = outcome["task_id"]
        baseline_id = baseline["task_id"]
        if baseline_id == task_id:
            raise TrialDataError(f"任务 {task_id} 的 baseline 不能引用自身")
        prior_outcome = outcomes.get(baseline_id)
        prior_decision = decisions.get(baseline_id)
        current_decision = decisions.get(task_id)
        if prior_outcome is None or prior_decision is None:
            raise TrialDataError(f"任务 {task_id} 的 baseline 必须引用日志内已有任务")
        if prior_outcome["status"] != "completed":
            raise TrialDataError(f"任务 {task_id} 的 baseline 必须引用已闭环任务")
        if logical_indexes[prior_outcome["event_id"]] >= logical_indexes[outcome["event_id"]]:
            raise TrialDataError(f"任务 {task_id} 的 baseline 必须引用更早记录的闭环结果")
        if baseline["task_class"] != prior_decision["task_class"] or baseline["task_class"] != current_decision["task_class"]:
            raise TrialDataError(f"任务 {task_id} 的 baseline.task_class 与路由记录不一致")
        if comparison_signature(prior_decision) != comparison_signature(current_decision):
            raise TrialDataError(f"任务 {task_id} 的 baseline 与当前任务原始路由输入不可比")
        if baseline["rework_count"] != prior_outcome["rework_count"]:
            raise TrialDataError(f"任务 {task_id} 的 baseline.rework_count 与被引用结果不一致")


def _logical_indexes(
    cutoff: list[dict[str, Any]],
    active: list[dict[str, Any]],
) -> dict[str, int]:
    by_id = {event["event_id"]: event for event in cutoff}
    raw_indexes = {event["event_id"]: index for index, event in enumerate(cutoff)}
    roots: dict[str, str] = {}

    def root_id(event_id: str) -> str:
        if event_id in roots:
            return roots[event_id]
        chain: list[str] = []
        current_id = event_id
        while True:
            chain.append(current_id)
            supersedes = by_id[current_id].get("supersedes_event_id")
            if supersedes is None:
                root = current_id
                break
            current_id = supersedes
        for member in chain:
            roots[member] = root
        return root

    return {event["event_id"]: raw_indexes[root_id(event["event_id"])] for event in active}


def active_events_at(
    events: list[dict[str, Any]],
    as_of: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cutoff = [event for event in events if parse_time(event["occurred_at"], "occurred_at") <= as_of]
    if not cutoff or cutoff[0]["event_type"] != "trial_started":
        raise TrialDataError("as_of 早于试运行开始时间")
    superseded = {event["supersedes_event_id"] for event in cutoff if event.get("supersedes_event_id") is not None}
    active = [event for event in cutoff if event["event_id"] not in superseded]
    logical_indexes = _logical_indexes(cutoff, active)

    for event_type in ("route_decision", "task_outcome"):
        task_ids = [event["task_id"] for event in active if event["event_type"] == event_type]
        if len(task_ids) != len(set(task_ids)):
            raise TrialDataError(f"每个 task_id 只能有一个有效 {event_type}")
    decisions = {event["task_id"]: event for event in active if event["event_type"] == "route_decision"}
    for outcome in (event for event in active if event["event_type"] == "task_outcome"):
        decision = decisions.get(outcome["task_id"])
        if decision is None:
            raise TrialDataError("task_outcome 必须对应有效 route_decision")
        if logical_indexes[outcome["event_id"]] <= logical_indexes[decision["event_id"]]:
            raise TrialDataError("有效 task_outcome 不能早于有效 route_decision")
    _validate_active_baselines(active, logical_indexes)
    return active, logical_indexes


def below_high_risk_floor(model: Any, effort: Any) -> bool:
    return model != "gpt-5.6-sol" or effort not in SAFE_HIGH_RISK_EFFORTS


def _quota_consumed(observation: dict[str, Any]) -> float:
    return float(observation["before_remaining_percent"] - observation["after_remaining_percent"])


def _quota_pair_is_comparable(current: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return (
        not current["reset_observed"]
        and not baseline["reset_observed"]
        and current["pool"] == baseline["pool"]
        and current["window_id"] == baseline["window_id"]
        and current["source"] == baseline["source"]
        and current["before_remaining_percent"] <= baseline["after_remaining_percent"]
    )


def _has_outcome_evidence(outcome: dict[str, Any]) -> bool:
    return any(
        isinstance(outcome.get(field), str) and bool(outcome[field].strip())
        for field in ("acceptance_evidence", "evidence_note")
    )


def _summarize_at_observed_time(
    state_input: Any,
    events_input: Any,
    as_of: datetime,
    *,
    observed_now: datetime,
) -> dict[str, Any]:
    state = validate_state(state_input)
    if as_of.tzinfo is None or as_of.utcoffset() != timedelta(hours=8):
        raise TrialDataError("as_of 必须包含 Asia/Shanghai 的 +08:00 时区")
    if observed_now.tzinfo is None or observed_now.utcoffset() != timedelta(hours=8):
        raise TrialDataError("observed_now 必须包含 Asia/Shanghai 的 +08:00 时区")
    if as_of > observed_now:
        raise TrialDataError("as_of 不能晚于当前实际时间")
    started_at = parse_time(state["started_at"], "trial-state.started_at")
    if as_of < started_at:
        raise TrialDataError("as_of 不能早于试运行开始时间")
    validated = validate_event_log(state, events_input)
    events, logical_indexes = active_events_at(validated, as_of)
    elapsed_days = (as_of.date() - started_at.date()).days + 1

    decisions = sorted(
        (event for event in events if event["event_type"] == "route_decision"),
        key=lambda event: logical_indexes[event["event_id"]],
    )
    outcomes = {event["task_id"]: event for event in events if event["event_type"] == "task_outcome"}
    completed = [event for event in outcomes.values() if event["status"] == "completed"]
    accepted = sum(event["user_choice"] == "accept_recommended" for event in decisions)
    acceptance_rate = accepted / len(decisions) if decisions else None

    advisor_under_task_ids: set[str] = set()
    selected_under_task_ids: set[str] = set()
    actual_under_task_ids: set[str] = set()
    actual_pending_task_ids: set[str] = set()
    for decision in decisions:
        if decision["risk_floor"] not in {"high", "critical"}:
            continue
        task_id = decision["task_id"]
        if below_high_risk_floor(decision["recommended_model"], decision["recommended_effort"]):
            advisor_under_task_ids.add(task_id)
        if below_high_risk_floor(decision["selected_model"], decision["selected_effort"]):
            selected_under_task_ids.add(task_id)
        outcome = outcomes.get(task_id)
        if outcome is None:
            actual_pending_task_ids.add(task_id)
        elif outcome["actual_model"] is not None:
            if below_high_risk_floor(outcome["actual_model"], outcome["actual_effort"]):
                actual_under_task_ids.add(task_id)
        else:
            actual_pending_task_ids.add(task_id)
    combined_under_task_ids = advisor_under_task_ids | actual_under_task_ids

    by_signature: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        by_signature[comparison_signature(decision)].append(decision)
    potential_changes = 0
    comparable_pairs = 0
    for group in by_signature.values():
        for previous, current in zip(group, group[1:]):
            comparable_pairs += 1
            if (previous["recommended_model"], previous["recommended_effort"]) != (
                current["recommended_model"],
                current["recommended_effort"],
            ):
                potential_changes += 1

    efficiency_task_ids: set[str] = set()
    rework_signal_task_ids: set[str] = set()
    quota_signal_task_ids: set[str] = set()
    comparable_rework = 0
    rework_deltas: list[int] = []
    comparable_quota = 0
    quota_deltas: list[float] = []
    quota_observation_count = 0
    quality_scores: list[float] = []
    for outcome in completed:
        task_id = outcome["task_id"]
        if outcome["quality_score"] is not None:
            quality_scores.append(float(outcome["quality_score"]))
        if outcome["quota_observation"] is not None:
            quota_observation_count += 1
        baseline_ref = outcome["baseline"]
        if baseline_ref is None:
            continue
        baseline_outcome = outcomes[baseline_ref["task_id"]]
        evidence_pair = _has_outcome_evidence(outcome) and _has_outcome_evidence(baseline_outcome)
        if evidence_pair and outcome["rework_count"] is not None and baseline_outcome["rework_count"] is not None:
            comparable_rework += 1
            delta = baseline_outcome["rework_count"] - outcome["rework_count"]
            rework_deltas.append(delta)
            if delta > 0:
                rework_signal_task_ids.add(task_id)
                efficiency_task_ids.add(task_id)
        current_quota = outcome["quota_observation"]
        baseline_quota = baseline_outcome["quota_observation"]
        quota_notes_present = (
            current_quota is not None
            and baseline_quota is not None
            and isinstance(current_quota.get("note"), str)
            and bool(current_quota["note"].strip())
            and isinstance(baseline_quota.get("note"), str)
            and bool(baseline_quota["note"].strip())
        )
        if (
            evidence_pair
            and quota_notes_present
            and current_quota is not None
            and baseline_quota is not None
            and _quota_pair_is_comparable(current_quota, baseline_quota)
        ):
            comparable_quota += 1
            quota_delta = _quota_consumed(baseline_quota) - _quota_consumed(current_quota)
            quota_deltas.append(quota_delta)
            if quota_delta > 0:
                quota_signal_task_ids.add(task_id)
                efficiency_task_ids.add(task_id)

    targets = state["targets"]
    review_target = targets["review_eligibility"]
    review_due = (
        elapsed_days >= review_target["elapsed_calendar_days_min"]
        or len(completed) >= review_target["completed_tasks_min"]
    )
    duplicate_prompt_count = sum(event["duplicate_prompt"] for event in decisions)
    metrics_pass = (
        acceptance_rate is not None
        and acceptance_rate >= targets["route_acceptance_rate_min"]
        and len(combined_under_task_ids) <= targets["high_risk_under_routing_max"]
        and not actual_pending_task_ids
        and potential_changes <= targets["same_class_unexplained_route_changes_max"]
        and duplicate_prompt_count <= targets["duplicate_prompts_max"]
        and len(efficiency_task_ids) >= targets["verified_efficiency_signals_min"]
    )
    if state["status"] == "rolled_back":
        review_status = "rolled_back"
    elif state["status"] == "completed":
        review_status = "trial_completed"
    elif not review_due:
        review_status = "not_due"
    elif metrics_pass:
        review_status = "eligible_for_user_review"
    else:
        review_status = "continue_trial_or_insufficient_evidence"

    return {
        "trial_id": state["trial_id"],
        "status": state["status"],
        "as_of": as_of.isoformat(),
        "elapsed_calendar_days": elapsed_days,
        "route_decision_count": len(decisions),
        "completed_task_count": len(completed),
        "acceptance": {"accepted": accepted, "denominator": len(decisions), "rate": acceptance_rate},
        "high_risk": {
            "advisor_under_routing_count": len(advisor_under_task_ids),
            "selected_under_routing_warning_count": len(selected_under_task_ids),
            "actual_under_routing_count": len(actual_under_task_ids),
            "under_routing_task_count": len(combined_under_task_ids),
            "actual_configuration_pending_count": len(actual_pending_task_ids),
        },
        "stability": {
            "comparable_pairs": comparable_pairs,
            "potential_unexplained_route_change_count": potential_changes,
            "duplicate_prompt_count": duplicate_prompt_count,
        },
        "outcomes": {
            "quality_score_count": len(quality_scores),
            "mean_quality_score": sum(quality_scores) / len(quality_scores) if quality_scores else None,
            "comparable_rework_count": comparable_rework,
            "mean_rework_delta": sum(rework_deltas) / len(rework_deltas) if rework_deltas else None,
            "rework_improvement_signal_count": len(rework_signal_task_ids),
            "quota_observation_count": quota_observation_count,
            "comparable_quota_count": comparable_quota,
            "mean_quota_consumption_delta": sum(quota_deltas) / len(quota_deltas) if quota_deltas else None,
            "quota_improvement_signal_count": len(quota_signal_task_ids),
            "verified_efficiency_signal_count": len(efficiency_task_ids),
        },
        "review_due": review_due,
        "review_status": review_status,
        "global_gate_enabled": False,
    }


def summarize(state_input: Any, events_input: Any, as_of: datetime) -> dict[str, Any]:
    return _summarize_at_observed_time(
        state_input,
        events_input,
        as_of,
        observed_now=datetime.now().astimezone(),
    )


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise TrialDataError(f"事件日志第 {line_number} 行不是有效 JSON") from exc
    return events


def parse_args(argv: list[str]) -> argparse.Namespace:
    source_root = Path(__file__).resolve().parents[3]
    personal_root = Path("/Users/pc/Documents/model-routing-advisor")
    repo_root = source_root if (source_root / "evidence" / "trial-state.json").exists() else personal_root
    parser = argparse.ArgumentParser(description="校验并汇总模型路由试运行证据")
    parser.add_argument("--state", type=Path, default=repo_root / "evidence" / "trial-state.json")
    parser.add_argument("--events", type=Path, default=repo_root / "evidence" / "trial-events.jsonl")
    parser.add_argument("--as-of", help="Asia/Shanghai 的 ISO 8601 时间；默认当前时间")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
        events = load_events(args.events)
        as_of = parse_time(args.as_of, "--as-of") if args.as_of else datetime.now().astimezone()
        result = summarize(state, events, as_of)
    except (OSError, json.JSONDecodeError, TrialDataError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
