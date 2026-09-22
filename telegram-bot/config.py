"""
config.py — every tunable the bot has, resolved once at import.

Secrets are never hardcoded and never logged. The Telegram token is looked up
in this order, first hit wins:
    1. $TELEGRAM_BOT_TOKEN
    2. telegram-bot/.env            (TELEGRAM_BOT_TOKEN=...)
    3. <repo>/.telegram_apikey      (the bare token on one line)
"""

import os
import pathlib
import re

BOT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = BOT_DIR.parent
DND_SKILL_DIR = REPO_ROOT / "skills" / "dnd"


def _load_dotenv(path: pathlib.Path) -> None:
    """Minimal .env reader — avoids a dependency for four lines of parsing."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


_load_dotenv(BOT_DIR / ".env")


def token(required: bool = True) -> str:
    """Resolve the bot token.

    Lazy on purpose: importing this module must not require a token, or
    the tests and any tooling that reads settings would need a live secret
    just to start.
    """
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not tok:
        keyfile = REPO_ROOT / ".telegram_apikey"
        if keyfile.is_file():
            tok = keyfile.read_text(encoding="utf-8").strip()
    if not tok:
        if not required:
            return ""
        raise SystemExit(
            "No Telegram token. Set TELEGRAM_BOT_TOKEN, put it in telegram-bot/.env, "
            f"or write it to {REPO_ROOT / '.telegram_apikey'}"
        )
    if not re.match(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$", tok):
        raise SystemExit("Telegram token is present but malformed (expected '<digits>:<secret>').")
    return tok



# ── Data locations ───────────────────────────────────────────────────────
DATA_ROOT = pathlib.Path(
    os.environ.get("DND_CAMPAIGN_ROOT", "~/.claude/dnd")
).expanduser().resolve()
MODULES_DIR = DATA_ROOT / "modules"
CAMPAIGNS_DIR = DATA_ROOT / "campaigns"

MODULE_ID = os.environ.get("DND_MODULE", "stormwreck-isle")
MODULE_DIR = MODULES_DIR / MODULE_ID

# ── Model ────────────────────────────────────────────────────────────────
# Opus by default: this is long-form narration in Russian where prose quality
# is the product. Set DND_MODEL=claude-sonnet-5 to trade some of it for speed.
MODEL = os.environ.get("DND_MODEL", "claude-opus-5")
EFFORT = os.environ.get("DND_EFFORT", "medium")
MAX_TURNS = int(os.environ.get("DND_MAX_TURNS", "40"))

# Who may talk to the bot. Empty = anyone who finds it. A bot token in the
# wrong hands is someone else spending your Claude quota, so set this.
_allow = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
ALLOWED_USERS = {int(x) for x in re.findall(r"-?\d+", _allow)} if _allow else set()

# ── Table rules ──────────────────────────────────────────────────────────
MIN_PARTY, MAX_PARTY = 1, 5
TELEGRAM_LIMIT = 4096          # hard cap per message
CHUNK_TARGET = 3500            # split narration below the cap, on a paragraph

# The module's five pregenerated characters, in the order the book lists them
# (page 2). Filenames must match what the module pack ships in pregens/.
PREGENS = [
    ("cleric-dwarf",   "Жрец",       "Холмовой дварф",        "🛡"),
    ("paladin-human",  "Паладин",    "Человек",               "⚔"),
    ("fighter-elf",    "Воин",       "Лесной эльф",           "🏹"),
    ("rogue-halfling", "Плут",       "Легконогий полурослик", "🗡"),
    ("wizard-elf",     "Волшебник",  "Высший эльф",           "✨"),
]
