"""
access.py — who may play: admins from .env, everyone else by the owner's leave.

`TELEGRAM_ALLOWED_USERS` in .env lists the admins: they always play, and they
decide on everybody else. A stranger who writes to the bot gets a "request
access" button; pressing it records a request here and pings the admins, who
approve or refuse with a button. Decisions land in one JSON file and are read
on every check, so letting someone in or out needs no restart:

    ~/.claude/dnd/access.json
        {"allowed": {"<user_id>": {"name", "username", "at"}},
         "pending": {...},
         "denied":  {...}}

A user is in at most one of the three. Refused and revoked users go to
`denied`, which keeps them from re-requesting until an admin lets them in.

With no admins configured the bot is open to anyone, as before, and none of
this is consulted.
"""

import json
import logging
import os
from datetime import datetime

from config import ALLOWED_USERS, DATA_ROOT

log = logging.getLogger("access")

ACCESS_FILE = DATA_ROOT / "access.json"
ADMINS = ALLOWED_USERS
SECTIONS = ("allowed", "pending", "denied")


def load() -> dict:
    try:
        data = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (ValueError, OSError) as e:
        # A broken file must not open the bot to everyone, nor lock out the
        # people already in without a word: log loudly, treat as empty.
        log.error("could not read %s: %s", ACCESS_FILE, e)
        data = {}
    return {k: dict(data.get(k) or {}) for k in SECTIONS}


def _save(data: dict) -> None:
    """Write through a temp file, so a crash mid-write leaves the old file."""
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCESS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ACCESS_FILE)


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def is_allowed(user_id: int) -> bool:
    if not ADMINS:
        return True
    return is_admin(user_id) or str(user_id) in load()["allowed"]


def status(user_id: int) -> str:
    """admin | allowed | pending | denied | none"""
    if is_admin(user_id):
        return "admin"
    data = load()
    for k in SECTIONS:
        if str(user_id) in data[k]:
            return k
    return "none"


def request(user_id: int, name: str, username: str) -> bool:
    """Record a request. True only if it is new — the admins are pinged once."""
    data = load()
    key = str(user_id)
    if any(key in data[k] for k in SECTIONS):
        return False
    data["pending"][key] = {"name": name, "username": username,
                            "at": datetime.now().isoformat(timespec="seconds")}
    _save(data)
    log.info("access requested by %s (%s @%s)", user_id, name, username)
    return True


def _move(user_id: int, to: str):
    """Move a user into `to` from wherever they are. Returns their record."""
    data = load()
    key = str(user_id)
    record = None
    for k in SECTIONS:
        record = data[k].pop(key, None) or record
    if record is None:
        record = {"name": "", "username": ""}
    record["at"] = datetime.now().isoformat(timespec="seconds")
    data[to][key] = record
    _save(data)
    return record


def approve(user_id: int) -> dict:
    log.info("access granted to %s", user_id)
    return _move(user_id, "allowed")


def refuse(user_id: int) -> dict:
    """Refuse a request or revoke access; either way they stop getting in."""
    log.info("access refused/revoked for %s", user_id)
    return _move(user_id, "denied")


def listing() -> dict:
    """The three sections, each as [(user_id, record)], newest first."""
    data = load()
    return {k: sorted(((int(uid), rec) for uid, rec in data[k].items()),
                      key=lambda x: x[1].get("at", ""), reverse=True)
            for k in SECTIONS}


def label(user_id: int, record: dict) -> str:
    """How a user is shown to an admin: name, @username, id."""
    parts = [record.get("name") or "без имени"]
    if record.get("username"):
        parts.append(f"@{record['username']}")
    parts.append(str(user_id))
    return " · ".join(parts)
