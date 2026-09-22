#!/usr/bin/env python3
"""
bot.py — Telegram front end for the D&D DM agent.

Private 1:1 play: one person runs the whole party, which is what the module
itself suggests for a small table. /start walks through picking and naming
characters, then hands the table to the DM agent and gets out of the way.

Run:  python3 bot.py      (see README.md)
"""

import asyncio
import atexit
import contextlib
import fcntl
import html
import logging
import os
import pathlib
import sys

from telegram.error import NetworkError, TimedOut
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    constants,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import campaign
import prompts
import tg_format
import transcript
from config import (
    ALLOWED_USERS,
    MAX_PARTY,
    MIN_PARTY,
    MODULE_DIR,
    token,
)
from dm_engine import SessionRegistry

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

REGISTRY = SessionRegistry()

# Names used in the written transcript. They match what Telegram shows, so a
# hand-saved chat export and the bot's own log are the same document.
BOT_SPEAKER = "DnD Master"


def log_player(update: Update) -> None:
    """Record the player's raw input, commands included."""
    text = (update.effective_message.text or "") if update.effective_message else ""
    chat = update.effective_chat
    if text and chat and campaign.exists(chat.id):
        transcript.append(campaign.campaign_dir(chat.id), player_speaker(update), text)


def player_speaker(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "Игрок"
    return user.first_name or user.username or f"user{user.id}"

# chat_data keys
K_STAGE = "stage"        # None | "size" | "pick" | "name"
K_SIZE = "size"
K_PARTY = "party"        # list of {id, name, klass, race}
K_PENDING = "pending"    # pregen dict awaiting a name

WELCOME = (
    "🐉 <b>Драконы острова Штормокрушений</b>\n"
    "Стартовое приключение D&amp;D 5e. Я веду, ты играешь.\n\n"
    "Твой корабль подходит к острову, о котором ходят скверные слухи. "
    "Но сперва — кто сошёл на берег?\n\n"
    "<b>Сколько персонажей в отряде?</b>\n"
    "<i>Модуль рассчитан на четверых. Меньше — тоже можно, "
    "просто будет опаснее.</i>"
)

HELP = (
    "<b>Команды</b>\n"
    "/start — начать или продолжить игру\n"
    "/party — состав отряда\n"
    "/sheet — лист персонажа\n"
    "/map — показать карту текущей местности\n"
    "/recap — краткий пересказ: где мы и что происходит\n"
    "/save — записать состояние кампании в файлы\n"
    "/reset — стереть кампанию и начать заново\n"
    "/help — эта справка\n\n"
    "Всё остальное просто пиши текстом — это твой ход. "
    "Можно обобщённо («идём в храм, Дарин осматривает статую») "
    "или репликой от лица персонажа."
)


# ── guards ───────────────────────────────────────────────────────────────
def authorised(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USERS)


async def deny(update: Update):
    await update.effective_message.reply_text(
        "Этот бот приватный. Попроси владельца добавить твой Telegram ID."
    )


# ── output helpers ───────────────────────────────────────────────────────
async def send_narration(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Send DM prose: strip map markers, send the maps, chunk the rest."""
    chat_id = update.effective_chat.id
    clean, maps = tg_format.extract_maps(text)

    transcript.append(campaign.campaign_dir(chat_id), BOT_SPEAKER, clean)

    for piece in tg_format.chunk(clean):
        await context.bot.send_message(
            chat_id=chat_id,
            text=tg_format.to_html(piece),
            parse_mode=constants.ParseMode.HTML,
        )

    # A map the player has already been shown is noise, and the DM re-emits the
    # marker more often than it means to — especially right after a restart,
    # when it re-establishes the scene. Track it here rather than trusting the
    # prompt's "once per session".
    session = REGISTRY.get(chat_id)
    for n in maps:
        if session is not None:
            if n in session.maps_shown:
                continue
            session.maps_shown.add(n)
        await send_map(update, context, n, quiet=True)


async def send_map(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   number: int, quiet: bool = False):
    matches = sorted((MODULE_DIR / "maps").glob(f"map-{number}-*"))
    matches = [m for m in matches if m.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    if not matches:
        if not quiet:
            await update.effective_message.reply_text(f"Карты {number} в паке нет.")
        return
    legend = MODULE_DIR / "maps" / f"map-{number}.md"
    caption = None
    if legend.is_file():
        for line in legend.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                caption = html.escape(line[2:].strip())[:1000]
                break
    with open(matches[0], "rb") as fh:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id, photo=fh,
            caption=caption, parse_mode=constants.ParseMode.HTML,
        )
    transcript.append(campaign.campaign_dir(update.effective_chat.id),
                      BOT_SPEAKER, f"\U0001f5bc {caption or f'Карта {number}'}")


@contextlib.asynccontextmanager
async def typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Keep the 'typing…' indicator alive for the length of a DM turn."""
    async def loop():
        try:
            while True:
                await context.bot.send_chat_action(chat_id, constants.ChatAction.TYPING)
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def run_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, player_text: str):
    chat_id = update.effective_chat.id
    session = REGISTRY.get(chat_id)
    if session is None:
        party = campaign.load_party(chat_id)
        if not party:
            await update.effective_message.reply_text(
                "Игра ещё не начата. Набери /start.")
            return
        session = await REGISTRY.open(
            chat_id, campaign.campaign_dir(chat_id),
            prompts.build_system_prompt(
                prompts.onboarding_summary(party["party"]),
                MODULE_DIR, campaign.campaign_dir(chat_id)),
        )
    async with typing(context, chat_id):
        try:
            reply = await session.ask(player_text)
        except Exception as e:
            log.exception("chat %s: DM turn failed", chat_id)
            await update.effective_message.reply_text(
                f"Мастер поперхнулся: {type(e).__name__}: {e}\n"
                "Попробуй повторить ход или /start заново.")
            return
    if reply:
        await send_narration(update, context, reply)
    else:
        await update.effective_message.reply_text(
            "Мастер промолчал. Повтори ход или уточни, что делаешь.")


# ── onboarding ───────────────────────────────────────────────────────────
def size_keyboard():
    row = [InlineKeyboardButton(str(n), callback_data=f"size:{n}")
           for n in range(MIN_PARTY, MAX_PARTY + 1)]
    return InlineKeyboardMarkup([row])


def pregen_keyboard(taken):
    buttons = []
    for p in campaign.available_pregens():
        if p["id"] in taken:
            continue
        buttons.append([InlineKeyboardButton(
            f"{p['emoji']} {p['klass']} — {p['race']}", callback_data=f"pick:{p['id']}")])
    return InlineKeyboardMarkup(buttons)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)

    ok, missing = campaign.pack_ready()
    if not ok:
        await update.effective_message.reply_text(
            "Пак модуля не готов — не хватает: " + ", ".join(missing))
        return

    chat_id = update.effective_chat.id
    if campaign.exists(chat_id) and not context.chat_data.get(K_STAGE):
        party = campaign.load_party(chat_id)
        names = ", ".join(p["name"] for p in party["party"])
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Продолжить", callback_data="go:continue"),
            InlineKeyboardButton("🗑 Начать заново", callback_data="go:reset"),
        ]])
        await update.effective_message.reply_text(
            f"У тебя уже есть кампания. Отряд: <b>{html.escape(names)}</b>.",
            parse_mode=constants.ParseMode.HTML, reply_markup=kb)
        return

    context.chat_data.clear()
    context.chat_data[K_STAGE] = "size"
    context.chat_data[K_PARTY] = []
    await update.effective_message.reply_text(
        WELCOME, parse_mode=constants.ParseMode.HTML, reply_markup=size_keyboard())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data.startswith("go:"):
        if data == "go:reset":
            await REGISTRY.close(update.effective_chat.id)
            campaign.delete(update.effective_chat.id)
            context.chat_data.clear()
            context.chat_data[K_STAGE] = "size"
            context.chat_data[K_PARTY] = []
            await q.edit_message_text(WELCOME, parse_mode=constants.ParseMode.HTML,
                                      reply_markup=size_keyboard())
        else:
            await q.edit_message_text("Возвращаемся к игре…")
            await run_turn(update, context,
                           "Игрок вернулся к игре. Кратко напомни, где отряд "
                           "и что происходит, затем продолжи сцену.")
        return

    if data.startswith("size:"):
        n = int(data.split(":", 1)[1])
        context.chat_data[K_SIZE] = n
        context.chat_data[K_STAGE] = "pick"
        await q.edit_message_text(
            f"Отряд из {n}. Выбери первого персонажа:",
            reply_markup=pregen_keyboard(set()))
        return

    if data.startswith("pick:"):
        pid = data.split(":", 1)[1]
        meta = campaign.pregen_meta(pid)
        if not meta:
            await q.edit_message_text("Такого персонажа в паке нет.")
            return
        context.chat_data[K_PENDING] = meta
        context.chat_data[K_STAGE] = "name"
        await q.edit_message_text(
            f"{meta['emoji']} <b>{meta['klass']}</b>, {meta['race']}.\n\n"
            "Как его зовут? Напиши имя сообщением.",
            parse_mode=constants.ParseMode.HTML)
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    stage = context.chat_data.get(K_STAGE)

    if stage == "name":
        name = text[:40]
        pending = context.chat_data.get(K_PENDING)
        party = context.chat_data.setdefault(K_PARTY, [])
        party.append({"id": pending["id"], "name": name,
                      "klass": pending["klass"], "race": pending["race"]})
        context.chat_data[K_PENDING] = None

        if len(party) < context.chat_data.get(K_SIZE, 1):
            context.chat_data[K_STAGE] = "pick"
            taken = {p["id"] for p in party}
            roster = "\n".join(f"{i + 1}. {p['name']} — {p['race']} {p['klass']}"
                               for i, p in enumerate(party))
            await update.effective_message.reply_text(
                f"Записал.\n\n<b>Отряд пока:</b>\n{html.escape(roster)}\n\n"
                "Выбери следующего:",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=pregen_keyboard(taken))
            return

        # Party complete — build the campaign and open the table.
        context.chat_data[K_STAGE] = None
        chat_id = update.effective_chat.id
        cdir = campaign.create(chat_id, party)
        roster = "\n".join(f"• <b>{html.escape(p['name'])}</b> — {p['race']} {p['klass']}"
                           for p in party)
        opening = ("Отряд собран:\n"
                   + "\n".join(f"• {p['name']} — {p['race']} {p['klass']}" for p in party)
                   + "\n\nЛисты персонажей готовы. Поднимаю паруса…")
        transcript.append(cdir, BOT_SPEAKER, opening)
        await update.effective_message.reply_text(
            f"<b>Отряд собран:</b>\n{roster}\n\n"
            "Листы персонажей готовы. Поднимаю паруса…",
            parse_mode=constants.ParseMode.HTML)

        saved = campaign.load_party(chat_id)
        await REGISTRY.open(
            chat_id, cdir,
            prompts.build_system_prompt(
                prompts.onboarding_summary(saved["party"]), MODULE_DIR, cdir))
        await run_turn(update, context,
                       "Начинаем игру. Это первый ход первой сессии — открой "
                       "приключение сценой прибытия на остров.")
        return

    if stage in ("size", "pick"):
        await update.effective_message.reply_text(
            "Сначала закончим со сбором отряда — жми кнопку выше.")
        return

    log_player(update)
    await run_turn(update, context, text)


# ── commands ─────────────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    await update.effective_message.reply_text(HELP, parse_mode=constants.ParseMode.HTML)


async def cmd_party(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    party = campaign.load_party(update.effective_chat.id)
    if not party:
        await update.effective_message.reply_text("Игра не начата. /start")
        return
    lines = [f"• <b>{html.escape(p['name'])}</b> — {p['race']} {p['klass']}"
             for p in party["party"]]
    await update.effective_message.reply_text(
        "<b>Отряд</b>\n" + "\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    log_player(update)
    arg = " ".join(context.args).strip() if context.args else ""
    await run_turn(update, context,
                   (f"Покажи текущий лист персонажа {arg}." if arg else
                    "Покажи краткие листы всех персонажей отряда: хиты, КД, "
                    "ключевые броски, ячейки заклинаний, заметный инвентарь.")
                   + " Возьми данные из файлов в characters/, не по памяти. "
                     "Это служебный запрос игрока, а не ход — не двигай сцену.")


async def cmd_map(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    log_player(update)
    if context.args and context.args[0].isdigit():
        await send_map(update, context, int(context.args[0]))
        return
    await run_turn(update, context,
                   "Игрок просит карту текущей местности. Определи, где сейчас "
                   "отряд, и выведи маркер нужной карты. Опиши коротко, что на "
                   "ней видно. Сцену не двигай.")


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    log_player(update)
    await run_turn(update, context,
                   "Сделай краткий пересказ: где отряд, что уже произошло, какие "
                   "зацепки открыты, чего ждут от партии. Пять-семь предложений. "
                   "Сцену не двигай.")


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    log_player(update)
    await run_turn(update, context,
                   "Сохрани состояние: обнови state.md (текущая сцена, локация, "
                   "квесты, состояние мира) и листы персонажей в characters/ "
                   "(хиты, ресурсы, инвентарь, опыт). Затем подтверди одной строкой, "
                   "что записано. Сцену не двигай.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return await deny(update)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Да, стереть", callback_data="go:reset"),
    ]])
    await update.effective_message.reply_text(
        "Стереть кампанию со всеми персонажами и прогрессом?", reply_markup=kb)


def acquire_single_instance_lock():
    """Refuse to start if another instance is already polling this token.

    Telegram allows exactly one getUpdates consumer per token; a second one
    does not queue, it kills the first with a Conflict and leaves a process
    alive but deaf. An advisory file lock turns that silent failure into a
    clear refusal at startup.
    """
    lock_path = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "dnd-telegram-bot.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        existing = ""
        with contextlib.suppress(Exception):
            existing = lock_path.read_text().strip()
        raise SystemExit(
            f"Another bot instance is already running (pid {existing or 'unknown'}).\n"
            f"Stop it first — two pollers on one token knock each other offline:\n"
            f"    kill {existing or '<pid>'}"
        )
    fh.write(str(os.getpid()))
    fh.flush()
    atexit.register(fh.close)
    return fh


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    # A dropped connection is weather, not a bug: the host sleeps, wifi moves,
    # Telegram closes an idle socket. python-telegram-bot retries on its own.
    # Logging a ~100-line traceback for each one buries the entries that matter.
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("network hiccup (retrying): %s: %s", type(err).__name__, err)
        return
    log.exception("handler error", exc_info=err)


async def post_shutdown(app: Application):
    await REGISTRY.close_all()


def main():
    _lock = acquire_single_instance_lock()  # noqa: F841 — held for process life

    ok, missing = campaign.pack_ready()
    if not ok:
        print(f"⚠  Module pack {MODULE_DIR} is incomplete — missing: {', '.join(missing)}",
              file=sys.stderr)
        print("   The bot will start, but /start will refuse until the pack is built.",
              file=sys.stderr)

    app = (Application.builder()
           .token(token())
           .post_shutdown(post_shutdown)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("party", cmd_party))
    app.add_handler(CommandHandler("sheet", cmd_sheet))
    app.add_handler(CommandHandler("map", cmd_map))
    app.add_handler(CommandHandler("recap", cmd_recap))
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Module pack: %s", MODULE_DIR)
    log.info("Campaigns:   %s", campaign.CAMPAIGNS_DIR)
    log.info("Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
