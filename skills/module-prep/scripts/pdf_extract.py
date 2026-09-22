#!/usr/bin/env python3
"""
pdf_extract.py — turn an adventure PDF into a per-page working corpus.

Produces the intermediate build/ tree that every later step reads. Nothing here
is interpretive: it de-columns, cleans, and splits by page. Deciding where a
chapter begins is the model's job, and it needs stable page-addressable text to
do it with.

Output layout (under --out, default ./build):
    pages/p001.txt … pNNN.txt   one file per page, reading order, cleaned
    pages.jsonl                 one JSON record per page (text, headings, stats)
    headings.tsv                page / size / text — the chapter-segmentation aid
    full.txt                    whole document, page-delimited
    extract.json                run manifest (source hash, settings, counts)

Why per-page and not one blob: a module pack must cite where each chapter came
from, and `module_check.py` measures coverage by comparing chapter files back
against these pages. A single blob makes both impossible.

Usage:
  python3 pdf_extract.py <source.pdf> --out build/
  python3 pdf_extract.py <source.pdf> --out build/ --pages 6-15
  python3 pdf_extract.py <source.pdf> --out build/ --keep-garbled
"""

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shared  # noqa: E402
import pdf_probe  # noqa: E402

PAGE_DELIM = "\n\n===== PAGE {n} =====\n\n"


def parse_pages(spec: str, total: int):
    """'6-15,20,33-' -> sorted list of 1-based page numbers."""
    if not spec:
        return list(range(1, total + 1))
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            lo = int(lo) if lo.strip() else 1
            hi = int(hi) if hi.strip() else total
        else:
            lo = hi = int(part)
        out.update(range(max(1, lo), min(total, hi) + 1))
    return sorted(out)


# A drop cap is set several times the body size and is one or two glyphs long.
# Judging it by size rather than by alphabet catches the Latin "H" opening a
# Russian chapter as readily as a symbol-font ornament — and never mistakes a
# legitimate short token such as the room code "B1" for decoration.
DROPCAP_SIZE_RATIO = 3.0
DROPCAP_MAX_CHARS = 2


def dropcap_tokens(page, body_size: float) -> set:
    """Exact strings on this page that are oversized one- or two-glyph runs."""
    if body_size <= 0:
        return set()
    floor = body_size * DROPCAP_SIZE_RATIO
    out = set()
    for size, t, _bbox in pdf_probe._spans(page):
        s = t.strip()
        if size >= floor and 0 < len(s) <= DROPCAP_MAX_CHARS:
            out.add(s)
    return out


def page_text(page, order_blocks, expected_script: str, keep_garbled: bool,
              body_size: float = 0.0, noise: set = None):
    """Reading-order text for one page, with decorative glyph runs removed."""
    raw = page.get_text("blocks")
    blocks = [
        (b[0], b[1], b[2], b[3], b[4])
        for b in raw
        if len(b) >= 7 and b[6] == 0 and b[4] and b[4].strip()
    ]
    ordered = order_blocks(blocks, page.rect.width)
    text = "\n\n".join(ordered)

    if not keep_garbled:
        # Two kinds of noise, both of which reach the table as gibberish if
        # left in: runs rendered through a symbol font (the text layer reports
        # them as an unrelated writing system) and oversized drop caps.
        drops = dropcap_tokens(page, body_size)
        cleaned_lines = []
        for line in text.split("\n"):
            kept = [
                w for w in line.split(" ")
                if w.strip() not in drops
                and not _shared.is_garbled(w, expected_script, min_letters=1)
            ]
            # A line that was entirely decorative disappears; one that merely
            # began with a drop cap keeps its real words.
            cleaned_lines.append(" ".join(kept))
        text = "\n".join(cleaned_lines)
        # Final character-level sweep: ornament glyphs fused onto a real
        # word survive token-level checks, because the token reads as
        # mostly-legitimate. Deleting the rare script outright catches them.
        text = _shared.strip_scripts(text, noise or set())

    return _shared.normalise(text)


def main():
    ap = argparse.ArgumentParser(description="Extract a page-addressable corpus from an adventure PDF.")
    ap.add_argument("source")
    ap.add_argument("--out", default="build", help="output directory (default: build)")
    ap.add_argument("--pages", default="", help="page selection, e.g. '6-15,20'")
    ap.add_argument("--keep-garbled", action="store_true",
                    help="retain foreign-script decorative runs instead of stripping them")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.source):
        print(f"Error: no such file: {args.source}", file=sys.stderr)
        sys.exit(1)

    fitz = pdf_probe._load_fitz()
    order_blocks = _shared.order_blocks()
    doc = fitz.open(args.source)

    out = pathlib.Path(args.out).resolve()
    (out / "pages").mkdir(parents=True, exist_ok=True)

    wanted = parse_pages(args.pages, doc.page_count)

    # Script is judged over the whole document, not per page: a single page of
    # a Russian book can be mostly Latin stat abbreviations and would otherwise
    # have its real text classified as foreign and stripped.
    whole = "\n".join(doc[i - 1].get_text() for i in wanted)
    expected = _shared.dominant_script(whole)
    noise = _shared.noise_scripts(whole)

    records, full_parts, headings = [], [], []
    body_size_counts = {}

    for n in wanted:
        page = doc[n - 1]
        for size, t, _bbox in pdf_probe._spans(page):
            body_size_counts[size] = body_size_counts.get(size, 0) + len(t)

    body_size = max(body_size_counts, key=body_size_counts.get) if body_size_counts else 0.0
    floor = body_size * pdf_probe.HEADING_SIZE_RATIO

    for n in wanted:
        page = doc[n - 1]
        text = page_text(page, order_blocks, expected, args.keep_garbled, body_size, noise)

        page_headings = []
        for size, t, _bbox in pdf_probe._spans(page):
            t = t.strip()
            if size < floor or len(t) < 2 or len(t) > pdf_probe.HEADING_MAX_CHARS:
                continue
            if t.isdigit() or _shared.is_garbled(t, expected):
                continue
            page_headings.append({"size": size, "text": t})
            headings.append((n, size, t))

        (out / "pages" / f"p{n:03d}.txt").write_text(text, encoding="utf-8")
        records.append({
            "page": n,
            "words": _shared.word_count(text),
            "chars": len(text),
            "headings": page_headings,
            "text": text,
        })
        full_parts.append(PAGE_DELIM.format(n=n) + text)

    with open(out / "pages.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with open(out / "headings.tsv", "w", encoding="utf-8") as f:
        f.write("page\tsize\ttext\n")
        for n, size, t in headings:
            f.write(f"{n}\t{size}\t{t}\n")

    (out / "full.txt").write_text("".join(full_parts).strip() + "\n", encoding="utf-8")

    manifest = {
        "source": os.path.abspath(args.source),
        "source_sha256": _shared.sha256_file(args.source),
        "pages_in_document": doc.page_count,
        "pages_extracted": wanted,
        "expected_script": expected,
        "noise_scripts_removed": sorted(noise),
        "body_font_size": body_size,
        "heading_floor": round(floor, 2),
        "garbled_stripped": not args.keep_garbled,
        "total_words": sum(r["words"] for r in records),
        "empty_pages": [r["page"] for r in records if r["words"] == 0],
        "headings": len(headings),
    }
    (out / "extract.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(f"Extracted {len(records)} page(s), {manifest['total_words']:,} words → {out}")
        print(f"  pages/p001.txt … p{wanted[-1]:03d}.txt")
        print(f"  headings.tsv   {len(headings)} candidate heading(s)")
        print(f"  script         {expected}"
              + ("  (decorative foreign-script runs stripped)" if not args.keep_garbled else ""))
        if manifest["empty_pages"]:
            print(f"  ! empty pages: {manifest['empty_pages']} — render them with pdf_assets.py --render-pages")


if __name__ == "__main__":
    main()
