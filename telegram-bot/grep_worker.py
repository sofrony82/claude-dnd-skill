"""
grep_worker.py — the matching half of the sandbox's grep_files, in its own process.

The pattern comes from the model, and through the model from a player. Python's
`re` backtracks, so a pattern like `(a|a)*b` against one long line can run for
hours — and a thread stuck inside `re.search` cannot be stopped; it just keeps
a worker thread and a core busy for every player. A process can be killed, so
the sandbox runs this one with a timeout.

Input is JSON on stdin: the pattern, the files the sandbox has already cleared
(path and display name), the context width and the hit cap. Output on stdout is
the finished tool text. Nothing here decides what may be read.
"""

import json
import re
import sys


def grep(pattern: str, files: list, ctx: int, max_hits: int) -> str:
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)

    out, hits = [], 0
    for path, name in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            hits += 1
            if hits > max_hits:
                out.append(f"[…обрезано на {max_hits} совпадениях — уточни запрос]")
                return "\n".join(out)
            if ctx:
                lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
                out.append(f"--- {name}:{i + 1}")
                out.extend(f"{j + 1}\t{lines[j]}" for j in range(lo, hi))
            else:
                out.append(f"{name}:{i + 1}\t{line.strip()}")
    return "\n".join(out) if out else f"[совпадений нет: {pattern}]"


if __name__ == "__main__":
    job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    text = grep(job["pattern"], job["files"], job["ctx"], job["max_hits"])
    sys.stdout.buffer.write(text.encode("utf-8"))
