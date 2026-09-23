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
         "denied":  {...},
         "invited": {"<username>": {"at"}}}

A user is in at most one of the three. Refused and revoked users go to
`denied`, which keeps them from re-requesting until an admin lets them in.

An admin can also invite by @username ahead of time. The Bot API cannot turn
a username into an id, so the invitation waits until that person first
writes, then becomes an ordinary `allowed` entry under their id. The bot
never writes first — Telegram does not let it — so nobody is messaged.

With no admins configured the bot is open to anyone, as before, and none of
this is consulted.
"""

import json
import logging
import os
import re
from datetime import datetime

from config import ALLOWED_USERS, DATA_ROOT

log = logging.getLogger("access")

ACCESS_FILE = DATA_ROOT / "access.json"
ADMINS = ALLOWED_USERS
SECTIONS = ("allowed", "pending", "denied")
# Telegram's rule for a username: 5–32 letters, digits, underscores.
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")


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
    return {k: dict(data.get(k) or {}) for k in SECTIONS + ("invited",)}


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


def username_key(username: str) -> str:
    """"@DanUZh1k" -> "danuzh1k": usernames are case-insensitive."""
    return (username or "").strip().lstrip("@").lower()


def invite(usernames) -> dict:
    """Invite @usernames. Returns {"added", "playing", "invalid"} lists.

    A name that already belongs to an allowed player is left out — there is
    nothing to wait for.
    """
    data = load()
    playing = {username_key(r.get("username")) for r in data["allowed"].values()}
    out = {"added": [], "playing": [], "invalid": []}
    for raw in usernames:
        key = username_key(raw)
        if not USERNAME_RE.fullmatch(key):
            out["invalid"].append(raw)
        elif key in playing:
            out["playing"].append(key)
        elif key not in data["invited"]:
            data["invited"][key] = {"at": datetime.now().isoformat(timespec="seconds")}
            out["added"].append(key)
    if out["added"]:
        _save(data)
        log.info("invited %s", ", ".join(out["added"]))
    return out


def uninvite(username: str) -> bool:
    data = load()
    if data["invited"].pop(username_key(username), None) is None:
        return False
    _save(data)
    log.info("invitation for %s withdrawn", username_key(username))
    return True


def redeem(user_id: int, name: str, username: str) -> bool:
    """Let in an invited @username on first contact. True if that happened.

    The invitation is used up: from here on they are an `allowed` id like
    anyone approved by button, and revoking them sticks.
    """
    key = username_key(username)
    if not key:
        return False
    data = load()
    if data["invited"].pop(key, None) is None:
        return False
    for k in SECTIONS:
        data[k].pop(str(user_id), None)
    data["allowed"][str(user_id)] = {"name": name, "username": username,
                                     "at": datetime.now().isoformat(timespec="seconds")}
    _save(data)
    log.info("access granted to %s (%s @%s) by invitation", user_id, name, username)
    return True


def invitations() -> list:
    """[(username, record)], newest first."""
    return sorted(load()["invited"].items(), key=lambda x: x[1].get("at", ""),
                  reverse=True)


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
