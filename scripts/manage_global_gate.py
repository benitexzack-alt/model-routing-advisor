#!/usr/bin/env python3
"""Install, inspect, remove, and roll back the global model-routing gate.

The manager owns one marked block in ``AGENTS.md``, one command hook inside
``hooks.json``, one installed hook script, and one state manifest.  Existing
instructions and unrelated hooks are preserved.  ``config.toml`` is never
edited directly; hook trust is persisted through the Codex app-server API.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional


GATE_ID = "MRA_GLOBAL_GATE_V1"
SCHEMA_VERSION = 1
AGENTS_BEGIN = "<!-- MRA_GLOBAL_GATE_V1:BEGIN -->"
AGENTS_END = "<!-- MRA_GLOBAL_GATE_V1:END -->"
STATE_FILENAME = "model-routing-gate-state.json"
BACKUP_DIRECTORY = "model-routing-gate"
DEFAULT_HOOK_TIMEOUT = 5
DEFAULT_CONTEXT_LIMIT = 2500


class GateError(RuntimeError):
    """Raised when a safe gate mutation cannot be completed."""


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _quoted(value: str) -> str:
    """Return a POSIX single-quoted argument without relying on PATH."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    codex_home: Path
    source_hook: Path
    target_hook: Path
    agents: Path
    hooks: Path
    config: Path
    state: Path
    backup_root: Path

    @classmethod
    def from_values(
        cls,
        repo_root: Optional[Path] = None,
        codex_home: Optional[Path] = None,
        source_hook: Optional[Path] = None,
        target_hook: Optional[Path] = None,
    ) -> "Paths":
        default_repo = Path(__file__).resolve().parents[1]
        repo = Path(repo_root or default_repo).expanduser().resolve()
        home_value = codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        home = Path(home_value).expanduser().resolve()
        source = Path(
            source_hook
            or repo
            / "skills"
            / "model-routing-advisor"
            / "scripts"
            / "global_gate.py"
        ).expanduser().resolve()
        target = Path(
            target_hook or home / "hooks" / "model-routing-gate.py"
        ).expanduser().resolve()
        return cls(
            repo_root=repo,
            codex_home=home,
            source_hook=source,
            target_hook=target,
            agents=home / "AGENTS.md",
            hooks=home / "hooks.json",
            config=home / "config.toml",
            state=home / STATE_FILENAME,
            backup_root=home / "backups" / BACKUP_DIRECTORY,
        )

    @property
    def hook_command(self) -> str:
        return "%s %s" % (_quoted("/usr/bin/python3"), _quoted(str(self.target_hook)))

    @property
    def agents_block(self) -> str:
        skill_path = self.codex_home / "skills" / "model-routing-advisor" / "SKILL.md"
        return "\n".join(
            [
                AGENTS_BEGIN,
                "## 模型路由强制入口（MRA_GLOBAL_GATE_V1）",
                "",
                "不得依赖 Skill 自动匹配。每个新对话或新任务的首个非闲聊、实质请求，在任何实质性调查、规划、写文件、改代码、外部调用或现实行动前，必须完整读取：",
                "",
                "`%s`" % skill_path,
                "",
                "首次选型必须按该新对话或项目可预见完整生命周期中的最高复杂度和最高风险下限进行，确保固定路线能够覆盖后续阶段。按该 Skill 只生成一张模型路由卡并等待用户明确确认后，才可继续。确认前只允许完成路由分类所必需的只读核查，不得产生写入或外部副作用。",
                "",
                "用户明确确认后，该对话后续的阶段、范围、风险、动作权限、交付对象变化以及归档恢复，默认沿用已经确认的模型与推理档位，不自动重问，也不再生成路由卡。同一 Codex 对话或任务只询问一次；新建独立对话或任务时再询问一次。只有用户明确要求改选或重新路由，或者已选模型或档位经当前运行时核验不可用时，才重新路由。",
                "",
                "用户尚未确认时，后续消息不得开始实质工作，也不得生成第二张路由卡；应明确指向本对话已有卡片并继续等待选择。若已有卡片不可定位，必须报告门禁状态异常并停止实质执行，不得伪造确认或沿用。普通寒暄、简单解释和微小追问可免路由。",
                "",
                "模型路由确认只决定模型与推理档位，不替代任何安全、权限、内容、发布、付款、部署或外部行动门禁；其他门禁及单独授权要求始终有效。Skill 缺失、不可读或返回 `needs-refresh` 时必须明确报告并停止实质执行，禁止静默跳过。",
                AGENTS_END,
            ]
        )

    @property
    def legacy_agents_blocks(self) -> List[str]:
        """Exact manager-owned blocks accepted for one-way migration."""
        skill_path = self.codex_home / "skills" / "model-routing-advisor" / "SKILL.md"
        return [
            "\n".join(
                [
                    AGENTS_BEGIN,
                    "## 模型路由强制入口（MRA_GLOBAL_GATE_V1）",
                    "",
                    "不得依赖 Skill 自动匹配。每个新任务的首个非闲聊请求，以及现有任务发生阶段、范围、风险、动作权限或交付对象显著变化时，在任何实质性调查、规划、写文件、改代码、外部调用或现实行动前，必须完整读取：",
                    "",
                    "`%s`" % skill_path,
                    "",
                    "按该 Skill 生成模型路由卡并等待用户确认后，才可继续。确认前只允许完成路由分类所必需的只读核查，不得产生写入或外部副作用。",
                    "",
                    "同一阶段已有可定位的确认记录时沿用，不重复弹卡；普通寒暄、简单解释、微小追问可免路由。不得在找不到本任务已确认路由时声称“沿用”。Skill 缺失、不可读或返回 `needs-refresh` 时必须明确报告并停止实质执行，禁止静默跳过。",
                    "",
                    "重大项目的调查或规划本身也属于需要路由的阶段；方向确认后进入执行、部署或公开交付时重新路由。",
                    AGENTS_END,
                ]
            )
        ]

    @property
    def hook_group(self) -> Dict[str, Any]:
        return {
            "hooks": [
                {
                    "type": "command",
                    "command": self.hook_command,
                    "timeout": DEFAULT_HOOK_TIMEOUT,
                    "additionalContextLimit": DEFAULT_CONTEXT_LIMIT,
                }
            ]
        }


@dataclass(frozen=True)
class OperationResult:
    action: str
    changed: bool
    dry_run: bool = False
    backup_id: Optional[str] = None
    trust_status: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "changed": self.changed,
            "dry_run": self.dry_run,
            "backup_id": self.backup_id,
            "trust_status": self.trust_status,
        }


@dataclass(frozen=True)
class CheckReport:
    ok: bool
    issues: List[str]
    trust_status: Optional[str] = None
    current_hash: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "trust_status": self.trust_status,
            "current_hash": self.current_hash,
        }


@dataclass(frozen=True)
class HookTrustSnapshot:
    """Exact user-layer value for one hook trust key."""

    key: str
    exists: bool
    value: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "exists": self.exists,
            "value": copy.deepcopy(self.value) if self.exists else None,
        }


@dataclass(frozen=True)
class HookTrustResult:
    metadata: Dict[str, Any]
    changed: bool
    original: Optional[HookTrustSnapshot] = None


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise GateError("拒绝修改符号链接：%s" % path)


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write a file through a same-directory temporary and atomic replace."""
    _reject_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), mode)
        os.replace(str(temporary), str(path))
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _existing_mode(path: Path, default: int = 0o600) -> int:
    if not path.exists():
        return default
    _reject_symlink(path)
    if not path.is_file():
        raise GateError("目标不是普通文件：%s" % path)
    return stat.S_IMODE(path.stat().st_mode)


def _read_text_or_empty(path: Path) -> str:
    if not path.exists():
        return ""
    _reject_symlink(path)
    if not path.is_file():
        raise GateError("目标不是普通文件：%s" % path)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GateError("无法读取 UTF-8 文件 %s：%s" % (path, error))


def _managed_block_span(text: str) -> Optional[tuple]:
    begin_count = text.count(AGENTS_BEGIN)
    end_count = text.count(AGENTS_END)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise GateError("AGENTS.md 的模型路由标记块不完整或重复")
    start = text.index(AGENTS_BEGIN)
    end = text.index(AGENTS_END, start) + len(AGENTS_END)
    if end <= start:
        raise GateError("AGENTS.md 的模型路由标记块顺序无效")
    return start, end


def render_agents_install(
    text: str, block: str, legacy_blocks: Optional[List[str]] = None
) -> str:
    span = _managed_block_span(text)
    if span is not None:
        start, end = span
        current = text[start:end]
        if current == block:
            return text
        if current in (legacy_blocks or []):
            return text[:start] + block + text[end:]
        if current != block:
            raise GateError("AGENTS.md 的模型路由标记块已被修改；拒绝覆盖")
        return text

    if not text:
        return block + "\n"

    first_newline = text.find("\n")
    if text.startswith("# ") and first_newline >= 0:
        insertion = first_newline + 1
        remainder = text[insertion:]
        separator = "\n" if not remainder.startswith("\n") else ""
        return text[:insertion] + "\n" + block + "\n" + separator + remainder

    return block + "\n\n" + text


def render_agents_uninstall(
    text: str, block: str, legacy_blocks: Optional[List[str]] = None
) -> str:
    span = _managed_block_span(text)
    if span is None:
        return text
    start, end = span
    if text[start:end] not in [block, *(legacy_blocks or [])]:
        raise GateError("AGENTS.md 的模型路由标记块已被修改；拒绝卸载")
    remove_end = end
    if text[remove_end : remove_end + 2] == "\n\n":
        remove_end += 2
    elif text[remove_end : remove_end + 1] == "\n":
        remove_end += 1
    result = text[:start] + text[remove_end:]
    if start > 0 and result[start - 1 : start + 1] == "\n\n":
        result = result[:start] + result[start + 1 :]
    return result


def _load_hooks(path: Path) -> Dict[str, Any]:
    text = _read_text_or_empty(path)
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise GateError("hooks.json 不是有效 JSON：%s" % error)
    if not isinstance(value, dict):
        raise GateError("hooks.json 顶层必须是对象")
    hooks = value.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise GateError("hooks.json 的 hooks 字段必须是对象")
    return value


def _owned_groups(config: Dict[str, Any], command: str) -> List[Dict[str, Any]]:
    result = []
    hooks = config.get("hooks", {})
    groups = hooks.get("UserPromptSubmit", []) if isinstance(hooks, dict) else []
    if groups is None:
        groups = []
    if not isinstance(groups, list):
        raise GateError("UserPromptSubmit 配置必须是数组")
    for group in groups:
        if not isinstance(group, dict):
            raise GateError("UserPromptSubmit 匹配组必须是对象")
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            raise GateError("UserPromptSubmit 的 hooks 必须是数组")
        for handler in handlers:
            if isinstance(handler, dict) and handler.get("command") == command:
                result.append(group)
                break
    return result


def render_hooks_install(config: Dict[str, Any], paths: Paths) -> Dict[str, Any]:
    desired = copy.deepcopy(config)
    hooks = desired.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise GateError("hooks.json 的 hooks 字段必须是对象")
    owned = _owned_groups(desired, paths.hook_command)
    if len(owned) > 1:
        raise GateError("检测到重复的模型路由 UserPromptSubmit Hook")
    if owned:
        if owned[0] != paths.hook_group:
            raise GateError("模型路由 Hook 配置已变化；拒绝覆盖")
        return desired
    groups = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(groups, list):
        raise GateError("UserPromptSubmit 配置必须是数组")
    groups.append(paths.hook_group)
    return desired


def render_hooks_uninstall(config: Dict[str, Any], paths: Paths) -> Dict[str, Any]:
    desired = copy.deepcopy(config)
    hooks = desired.get("hooks", {})
    if not isinstance(hooks, dict):
        raise GateError("hooks.json 的 hooks 字段必须是对象")
    groups = hooks.get("UserPromptSubmit")
    if groups is None:
        return desired
    owned = _owned_groups(desired, paths.hook_command)
    if len(owned) > 1:
        raise GateError("检测到重复的模型路由 UserPromptSubmit Hook")
    if owned and owned[0] != paths.hook_group:
        raise GateError("模型路由 Hook 配置已变化；拒绝卸载")
    if owned:
        groups.remove(owned[0])
    if groups == []:
        hooks.pop("UserPromptSubmit", None)
    return desired


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class CodexAppServer:
    """Minimal newline-delimited JSON-RPC client for the official app server."""

    def __init__(
        self,
        codex_home: Path,
        codex_binary: str = "codex",
        timeout: float = 10.0,
    ) -> None:
        self.codex_home = codex_home
        self.codex_binary = codex_binary
        self.timeout = timeout
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 0

    def __enter__(self) -> "CodexAppServer":
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.codex_home)
        try:
            self.process = subprocess.Popen(
                [self.codex_binary, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as error:
            raise GateError("无法启动 Codex app-server：%s" % error)
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "model-routing-gate-manager",
                        "version": "1.0",
                    },
                    "capabilities": None,
                },
            )
            self.notify("initialized", {})
        except Exception:
            self._close()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._close()

    def _close(self) -> None:
        if self.process is None:
            return
        process = self.process
        self.process = None
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()

    def _send(self, payload: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise GateError("Codex app-server 尚未启动")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + self.timeout
        while True:
            if self.process is None or self.process.stdout is None:
                raise GateError("Codex app-server 输出不可用")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateError("Codex app-server 调用超时：%s" % method)
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise GateError("Codex app-server 调用超时：%s" % method)
            line = self.process.stdout.readline()
            if not line:
                raise GateError("Codex app-server 在响应前退出：%s" % method)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise GateError("Codex app-server 返回错误：%s" % message["error"])
            result = message.get("result")
            if not isinstance(result, dict):
                raise GateError("Codex app-server 返回了无效结果：%s" % method)
            return result


def _find_hook_metadata(
    response: Dict[str, Any], hooks_path: Path, command: str
) -> Dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, list) or not data:
        raise GateError("hooks/list 没有返回工作目录结果")
    matches = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        errors = entry.get("errors") or []
        if errors:
            raise GateError("hooks/list 报告错误：%s" % errors)
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            source_path = hook.get("sourcePath")
            same_source = False
            if isinstance(source_path, str):
                same_source = Path(source_path).expanduser().resolve() == hooks_path.resolve()
            if (
                hook.get("eventName") == "userPromptSubmit"
                and hook.get("handlerType") == "command"
                and hook.get("command") == command
                and same_source
            ):
                matches.append(hook)
    if len(matches) != 1:
        raise GateError("hooks/list 中目标 UserPromptSubmit Hook 数量应为 1，实际为 %d" % len(matches))
    return matches[0]


def inspect_hook(
    rpc: Any, cwd: Path, hooks_path: Path, command: str
) -> Dict[str, Any]:
    response = rpc.call("hooks/list", {"cwds": [str(cwd)]})
    return _find_hook_metadata(response, hooks_path, command)


def _validate_owned_hook_key(hooks_path: Path, key: str) -> None:
    prefix = "%s:user_prompt_submit:" % hooks_path.expanduser().absolute()
    if not isinstance(key, str) or not key.startswith(prefix):
        raise GateError("Hook 信任 key 不属于当前模型路由门：%s" % key)
    position = key[len(prefix) :].split(":")
    if len(position) != 2 or not all(part.isdigit() for part in position):
        raise GateError("Hook 信任 key 位置格式无效：%s" % key)


def _validate_hook_key_lineage(state: Dict[str, Any], metadata: Dict[str, Any]) -> None:
    recorded = state.get("hook_key")
    current = metadata.get("key")
    if recorded is not None and recorded != current:
        raise GateError("Hook key 漂移，需迁移/重新安装")


def _user_hook_state(
    rpc: Any, cwd: Path, config_path: Path
) -> tuple:
    """Read the raw user-layer hooks.state and its optimistic-lock version."""
    response = rpc.call(
        "config/read", {"includeLayers": True, "cwd": str(cwd)}
    )
    layers = response.get("layers")
    if not isinstance(layers, list):
        raise GateError("config/read 未返回配置分层")
    expected_path = config_path.expanduser().resolve()
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        name = layer.get("name")
        if not isinstance(name, dict) or name.get("type") != "user":
            continue
        if name.get("profile") is not None:
            continue
        file_value = name.get("file")
        if not isinstance(file_value, str):
            continue
        if Path(file_value).expanduser().resolve() != expected_path:
            continue
        version = layer.get("version")
        if not isinstance(version, str) or not version:
            raise GateError("config/read 的用户配置版本无效")
        config = layer.get("config")
        if not isinstance(config, dict):
            raise GateError("config/read 的用户配置层无效")
        hooks = config.get("hooks") or {}
        if not isinstance(hooks, dict):
            raise GateError("用户配置 hooks 字段必须是对象")
        state = hooks.get("state") or {}
        if not isinstance(state, dict):
            raise GateError("用户配置 hooks.state 字段必须是对象")
        return copy.deepcopy(state), version
    # A missing user layer means config.toml did not exist yet.
    return {}, None


def _hook_state_key_path(key: str) -> str:
    # Quoting makes dots and punctuation in the opaque hook key one segment.
    return "hooks.state.%s" % json.dumps(key, ensure_ascii=False)


def _write_hook_trust_snapshot(
    rpc: Any,
    cwd: Path,
    config_path: Path,
    snapshot: HookTrustSnapshot,
    expected_current: Optional[Dict[str, Any]],
) -> None:
    """Restore one trust entry without replacing unrelated hooks.state keys."""
    state, version = _user_hook_state(rpc, cwd, config_path)
    previous = HookTrustSnapshot(
        snapshot.key,
        snapshot.key in state,
        copy.deepcopy(state.get(snapshot.key)) if snapshot.key in state else None,
    )
    if snapshot.exists:
        if snapshot.key in state and state.get(snapshot.key) == snapshot.value:
            return
    elif snapshot.key not in state:
        return
    current = state.get(snapshot.key)
    if current != expected_current:
        raise GateError("目标 Hook 信任项已被并发修改；拒绝覆盖")
    params: Dict[str, Any] = {
        "edits": [
            {
                "keyPath": _hook_state_key_path(snapshot.key),
                "value": copy.deepcopy(snapshot.value) if snapshot.exists else None,
                "mergeStrategy": "replace",
            }
        ],
        "filePath": str(config_path),
        "reloadUserConfig": True,
    }
    if version is not None:
        params["expectedVersion"] = version
    try:
        response = rpc.call("config/batchWrite", params)
        if response.get("status") not in {"ok", "okOverridden"}:
            raise GateError("config/batchWrite 未成功恢复 Hook 信任")
        restored, _ = _user_hook_state(rpc, cwd, config_path)
        if snapshot.exists:
            if (
                snapshot.key not in restored
                or restored.get(snapshot.key) != snapshot.value
            ):
                raise GateError("目标 Hook 信任项未精确恢复")
        elif snapshot.key in restored:
            raise GateError("目标 Hook 信任项未删除")
    except Exception as error:
        try:
            rollback_state, rollback_version = _user_hook_state(
                rpc, cwd, config_path
            )
            previous_is_current = (
                previous.key in rollback_state
                and rollback_state.get(previous.key) == previous.value
                if previous.exists
                else previous.key not in rollback_state
            )
            desired_is_current = (
                snapshot.key in rollback_state
                and rollback_state.get(snapshot.key) == snapshot.value
                if snapshot.exists
                else snapshot.key not in rollback_state
            )
            if not previous_is_current:
                if not desired_is_current:
                    raise GateError("目标 Hook 信任项在失败恢复中被并发修改")
                rollback_params: Dict[str, Any] = {
                    "edits": [
                        {
                            "keyPath": _hook_state_key_path(previous.key),
                            "value": copy.deepcopy(previous.value)
                            if previous.exists
                            else None,
                            "mergeStrategy": "replace",
                        }
                    ],
                    "filePath": str(config_path),
                    "reloadUserConfig": True,
                }
                if rollback_version is not None:
                    rollback_params["expectedVersion"] = rollback_version
                rollback_response = rpc.call("config/batchWrite", rollback_params)
                if rollback_response.get("status") not in {"ok", "okOverridden"}:
                    raise GateError("config/batchWrite 未成功回退 Hook 信任")
                verified, _ = _user_hook_state(rpc, cwd, config_path)
                if previous.exists:
                    if (
                        previous.key not in verified
                        or verified.get(previous.key) != previous.value
                    ):
                        raise GateError("目标 Hook 原信任项未恢复")
                elif previous.key in verified:
                    raise GateError("新增的目标 Hook 信任项未回退")
        except Exception as rollback_error:
            raise GateError(
                "目标 Hook 信任项写入失败且事务回退失败：%s；原错误：%s"
                % (rollback_error, error)
            )
        raise


def ensure_hook_trusted(
    rpc: Any,
    cwd: Path,
    hooks_path: Path,
    config_path: Path,
    command: str,
    expected_key: Optional[str] = None,
) -> HookTrustResult:
    """Trust exactly the target hook hash through config/batchWrite, then verify."""
    metadata = inspect_hook(rpc, cwd, hooks_path, command)
    if expected_key is not None and metadata.get("key") != expected_key:
        raise GateError("Hook key 漂移，需迁移/重新安装")
    current_hash = metadata.get("currentHash")
    if not isinstance(current_hash, str) or not current_hash.startswith("sha256:"):
        raise GateError("目标 Hook 的 currentHash 无效")
    metadata_key = metadata.get("key")
    if not isinstance(metadata_key, str) or not metadata_key:
        raise GateError("目标 Hook 缺少可持久化的 key")
    _validate_owned_hook_key(hooks_path, metadata_key)
    if metadata.get("trustStatus") != "trusted":
        hook_key = metadata.get("key")
        assert isinstance(hook_key, str)
        state, version = _user_hook_state(rpc, cwd, config_path)
        original = HookTrustSnapshot(
            key=hook_key,
            exists=hook_key in state,
            value=copy.deepcopy(state.get(hook_key)) if hook_key in state else None,
        )
        trusted_value = {"trusted_hash": current_hash}
        params: Dict[str, Any] = {
            "edits": [
                {
                    "keyPath": _hook_state_key_path(hook_key),
                    "value": trusted_value,
                    "mergeStrategy": "replace",
                }
            ],
            "filePath": str(config_path),
            "reloadUserConfig": True,
        }
        if version is not None:
            params["expectedVersion"] = version
        write_attempted = False
        try:
            write_attempted = True
            response = rpc.call("config/batchWrite", params)
            if response.get("status") not in {"ok", "okOverridden"}:
                raise GateError("config/batchWrite 未成功保存 Hook 信任")
            metadata = inspect_hook(rpc, cwd, hooks_path, command)
            if metadata.get("trustStatus") != "trusted":
                raise GateError(
                    "目标 Hook 信任状态不是 trusted：%s"
                    % metadata.get("trustStatus")
                )
            if metadata.get("enabled") is not True:
                raise GateError("目标 Hook enabled=false")
            if metadata.get("currentHash") != current_hash:
                raise GateError("目标 Hook 在信任过程中发生配置变化")
        except Exception as error:
            if write_attempted:
                try:
                    _write_hook_trust_snapshot(
                        rpc,
                        cwd,
                        config_path,
                        original,
                        expected_current=trusted_value,
                    )
                except Exception as restore_error:
                    raise GateError(
                        "Hook 信任失败，且原信任项恢复失败：%s；原错误：%s"
                        % (restore_error, error)
                    )
            raise
        return HookTrustResult(metadata=metadata, changed=True, original=original)
    if metadata.get("trustStatus") != "trusted":
        raise GateError("目标 Hook 信任状态不是 trusted：%s" % metadata.get("trustStatus"))
    if metadata.get("enabled") is not True:
        raise GateError("目标 Hook enabled=false")
    if metadata.get("currentHash") != current_hash:
        raise GateError("目标 Hook 在信任过程中发生配置变化")
    return HookTrustResult(metadata=metadata, changed=False)


class GlobalGateManager:
    def __init__(
        self,
        paths: Paths,
        rpc_factory: Optional[Callable[[], Any]] = None,
        codex_binary: str = "codex",
    ) -> None:
        self.paths = paths
        self.rpc_factory = rpc_factory or (
            lambda: CodexAppServer(paths.codex_home, codex_binary=codex_binary)
        )

    def _validate_source(self) -> bytes:
        source = self.paths.source_hook
        if not source.exists() or not source.is_file():
            raise GateError("源码 Hook 不存在：%s" % source)
        _reject_symlink(source)
        data = source.read_bytes()
        if not data:
            raise GateError("源码 Hook 为空：%s" % source)
        validation = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                "import sys; compile(sys.stdin.buffer.read(), sys.argv[1], 'exec')",
                str(source),
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if validation.returncode != 0:
            detail = validation.stderr.decode("utf-8", errors="replace").strip()
            raise GateError("源码 Hook 不能由系统 Python 3.9 解析：%s" % detail)
        return data

    def _validate_existing_target(
        self,
        source_data: bytes,
        existing_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        target = self.paths.target_hook
        if not target.exists():
            if existing_state is not None:
                raise GateError("安装状态存在但目标 Hook 已缺失：%s" % target)
            return
        _reject_symlink(target)
        if not target.is_file():
            raise GateError("目标 Hook 不是普通文件：%s" % target)
        target_hash = sha256_bytes(target.read_bytes())
        if target_hash != sha256_bytes(source_data):
            if existing_state is None:
                raise GateError("目标 Hook 与源码 Hook 哈希不匹配；拒绝覆盖")
            self._validate_state_identity(existing_state)
            if target_hash != existing_state.get("target_sha256"):
                raise GateError("目标 Hook 与安装状态哈希不匹配；拒绝升级")
        if not os.access(str(target), os.X_OK):
            raise GateError("目标 Hook 不可执行：%s" % target)

    def _desired_install(
        self,
        source_data: bytes,
        existing_state: Optional[Dict[str, Any]],
    ) -> Dict[Path, Optional[bytes]]:
        agents_text = _read_text_or_empty(self.paths.agents)
        hooks_config = _load_hooks(self.paths.hooks)
        desired_agents = render_agents_install(
            agents_text,
            self.paths.agents_block,
            self.paths.legacy_agents_blocks,
        )
        desired_hooks = render_hooks_install(hooks_config, self.paths)
        if existing_state is not None:
            self._validate_state_identity(existing_state)
        source_hash = sha256_bytes(source_data)
        agents_block_hash = sha256_bytes(self.paths.agents_block.encode("utf-8"))
        if (
            existing_state is not None
            and existing_state.get("source_sha256") == source_hash
            and existing_state.get("target_sha256") == source_hash
            and existing_state.get("agents_block_sha256") == agents_block_hash
        ):
            state = existing_state
        else:
            state = self._new_state(source_data)
            if existing_state is not None:
                state["installed_at"] = existing_state.get(
                    "installed_at", state["installed_at"]
                )
                state["upgraded_at"] = datetime.now(timezone.utc).isoformat()
                if (
                    existing_state.get("source_sha256") != source_hash
                    or existing_state.get("target_sha256") != source_hash
                ):
                    state["upgraded_from_target_sha256"] = existing_state.get(
                        "target_sha256"
                    )
                if existing_state.get("agents_block_sha256") != agents_block_hash:
                    state["upgraded_from_agents_block_sha256"] = existing_state.get(
                        "agents_block_sha256"
                    )
                for key in (
                    "hook_current_hash",
                    "trust_status",
                    "hook_key",
                    "trust_managed",
                    "trust_restore",
                    "trust_restore_inferred_legacy",
                ):
                    if key in existing_state:
                        state[key] = copy.deepcopy(existing_state[key])
        return {
            self.paths.agents: desired_agents.encode("utf-8"),
            self.paths.hooks: _json_bytes(desired_hooks),
            self.paths.target_hook: source_data,
            self.paths.state: _json_bytes(state),
        }

    def _new_state(self, source_data: bytes) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "gate_id": GATE_ID,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "source_hook": str(self.paths.source_hook),
            "source_sha256": sha256_bytes(source_data),
            "target_hook": str(self.paths.target_hook),
            "target_sha256": sha256_bytes(source_data),
            "agents_path": str(self.paths.agents),
            "agents_block_sha256": sha256_bytes(
                self.paths.agents_block.encode("utf-8")
            ),
            "hooks_path": str(self.paths.hooks),
            "hook_group_sha256": sha256_bytes(_json_bytes(self.paths.hook_group)),
            "hook_command": self.paths.hook_command,
            "hook_current_hash": None,
            "trust_status": None,
        }

    def _load_state(self, required: bool) -> Optional[Dict[str, Any]]:
        if not self.paths.state.exists():
            if required:
                raise GateError("全局门状态文件不存在：%s" % self.paths.state)
            return None
        _reject_symlink(self.paths.state)
        try:
            state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GateError("全局门状态文件无效：%s" % error)
        if not isinstance(state, dict):
            raise GateError("全局门状态文件必须是对象")
        return state

    def _validate_state(self, state: Dict[str, Any], source_data: bytes) -> None:
        self._validate_state_identity(state)
        expected = self._new_state(source_data)
        for key in (
            "source_sha256",
            "target_sha256",
            "agents_block_sha256",
        ):
            if state.get(key) != expected.get(key):
                raise GateError("全局门状态或配置变化：%s" % key)

    def _validate_state_identity(self, state: Dict[str, Any]) -> None:
        expected = self._new_state(b"identity-placeholder")
        for key in (
            "schema_version",
            "gate_id",
            "source_hook",
            "target_hook",
            "agents_path",
            "hooks_path",
            "hook_group_sha256",
            "hook_command",
        ):
            if state.get(key) != expected.get(key):
                raise GateError("全局门状态或配置变化：%s" % key)
        accepted_agents_hashes = {
            expected["agents_block_sha256"],
            *(
                sha256_bytes(block.encode("utf-8"))
                for block in self.paths.legacy_agents_blocks
            ),
        }
        if state.get("agents_block_sha256") not in accepted_agents_hashes:
            raise GateError("全局门状态或配置变化：agents_block_sha256")
        for key in ("source_sha256", "target_sha256"):
            value = state.get(key)
            if not isinstance(value, str) or not value.startswith("sha256:"):
                raise GateError("全局门状态哈希无效：%s" % key)

    def _changed_paths(self, desired: Dict[Path, Optional[bytes]]) -> List[Path]:
        changed = []
        for path, data in desired.items():
            if data is None:
                if path.exists():
                    changed.append(path)
            elif not path.exists() or path.read_bytes() != data:
                changed.append(path)
        return changed

    def _snapshot_paths(self) -> List[Path]:
        return [
            self.paths.agents,
            self.paths.hooks,
            self.paths.target_hook,
            self.paths.state,
        ]

    def _create_backup(
        self, action: str, hook_trust: Optional[HookTrustSnapshot] = None
    ) -> str:
        for path in self._snapshot_paths():
            _reject_symlink(path)
        backup_id = "%s-%s-%s" % (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
            action,
            uuid.uuid4().hex[:8],
        )
        self.paths.backup_root.mkdir(parents=True, exist_ok=True)
        pending = Path(
            tempfile.mkdtemp(prefix=".pending-", dir=str(self.paths.backup_root))
        )
        final = self.paths.backup_root / backup_id
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "gate_id": GATE_ID,
            "backup_id": backup_id,
            "action": action,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": [],
        }
        if hook_trust is not None:
            snapshot["hook_trust"] = hook_trust.as_dict()
        try:
            for path in self._snapshot_paths():
                exists = path.exists()
                snapshot["files"].append(
                    {
                        "path": str(path),
                        "exists": exists,
                        "mode": _existing_mode(path) if exists else None,
                        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii")
                        if exists
                        else None,
                    }
                )
            atomic_write(pending / "snapshot.json", _json_bytes(snapshot), 0o600)
            os.replace(str(pending), str(final))
        except Exception:
            shutil.rmtree(str(pending), ignore_errors=True)
            raise
        return backup_id

    def _expected_hook_key_from_snapshot(
        self, entries: List[Dict[str, Any]]
    ) -> str:
        hooks_entry = next(
            (
                entry
                for entry in entries
                if entry.get("path") == str(self.paths.hooks)
            ),
            None,
        )
        if not isinstance(hooks_entry, dict):
            raise GateError("备份缺少 hooks.json 记录")
        if hooks_entry.get("exists") is True:
            try:
                hooks_config = json.loads(
                    base64.b64decode(
                        hooks_entry["content_base64"], validate=True
                    ).decode("utf-8")
                )
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise GateError("备份 hooks.json 无效：%s" % error)
            if not isinstance(hooks_config, dict):
                raise GateError("备份 hooks.json 顶层必须是对象")
        else:
            hooks_config = {}
        desired = render_hooks_install(hooks_config, self.paths)
        groups = desired.get("hooks", {}).get("UserPromptSubmit", [])
        matches = [
            index for index, group in enumerate(groups) if group == self.paths.hook_group
        ]
        if len(matches) != 1:
            raise GateError("备份无法定位唯一模型路由 Hook")
        return "%s:user_prompt_submit:%d:0" % (
            self.paths.hooks,
            matches[0],
        )

    def _load_backup_snapshot(self, backup_id: str) -> Dict[str, Any]:
        if (
            not backup_id
            or Path(backup_id).name != backup_id
            or backup_id.startswith(".")
        ):
            raise GateError("备份编号无效：%s" % backup_id)
        snapshot_path = self.paths.backup_root / backup_id / "snapshot.json"
        if not snapshot_path.exists() or not snapshot_path.is_file():
            raise GateError("找不到备份：%s" % backup_id)
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GateError("备份清单无效：%s" % error)
        if not isinstance(snapshot, dict) or snapshot.get("gate_id") != GATE_ID:
            raise GateError("备份不属于当前模型路由门")
        allowed = {str(path) for path in self._snapshot_paths()}
        entries = snapshot.get("files")
        if not isinstance(entries, list):
            raise GateError("备份缺少文件清单")
        seen = set()
        for entry in entries:
            path_text = entry.get("path") if isinstance(entry, dict) else None
            if path_text not in allowed:
                raise GateError("备份包含越界路径：%s" % path_text)
            if path_text in seen:
                raise GateError("备份包含重复路径：%s" % path_text)
            seen.add(path_text)
            exists = entry.get("exists")
            mode = entry.get("mode")
            encoded = entry.get("content_base64")
            if not isinstance(exists, bool):
                raise GateError("备份文件存在状态无效：%s" % path_text)
            if exists:
                if not isinstance(mode, int) or mode < 0 or mode > 0o7777:
                    raise GateError("备份文件权限无效：%s" % path_text)
                if not isinstance(encoded, str):
                    raise GateError("备份文件内容缺失：%s" % path_text)
                try:
                    base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as error:
                    raise GateError("备份文件内容无效：%s" % error)
            elif mode is not None or encoded is not None:
                raise GateError("不存在的备份文件不应包含内容：%s" % path_text)
        if seen != allowed:
            raise GateError("备份文件清单不完整")
        trust = snapshot.get("hook_trust")
        if trust is not None:
            if not isinstance(trust, dict):
                raise GateError("备份 Hook 信任记录无效")
            key = trust.get("key")
            exists = trust.get("exists")
            value = trust.get("value")
            if not isinstance(key, str) or not key or not isinstance(exists, bool):
                raise GateError("备份 Hook 信任记录不完整")
            _validate_owned_hook_key(self.paths.hooks, key)
            expected_key = self._expected_hook_key_from_snapshot(entries)
            if key != expected_key:
                raise GateError("Hook key 漂移，需迁移/重新安装")
            if exists and not isinstance(value, dict):
                raise GateError("备份 Hook 信任值无效")
            if not exists and value is not None:
                raise GateError("备份 Hook 信任空值无效")
            state_entry = next(
                (
                    entry
                    for entry in entries
                    if entry.get("path") == str(self.paths.state)
                ),
                None,
            )
            if isinstance(state_entry, dict) and state_entry.get("exists") is True:
                try:
                    state_value = json.loads(
                        base64.b64decode(
                            state_entry["content_base64"], validate=True
                        ).decode("utf-8")
                    )
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise GateError("备份全局门状态无效：%s" % error)
                recorded_key = (
                    state_value.get("hook_key")
                    if isinstance(state_value, dict)
                    else None
                )
                if recorded_key is not None and recorded_key != key:
                    raise GateError("Hook key 漂移，需迁移/重新安装")
        return snapshot

    def _attach_backup_trust(
        self, backup_id: str, hook_trust: HookTrustSnapshot
    ) -> None:
        snapshot = self._load_backup_snapshot(backup_id)
        snapshot["hook_trust"] = hook_trust.as_dict()
        snapshot_path = self.paths.backup_root / backup_id / "snapshot.json"
        atomic_write(snapshot_path, _json_bytes(snapshot), 0o600)

    def _apply_desired(self, desired: Dict[Path, Optional[bytes]]) -> None:
        for path, data in desired.items():
            if data is None:
                _reject_symlink(path)
                if path.exists():
                    path.unlink()
                continue
            default_mode = 0o700 if path == self.paths.target_hook else 0o600
            mode = _existing_mode(path, default=default_mode)
            if path == self.paths.target_hook:
                mode = 0o700
            atomic_write(path, data, mode)

    @contextmanager
    def _rpc(self) -> Iterator[Any]:
        client = self.rpc_factory()
        enter = getattr(client, "__enter__", None)
        if callable(enter):
            with client as active:
                yield active
        else:
            yield client

    def _inspect_or_trust(
        self, trust: bool, state: Optional[Dict[str, Any]] = None
    ) -> HookTrustResult:
        with self._rpc() as rpc:
            if trust:
                return ensure_hook_trusted(
                    rpc,
                    cwd=self.paths.repo_root,
                    hooks_path=self.paths.hooks,
                    config_path=self.paths.config,
                    command=self.paths.hook_command,
                    expected_key=state.get("hook_key")
                    if isinstance(state, dict)
                    else None,
                )
            metadata = inspect_hook(
                rpc,
                cwd=self.paths.repo_root,
                hooks_path=self.paths.hooks,
                command=self.paths.hook_command,
            )
            _validate_owned_hook_key(self.paths.hooks, metadata.get("key"))
            if isinstance(state, dict):
                _validate_hook_key_lineage(state, metadata)
            return HookTrustResult(metadata=metadata, changed=False)

    def _restore_trust_snapshot(
        self,
        snapshot: HookTrustSnapshot,
        expected_current: Optional[Dict[str, Any]],
    ) -> None:
        with self._rpc() as rpc:
            _write_hook_trust_snapshot(
                rpc,
                self.paths.repo_root,
                self.paths.config,
                snapshot,
                expected_current,
            )

    @staticmethod
    def _trust_snapshot_from_dict(value: Any) -> Optional[HookTrustSnapshot]:
        if not isinstance(value, dict):
            return None
        key = value.get("key")
        exists = value.get("exists")
        item = value.get("value")
        if not isinstance(key, str) or not key or not isinstance(exists, bool):
            raise GateError("全局门信任恢复状态无效")
        if exists and not isinstance(item, dict):
            raise GateError("全局门信任恢复值无效")
        if not exists and item is not None:
            raise GateError("全局门信任恢复空值无效")
        return HookTrustSnapshot(key, exists, copy.deepcopy(item))

    def install(self, dry_run: bool = False, trust: bool = False) -> OperationResult:
        source_data = self._validate_source()
        existing_state = self._load_state(required=False)
        self._validate_existing_target(source_data, existing_state)
        desired = self._desired_install(source_data, existing_state)
        pretrust_metadata = None
        if existing_state is not None:
            pretrust_metadata = self._inspect_or_trust(
                False, existing_state
            ).metadata
        changed_paths = self._changed_paths(desired)
        if dry_run:
            return OperationResult("install", bool(changed_paths), dry_run=True)

        trust_needs_backup = False
        if trust and not changed_paths:
            trust_needs_backup = (
                (pretrust_metadata or {}).get("trustStatus")
                != "trusted"
            )
        state_needs_trust_migration = (
            trust
            and isinstance(existing_state, dict)
            and "trust_managed" not in existing_state
        )
        backup_id = None
        if changed_paths or trust_needs_backup or state_needs_trust_migration:
            backup_id = self._create_backup("install")
            try:
                self._apply_desired(desired)
            except Exception:
                self._restore_backup(backup_id)
                raise

        trust_result: Optional[HookTrustResult] = None
        state_changed = False
        try:
            applied_state = self._load_state(required=True)
            assert applied_state is not None
            trust_result = self._inspect_or_trust(trust, applied_state)
            metadata = trust_result.metadata
            state = applied_state
            previous_status = state.get("trust_status")
            previous_hash = state.get("hook_current_hash")
            trust_management_recorded = "trust_managed" in state
            previously_managed_trust = state.get("trust_managed") is True
            state["hook_current_hash"] = metadata.get("currentHash")
            state["trust_status"] = metadata.get("trustStatus")
            hook_key = metadata.get("key")
            if isinstance(hook_key, str) and hook_key:
                state["hook_key"] = hook_key
            if trust_result.changed and trust_result.original is not None:
                state["trust_managed"] = True
                legacy_trust = (
                    not trust_management_recorded
                    and previous_status == "trusted"
                    and trust_result.original.exists
                    and trust_result.original.value
                    == {"trusted_hash": previous_hash}
                )
                if legacy_trust:
                    state["trust_restore"] = HookTrustSnapshot(
                        trust_result.original.key, False, None
                    ).as_dict()
                    state["trust_restore_inferred_legacy"] = True
                elif not previously_managed_trust:
                    state["trust_restore"] = trust_result.original.as_dict()
                if backup_id is not None:
                    self._attach_backup_trust(backup_id, trust_result.original)
            elif (
                trust
                and not trust_management_recorded
                and previous_status == "trusted"
                and previous_hash == metadata.get("currentHash")
                and isinstance(hook_key, str)
            ):
                # Migration for installs made by manager versions that persisted
                # trust but did not record its pre-install value.  The hook key is
                # exclusively owned by this gate, so only that key is removed.
                state["trust_managed"] = True
                state["trust_restore"] = HookTrustSnapshot(
                    hook_key, False, None
                ).as_dict()
                state["trust_restore_inferred_legacy"] = True
            elif not trust_management_recorded:
                state["trust_managed"] = False
            state_bytes = _json_bytes(state)
            if self.paths.state.read_bytes() != state_bytes:
                state_changed = True
                atomic_write(
                    self.paths.state,
                    state_bytes,
                    _existing_mode(self.paths.state, default=0o600),
                )
        except Exception as error:
            restore_error = None
            if (
                trust_result is not None
                and trust_result.changed
                and trust_result.original is not None
            ):
                try:
                    self._restore_trust_snapshot(
                        trust_result.original,
                        {"trusted_hash": trust_result.metadata.get("currentHash")},
                    )
                except Exception as caught:
                    restore_error = caught
            if backup_id is not None:
                self._restore_backup(backup_id)
            if restore_error is not None:
                raise GateError(
                    "安装失败，且目标 Hook 信任项恢复失败：%s；原错误：%s"
                    % (restore_error, error)
                )
            raise
        return OperationResult(
            "install",
            bool(changed_paths or trust_result.changed or state_changed),
            backup_id=backup_id,
            trust_status=metadata.get("trustStatus"),
        )

    def check(self, require_trust: bool = True) -> CheckReport:
        issues = []
        source_data: Optional[bytes] = None
        try:
            source_data = self._validate_source()
        except GateError as error:
            issues.append(str(error))

        if source_data is not None:
            target = self.paths.target_hook
            if not target.exists():
                issues.append("目标 Hook 不存在：%s" % target)
            else:
                try:
                    _reject_symlink(target)
                    if sha256_bytes(target.read_bytes()) != sha256_bytes(source_data):
                        issues.append("目标 Hook 与源码 Hook 哈希不匹配")
                    if not os.access(str(target), os.X_OK):
                        issues.append("目标 Hook 不可执行")
                except (GateError, OSError) as error:
                    issues.append(str(error))

            try:
                agents = _read_text_or_empty(self.paths.agents)
                span = _managed_block_span(agents)
                if span is None:
                    issues.append("AGENTS.md 缺少模型路由标记块")
                elif agents[span[0] : span[1]] != self.paths.agents_block:
                    issues.append("AGENTS.md 的模型路由标记块已变化")
            except GateError as error:
                issues.append(str(error))

            try:
                hooks = _load_hooks(self.paths.hooks)
                owned = _owned_groups(hooks, self.paths.hook_command)
                if len(owned) != 1 or owned[0] != self.paths.hook_group:
                    issues.append("模型路由 Hook 配置缺失、重复或已变化")
            except GateError as error:
                issues.append(str(error))

            try:
                state = self._load_state(required=True)
                assert state is not None
                self._validate_state(state, source_data)
            except GateError as error:
                issues.append(str(error))

        trust_status = None
        current_hash = None
        try:
            state = self._load_state(required=False)
            metadata = self._inspect_or_trust(False, state).metadata
            trust_status = metadata.get("trustStatus")
            current_hash = metadata.get("currentHash")
            if metadata.get("enabled") is not True:
                issues.append("目标 Hook enabled=false")
            if require_trust and trust_status != "trusted":
                issues.append("目标 Hook trustStatus=%s" % trust_status)
            if state is not None and state.get("hook_current_hash") not in {
                None,
                current_hash,
            }:
                issues.append("目标 Hook currentHash 与安装状态不一致")
        except GateError as error:
            issues.append(str(error))
        return CheckReport(
            ok=not issues,
            issues=issues,
            trust_status=trust_status,
            current_hash=current_hash,
        )

    def uninstall(self, dry_run: bool = False) -> OperationResult:
        state = self._load_state(required=True)
        assert state is not None
        self._validate_state_identity(state)
        metadata = self._inspect_or_trust(False, state).metadata
        target = self.paths.target_hook
        if not target.exists() or not target.is_file():
            raise GateError("安装状态存在但目标 Hook 已缺失：%s" % target)
        _reject_symlink(target)
        if sha256_bytes(target.read_bytes()) != state.get("target_sha256"):
            raise GateError("目标 Hook 与安装状态哈希不匹配；拒绝卸载")
        if not os.access(str(target), os.X_OK):
            raise GateError("目标 Hook 不可执行：%s" % target)
        agents = _read_text_or_empty(self.paths.agents)
        hooks = _load_hooks(self.paths.hooks)
        desired = {
            self.paths.agents: render_agents_uninstall(
                agents,
                self.paths.agents_block,
                self.paths.legacy_agents_blocks,
            ).encode("utf-8"),
            self.paths.hooks: _json_bytes(render_hooks_uninstall(hooks, self.paths)),
            self.paths.target_hook: None,
            self.paths.state: None,
        }
        changed = self._changed_paths(desired)
        if dry_run:
            return OperationResult("uninstall", bool(changed), dry_run=True)
        if not changed:
            return OperationResult("uninstall", False)
        trust_restore = None
        current_trust = None
        if state.get("trust_managed") is True:
            trust_restore = self._trust_snapshot_from_dict(state.get("trust_restore"))
            if trust_restore is None:
                raise GateError("缺少目标 Hook 的信任恢复状态")
            recorded_key = state.get("hook_key")
            if recorded_key != trust_restore.key or metadata.get("key") != recorded_key:
                raise GateError("Hook key 漂移，需迁移/重新安装")
            with self._rpc() as rpc:
                current_state, _ = _user_hook_state(
                    rpc, self.paths.repo_root, self.paths.config
                )
            current_trust = HookTrustSnapshot(
                trust_restore.key,
                trust_restore.key in current_state,
                copy.deepcopy(current_state.get(trust_restore.key))
                if trust_restore.key in current_state
                else None,
            )
        backup_id = self._create_backup("uninstall", hook_trust=current_trust)
        trust_restored = False
        try:
            if trust_restore is not None:
                expected = (
                    copy.deepcopy(current_trust.value)
                    if current_trust is not None and current_trust.exists
                    else None
                )
                self._restore_trust_snapshot(trust_restore, expected)
                trust_restored = True
            self._apply_desired(desired)
        except Exception:
            self._restore_backup(backup_id)
            if trust_restored and current_trust is not None:
                expected = (
                    copy.deepcopy(trust_restore.value)
                    if trust_restore is not None and trust_restore.exists
                    else None
                )
                self._restore_trust_snapshot(current_trust, expected)
            raise
        return OperationResult("uninstall", True, backup_id=backup_id)

    def _restore_backup(self, backup_id: str) -> None:
        snapshot = self._load_backup_snapshot(backup_id)
        entries = snapshot["files"]
        for entry in entries:
            path = Path(entry["path"])
            _reject_symlink(path)
            if entry.get("exists"):
                encoded = entry.get("content_base64")
                mode = entry.get("mode")
                if not isinstance(encoded, str) or not isinstance(mode, int):
                    raise GateError("备份文件记录不完整：%s" % path)
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as error:
                    raise GateError("备份文件内容无效：%s" % error)
                atomic_write(path, content, mode)
            elif path.exists():
                path.unlink()

    def rollback(
        self, backup_id: Optional[str] = None, dry_run: bool = False
    ) -> OperationResult:
        selected = backup_id
        if selected is None:
            if not self.paths.backup_root.exists():
                raise GateError("没有可用备份")
            candidates = sorted(
                path.name
                for path in self.paths.backup_root.iterdir()
                if path.is_dir() and not path.name.startswith(".pending-")
            )
            if not candidates:
                raise GateError("没有可用备份")
            selected = candidates[-1]
        selected_snapshot = self._load_backup_snapshot(selected)
        if dry_run:
            return OperationResult("rollback", True, dry_run=True, backup_id=selected)
        selected_trust = self._trust_snapshot_from_dict(
            selected_snapshot.get("hook_trust")
        )
        if selected_trust is None and selected_snapshot.get("action") == "install":
            target_entry = next(
                (
                    entry
                    for entry in selected_snapshot["files"]
                    if entry.get("path") == str(self.paths.target_hook)
                ),
                None,
            )
            current_state = self._load_state(required=False)
            if (
                isinstance(target_entry, dict)
                and target_entry.get("exists") is False
                and isinstance(current_state, dict)
                and current_state.get("trust_managed") is True
            ):
                # Compatibility with backups created before hook trust was
                # included in snapshot.json.  A first-install snapshot with no
                # target script must restore the manager-recorded preinstall
                # trust value as well.
                selected_trust = self._trust_snapshot_from_dict(
                    current_state.get("trust_restore")
                )
                if selected_trust is None:
                    raise GateError("旧版安装备份缺少可恢复的 Hook 信任状态")
        current_gate_state = self._load_state(required=False)
        if selected_trust is not None and isinstance(current_gate_state, dict):
            recorded_key = current_gate_state.get("hook_key")
            if recorded_key is not None and recorded_key != selected_trust.key:
                raise GateError("Hook key 漂移，需迁移/重新安装")
        current_trust = None
        if selected_trust is not None:
            with self._rpc() as rpc:
                current_state, _ = _user_hook_state(
                    rpc, self.paths.repo_root, self.paths.config
                )
            current_trust = HookTrustSnapshot(
                selected_trust.key,
                selected_trust.key in current_state,
                copy.deepcopy(current_state.get(selected_trust.key))
                if selected_trust.key in current_state
                else None,
            )
        safety_backup = self._create_backup(
            "pre-rollback", hook_trust=current_trust
        )
        trust_restored = False
        try:
            self._restore_backup(selected)
            if selected_trust is not None:
                expected = (
                    copy.deepcopy(current_trust.value)
                    if current_trust is not None and current_trust.exists
                    else None
                )
                self._restore_trust_snapshot(selected_trust, expected)
                trust_restored = True
        except Exception:
            self._restore_backup(safety_backup)
            if trust_restored and current_trust is not None:
                expected = (
                    copy.deepcopy(selected_trust.value)
                    if selected_trust is not None and selected_trust.exists
                    else None
                )
                self._restore_trust_snapshot(current_trust, expected)
            raise
        return OperationResult("rollback", True, backup_id=safety_backup)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理 Codex 全局模型路由门")
    parser.add_argument(
        "action", choices=("install", "check", "uninstall", "rollback", "trust")
    )
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--source-hook", type=Path)
    parser.add_argument("--target-hook", type=Path)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--backup-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--trust", action="store_true", help="install 后信任目标 Hook")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    paths = Paths.from_values(
        repo_root=args.repo_root,
        codex_home=args.codex_home,
        source_hook=args.source_hook,
        target_hook=args.target_hook,
    )
    manager = GlobalGateManager(paths, codex_binary=args.codex_binary)
    try:
        if args.action == "install":
            result: Any = manager.install(dry_run=args.dry_run, trust=args.trust)
            exit_code = 0
        elif args.action == "check":
            result = manager.check(require_trust=True)
            exit_code = 0 if result.ok else 1
        elif args.action == "uninstall":
            result = manager.uninstall(dry_run=args.dry_run)
            exit_code = 0
        elif args.action == "rollback":
            result = manager.rollback(args.backup_id, dry_run=args.dry_run)
            exit_code = 0
        else:
            if args.dry_run:
                raise GateError("trust 不支持 --dry-run")
            trust_install = manager.install(trust=True)
            result = {
                "action": "trust",
                "changed": trust_install.changed,
                "backup_id": trust_install.backup_id,
                "trust_status": trust_install.trust_status,
            }
            exit_code = 0
    except GateError as error:
        payload = {"ok": False, "error": str(error)}
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("失败：%s" % error, file=sys.stderr)
        return 2

    payload = result.as_dict() if hasattr(result, "as_dict") else result
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
