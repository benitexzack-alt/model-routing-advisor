#!/usr/bin/env python3
"""Behavior tests for the model-routing UserPromptSubmit hook."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
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

    def test_obvious_chat_and_simple_explanation_skip(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            chat = process_hook(payload("你好", session_id="chat"), state_dir=state_dir)
            explanation = process_hook(
                payload("什么是依赖注入？", session_id="explain"), state_dir=state_dir
            )

            self.assertIsNone(injected_context(chat))
            self.assertIsNone(injected_context(explanation))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual([event["reason"] for event in events], ["chat", "simple_explanation"])
            self.assertTrue(all(event["decision"] == "skip" for event in events))

    def test_archived_resume_rechecks_existing_session(self) -> None:
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
            self.assertIn('reason="continuity_check"', injected_context(acknowledged))
            self.assertIn('reason="resume"', injected_context(resumed))

            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"]["session-1"]
            self.assertNotIn("confirmed", json.dumps(session, ensure_ascii=False).lower())
            self.assertNotIn("last_trigger_signature", session)

    def test_same_stage_three_followups_are_checked_without_forcing_reroute(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            first = process_hook(
                payload("现在开始实现数据导入功能。", turn_id="turn-1"),
                state_dir=state_dir,
            )
            followups = [
                process_hook(
                    payload(text, turn_id=f"turn-{index}"), state_dir=state_dir
                )
                for index, text in enumerate(
                    ["按推荐执行。", "继续完善错误提示。", "再补一个本地单元测试。"],
                    start=2,
                )
            ]

            self.assertIn('reason="new_task"', injected_context(first))
            self.assertTrue(all(injected_context(result) is not None for result in followups))
            self.assertTrue(
                all(
                    'reason="continuity_check"' in injected_context(result)
                    for result in followups
                )
            )
            self.assertTrue(
                all("同阶段已确认则静默沿用" in injected_context(result) for result in followups)
            )
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(sum(event["decision"] == "inject" for event in events), 4)

    def test_explicit_stage_change_rechecks_once(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(
                payload("现在开始实现导入功能。", turn_id="turn-1"),
                state_dir=state_dir,
            )
            changed = process_hook(
                payload("现在进入验证阶段，运行回归测试。", turn_id="turn-2"),
                state_dir=state_dir,
            )
            continued = process_hook(
                payload("继续验证边界条件。", turn_id="turn-3"), state_dir=state_dir
            )

            self.assertIn('reason="stage_change"', injected_context(changed))
            self.assertIn('reason="continuity_check"', injected_context(continued))

    def test_high_risk_request_rechecks_before_action(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(
                payload("先整理本地部署清单。", turn_id="turn-1"),
                state_dir=state_dir,
            )
            risky = process_hook(
                payload("现在直接部署到生产环境并重启服务。", turn_id="turn-2"),
                state_dir=state_dir,
            )

            self.assertIn('reason="high_risk"', injected_context(risky))

    def test_micro_followup_skips(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("分析这个项目。"), state_dir=state_dir)
            result = process_hook(
                payload("好的", turn_id="turn-2"), state_dir=state_dir
            )
            self.assertIsNone(injected_context(result))

    def test_negated_production_action_is_not_labeled_high_risk(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("先分析本地方案。"), state_dir=state_dir)
            result = process_hook(
                payload("不要部署到生产环境，先保留本地方案。", turn_id="turn-2"),
                state_dir=state_dir,
            )

            context = injected_context(result)
            self.assertIn('reason="continuity_check"', context)
            self.assertNotIn('reason="high_risk"', context)

    def test_earlier_negated_action_does_not_hide_current_high_risk_intent(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("先分析本地方案。"), state_dir=state_dir)
            result = process_hook(
                payload("不要拖延现在部署到生产环境。", turn_id="turn-2"),
                state_dir=state_dir,
            )

            self.assertIn('reason="high_risk"', injected_context(result))

    def test_second_independent_task_in_same_session_is_not_silently_skipped(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("分析第一个项目。"), state_dir=state_dir)
            result = process_hook(
                payload("帮我整理另一批客户访谈记录。", turn_id="turn-2"),
                state_dir=state_dir,
            )

            context = injected_context(result)
            self.assertIsNotNone(context)
            self.assertIn('reason="continuity_check"', context)

    def test_duplicate_host_invocation_skips_but_same_prompt_new_turn_rechecks(self) -> None:
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
            self.assertIn('reason="continuity_check"', injected_context(next_turn))

            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [event["reason"] for event in events],
                ["new_task", "duplicate_hook_invocation", "continuity_check"],
            )
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["sessions"]["session-1"]["check_count"], 2)

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
