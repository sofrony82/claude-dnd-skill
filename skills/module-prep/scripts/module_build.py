#!/usr/bin/env python3
"""
module_build.py — assemble a module pack's lazy corpus from a chapter plan.

The model decides where chapters begin (reading build/headings.tsv); this
script does the mechanical part that must not drift: slicing the extracted
pages into one file per chapter, writing the index, and recording provenance.

Chapter plan format — one chapter per line, on stdin or via --plan:

    <id>|<title>|<page-range>
    1.1|Драконий Покой|6-15
    app-b|Приложение Б. Существа|38-49

Output (under --pack):
    source/<id>.md        chapter text, with a provenance header
    source-index.md       id → file → pages → word count
    build-manifest.json   source hash, plan, per-chapter stats, coverage

Coverage is the number that matters: every extracted page must land in exactly
one chapter, or the DM will hit a scene whose text was never carried over.

Usage:
  python3 module_build.py --build build/ --pack ~/.claude/dnd/modules/foo --plan plan.txt
  cat plan.txt | python3 module_build.py --build build/ --pack ~/.claude/dnd/modules/foo
"""

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shared  # noqa: E402
from pdf_extract import parse_pages  # noqa: E402


def read_plan(text: str, total_pages: int):
    chapters = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 3:
            raise SystemExit(
                f"Plan line {lineno} malformed (need '<id>|<title>|<pages>'): {raw!r}"
            )
        cid, title, pages = (p.strip() for p in parts)
        if not cid or not title or not pages:
            raise SystemExit(f"Plan line {lineno} has an empty field: {raw!r}")
        chapters.append({"id": cid, "title": title,
                         "pages": parse_pages(pages, total_pages), "range": pages})
    if not chapters:
        raise SystemExit("Plan is empty — nothing to build.")
    return chapters


def main():
    ap = argparse.ArgumentParser(description="Build a module pack's lazy corpus from a chapter plan.")
    ap.add_argument("--build", required=True, help="pdf_extract.py output directory")
    ap.add_argument("--pack", required=True, help="module pack directory to write into")
    ap.add_argument("--plan", help="chapter plan file (default: stdin)")
    ap.add_argument("--title", default="", help="module title for the index header")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    build = pathlib.Path(args.build).resolve()
    extract = json.loads((build / "extract.json").read_text(encoding="utf-8"))
    total = extract["pages_in_document"]

    plan_text = pathlib.Path(args.plan).read_text(encoding="utf-8") if args.plan else sys.stdin.read()
    chapters = read_plan(plan_text, total)

    pack = pathlib.Path(args.pack).expanduser().resolve()
    (pack / "source").mkdir(parents=True, exist_ok=True)

    extracted = set(extract["pages_extracted"])
    assigned, duplicated = set(), {}

    for ch in chapters:
        parts, words = [], 0
        for n in ch["pages"]:
            f = build / "pages" / f"p{n:03d}.txt"
            if not f.is_file():
                continue
            body = f.read_text(encoding="utf-8").strip()
            if n in assigned:
                duplicated.setdefault(n, []).append(ch["id"])
            assigned.add(n)
            if not body:
                continue
            parts.append(f"<!-- страница {n} -->\n\n{body}")
            words += _shared.word_count(body)

        header = (
            f"# {ch['title']}\n\n"
            f"*Источник: {os.path.basename(extract['source'])}, "
            f"страницы {ch['range']}. Текст извлечён автоматически и не "
            f"редактировался — это дословный материал модуля для мастера.*\n\n---\n\n"
        )
        (pack / "source" / f"{ch['id']}.md").write_text(header + "\n\n".join(parts) + "\n",
                                                        encoding="utf-8")
        ch["words"] = words
        ch["file"] = f"source/{ch['id']}.md"

    missing = sorted(extracted - assigned)

    lines = [f"# Указатель исходника — {args.title or 'модуль'}", ""]
    lines.append(f"*Собрано из `{os.path.basename(extract['source'])}` "
                 f"(sha256 `{extract['source_sha256'][:16]}…`). "
                 f"Каждая глава читается по требованию, не при загрузке.*")
    lines += ["", "| Глава | Файл | Страницы | Слов |", "|---|---|---|---|"]
    for ch in chapters:
        lines.append(f"| `{ch['id']}` | [{ch['file']}]({ch['file']}) | {ch['range']} | {ch['words']:,} |")
    if missing:
        lines += ["", f"> ⚠ Страницы, не вошедшие ни в одну главу: {missing}"]
    (pack / "source-index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest = {
        "title": args.title,
        "source": extract["source"],
        "source_sha256": extract["source_sha256"],
        "built_from_build_dir": str(build),
        "chapters": [{k: ch[k] for k in ("id", "title", "range", "file", "words")} for ch in chapters],
        "coverage": {
            "pages_extracted": len(extracted),
            "pages_assigned": len(assigned & extracted),
            "pages_missing": missing,
            "pages_duplicated": {str(k): v for k, v in duplicated.items()},
            "total_words": sum(ch["words"] for ch in chapters),
        },
    }
    (pack / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                              encoding="utf-8")

    if not args.quiet:
        cov = manifest["coverage"]
        print(f"Built {len(chapters)} chapter(s) → {pack}")
        for ch in chapters:
            print(f"  {ch['id']:<8} {ch['range']:<8} {ch['words']:>6,} words  {ch['title']}")
        print(f"  coverage: {cov['pages_assigned']}/{cov['pages_extracted']} pages, "
              f"{cov['total_words']:,} words")
        if missing:
            print(f"  ! unassigned pages: {missing}")
        if duplicated:
            print(f"  ! pages in more than one chapter: {duplicated}")


if __name__ == "__main__":
    main()
