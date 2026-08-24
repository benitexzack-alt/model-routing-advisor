#!/usr/bin/env python3
"""Tests for the reversible global model-routing gate installer."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import manage_global_gate as gate_module  # noqa: E402
from manage_global_gate import (  # noqa: E402
    AGENTS_BEGIN,
    AGENTS_END,
    GateError,
    GlobalGateManager,
    Paths,
    ensure_hook_trusted,
)


class FakeRpc:
    def __init__(
        self,
        listings: List[Dict],
        hook_state: Optional[Dict] = None,
        config_path: Optional[Path] = None,
        fail_config_read_calls: Optional[List[int]] = None,
    ) -> None:
        self.listings = list(listings)
        self.last_listing = None
        self.calls = []  # type: List[tuple]
        self.hook_state = dict(hook_state or {})
        self.config_path = config_path or Path("/tmp/codex-home/config.toml")
        self.version = 1
        self.config_read_count = 0
        self.fail_config_read_calls = set(fail_config_read_calls or [])

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "hooks/list":
            if not self.listings:
                if self.last_listing is None:
                    raise AssertionError("没有为 hooks/list 准备响应")
                return self.last_listing
            self.last_listing = self.listings.pop(0)
            return self.last_listing
        if method == "config/batchWrite":
            expected = params.get("expectedVersion")
            if expected is not None and expected != "version-%d" % self.version:
                raise AssertionError("expectedVersion 不匹配")
            for edit in params.get("edits", []):
                prefix = "hooks.state."
                key_path = edit.get("keyPath", "")
                if not key_path.startswith(prefix):
                    raise AssertionError("测试仅支持精确 hooks.state 项写入")
                key = json.loads(key_path[len(prefix) :])
                if edit.get("value") is None:
                    self.hook_state.pop(key, None)
                else:
                    self.hook_state[key] = edit["value"]
            self.version += 1
            return {
                "filePath": params.get("filePath"),
                "status": "ok",
                "version": "version-%d" % self.version,
            }
        if method == "config/read":
            self.config_read_count += 1
            if self.config_read_count in self.fail_config_read_calls:
                raise GateError("simulated config/read failure")
            return {
                "config": {},
                "origins": {},
                "layers": [
                    {
                        "name": {
                            "type": "user",
                            "file": str(self.config_path),
                            "profile": None,
                        },
                        "version": "version-%d" % self.version,
                        "config": {"hooks": {"state": dict(self.hook_state)}},
                    }
                ],
            }
        raise AssertionError(f"未预期的 RPC 方法：{method}")


def hook_listing(
    *,
    hooks_path: Path,
    command: str,
    trust_status: str,
    current_hash: str = "sha256:gate-hook-hash",
    enabled: bool = True,
    extra_hooks: Optional[List[Dict]] = None,
    hook_key: Optional[str] = None,
) -> dict:
    target = {
        "key": hook_key or f"{hooks_path}:user_prompt_submit:0:0",
        "eventName": "userPromptSubmit",
        "handlerType": "command",
        "command": command,
        "sourcePath": str(hooks_path),
        "source": "user",
        "displayOrder": 3,
        "enabled": enabled,
        "isManaged": False,
        "currentHash": current_hash,
        "trustStatus": trust_status,
        "timeoutSec": 5,
    }
    return {
        "data": [
            {
                "cwd": "/tmp/project",
                "hooks": [*(extra_hooks or []), target],
                "warnings": [],
                "errors": [],
            }
        ]
    }


class GlobalGateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.codex_home = self.root / "codex-home"
        self.source = (
            self.repo / "skills" / "model-routing-advisor" / "scripts" / "global_gate.py"
        )
        self.source.parent.mkdir(parents=True)
        self.source.write_text("#!/usr/bin/env python3\nprint('{}')\n", encoding="utf-8")
        self.source.chmod(0o755)
        self.codex_home.mkdir()
        self.paths = Paths.from_values(
            repo_root=self.repo,
            codex_home=self.codex_home,
            source_hook=self.source,
        )
        self.original_agents = "# 用户原有规则\n\n- 保留这一行。\n"
        self.paths.agents.write_text(self.original_agents, encoding="utf-8")
        self.original_hooks = {
            "description": "用户已有 Hook",
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "TodoWrite",
                        "hooks": [{"type": "command", "command": "post.sh"}],
                    }
                ],
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "start.sh"}]}
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "stop.sh"}]}],
            },
        }
        self.paths.hooks.write_text(
            json.dumps(self.original_hooks, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def manager_with_trusted_rpc(self) -> GlobalGateManager:
        listing = hook_listing(
            hooks_path=self.paths.hooks,
            command=self.paths.hook_command,
            trust_status="trusted",
        )
        return GlobalGateManager(self.paths, rpc_factory=lambda: FakeRpc([listing]))

    def test_install_is_idempotent_and_preserves_existing_content(self) -> None:
        manager = self.manager_with_trusted_rpc()
        first = manager.install(trust=False)

        agents = self.paths.agents.read_text(encoding="utf-8")
        self.assertIn("# 用户原有规则", agents)
        self.assertIn("- 保留这一行。", agents)
        self.assertLess(agents.index("# 用户原有规则"), agents.index("- 保留这一行。"))
        self.assertEqual(agents.count(AGENTS_BEGIN), 1)
        self.assertEqual(agents.count(AGENTS_END), 1)

        hooks = json.loads(self.paths.hooks.read_text(encoding="utf-8"))
        for event in ("PostToolUse", "SessionStart", "Stop"):
            self.assertEqual(hooks["hooks"][event], self.original_hooks["hooks"][event])
        self.assertEqual(len(hooks["hooks"]["UserPromptSubmit"]), 1)
        self.assertTrue(self.paths.target_hook.exists())
        self.assertTrue(os.access(self.paths.target_hook, os.X_OK))
        self.assertEqual(self.paths.target_hook.read_bytes(), self.source.read_bytes())
        self.assertTrue(first.changed)
        self.assertIsNotNone(first.backup_id)

        before = {
            path: path.read_bytes()
            for path in (
                self.paths.agents,
                self.paths.hooks,
                self.paths.target_hook,
                self.paths.state,
            )
        }
        backup_count = len(list(self.paths.backup_root.glob("*/snapshot.json")))
        second = self.manager_with_trusted_rpc().install(trust=False)
        self.assertFalse(second.changed)
        self.assertIsNone(second.backup_id)
        self.assertEqual(
            backup_count, len(list(self.paths.backup_root.glob("*/snapshot.json")))
        )
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_agents_block_requires_one_sticky_route_per_codex_task(self) -> None:
        block = self.paths.agents_block

        for required in (
            "可预见完整生命周期中的最高复杂度和最高风险下限",
            "只生成一张模型路由卡并等待用户明确确认",
            "归档恢复",
            "不自动重问，也不再生成路由卡",
            "同一 Codex 对话或任务只询问一次",
            "新建独立对话或任务时再询问一次",
            "用户明确要求改选或重新路由",
            "已选模型或档位经当前运行时核验不可用",
            "Codex 宿主明确标识为 cron 的自动化",
            "模型与推理档位已经在创建或更新时固定",
            "运行时才不生成模型路由卡，也不等待模型路由确认",
            "Codex 宿主明确标识为 heartbeat 的自动化",
            "只有目标 Codex 任务已经明确确认模型与推理档位时",
            "目标任务尚未确认时，必须保留并指向该任务唯一一张既有模型路由卡",
            "不得新建第二张，也不得绕过等待",
            "普通人工发起的新对话或新任务仍按上述规则先询问一次",
            "只避免重复的运行时模型路由",
            "不豁免任何权限、安全、隐私、内容、发布、付款、部署、删除",
            "不得把 heartbeat 视为无条件豁免",
            "不得仅凭提示词自称定时任务来绕过模型路由",
            "不得生成第二张路由卡",
            "已有卡片不可定位",
            "不替代任何安全、权限、内容、发布、付款、部署或外部行动门禁",
        ):
            self.assertIn(required, block)
        self.assertNotIn("现有任务发生阶段、范围、风险", block)
        self.assertNotIn("进入执行、部署或公开交付时重新路由", block)
        self.assertNotIn("cron/heartbeat、且", block)

        v11_block = self.paths.legacy_agents_blocks[-1]
        self.assertIn("同一 Codex 对话或任务只询问一次", v11_block)
        self.assertNotIn("自动化例外", v11_block)
        self.assertNotIn("cron/heartbeat", v11_block)

    def test_install_and_check_apply_distinct_cron_and_heartbeat_rules(self) -> None:
        installed = self.manager_with_trusted_rpc().install(trust=False)

        self.assertTrue(installed.changed)
        agents = self.paths.agents.read_text(encoding="utf-8")
        self.assertEqual(agents.count(self.paths.agents_block), 1)
        self.assertIn("Codex 宿主明确标识为 cron 的自动化", agents)
        self.assertIn("模型与推理档位已经在创建或更新时固定", agents)
        self.assertIn("Codex 宿主明确标识为 heartbeat 的自动化", agents)
        self.assertIn(
            "目标任务尚未确认时，必须保留并指向该任务唯一一张既有模型路由卡",
            agents,
        )
        self.assertIn("普通人工发起的新对话或新任务仍按上述规则先询问一次", agents)
        self.assertIn("不得把 heartbeat 视为无条件豁免", agents)

        report = self.manager_with_trusted_rpc().check(require_trust=True)
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(report.issues, [])

    def test_dry_run_writes_nothing(self) -> None:
        before_agents = self.paths.agents.read_bytes()
        before_hooks = self.paths.hooks.read_bytes()

        result = GlobalGateManager(self.paths).install(dry_run=True, trust=True)

        self.assertTrue(result.changed)
        self.assertTrue(result.dry_run)
        self.assertEqual(self.paths.agents.read_bytes(), before_agents)
        self.assertEqual(self.paths.hooks.read_bytes(), before_hooks)
        self.assertFalse(self.paths.target_hook.exists())
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.backup_root.exists())

    def test_install_rejects_target_hash_drift_without_writing(self) -> None:
        self.paths.target_hook.parent.mkdir(parents=True)
        self.paths.target_hook.write_text("changed by user\n", encoding="utf-8")
        before = self.paths.target_hook.read_bytes()

        with self.assertRaisesRegex(GateError, "哈希"):
            GlobalGateManager(self.paths).install()

        self.assertEqual(self.paths.target_hook.read_bytes(), before)
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.backup_root.exists())

    def test_install_rejects_existing_non_executable_target(self) -> None:
        self.paths.target_hook.parent.mkdir(parents=True)
        self.paths.target_hook.write_bytes(self.source.read_bytes())
        self.paths.target_hook.chmod(0o600)

        with self.assertRaisesRegex(GateError, "不可执行"):
            GlobalGateManager(self.paths).install()

        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.backup_root.exists())

    def test_install_rejects_source_not_parseable_by_system_python_39(self) -> None:
        self.source.write_text(
            "match 1:\n    case 1:\n        pass\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(GateError, "系统 Python 3.9"):
            GlobalGateManager(self.paths).install()

        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.backup_root.exists())

    def test_install_rejects_malformed_or_modified_managed_block(self) -> None:
        self.paths.agents.write_text(
            f"原文\n{AGENTS_BEGIN}\n被修改\n{AGENTS_END}\n", encoding="utf-8"
        )
        before = self.paths.agents.read_bytes()

        with self.assertRaisesRegex(GateError, "标记块"):
            GlobalGateManager(self.paths).install()

        self.assertEqual(self.paths.agents.read_bytes(), before)
        self.assertFalse(self.paths.backup_root.exists())

    def test_check_detects_script_config_mode_and_trust_drift(self) -> None:
        manager = self.manager_with_trusted_rpc()
        manager.install(trust=False)

        self.paths.target_hook.chmod(0o600)
        hooks = json.loads(self.paths.hooks.read_text(encoding="utf-8"))
        hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] = 99
        self.paths.hooks.write_text(json.dumps(hooks), encoding="utf-8")
        rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="modified",
                    enabled=False,
                )
            ]
        )

        report = GlobalGateManager(
            self.paths, rpc_factory=lambda: rpc
        ).check(require_trust=True)

        self.assertFalse(report.ok)
        joined = "\n".join(report.issues)
        self.assertIn("不可执行", joined)
        self.assertIn("Hook 配置", joined)
        self.assertIn("modified", joined)
        self.assertIn("enabled=false", joined)

    def test_uninstall_removes_only_managed_artifacts(self) -> None:
        manager = self.manager_with_trusted_rpc()
        manager.install(trust=False)

        uninstall = self.manager_with_trusted_rpc().uninstall()

        self.assertTrue(uninstall.changed)
        self.assertEqual(
            self.paths.agents.read_text(encoding="utf-8"), self.original_agents
        )
        hooks = json.loads(self.paths.hooks.read_text(encoding="utf-8"))
        self.assertNotIn("UserPromptSubmit", hooks["hooks"])
        for event in ("PostToolUse", "SessionStart", "Stop"):
            self.assertEqual(hooks["hooks"][event], self.original_hooks["hooks"][event])
        self.assertFalse(self.paths.target_hook.exists())
        self.assertFalse(self.paths.state.exists())
        self.assertIsNotNone(uninstall.backup_id)

    def test_trusted_install_uninstall_restores_only_target_trust_entry(self) -> None:
        target_key = f"{self.paths.hooks}:user_prompt_submit:0:0"
        unrelated_key = f"{self.paths.hooks}:stop:0:0"
        rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="untrusted",
                ),
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="trusted",
                ),
            ],
            hook_state={unrelated_key: {"trusted_hash": "sha256:unrelated"}},
            config_path=self.paths.config,
        )
        manager = GlobalGateManager(self.paths, rpc_factory=lambda: rpc)

        manager.install(trust=True)
        self.assertIn(target_key, rpc.hook_state)
        self.assertEqual(
            rpc.hook_state[unrelated_key], {"trusted_hash": "sha256:unrelated"}
        )
        state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        self.assertTrue(state["trust_managed"])
        self.assertEqual(
            state["trust_restore"],
            {"key": target_key, "exists": False, "value": None},
        )

        manager.uninstall()
        self.assertNotIn(target_key, rpc.hook_state)
        self.assertEqual(
            rpc.hook_state,
            {unrelated_key: {"trusted_hash": "sha256:unrelated"}},
        )

    def test_failure_after_trust_write_restores_trust_and_files(self) -> None:
        target_key = f"{self.paths.hooks}:user_prompt_submit:0:0"
        unrelated_key = f"{self.paths.hooks}:stop:0:0"
        rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="untrusted",
                ),
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="trusted",
                ),
            ],
            hook_state={unrelated_key: {"trusted_hash": "sha256:unrelated"}},
            config_path=self.paths.config,
        )
        real_atomic_write = gate_module.atomic_write

        def fail_final_state(path: Path, data: bytes, mode: int) -> None:
            if path == self.paths.state and b'"trust_status": "trusted"' in data:
                raise OSError("simulated final state failure")
            real_atomic_write(path, data, mode)

        with mock.patch.object(gate_module, "atomic_write", side_effect=fail_final_state):
            with self.assertRaisesRegex(OSError, "simulated"):
                GlobalGateManager(self.paths, rpc_factory=lambda: rpc).install(
                    trust=True
                )

        self.assertNotIn(target_key, rpc.hook_state)
        self.assertEqual(
            rpc.hook_state,
            {unrelated_key: {"trusted_hash": "sha256:unrelated"}},
        )
        self.assertEqual(self.paths.agents.read_text(encoding="utf-8"), self.original_agents)
        self.assertEqual(json.loads(self.paths.hooks.read_text()), self.original_hooks)
        self.assertFalse(self.paths.target_hook.exists())
        self.assertFalse(self.paths.state.exists())

    def test_uninstall_postwrite_read_failure_rolls_back_trust_transaction(self) -> None:
        target_key = f"{self.paths.hooks}:user_prompt_submit:0:0"
        unrelated_key = f"{self.paths.hooks}:stop:0:0"
        rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="untrusted",
                ),
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="trusted",
                ),
            ],
            hook_state={unrelated_key: {"enabled": False}},
            config_path=self.paths.config,
            fail_config_read_calls=[4],
        )
        manager = GlobalGateManager(self.paths, rpc_factory=lambda: rpc)
        manager.install(trust=True)
        installed_agents = self.paths.agents.read_bytes()
        installed_hooks = self.paths.hooks.read_bytes()

        with self.assertRaisesRegex(GateError, "simulated config/read failure"):
            manager.uninstall()

        self.assertEqual(
            rpc.hook_state[target_key],
            {"trusted_hash": "sha256:gate-hook-hash"},
        )
        self.assertEqual(rpc.hook_state[unrelated_key], {"enabled": False})
        self.assertEqual(self.paths.agents.read_bytes(), installed_agents)
        self.assertEqual(self.paths.hooks.read_bytes(), installed_hooks)
        self.assertTrue(self.paths.target_hook.exists())
        self.assertTrue(self.paths.state.exists())

    def test_rollback_restores_exact_preinstall_snapshot(self) -> None:
        original_agents_bytes = self.paths.agents.read_bytes()
        original_hooks_bytes = self.paths.hooks.read_bytes()
        installed = self.manager_with_trusted_rpc().install(trust=False)
        self.assertIsNotNone(installed.backup_id)

        result = GlobalGateManager(self.paths).rollback(installed.backup_id)

        self.assertTrue(result.changed)
        self.assertEqual(self.paths.agents.read_bytes(), original_agents_bytes)
        self.assertEqual(self.paths.hooks.read_bytes(), original_hooks_bytes)
        self.assertFalse(self.paths.target_hook.exists())
        self.assertFalse(self.paths.state.exists())

    def test_rollback_restores_target_trust_without_touching_unrelated_state(self) -> None:
        target_key = f"{self.paths.hooks}:user_prompt_submit:0:0"
        unrelated_key = f"{self.paths.hooks}:stop:0:0"
        rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="untrusted",
                ),
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="trusted",
                ),
            ],
            hook_state={unrelated_key: {"enabled": False}},
            config_path=self.paths.config,
        )
        manager = GlobalGateManager(self.paths, rpc_factory=lambda: rpc)
        installed = manager.install(trust=True)

        manager.rollback(installed.backup_id)

        self.assertNotIn(target_key, rpc.hook_state)
        self.assertEqual(rpc.hook_state, {unrelated_key: {"enabled": False}})
        self.assertEqual(self.paths.agents.read_text(encoding="utf-8"), self.original_agents)

    def test_rollback_migrates_legacy_install_backup_without_trust_metadata(self) -> None:
        target_key = f"{self.paths.hooks}:user_prompt_submit:0:0"
        unrelated_key = f"{self.paths.hooks}:stop:0:0"
        rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="untrusted",
                ),
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="trusted",
                ),
            ],
            hook_state={unrelated_key: {"enabled": False}},
            config_path=self.paths.config,
        )
        manager = GlobalGateManager(self.paths, rpc_factory=lambda: rpc)
        installed = manager.install(trust=True)
        snapshot_path = self.paths.backup_root / installed.backup_id / "snapshot.json"
        legacy_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        legacy_snapshot.pop("hook_trust")
        snapshot_path.write_text(json.dumps(legacy_snapshot), encoding="utf-8")

        manager.rollback(installed.backup_id)

        self.assertNotIn(target_key, rpc.hook_state)
        self.assertEqual(rpc.hook_state, {unrelated_key: {"enabled": False}})
        self.assertFalse(self.paths.target_hook.exists())
        self.assertFalse(self.paths.state.exists())

    def test_install_safely_upgrades_owned_hook_from_recorded_old_hash(self) -> None:
        manager = self.manager_with_trusted_rpc()
        installed = manager.install(trust=False)
        old_target = self.paths.target_hook.read_bytes()
        old_state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        self.source.write_text("#!/usr/bin/env python3\nprint('{\"v\": 2}')\n", encoding="utf-8")

        upgraded = self.manager_with_trusted_rpc().install(trust=False)

        self.assertTrue(upgraded.changed)
        self.assertNotEqual(self.paths.target_hook.read_bytes(), old_target)
        self.assertEqual(self.paths.target_hook.read_bytes(), self.source.read_bytes())
        new_state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        self.assertEqual(
            new_state["upgraded_from_target_sha256"], old_state["target_sha256"]
        )
        self.assertNotEqual(new_state["target_sha256"], old_state["target_sha256"])
        self.assertIsNotNone(installed.backup_id)

    def test_install_safely_upgrades_legacy_agents_block_and_old_hook(self) -> None:
        self.manager_with_trusted_rpc().install(trust=False)
        legacy_block = self.paths.legacy_agents_blocks[0]
        legacy_block_hash = gate_module.sha256_bytes(legacy_block.encode("utf-8"))
        agents = self.paths.agents.read_text(encoding="utf-8")
        self.paths.agents.write_text(
            agents.replace(self.paths.agents_block, legacy_block),
            encoding="utf-8",
        )
        legacy_state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        legacy_state["agents_block_sha256"] = legacy_block_hash
        self.paths.state.write_text(json.dumps(legacy_state), encoding="utf-8")
        legacy_agents_bytes = self.paths.agents.read_bytes()
        legacy_state_bytes = self.paths.state.read_bytes()
        old_target = self.paths.target_hook.read_bytes()
        old_hooks_bytes = self.paths.hooks.read_bytes()
        self.source.write_text(
            "#!/usr/bin/env python3\nprint('{\"v\": 2}')\n", encoding="utf-8"
        )

        upgraded = self.manager_with_trusted_rpc().install(trust=False)

        self.assertTrue(upgraded.changed)
        self.assertIsNotNone(upgraded.backup_id)
        upgraded_agents = self.paths.agents.read_text(encoding="utf-8")
        self.assertEqual(upgraded_agents.count(self.paths.agents_block), 1)
        self.assertNotIn(legacy_block, upgraded_agents)
        self.assertIn("# 用户原有规则", upgraded_agents)
        self.assertIn("- 保留这一行。", upgraded_agents)
        self.assertEqual(self.paths.hooks.read_bytes(), old_hooks_bytes)
        self.assertNotEqual(self.paths.target_hook.read_bytes(), old_target)
        self.assertEqual(self.paths.target_hook.read_bytes(), self.source.read_bytes())
        upgraded_state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        self.assertEqual(
            upgraded_state["agents_block_sha256"],
            gate_module.sha256_bytes(self.paths.agents_block.encode("utf-8")),
        )
        self.assertEqual(
            upgraded_state["upgraded_from_agents_block_sha256"], legacy_block_hash
        )
        self.assertEqual(
            upgraded_state["upgraded_from_target_sha256"],
            legacy_state["target_sha256"],
        )

        GlobalGateManager(self.paths).rollback(upgraded.backup_id)
        self.assertEqual(self.paths.agents.read_bytes(), legacy_agents_bytes)
        self.assertEqual(self.paths.hooks.read_bytes(), old_hooks_bytes)
        self.assertEqual(self.paths.target_hook.read_bytes(), old_target)
        self.assertEqual(self.paths.state.read_bytes(), legacy_state_bytes)

    def test_install_upgrades_exact_v11_agents_block_and_rollback_restores_it(self) -> None:
        self.manager_with_trusted_rpc().install(trust=False)
        v11_block = self.paths.legacy_agents_blocks[-1]
        self.assertIn("同一 Codex 对话或任务只询问一次", v11_block)
        self.assertNotIn("cron/heartbeat", v11_block)
        v11_hash = gate_module.sha256_bytes(v11_block.encode("utf-8"))

        agents = self.paths.agents.read_text(encoding="utf-8")
        self.paths.agents.write_text(
            agents.replace(self.paths.agents_block, v11_block),
            encoding="utf-8",
        )
        state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        state["agents_block_sha256"] = v11_hash
        self.paths.state.write_text(json.dumps(state), encoding="utf-8")
        v11_agents_bytes = self.paths.agents.read_bytes()
        v11_state_bytes = self.paths.state.read_bytes()

        upgraded = self.manager_with_trusted_rpc().install(trust=False)

        self.assertTrue(upgraded.changed)
        self.assertIsNotNone(upgraded.backup_id)
        upgraded_agents = self.paths.agents.read_text(encoding="utf-8")
        self.assertEqual(upgraded_agents.count(self.paths.agents_block), 1)
        self.assertNotIn(v11_block, upgraded_agents)
        self.assertIn("Codex 宿主明确标识为 cron 的自动化", upgraded_agents)
        self.assertIn("Codex 宿主明确标识为 heartbeat 的自动化", upgraded_agents)
        upgraded_state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        self.assertEqual(
            upgraded_state["upgraded_from_agents_block_sha256"], v11_hash
        )

        report = self.manager_with_trusted_rpc().check(require_trust=True)
        self.assertTrue(report.ok, report.issues)

        GlobalGateManager(self.paths).rollback(upgraded.backup_id)
        self.assertEqual(self.paths.agents.read_bytes(), v11_agents_bytes)
        self.assertEqual(self.paths.state.read_bytes(), v11_state_bytes)

    def test_uninstall_accepts_exact_legacy_agents_block_and_state(self) -> None:
        self.manager_with_trusted_rpc().install(trust=False)
        legacy_block = self.paths.legacy_agents_blocks[0]
        agents = self.paths.agents.read_text(encoding="utf-8")
        self.paths.agents.write_text(
            agents.replace(self.paths.agents_block, legacy_block),
            encoding="utf-8",
        )
        state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        state["agents_block_sha256"] = gate_module.sha256_bytes(
            legacy_block.encode("utf-8")
        )
        self.paths.state.write_text(json.dumps(state), encoding="utf-8")

        result = self.manager_with_trusted_rpc().uninstall()

        self.assertTrue(result.changed)
        self.assertEqual(self.paths.agents.read_text(encoding="utf-8"), self.original_agents)
        self.assertFalse(self.paths.target_hook.exists())
        self.assertFalse(self.paths.state.exists())

    def test_trusted_upgrade_migrates_legacy_restore_state_without_stale_hash(self) -> None:
        target_key = f"{self.paths.hooks}:user_prompt_submit:0:0"
        old_hash = "sha256:gate-hook-hash"
        initial_rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="untrusted",
                    current_hash=old_hash,
                ),
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="trusted",
                    current_hash=old_hash,
                ),
            ],
            config_path=self.paths.config,
        )
        GlobalGateManager(self.paths, rpc_factory=lambda: initial_rpc).install(
            trust=True
        )
        legacy_state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        for key in ("hook_key", "trust_managed", "trust_restore"):
            legacy_state.pop(key, None)
        self.paths.state.write_text(json.dumps(legacy_state), encoding="utf-8")
        self.source.write_text("#!/usr/bin/env python3\nprint('v2')\n", encoding="utf-8")
        new_hash = "sha256:new-gate-hook-hash"
        upgrade_rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="modified",
                    current_hash=new_hash,
                ),
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="modified",
                    current_hash=new_hash,
                ),
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="trusted",
                    current_hash=new_hash,
                ),
            ],
            hook_state={target_key: {"trusted_hash": old_hash}},
            config_path=self.paths.config,
        )

        GlobalGateManager(self.paths, rpc_factory=lambda: upgrade_rpc).install(
            trust=True
        )

        state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        self.assertTrue(state["trust_managed"])
        self.assertTrue(state["trust_restore_inferred_legacy"])
        self.assertEqual(
            state["trust_restore"],
            {"key": target_key, "exists": False, "value": None},
        )
        self.assertEqual(
            upgrade_rpc.hook_state[target_key], {"trusted_hash": new_hash}
        )

    def test_install_rejects_upgrade_when_installed_target_drifted(self) -> None:
        self.manager_with_trusted_rpc().install(trust=False)
        self.paths.target_hook.write_text("user changed target\n", encoding="utf-8")
        self.paths.target_hook.chmod(0o700)
        self.source.write_text("#!/usr/bin/env python3\nprint('v2')\n", encoding="utf-8")

        with self.assertRaisesRegex(GateError, "安装状态哈希"):
            self.manager_with_trusted_rpc().install(trust=False)

    def test_hook_key_position_drift_fails_closed_everywhere(self) -> None:
        self.manager_with_trusted_rpc().install(trust=False)
        before = {
            path: path.read_bytes()
            for path in (
                self.paths.agents,
                self.paths.hooks,
                self.paths.target_hook,
                self.paths.state,
            )
        }
        drifted_key = f"{self.paths.hooks}:user_prompt_submit:1:0"
        drifted_rpc = FakeRpc(
            [
                hook_listing(
                    hooks_path=self.paths.hooks,
                    command=self.paths.hook_command,
                    trust_status="untrusted",
                    hook_key=drifted_key,
                )
            ],
            config_path=self.paths.config,
        )
        manager = GlobalGateManager(self.paths, rpc_factory=lambda: drifted_rpc)

        with self.assertRaisesRegex(GateError, "Hook key 漂移"):
            manager.install(trust=True)
        report = manager.check(require_trust=True)
        self.assertFalse(report.ok)
        self.assertIn("Hook key 漂移", "\n".join(report.issues))
        with self.assertRaisesRegex(GateError, "Hook key 漂移"):
            manager.uninstall()

        self.assertFalse(
            any(method == "config/batchWrite" for method, _ in drifted_rpc.calls)
        )
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_uninstall_uses_installed_hash_when_repository_source_changed(self) -> None:
        self.manager_with_trusted_rpc().install(trust=False)
        self.source.write_text("#!/usr/bin/env python3\nprint('future v2')\n", encoding="utf-8")

        result = self.manager_with_trusted_rpc().uninstall()

        self.assertTrue(result.changed)
        self.assertFalse(self.paths.target_hook.exists())
        self.assertFalse(self.paths.state.exists())

    def test_rollback_dry_run_validates_snapshot_structure(self) -> None:
        installed = self.manager_with_trusted_rpc().install(trust=False)
        snapshot_path = self.paths.backup_root / installed.backup_id / "snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["files"].pop()
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        with self.assertRaisesRegex(GateError, "不完整"):
            GlobalGateManager(self.paths).rollback(
                installed.backup_id, dry_run=True
            )

    def test_rollback_rejects_backup_trust_key_from_another_hooks_file(self) -> None:
        installed = self.manager_with_trusted_rpc().install(trust=False)
        snapshot_path = self.paths.backup_root / installed.backup_id / "snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["hook_trust"] = {
            "key": "/tmp/other-hooks.json:user_prompt_submit:0:0",
            "exists": False,
            "value": None,
        }
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        with self.assertRaisesRegex(GateError, "不属于当前模型路由门"):
            GlobalGateManager(self.paths).rollback(installed.backup_id)

    def test_rollback_rejects_wrong_index_in_same_hooks_file(self) -> None:
        installed = self.manager_with_trusted_rpc().install(trust=False)
        snapshot_path = self.paths.backup_root / installed.backup_id / "snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["hook_trust"] = {
            "key": f"{self.paths.hooks}:user_prompt_submit:9:0",
            "exists": False,
            "value": None,
        }
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        with self.assertRaisesRegex(GateError, "Hook key 漂移"):
            GlobalGateManager(self.paths).rollback(installed.backup_id)

    def test_install_requires_source_and_check_reports_missing_source(self) -> None:
        self.source.unlink()
        with self.assertRaisesRegex(GateError, "源码 Hook"):
            GlobalGateManager(self.paths).install()
        report = GlobalGateManager(self.paths).check(require_trust=False)
        self.assertFalse(report.ok)
        self.assertIn("源码 Hook", "\n".join(report.issues))

    def test_atomic_writes_leave_expected_permissions(self) -> None:
        self.manager_with_trusted_rpc().install(trust=False)
        self.assertEqual(stat.S_IMODE(self.paths.target_hook.stat().st_mode), 0o700)
        temporary_files = list(self.codex_home.rglob("*.tmp"))
        self.assertEqual(temporary_files, [])

    def test_failed_trust_restores_fresh_install_snapshot(self) -> None:
        untrusted = hook_listing(
            hooks_path=self.paths.hooks,
            command=self.paths.hook_command,
            trust_status="untrusted",
        )
        still_untrusted = hook_listing(
            hooks_path=self.paths.hooks,
            command=self.paths.hook_command,
            trust_status="untrusted",
        )
        manager = GlobalGateManager(
            self.paths,
            rpc_factory=lambda: FakeRpc([untrusted, still_untrusted]),
        )

        with self.assertRaisesRegex(GateError, "不是 trusted"):
            manager.install(trust=True)

        self.assertEqual(
            self.paths.agents.read_text(encoding="utf-8"), self.original_agents
        )
        self.assertEqual(
            json.loads(self.paths.hooks.read_text(encoding="utf-8")),
            self.original_hooks,
        )
        self.assertFalse(self.paths.target_hook.exists())
        self.assertFalse(self.paths.state.exists())


class HookTrustTests(unittest.TestCase):
    def test_trust_writes_only_target_current_hash_then_rechecks(self) -> None:
        hooks_path = Path("/tmp/codex-home/hooks.json")
        command = "'/usr/bin/python3' '/tmp/codex-home/hooks/model-routing-gate.py'"
        unrelated = {
            "key": "/tmp/codex-home/hooks.json:stop:0:0",
            "eventName": "stop",
            "handlerType": "command",
            "command": "stop.sh",
            "sourcePath": str(hooks_path),
            "source": "user",
            "displayOrder": 0,
            "enabled": True,
            "isManaged": False,
            "currentHash": "sha256:do-not-trust-this",
            "trustStatus": "untrusted",
            "timeoutSec": 5,
        }
        before = hook_listing(
            hooks_path=hooks_path,
            command=command,
            trust_status="untrusted",
            extra_hooks=[unrelated],
        )
        after = hook_listing(
            hooks_path=hooks_path,
            command=command,
            trust_status="trusted",
            extra_hooks=[unrelated],
        )
        rpc = FakeRpc([before, after])

        result = ensure_hook_trusted(
            rpc,
            cwd=Path("/tmp/project"),
            hooks_path=hooks_path,
            config_path=Path("/tmp/codex-home/config.toml"),
            command=command,
        )

        self.assertEqual(result.metadata["trustStatus"], "trusted")
        self.assertTrue(result.changed)
        self.assertIsNotNone(result.original)
        methods = [method for method, _ in rpc.calls]
        self.assertEqual(
            methods,
            ["hooks/list", "config/read", "config/batchWrite", "hooks/list"],
        )
        params = rpc.calls[2][1]
        self.assertEqual(params["filePath"], "/tmp/codex-home/config.toml")
        self.assertTrue(params["reloadUserConfig"])
        self.assertEqual(len(params["edits"]), 1)
        edit = params["edits"][0]
        self.assertEqual(
            edit["keyPath"],
            'hooks.state."/tmp/codex-home/hooks.json:user_prompt_submit:0:0"',
        )
        self.assertEqual(edit["mergeStrategy"], "replace")
        self.assertEqual(
            edit["value"],
            {"trusted_hash": "sha256:gate-hook-hash"},
        )
        self.assertNotIn("sha256:do-not-trust-this", json.dumps(edit))

    def test_trust_fails_if_post_write_status_is_not_trusted_and_enabled(self) -> None:
        hooks_path = Path("/tmp/codex-home/hooks.json")
        command = "'/usr/bin/python3' '/tmp/codex-home/hooks/model-routing-gate.py'"
        before = hook_listing(
            hooks_path=hooks_path,
            command=command,
            trust_status="untrusted",
        )
        after = hook_listing(
            hooks_path=hooks_path,
            command=command,
            trust_status="trusted",
            enabled=False,
        )
        with self.assertRaisesRegex(GateError, "enabled"):
            ensure_hook_trusted(
                FakeRpc([before, after]),
                cwd=Path("/tmp/project"),
                hooks_path=hooks_path,
                config_path=Path("/tmp/codex-home/config.toml"),
                command=command,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
