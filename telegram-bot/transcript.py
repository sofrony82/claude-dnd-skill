"""
transcript.py — append-only record of everything said at the table.

The campaign files hold *state*: where the party is, what the sheet says. They
deliberately do not hold the play itself — state.md is rewritten each save, so
the prose is overwritten as the game moves on. This file is the other half: the
verbatim transcript, never rewritten, only appended.

Format matches what a person would paste out of Telegram, so a hand-saved log
and a bot-written one are the same document:

    > DnD Master:
    <narration>

    > Sofrony:
    <what the player typed>

Failures here never interrupt play. A game that stops because its logger could
not write is worse than a game with a gap in its log.

The log is also what makes a lost DM session cheap. The DM saves state.md when
it judges a scene over, so a restart or a campaign switch can land between
saves. `.saved.json` records how far into the log the last save reached; on
resume, `pending()` hands the DM everything after that point.
"""

import json
import logging
import os
import pathlib
import re

log = logging.getLogger("transcript")

FILENAME = "raw-log.md"
CHECKPOINT = ".saved.json"
STATE = "state.md"

# Speaker name the bot writes for the DM. Everyone else is a player.
BOT_SPEAKER = "DnD Master"

# Cap on the log tail handed to a resuming DM, in characters. Whole entries are
# kept from the end; a dozen exchanges fit, which is more than the DM usually
# goes between saves.
TAIL_CHARS = 12000

# Without a save point, how much newer the log may be than state.md and still
# count as saved. The DM writes state.md mid-turn and the bot logs its narration
# after the turn ends, so on a turn that saved, the log is always a little newer.
UNMARKED_SLACK = 300

_ENTRY = re.compile(r"^> ([^\n]*):\n", flags=re.M)


def path_for(campaign_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(campaign_dir) / FILENAME


def append(campaign_dir, speaker: str, text: str) -> None:
    """Append one utterance. Never raises."""
    if not text or not text.strip():
        return
    try:
        p = path_for(campaign_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"> {speaker}:\n{text.strip()}\n\n")
    except Exception as e:  # disk full, permissions, races — log and play on
        log.warning("could not append to transcript: %s", e)


# ── save point ───────────────────────────────────────────────────────────
def state_mtime(campaign_dir):
    f = pathlib.Path(campaign_dir) / STATE
    try:
        return f.stat().st_mtime
    except OSError:
        return None


def mark_saved(campaign_dir) -> None:
    """Record that state.md now covers the log up to its current end."""
    cdir = pathlib.Path(campaign_dir)
    try:
        size = path_for(cdir).stat().st_size if path_for(cdir).is_file() else 0
        tmp = cdir / (CHECKPOINT + ".tmp")
        tmp.write_text(json.dumps({"raw_log_bytes": size}), encoding="utf-8")
        os.replace(tmp, cdir / CHECKPOINT)
    except Exception as e:
        log.warning("could not record save point: %s", e)


def entries(text: str) -> list:
    """[(speaker, body)] in log order."""
    heads = list(_ENTRY.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m.group(1), text[m.end():end].strip()))
    return out


def _is_play(speaker: str, body: str) -> bool:
    """A player turn that moves the game — not a /sheet or /map lookup."""
    return speaker != BOT_SPEAKER and not body.startswith("/")


def pending(campaign_dir, limit: int = TAIL_CHARS):
    """What was played after the last save, or None if nothing was.

    Returns {"text", "player_turns", "exact", "truncated"}. `exact` is False
    for a campaign that predates save points: then the tail is a guess taken
    from the end of the log and may repeat what state.md already holds.
    """
    cdir = pathlib.Path(campaign_dir)
    logf = path_for(cdir)
    if not logf.is_file():
        return None
    raw = logf.read_bytes()

    offset, exact = None, True
    try:
        offset = json.loads((cdir / CHECKPOINT).read_text(encoding="utf-8"))["raw_log_bytes"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not isinstance(offset, int) or not 0 <= offset <= len(raw):
        # No usable save point. If state.md was written around the time of the
        # last entry, that turn saved; otherwise take the end of the log.
        saved_at = state_mtime(cdir)
        if saved_at is not None and saved_at >= logf.stat().st_mtime - UNMARKED_SLACK:
            return None
        offset, exact = 0, False

    items = entries(raw[offset:].decode("utf-8", errors="replace"))
    turns = sum(1 for sp, body in items if _is_play(sp, body))
    if not turns:
        return None

    kept, size = [], 0
    for sp, body in reversed(items):
        block = f"> {sp}:\n{body}\n"
        if kept and size + len(block) > limit:
            break
        kept.append(block)
        size += len(block)
    kept.reverse()
    return {"text": "\n".join(kept).strip(), "player_turns": turns,
            "exact": exact, "truncated": len(kept) < len(items)}


def has_unsaved(campaign_dir) -> bool:
    return pending(campaign_dir) is not None
