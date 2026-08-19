#!/usr/bin/env python3
"""Deterministic baseline router for the model-routing-advisor skill."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROUTE_VERSION = "0.1.0"

ENUMS = {
    "stage": {"investigate", "plan", "execute", "verify", "deploy"},
    "action_scope": {"read_only", "local_write", "external_effect"},
    "task_kind": {
        "coding",
        "research",
        "content",
        "media",
        "business",
        "governance",
        "operations",
    },
    "preference": {"balanced", "quota", "quality"},
}

SCORES = {
    "complexity",
    "ambiguity",
    "context_load",
    "tool_load",
    "error_cost",
    "latency_need",
    "repeatability",
}

BOOLEANS = {"parallelizable", "rapid_coding_iteration", "text_only"}

RISK_FLAGS = {
    "production",
    "public_release",
    "payment",
    "legal",
    "security",
    "privacy",
    "client_delivery",
    "irreversible",
}

SUPPORTED_EFFORTS = {
    "gpt-5.6-sol": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "gpt-5.6-terra": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "gpt-5.6-luna": {"low", "medium", "high", "xhigh", "max"},
    "gpt-5.3-codex-spark": {"low", "medium", "high", "xhigh"},
    "gpt-5.5": {"low", "medium", "high", "xhigh"},
}


class InputError(ValueError):
    """Raised when task classification input is invalid."""


def validate_task(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InputError("输入必须是 JSON 对象")

    required = set(ENUMS) | SCORES | BOOLEANS | {"risk_flags"}
    missing = sorted(required - raw.keys())
    unknown = sorted(raw.keys() - required)
    if missing:
        raise InputError(f"缺少字段: {', '.join(missing)}")
    if unknown:
        raise InputError(f"存在未知字段: {', '.join(unknown)}")

    task = dict(raw)
    for field, allowed in ENUMS.items():
        value = task[field]
        if value not in allowed:
            raise InputError(f"{field} 必须是: {', '.join(sorted(allowed))}")

    for field in SCORES:
        value = task[field]
        if type(value) is not int or not 1 <= value <= 5:
            raise InputError(f"{field} 必须是 1 到 5 的整数")

    for field in BOOLEANS:
        if type(task[field]) is not bool:
            raise InputError(f"{field} 必须是布尔值")

    flags = task["risk_flags"]
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise InputError("risk_flags 必须是字符串数组")
    invalid_flags = sorted(set(flags) - RISK_FLAGS)
    if invalid_flags:
        raise InputError(f"未知风险标记: {', '.join(invalid_flags)}")
    if len(flags) != len(set(flags)):
        raise InputError("risk_flags 不能包含重复项")

    return task


def determine_risk_floor(task: dict[str, Any]) -> str:
    if task["risk_flags"] or task["stage"] == "deploy":
        return "critical"
    if task["action_scope"] == "external_effect" or task["error_cost"] >= 4:
        return "high"
    load_fields = ("complexity", "ambiguity", "context_load", "tool_load")
    if task["error_cost"] == 3 or any(task[field] >= 4 for field in load_fields):
        return "medium"
    return "low"


def is_ultra_candidate(task: dict[str, Any]) -> bool:
    return (
        task["parallelizable"]
        and task["action_scope"] != "external_effect"
        and task["stage"] != "deploy"
        and task["complexity"] == 5
        and task["context_load"] == 5
        and task["tool_load"] >= 4
    )


def is_spark_candidate(task: dict[str, Any]) -> bool:
    return (
        task["task_kind"] == "coding"
        and task["rapid_coding_iteration"]
        and task["text_only"]
        and not task["risk_flags"]
        and task["action_scope"] != "external_effect"
        and task["complexity"] <= 2
        and task["ambiguity"] <= 2
        and task["context_load"] <= 3
        and task["tool_load"] <= 3
        and task["error_cost"] <= 2
    )


def is_luna_candidate(task: dict[str, Any]) -> bool:
    return (
        task["ambiguity"] <= 2
        and task["repeatability"] >= 4
        and task["complexity"] <= 3
        and task["tool_load"] <= 3
        and task["error_cost"] <= 2
        and task["action_scope"] != "external_effect"
        and not task["risk_flags"]
    )


def relative_quota(model: str, effort: str) -> str:
    if model == "gpt-5.3-codex-spark":
        return "低（独立额度池，需实测）"
    if model == "gpt-5.6-luna":
        return "低" if effort in {"low", "medium"} else "中"
    if model == "gpt-5.6-terra":
        return "中" if effort in {"low", "medium"} else "高"
    if effort in {"xhigh", "max", "ultra"}:
        return "很高"
    return "高"


def choose_baseline(task: dict[str, Any], risk_floor: str) -> tuple[str, str, list[str], list[str]]:
    if is_ultra_candidate(task):
        return (
            "gpt-5.6-sol",
            "ultra",
            ["parallelizable_large_task"],
            ["任务规模大且可拆成相互独立的部分，适合 Ultra 并行处理。"],
        )

    if risk_floor in {"critical", "high"}:
        effort = "xhigh" if task["stage"] == "deploy" or task["error_cost"] == 5 else "high"
        flags = "、".join(task["risk_flags"])
        reason = "任务触及高风险或外部影响，必须由 Sol 守住质量下限。"
        if flags:
            reason = f"任务命中风险标记（{flags}），必须由 Sol 守住质量下限。"
        reasons = [reason]
        codes = ["risk_floor_requires_sol"]
        if effort == "xhigh":
            codes.append("extreme_error_cost_or_deploy")
            reasons.append("生产部署或最高错误代价要求额外推理与复核深度。")
        return "gpt-5.6-sol", effort, codes, reasons

    if is_spark_candidate(task):
        return (
            "gpt-5.3-codex-spark",
            "high",
            ["rapid_low_risk_text_coding"],
            ["这是文本、范围清晰、低错误代价的小型编码迭代，适合 Spark 的低延迟反馈。"],
        )

    if is_luna_candidate(task):
        effort = "low" if task["complexity"] == 1 and task["preference"] == "quota" else "medium"
        return (
            "gpt-5.6-luna",
            effort,
            ["clear_repeatable_work"],
            ["任务清晰、可重复且错误易回滚，Luna 足以完成并节省通用额度。"],
        )

    if task["complexity"] >= 4 and (
        task["ambiguity"] >= 4 or task["context_load"] >= 4
    ):
        return (
            "gpt-5.6-sol",
            "high",
            ["complex_open_ended_work"],
            ["任务复杂且开放，需要 Sol 处理较大的上下文与不确定性。"],
        )

    demanding = any(
        task[field] >= 4 for field in ("complexity", "ambiguity", "context_load", "tool_load")
    ) or task["error_cost"] == 3
    effort = "high" if demanding else "medium"
    return (
        "gpt-5.6-terra",
        effort,
        ["everyday_tool_work"],
        ["这是需要稳定推理和工具使用的日常多步骤工作，Terra 提供较好的质量与额度平衡。"],
    )


def apply_preference(
    task: dict[str, Any],
    risk_floor: str,
    model: str,
    effort: str,
    codes: list[str],
    reasons: list[str],
) -> tuple[str, str, list[str], list[str]]:
    if task["preference"] == "quality" and effort == "medium":
        effort = "high"
        codes.append("quality_preference_effort_bump")
        reasons.append("用户偏向质量，因此在不启用 Max 或 Ultra 的前提下提高一档推理。")
    elif (
        task["preference"] == "quota"
        and risk_floor == "medium"
        and model == "gpt-5.6-terra"
        and effort == "high"
    ):
        effort = "medium"
        codes.append("quota_preference_safe_effort_drop")
        reasons.append("任务仍由 Terra 守住风险下限，但按额度偏好降低一档推理。")
    return model, effort, codes, reasons


def build_alternatives(task: dict[str, Any], risk_floor: str, model: str, effort: str) -> dict[str, Any]:
    if risk_floor in {"critical", "high"}:
        quota_model = "gpt-5.6-sol"
        quota_effort = "high"
        quota_condition = "仅可通过缩小为只读、沙箱或分阶段验证来节省；外部执行仍须保持风险下限。"
    elif risk_floor == "medium":
        quota_model = "gpt-5.6-terra"
        quota_effort = "medium"
        quota_condition = "先明确完成标准并缩小工具与上下文范围。"
    elif is_spark_candidate(task):
        quota_model = "gpt-5.3-codex-spark"
        quota_effort = "high"
        quota_condition = "保持文本输入、局部范围和可回滚验证。"
    else:
        quota_model = "gpt-5.6-luna"
        quota_effort = "medium"
        quota_condition = "任务必须清晰、重复且低错误代价。"

    if effort in {"xhigh", "ultra"}:
        quality_model, quality_effort = model, effort
    else:
        quality_model, quality_effort = "gpt-5.6-sol", "xhigh"

    return {
        "quota_saver": {
            "model": quota_model,
            "effort": quota_effort,
            "relative_quota": relative_quota(quota_model, quota_effort),
            "condition": quota_condition,
        },
        "quality_first": {
            "model": quality_model,
            "effort": quality_effort,
            "relative_quota": relative_quota(quality_model, quality_effort),
            "condition": "仅在代表性结果显示质量不足，或用户明确接受更高额度后使用。",
        },
    }


def route(task_input: Any) -> dict[str, Any]:
    task = validate_task(task_input)
    risk_floor = determine_risk_floor(task)
    model, effort, codes, reasons = choose_baseline(task, risk_floor)
    model, effort, codes, reasons = apply_preference(
        task, risk_floor, model, effort, codes, reasons
    )

    if effort not in SUPPORTED_EFFORTS[model]:
        raise RuntimeError(f"内部错误：{model} 不支持 {effort}")

    upgrade_when = [
        "动作范围升级为生产、付款、公开发布、客户交付、隐私、安全或不可逆操作。",
        "代表性结果出现事实遗漏、复杂依赖误判或需要显著返工。",
    ]
    downgrade_when = [
        "任务已缩小为完成标准明确、低歧义、可回滚的单一步骤。",
        "代表性样本证明更低配置仍能稳定通过同一验收标准。",
    ]

    return {
        "router_version": ROUTE_VERSION,
        "model": model,
        "effort": effort,
        "relative_quota": relative_quota(model, effort),
        "risk_floor": risk_floor,
        "reason_codes": codes,
        "reasons": reasons,
        "upgrade_when": upgrade_when,
        "downgrade_when": downgrade_when,
        "quota_guardrail": "额度不足时先缩小范围、分阶段、等待重置或使用额外 credits；不得突破风险下限。",
        "requires_user_confirmation": True,
        "alternatives": build_alternatives(task, risk_floor, model, effort),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据标准化任务字段生成模型路由基线")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", help="任务 JSON 字符串")
    source.add_argument("--input", type=Path, help="包含任务 JSON 对象的文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        raw_text = args.json if args.json is not None else args.input.read_text(encoding="utf-8")
        task_input = json.loads(raw_text)
        result = route(task_input)
    except (InputError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
