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
# users/<user_id>/ holds everything one player owns (see campaign.py)
USERS_DIR = DATA_ROOT / "users"
# The flat layout before that, read only to migrate it.
LEGACY_CAMPAIGNS_DIR = DATA_ROOT / "campaigns"
LEGACY_CHATS_DIR = DATA_ROOT / "chats"

MODULE_ID = os.environ.get("DND_MODULE", "stormwreck-isle")
MODULE_DIR = MODULES_DIR / MODULE_ID

# ── Model ────────────────────────────────────────────────────────────────
# Opus by default: this is long-form narration in Russian where prose quality
# is the product. Set DND_MODEL=claude-sonnet-5 to trade some of it for speed.
MODEL = os.environ.get("DND_MODEL", "claude-opus-5")
EFFORT = os.environ.get("DND_EFFORT", "medium")
MAX_TURNS = int(os.environ.get("DND_MAX_TURNS", "40"))

# Admins: they always play and approve everyone else (see access.py). Empty =
# anyone who finds the bot plays. A bot token in the wrong hands is someone
# else spending your quota, so set this.
_allow = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
ALLOWED_USERS = {int(x) for x in re.findall(r"-?\d+", _allow)} if _allow else set()

# A DM session nobody has written to for this long is closed; the next message
# reopens it from the campaign files and the raw-log tail. An idle SDK session
# is a live subprocess, an idle DeepSeek one a history kept in memory.
IDLE_CLOSE_MINUTES = int(os.environ.get("DND_IDLE_CLOSE_MINUTES", "30"))

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


# ── Backend ──────────────────────────────────────────────────────────────
# "claude"  — the Claude Agent SDK (dm_engine.py); the SDK owns the agent loop.
# "deepseek" — DeepSeek on Nebius Token Factory (ds_engine.py); the loop is ours.
BACKEND = os.environ.get("DND_BACKEND", "claude").strip().lower()

DS_BASE_URL = os.environ.get(
    "NB_BASE_URL", "https://api.tokenfactory.nebius.com/v1").rstrip("/")
DS_MODEL = os.environ.get("DND_DS_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash")

# Warm prose, not code: DeepSeek's own guidance puts creative writing near 1.0,
# and at 0.2 the DM repeats sentence shapes within a single scene.
DS_TEMPERATURE = float(os.environ.get("DND_DS_TEMPERATURE", "0.8"))
# Cap on one completion; 0 means no cap and the model stops when it is done.
# A cap counts reasoning tokens too, and a combat round that reasons long gets
# cut mid-sentence — the narration the player sees simply stops.
DS_MAX_TOKENS = int(os.environ.get("DND_DS_MAX_TOKENS", "0"))

# Tool calls the DM may make inside ONE player turn before the loop gives up.
# Opening a chapter legitimately costs a dozen: world, npcs, arc, the chapter
# source, then a few rolls.
DS_MAX_STEPS = int(os.environ.get("DND_DS_MAX_STEPS", "24"))

# Chat Completions requests the whole bot may make per day, all players
# together (see usage.py); 0 = no limit. The day ends at midnight in USAGE_TZ.
DAILY_COMPLETIONS = int(os.environ.get("DND_DAILY_COMPLETIONS", "50000"))
USAGE_TZ = os.environ.get("DND_USAGE_TZ", "Europe/Moscow")

# Player turns kept verbatim before the window is trimmed on a turn boundary.
HISTORY_TURNS = int(os.environ.get("DND_HISTORY_TURNS", "12"))

# Player turns the DM may go without writing state.md before the loop reminds
# it. The prompt asks for saves at scene boundaries; left alone, the model skips
# them for a whole session and a restart loses everything since the opening.
SAVE_REMIND_TURNS = int(os.environ.get("DND_SAVE_REMIND_TURNS", "4"))


def _nebius_key() -> str:
    """Nebius token, from the environment or a key file next to the repo.

    Same shape as `token()` above: resolved lazily and never logged, so importing
    settings does not require a live secret.
    """
    key = os.environ.get("NB_STUDIO_API_KEY", "").strip()
    if not key:
        for name in (".nebius_apikey", ".nb_studio_apikey"):
            f = REPO_ROOT / name
            if f.is_file():
                key = f.read_text(encoding="utf-8").strip()
                break
    return key


DS_KEY = _nebius_key()
