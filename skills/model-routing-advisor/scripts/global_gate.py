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
import stat
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
MAX_SESSION_META_BYTES = 1024 * 1024
MAX_AUTOMATION_CONFIG_BYTES = 1024 * 1024

AUTOMATION_ID_TEXT = r"[A-Za-z0-9._-]+"
CRON_ENVELOPE_PATTERN = re.compile(
    rf"\AAutomation: (?P<name>[^\r\n]+)\r?\n"
    rf"Automation ID: (?P<id>{AUTOMATION_ID_TEXT})\r?\n"
    r"Automation memory: \$CODEX_HOME/automations/(?P=id)/memory\.md\r?\n"
    r"Last run: (?:never|"
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z \(\d+\))\r?\n"
    r"\r?\n"
    r"(?P<instructions>[\s\S]+)\Z"
)
AUTOMATION_CONFIG_STRING_LINE = re.compile(
    r"^(?P<key>id|kind|name|prompt|status|model|reasoning_effort)"
    r"\s*=\s*(?P<value>\"(?:[^\"\\]|\\.)*\")\s*$"
)
AUTOMATION_CONFIG_CWDS_LINE = re.compile(
    r"^cwds\s*=\s*(?P<value>\[(?:[^\"\\]|\"(?:[^\"\\]|\\.)*\")*\])\s*$"
)

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
    r"(?:吧|了)?"
    r"(?=$|[。！!，,；;：:])"
)

SELECTION_PHRASES = ("按推荐执行", "优先节省额度", "优先保证质量")
SELECTION_QUESTION_OR_ALTERNATIVE = re.compile(
    r"(?:[？?]|还是|或者|或是|是否|要不要|可以吗|行吗|好吗|对吗|"
    r"能不能|可不可以|好不好|怎么样|选哪个|选哪一个|哪个好|哪一个好)"
)

MODEL_ROUTE_OBJECT = (
    r"(?:(?:codex)?模型"
    r"(?!服务|接口|应用|系统|实例|端点|部署|环境|网关|供应商|权重|参数|结构|架构|字段|数据)"
    r"|档位|推理档位)"
)
MODEL_ROUTE_TARGET = (
    r"(?:gpt-5\.6-(?:sol|terra|luna)|gpt-5\.5|"
    r"gpt-5\.3-codex-spark|sol|terra|luna|spark)"
)
EFFORT_ROUTE_TARGET = r"(?:low|medium|high|xhigh|max|ultra|低|中|高|很高|极高|最大)"

EXPLICIT_CONFIGURATION_PATTERN = re.compile(
    r"^(?:(?:好的?|确认|可以|同意|麻烦)[，,]?)?(?:那就|就)?"
    r"(?:(?:我)?(?:用|使用|选择|选|改用|采用)|切换到|设为)?"
    rf"{MODEL_ROUTE_TARGET}(?:的)?[·./,+_—-]?{EFFORT_ROUTE_TARGET}"
    r"(?:档位|档)?(?:吧|了)?"
    r"(?=$|[。！!，,；;：:])"
)
EXPLICIT_CONFIGURATION_TOKEN_PATTERN = re.compile(
    rf"{MODEL_ROUTE_TARGET}(?:的)?[·./,+_—-]?{EFFORT_ROUTE_TARGET}(?:档位|档)?"
)

POLITE_ROUTE_PREFIX = r"(?:(?:请帮我|麻烦帮我|请|麻烦|现在|帮我|给我|我想|我要))?"
ROUTE_CLAUSE_START = r"(?:^|[。！!，,；;：:])"
ROUTE_CLAUSE_END = r"(?=$|[。！!，,；;：:])"

USER_ROUTE_REQUEST_PATTERNS = (
    re.compile(
        rf"{ROUTE_CLAUSE_START}{POLITE_ROUTE_PREFIX}"
        rf"(?:重选(?:一下|一次|一遍)?{MODEL_ROUTE_OBJECT}|"
        r"(?:重新|再次|再)(?:选|选择|推荐|评估)"
        rf"(?:一下|一次|一遍)?{MODEL_ROUTE_OBJECT})"
        rf"{ROUTE_CLAUSE_END}"
    ),
    re.compile(
        rf"{ROUTE_CLAUSE_START}{POLITE_ROUTE_PREFIX}"
        r"(?:重新|再次|再)(?:(?:给我)?(?:生成|做)?)?"
        r"(?:一张|一次|一遍)?(?:模型)?(?:路由卡|路由|选型)"
        rf"{ROUTE_CLAUSE_END}"
    ),
    re.compile(
        rf"{ROUTE_CLAUSE_START}{POLITE_ROUTE_PREFIX}"
        rf"(?:换|更换)(?:一个|个|一下)?{MODEL_ROUTE_OBJECT}"
        rf"{ROUTE_CLAUSE_END}"
    ),
    re.compile(
        rf"{ROUTE_CLAUSE_START}{POLITE_ROUTE_PREFIX}(?:把|将)?"
        rf"(?:当前|本任务|这个任务)?{MODEL_ROUTE_OBJECT}.{{0,8}}"
        rf"(?:换成|改成|改为|调整为|切换到){MODEL_ROUTE_TARGET}"
        rf"{ROUTE_CLAUSE_END}"
    ),
    re.compile(
        rf"{ROUTE_CLAUSE_START}{POLITE_ROUTE_PREFIX}(?:把|将)?"
        rf"(?:当前|本任务|这个任务)?(?:档位|推理档位).{{0,8}}"
        rf"(?:换成|改成|改为|调整为|调到|切换到){EFFORT_ROUTE_TARGET}"
        rf"{ROUTE_CLAUSE_END}"
    ),
    re.compile(
        r"(?:^|[。！!，,；;：:])(?:请)?(?:把)?"
        r"(?:(?:模型)?路由|路由策略|额度偏好|这次选型)?"
        r"(?:改成|改为|调整为|切换为)"
        r"(?:(?:优先)?(?:节省|省)(?:一点|一些)?额度|额度优先)"
        r"(?=$|[。！!，,；;：:])"
    ),
    re.compile(
        r"(?:^|[。！!，,；;：:])(?:请)?(?:把)?"
        r"(?:(?:模型)?路由|路由策略|额度偏好|这次选型)?"
        r"(?:改成|改为|调整为|切换为)"
        r"(?:(?:优先)?(?:保证|保障|保)质量|质量优先)"
        r"(?=$|[。！!，,；;：:])"
    ),
    re.compile(
        r"(?:^|[。！!，,；;：:])(?:请)?(?:把)?"
        r"(?:(?:模型)?路由|路由策略|额度偏好|这次选型)?"
        r"(?:改成|改为|调整为|切换为|改回)"
        r"(?:按推荐执行|平衡|均衡)"
        r"(?=$|[。！!，,；;：:])"
    ),
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
    "不是",
    "并非",
    "不想",
    "没有",
    "无意",
)
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
    transcript_path = raw.get("transcript_path")
    if isinstance(transcript_path, str):
        result["transcript_path"] = transcript_path
    return result


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_authoritative_automation_transcript(
    transcript_path: Optional[str],
    session_id: str,
) -> bool:
    """Verify automation provenance from the first transcript record only.

    A caller-controlled path is never enough: the resolved regular file must
    live below Codex's active or archived transcript roots, use the expected
    rollout filename, be owned by the current user, and start with matching
    ``session_meta``. Invalid or unreadable input simply receives no exemption
    so the ordinary routing policy remains in force.
    """

    if not transcript_path or not transcript_path.strip():
        return False
    try:
        candidate = Path(transcript_path).expanduser()
        resolved_path = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False

    resolved_roots: list[Path] = []
    for root in (_codex_home() / "sessions", _codex_home() / "archived_sessions"):
        try:
            resolved_roots.append(root.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    if not any(_path_is_within(resolved_path, root) for root in resolved_roots):
        return False

    expected_suffix = f"-{session_id}.jsonl"
    if (
        not resolved_path.name.startswith("rollout-")
        or not resolved_path.name.endswith(expected_suffix)
    ):
        return False
    try:
        metadata = resolved_path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        return False

    try:
        with resolved_path.open("rb") as handle:
            first_line = handle.readline(MAX_SESSION_META_BYTES + 1)
        if not first_line or len(first_line) > MAX_SESSION_META_BYTES:
            return False
        record = json.loads(first_line.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return False
    session_meta = record.get("payload")
    if not isinstance(session_meta, dict):
        return False
    if session_meta.get("id") != session_id:
        return False
    recorded_session_id = session_meta.get("session_id")
    if recorded_session_id is not None and recorded_session_id != session_id:
        return False
    return session_meta.get("thread_source") == "automation"


def _read_active_automation_config(
    automation_id: str,
    expected_kind: str,
) -> Optional[dict[str, Any]]:
    if re.fullmatch(AUTOMATION_ID_TEXT, automation_id) is None:
        return None
    if automation_id in {".", ".."}:
        return None

    try:
        automation_root = (_codex_home() / "automations").resolve(strict=True)
        unresolved_path = automation_root / automation_id / "automation.toml"
        unresolved_metadata = unresolved_path.lstat()
        config_path = unresolved_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISREG(unresolved_metadata.st_mode):
        return None
    if not _path_is_within(config_path, automation_root):
        return None
    if config_path.parent.name != automation_id or config_path.parent.parent != automation_root:
        return None
    try:
        metadata = config_path.stat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        return None

    try:
        with config_path.open("rb") as handle:
            raw_config = handle.read(MAX_AUTOMATION_CONFIG_BYTES + 1)
        if not raw_config or len(raw_config) > MAX_AUTOMATION_CONFIG_BYTES:
            return None
        config_text = raw_config.decode("utf-8")
    except (OSError, UnicodeError):
        return None

    parsed: dict[str, Any] = {}
    for line in config_text.splitlines():
        string_match = AUTOMATION_CONFIG_STRING_LINE.fullmatch(line)
        if string_match:
            key = string_match.group("key")
            if key in parsed:
                return None
            try:
                value = json.loads(string_match.group("value"))
            except json.JSONDecodeError:
                return None
            if not isinstance(value, str):
                return None
            parsed[key] = value
            continue
        cwds_match = AUTOMATION_CONFIG_CWDS_LINE.fullmatch(line)
        if cwds_match:
            if "cwds" in parsed:
                return None
            try:
                cwds = json.loads(cwds_match.group("value"))
            except json.JSONDecodeError:
                return None
            if not isinstance(cwds, list) or not all(
                isinstance(item, str) and item for item in cwds
            ):
                return None
            parsed["cwds"] = cwds

    if parsed.get("id") != automation_id:
        return None
    if parsed.get("kind") != expected_kind or parsed.get("status") != "ACTIVE":
        return None
    return parsed


def _same_automation_instructions(envelope_text: str, configured_text: Any) -> bool:
    if not isinstance(configured_text, str):
        return False
    normalized_envelope = envelope_text.replace("\r\n", "\n").rstrip("\n")
    normalized_config = configured_text.replace("\r\n", "\n").rstrip("\n")
    return normalized_envelope == normalized_config


def _validated_cron_automation_envelope(
    prompt: str,
    *,
    cwd: str,
) -> bool:
    cron_match = CRON_ENVELOPE_PATTERN.fullmatch(prompt)
    if not cron_match:
        return False
    automation_id = cron_match.group("id")
    config = _read_active_automation_config(automation_id, "cron")
    if not config:
        return False
    if config.get("name") != cron_match.group("name"):
        return False
    if not _same_automation_instructions(
        cron_match.group("instructions"), config.get("prompt")
    ):
        return False
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        return False
    if not isinstance(config.get("reasoning_effort"), str) or not config[
        "reasoning_effort"
    ].strip():
        return False
    configured_cwds = config.get("cwds")
    if not isinstance(configured_cwds, list):
        return False
    normalized_cwd = os.path.normpath(os.path.expanduser(cwd))
    if normalized_cwd not in {
        os.path.normpath(os.path.expanduser(configured_cwd))
        for configured_cwd in configured_cwds
    }:
        return False
    return True


def _is_chat(text: str) -> bool:
    return _strip_terminal_punctuation(text) in CHAT_MESSAGES


def _is_simple_explanation(text: str) -> bool:
    if len(text) > 80:
        return False
    return text.startswith(EXPLANATION_PREFIXES)


def _match_is_negated(text: str, match: re.Match[str]) -> bool:
    prefix = text[: match.start()]
    clause_start = max(prefix.rfind(mark) for mark in "。！？!?，,；;：:\n")
    local_prefix = prefix[clause_start + 1 :]
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
    return _is_explicit_configuration(text)


def _decision_clause(text: str) -> str:
    return re.split(r"(?<=[。！!\n])", text, maxsplit=1)[0]


def _normalize_final_still_decision(decision_clause: str) -> str:
    return re.sub(
        r"^((?:(?:好的?|确认|可以|同意)[，,]?)?)(?:那我|我|那)?还是"
        r"(?=(?:按推荐执行|优先节省额度|优先保证质量|"
        r"(?:(?:用|使用|选择|选|改用|采用)|切换到|设为)?"
        rf"{MODEL_ROUTE_TARGET}))",
        r"\1",
        decision_clause,
        count=1,
    )


def _selection_counts(decision_clause: str) -> tuple[int, int]:
    choice_count = sum(
        decision_clause.count(choice) for choice in SELECTION_PHRASES
    )
    configuration_count = len(
        EXPLICIT_CONFIGURATION_TOKEN_PATTERN.findall(decision_clause)
    )
    return choice_count, configuration_count


def _is_explicit_configuration(text: str) -> bool:
    decision_clause = _normalize_final_still_decision(_decision_clause(text))
    if SELECTION_QUESTION_OR_ALTERNATIVE.search(decision_clause):
        return False
    choice_count, configuration_count = _selection_counts(decision_clause)
    if choice_count != 0 or configuration_count != 1:
        return False
    stripped = _strip_terminal_punctuation(decision_clause)
    return EXPLICIT_CONFIGURATION_PATTERN.match(stripped) is not None


def _is_initial_selection(text: str) -> bool:
    decision_clause = _normalize_final_still_decision(_decision_clause(text))
    if SELECTION_QUESTION_OR_ALTERNATIVE.search(decision_clause):
        return False
    choice_count, configuration_count = _selection_counts(decision_clause)
    if choice_count + configuration_count != 1:
        return False
    stripped = _strip_terminal_punctuation(decision_clause)
    return bool(
        INITIAL_SELECTION_PATTERN.match(stripped)
        or EXPLICIT_CONFIGURATION_PATTERN.match(stripped)
    )


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


def _automation_exempt(session: Optional[dict[str, Any]]) -> bool:
    return _session_flag(session, "automation_exempt", default=False)


def _decide(
    prompt: str,
    session: Optional[dict[str, Any]],
    *,
    automation_exempt: bool = False,
    current_automation_trigger: bool = False,
) -> tuple[str, str, Optional[str], bool]:
    text = _normalize_for_match(prompt)
    last_signature = session.get("last_observed_signature") if session else None
    prompt_shown = _route_prompt_shown(session)
    selection_observed = _route_selection_observed(session)

    if automation_exempt:
        if current_automation_trigger:
            return "skip", "scheduled_automation", last_signature, selection_observed
        replacement_pending = bool(
            prompt_shown
            and not selection_observed
            and last_signature == "routing:user_requested"
        )
        if replacement_pending:
            if _is_initial_selection(text):
                return "skip", "route_already_set", last_signature, True
            return "skip", "route_prompt_pending", last_signature, False
        if _is_user_route_request(text):
            return "inject", "user_requested", "routing:user_requested", False
        return (
            "skip",
            "automation_thread_followup",
            last_signature,
            selection_observed,
        )

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
    elif reason == "scheduled_automation":
        instruction = (
            "自动化来源已核验：routing-not-required；不生成模型路由卡、不等待确认，"
            "直接执行本次自动化；其他安全、权限及现实行动门禁照常。"
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
    if decision == "inject" or reason == "scheduled_automation":
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
    return _codex_home() / "model-routing-advisor" / "global-gate"


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
            previously_automation_exempt = _automation_exempt(session)
            transcript_automation = _is_authoritative_automation_transcript(
                payload.get("transcript_path"), payload["session_id"]
            )
            cron_envelope_valid = bool(
                transcript_automation
                and _validated_cron_automation_envelope(
                    prompt,
                    cwd=payload["cwd"],
                )
            )
            current_automation_trigger = bool(
                cron_envelope_valid and not previously_automation_exempt
            )
            automation_exempt = bool(
                previously_automation_exempt or transcript_automation
            )

            is_duplicate = bool(
                session
                and session.get("turn_id") == payload["turn_id"]
                and session.get("last_prompt_sha256") == prompt_sha256
            )
            if is_duplicate:
                decision = "skip"
                reason = (
                    "scheduled_automation"
                    if automation_exempt
                    and session.get("last_reason") == "scheduled_automation"
                    else "duplicate_hook_invocation"
                )
                signature = session.get("last_observed_signature")
            else:
                decision, reason, signature, selection_observed = _decide(
                    prompt,
                    session,
                    automation_exempt=automation_exempt,
                    current_automation_trigger=current_automation_trigger,
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
                "automation_exempt": automation_exempt,
                "last_seen_at": event_time,
            }
            # Do not evict old session records. Losing a record would make an
            # archived task look new and violate the one-card-per-task policy.

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
                "automation_exempt": automation_exempt,
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
