#!/usr/bin/env python3
"""
pdf_assets.py — pull maps, artwork and full-page plates out of an adventure PDF.

Consumes pdf_probe.py's figure classification (same filters, same thresholds)
and writes the images a module pack ships, plus a manifest naming where each
one came from. Pages with no text layer (character sheets, handouts) can be
rendered whole.

Output layout (under --out):
    maps/map-<n>-p<page>.<ext>      figures whose caption reads "Map N"
    art/art-p<page>-<xref>.<ext>    everything else that survived filtering
    plates/page-<n>.png             whole pages rendered via --render-pages
    assets.json                     manifest: kind, page, caption, dimensions

Usage:
  python3 pdf_assets.py <source.pdf> --out pack/
  python3 pdf_assets.py <source.pdf> --out pack/ --maps-only
  python3 pdf_assets.py <source.pdf> --out pack/ --render-pages 50,51,52 --dpi 200
"""

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shared  # noqa: E402
import pdf_probe  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Extract maps and artwork from an adventure PDF.")
    ap.add_argument("source")
    ap.add_argument("--out", default="pack", help="output directory (default: pack)")
    ap.add_argument("--maps-only", action="store_true", help="skip art/, write maps/ only")
    ap.add_argument("--render-pages", default="",
                    help="render whole pages to plates/, e.g. '50,51,52'")
    ap.add_argument("--dpi", type=int, default=180, help="render DPI for --render-pages")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.source):
        print(f"Error: no such file: {args.source}", file=sys.stderr)
        sys.exit(1)

    fitz = pdf_probe._load_fitz()
    doc = fitz.open(args.source)
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # Reuse the probe's classification wholesale rather than re-deriving it —
    # the two must never disagree about what counts as a map.
    rep = pdf_probe.probe(args.source)

    assets = []
    maps_dir, art_dir, plates_dir = out / "maps", out / "art", out / "plates"

    for fig in rep["figures"]:
        kind = fig["kind_hint"]
        if args.maps_only and kind != "map":
            continue
        page = fig["pages"][0]
        try:
            info = doc.extract_image(fig["xref"])
        except Exception as e:
            print(f"  ! could not extract xref {fig['xref']} on p{page}: {e}", file=sys.stderr)
            continue
        ext = info["ext"]

        if kind == "map":
            n = fig.get("caption_number")
            stem = f"map-{n}-p{page:03d}" if n is not None else f"map-p{page:03d}-{fig['xref']}"
            target_dir = maps_dir
        elif kind == "full-page":
            stem = f"plate-p{page:03d}-{fig['xref']}"
            target_dir = plates_dir
        else:
            stem = f"art-p{page:03d}-{fig['xref']}"
            target_dir = art_dir

        target_dir.mkdir(parents=True, exist_ok=True)
        name = f"{stem}.{ext}"
        (target_dir / name).write_bytes(info["image"])

        assets.append({
            "kind": kind,
            "file": str((target_dir / name).relative_to(out)),
            "page": page,
            "xref": fig["xref"],
            "width": fig["width"],
            "height": fig["height"],
            "bytes": fig["bytes"],
            "caption": fig.get("caption"),
            "caption_number": fig.get("caption_number"),
            "page_area_share": fig.get("page_area_share"),
        })

    if args.render_pages:
        plates_dir.mkdir(parents=True, exist_ok=True)
        from pdf_extract import parse_pages
        for n in parse_pages(args.render_pages, doc.page_count):
            pix = doc[n - 1].get_pixmap(dpi=args.dpi)
            name = f"page-{n:03d}.png"
            pix.save(str(plates_dir / name))
            assets.append({
                "kind": "rendered-page",
                "file": str((plates_dir / name).relative_to(out)),
                "page": n,
                "width": pix.width,
                "height": pix.height,
                "bytes": os.path.getsize(plates_dir / name),
                "caption": None,
                "caption_number": None,
                "page_area_share": 1.0,
            })

    manifest = {
        "source": os.path.abspath(args.source),
        "source_sha256": rep["sha256"],
        "counts": {
            k: sum(1 for a in assets if a["kind"] == k)
            for k in ("map", "art", "full-page", "unplaced", "rendered-page")
        },
        "assets": assets,
    }
    (out / "assets.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(f"Wrote {len(assets)} asset(s) → {out}")
        for k, v in manifest["counts"].items():
            if v:
                print(f"  {k:<14} {v}")
        for a in assets:
            if a["kind"] in ("map", "rendered-page"):
                print(f"    {a['file']}  p{a['page']}  {a.get('caption') or ''}")


if __name__ == "__main__":
    main()
