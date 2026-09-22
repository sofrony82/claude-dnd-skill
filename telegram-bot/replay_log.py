#!/usr/bin/env python3
"""
replay_log.py — replay a recorded session against the running DM and grade it.

Takes a `raw-log.md` written by a previous game, pulls out just the player's
turns, and feeds them to the API server in order. The DM's answers will differ —
different model, different dice — so this does not diff prose. It produces a new
transcript for reading side by side, and it checks the things that are true or
false regardless of which way a scene went.

The checks, and why each one is here:

  invented_dice   The prose shows "🎲 d20+3 → 17" but the sandbox executed no
                  roll on that turn. This is the failure that matters most: a
                  DM that fakes dice is not running the game, and it is
                  invisible from the chat window.
  not_russian     The table is Russian. A reply that comes back mostly Latin is
                  usually reasoning or a preamble leaking into narration.
  leaked_paths    A filesystem path or a module filename reached the player.
  leaked_tools    A tool name reached the player.
  leaked_headings Markdown headings in prose, which the prompt forbids.
  room_codes      Raw module room codes such as "A3" instead of a description.
  empty           The turn produced no narration at all.

Usage:
    python3 replay_log.py --log ~/.claude/dnd/users/401712068/campaigns/tg-401712068/raw-log.md \\
                          --chat-id 991712068 --out /tmp/raw-log-deepseek.md
    python3 replay_log.py --log … --limit 8        # smoke test, first 8 turns
"""

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

DM_SPEAKER = "dnd master"

# A roll claimed in prose: the dice emoji, or dice notation followed by a result.
_CLAIM_EMOJI = re.compile(r"🎲")
_CLAIM_MATH = re.compile(r"\b\d*d\d+\s*(?:[+-]\s*\d+)?\s*(?:→|->|=|:)\s*\d", re.I)

_LEAK_PATHS = re.compile(
    r"/home/|/Users/|~/\.claude|\bnpcs-full\.md|\bworld\.md|\bstate\.md|"
    r"\barc\.md|\bbestiary\.md|\bsource/\d|\bcharacters/", re.I)
_LEAK_TOOLS = re.compile(
    r"\b(read_file|write_file|edit_file|glob_files|grep_files|run_script|"
    r"roll_dice|tool_call|function_call)\b", re.I)
_HEADINGS = re.compile(r"^#{1,6}\s+\S", re.M)
# Module room codes: a lone capital+digit token like "A3", "B12". Excludes
# things that legitimately look similar, e.g. "d20" or a 1d8.
_ROOM_CODE = re.compile(r"(?<![\w\d])[A-ZА-Я]\d{1,2}(?![\w\d])")


def parse_player_turns(log_path: pathlib.Path) -> list:
    """Player utterances from a transcript, in order, DM lines dropped."""
    text = log_path.read_text(encoding="utf-8")
    parts = re.split(r"^> (.+?):\s*$", text, flags=re.M)
    turns = []
    for i in range(1, len(parts) - 1, 2):
        speaker, body = parts[i].strip(), parts[i + 1].strip()
        if speaker.lower() != DM_SPEAKER and body:
            turns.append(body)
    return turns


def post(base: str, path: str, payload: dict, timeout: float = 900.0) -> dict:
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get(base: str, path: str, timeout: float = 60.0) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as r:
        return json.load(r)


def grade(narration: str, rolls: list) -> list:
    """Problems with one DM reply. Empty list means it passed."""
    flags = []
    if not narration.strip():
        return ["empty"]

    claimed = len(_CLAIM_EMOJI.findall(narration)) or len(_CLAIM_MATH.findall(narration))
    if claimed and not rolls:
        flags.append("invented_dice")

    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", narration)
    if letters:
        cyr = sum(1 for c in letters if c.isalpha() and c.lower() >= "а")
        if cyr / len(letters) < 0.75:
            flags.append("not_russian")

    if _LEAK_PATHS.search(narration):
        flags.append("leaked_paths")
    if _LEAK_TOOLS.search(narration):
        flags.append("leaked_tools")
    if _HEADINGS.search(narration):
        flags.append("leaked_headings")
    if _ROOM_CODE.search(narration):
        flags.append("room_codes")
    return flags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, type=pathlib.Path)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--chat-id", type=int, required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--pregen", default="wizard-elf")
    ap.add_argument("--name", default="sofrony")
    ap.add_argument("--limit", type=int, default=0, help="only the first N turns")
    ap.add_argument("--report", type=pathlib.Path, help="write the JSON report here")
    args = ap.parse_args()

    turns = parse_player_turns(args.log)
    if args.limit:
        turns = turns[:args.limit]
    if not turns:
        print("no player turns found in the log", file=sys.stderr)
        return 2

    health = get(args.base, "/health")
    print(f"engine   : {health['engine']}")
    print(f"module   : {health['module_dir']} ({health['status']})")
    print(f"replaying: {len(turns)} player turns from {args.log}")
    print("-" * 72)

    session = post(args.base, "/session", {
        "chat_id": args.chat_id,
        "party": [{"id": args.pregen, "name": args.name}],
        "reset": True,
    })
    print(f"campaign : {session['campaign_dir']}")
    print("-" * 72)

    # The opening turn is the bot's own, not the player's: /start hands the DM
    # this instruction before the player has typed anything, so a replay that
    # skips it starts the session mid-scene with no arrival.
    script = [("(открытие сессии)",
               "Начинаем игру. Это первый ход первой сессии — открой "
               "приключение сценой прибытия на остров.")] + \
             [(t, t) for t in turns]

    results, t_start = [], time.monotonic()
    for i, (shown, sent) in enumerate(script, 1):
        t0 = time.monotonic()
        try:
            r = post(args.base, "/turn", {"chat_id": args.chat_id, "text": sent})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            print(f"{i:3}/{len(script)}  HTTP {e.code}  {body}")
            results.append({"n": i, "player": shown, "error": f"HTTP {e.code}: {body}",
                            "flags": ["error"], "narration": "", "rolls": [],
                            "tool_calls": [], "seconds": round(time.monotonic() - t0, 1)})
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"{i:3}/{len(script)}  transport error: {e}")
            results.append({"n": i, "player": shown, "error": str(e),
                            "flags": ["error"], "narration": "", "rolls": [],
                            "tool_calls": [], "seconds": round(time.monotonic() - t0, 1)})
            continue

        flags = grade(r["narration"], r["rolls"])
        results.append({
            "n": i, "player": shown, "narration": r["narration"],
            "maps": r["maps"], "rolls": r["rolls"], "tool_calls": r["tool_calls"],
            "chars": r["chars"], "seconds": r["seconds"], "flags": flags,
        })
        mark = "ok " if not flags else "!! "
        print(f"{i:3}/{len(script)}  {mark} {r['seconds']:6.1f}s  "
              f"{r['chars']:5}ch  rolls={len(r['rolls'])} "
              f"tools={len(r['tool_calls'])}"
              + (f"  maps={r['maps']}" if r["maps"] else "")
              + (f"  FLAGS={','.join(flags)}" if flags else ""))

    total = time.monotonic() - t_start

    # ── transcript, in the same shape the bot writes ──────────────────────
    lines = [f"# Replay of {args.log.name} — {health['engine']}",
             f"*{len(results)} turns, {total / 60:.1f} min*", ""]
    for r in results:
        lines.append(f"> Player:\n{r['player']}\n")
        if r.get("error"):
            lines.append(f"> DnD Master:\n[ОШИБКА: {r['error']}]\n")
            continue
        for roll in r["rolls"]:
            lines.append(f"`roll {roll['notation']} "
                         f"{roll.get('label', '')}` → {roll['output'].strip()}")
        if r["rolls"]:
            lines.append("")
        lines.append(f"> DnD Master:\n{r['narration']}\n")
        if r["maps"]:
            lines.append(f"🖼 maps: {r['maps']}\n")
    args.out.write_text("\n".join(lines), encoding="utf-8")

    # ── summary ──────────────────────────────────────────────────────────
    from collections import Counter
    flagged = Counter(f for r in results for f in r["flags"])
    graded = [r for r in results if not r.get("error")]
    ok = sum(1 for r in results if not r["flags"])
    rolls = sum(len(r["rolls"]) for r in results)
    print("-" * 72)
    print(f"turns        : {len(results)}  ({ok} clean, {len(results) - ok} flagged)")
    print(f"wall clock   : {total / 60:.1f} min  "
          f"(median {sorted(r['seconds'] for r in graded)[len(graded) // 2]:.1f}s/turn)"
          if graded else "")
    print(f"dice rolled  : {rolls} through the script")
    print(f"tool calls   : {sum(len(r['tool_calls']) for r in results)}")
    print(f"flags        : {dict(flagged) or 'none'}")
    print(f"transcript   : {args.out}")

    if args.report:
        args.report.write_text(json.dumps(
            {"engine": health["engine"], "log": str(args.log),
             "turns": len(results), "clean": ok, "flags": dict(flagged),
             "rolls": rolls, "seconds": round(total, 1), "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report       : {args.report}")

    # Only dice dishonesty and hard errors fail the run. The cosmetic flags are
    # reported for reading, not as a gate — a single stray heading in 33 turns
    # of improvised prose is a note, not a broken build.
    fatal = flagged["invented_dice"] + flagged["error"] + flagged["empty"]
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
