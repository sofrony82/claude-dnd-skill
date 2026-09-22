#!/usr/bin/env python3
"""
_shared.py — common helpers for the module-prep pipeline.

The pipeline turns a published adventure (PDF/DOCX/MD) into a *module pack*: a
pre-parsed bundle of markdown + images that a DM agent reads directly, so no
agent ever has to parse the source document at play time.

Everything here is deterministic. Semantic work (where a chapter starts, who is
an NPC, what the arc is) belongs to the model, not to these scripts.
"""

import hashlib
import importlib.util
import os
import pathlib
import re
import sys
import unicodedata

# Every script in this pipeline prints extracted source text, which is rarely
# ASCII. Force UTF-8 once here so a cp1251/GBK console never kills a run.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8")
    except Exception:
        pass


# ── Locating the sibling dnd skill ───────────────────────────────────────
# Both skills ship in the same plugin, so the dnd skill sits beside this one:
#   <plugin>/skills/module-prep/scripts/_shared.py   (this file)
#   <plugin>/skills/dnd/scripts/import_campaign.py   (the code we reuse)
# parents[2] is <plugin>/skills/ in both the repo checkout and a plugin install.

def dnd_scripts_dir() -> pathlib.Path:
    """Return the dnd skill's scripts dir, or raise with a clear message."""
    here = pathlib.Path(__file__).resolve()
    candidates = [
        here.parents[2] / "dnd" / "scripts",           # plugin / repo layout
        pathlib.Path("~/.claude/skills/dnd/scripts").expanduser(),  # legacy install
    ]
    env = os.environ.get("DND_SKILL_DIR", "").strip()
    if env:
        candidates.insert(0, pathlib.Path(env).expanduser() / "scripts")
    for c in candidates:
        if (c / "import_campaign.py").is_file():
            return c
    raise RuntimeError(
        "Cannot locate the dnd skill's scripts/ directory (looked for "
        "import_campaign.py in: " + ", ".join(str(c) for c in candidates) + "). "
        "Set DND_SKILL_DIR to the dnd skill directory."
    )


def load_module(name: str, path: pathlib.Path):
    """Import a python file by explicit path (no package assumptions)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def order_blocks():
    """Return the dnd skill's column-aware block sorter.

    Reused rather than reimplemented: it is the one piece of extraction logic
    the two skills must agree on, or `/dm:dnd import` and a prepared pack would
    disagree about what the source text even says.
    """
    # Loaded strictly by path. Putting the dnd scripts directory on sys.path
    # would shadow the standard library for the rest of the process — it holds
    # a `calendar.py`, and stdlib `http.cookiejar` does `from calendar import
    # timegm`, so anything importing HTTP afterwards dies with a confusing
    # ImportError far from here. import_campaign.py needs only stdlib itself,
    # so path insertion buys nothing.
    d = dnd_scripts_dir()
    return load_module("import_campaign", d / "import_campaign.py").order_blocks


# ── Data root ────────────────────────────────────────────────────────────

def modules_root() -> pathlib.Path:
    """Where built packs live. Mirrors the dnd skill's DND_CAMPAIGN_ROOT."""
    raw = os.environ.get("DND_CAMPAIGN_ROOT", "").strip()
    root = pathlib.Path(raw).expanduser() if raw else pathlib.Path("~/.claude/dnd").expanduser()
    return (root / "modules").resolve()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ── Script (writing-system) classification ───────────────────────────────
# Ghostscript-produced PDFs often render decorative drop caps through a symbol
# font, so the text layer contains codepoints from an unrelated block (this
# repo's test module emits Thai for its chapter-opening capitals). Those runs
# are noise: they must be detected so extraction can drop them instead of
# pushing gibberish into the corpus the DM reads at the table.

_RANGES = (
    ("latin",      ((0x0041, 0x024F),)),
    ("cyrillic",   ((0x0400, 0x04FF), (0x0500, 0x052F))),
    ("greek",      ((0x0370, 0x03FF),)),
    ("hebrew",     ((0x0590, 0x05FF),)),
    ("arabic",     ((0x0600, 0x06FF),)),
    ("devanagari", ((0x0900, 0x097F),)),
    ("thai",       ((0x0E00, 0x0E7F),)),
    ("cjk",        ((0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF))),
)


def char_script(ch: str) -> str:
    """Coarse writing-system label for one character.

    Combining marks count as script-bearing: a symbol-font ornament often
    decomposes to bare combining marks, which carry no `isalpha` flag but are
    exactly the noise this classification exists to catch.
    """
    cp = ord(ch)
    if ch.isspace():
        return "space"
    if not (ch.isalpha() or unicodedata.category(ch).startswith("M")):
        return "other"
    for name, ranges in _RANGES:
        for lo, hi in ranges:
            if lo <= cp <= hi:
                return name
    return "unknown"


def script_profile(text: str) -> dict:
    """Count letters per writing system. Keys are labels from char_script."""
    counts = {}
    for ch in text:
        s = char_script(ch)
        if s in ("space", "other"):
            continue
        counts[s] = counts.get(s, 0) + 1
    return counts


def dominant_script(text: str) -> str:
    prof = script_profile(text)
    return max(prof, key=prof.get) if prof else "none"


def garble_ratio(text: str, expected: str) -> float:
    """Fraction of letters that are NOT in the expected writing system.

    Latin is always tolerated alongside any script: published modules keep
    English proper nouns, stat abbreviations and page furniture in Latin even
    in translation.
    """
    prof = script_profile(text)
    total = sum(prof.values())
    if not total:
        return 0.0
    ok = prof.get(expected, 0) + (prof.get("latin", 0) if expected != "latin" else 0)
    return 1.0 - (ok / total)


def is_garbled(text: str, expected: str, threshold: float = 0.5, min_letters: int = 3) -> bool:
    """True when a span is mostly foreign-script noise (decorative glyph runs)."""
    prof = script_profile(text)
    if sum(prof.values()) < min_letters:
        return False
    return garble_ratio(text, expected) >= threshold


def noise_scripts(text: str, threshold: float = 0.01) -> set:
    """Writing systems too rare in this document to be real content.

    A translated module is one script plus Latin. Anything else that shows up
    in a fraction of a percent of characters is a symbol font bleeding into the
    text layer — including single ornament glyphs fused onto a real word, which
    no token-level check can catch ("Caves)" plus a stray Thai mark reads as
    mostly-Latin and survives).
    """
    prof = script_profile(text)
    total = sum(prof.values())
    if not total:
        return set()
    return {s for s, c in prof.items() if s != "latin" and c / total < threshold}


def strip_scripts(text: str, drop: set) -> str:
    """Delete every character belonging to one of the given writing systems."""
    if not drop:
        return text
    return "".join(ch for ch in text if char_script(ch) not in drop)


# ── Text tidying ─────────────────────────────────────────────────────────

_SOFT_HYPHEN = "­"
_NBSP = " "


def normalise(text: str) -> str:
    """Collapse PDF extraction artefacts without touching meaningful content."""
    text = text.replace(_SOFT_HYPHEN, "").replace(_NBSP, " ")
    text = text.replace("﻿", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(text.split())


def slugify(text: str, maxlen: int = 60) -> str:
    """ASCII-safe slug. Transliterates Cyrillic so filenames stay portable."""
    text = translit(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:maxlen].strip("-") or "untitled"


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit(text: str) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT:
            rep = _TRANSLIT[low]
            out.append(rep.upper() if ch.isupper() and rep else rep)
        else:
            out.append(ch)
    return "".join(out)
