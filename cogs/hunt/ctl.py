"""File inbox for local turkey-hunt test controls.

The CLI writes one JSON command. The running cog claims it on the next tick.
Nothing here opens a socket, an HTTP server, or Discord, and nothing here
reads ``config.json`` or the bot token.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cogs.candyhunt import SUPPORT_TEST_CHANNEL_ID
from cogs.utils.constants import DATA_DIR

log = logging.getLogger(__name__)

# Reused from candyhunt so the support test channel is not a second magic number.
DEFAULT_TEST_CHANNEL_ID = SUPPORT_TEST_CHANNEL_ID

CTL_DIR_NAME = "turkey_hunt_ctl"
INBOX_NAME = "inbox"
PROCESSING_NAME = "processing"
RESULTS_NAME = "results"
MAX_COMMANDS_PER_TICK = 8
MAX_COMMAND_BYTES = 8192
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
ACTIONS = frozenset(
    {
        "testdrop",
        "test_session_start",
        "test_session_stop",
        "status",
        "schedule",
    }
)
_CHANNEL_ACTIONS = frozenset({"testdrop", "test_session_start"})
_BARE_ACTIONS = frozenset({"test_session_stop", "status", "schedule"})
_ALLOWED_KEYS = frozenset({"id", "action", "channel_id", "minutes", "interval"})
_SNOWFLAKE_MAX = 2**63 - 1

REJECT_TEXT = {
    "malformed": "Ignored a control file that was not valid JSON.",
    "schema": "Ignored a control file that does not match the command schema.",
    "unknown_action": "Ignored a control file with an unknown action.",
    "id_mismatch": "Ignored a control file whose id does not match its name.",
    "too_large": "Ignored a control file that is too large.",
    "rejected": "Ignored a control file.",
}


class ResultError(Exception):
    """The result file was present but not a readable command result."""


@dataclass(frozen=True)
class ControlCommand:
    request_id: str
    action: str
    channel_id: int | None = None
    minutes: int | None = None
    interval: int | None = None


@dataclass(frozen=True)
class ClaimedFile:
    path: Path
    request_id: str | None
    command: ControlCommand | None
    error: str | None
    raw_action: str = ""


def default_ctl_root() -> Path:
    return DATA_DIR / CTL_DIR_NAME


def inbox_dir(root: Path) -> Path:
    return Path(root) / INBOX_NAME


def processing_dir(root: Path) -> Path:
    return Path(root) / PROCESSING_NAME


def results_dir(root: Path) -> Path:
    return Path(root) / RESULTS_NAME


def new_request_id() -> str:
    return uuid.uuid4().hex


def inbox_file_is_local(inbox: Path, candidate: Path) -> bool:
    """True only for a real file whose resolved parent is the inbox itself."""
    try:
        root = inbox.resolve()
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved.is_file() and resolved.parent == root


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > _SNOWFLAKE_MAX:
        return None
    return value


def _id_from_filename(name: str) -> str | None:
    if not name.endswith(".json"):
        return None
    stem = name[:-5]
    if REQUEST_ID_RE.fullmatch(stem):
        return stem
    return None


def _present(payload: dict, key: str) -> bool:
    return key in payload and payload[key] is not None


def parse_command(payload: object, *, expected_id: str) -> tuple[ControlCommand | None, str | None]:
    """Return a command, or ``(None, error code)`` when the body is rejected."""
    if not isinstance(payload, dict):
        return None, "schema"
    if not set(payload).issubset(_ALLOWED_KEYS):
        return None, "schema"
    if payload.get("id") != expected_id:
        return None, "id_mismatch"
    action = payload.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        return None, "unknown_action" if isinstance(action, str) else "schema"
    if action in _BARE_ACTIONS and any(_present(payload, key) for key in ("channel_id", "minutes", "interval")):
        return None, "schema"
    if action == "testdrop" and any(_present(payload, key) for key in ("minutes", "interval")):
        return None, "schema"
    channel_id = None
    if _present(payload, "channel_id"):
        if action not in _CHANNEL_ACTIONS:
            return None, "schema"
        channel_id = _positive_int(payload.get("channel_id"))
        if channel_id is None:
            return None, "schema"
    minutes = None
    interval = None
    if action == "test_session_start":
        if _present(payload, "minutes"):
            minutes = _positive_int(payload.get("minutes"))
            if minutes is None:
                return None, "schema"
        if _present(payload, "interval"):
            interval = _positive_int(payload.get("interval"))
            if interval is None:
                return None, "schema"
    return ControlCommand(expected_id, action, channel_id, minutes, interval), None


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON via a temp file in the same directory, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _result_path(root: Path, request_id: str) -> Path:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("bad request id")
    directory = results_dir(root)
    path = directory / f"{request_id}.json"
    # The id cannot contain a separator, so the join stays inside results.
    if path.parent != directory:
        raise ValueError("path escapes results")
    return path


def result_exists(root: Path, request_id: str) -> bool:
    try:
        path = _result_path(root, request_id)
    except ValueError:
        return False
    return path.is_file()


def write_result(root: Path, payload: dict) -> Path:
    request_id = payload.get("id")
    if not isinstance(request_id, str):
        raise ValueError("bad request id")
    path = _result_path(root, request_id)
    body = {
        "ok": bool(payload.get("ok")),
        "action": payload.get("action") if isinstance(payload.get("action"), str) else "",
        "id": request_id,
        "text": payload.get("text") if isinstance(payload.get("text"), str) else "",
        "channel_id": payload.get("channel_id") if _positive_int(payload.get("channel_id")) else None,
        "error": payload.get("error") if isinstance(payload.get("error"), str) else None,
    }
    atomic_write_json(path, body)
    return path


def read_result(root: Path, request_id: str) -> dict | None:
    path = _result_path(root, request_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("ok") not in (True, False) or not isinstance(data.get("text"), str):
        return None
    if data.get("id") != request_id:
        return None
    return data


def wait_for_result(root: Path, request_id: str, timeout: float, *, poll: float = 0.2) -> dict | None:
    """Return the result dict, or None on timeout. Raises ResultError if unreadable."""
    deadline = time.monotonic() + max(0.0, timeout)
    pause = poll if timeout >= 1 else min(poll, 0.05)
    while True:
        path = _result_path(root, request_id)
        if path.is_file():
            data = read_result(root, request_id)
            if data is None:
                raise ResultError(request_id)
            return data
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(pause, remaining))


def write_command(root: Path, payload: dict) -> Path:
    """Atomically write a validated command into the inbox."""
    request_id = payload.get("id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("bad request id")
    command, error = parse_command(payload, expected_id=request_id)
    if command is None:
        raise ValueError(error or "schema")
    path = inbox_dir(root) / f"{request_id}.json"
    if path.parent != inbox_dir(root):
        raise ValueError("path escapes inbox")
    atomic_write_json(path, payload)
    return path


def discard_claimed(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("turkey ctl could not remove claimed file name=%s", path.name)


def _raw_action(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    action = payload.get("action")
    if isinstance(action, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", action):
        return action
    return ""


def _read_claimed(path: Path, request_id: str | None) -> tuple[ControlCommand | None, str | None, str]:
    try:
        size = path.stat().st_size
    except OSError:
        return None, "rejected", ""
    if size > MAX_COMMAND_BYTES:
        return None, "too_large", ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "malformed", ""
    raw_action = _raw_action(payload)
    if request_id is None:
        return None, "schema", raw_action
    command, error = parse_command(payload, expected_id=request_id)
    if command is None:
        return None, error or "schema", raw_action
    return command, None, command.action


def claim_pending(root: Path, *, limit: int = MAX_COMMANDS_PER_TICK) -> list[ClaimedFile]:
    """Move up to ``limit`` inbox files into processing and parse them.

    Symlinks, names that escape the inbox, and non-files are logged and skipped.
    A failure here is logged by the caller; this function itself does not raise
    for a bad file.
    """
    inbox = inbox_dir(root)
    if not inbox.is_dir():
        return []
    try:
        entries = sorted(inbox.iterdir(), key=lambda item: item.name)
    except OSError:
        log.exception("turkey ctl inbox failed")
        return []
    claimed: list[ClaimedFile] = []
    touched = 0
    for entry in entries:
        if touched >= limit:
            break
        try:
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                log.warning("turkey ctl ignored symlink name=%s", entry.name)
                entry.unlink(missing_ok=True)
                touched += 1
                continue
            if not entry.is_file() or entry.suffix != ".json":
                continue
            touched += 1
            if not inbox_file_is_local(inbox, entry):
                log.warning("turkey ctl ignored path outside inbox name=%s", entry.name)
                continue
            request_id = _id_from_filename(entry.name)
            destination_dir = processing_dir(root)
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / entry.name
            if destination.exists() and not destination.is_symlink():
                destination.unlink()
            entry.replace(destination)
            command, error, raw_action = _read_claimed(destination, request_id)
            claimed.append(ClaimedFile(destination, request_id, command, error, raw_action))
        except Exception:
            log.exception("turkey ctl skipped a file name=%s", getattr(entry, "name", "?"))
    return claimed
