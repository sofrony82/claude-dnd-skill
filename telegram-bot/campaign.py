"""
campaign.py — campaigns on disk, and which one each chat is playing.

A campaign directory holds only what *changes* during play: the party's sheets,
the world state, the log. The module pack stays where it is and is read in
place — it is identical for every table and can run to megabytes, so copying it
per chat would be waste with a consistency hazard attached.

    ~/.claude/dnd/campaigns/<campaign_id>/
        party.json          who is at the table, who owns it, its title
        state.md            current scene, quests, world state
        session-log.md      what happened, per session
        raw-log.md          verbatim transcript
        characters/*.md     one sheet per player character
    ~/.claude/dnd/campaigns/.trash/<campaign_id>-<stamp>/
                            deleted campaigns, kept until removed by hand
    ~/.claude/dnd/chats/<chat_id>.json
                            {"active": "<campaign_id>"} — what this chat plays

A player can keep several campaigns and switch between them; the chat only
points at one. Campaigns made before this layout are named `tg-<chat_id>` and
have no pointer or owner: a chat with no pointer falls back to its `tg-` dir,
and in a private chat the chat id *is* the user id, so ownership is inferred
from the `chat_id` recorded in party.json.
"""

import json
import pathlib
import re
import shutil
import unicodedata
from datetime import date, datetime

import transcript
from config import CAMPAIGNS_DIR, CHATS_DIR, MODULE_DIR, PREGENS

TRASH_DIR = CAMPAIGNS_DIR / ".trash"
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


# ── ids, paths, the per-chat pointer ─────────────────────────────────────
def legacy_id(chat_id: int) -> str:
    return f"tg-{chat_id}"


def valid_id(campaign_id: str) -> bool:
    """Ids arrive in callback data, so they are checked before touching disk."""
    return bool(campaign_id) and bool(_ID_RE.match(campaign_id))


def path(campaign_id: str) -> pathlib.Path:
    if not valid_id(campaign_id):
        raise ValueError(f"bad campaign id {campaign_id!r}")
    return CAMPAIGNS_DIR / campaign_id


def _is_campaign(campaign_id: str) -> bool:
    return valid_id(campaign_id) and (CAMPAIGNS_DIR / campaign_id / "party.json").is_file()


def _pointer(chat_id: int) -> pathlib.Path:
    return CHATS_DIR / f"{chat_id}.json"


def active_id(chat_id: int):
    """The campaign this chat is playing, or None.

    An explicit pointer wins. With none — a chat from before pointers existed —
    the chat's own `tg-<chat_id>` campaign is the active one if it is there.
    A pointer to a campaign that has since been trashed resolves to None.
    """
    f = _pointer(chat_id)
    if f.is_file():
        try:
            cid = json.loads(f.read_text(encoding="utf-8")).get("active")
        except (ValueError, OSError):
            cid = None
        return cid if cid and _is_campaign(cid) else None
    cid = legacy_id(chat_id)
    return cid if _is_campaign(cid) else None


def set_active(chat_id: int, campaign_id) -> None:
    """Point the chat at a campaign; None leaves it with nothing active."""
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    _pointer(chat_id).write_text(json.dumps({"active": campaign_id}), encoding="utf-8")


def campaign_dir(chat_id: int) -> pathlib.Path:
    """Directory of the chat's active campaign.

    With nothing active this is still a path — the legacy one, which does not
    exist — so callers that only log or test for existence need no branch.
    """
    return CAMPAIGNS_DIR / (active_id(chat_id) or legacy_id(chat_id))


def exists(chat_id: int) -> bool:
    return active_id(chat_id) is not None


def read_party(campaign_id: str):
    f = CAMPAIGNS_DIR / campaign_id / "party.json"
    if not valid_id(campaign_id) or not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def load_party(chat_id: int):
    cid = active_id(chat_id)
    return read_party(cid) if cid else None


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


def new_id(party: list) -> str:
    """A readable, unused id: `<date>-<first character>`, suffixed on a clash."""
    name = slug(party[0]["name"])[:24].strip("-") if party else ""
    base = f"{date.today():%Y%m%d}-{name or 'party'}"
    cid, n = base, 2
    while (CAMPAIGNS_DIR / cid).exists():
        cid, n = f"{base}-{n}", n + 1
    return cid


def create(chat_id: int, party: list, owner=None, campaign_id=None) -> pathlib.Path:
    """Create a campaign and make it the chat's active one.

    `party` is a list of {id, name, klass, race} chosen during onboarding.
    With no `campaign_id` a fresh one is minted and nothing existing is
    touched. With an explicit id — the API's fixed per-chat namespace — an
    existing campaign of that id is replaced, which is what a test reset wants.
    Returns the campaign directory.
    """
    cid = campaign_id or new_id(party)
    cdir = path(cid)
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
        json.dumps({"chat_id": chat_id, "owner": owner,
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
    set_active(chat_id, cid)
    return cdir


# ── several campaigns per player ─────────────────────────────────────────
def owned_by(campaign_id: str, party: dict, user_id: int) -> bool:
    owner = party.get("owner")
    if owner is not None:
        return owner == user_id
    # Pre-ownership campaign: it recorded the chat it was played in, and in a
    # private chat that is the user. Trust the record over the directory name —
    # a campaign restored from a backup may sit under another `tg-` name.
    if party.get("chat_id") is not None:
        return party["chat_id"] == user_id
    return campaign_id == legacy_id(user_id)


def _location(cdir: pathlib.Path) -> str:
    f = cdir / "state.md"
    if not f.is_file():
        return ""
    m = re.search(r"^\s*-\s*\*\*Location:\*\*\s*(.+)$",
                  f.read_text(encoding="utf-8"), flags=re.M)
    return m.group(1).strip() if m else ""


def summary(campaign_id: str):
    """What a campaign list shows: title, party, where they are, last played."""
    party = read_party(campaign_id)
    if not party:
        return None
    cdir = CAMPAIGNS_DIR / campaign_id
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
    """The user's campaigns, most recently played first."""
    out = []
    if not CAMPAIGNS_DIR.is_dir():
        return out
    for d in CAMPAIGNS_DIR.iterdir():
        if d.name.startswith(".") or not _is_campaign(d.name):
            continue
        party = read_party(d.name)
        if party and owned_by(d.name, party, user_id):
            out.append(summary(d.name))
    out.sort(key=lambda s: s["last_played"] or datetime.min, reverse=True)
    return out


def rename(campaign_id: str, title: str) -> None:
    f = path(campaign_id) / "party.json"
    party = json.loads(f.read_text(encoding="utf-8"))
    party["title"] = title
    f.write_text(json.dumps(party, ensure_ascii=False, indent=2), encoding="utf-8")


def trash(campaign_id: str):
    """Move a campaign to `.trash/`. Returns where it went, or None.

    Nothing is erased: restoring is a `mv` back into campaigns/. Chats pointing
    at it simply resolve to no active campaign.
    """
    src = path(campaign_id)
    if not src.is_dir():
        return None
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    dst = TRASH_DIR / f"{campaign_id}-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.move(str(src), str(dst))
    return dst


def pack_ready() -> tuple:
    """(ok, missing) — is the module pack complete enough to start a game?"""
    required = ["world.md", "npcs.md", "arc.md", "source/1.1.md"]
    missing = [r for r in required if not (MODULE_DIR / r).is_file()]
    if not (MODULE_DIR / "pregens").is_dir() or not available_pregens():
        missing.append("pregens/*.md")
    return (not missing), missing
