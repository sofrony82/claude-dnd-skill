"""
campaign.py — campaigns on disk, grouped by player, and which one is active.

A campaign directory holds only what *changes* during play: the party's sheets,
the world state, the log. The module pack stays where it is and is read in
place — it is identical for every table and can run to megabytes, so copying it
per chat would be waste with a consistency hazard attached.

    ~/.claude/dnd/users/<user_id>/
        active.json                 {"active": "<campaign_id>"} — what they play
        campaigns/<campaign_id>/
            party.json              who is at the table, its title
            state.md                current scene, quests, world state
            session-log.md          what happened, per session
            raw-log.md              verbatim transcript
            characters/*.md         one sheet per player character
        campaigns/.trash/<campaign_id>-<stamp>/
                                    deleted campaigns, kept until removed by hand

Everything a player owns lives under their own directory, so ownership is where
a campaign *is*, not a field to check; the helper scripts run with that
directory as their data root and cannot name anyone else's campaign; and
deleting a player's data is removing one directory. The bot serves private
chats only, where the chat id is the user id, so the two are used
interchangeably here.

Campaigns from the earlier flat layout (`campaigns/<id>/`, `chats/<id>.json`)
are moved into place by `migrate_flat_layout()` at startup.
"""

import json
import logging
import pathlib
import re
import shutil
import unicodedata
from datetime import date, datetime

import transcript
from config import (
    LEGACY_CAMPAIGNS_DIR,
    LEGACY_CHATS_DIR,
    MODULE_DIR,
    PREGENS,
    USERS_DIR,
)

log = logging.getLogger("campaign")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slug(name: str) -> str:
    out = []
    for ch in name.lower():
        out.append(_TRANSLIT.get(ch, ch))
    s = unicodedata.normalize("NFKD", "".join(out))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "pc"


# ── where a player's things live ─────────────────────────────────────────
def user_dir(user_id: int) -> pathlib.Path:
    return USERS_DIR / str(int(user_id))


def campaigns_root(user_id: int) -> pathlib.Path:
    return user_dir(user_id) / "campaigns"


def trash_dir(user_id: int) -> pathlib.Path:
    return campaigns_root(user_id) / ".trash"


def legacy_id(chat_id: int) -> str:
    """The fixed id the API server uses for its per-chat test campaign."""
    return f"tg-{chat_id}"


def valid_id(campaign_id: str) -> bool:
    """Ids arrive in callback data, so they are checked before touching disk."""
    return bool(campaign_id) and bool(_ID_RE.match(campaign_id))


def path(user_id: int, campaign_id: str) -> pathlib.Path:
    if not valid_id(campaign_id):
        raise ValueError(f"bad campaign id {campaign_id!r}")
    return campaigns_root(user_id) / campaign_id


def _is_campaign(user_id: int, campaign_id: str) -> bool:
    return (valid_id(campaign_id)
            and (campaigns_root(user_id) / campaign_id / "party.json").is_file())


def _pointer(user_id: int) -> pathlib.Path:
    return user_dir(user_id) / "active.json"


def active_id(user_id: int):
    """The campaign this player is playing, or None.

    A pointer to a campaign that has since been trashed resolves to None.
    """
    f = _pointer(user_id)
    if not f.is_file():
        return None
    try:
        cid = json.loads(f.read_text(encoding="utf-8")).get("active")
    except (ValueError, OSError):
        return None
    return cid if cid and _is_campaign(user_id, cid) else None


def set_active(user_id: int, campaign_id) -> None:
    """Point the player at a campaign; None leaves nothing active."""
    user_dir(user_id).mkdir(parents=True, exist_ok=True)
    _pointer(user_id).write_text(json.dumps({"active": campaign_id}), encoding="utf-8")


def campaign_dir(user_id: int) -> pathlib.Path:
    """Directory of the player's active campaign.

    With nothing active this is still a path — one that does not exist — so
    callers that only log or test for existence need no branch.
    """
    return campaigns_root(user_id) / (active_id(user_id) or legacy_id(user_id))


def exists(user_id: int) -> bool:
    return active_id(user_id) is not None


def read_party(user_id: int, campaign_id: str):
    if not valid_id(campaign_id):
        return None
    f = campaigns_root(user_id) / campaign_id / "party.json"
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def load_party(user_id: int):
    cid = active_id(user_id)
    return read_party(user_id, cid) if cid else None


def pregen_meta(pregen_id: str):
    for pid, klass, race, emoji in PREGENS:
        if pid == pregen_id:
            return {"id": pid, "klass": klass, "race": race, "emoji": emoji}
    return None


def available_pregens():
    """Pregens the module pack actually shipped, in book order."""
    out = []
    for pid, klass, race, emoji in PREGENS:
        if (MODULE_DIR / "pregens" / f"{pid}.md").is_file():
            out.append({"id": pid, "klass": klass, "race": race, "emoji": emoji})
    return out


def new_id(user_id: int, party: list) -> str:
    """A readable, unused id: `<date>-<first character>`, suffixed on a clash."""
    name = slug(party[0]["name"])[:24].strip("-") if party else ""
    base = f"{date.today():%Y%m%d}-{name or 'party'}"
    cid, n = base, 2
    while (campaigns_root(user_id) / cid).exists():
        cid, n = f"{base}-{n}", n + 1
    return cid


def create(user_id: int, party: list, campaign_id=None) -> pathlib.Path:
    """Create a campaign for the player and make it their active one.

    `party` is a list of {id, name, klass, race} chosen during onboarding.
    With no `campaign_id` a fresh one is minted and nothing existing is
    touched. With an explicit id — the API's fixed per-chat namespace — an
    existing campaign of that id is replaced, which is what a test reset wants.
    Returns the campaign directory.
    """
    cid = campaign_id or new_id(user_id, party)
    cdir = path(user_id, cid)
    if cdir.exists():
        shutil.rmtree(cdir)
    (cdir / "characters").mkdir(parents=True)

    records = []
    for pc in party:
        s = slug(pc["name"])
        src = MODULE_DIR / "pregens" / f"{pc['id']}.md"
        sheet = src.read_text(encoding="utf-8") if src.is_file() else f"# {pc['name']}\n"
        # The pack ships sheets with the name left for the player to fill.
        sheet = sheet.replace("<имя задаёт игрок>", pc["name"])
        sheet = re.sub(r"^# .*$", f"# {pc['name']}", sheet, count=1, flags=re.M)
        (cdir / "characters" / f"{s}.md").write_text(sheet, encoding="utf-8")
        records.append({"id": pc["id"], "name": pc["name"], "klass": pc["klass"],
                        "race": pc["race"], "slug": s})

    (cdir / "party.json").write_text(
        json.dumps({"owner": user_id,
                    "title": ", ".join(r["name"] for r in records),
                    "created": date.today().isoformat(),
                    "module": MODULE_DIR.name, "party": records},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    seed = MODULE_DIR / "state-seed.md"
    state = seed.read_text(encoding="utf-8") if seed.is_file() else ""
    # The seed opens with a note to whoever builds packs ("a snapshot for a NEW
    # playthrough, the boat has just left…"). Left in, it sits above the live
    # sections for the whole campaign and tells a resuming DM the game has not
    # started. Keep only the sections.
    first = re.search(r"^## ", state, flags=re.M)
    if first:
        state = state[first.start():]
    party_lines = "\n".join(
        f"  - {r['name']} — {r['race']} {r['klass']} 1 ур." for r in records)
    header = (
        f"# Кампания: Драконы острова Штормокрушений\n"
        f"**Создана:** {date.today().isoformat()}  **Сессий:** 0  **Правила:** 2014\n\n"
        f"## Отряд\n{party_lines}\n\n"
    )
    (cdir / "state.md").write_text(header + state, encoding="utf-8")
    (cdir / "session-log.md").write_text(
        f"# Журнал кампании\n\n*Сессия 1 начата {date.today().isoformat()}.*\n",
        encoding="utf-8")
    # A fresh campaign is fully saved: nothing in the log is newer than state.
    transcript.mark_saved(cdir)
    set_active(user_id, cid)
    return cdir


# ── several campaigns per player ─────────────────────────────────────────
def _location(cdir: pathlib.Path) -> str:
    f = cdir / "state.md"
    if not f.is_file():
        return ""
    m = re.search(r"^\s*-\s*\*\*Location:\*\*\s*(.+)$",
                  f.read_text(encoding="utf-8"), flags=re.M)
    return m.group(1).strip() if m else ""


def summary(user_id: int, campaign_id: str):
    """What a campaign list shows: title, party, where they are, last played."""
    party = read_party(user_id, campaign_id)
    if not party:
        return None
    cdir = campaigns_root(user_id) / campaign_id
    # Play touches the log and the state; party.json only changes on a rename,
    # which is not playing. It stands in only for a campaign never played.
    touched = [f.stat().st_mtime for f in (cdir / "raw-log.md", cdir / "state.md")
               if f.is_file()]
    if not touched and (cdir / "party.json").is_file():
        touched = [(cdir / "party.json").stat().st_mtime]
    return {
        "id": campaign_id,
        "title": party.get("title") or ", ".join(p["name"] for p in party["party"]),
        "party": party["party"],
        "created": party.get("created", ""),
        "location": _location(cdir),
        "last_played": datetime.fromtimestamp(max(touched)) if touched else None,
    }


def list_for(user_id: int) -> list:
    """The player's campaigns, most recently played first."""
    root = campaigns_root(user_id)
    if not root.is_dir():
        return []
    out = [summary(user_id, d.name) for d in root.iterdir()
           if not d.name.startswith(".") and _is_campaign(user_id, d.name)]
    out.sort(key=lambda s: s["last_played"] or datetime.min, reverse=True)
    return out


def rename(user_id: int, campaign_id: str, title: str) -> None:
    f = path(user_id, campaign_id) / "party.json"
    party = json.loads(f.read_text(encoding="utf-8"))
    party["title"] = title
    f.write_text(json.dumps(party, ensure_ascii=False, indent=2), encoding="utf-8")


def trash(user_id: int, campaign_id: str):
    """Move a campaign to the player's `.trash/`. Returns where it went, or None.

    Nothing is erased: restoring is a `mv` back into campaigns/. A pointer at
    it simply resolves to no active campaign.
    """
    src = path(user_id, campaign_id)
    if not src.is_dir():
        return None
    trash_dir(user_id).mkdir(parents=True, exist_ok=True)
    dst = trash_dir(user_id) / f"{campaign_id}-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.move(str(src), str(dst))
    return dst


# ── the flat layout this replaced ────────────────────────────────────────
def _flat_owner(cdir: pathlib.Path):
    """Who a campaign from the flat layout belongs to, or None if unknowable.

    party.json's `owner` if set; else the chat it was played in, which in a
    private chat is the user; else the `tg-<chat_id>` directory name.
    """
    with_party = cdir / "party.json"
    if with_party.is_file():
        try:
            party = json.loads(with_party.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            party = {}
        for key in ("owner", "chat_id"):
            if isinstance(party.get(key), int):
                return party[key]
    m = re.match(r"^tg-(-?\d+)(?:-\d{8}-\d{6})?$", cdir.name)
    return int(m.group(1)) if m else None


def _move(src: pathlib.Path, dst: pathlib.Path, moved: list) -> None:
    if dst.exists():
        log.warning("migrate: %s already exists, left %s where it is", dst, src)
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append((src, dst))
    except (OSError, shutil.Error) as e:
        # Another process (bot and API server start together) may have got
        # there first; the next start retries whatever is left.
        log.warning("migrate: could not move %s: %s", src, e)


def migrate_flat_layout() -> list:
    """Move flat-layout campaigns, trash and pointers under users/<owner>/.

    Idempotent and safe to run on every start: once the old directories are
    empty it does nothing. A campaign whose owner cannot be told is left where
    it is and logged. Returns the (src, dst) pairs it moved.
    """
    moved = []
    old = LEGACY_CAMPAIGNS_DIR
    if old.is_dir():
        trash = old / ".trash"
        for d in sorted(trash.iterdir()) if trash.is_dir() else []:
            owner = _flat_owner(d)
            if owner is None:
                log.warning("migrate: owner of trashed %s unknown, left in place", d)
                continue
            _move(d, trash_dir(owner) / d.name, moved)
        for d in sorted(old.iterdir()):
            if d.name.startswith(".") or not d.is_dir():
                continue
            owner = _flat_owner(d)
            if owner is None:
                log.warning("migrate: owner of %s unknown, left in place", d)
                continue
            _move(d, campaigns_root(owner) / d.name, moved)
    if LEGACY_CHATS_DIR.is_dir():
        for f in sorted(LEGACY_CHATS_DIR.glob("*.json")):
            if re.fullmatch(r"-?\d+", f.stem):
                _move(f, _pointer(int(f.stem)), moved)
    for d in (old / ".trash", old, LEGACY_CHATS_DIR):
        try:
            d.rmdir()               # only if empty — anything left stays visible
        except OSError:
            pass
    for src, dst in moved:
        log.info("migrate: %s -> %s", src, dst)
    return moved


def pack_ready() -> tuple:
    """(ok, missing) — is the module pack complete enough to start a game?"""
    required = ["world.md", "npcs.md", "arc.md", "source/1.1.md"]
    missing = [r for r in required if not (MODULE_DIR / r).is_file()]
    if not (MODULE_DIR / "pregens").is_dir() or not available_pregens():
        missing.append("pregens/*.md")
    return (not missing), missing
