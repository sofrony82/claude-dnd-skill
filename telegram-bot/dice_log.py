"""
dice_log.py — the rolls of one turn, as the player sees them.

The prompt tells the DM that dice mean what they show. A live session showed
that this is not enough: the model rolled a zombie's critical hit that would
have dropped the only character, spent its whole reply reasoning about it, and
the narration that finally came out simply skipped those rolls. The dice were
real; the player never saw them.

So the bot, not the model, shows every roll the sandbox executed this turn,
under the narration. A roll the story ignores is now visible as one. Rolls the
DM marks `hidden` (an enemy's Stealth, a check whose number would give a secret
away) are shown as a secret roll without label or result — the player still
sees that something was rolled.

The same summary goes back to the model when a turn has to be nudged, so a
retry narrates the dice it already has instead of rolling them again.
"""

import html
import re

TITLE = "🎲 Броски за ход"
HIDDEN = "тайный бросок мастера"

# A combat round with a dozen rolls fits; a runaway turn does not flood the chat.
MAX_LINES = 30

_NAT = (("CRITICAL HIT", "натуральная 20"), ("FUMBLE", "натуральная 1"))


def summarize(output: str) -> str:
    """dice.py output -> one short line: '9 + 3 = 12', '14 и 6 → 17'."""
    out = (output or "").strip()
    if not out or out.startswith(("ОШИБКА", "Скрипт вернул")):
        return "бросок не удался"
    out = out.replace(" [auto]", "")
    nat = next((ru for en, ru in _NAT if en in out), "")
    out = re.sub(r"\s*\*\*\*.*?\*\*\*", "", out)

    a = re.search(r"Roll A: \[(\d+)\]", out)
    b = re.search(r"Roll B: \[(\d+)\]", out)
    total = re.search(r"Total: (-?\d+)", out)
    if a and b and total:
        mode = "преимущество" if "[ADV]" in out else "помеха"
        mod = re.search(r"Roll A: \[\d+\]( [+-] \d+)", out)
        mod = mod.group(1) if mod else ""
        return f"{a.group(1)} и {b.group(1)} ({mode}){mod} → {total.group(1)}"

    kept = re.search(r"Kept \([^)]*\): \[([^\]]*)\](.*)$", out, re.M)
    if kept:
        return f"{kept.group(1)}{kept.group(2)}".strip()

    line = out.splitlines()[0]
    line = re.sub(r"^Rolls?: ", "", line)
    # "[4, 4] + 1 = 9" -> "4 + 4 + 1 = 9"
    line = re.sub(r"\[([\d, ]+)\]",
                  lambda m: " + ".join(x.strip() for x in m.group(1).split(",")), line)
    lhs, eq, rhs = line.partition(" = ")
    if eq and lhs.strip() == rhs.strip():
        line = rhs.strip()                    # a bare d20: "20 = 20" -> "20"
    return f"{line} — {nat}" if nat else line


def lines(rolls: list) -> list:
    """Player-facing lines, one per roll, secrets masked."""
    out = []
    for r in rolls:
        if r.get("hidden"):
            out.append(HIDDEN)
            continue
        name = r.get("label") or r.get("notation") or "бросок"
        out.append(f"{name}: {summarize(r.get('output', ''))}")
    if len(out) > MAX_LINES:
        extra = len(out) - MAX_LINES
        out = out[:MAX_LINES] + [f"…и ещё {extra}"]
    return out


def text(rolls: list) -> str:
    """Plain text for the transcript."""
    body = lines(rolls)
    return f"{TITLE}\n" + "\n".join(body) if body else ""


def to_html(rolls: list) -> str:
    """A collapsed quote under the narration: there if you look, quiet if not."""
    body = lines(rolls)
    if not body:
        return ""
    inner = "\n".join(html.escape(l) for l in body)
    return f"<blockquote expandable><b>{TITLE}</b>\n{inner}</blockquote>"


def for_dm(rolls: list) -> str:
    """The same rolls for the model, secrets included — it made them."""
    return "\n".join(
        f"- {r.get('label') or r.get('notation')}: {summarize(r.get('output', ''))}"
        + (" (тайный)" if r.get("hidden") else "")
        for r in rolls)
