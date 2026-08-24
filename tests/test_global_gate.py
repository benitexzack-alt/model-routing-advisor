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
from unittest import mock


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
    transcript_path: str = "/tmp/session.jsonl",
) -> dict[str, str]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": cwd,
        "prompt": prompt,
        "model": "gpt-5.6-sol",
        "permission_mode": "dontAsk",
        "transcript_path": transcript_path,
    }


def write_transcript(
    codex_home: Path,
    session_id: str,
    *,
    day: str = "2026/08/24",
    thread_source: str = "automation",
    recorded_id: Optional[str] = None,
) -> Path:
    transcript_dir = codex_home / "sessions" / day
    transcript_dir.mkdir(parents=True, exist_ok=True)
    date_label = day.replace("/", "-")
    transcript_path = (
        transcript_dir / f"rollout-{date_label}T01-00-00-{session_id}.jsonl"
    )
    metadata_id = recorded_id or session_id
    record = {
        "timestamp": "2026-08-24T01:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": metadata_id,
            "session_id": metadata_id,
            "thread_source": thread_source,
        },
    }
    transcript_path.write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return transcript_path


def write_automation_config(
    codex_home: Path,
    automation_id: str,
    *,
    kind: str,
    prompt: str,
    name: str = "测试自动化",
    status: str = "ACTIVE",
    target_thread_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    cwds: Optional[list[str]] = None,
    configured_id: Optional[str] = None,
) -> Path:
    automation_dir = codex_home / "automations" / automation_id
    automation_dir.mkdir(parents=True, exist_ok=True)
    fields = [
        "version = 1",
        f"id = {json.dumps(configured_id or automation_id, ensure_ascii=False)}",
        f"kind = {json.dumps(kind, ensure_ascii=False)}",
        f"name = {json.dumps(name, ensure_ascii=False)}",
        f"prompt = {json.dumps(prompt, ensure_ascii=False)}",
        f"status = {json.dumps(status, ensure_ascii=False)}",
    ]
    if target_thread_id is not None:
        fields.append(
            f"target_thread_id = {json.dumps(target_thread_id, ensure_ascii=False)}"
        )
    if model is not None:
        fields.append(f"model = {json.dumps(model, ensure_ascii=False)}")
    if reasoning_effort is not None:
        fields.append(
            f"reasoning_effort = {json.dumps(reasoning_effort, ensure_ascii=False)}"
        )
    if cwds is not None:
        fields.append(f"cwds = {json.dumps(cwds, ensure_ascii=False)}")
    config_path = automation_dir / "automation.toml"
    config_path.write_text("\n".join(fields) + "\n", encoding="utf-8")
    return config_path


def heartbeat_envelope(
    automation_id: str,
    instructions: str,
    *,
    current_time: str = "2026-08-25T00:31:41.123Z",
) -> str:
    return (
        "<heartbeat>\n"
        f"  <automation_id>{automation_id}</automation_id>\n"
        f"  <current_time_iso>{current_time}</current_time_iso>\n"
        "  <instructions>\n"
        f"{instructions}\n"
        "  </instructions>\n"
        "</heartbeat>\n"
    )


def cron_envelope(
    automation_id: str,
    name: str,
    instructions: str,
    *,
    last_run: str = "never",
) -> str:
    return (
        f"Automation: {name}\n"
        f"Automation ID: {automation_id}\n"
        f"Automation memory: $CODEX_HOME/automations/{automation_id}/memory.md\n"
        f"Last run: {last_run}\n\n"
        f"{instructions}"
    )


def injected_context(result: dict) -> Optional[str]:
    output = result.get("hookSpecificOutput")
    if not isinstance(output, dict):
        return None
    return output.get("additionalContext")


class GlobalGateBehaviorTests(unittest.TestCase):
    def assert_scheduled_context(self, result: dict) -> None:
        context = injected_context(result)
        self.assertIsNotNone(context)
        self.assertIn('reason="scheduled_automation"', context)
        self.assertIn("routing-not-required", context)
        self.assertIn("不生成模型路由卡", context)
        self.assertIn("不等待确认", context)
        self.assertIn("直接执行本次自动化", context)
        self.assertIn("其他安全、权限及现实行动门禁照常", context)
        self.assertNotIn("给出路由卡并等待选择", context)
        self.assertNotIn("请用户确认", context)

    def test_distinct_daily_automation_sessions_are_exempt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            automation_id = "daily-cron"
            automation_name = "每日任务"
            instructions = "生成今天的每日任务报告。"
            write_automation_config(
                codex_home,
                automation_id,
                kind="cron",
                prompt=instructions,
                name=automation_name,
                model="gpt-5.6-sol",
                reasoning_effort="low",
                cwds=["/tmp/example-project"],
            )
            first_path = write_transcript(
                codex_home,
                "daily-session-one",
                day="2026/08/24",
            )
            second_path = write_transcript(
                codex_home,
                "daily-session-two",
                day="2026/08/25",
            )
            first_prompt = cron_envelope(
                automation_id,
                automation_name,
                instructions,
            )
            second_prompt = cron_envelope(
                automation_id,
                automation_name,
                instructions,
                last_run="2026-08-24T01:00:00.000Z (1787533200000)",
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                first = process_hook(
                    payload(
                        first_prompt,
                        session_id="daily-session-one",
                        transcript_path=str(first_path),
                    ),
                    state_dir=state_dir,
                )
                second = process_hook(
                    payload(
                        second_prompt,
                        session_id="daily-session-two",
                        transcript_path=str(second_path),
                    ),
                    state_dir=state_dir,
                )

            self.assert_scheduled_context(first)
            self.assert_scheduled_context(second)
            state_text = (state_dir / "gate-state.json").read_text(encoding="utf-8")
            state = json.loads(state_text)
            for session_id in ("daily-session-one", "daily-session-two"):
                session = state["sessions"][session_id]
                self.assertTrue(session["automation_exempt"])
                self.assertFalse(session["route_prompt_shown"])
                self.assertEqual(session["route_prompt_count"], 0)
                self.assertEqual(session["last_reason"], "scheduled_automation")
            log_text = (state_dir / "gate-events.jsonl").read_text(encoding="utf-8")
            events = [json.loads(line) for line in log_text.splitlines()]
            self.assertEqual(
                [event["reason"] for event in events],
                ["scheduled_automation", "scheduled_automation"],
            )
            self.assertTrue(all(event["automation_exempt"] for event in events))
            self.assertNotIn(first_prompt, state_text)
            self.assertNotIn(second_prompt, state_text)
            self.assertNotIn(first_prompt, log_text)
            self.assertNotIn(second_prompt, log_text)
            self.assertNotIn(str(first_path), log_text)
            self.assertNotIn("thread_source", log_text)

    def test_automation_exemption_persists_for_later_turns_in_same_session(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            transcript_path = write_transcript(codex_home, "daily-followup")
            instructions = "执行今天的自动巡检。"
            write_automation_config(
                codex_home,
                "daily-followup-cron",
                kind="cron",
                prompt=instructions,
                name="每日巡检",
                model="gpt-5.6-sol",
                reasoning_effort="low",
                cwds=["/tmp/example-project"],
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                first = process_hook(
                    payload(
                        cron_envelope(
                            "daily-followup-cron",
                            "每日巡检",
                            instructions,
                        ),
                        session_id="daily-followup",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                followup = process_hook(
                    payload(
                        "继续生成巡检摘要。",
                        session_id="daily-followup",
                        turn_id="turn-2",
                        transcript_path="",
                    ),
                    state_dir=state_dir,
                )

            self.assert_scheduled_context(first)
            self.assertIsNone(injected_context(followup))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [event["reason"] for event in events],
                ["scheduled_automation", "automation_thread_followup"],
            )
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"]["daily-followup"]
            self.assertTrue(session["automation_exempt"])
            self.assertEqual(session["route_prompt_count"], 0)

    def test_automation_explicit_reroute_gets_one_replacement_card(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            session_id = "daily-reroute"
            transcript_path = write_transcript(codex_home, session_id)
            instructions = "重新选一下模型。"
            write_automation_config(
                codex_home,
                "daily-reroute-cron",
                kind="cron",
                prompt=instructions,
                name="每日重选词测试",
                model="gpt-5.6-sol",
                reasoning_effort="low",
                cwds=["/tmp/example-project"],
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                initial = process_hook(
                    payload(
                        cron_envelope(
                            "daily-reroute-cron",
                            "每日重选词测试",
                            instructions,
                        ),
                        session_id=session_id,
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                rerouted = process_hook(
                    payload(
                        "重新选一下模型。",
                        session_id=session_id,
                        turn_id="turn-2",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                duplicate_request = process_hook(
                    payload(
                        "重新选一下模型。",
                        session_id=session_id,
                        turn_id="turn-3",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                confirmed = process_hook(
                    payload(
                        "按推荐执行。",
                        session_id=session_id,
                        turn_id="turn-4",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                later_run = process_hook(
                    payload(
                        "继续运行每日巡检。",
                        session_id=session_id,
                        turn_id="turn-5",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )

            self.assert_scheduled_context(initial)
            self.assertIn('reason="user_requested"', injected_context(rerouted))
            self.assertIsNone(injected_context(duplicate_request))
            self.assertIsNone(injected_context(confirmed))
            self.assertIsNone(injected_context(later_run))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [event["reason"] for event in events],
                [
                    "scheduled_automation",
                    "user_requested",
                    "route_prompt_pending",
                    "route_already_set",
                    "automation_thread_followup",
                ],
            )
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"][session_id]
            self.assertTrue(session["automation_exempt"])
            self.assertTrue(session["route_selection_observed"])
            self.assertEqual(session["route_prompt_count"], 1)

    def test_old_automation_transcript_pending_state_does_not_swallow_manual_reroute(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            session_id = "old-automation-pending"
            transcript_path = write_transcript(codex_home, session_id)

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                old_pending = process_hook(
                    payload(
                        "开始旧自动化任务。",
                        session_id=session_id,
                        transcript_path="/missing/old-transcript.jsonl",
                    ),
                    state_dir=state_dir,
                )
                manual_reroute = process_hook(
                    payload(
                        "重新选一下模型。",
                        session_id=session_id,
                        turn_id="turn-2",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                repeated = process_hook(
                    payload(
                        "重新选一下模型。",
                        session_id=session_id,
                        turn_id="turn-3",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )

            self.assertIn('reason="new_task"', injected_context(old_pending))
            self.assertIn('reason="user_requested"', injected_context(manual_reroute))
            self.assertIsNone(injected_context(repeated))
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"][session_id]
            self.assertTrue(session["automation_exempt"])
            self.assertEqual(session["route_prompt_count"], 2)
            self.assertFalse(session["route_selection_observed"])
            self.assertEqual(session["last_reason"], "route_prompt_pending")

    def test_invalid_or_forged_transcripts_use_ordinary_routing(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            (codex_home / "sessions").mkdir(parents=True)

            outside_path = write_transcript(root / "outside-home", "outside-source")
            wrong_id_path = write_transcript(
                codex_home,
                "wrong-id-source",
                recorded_id="different-session",
            )
            manual_path = write_transcript(
                codex_home,
                "manual-source",
                thread_source="manual",
            )
            corrupt_path = write_transcript(codex_home, "corrupt-source")
            corrupt_path.write_text("{broken\n", encoding="utf-8")
            oversized_path = write_transcript(codex_home, "oversized-source")
            oversized_path.write_bytes(b"{" + b"x" * (1024 * 1024) + b"}\n")
            delayed_meta_path = write_transcript(codex_home, "delayed-meta-source")
            valid_meta = delayed_meta_path.read_text(encoding="utf-8")
            delayed_meta_path.write_text(
                json.dumps({"type": "event_msg", "payload": {"kind": "started"}})
                + "\n"
                + valid_meta,
                encoding="utf-8",
            )

            cases = (
                ("missing-source", "/path/that/does/not/exist.jsonl"),
                ("outside-source", str(outside_path)),
                ("wrong-id-source", str(wrong_id_path)),
                ("manual-source", str(manual_path)),
                ("corrupt-source", str(corrupt_path)),
                ("oversized-source", str(oversized_path)),
                ("delayed-meta-source", str(delayed_meta_path)),
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                for index, (session_id, transcript_path) in enumerate(cases, start=1):
                    with self.subTest(session_id=session_id):
                        result = process_hook(
                            payload(
                                "执行每日任务。",
                                session_id=session_id,
                                turn_id=f"turn-{index}",
                                transcript_path=transcript_path,
                            ),
                            state_dir=state_dir,
                        )
                        self.assertIn('reason="new_task"', injected_context(result))

            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            for session_id, _ in cases:
                self.assertFalse(state["sessions"][session_id]["automation_exempt"])

    def test_automation_body_with_routing_words_still_skips(self) -> None:
        from tempfile import TemporaryDirectory

        prompts = (
            "每日统计正文中“重新选模型”和“优先保证质量”的出现次数。",
            "每日检查模型路由数据；不要重新选模型，按原计划生成日报。",
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            session_id = "daily-route-words"
            transcript_path = write_transcript(codex_home, session_id)
            write_automation_config(
                codex_home,
                "daily-route-words-cron",
                kind="cron",
                prompt=prompts[0],
                name="路由词日报",
                model="gpt-5.6-sol",
                reasoning_effort="low",
                cwds=["/tmp/example-project"],
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                initial = process_hook(
                    payload(
                        cron_envelope(
                            "daily-route-words-cron",
                            "路由词日报",
                            prompts[0],
                        ),
                        session_id=session_id,
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                followup = process_hook(
                    payload(
                        prompts[1],
                        session_id=session_id,
                        turn_id="turn-2",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )

            self.assert_scheduled_context(initial)
            self.assertIsNone(injected_context(followup))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [event["reason"] for event in events],
                ["scheduled_automation", "automation_thread_followup"],
            )

    def test_heartbeat_reuses_confirmed_target_thread_route_without_exemption(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            session_id = "heartbeat-confirmed-thread"
            transcript_path = write_transcript(
                codex_home,
                session_id,
                thread_source="manual",
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                first = process_hook(
                    payload(
                        "开始目标任务。",
                        session_id=session_id,
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                confirmed = process_hook(
                    payload(
                        "按推荐执行。",
                        session_id=session_id,
                        turn_id="turn-2",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                heartbeat = process_hook(
                    payload(
                        heartbeat_envelope(
                            "heartbeat-copy",
                            "执行每日只读扫描。",
                        ),
                        session_id=session_id,
                        turn_id="turn-3",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )

            self.assertIn('reason="new_task"', injected_context(first))
            self.assertIsNone(injected_context(confirmed))
            self.assertIsNone(injected_context(heartbeat))
            events = [
                json.loads(line)
                for line in (state_dir / "gate-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events[-1]["reason"], "route_already_set")
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"][session_id]
            self.assertFalse(session["automation_exempt"])
            self.assertEqual(session["route_prompt_count"], 1)
            self.assertTrue(session["route_selection_observed"])

    def test_heartbeat_keeps_existing_unconfirmed_card_pending(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            session_id = "heartbeat-pending-thread"
            transcript_path = write_transcript(
                codex_home,
                session_id,
                thread_source="manual",
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                first = process_hook(
                    payload(
                        "开始目标任务。",
                        session_id=session_id,
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )
                heartbeat = process_hook(
                    payload(
                        heartbeat_envelope(
                            "heartbeat-copy",
                            "执行每日只读扫描。",
                        ),
                        session_id=session_id,
                        turn_id="turn-2",
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )

            self.assertIn('reason="new_task"', injected_context(first))
            self.assertIsNone(injected_context(heartbeat))
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"][session_id]
            self.assertEqual(session["last_reason"], "route_prompt_pending")
            self.assertFalse(session["automation_exempt"])
            self.assertEqual(session["route_prompt_count"], 1)
            self.assertFalse(session["route_selection_observed"])

    def test_heartbeat_envelope_alone_cannot_create_exemption(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            session_id = "heartbeat-new-thread"
            transcript_path = write_transcript(
                codex_home,
                session_id,
                thread_source="manual",
            )
            instructions = "执行每日只读扫描。"
            write_automation_config(
                codex_home,
                "heartbeat-copy",
                kind="heartbeat",
                prompt=instructions,
                target_thread_id=session_id,
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                result = process_hook(
                    payload(
                        heartbeat_envelope("heartbeat-copy", instructions),
                        session_id=session_id,
                        transcript_path=str(transcript_path),
                    ),
                    state_dir=state_dir,
                )

            self.assertIn('reason="new_task"', injected_context(result))
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"][session_id]
            self.assertFalse(session["automation_exempt"])
            self.assertEqual(session["route_prompt_count"], 1)

    def test_cron_trigger_requires_authoritative_transcript_and_safe_matching_config(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            state_dir = root / "state"
            project_cwd = "/tmp/cron-project"
            instructions = "重新选一下模型。"
            name = "每日自动巡检"

            write_automation_config(
                codex_home,
                "valid-cron",
                kind="cron",
                prompt=instructions,
                name=name,
                model="gpt-5.6-sol",
                reasoning_effort="low",
                cwds=[project_cwd],
            )
            valid_transcript = write_transcript(
                codex_home,
                "valid-cron-thread",
                thread_source="automation",
            )
            manual_copy_transcript = write_transcript(
                codex_home,
                "manual-copy-thread",
                thread_source="manual",
            )

            write_automation_config(
                codex_home,
                "wrong-cwd-cron",
                kind="cron",
                prompt=instructions,
                name=name,
                model="gpt-5.6-sol",
                reasoning_effort="low",
                cwds=["/tmp/another-project"],
            )
            wrong_cwd_transcript = write_transcript(
                codex_home,
                "wrong-cwd-cron-thread",
                thread_source="automation",
            )

            write_automation_config(
                codex_home,
                "missing-model-cron",
                kind="cron",
                prompt=instructions,
                name=name,
                reasoning_effort="low",
                cwds=[project_cwd],
            )
            missing_model_transcript = write_transcript(
                codex_home,
                "missing-model-cron-thread",
                thread_source="automation",
            )
            writable_config = write_automation_config(
                codex_home,
                "writable-config-cron",
                kind="cron",
                prompt=instructions,
                name=name,
                model="gpt-5.6-sol",
                reasoning_effort="low",
                cwds=[project_cwd],
            )
            writable_config.chmod(0o666)
            writable_config_transcript = write_transcript(
                codex_home,
                "writable-config-cron-thread",
                thread_source="automation",
            )

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                valid = process_hook(
                    payload(
                        cron_envelope("valid-cron", name, instructions),
                        session_id="valid-cron-thread",
                        cwd=project_cwd,
                        transcript_path=str(valid_transcript),
                    ),
                    state_dir=state_dir,
                )
                manual_copy = process_hook(
                    payload(
                        cron_envelope("valid-cron", name, instructions),
                        session_id="manual-copy-thread",
                        cwd=project_cwd,
                        transcript_path=str(manual_copy_transcript),
                    ),
                    state_dir=state_dir,
                )
                wrong_cwd = process_hook(
                    payload(
                        cron_envelope("wrong-cwd-cron", name, instructions),
                        session_id="wrong-cwd-cron-thread",
                        cwd=project_cwd,
                        transcript_path=str(wrong_cwd_transcript),
                    ),
                    state_dir=state_dir,
                )
                missing_model = process_hook(
                    payload(
                        cron_envelope("missing-model-cron", name, instructions),
                        session_id="missing-model-cron-thread",
                        cwd=project_cwd,
                        transcript_path=str(missing_model_transcript),
                    ),
                    state_dir=state_dir,
                )
                writable_config_result = process_hook(
                    payload(
                        cron_envelope("writable-config-cron", name, instructions),
                        session_id="writable-config-cron-thread",
                        cwd=project_cwd,
                        transcript_path=str(writable_config_transcript),
                    ),
                    state_dir=state_dir,
                )

            self.assert_scheduled_context(valid)
            self.assertIn('reason="new_task"', injected_context(manual_copy))
            self.assertIsNone(injected_context(wrong_cwd))
            self.assertIsNone(injected_context(missing_model))
            self.assertIsNone(injected_context(writable_config_result))
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                state["sessions"]["valid-cron-thread"]["automation_exempt"]
            )
            self.assertEqual(
                state["sessions"]["valid-cron-thread"]["route_prompt_count"], 0
            )
            self.assertFalse(
                state["sessions"]["manual-copy-thread"]["automation_exempt"]
            )
            for session_id in (
                "wrong-cwd-cron-thread",
                "missing-model-cron-thread",
                "writable-config-cron-thread",
            ):
                session = state["sessions"][session_id]
                self.assertTrue(session["automation_exempt"])
                self.assertEqual(session["last_reason"], "automation_thread_followup")
                self.assertEqual(session["route_prompt_count"], 0)
            self.assertTrue(writable_config.stat().st_mode & stat.S_IWOTH)

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
            "为了测试，请重新生成模型路由数据。",
            "把业务模型换成 Terra。",
            "把推理模型换成 Spark。",
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
            "按推荐执行；优先节省额度。",
            "按推荐执行；还是优先节省额度？",
            "使用 GPT-5.6-Sol high 可以吗？",
            "按推荐执行，行吗",
            "使用 Sol high，能不能",
            "按推荐执行，好不好",
            "Sol high，Terra medium。",
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
            "使用 GPT-5.6-Sol 的 high 档。",
            "我选择 Sol ultra。",
            "还是按推荐执行吧。",
            "我还是按推荐执行吧。",
            "那还是按推荐执行吧。",
            "我还是用 Sol ultra 吧。",
            "按推荐执行。接下来可以继续吗？",
            "按推荐执行；继续执行测试。",
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
            "麻烦重新选择模型",
            "请帮我重新选择模型",
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

    def test_explicit_configuration_reroutes_then_can_confirm_replacement(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            process_hook(payload("开始一个新的项目。"), state_dir=state_dir)
            process_hook(
                payload("按推荐执行。", turn_id="turn-2"), state_dir=state_dir
            )
            rerouted = process_hook(
                payload("我选择 Sol ultra。", turn_id="turn-3"),
                state_dir=state_dir,
            )
            confirmed = process_hook(
                payload("我选择 Sol ultra。", turn_id="turn-4"),
                state_dir=state_dir,
            )

            self.assertIn('reason="user_requested"', injected_context(rerouted))
            self.assertIsNone(injected_context(confirmed))
            state = json.loads(
                (state_dir / "gate-state.json").read_text(encoding="utf-8")
            )
            session = state["sessions"]["session-1"]
            self.assertTrue(session["route_selection_observed"])
            self.assertEqual(session["route_prompt_count"], 2)
            self.assertEqual(session["last_reason"], "route_already_set")

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
