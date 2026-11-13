"""Local turkey hunt controls for the machine the bot runs on.

Writes one JSON command into the turkey hunt inbox and waits for the cog to
write a result file. This script does not read config.json, does not use the
bot token, and does not connect to Discord.

Run from the repo root. ``--timeout`` may appear anywhere on the command line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cogs.candyhunt import SUPPORT_TEST_CHANNEL_ID
from cogs.hunt.ctl import (
    ResultError,
    default_ctl_root,
    new_request_id,
    wait_for_result,
    write_command,
)

DEFAULT_TIMEOUT = 60.0
DEFAULT_MINUTES = 15
DEFAULT_INTERVAL = 2


def _extract_global(argv: list[str]) -> tuple[list[str], float, Path | None]:
    """Pull ``--timeout`` and ``--root`` out so they work before or after the action."""
    timeout = DEFAULT_TIMEOUT
    root: Path | None = None
    cleaned: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--timeout" or token.startswith("--timeout="):
            if token.startswith("--timeout="):
                raw = token.split("=", 1)[1]
            else:
                index += 1
                if index >= len(argv):
                    raise ValueError("timeout")
                raw = argv[index]
            timeout = float(raw)
            index += 1
            continue
        if token == "--root" or token.startswith("--root="):
            if token.startswith("--root="):
                raw_root = token.split("=", 1)[1]
            else:
                index += 1
                if index >= len(argv):
                    raise ValueError("root")
                raw_root = argv[index]
            root = Path(raw_root)
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned, timeout, root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turkeyhunt_ctl.py",
        description="Ask the running turkey hunt cog to run a local test action.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    testdrop = sub.add_parser("testdrop", help="Post one test turkey.")
    testdrop.add_argument("--channel", type=int, default=None)

    session = sub.add_parser("test_session", help="Start or stop a test session.")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    start = session_sub.add_parser("start", help="Start a test session and post the first turkey.")
    start.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    start.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    start.add_argument("--channel", type=int, default=None)
    session_sub.add_parser("stop", help="Stop the test session if one is running.")

    sub.add_parser("status", help="Write the hunt status into the result file.")
    sub.add_parser("schedule", help="Preview a schedule without saving it.")
    return parser


def command_from_args(args: argparse.Namespace, request_id: str | None = None) -> dict:
    """Build the command JSON. An omitted channel becomes the support test channel."""
    request_id = request_id or new_request_id()
    if args.command == "testdrop":
        return {
            "id": request_id,
            "action": "testdrop",
            "channel_id": SUPPORT_TEST_CHANNEL_ID if args.channel is None else args.channel,
        }
    if args.command == "test_session" and args.session_command == "start":
        return {
            "id": request_id,
            "action": "test_session_start",
            "channel_id": SUPPORT_TEST_CHANNEL_ID if args.channel is None else args.channel,
            "minutes": args.minutes,
            "interval": args.interval,
        }
    if args.command == "test_session" and args.session_command == "stop":
        return {"id": request_id, "action": "test_session_stop"}
    if args.command == "status":
        return {"id": request_id, "action": "status"}
    if args.command == "schedule":
        return {"id": request_id, "action": "schedule"}
    raise ValueError("unknown command")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        cleaned, timeout, root = _extract_global(raw)
    except ValueError:
        print("Could not read --timeout or --root.", file=sys.stderr)
        return 2
    if not (0 < timeout <= 3600):
        print("Timeout must be a positive number of seconds up to 3600.", file=sys.stderr)
        return 2
    args = build_parser().parse_args(cleaned)
    try:
        payload = command_from_args(args)
    except ValueError:
        print("Could not build that control command.", file=sys.stderr)
        return 2
    numbers = [payload.get("channel_id"), payload.get("minutes"), payload.get("interval")]
    if any(isinstance(value, int) and value <= 0 for value in numbers):
        print("Channel, minutes, and interval must be positive.", file=sys.stderr)
        return 2
    ctl_root = default_ctl_root() if root is None else root
    try:
        write_command(ctl_root, payload)
    except ValueError:
        print("Could not write that control command.", file=sys.stderr)
        return 2
    except OSError:
        print("Could not write the control command file.", file=sys.stderr)
        return 2
    try:
        result = wait_for_result(ctl_root, payload["id"], timeout)
    except ResultError:
        print("The turkey hunt cog wrote a result that could not be read.")
        return 1
    if result is None:
        print("Timed out waiting for the turkey hunt cog.")
        return 1
    print(result["text"])
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
