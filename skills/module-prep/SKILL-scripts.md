# Module Prep — Scripts Reference

All scripts live in `${CLAUDE_SKILL_DIR}/scripts/`. They require **PyMuPDF**
(`pip3 install pymupdf`). They reuse the `dnd` skill's column sorter, so the two
skills can never disagree about what the source text says.

> Substitute the absolute skill-dir path for `${CLAUDE_SKILL_DIR}` before running.

---

## `pdf_probe.py` — survey before parsing

```bash
python3 pdf_probe.py <source.pdf>                    # human report
python3 pdf_probe.py <source.pdf> --json             # machine-readable
python3 pdf_probe.py <source.pdf> --headings         # heading candidates only
python3 pdf_probe.py <source.pdf> --out probe.json   # also write JSON
```

Reports: page count, metadata, TOC, writing-system profile, text-layer density,
column verdict, body font size, heading candidates, figures with captions,
recurring page furniture, decorative glyph runs, image-only pages, warnings.

**How it tells a figure from page furniture** — three independent filters,
because no single one suffices:

| Filter | Constant | Catches |
|---|---|---|
| Byte-identical across many pages | `RECURRING_PAGE_SHARE = 0.15` | Backgrounds, borders, stat-block frames — even re-embedded under a fresh xref per page |
| Too small or too light | `FIGURE_MIN_PIXELS`, `FIGURE_MIN_BYTES` | Icons, rules, flat vector frames |
| Placed larger than the page | `MAX_AREA_SHARE = 1.2` | Tiled or clipped background texture |

**Heading detection** is font-size based (`HEADING_SIZE_RATIO = 1.15` above the
body size), so it works regardless of language. Spans on one line are joined,
with word gaps reconstructed from geometry — PDF writers often express a space
as position rather than a space character.

**Caption detection** looks for a caption-shaped line (`Map 3.`, `Карта 3.`)
near either horizontal edge of the image, including *inside* the image box,
where captions frequently sit. Add languages to `CAPTION_PATTERNS`.

---

## `pdf_extract.py` — the working corpus

```bash
python3 pdf_extract.py <source.pdf> --out build/
python3 pdf_extract.py <source.pdf> --out build/ --pages 6-15,20
python3 pdf_extract.py <source.pdf> --out build/ --keep-garbled
```

Writes `pages/pNNN.txt`, `pages.jsonl`, `headings.tsv`, `full.txt`, `extract.json`.

Three cleanup passes, in order — each catches what the previous cannot:

1. **Drop caps by size** (`DROPCAP_SIZE_RATIO = 3.0`, ≤ 2 glyphs). Size, not
   alphabet: catches a Latin `H` opening a Russian chapter, and never eats a
   legitimate short token like the room code `B1`.
2. **Foreign-script tokens** — whole words in a writing system the document
   does not use.
3. **Rare-script characters** — any writing system under 1% of the document is
   deleted character-by-character. This is what removes an ornament glyph fused
   onto a real word (`Caves)` plus a stray mark reads as mostly-Latin and
   survives passes 1 and 2).

`--keep-garbled` disables all three, for diagnosing a bad extraction.

---

## `pdf_assets.py` — maps, art and plates

```bash
python3 pdf_assets.py <source.pdf> --out <pack>/
python3 pdf_assets.py <source.pdf> --out <pack>/ --maps-only
python3 pdf_assets.py <source.pdf> --out <pack>/ --render-pages 50,51 --dpi 200
```

Reuses `pdf_probe.probe()` wholesale, so classification is identical. Writes
`maps/map-<n>-p<page>.<ext>`, `art/`, `plates/`, and `assets.json`.

`--render-pages` rasterises whole pages — use it for the `image_only_pages` the
probe reports, then read them with the Read tool.

---

## `module_build.py` — slice chapters, measure coverage

```bash
python3 module_build.py --build build/ --pack <pack>/ --plan plan.txt --title "<Title>"
cat plan.txt | python3 module_build.py --build build/ --pack <pack>/
```

Plan format, one chapter per line: `<id>|<title>|<page-range>`.
`#` comments and blank lines are ignored.

Writes `source/<id>.md` (with a provenance header), `source-index.md`, and
`build-manifest.json`. Reports **coverage**: pages assigned, pages missing,
pages landing in more than one chapter. Every page should be assigned exactly
once.

---

## `module_check.py` — validate the pack

```bash
python3 module_check.py --pack <pack>/
python3 module_check.py --pack <pack>/ --json
```

Exit 0 clean, 1 problems found. Checks:

- required files present and non-empty
- **load-time budget**: `world.md`, `npcs.md`, `arc.md` under their size caps —
  the single most important check, since these are read every session
- `npcs.md` is an index table, and every NPC in it has an entry in `npcs-full.md`
- `arc.md` `source_ref`s resolve to real files; no orphan chapters
- `source-index.md` and `source/` agree
- no chapter over `LARGE_CHAPTER_WORDS`
- every `maps/*.md` legend has an image and vice versa
- page coverage from `build-manifest.json`
- UTF-8 valid, no replacement characters, no leftover `<placeholder>` text
