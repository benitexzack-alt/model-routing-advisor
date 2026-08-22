#!/usr/bin/env python3
"""Fail-open UserPromptSubmit gate for model-routing-advisor.

The hook stores only metadata and a SHA-256 digest of the prompt. It never
persists the prompt text itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Union

try:
    import fcntl
except ImportError:  # pragma: no cover - Codex currently runs this hook on macOS/Linux.
    fcntl = None


SCHEMA_VERSION = 1
STATE_FILENAME = "gate-state.json"
LOG_FILENAME = "gate-events.jsonl"
LOCK_FILENAME = ".gate.lock"
STATE_DIR_ENV = "MODEL_ROUTING_GATE_STATE_DIR"
MAX_SESSIONS = 1000

REQUIRED_TEXT_FIELDS = ("session_id", "turn_id", "cwd", "prompt")

CHAT_MESSAGES = {
    "你好",
    "您好",
    "在吗",
    "谢谢",
    "多谢",
    "好的",
    "好",
    "收到",
    "明白",
    "懂了",
    "知道了",
    "可以",
    "行",
    "嗯",
    "对",
    "是的",
    "没事",
}

EXPLANATION_PREFIXES = (
    "什么是",
    "解释一下",
    "这是什么意思",
    "什么意思",
    "为什么",
    "怎么理解",
    "区别是什么",
    "有什么区别",
)

INITIAL_SELECTION_PATTERN = re.compile(
    r"^(?:(?:好的?|确认|可以|同意)[，,]?)?(?:那就|就)?"
    r"(?:按推荐执行|优先节省额度|优先保证质量)"
    r"(?=$|[。！!，,；;：:])"
)

USER_ROUTE_REQUEST_PATTERNS = (
    re.compile(r"重选(?:一下|一次|一遍)?(?:模型|档位|推理档位)"),
    re.compile(
        r"(?:重新|再次|再)(?:选|选择|推荐|评估)"
        r"(?:一下|一次|一遍)?(?:模型|档位|推理档位)"
    ),
    re.compile(
        r"(?:重新|再次|再)(?:给我|生成|做)?(?:一张|一次|一遍)?"
        r"(?:模型)?(?:路由卡|路由|选型)"
    ),
    re.compile(r"(?:换|更换)(?:一个|个|一下)?模型"),
    re.compile(
        r"(?:把)?(?:模型|档位|推理档位).{0,8}"
        r"(?:换成|改成|改为|调整为|调到|切换到)"
    ),
    re.compile(
        r"(?:换成|改成|改为|调整为|切换为|切换到).{0,8}"
        r"(?:gpt[-\w.]*|sol|terra|luna|spark)"
    ),
    re.compile(r"(?:改成|改为|调整为|切换为)(?:优先)?(?:节省|省)(?:一点|一些)?额度"),
    re.compile(r"(?:改成|改为|调整为|切换为)(?:优先)?(?:保证|保障|保)质量"),
)

NEGATION_TERMS = (
    "不要",
    "不得",
    "禁止",
    "不允许",
    "无需",
    "不需要",
    "不能",
    "别",
    "不用",
    "暂不",
    "先不",
)
NEGATION_WINDOW_CHARS = 12
NEGATION_SCOPE_BREAKERS = re.compile(
    r"(?:而是|但是|但|却|改为|转为|然后|随后|接下来|"
    r"拖延|等待|推迟|阻止|避免|拒绝|取消)"
)

class HookInputError(ValueError):
    """Raised when UserPromptSubmit input is unusable."""


class GateStateError(RuntimeError):
    """Raised when the gate state cannot be trusted."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_for_match(prompt: str) -> str:
    text = unicodedata.normalize("NFKC", prompt).strip().lower()
    return re.sub(r"\s+", "", text)


def _strip_terminal_punctuation(text: str) -> str:
    return text.strip("。！!？?，,；;：:～~… \t\r\n")


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _validate_payload(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise HookInputError("input_not_object")
    if raw.get("hook_event_name") != "UserPromptSubmit":
        raise HookInputError("unexpected_hook_event")

    result: dict[str, str] = {}
    for field in REQUIRED_TEXT_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise HookInputError(f"missing_or_invalid_{field}")
        result[field] = value
    return result


def _is_chat(text: str) -> bool:
    return _strip_terminal_punctuation(text) in CHAT_MESSAGES


def _is_simple_explanation(text: str) -> bool:
    if len(text) > 80:
        return False
    return text.startswith(EXPLANATION_PREFIXES)


def _match_is_negated(text: str, match: re.Match[str]) -> bool:
    prefix = text[: match.start()]
    clause_start = max(prefix.rfind(mark) for mark in "。！？!?，,；;：:\n")
    local_prefix = prefix[clause_start + 1 :][-NEGATION_WINDOW_CHARS:]
    candidates = [
        (local_prefix.rfind(term), term)
        for term in NEGATION_TERMS
        if term in local_prefix
    ]
    if not candidates:
        return False
    negation_start, negation = max(candidates, key=lambda item: item[0])
    intervening = local_prefix[negation_start + len(negation) :]
    return NEGATION_SCOPE_BREAKERS.search(intervening) is None


def _is_user_route_request(text: str) -> bool:
    for pattern in USER_ROUTE_REQUEST_PATTERNS:
        for match in pattern.finditer(text):
            if not _match_is_negated(text, match):
                return True
    return False


def _is_initial_selection(text: str) -> bool:
    return INITIAL_SELECTION_PATTERN.match(_strip_terminal_punctuation(text)) is not None


def _new_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "sessions": {}}


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _new_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GateStateError("state_read_failed") from error
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise GateStateError("unsupported_state_schema")
    if not isinstance(state.get("sessions"), dict):
        raise GateStateError("invalid_sessions_state")
    return state


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _append_event(path: Path, event: dict[str, Any]) -> None:
    line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def _unsafe_state_roots() -> set[Path]:
    """Return broad roots that must never be used as the managed state dir."""

    candidates = {
        Path("/"),
        Path("/home"),
        Path("/tmp"),
        Path("/var"),
        Path("/var/tmp"),
        Path("/var/folders"),
        Path("/private"),
        Path("/private/tmp"),
        Path("/private/var"),
        Path("/private/var/tmp"),
        Path("/private/var/folders"),
        Path("/Users"),
        Path.home(),
        Path(tempfile.gettempdir()),
    }
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        candidates.add(Path(codex_home).expanduser())
    else:
        candidates.add(Path.home() / ".codex")

    resolved: set[Path] = set()
    for candidate in candidates:
        try:
            resolved.add(candidate.resolve(strict=False))
        except OSError:
            continue
    return resolved


def _validate_state_dir(state_dir: Path) -> None:
    try:
        resolved = state_dir.expanduser().resolve(strict=False)
    except OSError as error:
        raise GateStateError("state_dir_resolution_failed") from error
    if resolved in _unsafe_state_roots():
        raise GateStateError("unsafe_state_dir")
    if resolved.exists():
        try:
            metadata = resolved.stat()
        except OSError as error:
            raise GateStateError("state_dir_stat_failed") from error
        if metadata.st_uid != os.getuid():
            raise GateStateError("state_dir_not_owned")
        if metadata.st_mode & 0o022:
            raise GateStateError("state_dir_writable_by_others")


@contextmanager
def _state_lock(state_dir: Path) -> Iterator[None]:
    _validate_state_dir(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not state_dir.is_dir():
        raise NotADirectoryError(str(state_dir))
    lock_path = state_dir / LOCK_FILENAME
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prune_sessions(sessions: dict[str, Any]) -> None:
    overflow = len(sessions) - MAX_SESSIONS
    if overflow <= 0:
        return
    oldest = sorted(
        sessions,
        key=lambda key: str(sessions[key].get("last_seen_at", "")),
    )
    for key in oldest[:overflow]:
        del sessions[key]


def _session_counter(session: Optional[dict[str, Any]]) -> int:
    if not session:
        return 0
    for field in ("route_prompt_count", "injection_count", "check_count"):
        if field not in session:
            continue
        value = session[field]
        if type(value) is not int or value < 0:
            raise GateStateError("invalid_route_prompt_count")
        return value
    return 0


def _session_flag(
    session: Optional[dict[str, Any]],
    field: str,
    *,
    default: bool,
) -> bool:
    if not session or field not in session:
        return default
    value = session[field]
    if type(value) is not bool:
        raise GateStateError(f"invalid_{field}")
    return value


def _route_prompt_shown(session: Optional[dict[str, Any]]) -> bool:
    return _session_flag(
        session,
        "route_prompt_shown",
        default=_session_counter(session) > 0,
    )


def _route_selection_observed(session: Optional[dict[str, Any]]) -> bool:
    # New state records only that the hook saw one of the three choice phrases;
    # it is not evidence that the runtime applied a model or effort setting.
    # Legacy state predates this field, so a prior injected card is migrated as
    # sticky instead of trapping an existing conversation in pending forever.
    if not session:
        return False
    return _session_flag(
        session,
        "route_selection_observed",
        default=_session_counter(session) > 0,
    )


def _decide(
    prompt: str,
    session: Optional[dict[str, Any]],
) -> tuple[str, str, Optional[str], bool]:
    text = _normalize_for_match(prompt)
    last_signature = session.get("last_observed_signature") if session else None
    prompt_shown = _route_prompt_shown(session)
    selection_observed = _route_selection_observed(session)

    if not prompt_shown:
        if _is_chat(text):
            return "skip", "chat", last_signature, False
        if _is_simple_explanation(text):
            return "skip", "simple_explanation", last_signature, False
        return "inject", "new_task", "routing:initial", False

    if not selection_observed:
        if _is_initial_selection(text):
            return "skip", "route_already_set", last_signature, True
        return "skip", "route_prompt_pending", last_signature, False

    if _is_user_route_request(text):
        return "inject", "user_requested", "routing:user_requested", False
    return "skip", "route_already_set", last_signature, True


def _gate_context(reason: str) -> str:
    if reason == "user_requested":
        instruction = (
            "用户明确要求改选：调用 model-routing-advisor 重新给出路由卡并等待选择；"
            "不得自动切换模型。"
        )
    elif reason == "new_task":
        instruction = (
            "本会话首次实质任务：执行前调用 model-routing-advisor 给出路由卡并等待选择。"
            "之后仅在用户明确要求改选时重路由；不得自动切换模型。"
        )
    else:
        instruction = (
            "模型路由门禁状态异常：执行前人工检查 model-routing-advisor；"
            "不得自动切换模型。"
        )
    return (
        f'<model-routing-gate reason="{reason}">'
        f"{instruction}"
        "</model-routing-gate>"
    )


def _normal_output(decision: str, reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {"continue": True}
    if decision == "inject":
        result["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _gate_context(reason),
        }
    return result


def _warning_output(code: str, *, include_context: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "continue": True,
        "systemMessage": (
            f"模型路由门禁告警：{code}；本轮按 fail-open 继续，"
            "请人工检查 model-routing-advisor，且不要自动切换模型。"
        ),
    }
    if include_context:
        result["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _gate_context("gate_error"),
        }
    return result


def resolve_state_dir() -> Path:
    configured = os.environ.get(STATE_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return base / "model-routing-advisor" / "global-gate"


def process_hook(
    raw_payload: Any,
    *,
    state_dir: Optional[Union[Path, str]] = None,
    occurred_at: Optional[str] = None,
) -> dict[str, Any]:
    """Process one UserPromptSubmit payload without ever blocking the request."""

    try:
        payload = _validate_payload(raw_payload)
    except HookInputError as error:
        return _warning_output(str(error), include_context=False)

    prompt = payload["prompt"]
    prompt_sha256 = _prompt_hash(prompt)
    state_root = Path(state_dir) if state_dir is not None else resolve_state_dir()
    event_time = occurred_at or _now_iso()

    try:
        with _state_lock(state_root):
            state_path = state_root / STATE_FILENAME
            log_path = state_root / LOG_FILENAME
            state = _load_state(state_path)
            sessions = state["sessions"]
            session = sessions.get(payload["session_id"])
            if session is not None and not isinstance(session, dict):
                raise GateStateError("invalid_session_state")

            prompt_count = _session_counter(session)
            prompt_shown = _route_prompt_shown(session)
            selection_observed = _route_selection_observed(session)

            is_duplicate = bool(
                session
                and session.get("turn_id") == payload["turn_id"]
                and session.get("last_prompt_sha256") == prompt_sha256
            )
            if is_duplicate:
                decision = "skip"
                reason = "duplicate_hook_invocation"
                signature = session.get("last_observed_signature")
            else:
                decision, reason, signature, selection_observed = _decide(
                    prompt, session
                )
            if decision == "inject":
                prompt_count += 1
                prompt_shown = True

            sessions[payload["session_id"]] = {
                "session_id": payload["session_id"],
                "turn_id": payload["turn_id"],
                "cwd": payload["cwd"],
                "last_prompt_sha256": prompt_sha256,
                "last_decision": decision,
                "last_reason": reason,
                "last_observed_signature": signature,
                "route_prompt_count": prompt_count,
                "route_prompt_shown": prompt_shown,
                "route_selection_observed": selection_observed,
                "last_seen_at": event_time,
            }
            _prune_sessions(sessions)

            event = {
                "schema_version": SCHEMA_VERSION,
                "occurred_at": event_time,
                "session_id": payload["session_id"],
                "turn_id": payload["turn_id"],
                "cwd": payload["cwd"],
                "prompt_sha256": prompt_sha256,
                "decision": decision,
                "reason": reason,
                "observed_signature": signature,
            }
            _atomic_write_json(state_path, state)
            _append_event(log_path, event)
    # A prompt-submit hook must never lose the user's message because an
    # unforeseen storage/runtime error escaped the known failure classes.
    except Exception as error:
        code = f"state_unavailable:{type(error).__name__}"
        return _warning_output(code, include_context=True)

    return _normal_output(decision, reason)


def main() -> int:
    try:
        raw_text = sys.stdin.read()
        payload = json.loads(raw_text)
    except Exception:
        result = _warning_output("invalid_stdin_json", include_context=False)
    else:
        try:
            result = process_hook(payload)
        except Exception:
            result = _warning_output("unexpected_hook_failure", include_context=True)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
