#!/usr/bin/env python3
"""
pdf_probe.py — survey an adventure source before extracting anything.

Answers the questions that decide how the rest of the pipeline runs, without
committing to any of it:

  * Is there a real text layer, or is this a scan? (a scan needs OCR, not this)
  * What language / writing system is the body text?
  * One column or two? (drives de-columning)
  * Where are the chapter headings? (font-size analysis — language-agnostic)
  * Which images are content (maps, art, character sheets) and which are page
    furniture repeated on every page?
  * Which pages carry no text at all? (image-only pages need rendering)
  * Is the text layer polluted by decorative glyph runs?

Read the report, then drive pdf_extract.py and pdf_assets.py from it.

Usage:
  python3 pdf_probe.py <source.pdf>            # human-readable report
  python3 pdf_probe.py <source.pdf> --json     # machine-readable, for scripting
  python3 pdf_probe.py <source.pdf> --headings # just the heading candidates
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shared  # noqa: E402

# An image repeated across at least this share of pages is page furniture
# (background texture, border, watermark, stat-block frame), never content.
# Recurrence is judged on the image BYTES, not the xref: a writer that re-embeds
# the same frame under a fresh xref on every page would defeat an xref-keyed
# check, and published modules do exactly that around stat blocks.
RECURRING_PAGE_SHARE = 0.15
RECURRING_MIN_PAGES = 3

# Below this pixel count an image is an icon, bullet or rule — not a figure.
FIGURE_MIN_PIXELS = 150_000

# Flat vector-ish frames compress to almost nothing. A real map, photo or
# painting carries orders of magnitude more bytes per pixel.
FIGURE_MIN_BYTES = 18_000

# An image placed larger than the page is a tiled or clipped background, not a
# figure — the writer is painting texture under the text.
MAX_AREA_SHARE = 1.2

# A heading is set larger than the body. 1.15 catches subheads without
# swallowing emphasised body runs.
HEADING_SIZE_RATIO = 1.15
HEADING_MAX_CHARS = 120

# Caption patterns worth flagging automatically. Extend per-language as needed;
# unmatched figures still get their nearest text line reported as a hint.
CAPTION_PATTERNS = [
    r"(?i)\b(?:map|карта|carte|mapa|karte|mappa)\s*(\d+)",
    r"(?i)\b(?:figure|рис\.?|рисунок|abb\.?)\s*(\d+)",
]


def _load_fitz():
    try:
        import fitz
        return fitz
    except ImportError:
        print(
            "PyMuPDF is required for probing. Install it:\n  pip3 install pymupdf",
            file=sys.stderr,
        )
        sys.exit(2)


def _spans(page):
    """Yield (size, text, bbox) for every text span on the page.

    Spans that share a line and a font size are joined — PDF writers routinely
    split one visual heading across many spans, sometimes one per word or even
    per glyph. Where the writer relied on positioning rather than a space
    character to separate words, the horizontal gap between spans is
    reintroduced as a space; without that, headings come back as
    "Ch1TheDrownedSailors".
    """
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            buf, size, bbox, prev_x1 = [], None, None, None
            for sp in line.get("spans", []):
                t = sp.get("text", "")
                if not t.strip():
                    continue
                s = round(sp.get("size", 0), 1)
                sb = sp["bbox"]
                if size is None or abs(s - size) < 0.6:
                    if buf and prev_x1 is not None:
                        gap = sb[0] - prev_x1
                        # A gap wider than a fifth of the em is a word break the
                        # writer expressed as geometry instead of a space.
                        if gap > 0.2 * s and not t[:1].isspace() and not buf[-1][-1:].isspace():
                            buf.append(" ")
                    buf.append(t)
                    size = s if size is None else size
                    bbox = sb if bbox is None else (
                        min(bbox[0], sb[0]), min(bbox[1], sb[1]),
                        max(bbox[2], sb[2]), max(bbox[3], sb[3]),
                    )
                else:
                    if buf:
                        yield size, "".join(buf).strip(), bbox
                    buf, size, bbox = [t], s, sb
                prev_x1 = sb[2]
            if buf:
                yield size, "".join(buf).strip(), bbox


def _column_verdict(page):
    """'one' | 'two' — how many text columns this page appears to use."""
    blocks = [b for b in page.get_text("blocks") if len(b) >= 7 and b[6] == 0 and b[4].strip()]
    if len(blocks) < 4:
        return "one"
    mid = page.rect.width / 2.0
    tol = page.rect.width * 0.05
    left = sum(1 for b in blocks if b[2] <= mid + tol)
    right = sum(1 for b in blocks if b[0] >= mid - tol)
    share = max(1, int(0.15 * len(blocks)))
    return "two" if left >= share and right >= share else "one"


def _caption_number(text: str):
    for pat in CAPTION_PATTERNS:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


# How far from an image edge a line can sit and still be its caption. Captions
# are often set *inside* the image box (overlapping its bottom edge), so the
# window is symmetric rather than strictly outside.
CAPTION_WINDOW = 60.0
CAPTION_MAX_CHARS = 90


def _caption_for(page, rect, page_text_lines):
    """Find a figure's caption line, returning (text, number or None).

    Two passes, because layout alone is ambiguous in a two-column book where
    body text runs right up to the artwork:
      1. A line near either horizontal edge of the image that reads like a
         caption ("Map 3.", "Карта 3.", "Figure 2") wins outright.
      2. Otherwise the shortest nearby line below the image is the best guess,
         reported as a hint for the model rather than as a fact.
    """
    if rect is None:
        return None, None

    near = []
    for text, bbox in page_text_lines:
        t = text.strip()
        if not t:
            continue
        if bbox[2] < rect.x0 - 20 or bbox[0] > rect.x1 + 20:
            continue  # no horizontal overlap with the image
        d_bottom = abs(bbox[1] - rect.y1)
        d_top = abs(bbox[3] - rect.y0)
        d = min(d_bottom, d_top)
        if d <= CAPTION_WINDOW:
            near.append((d, d_bottom, t))

    labelled = [(d, t) for d, _db, t in near if _caption_number(t) is not None]
    if labelled:
        labelled.sort(key=lambda z: z[0])
        return labelled[0][1], _caption_number(labelled[0][1])

    # Page-level fallback: exactly one caption-shaped line on the page and no
    # competing figure — safe to attribute it to this image.
    page_labelled = [t.strip() for t, _b in page_text_lines if _caption_number(t) is not None]
    if len(set(page_labelled)) == 1:
        only = page_labelled[0]
        return only, _caption_number(only)

    short = [(db, t) for _d, db, t in near if len(t) <= CAPTION_MAX_CHARS]
    if short:
        short.sort(key=lambda z: z[0])
        return short[0][1], None
    return None, None


def probe(path: str) -> dict:
    fitz = _load_fitz()
    doc = fitz.open(path)
    n = doc.page_count

    xref_pages = {}      # xref -> set of pages it appears on
    xref_meta = {}       # xref -> (w, h, ext, bytes, sha256)
    sha_pages = {}       # image bytes sha -> set of pages (xref-independent)
    per_page = []
    all_sizes = {}       # rounded size -> char count
    headings_raw = []
    full_text_parts = []

    for i, page in enumerate(doc, start=1):
        text = page.get_text()
        full_text_parts.append(text)

        spans = list(_spans(page))
        for size, t, bbox in spans:
            all_sizes[size] = all_sizes.get(size, 0) + len(t)

        images = page.get_images(full=True)
        for im in images:
            xref = im[0]
            xref_pages.setdefault(xref, set()).add(i)
            if xref not in xref_meta:
                try:
                    info = doc.extract_image(xref)
                    sha = hashlib.sha256(info["image"]).hexdigest()
                    xref_meta[xref] = (info["width"], info["height"], info["ext"],
                                       len(info["image"]), sha)
                except Exception:
                    xref_meta[xref] = (0, 0, "?", 0, "")
            sha = xref_meta[xref][4]
            if sha:
                sha_pages.setdefault(sha, set()).add(i)

        per_page.append({
            "page": i,
            "chars": len(text.strip()),
            "words": len(text.split()),
            "images": len(images),
            "columns": _column_verdict(page),
            "spans": spans,
        })

    full_text = "\n".join(full_text_parts)
    expected = _shared.dominant_script(full_text)
    profile = _shared.script_profile(full_text)

    # Body size = the size that sets the most characters.
    body_size = max(all_sizes, key=all_sizes.get) if all_sizes else 0.0
    heading_floor = body_size * HEADING_SIZE_RATIO

    for p in per_page:
        for size, t, bbox in p["spans"]:
            if size < heading_floor or not t or len(t) > HEADING_MAX_CHARS:
                continue
            if t.strip().isdigit():
                continue
            # A lone oversized character is a decorative drop cap opening a
            # chapter, not a heading — the real heading sits beside it.
            if len(t.strip()) < 2:
                continue
            if _shared.is_garbled(t, expected):
                continue
            headings_raw.append({"page": p["page"], "size": size, "text": t.strip()})
        del p["spans"]

    # Figures: strip page furniture, then anything that cannot be content.
    #
    # Three independent filters, because no single one is sufficient:
    #   * byte-identical repetition  → backgrounds and stat-block frames, even
    #     when each copy carries its own xref
    #   * size / weight              → icons, rules, flat vector frames
    #   * placement larger than page → tiled or clipped background texture
    recurring_floor = max(RECURRING_MIN_PAGES, int(RECURRING_PAGE_SHARE * n))
    recurring, candidates = [], []
    seen_sha = set()
    for xref, pages in sorted(xref_pages.items()):
        w, h, ext, nbytes, sha = xref_meta.get(xref, (0, 0, "?", 0, ""))
        occurrences = sha_pages.get(sha, pages)
        rec = {"xref": xref, "pages": sorted(pages), "page_count": len(occurrences),
               "width": w, "height": h, "ext": ext, "bytes": nbytes, "sha256": sha[:16]}
        if len(occurrences) >= recurring_floor:
            if sha not in seen_sha:
                seen_sha.add(sha)
                rec["pages"] = sorted(occurrences)
                rec["reason"] = f"byte-identical on {len(occurrences)} pages"
                recurring.append(rec)
            continue
        if sha and sha in seen_sha:
            continue  # duplicate of a figure already recorded
        if w * h < FIGURE_MIN_PIXELS or nbytes < FIGURE_MIN_BYTES:
            continue
        if sha:
            seen_sha.add(sha)
        candidates.append(rec)

    # Caption hunt and placement check, only for surviving candidates.
    figures = []
    for fig in candidates:
        pno = fig["pages"][0]
        page = doc[pno - 1]
        lines = [(t, b) for _s, t, b in _spans(page)]
        try:
            rects = page.get_image_rects(fig["xref"])
            rect = rects[0] if rects else None
        except Exception:
            rect = None
        page_area = page.rect.width * page.rect.height
        share = round((rect.width * rect.height) / page_area, 3) if rect is not None else None
        if share is not None and share > MAX_AREA_SHARE:
            fig["reason"] = f"placed at {share}× page area — background texture"
            recurring.append(fig)
            continue
        cap, num = _caption_for(page, rect, lines)
        fig["caption"] = cap
        fig["caption_number"] = num
        fig["page_area_share"] = share
        is_map_caption = bool(cap and re.search(CAPTION_PATTERNS[0], cap))
        fig["kind_hint"] = (
            "map" if is_map_caption
            # An image with no text on its page, filling that page, is a plate:
            # a character sheet, handout or full-page illustration.
            else "full-page" if (share or 0) > 0.9 and per_page[pno - 1]["chars"] < 40
            # PyMuPDF could not resolve a placement rectangle — usually an image
            # drawn through a form XObject. Extractable, but its role on the page
            # is unknown, so it is never guessed at.
            else "unplaced" if rect is None
            else "art"
        )
        figures.append(fig)
    # Unplaced images sort last: they are the least likely to be content.
    figures.sort(key=lambda f: (f["kind_hint"] == "unplaced", f["pages"][0],
                                -f["width"] * f["height"]))

    image_only = [p["page"] for p in per_page if p["chars"] < 40 and p["images"] > 0]
    two_col = sum(1 for p in per_page if p["columns"] == "two")

    # Garbled-run census: how much decorative noise sits in the text layer.
    garbled_pages = []
    for i, part in enumerate(full_text_parts, start=1):
        bad = [w for w in part.split() if _shared.is_garbled(w, expected)]
        if bad:
            garbled_pages.append({"page": i, "runs": len(bad), "sample": " ".join(bad[:4])})

    warnings = []
    if sum(p["words"] for p in per_page) < 50 * n:
        warnings.append(
            "Sparse text layer — this may be a scanned PDF. Verify a page before "
            "extracting; if the text is empty, OCR the source first (this pipeline "
            "does not OCR)."
        )
    if image_only:
        warnings.append(
            f"{len(image_only)} page(s) carry images but no text "
            f"({', '.join(map(str, image_only[:10]))}). Render them with "
            "pdf_assets.py --render-pages and read them visually."
        )
    if garbled_pages:
        warnings.append(
            f"{len(garbled_pages)} page(s) contain decorative glyph runs in a "
            f"foreign script. pdf_extract.py strips these by default "
            "(--keep-garbled to retain)."
        )
    if not doc.get_toc():
        warnings.append("No PDF bookmarks. Use the heading candidates below to segment chapters.")

    return {
        "file": os.path.abspath(path),
        "filename": os.path.basename(path),
        "sha256": _shared.sha256_file(path),
        "pages": n,
        "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
        "toc": doc.get_toc(),
        "language": {
            "dominant_script": expected,
            "script_counts": dict(sorted(profile.items(), key=lambda kv: -kv[1])),
        },
        "text_layer": {
            "total_words": sum(p["words"] for p in per_page),
            "pages_with_text": sum(1 for p in per_page if p["chars"] >= 40),
            "image_only_pages": image_only,
        },
        "columns": {
            "two_column_pages": two_col,
            "verdict": "two-column" if two_col > n * 0.4 else "single-column",
        },
        "body_font_size": body_size,
        "heading_floor": round(heading_floor, 2),
        "headings": headings_raw,
        "figures": figures,
        "recurring_images": sorted(recurring, key=lambda r: -r["page_count"]),
        "garbled_pages": garbled_pages,
        "per_page": per_page,
        "warnings": warnings,
    }


def render(rep: dict) -> str:
    L = []
    a = L.append
    a(f"Source:    {rep['filename']}")
    a(f"SHA-256:   {rep['sha256'][:16]}…")
    a(f"Pages:     {rep['pages']}")
    if rep["metadata"]:
        a(f"Metadata:  {json.dumps(rep['metadata'], ensure_ascii=False)}")
    lang = rep["language"]
    a(f"Script:    {lang['dominant_script']}  {lang['script_counts']}")
    tl = rep["text_layer"]
    a(f"Text:      {tl['total_words']:,} words over {tl['pages_with_text']}/{rep['pages']} pages")
    a(f"Layout:    {rep['columns']['verdict']} ({rep['columns']['two_column_pages']} two-column pages)")
    a(f"Body size: {rep['body_font_size']}pt  →  heading floor {rep['heading_floor']}pt")
    a(f"TOC:       {len(rep['toc'])} bookmark(s)")
    a("")
    a(f"── Heading candidates ({len(rep['headings'])}) " + "─" * 30)
    for h in rep["headings"]:
        a(f"  p{h['page']:>3}  {h['size']:>5.1f}pt  {h['text']}")
    a("")
    a(f"── Figures ({len(rep['figures'])}) " + "─" * 40)
    for f in rep["figures"]:
        cap = f.get("caption") or "—"
        a(f"  p{f['pages'][0]:>3}  xref {f['xref']:<6} {f['width']}x{f['height']} "
          f"{f['ext']:<5} {f['bytes']//1024:>5}KB  share={f.get('page_area_share')}  "
          f"[{f.get('kind_hint')}]  {cap[:70]}")
    a("")
    a(f"── Page furniture, ignored ({len(rep['recurring_images'])}) " + "─" * 20)
    for r in rep["recurring_images"]:
        a(f"  xref {r['xref']:<6} on {r['page_count']}/{rep['pages']} pages  "
          f"{r['width']}x{r['height']} {r['ext']}")
    if rep["garbled_pages"]:
        a("")
        a(f"── Decorative glyph runs ({len(rep['garbled_pages'])} pages) " + "─" * 15)
        for g in rep["garbled_pages"][:12]:
            a(f"  p{g['page']:>3}  {g['runs']} run(s)  {g['sample'][:50]}")
    if rep["warnings"]:
        a("")
        a("── Warnings " + "─" * 48)
        for w in rep["warnings"]:
            a(f"  ! {w}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Survey an adventure PDF before extraction.")
    ap.add_argument("source")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--headings", action="store_true", help="print heading candidates only")
    ap.add_argument("--out", help="also write the JSON report to this path")
    args = ap.parse_args()

    if not os.path.exists(args.source):
        print(f"Error: no such file: {args.source}", file=sys.stderr)
        sys.exit(1)

    rep = probe(args.source)

    if args.out:
        pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.headings:
        for h in rep["headings"]:
            print(f"p{h['page']}\t{h['size']}\t{h['text']}")
    elif args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))


if __name__ == "__main__":
    main()
