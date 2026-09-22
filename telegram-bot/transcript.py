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
"""

import logging
import pathlib

log = logging.getLogger("transcript")

FILENAME = "raw-log.md"


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
