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


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from manage_global_gate import (  # noqa: E402
    AGENTS_BEGIN,
    AGENTS_END,
    GateError,
    GlobalGateManager,
    Paths,
    ensure_hook_trusted,
)


class FakeRpc:
    def __init__(self, listings: List[Dict]) -> None:
        self.listings = list(listings)
        self.calls = []  # type: List[tuple]

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "hooks/list":
            if not self.listings:
                raise AssertionError("没有为 hooks/list 准备响应")
            return self.listings.pop(0)
        if method == "config/batchWrite":
            return {
                "filePath": params.get("filePath"),
                "status": "ok",
                "version": "test-version",
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
) -> dict:
    target = {
        "key": f"{hooks_path}:user_prompt_submit:1:0",
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

        metadata = ensure_hook_trusted(
            rpc,
            cwd=Path("/tmp/project"),
            hooks_path=hooks_path,
            config_path=Path("/tmp/codex-home/config.toml"),
            command=command,
        )

        self.assertEqual(metadata["trustStatus"], "trusted")
        methods = [method for method, _ in rpc.calls]
        self.assertEqual(methods, ["hooks/list", "config/batchWrite", "hooks/list"])
        params = rpc.calls[1][1]
        self.assertEqual(params["filePath"], "/tmp/codex-home/config.toml")
        self.assertTrue(params["reloadUserConfig"])
        self.assertEqual(len(params["edits"]), 1)
        edit = params["edits"][0]
        self.assertEqual(edit["keyPath"], "hooks.state")
        self.assertEqual(edit["mergeStrategy"], "upsert")
        self.assertEqual(
            edit["value"],
            {
                f"{hooks_path}:user_prompt_submit:1:0": {
                    "trusted_hash": "sha256:gate-hook-hash"
                }
            },
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
