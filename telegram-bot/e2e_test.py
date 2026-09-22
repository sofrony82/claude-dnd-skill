#!/usr/bin/env python3
"""
e2e_test.py — is the whole stack still working after a change?

The unit suite (`tests/test_telegram_bot.py`) covers the parts that are pure
logic: message shaping, path containment, the shell allowlist. It cannot catch
the failures that actually take this bot down, because every one of them lives
in the wiring: a missing key, a module pack that moved, a prompt that names a
tool the backend does not have, a model that stopped returning tool calls.

So this script exercises the real thing — real config, real module pack, real
sandbox, and (unless `--offline`) the real model behind the real HTTP server.

Two tiers, because they have different costs:

    --offline   No model calls, no server, no money, ~2 seconds. Everything
                that can be known without asking DeepSeek anything. Run this
                on every change; it is the gate that catches broken wiring.

    (default)   The offline checks, then a live session against a running
                api_server: real turns, real dice, real files on disk.
                Costs tokens and about a minute.

A nondeterministic DM makes assertion design the hard part. Anything the model
legitimately gets to decide — whether a scene needs a roll, whether to show a
map — is reported as WARN, never FAIL. A test that fails because the DM chose
not to roll this time trains you to ignore it, which is worse than no test.
FAIL is reserved for things that are wrong no matter how the scene went:

    * narration came back empty, or not in Russian
    * a filesystem path or tool name reached the player
    * the prose claims dice the sandbox never rolled
    * the campaign directory or its files were not written
    * the DM answered without ever consulting the module

Usage:
    python3 e2e_test.py --offline                  # fast gate, no model calls
    python3 e2e_test.py                            # full, against localhost:8000
    python3 e2e_test.py --base http://127.0.0.1:8000 --chat-id 991700001
    python3 e2e_test.py --keep                     # leave the campaign for inspection

Exit code is 0 only if every FAIL-level check passed.
"""

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"
_MARK = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn ", INFO: "  ··  "}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, name, detail=""):
        self.rows.append((level, name, detail))
        print(f"[{_MARK[level]}] {name}" + (f"  — {detail}" if detail else ""))
        return level != FAIL

    @property
    def failures(self):
        return [r for r in self.rows if r[0] == FAIL]

    @property
    def warnings(self):
        return [r for r in self.rows if r[0] == WARN]

    def summary(self):
        n = len(self.rows)
        f, w = len(self.failures), len(self.warnings)
        print("-" * 72)
        print(f"{n} checks — {n - f - w} ok, {w} warn, {f} FAIL")
        for _lvl, name, detail in self.failures:
            print(f"  FAIL: {name} — {detail}")
        return 1 if f else 0


# ── tier 1: offline ──────────────────────────────────────────────────────
def offline_checks(r: Report, want_backend: str | None) -> None:
    print("── offline: wiring, config, pack, sandbox " + "─" * 28)

    try:
        import config
    except Exception as e:                          # noqa: BLE001
        r.add(FAIL, "config imports", f"{type(e).__name__}: {e}")
        return
    r.add(PASS, "config imports")

    backend = want_backend or config.BACKEND
    r.add(INFO, "backend", backend)

    # Secrets: presence only. Never print or log a key.
    if backend == "deepseek":
        r.add(PASS if config.DS_KEY else FAIL, "NB_STUDIO_API_KEY present",
              "" if config.DS_KEY else "set it in .env or the environment")
    try:
        tok = config.token(required=False)
        r.add(PASS if tok else WARN, "Telegram token present",
              "" if tok else "bot.py will refuse to start; the API server will not")
    except SystemExit as e:
        r.add(FAIL, "Telegram token well-formed", str(e))

    # Module pack — the single most common cause of a bot that starts and then
    # refuses every /start.
    import campaign
    ok, missing = campaign.pack_ready()
    r.add(PASS if ok else FAIL, "module pack complete",
          str(config.MODULE_DIR) if ok else f"missing: {', '.join(missing)}")
    pregens = campaign.available_pregens()
    r.add(PASS if pregens else FAIL, "pregens available", f"{len(pregens)} sheets")
    maps = sorted((config.MODULE_DIR / "maps").glob("map-*-*"))
    imgs = [m for m in maps if m.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    r.add(PASS if imgs else WARN, "map images present", f"{len(imgs)} images")

    # The engine selector must resolve without importing the other backend's
    # heavy optional dependency.
    try:
        import engine
        r.add(PASS, "engine selector resolves", engine.describe())
    except Exception as e:                          # noqa: BLE001
        r.add(FAIL, "engine selector resolves", f"{type(e).__name__}: {e}")

    # The prompt must name tools the chosen backend actually exposes. Naming a
    # tool the model does not have is how a DM ends up inventing dice.
    try:
        import prompts
        from sandbox import tool_schemas
        sp = prompts.build_system_prompt(
            "- Тест — Высший эльф Волшебник (лист: characters/test.md)",
            config.MODULE_DIR, pathlib.Path("/tmp/x"), backend)
        r.add(PASS if len(sp) > 2000 else FAIL, "system prompt builds",
              f"{len(sp)} chars")
        if backend == "deepseek":
            names = {s["function"]["name"] for s in tool_schemas(pathlib.Path("/tmp/x"))}
            named = {n for n in names if n in sp}
            r.add(PASS if "roll_dice" in named else FAIL,
                  "prompt names the dice tool the backend has",
                  f"referenced: {sorted(named)}")
            r.add(PASS if "Bash" not in sp else FAIL,
                  "prompt does not name SDK-only tools",
                  "" if "Bash" not in sp else "prompt still says Bash")
    except Exception as e:                          # noqa: BLE001
        r.add(FAIL, "system prompt builds", f"{type(e).__name__}: {e}")

    # Sandbox rules. The unit suite covers these in depth; this is the
    # smoke-level assurance that the gate is still wired at all.
    try:
        import tempfile

        import sandbox
        box = sandbox.Sandbox(pathlib.Path(tempfile.mkdtemp()))
        checks = [
            ("read outside roots refused",
             "ОТКАЗАНО" in box.run("read_file", {"path": "/etc/passwd"})),
            ("write outside campaign refused",
             "ОТКАЗАНО" in box.run("write_file", {"path": "/tmp/e2e-evil", "content": "x"})),
            ("module pack read-only",
             "ОТКАЗАНО" in box.run("write_file",
                                   {"path": str(config.MODULE_DIR / "world.md"),
                                    "content": "x"})),
            ("shell chaining refused",
             not sandbox.bash_allowed(
                 f"python3 {config.DND_SKILL_DIR}/scripts/dice.py d20; id")),
            ("helper script allowed",
             sandbox.bash_allowed(
                 f"python3 {config.DND_SKILL_DIR}/scripts/dice.py d20+1")),
            ("write inside campaign works",
             "Записано" in box.run("write_file", {"path": "state.md", "content": "тест"})),
        ]
        for name, ok_ in checks:
            r.add(PASS if ok_ else FAIL, name)
        out = box.run("roll_dice", {"notation": "d20+3", "label": "E2E"})
        rolled = bool(box.rolls) and "Roll" in out
        r.add(PASS if rolled else FAIL, "dice script executes", out.strip()[:50])
    except Exception as e:                          # noqa: BLE001
        r.add(FAIL, "sandbox usable", f"{type(e).__name__}: {e}")

    # The unit suite itself — if it is red, stop here rather than spend tokens.
    try:
        p = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_telegram_bot"],
            cwd=str(HERE.parent), capture_output=True, text=True, timeout=300)
        tail = (p.stderr or "").strip().splitlines()[-1:] or [""]
        r.add(PASS if p.returncode == 0 else FAIL, "unit suite", tail[0])
    except Exception as e:                          # noqa: BLE001
        r.add(WARN, "unit suite", f"could not run: {e}")


# ── tier 2: live ─────────────────────────────────────────────────────────
def _req(url, payload=None, method=None, timeout=600.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def live_checks(r: Report, base: str, chat_id: int, keep: bool) -> None:
    print("\n── live: server, model, dice, disk " + "─" * 35)

    try:
        h = _req(f"{base}/health", timeout=30)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        r.add(FAIL, "API server reachable",
              f"{e} — is dnd-api running? `systemctl --user status dnd-api`")
        return
    r.add(PASS, "API server reachable", h["engine"])
    r.add(PASS if h["status"] == "ok" else FAIL, "server reports pack ok",
          h["status"])

    try:
        s = _req(f"{base}/session",
                 {"chat_id": chat_id,
                  "party": [{"id": "wizard-elf", "name": "Тестомаг"}],
                  "reset": True}, timeout=120)
    except urllib.error.HTTPError as e:
        r.add(FAIL, "session creates", e.read().decode("utf-8", "replace")[:200])
        return
    cdir = pathlib.Path(s["campaign_dir"])
    r.add(PASS, "session creates", str(cdir))

    # Onboarding must land on disk, or a restart loses the table.
    for rel in ("party.json", "state.md", "session-log.md"):
        r.add(PASS if (cdir / rel).is_file() else FAIL, f"{rel} written")
    sheets = list((cdir / "characters").glob("*.md"))
    r.add(PASS if sheets else FAIL, "character sheet written",
          sheets[0].name if sheets else "none")

    import replay_log  # reuse the grader: one definition of "leaked"

    def turn(text, label, timeout=600.0):
        t0 = time.monotonic()
        try:
            resp = _req(f"{base}/turn", {"chat_id": chat_id, "text": text},
                        timeout=timeout)
        except urllib.error.HTTPError as e:
            r.add(FAIL, label, e.read().decode("utf-8", "replace")[:200])
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            r.add(FAIL, label, f"transport: {e}")
            return None
        dt = time.monotonic() - t0
        flags = replay_log.grade(resp["narration"], resp["rolls"])
        hard = [f for f in flags
                if f in ("empty", "not_russian", "leaked_paths",
                         "leaked_tools", "invented_dice")]
        soft = [f for f in flags if f not in hard]
        r.add(FAIL if hard else PASS, label,
              f"{dt:.0f}s {resp['chars']}ch rolls={len(resp['rolls'])} "
              f"tools={len(resp['tool_calls'])}"
              + (f" HARD={','.join(hard)}" if hard else ""))
        if soft:
            r.add(WARN, f"{label}: style", ",".join(soft))
        return resp

    # 1. Opening turn. The DM must consult the module rather than improvise the
    #    adventure from the model's memory of it.
    first = turn("Начинаем игру. Это первый ход первой сессии — открой "
                 "приключение сценой прибытия на остров.", "turn 1: opening")
    if first is None:
        return
    r.add(PASS if first["tool_calls"] else FAIL, "DM reads the module",
          f"{len(first['tool_calls'])} tool calls")
    if first["maps"]:
        r.add(PASS, "map marker emitted", f"maps={first['maps']}")
    else:
        r.add(WARN, "map marker emitted", "none on the opening turn (DM's call)")

    # 2. A turn that asks for a roll outright. Whether a *scene* needs dice is
    #    the DM's judgement, but an explicit request is not — and this is the
    #    check that catches a backend whose tool calls silently stopped working.
    rolled = turn("Софрони осматривает причал. Брось проверку Внимательности "
                  "и скажи результат.", "turn 2: explicit dice request")
    if rolled is not None:
        if rolled["rolls"]:
            got = rolled["rolls"][0]
            r.add(PASS, "roll executed through the script",
                  f"{got['notation']} → {got['output'].strip()[:40]}")
        else:
            r.add(FAIL, "roll executed through the script",
                  "asked for a roll, sandbox executed none — tool calling broken?")

    # 3. Dice honesty across the whole run: prose must not claim more rolls than
    #    the sandbox performed.
    claimed = sum(len(re.findall("🎲", t["narration"]))
                  for t in (first, rolled) if t)
    executed = sum(len(t["rolls"]) for t in (first, rolled) if t)
    r.add(PASS if claimed <= executed else FAIL, "no invented dice",
          f"{claimed} claimed in prose vs {executed} executed")

    # 4. State persistence — /save is the command players rely on. The text
    #    below is what bot.py's /save actually sends, verbatim: a test that
    #    phrases the request its own way tests its own phrasing. (It also must
    #    not name a file itself, or it invites the leak it is checking for.)
    saved = turn("Сохрани состояние: обнови файл состояния (текущая сцена, "
                 "локация, квесты, состояние мира) и листы персонажей "
                 "(хиты, ресурсы, инвентарь, опыт). Затем подтверди одной "
                 "строкой — что именно записано, человеческим языком, без имён "
                 "файлов и путей. Сцену не двигай.", "turn 3: /save")
    if saved is not None:
        state = (cdir / "state.md")
        r.add(PASS if state.is_file() and state.stat().st_size > 100 else FAIL,
              "state.md holds content", f"{state.stat().st_size} bytes")

    try:
        st = _req(f"{base}/state/{chat_id}", timeout=60)
        r.add(PASS if st["files"]["state.md"] else FAIL, "/state serves the files",
              f"{len(st['files']['characters'])} sheets")
    except Exception as e:                          # noqa: BLE001
        r.add(FAIL, "/state serves the files", str(e))

    # 5. Transcript — the campaign's own record of play.
    try:
        tr = _req(f"{base}/transcript/{chat_id}", timeout=60)
        r.add(PASS if "DnD Master" in tr["text"] else FAIL, "transcript written",
              f"{len(tr['text'])} chars")
    except Exception as e:                          # noqa: BLE001
        r.add(FAIL, "transcript written", str(e))

    # 6. Session teardown must not raise.
    try:
        _req(f"{base}/session/{chat_id}", method="DELETE", timeout=60)
        r.add(PASS, "session closes")
    except Exception as e:                          # noqa: BLE001
        r.add(FAIL, "session closes", str(e))

    if keep:
        r.add(INFO, "campaign kept for inspection", str(cdir))
    else:
        shutil.rmtree(cdir, ignore_errors=True)
        r.add(INFO, "test campaign removed", str(cdir))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="skip every model call and the HTTP server")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--chat-id", type=int, default=991700001,
                    help="campaign namespace for the test; it gets reset")
    ap.add_argument("--backend", choices=("claude", "deepseek"),
                    help="assert the prompt/tool wiring for this backend")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the test campaign directory")
    args = ap.parse_args()

    if args.chat_id < 900000000:
        print("refusing: --chat-id under 900000000 may be a real chat, and the "
              "test resets that campaign. Pick a 99xxxxxxx id.", file=sys.stderr)
        return 2

    r = Report()
    t0 = time.monotonic()
    offline_checks(r, args.backend)

    if args.offline:
        print("\n(offline mode — skipped the live stack)")
    elif r.failures:
        print("\n(offline checks failed — not spending tokens on the live stack)")
    else:
        live_checks(r, args.base.rstrip("/"), args.chat_id, args.keep)

    print(f"\nelapsed {time.monotonic() - t0:.1f}s")
    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
