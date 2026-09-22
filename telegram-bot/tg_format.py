"""
tg_format.py — turn the DM's prose into Telegram messages.

Two jobs: pull out the map markers the DM emits, and split long narration on
paragraph boundaries so a scene never arrives cut mid-sentence.
"""

import re

from config import CHUNK_TARGET, TELEGRAM_LIMIT

MAP_MARKER = re.compile(r"^\s*\[\[map:(\d+)\]\]\s*$", re.M)
_INLINE_MARKER = re.compile(r"\[\[map:(\d+)\]\]")


def extract_maps(text: str):
    """Return (clean_text, [map_numbers]) — markers removed, order preserved."""
    found = []
    for m in _INLINE_MARKER.finditer(text):
        n = int(m.group(1))
        if n not in found:
            found.append(n)
    clean = _INLINE_MARKER.sub("", text)
    # Removing a marker that sat on its own line leaves a triple newline,
    # which renders as a visible gap in Telegram.
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip(), found


def to_html(text: str) -> str:
    """Escape HTML, then re-apply the only two styles the DM is allowed.

    Telegram's HTML parse mode is used rather than Markdown because the DM
    writes Russian prose full of underscores, asterisks and quotation marks that
    Markdown would either swallow or choke on.
    """
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    # **bold** / *bold* -> <b>, _italic_ -> <i>. Non-greedy, single-line only,
    # so an unmatched marker cannot swallow the rest of the message.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+?)_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)
    return text


def chunk(text: str, limit: int = CHUNK_TARGET):
    """Split into Telegram-sized pieces, preferring paragraph then sentence breaks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    out, buf = [], ""
    for para in text.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            out.append(buf)
            buf = ""
        # A single paragraph over the limit: fall back to sentence boundaries.
        if len(para) <= limit:
            buf = para
            continue
        sentences = re.split(r"(?<=[.!?…])\s+", para)
        for s in sentences:
            cand = f"{buf} {s}".strip()
            if len(cand) <= limit:
                buf = cand
            else:
                if buf:
                    out.append(buf)
                # Still too long (no sentence breaks at all) — hard split.
                while len(s) > TELEGRAM_LIMIT:
                    out.append(s[:TELEGRAM_LIMIT])
                    s = s[TELEGRAM_LIMIT:]
                buf = s
    if buf:
        out.append(buf)
    return out
