"""
usage.py — the bot's daily budget of Chat Completions requests, shared by all.

Every completion the DeepSeek loop makes is one request to the provider, and a
single player turn can make up to DS_MAX_STEPS of them. The count lives in a
file, not in a session, so it survives restarts and idle-session closes, and
the bot and the API server (two processes) add to the same total:

    ~/.claude/dnd/usage.json
        {"days": {"2026-09-22": {"completions": 812,
                                 "prompt_tokens": ..., "completion_tokens": ...,
                                 "users": {"<user_id>": 640, ...}}}}

Two checks keep the day under DAILY_COMPLETIONS:
  * `can_start_turn` — a new turn starts only if a whole turn's worth
    (TURN_RESERVE) still fits, so turns are not cut off halfway as a rule;
  * `check` — before every single request, a hard stop at the limit. It only
    fires when several turns running at once eat the reserve together.

The day turns over at midnight in USAGE_TZ (Moscow by default). Only requests
that got a response are counted; the client's own retries of a failed request
are not visible here, which TURN_RESERVE's margin absorbs.
"""

import contextlib
import fcntl
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from config import DAILY_COMPLETIONS, DATA_ROOT, DS_MAX_STEPS, USAGE_TZ

log = logging.getLogger("usage")

USAGE_FILE = DATA_ROOT / "usage.json"
LOCK_FILE = DATA_ROOT / "usage.lock"
KEEP_DAYS = 60
# Requests one turn may need: its step ceiling, nudges and continuations included.
TURN_RESERVE = DS_MAX_STEPS


class LimitReached(Exception):
    """The day's request budget is spent; no more completions until midnight."""


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(USAGE_TZ)
    except Exception:                               # noqa: BLE001 — no tzdata
        return timezone(timedelta(hours=3))


def today() -> str:
    return datetime.now(_tz()).date().isoformat()


def _load() -> dict:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"days": {}}
    except (ValueError, OSError) as e:
        log.error("could not read %s: %s", USAGE_FILE, e)
        return {"days": {}}
    data.setdefault("days", {})
    return data


@contextlib.contextmanager
def _locked():
    """Serialise read-modify-write across the bot and the API server."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def used_today() -> int:
    return int(_load()["days"].get(today(), {}).get("completions", 0))


def remaining() -> int | None:
    """Requests left today, or None if there is no limit."""
    if DAILY_COMPLETIONS <= 0:
        return None
    return max(0, DAILY_COMPLETIONS - used_today())


def can_start_turn() -> bool:
    left = remaining()
    return left is None or left >= TURN_RESERVE


def check() -> None:
    """Raise LimitReached if not even one more request fits today."""
    left = remaining()
    if left is not None and left <= 0:
        raise LimitReached()


def record(user_id: int, prompt_tokens: int = 0, completion_tokens: int = 0) -> int:
    """Count one completion for `user_id`. Returns today's total."""
    with _locked():
        data = _load()
        day = data["days"].setdefault(today(), {
            "completions": 0, "prompt_tokens": 0, "completion_tokens": 0, "users": {}})
        before = day["completions"]
        day["completions"] = before + 1
        day["prompt_tokens"] += int(prompt_tokens or 0)
        day["completion_tokens"] += int(completion_tokens or 0)
        key = str(user_id)
        day["users"][key] = day["users"].get(key, 0) + 1
        for old in sorted(data["days"])[:-KEEP_DAYS]:
            del data["days"][old]
        tmp = USAGE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, USAGE_FILE)
    total = day["completions"]
    if DAILY_COMPLETIONS > 0:
        for share in (0.8, 1.0):
            mark = int(DAILY_COMPLETIONS * share)
            if before < mark <= total:
                log.warning("daily request budget %d%% used: %d of %d",
                            int(share * 100), total, DAILY_COMPLETIONS)
    return total


def days(n: int = 7) -> list:
    """The last `n` days that have any usage, newest first: [(date, day)]."""
    data = _load()["days"]
    return [(d, data[d]) for d in sorted(data, reverse=True)[:n]]
