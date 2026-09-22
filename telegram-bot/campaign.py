"""
campaign.py — per-chat campaign state on disk.

A campaign directory holds only what *changes* during play: the party's sheets,
the world state, the log. The module pack stays where it is and is read in
place — it is identical for every table and can run to megabytes, so copying it
per chat would be waste with a consistency hazard attached.

    ~/.claude/dnd/campaigns/tg-<chat_id>/
        party.json          who is at the table (the bot's own record)
        state.md            current scene, quests, world state
        session-log.md      what happened, per session
        characters/*.md     one sheet per player character
"""

import json
import pathlib
import re
import shutil
import unicodedata
from datetime import date

from config import CAMPAIGNS_DIR, MODULE_DIR, PREGENS

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


def campaign_dir(chat_id: int) -> pathlib.Path:
    return CAMPAIGNS_DIR / f"tg-{chat_id}"


def exists(chat_id: int) -> bool:
    return (campaign_dir(chat_id) / "party.json").is_file()


def load_party(chat_id: int):
    f = campaign_dir(chat_id) / "party.json"
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


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


def create(chat_id: int, party: list) -> pathlib.Path:
    """Create (or reset) a campaign directory for this chat.

    `party` is a list of {id, name, klass, race} chosen during onboarding.
    Returns the campaign directory.
    """
    cdir = campaign_dir(chat_id)
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
        json.dumps({"chat_id": chat_id, "created": date.today().isoformat(),
                    "module": MODULE_DIR.name, "party": records},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    seed = MODULE_DIR / "state-seed.md"
    state = seed.read_text(encoding="utf-8") if seed.is_file() else ""
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
    return cdir


def delete(chat_id: int) -> bool:
    cdir = campaign_dir(chat_id)
    if cdir.exists():
        shutil.rmtree(cdir)
        return True
    return False


def pack_ready() -> tuple:
    """(ok, missing) — is the module pack complete enough to start a game?"""
    required = ["world.md", "npcs.md", "arc.md", "source/1.1.md"]
    missing = [r for r in required if not (MODULE_DIR / r).is_file()]
    if not (MODULE_DIR / "pregens").is_dir() or not available_pregens():
        missing.append("pregens/*.md")
    return (not missing), missing
