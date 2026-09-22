---
name: module-prep
description: "Turn a published tabletop adventure (PDF/DOCX/MD) into a *module pack* — a pre-parsed bundle of markdown and images that a DM agent reads directly at the table, so no agent ever parses the source document during play. Use when asked to prepare, import, convert or pre-process an adventure, module, campaign book or one-shot for automated play. Produces world/npcs/arc/bestiary/items/maps/pregens plus a lazily-loaded per-chapter corpus."
tools: Read, Write, Edit, Glob, Grep, Bash, AskUserQuestion
---

# Adventure Module Prep

> **Skill directory.** `${CLAUDE_SKILL_DIR}` is this skill's directory, already
> resolved to an absolute path in *this* file. Substitute that path before running
> any command — a shell expands the literal token to nothing and you get a broken
> `/scripts/…` path.

## What this skill is for

A DM agent that parses a PDF mid-session is a DM agent that is slow, expensive,
and wrong about page 34. This skill front-loads all of that once, offline, into
a **module pack**: plain markdown the agent reads at play time, plus the images
players actually see.

The division of labour is the whole design, and it is what makes the next
adventure as tractable as this one:

| Deterministic — scripts do it | Interpretive — you do it |
|---|---|
| De-columning, page splitting, cleaning | Where a chapter begins and ends |
| Heading detection by font size | Which headings are chapters vs. rooms |
| Figure vs. page-furniture classification | Which figure is a map of what |
| Coverage and consistency checks | NPC motivations, arc shape, threat ladder |

Never hand-transcribe what a script can extract, and never let a script guess
at meaning. When they disagree, the script is describing the document and you
are describing the adventure — both can be right.

---

## Output: the module pack

```
<pack>/
  module.json          manifest: title, system, levels, language, chapters, maps
  README.md            provenance, what was built, what was inferred, known gaps
  world.md             load-time core — read in full at every session start
  npcs.md              INDEX TABLE ONLY — one row per NPC
  npcs-full.md         full entries, read per-NPC on demand
  arc.md               act/chapter tree with key beats and telegraph scenes
  state-seed.md        starting Current Situation / World State for a new table
  bestiary.md          stat blocks, combat-ready
  items.md             magic items + a "where the treasure is" table
  maps/
    map-N-pNNN.jpeg    the image players are shown
    map-N.md           keyed legend + a "DM only" secrets section
    README.md          map → chapter → when to reveal
  pregens/*.md         pre-generated characters, one file each
  source/<id>.md       VERBATIM module text, one file per chapter (lazy corpus)
  source-index.md      chapter id → file → pages → word count
  build/               the extraction intermediate, kept for re-checking
  build-manifest.json  source hash, chapter plan, page coverage
```

**The load-time contract.** `world.md` + `npcs.md` (index) + `arc.md` are read at
session start. Everything else is read on demand. A pack that violates this —
a 20 KB `npcs.md`, a chapter file holding half the book — costs context on every
single session, forever. `module_check.py` enforces it.

---

## Procedure

### Step 0 — Establish rights and scope

The pack contains the adventure's text nearly verbatim. Build packs only from
material the user owns, and write them into the **data root**
(`~/.claude/dnd/modules/<id>/`, or `$DND_CAMPAIGN_ROOT/modules/<id>/`) — never
into the repository. This repo ships the pipeline, not the output. Confirm the
source path and pack id with the user before writing anything.

### Step 1 — Probe before you parse

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/pdf_probe.py "<source.pdf>" --out build/probe.json
```

Read the report in full. It decides everything downstream:

- **Text layer sparse or absent** → this is a scan. Stop: the pipeline does not
  OCR. Tell the user, and offer to proceed only if they can supply an OCR'd file.
- **`dominant_script`** → the pack's language. The pack is written in the
  source's language unless the user asks otherwise.
- **`columns.verdict`** → two-column is the norm for published modules and is
  handled automatically.
- **`headings`** → your chapter-segmentation evidence. Heading *size* separates
  levels: the largest recurring size is chapters, the next is sections, the next
  is keyed rooms.
- **`figures`** with `kind_hint: map` → the maps, already captioned.
- **`image_only_pages`** → pages whose content is a picture (character sheets,
  handouts). They need rendering and reading with vision.
- **`warnings`** → act on every one.

### Step 2 — Extract the working corpus

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/pdf_extract.py "<source.pdf>" --out build/
```

Writes `build/pages/pNNN.txt`, `build/headings.tsv`, `build/full.txt`. Text is
de-columned into reading order, drop caps and symbol-font ornament runs are
stripped, and writing systems that make up under 1% of the document are deleted
outright (that last rule is what catches an ornament glyph fused onto a real
word, which no token-level filter can see).

Spot-check two or three pages against the PDF before continuing. If the text is
scrambled, the source is not two-column in the way the sorter assumes — re-run
with `--keep-garbled` to see the raw form and investigate.

### Step 3 — Plan the chapters

Read `build/headings.tsv`. Write a chapter plan, one line per chapter:

```
<id>|<title>|<page-range>
1.1|Chapter 1. The Monastery|6-15
app-b|Appendix B. Creatures|38-49
```

Rules that keep a pack playable:

- **Cover every page.** Front matter and appendices are chapters too. The
  builder reports unassigned pages, and unassigned means unreachable at the table.
- **Chapter ids match the arc.** `arc.md` refers to `source/<id>.md` by these ids.
- **Split anything over ~12 000 words** at a natural scene break (`3.1a`, `3.1b`).
  One giant chapter is the one remaining way a structured campaign bloats a load.
- **Follow the book's own divisions.** Do not re-cut the adventure into what you
  think is a better shape; the DM steers, the book supplies situations.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/module_build.py \
  --build build/ --pack ~/.claude/dnd/modules/<id>/ \
  --plan plan.txt --title "<Adventure Title>"
```

Check the printed coverage line. Anything less than every page assigned exactly
once is a bug in the plan, not an acceptable loss.

### Step 4 — Pull the images

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/pdf_assets.py "<source.pdf>" \
  --out ~/.claude/dnd/modules/<id>/ --render-pages <image-only pages> --dpi 200
```

Maps land in `maps/`, illustrations in `art/`, whole rendered pages in `plates/`.
Read the rendered plates with the Read tool — they are usually character sheets
or handouts, and their content has to be transcribed by eye.

### Step 5 — Write the semantic layer

This is the part only a model can do. Work **from the chapter files**, one
artifact at a time, and parallelise across artifacts — they touch different files.

For each artifact below, the standing rules are:

1. **Invent nothing.** Anything not in the source either is omitted or is marked
   `> ДОСТРОЕНО:` / `> INFERRED:` with the basis for the inference. A fabricated
   fact presented as canon is the worst failure mode here: the DM will hand it
   to players as truth and the campaign will diverge from the book permanently.
2. **Copy names and codes character-for-character** from the source, including
   room codes (`A1`, `B2`) and translated proper nouns.
3. **Cite pages** where it helps the DM check you: `(p. 12)`.
4. **Numbers are the product.** AC, HP, damage, DCs, CR, coin values, skill
   bonuses — every one gets verified twice against the page it came from.

| Artifact | Built from | Watch for |
|---|---|---|
| `world.md` | front matter + skim of all chapters | Keep it tight — this is read every session. Setting, backstory, three truths, factions, threat ladder. |
| `npcs.md` | all chapters | **Index table only.** Name, role, faction, where, attitude, one line. |
| `npcs-full.md` | all chapters | Speech quirks and small contradictions verbatim — they are what makes an NPC live at the table. Min. 2 relationships each. |
| `arc.md` | chapter structure | Identify the shape first: linear, hub-and-spoke, or faction-web. Key beats = what must land for the chapter to be done. |
| `bestiary.md` | creature appendix | A combat reference opened mid-fight. Index table first, then stat blocks. Damage as `14 (2d8+5)`. |
| `items.md` | item appendix + all chapters | Also a "where the treasure is" table — location, chapter, how it's obtained. |
| `maps/map-N.md` | the chapter keying that map | Legend from the *text*, not the picture: room codes, what's there, connections. Secrets under a `DM only` heading. |
| `pregens/*.md` | pregen sheets if present, else the rules | If the book ships sheets as images, transcribe them. If it names pregens but omits sheets, build them to the system's rules and mark them inferred. Per-class hooks from the book are not optional — they are each character's reason to be there. |

### Step 6 — Validate

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/module_check.py --pack ~/.claude/dnd/modules/<id>/
```

Fix everything it reports, then re-run until clean. It checks page coverage,
load-time file sizes, map image/legend pairing, NPC index vs. full-entry
consistency, arc → source references, encoding, and leftover template
placeholders.

### Step 7 — Write `README.md` and hand over

The pack's README is for the human who will run it. State: source and its hash,
when it was built, what the pack contains, **what was inferred rather than
extracted**, and any known gaps. A pack whose gaps are documented is usable; one
whose gaps are hidden is a trap.

Then tell the user how to play it — `/dm:dnd` against the pack, or the Telegram
bot in `telegram-bot/`.

---

## Verification pass

Whatever produced the semantic layer, check it adversarially before declaring
the pack done. For each artifact, re-read the source pages it came from and hunt
specifically for: invented facts, wrong numbers, misspelled names and room
codes, material that should have been carried over and was not, and language
drift. Fix in place. This pass routinely finds real errors in otherwise
plausible-looking files — plausibility is exactly the failure mode.

## Non-PDF sources

`.md`, `.txt` and `.docx` skip steps 1–2: there is no layout to recover. Split
the text into `source/<id>.md` by its own headings, write `source-index.md` and
`build-manifest.json` by hand, then continue from step 5. There will be no maps
unless the user supplies images separately.

## Reference

Script details, flags and tuning constants: `SKILL-scripts.md` in this directory.
