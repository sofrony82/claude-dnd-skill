#!/usr/bin/env python3
"""
module_check.py — validate a module pack before anyone plays it.

The checks exist because each corresponds to a way a pack fails at the table:

  missing file          the DM asks for something that is not there, mid-scene
  oversized load-time   every session pays context for what it does not need
  NPC in index only     the DM voices a character with no motivation or secret
  dangling source_ref   a chapter the arc points at and cannot open
  unassigned pages      adventure text that exists but is unreachable
  orphan map legend     a map described to players that cannot be shown
  placeholder left in   "<name>" reaches a player as canon

Exit 0 = clean (warnings allowed), 1 = problems found.

Usage:
  python3 module_check.py --pack ~/.claude/dnd/modules/<id>/
  python3 module_check.py --pack <pack> --json
"""

import argparse
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shared  # noqa: E402

REQUIRED = ["world.md", "npcs.md", "arc.md", "source-index.md"]
RECOMMENDED = ["npcs-full.md", "bestiary.md", "items.md", "state-seed.md", "README.md"]

# Read at every session start, so their size is a permanent per-session tax.
# Together these cap the load at roughly 11.5k words — about 17k tokens for a
# language like Russian, which is a reasonable standing cost for the hub of a
# full adventure and still leaves the session almost all of its context. The
# per-file split reflects what each is for: world.md carries setting and
# backstory, arc.md the whole act/chapter tree, npcs.md only an index row per
# character (~20 NPCs × a one-line summary).
LOAD_TIME_BUDGET_WORDS = {"world.md": 5000, "npcs.md": 2500, "arc.md": 4000}

# One chapter is read whole on demand; past this it crowds out everything else.
LARGE_CHAPTER_WORDS = 12000

PLACEHOLDER = re.compile(r"<(?:name|имя|title|название|faction|npc|tbd|todo)[^>\n]{0,30}>", re.I)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


class Report:
    def __init__(self):
        self.errors, self.warnings, self.info = [], [], []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def note(self, msg):
        self.info.append(msg)

    @property
    def ok(self):
        return not self.errors


def _words(p: pathlib.Path) -> int:
    return _shared.word_count(p.read_text(encoding="utf-8", errors="replace"))


def _table_names(text: str) -> set:
    """First column of every markdown table row that is not a header/divider."""
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        first = re.sub(r"[*_`\[\]]", "", cells[0]).strip()
        if first.lower() in ("имя", "name", "существо", "creature", "предмет",
                             "item", "глава", "chapter", "карта", "map"):
            continue
        if first:
            names.add(first)
    return names


def check(pack: pathlib.Path) -> Report:
    r = Report()
    if not pack.is_dir():
        r.error(f"pack directory not found: {pack}")
        return r

    # ── presence ─────────────────────────────────────────────────────────
    for name in REQUIRED:
        f = pack / name
        if not f.is_file():
            r.error(f"missing required file: {name}")
        elif f.stat().st_size == 0:
            r.error(f"required file is empty: {name}")
    for name in RECOMMENDED:
        if not (pack / name).is_file():
            r.warn(f"recommended file absent: {name}")

    # ── load-time budget ─────────────────────────────────────────────────
    for name, budget in LOAD_TIME_BUDGET_WORDS.items():
        f = pack / name
        if not f.is_file():
            continue
        n = _words(f)
        if n > budget:
            r.error(
                f"{name} is {n:,} words, over the {budget:,}-word load-time budget. "
                f"This file is read at every session start — move detail into a "
                f"lazily-read file (npcs-full.md, world-nodes.md, source/)."
            )
        else:
            r.note(f"{name}: {n:,}/{budget:,} words")

    # ── npcs index vs full entries ───────────────────────────────────────
    idx, full = pack / "npcs.md", pack / "npcs-full.md"
    if idx.is_file():
        itext = idx.read_text(encoding="utf-8", errors="replace")
        names = _table_names(itext)
        if not names:
            r.warn("npcs.md has no parsable index table — the DM loads this as its NPC roster")
        body_lines = [ln for ln in itext.splitlines()
                      if ln.strip() and not ln.strip().startswith(("|", "#", "*", ">"))]
        if len(body_lines) > 40:
            r.warn(f"npcs.md carries {len(body_lines)} lines of prose outside the table — "
                   "it is meant to be an index; full entries belong in npcs-full.md")
        if full.is_file():
            ftext = full.read_text(encoding="utf-8", errors="replace")
            absent = sorted(n for n in names if n and n not in ftext)
            if absent:
                r.error("in npcs.md index but missing from npcs-full.md: " +
                        ", ".join(absent[:12]) + (" …" if len(absent) > 12 else ""))
            else:
                r.note(f"npcs: {len(names)} indexed, all present in npcs-full.md")

    # ── arc -> source refs ───────────────────────────────────────────────
    arc = pack / "arc.md"
    refs = set()
    if arc.is_file():
        atext = arc.read_text(encoding="utf-8", errors="replace")
        refs = set(re.findall(r"source/([A-Za-z0-9][\w.\-]*)\.md", atext))
        for cid in sorted(refs):
            if not (pack / "source" / f"{cid}.md").is_file():
                r.error(f"arc.md references source/{cid}.md, which does not exist")
        if not refs:
            r.warn("arc.md contains no source_ref entries — the DM cannot find chapter text")

    # ── source index vs files ────────────────────────────────────────────
    sdir = pack / "source"
    if sdir.is_dir():
        on_disk = {p.stem for p in sdir.glob("*.md")}
        si = pack / "source-index.md"
        indexed = set()
        if si.is_file():
            indexed = set(re.findall(r"source/([A-Za-z0-9][\w.\-]*)\.md",
                                     si.read_text(encoding="utf-8", errors="replace")))
        for cid in sorted(indexed - on_disk):
            r.error(f"source-index.md lists source/{cid}.md, which does not exist")
        for cid in sorted(on_disk - indexed):
            r.warn(f"source/{cid}.md is not listed in source-index.md")
        for p in sorted(sdir.glob("*.md")):
            n = _words(p)
            if n > LARGE_CHAPTER_WORDS:
                r.warn(f"source/{p.name} is {n:,} words — split it at a scene break "
                       f"(over {LARGE_CHAPTER_WORDS:,} bloats the load that reads it)")
            if n == 0:
                r.warn(f"source/{p.name} is empty — its pages carried no text layer")
    else:
        r.error("no source/ directory — the pack has no module text to read")

    # ── maps: image and legend must pair up ──────────────────────────────
    mdir = pack / "maps"
    if mdir.is_dir():
        images, legends = {}, {}
        for p in mdir.iterdir():
            m = re.match(r"map-(\d+)", p.stem)
            if not m:
                continue
            if p.suffix.lower() in IMAGE_EXT:
                images[m.group(1)] = p.name
            elif p.suffix.lower() == ".md":
                legends[m.group(1)] = p.name
        for n in sorted(set(images) - set(legends)):
            r.warn(f"map {n} has an image ({images[n]}) but no legend map-{n}.md — "
                   "the DM has nothing to describe from")
        for n in sorted(set(legends) - set(images)):
            r.error(f"map {n} has a legend ({legends[n]}) but no image — "
                    "players cannot be shown it")
        if images:
            r.note(f"maps: {len(images)} image(s), {len(legends)} legend(s)")
    else:
        r.warn("no maps/ directory")

    # ── pregens ──────────────────────────────────────────────────────────
    pdir = pack / "pregens"
    if pdir.is_dir():
        sheets = sorted(pdir.glob("*.md"))
        if not sheets:
            r.warn("pregens/ is empty — a table has no characters to start with")
        for p in sheets:
            if _words(p) < 120:
                r.warn(f"pregens/{p.name} looks like a stub ({_words(p)} words)")
        if sheets:
            r.note(f"pregens: {len(sheets)} sheet(s)")

    # ── coverage ─────────────────────────────────────────────────────────
    bm = pack / "build-manifest.json"
    if bm.is_file():
        try:
            data = json.loads(bm.read_text(encoding="utf-8"))
            cov = data.get("coverage", {})
            missing = cov.get("pages_missing") or []
            dup = cov.get("pages_duplicated") or {}
            if missing:
                r.error(f"{len(missing)} source page(s) landed in no chapter: {missing[:20]}")
            if dup:
                r.warn(f"page(s) in more than one chapter: {dup}")
            if not missing and not dup and cov:
                r.note(f"coverage: {cov.get('pages_assigned')}/{cov.get('pages_extracted')} "
                       f"pages, {cov.get('total_words', 0):,} words")
        except json.JSONDecodeError as e:
            r.error(f"build-manifest.json is not valid JSON: {e}")

    # ── text hygiene ─────────────────────────────────────────────────────
    for p in sorted(pack.rglob("*.md")):
        if "build" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            r.error(f"{p.relative_to(pack)} is not valid UTF-8")
            continue
        if "�" in text:
            r.error(f"{p.relative_to(pack)} contains U+FFFD replacement characters "
                    "— the source was decoded with the wrong encoding")
        # A name placeholder on a pregenerated sheet is the design: the player
        # supplies the name at the table and the runner substitutes it.
        if p.parent.name == "pregens":
            continue
        hits = PLACEHOLDER.findall(text)
        if hits:
            r.warn(f"{p.relative_to(pack)} still has template placeholders: "
                   + ", ".join(sorted(set(hits))[:5]))

    return r


def main():
    ap = argparse.ArgumentParser(description="Validate a prepared module pack.")
    ap.add_argument("--pack", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    pack = pathlib.Path(args.pack).expanduser().resolve()
    r = check(pack)

    if args.json:
        print(json.dumps({"pack": str(pack), "ok": r.ok, "errors": r.errors,
                          "warnings": r.warnings, "info": r.info},
                         ensure_ascii=False, indent=2))
    else:
        print(f"Pack: {pack}")
        for msg in r.info:
            print(f"  ·  {msg}")
        for msg in r.warnings:
            print(f"  ⚠  {msg}")
        for msg in r.errors:
            print(f"  ✗  {msg}")
        print()
        if r.ok:
            print(f"OK — pack is playable ({len(r.warnings)} warning(s))")
        else:
            print(f"{len(r.errors)} problem(s) must be fixed before play")

    sys.exit(0 if r.ok else 1)


if __name__ == "__main__":
    main()
