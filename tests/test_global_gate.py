#!/usr/bin/env python3
"""Behavior tests for the model-routing UserPromptSubmit hook."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "skills" / "model-routing-advisor" / "scripts"
SCRIPT_PATH = SCRIPT_DIR / "global_gate.py"
sys.path.insert(0, str(SCRIPT_DIR))

from global_gate import process_hook  # noqa: E402


def payload(
    prompt: str,
    *,
    session_id: str = "session-1",
    turn_id: str = "turn-1",
    cwd: str = "/tmp/example-project",
) -> dict[str, str]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": cwd,
        "prompt": prompt,
        "model": "gpt-5.6-sol",
        "permission_mode": "dontAsk",
        "transcript_path": "/tmp/session.jsonl",
    }


def injected_context(result: dict) -> Optional[str]:
    output = result.get("hookSpecificOutput")
    if not isinstance(output, dict):
        return None
    return output.get("additionalContext")


class GlobalGateBehaviorTests(unittest.TestCase):
    def test_new_substantive_task_injects_short_context_and_hash_only_log(self) -> None:
        with self.subTest("new task"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir)
                prompt = "我要开始开发一个新的客户数据导入功能。"
                result = process_hook(payload(prompt), state_dir=state_dir)

                context = injected_context(result)
                self.assertIsNotNone(context)
                self.assertIn("model-routing-advisor", context)
                self.assertIn('reason="new_task"', context)
                self.assertLess(len(context), 160)
                self.assertLess(len(context.encode("utf-8")), 480)
                self.assertNotEqual(result.get("decision"), "block")

                state_text = (state_dir / "gate-state.json").read_text(encoding="utf-8")
                log_lines = (state_dir / "gate-events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                self.assertEqual(len(log_lines), 1)
                event = json.loads(log_lines[0])
                self.assertEqual(event["session_id"], "session-1")
                self.assertEqual(event["turn_id"], "turn-1")
                self.assertEqual(event["cwd"], "/tmp/example-project")
                self.assertEqual(
                    event["prompt_sha256"],
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                )
                self.assertEqual(event["decision"], "inject")
                self.assertEqual(event["reason"], "new_task")
                self.assertNotIn(prompt, state_text)
                self.assertNotIn(prompt, log_lines[0])

    def test_chat_and_simple_explanation_do_not_consume_first_route_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            chat = process_hook(
                payload("你好", session_id="chat", turn_id="chat-1"),
                state_dir=state_dir,
            )
            after_chat = process_hook(
                payload(
                    "开始整理客户数据。",
                    session_id="chat",
                    turn_id="chat-2",
                ),
                state_dir=state_dir,
            )
            explanation = process_hook(
                payload(
                    "什么是依赖注入？",
                    session_id="explain",
                    turn_id="explain-1",
                ),
                state_dir=state_dir,
            )
            after_explanation = process_hook(
                payload(
                    "开始开发依赖注入示例。",
                    session_id="explain",
                    turn_id="explain-2",
                ),
                state_dir=state_dir,
            )

            self.assertIsNone(injected_context(chat))
            self.assertIn('reason="new_task"', injected_context(after_chat))
            self.assertIsNone(injected_context(explanation))
            self.assertIn('reason="new_task"', injected_context(after_explanation))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [event["reason"] for event in events],
                ["chat", "new_task", "simple_explanation", "new_task"],
            )

    def test_confirmation_and_archived_resume_do_not_repeat_route_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            first = process_hook(
                payload("开始整理客户数据。", turn_id="turn-1"), state_dir=state_dir
            )
            acknowledged = process_hook(
                payload("按推荐执行。", turn_id="turn-2"), state_dir=state_dir
            )
            resumed = process_hook(
                payload("恢复刚才归档的任务，继续整理。", turn_id="turn-3"),
                state_dir=state_dir,
            )

            self.assertIn('reason="new_task"', injected_context(first))
            self.assertIsNone(injected_context(acknowledged))
            self.assertIsNone(injected_context(resumed))

            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"]["session-1"]
            self.assertNotIn("confirmed", json.dumps(session, ensure_ascii=False).lower())
            self.assertTrue(session["route_prompt_shown"])
            self.assertTrue(session["route_selection_observed"])
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [event["reason"] for event in events],
                ["new_task", "route_already_set", "route_already_set"],
            )

    def test_same_stage_followups_do_not_repeat_route_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            first = process_hook(
                payload("现在开始实现数据导入功能。", turn_id="turn-1"),
                state_dir=state_dir,
            )
            confirmation = process_hook(
                payload("按推荐执行。", turn_id="turn-2"), state_dir=state_dir
            )
            followups = [
                process_hook(
                    payload(text, turn_id=f"turn-{index}"), state_dir=state_dir
                )
                for index, text in enumerate(
                    ["继续完善错误提示。", "再补一个本地单元测试。"],
                    start=3,
                )
            ]

            self.assertIn('reason="new_task"', injected_context(first))
            self.assertIsNone(injected_context(confirmation))
            self.assertTrue(all(injected_context(result) is None for result in followups))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(sum(event["decision"] == "inject" for event in events), 1)
            self.assertEqual(
                [event["reason"] for event in events[1:]],
                ["route_already_set", "route_already_set", "route_already_set"],
            )

    def test_explicit_stage_change_does_not_repeat_route_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(
                payload("现在开始实现导入功能。", turn_id="turn-1"),
                state_dir=state_dir,
            )
            process_hook(
                payload("按推荐执行。", turn_id="turn-2"), state_dir=state_dir
            )
            changed = process_hook(
                payload("现在进入验证阶段，运行回归测试。", turn_id="turn-3"),
                state_dir=state_dir,
            )
            continued = process_hook(
                payload("继续验证边界条件。", turn_id="turn-4"), state_dir=state_dir
            )

            self.assertIsNone(injected_context(changed))
            self.assertIsNone(injected_context(continued))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events[-2]["reason"], "route_already_set")
            self.assertEqual(events[-1]["reason"], "route_already_set")

    def test_high_risk_change_does_not_repeat_route_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(
                payload("先整理本地部署清单。", turn_id="turn-1"),
                state_dir=state_dir,
            )
            process_hook(
                payload("按推荐执行。", turn_id="turn-2"), state_dir=state_dir
            )
            risky = process_hook(
                payload("现在直接部署到生产环境并重启服务。", turn_id="turn-3"),
                state_dir=state_dir,
            )

            self.assertIsNone(injected_context(risky))
            event = json.loads(
                (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(event["reason"], "route_already_set")

    def test_micro_followup_skips(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("分析这个项目。"), state_dir=state_dir)
            result = process_hook(
                payload("好的", turn_id="turn-2"), state_dir=state_dir
            )
            self.assertIsNone(injected_context(result))

    def test_messages_before_first_selection_do_not_repeat_pending_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("先分析本地方案。"), state_dir=state_dir)
            result = process_hook(
                payload("现在进入执行阶段。", turn_id="turn-2"),
                state_dir=state_dir,
            )
            premature_reroute = process_hook(
                payload("重新选一下模型。", turn_id="turn-3"),
                state_dir=state_dir,
            )

            self.assertIsNone(injected_context(result))
            self.assertIsNone(injected_context(premature_reroute))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events[-2]["reason"], "route_prompt_pending")
            self.assertEqual(events[-1]["reason"], "route_prompt_pending")

    def test_negated_model_change_does_not_request_reroute(self) -> None:
        from tempfile import TemporaryDirectory

        prompts = (
            "不要重新选模型，继续执行。",
            "我不是要重新选择模型，只是问为什么。",
            "不要因为现在进入部署阶段就重新选择模型，继续原方案。",
            "我无意重选模型，继续现有路线。",
        )
        for index, prompt_text in enumerate(prompts, start=1):
            with self.subTest(prompt=prompt_text), TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir)
                session_id = f"negated-reroute-{index}"
                process_hook(
                    payload("先分析本地方案。", session_id=session_id),
                    state_dir=state_dir,
                )
                process_hook(
                    payload(
                        "按推荐执行。",
                        session_id=session_id,
                        turn_id="turn-2",
                    ),
                    state_dir=state_dir,
                )
                result = process_hook(
                    payload(
                        prompt_text,
                        session_id=session_id,
                        turn_id="turn-3",
                    ),
                    state_dir=state_dir,
                )

                self.assertIsNone(injected_context(result))
                event = json.loads(
                    (state_dir / "gate-events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()[-1]
                )
                self.assertEqual(event["reason"], "route_already_set")

    def test_business_model_service_changes_do_not_request_reroute(self) -> None:
        from tempfile import TemporaryDirectory

        prompts = (
            "现在进入部署阶段，请把模型服务切换到生产环境。",
            "请更换模型服务供应商，然后继续。",
            "调整数据模型字段后继续执行。",
            "把页面输出改为质量优先，然后继续验收。",
        )
        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("先分析部署方案。"), state_dir=state_dir)
            process_hook(
                payload("按推荐执行。", turn_id="turn-2"), state_dir=state_dir
            )

            for index, prompt_text in enumerate(prompts, start=3):
                result = process_hook(
                    payload(prompt_text, turn_id=f"turn-{index}"),
                    state_dir=state_dir,
                )
                self.assertIsNone(injected_context(result))

            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(
                all(event["reason"] == "route_already_set" for event in events[2:])
            )

    def test_second_independent_task_in_same_session_reuses_route(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("分析第一个项目。"), state_dir=state_dir)
            process_hook(
                payload("按推荐执行。", turn_id="turn-2"), state_dir=state_dir
            )
            result = process_hook(
                payload("帮我整理另一批客户访谈记录。", turn_id="turn-3"),
                state_dir=state_dir,
            )

            self.assertIsNone(injected_context(result))
            event = json.loads(
                (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(event["reason"], "route_already_set")

    def test_duplicate_host_invocation_and_later_turn_do_not_repeat_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            request = payload("开始整理新的客户资料。")

            first = process_hook(request, state_dir=state_dir)
            duplicate = process_hook(request, state_dir=state_dir)
            next_turn = process_hook(
                payload("开始整理新的客户资料。", turn_id="turn-2"),
                state_dir=state_dir,
            )

            self.assertIn('reason="new_task"', injected_context(first))
            self.assertIsNone(injected_context(duplicate))
            self.assertIsNone(injected_context(next_turn))

            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [event["reason"] for event in events],
                ["new_task", "duplicate_hook_invocation", "route_prompt_pending"],
            )
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"]["session-1"]
            self.assertEqual(session["route_prompt_count"], 1)
            self.assertTrue(session["route_prompt_shown"])
            self.assertFalse(session["route_selection_observed"])

    def test_three_initial_choice_phrases_never_trigger_a_second_route_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        choices = ("按推荐执行", "优先节省额度", "优先保证质量")
        for index, choice in enumerate(choices, start=1):
            with self.subTest(choice=choice), TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir)
                session_id = f"choice-{index}"
                first = process_hook(
                    payload("开始一个新的项目。", session_id=session_id),
                    state_dir=state_dir,
                )
                selected = process_hook(
                    payload(choice, session_id=session_id, turn_id="turn-2"),
                    state_dir=state_dir,
                )

                self.assertIn('reason="new_task"', injected_context(first))
                self.assertIsNone(injected_context(selected))
                event = json.loads(
                    (state_dir / "gate-events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()[-1]
                )
                self.assertEqual(event["reason"], "route_already_set")
                state = json.loads(
                    (state_dir / "gate-state.json").read_text(encoding="utf-8")
                )
                session = state["sessions"][session_id]
                self.assertTrue(session["route_selection_observed"])
                self.assertEqual(session["route_prompt_count"], 1)

    def test_initial_choice_with_followup_instruction_is_observed_but_negation_is_not(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(
                payload("开始一个新的项目。", session_id="choice-with-detail"),
                state_dir=state_dir,
            )
            selected = process_hook(
                payload(
                    "好的，优先保证质量。只确认并继续沿用，不要再展示路由卡。",
                    session_id="choice-with-detail",
                    turn_id="turn-2",
                ),
                state_dir=state_dir,
            )
            self.assertIsNone(injected_context(selected))
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                state["sessions"]["choice-with-detail"]["route_selection_observed"]
            )

            process_hook(
                payload("开始另一个项目。", session_id="negated-choice"),
                state_dir=state_dir,
            )
            rejected = process_hook(
                payload(
                    "不要按推荐执行，先解释原卡。",
                    session_id="negated-choice",
                    turn_id="turn-2",
                ),
                state_dir=state_dir,
            )
            self.assertIsNone(injected_context(rejected))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events[-1]["reason"], "route_prompt_pending")
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            self.assertFalse(
                state["sessions"]["negated-choice"]["route_selection_observed"]
            )

    def test_confirmation_questions_and_alternatives_stay_pending(self) -> None:
        from tempfile import TemporaryDirectory

        prompts = (
            "按推荐执行？",
            "按推荐执行，还是优先节省额度？",
            "使用 GPT-5.6-Sol high 可以吗？",
        )
        for index, prompt_text in enumerate(prompts, start=1):
            with self.subTest(prompt=prompt_text), TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir)
                session_id = f"question-choice-{index}"
                process_hook(
                    payload("开始一个新的项目。", session_id=session_id),
                    state_dir=state_dir,
                )
                result = process_hook(
                    payload(
                        prompt_text,
                        session_id=session_id,
                        turn_id="turn-2",
                    ),
                    state_dir=state_dir,
                )

                self.assertIsNone(injected_context(result))
                state = json.loads(
                    (state_dir / "gate-state.json").read_text(encoding="utf-8")
                )
                session = state["sessions"][session_id]
                self.assertFalse(session["route_selection_observed"])
                self.assertEqual(session["last_reason"], "route_prompt_pending")

    def test_declarative_choice_and_explicit_configuration_confirm(self) -> None:
        from tempfile import TemporaryDirectory

        prompts = (
            "就按推荐执行吧。",
            "使用 GPT-5.6-Sol high。",
            "Sol ultra。",
            "按推荐执行。接下来可以继续吗？",
        )
        for index, prompt_text in enumerate(prompts, start=1):
            with self.subTest(prompt=prompt_text), TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir)
                session_id = f"declarative-choice-{index}"
                process_hook(
                    payload("开始一个新的项目。", session_id=session_id),
                    state_dir=state_dir,
                )
                result = process_hook(
                    payload(
                        prompt_text,
                        session_id=session_id,
                        turn_id="turn-2",
                    ),
                    state_dir=state_dir,
                )

                self.assertIsNone(injected_context(result))
                state = json.loads(
                    (state_dir / "gate-state.json").read_text(encoding="utf-8")
                )
                session = state["sessions"][session_id]
                self.assertTrue(session["route_selection_observed"])
                self.assertEqual(session["last_reason"], "route_already_set")
                self.assertEqual(session["route_prompt_count"], 1)

    def test_explicit_change_after_selection_can_request_one_new_route_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        requests = (
            "重选模型",
            "重新选一下模型",
            "把模型换成 Terra",
            "把推理档位调到 high",
            "改成优先节省额度",
            "改成优先保证质量",
            "改为质量优先",
            "改回按推荐执行",
            "把模型路由改为平衡",
            "再给我一张模型路由卡",
        )
        for index, request in enumerate(requests, start=1):
            with self.subTest(request=request), TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir)
                session_id = f"reroute-{index}"
                process_hook(
                    payload("开始一个新的项目。", session_id=session_id),
                    state_dir=state_dir,
                )
                process_hook(
                    payload(
                        "按推荐执行。",
                        session_id=session_id,
                        turn_id="turn-2",
                    ),
                    state_dir=state_dir,
                )
                rerouted = process_hook(
                    payload(request, session_id=session_id, turn_id="turn-3"),
                    state_dir=state_dir,
                )
                pending_followup = process_hook(
                    payload(
                        request,
                        session_id=session_id,
                        turn_id="turn-4",
                    ),
                    state_dir=state_dir,
                )

                self.assertIn('reason="user_requested"', injected_context(rerouted))
                self.assertIsNone(injected_context(pending_followup))
                events = [
                    json.loads(line)
                    for line in (state_dir / "gate-events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(events[-1]["reason"], "route_prompt_pending")
                state = json.loads(
                    (state_dir / "gate-state.json").read_text(encoding="utf-8")
                )
                session = state["sessions"][session_id]
                self.assertFalse(session["route_selection_observed"])
                self.assertEqual(session["route_prompt_count"], 2)

    def test_legacy_check_count_migrates_to_sticky_route_without_duplicate(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            legacy_prompt = "旧版本已经展示过路由卡。"
            legacy_state = {
                "schema_version": 1,
                "sessions": {
                    "legacy-session": {
                        "session_id": "legacy-session",
                        "turn_id": "legacy-turn",
                        "cwd": "/tmp/example-project",
                        "last_prompt_sha256": hashlib.sha256(
                            legacy_prompt.encode("utf-8")
                        ).hexdigest(),
                        "last_decision": "inject",
                        "last_reason": "new_task",
                        "last_observed_signature": "stage:general",
                        "check_count": 1,
                        "last_seen_at": "2026-08-23T00:00:00+00:00",
                    }
                },
            }
            (state_dir / "gate-state.json").write_text(
                json.dumps(legacy_state, ensure_ascii=False), encoding="utf-8"
            )

            result = process_hook(
                payload(
                    "继续执行。",
                    session_id="legacy-session",
                    turn_id="new-turn",
                ),
                state_dir=state_dir,
            )

            self.assertIsNone(injected_context(result))
            first_event = json.loads(
                (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(first_event["reason"], "route_already_set")

            reroute = process_hook(
                payload(
                    "重新选一下模型。",
                    session_id="legacy-session",
                    turn_id="reroute-turn",
                ),
                state_dir=state_dir,
            )
            self.assertIn('reason="user_requested"', injected_context(reroute))
            event = json.loads(
                (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(event["reason"], "user_requested")
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            migrated = state["sessions"]["legacy-session"]
            self.assertTrue(migrated["route_prompt_shown"])
            self.assertEqual(migrated["route_prompt_count"], 2)
            self.assertFalse(migrated["route_selection_observed"])
            self.assertNotIn("check_count", migrated)

    def test_each_session_gets_its_own_first_route_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            first = process_hook(
                payload("开始项目甲。", session_id="session-a"), state_dir=state_dir
            )
            second = process_hook(
                payload("开始项目乙。", session_id="session-b"), state_dir=state_dir
            )

            self.assertIn('reason="new_task"', injected_context(first))
            self.assertIn('reason="new_task"', injected_context(second))

    def test_confirmed_session_is_not_evicted_after_one_thousand_later_sessions(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            sessions = {
                "archived-session": {
                    "session_id": "archived-session",
                    "turn_id": "old-turn",
                    "cwd": "/tmp/example-project",
                    "last_prompt_sha256": "old-hash",
                    "last_decision": "skip",
                    "last_reason": "route_already_set",
                    "last_observed_signature": "routing:initial",
                    "route_prompt_count": 1,
                    "route_prompt_shown": True,
                    "route_selection_observed": True,
                    "last_seen_at": "2026-01-01T00:00:00+00:00",
                }
            }
            for index in range(1000):
                session_id = f"later-session-{index:04d}"
                sessions[session_id] = {
                    "session_id": session_id,
                    "last_seen_at": f"2026-02-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
                }
            (state_dir / "gate-state.json").write_text(
                json.dumps({"schema_version": 1, "sessions": sessions}),
                encoding="utf-8",
            )

            process_hook(
                payload(
                    "开始一个全新的任务。",
                    session_id="overflow-session",
                    turn_id="overflow-turn",
                ),
                state_dir=state_dir,
            )
            resumed = process_hook(
                payload(
                    "恢复旧归档任务并继续。",
                    session_id="archived-session",
                    turn_id="resume-turn",
                ),
                state_dir=state_dir,
            )

            self.assertIsNone(injected_context(resumed))
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(state["sessions"]), 1002)
            archived = state["sessions"]["archived-session"]
            self.assertEqual(archived["last_reason"], "route_already_set")
            self.assertEqual(archived["route_prompt_count"], 1)

    def test_bad_payload_warns_and_fails_open(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            result = process_hook(
                {"hook_event_name": "UserPromptSubmit", "prompt": "开始项目"},
                state_dir=Path(temp_dir),
            )

            self.assertTrue(result["continue"])
            self.assertIn("门禁告警", result["systemMessage"])
            self.assertNotEqual(result.get("decision"), "block")

    def test_unwritable_state_warns_and_fails_open(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            invalid_state_dir = Path(temp_dir) / "not-a-directory"
            invalid_state_dir.write_text("occupied", encoding="utf-8")
            result = process_hook(
                payload("开始一个新的重要项目。"), state_dir=invalid_state_dir
            )

            self.assertTrue(result["continue"])
            self.assertIn("门禁告警", result["systemMessage"])
            self.assertIn("fail-open", result["systemMessage"])
            self.assertNotEqual(result.get("decision"), "block")

    def test_existing_state_directory_mode_is_preserved(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "existing-state"
            state_dir.mkdir(mode=0o755)
            state_dir.chmod(0o755)

            result = process_hook(
                payload("开始一个新的重要项目。"), state_dir=state_dir
            )

            self.assertIn('reason="new_task"', injected_context(result))
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o755)
            self.assertTrue((state_dir / "gate-state.json").is_file())
            self.assertTrue((state_dir / "gate-events.jsonl").is_file())

    def test_new_state_directory_is_private_without_changing_existing_parent(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "existing-parent"
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            state_dir = parent / "new-state"

            result = process_hook(
                payload("开始一个新的重要项目。"), state_dir=state_dir
            )

            self.assertIn('reason="new_task"', injected_context(result))
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)

    def test_shared_temporary_root_is_rejected_without_changing_mode(self) -> None:
        shared_temp_root = Path(tempfile.gettempdir()).resolve()
        original_mode = stat.S_IMODE(shared_temp_root.stat().st_mode)

        result = process_hook(
            payload("开始一个新的重要项目。"), state_dir=shared_temp_root
        )

        self.assertTrue(result["continue"])
        self.assertIn("state_unavailable:GateStateError", result["systemMessage"])
        self.assertIn("fail-open", result["systemMessage"])
        self.assertIn('reason="gate_error"', injected_context(result))
        self.assertEqual(stat.S_IMODE(shared_temp_root.stat().st_mode), original_mode)
        self.assertNotEqual(result.get("decision"), "block")

    def test_existing_world_writable_state_directory_is_rejected_unchanged(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "unsafe-state"
            state_dir.mkdir(mode=0o777)
            state_dir.chmod(0o777)

            result = process_hook(
                payload("开始一个新的重要项目。"), state_dir=state_dir
            )

            self.assertTrue(result["continue"])
            self.assertIn("state_unavailable:GateStateError", result["systemMessage"])
            self.assertIn('reason="gate_error"', injected_context(result))
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o777)
            self.assertEqual(list(state_dir.iterdir()), [])

    def test_cli_rejects_shared_temporary_root_from_environment(self) -> None:
        shared_temp_root = Path(tempfile.gettempdir()).resolve()
        original_mode = stat.S_IMODE(shared_temp_root.stat().st_mode)
        env = dict(os.environ)
        env["MODEL_ROUTING_GATE_STATE_DIR"] = str(shared_temp_root)

        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            input=json.dumps(payload("开始一个新的重要项目。")),
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

        self.assertEqual(completed.returncode, 0)
        result = json.loads(completed.stdout)
        self.assertTrue(result["continue"])
        self.assertIn("state_unavailable:GateStateError", result["systemMessage"])
        self.assertIn('reason="gate_error"', injected_context(result))
        self.assertEqual(stat.S_IMODE(shared_temp_root.stat().st_mode), original_mode)
        self.assertEqual(completed.stderr, "")

    def test_corrupt_state_warns_and_fails_open(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            (state_dir / "gate-state.json").write_text("{broken", encoding="utf-8")
            result = process_hook(payload("开始一个新的重要项目。"), state_dir=state_dir)

            self.assertTrue(result["continue"])
            self.assertIn("state_unavailable:GateStateError", result["systemMessage"])
            self.assertIn('reason="gate_error"', injected_context(result))
            self.assertNotEqual(result.get("decision"), "block")

    def test_cli_always_emits_valid_hook_json_for_invalid_stdin(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            env = dict(os.environ)
            env["MODEL_ROUTING_GATE_STATE_DIR"] = temp_dir
            completed = subprocess.run(
                [sys.executable, str(SCRIPT_PATH)],
                input="{not-json",
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(completed.returncode, 0)
            result = json.loads(completed.stdout)
            self.assertTrue(result["continue"])
            self.assertIn("门禁告警", result["systemMessage"])
            self.assertEqual(completed.stderr, "")

    def test_cli_uses_injected_state_root_independent_of_working_directory(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir, TemporaryDirectory() as work_dir:
            state_dir = Path(temp_dir) / "nested" / "gate"
            env = dict(os.environ)
            env["MODEL_ROUTING_GATE_STATE_DIR"] = str(state_dir)
            completed = subprocess.run(
                [sys.executable, str(SCRIPT_PATH)],
                input=json.dumps(payload("开始整理一个新的客户资料库。")),
                text=True,
                capture_output=True,
                check=False,
                cwd=work_dir,
                env=env,
            )

            self.assertEqual(completed.returncode, 0)
            result = json.loads(completed.stdout)
            self.assertIn('reason="new_task"', injected_context(result))
            self.assertTrue((state_dir / "gate-state.json").is_file())
            self.assertTrue((state_dir / "gate-events.jsonl").is_file())
            self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
