#!/usr/bin/env python3
"""
bot.py — Telegram front end for the D&D DM agent.

Private 1:1 play: one person runs the whole party, which is what the module
itself suggests for a small table. /start walks through picking and naming
characters, then hands the table to the DM agent and gets out of the way.

A player may keep several campaigns: /games lists them and switches the chat
between them, /new starts another without touching the rest, and deleting one
moves it to the trash rather than erasing it (see campaign.py).

Access: admins from TELEGRAM_ALLOWED_USERS, everyone else by request — a
stranger presses "request access", an admin approves with a button (access.py).

Several players at once: updates from different chats run concurrently, a
chat's own updates run in order (chat_lanes.py). Only private chats are
served; the bot leaves any group it is added to.

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

from telegram.error import BadRequest, NetworkError, TimedOut
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    constants,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import access
import campaign
import chat_lanes
import dice_log
import prompts
import tg_format
import transcript
import usage
from config import (
    BACKEND,
    IDLE_CLOSE_MINUTES,
    MAX_PARTY,
    MIN_PARTY,
    MODULE_DIR,
    token,
)
import engine

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

REGISTRY = engine.new_registry()
LANES = chat_lanes.ChatLanes()

# How often the idle-session sweep looks around.
SWEEP_EVERY = 300

# Names used in the written transcript. They match what Telegram shows, so a
# hand-saved chat export and the bot's own log are the same document.
BOT_SPEAKER = transcript.BOT_SPEAKER

SAVE_REQUEST = ("Сохрани состояние: обнови файл состояния (текущая сцена, "
                "локация, квесты, состояние мира) и листы персонажей "
                "(хиты, ресурсы, инвентарь, опыт). Затем подтверди одной "
                "строкой — что именно записано, человеческим языком, без имён "
                "файлов и путей. Сцену не двигай.")

# A save before switching away runs while the player waits on a status line.
# Past this, give up: the log tail covers the gap on resume anyway.
LEAVE_SAVE_TIMEOUT = 120

# The "Menu" button beside the input field. /save is left out on purpose: the
# bot saves on its own, and a button invites the idea that it must be pressed.
MENU = [
    ("games", "Мои кампании — переключиться, новая, корзина"),
    ("recap", "Где мы и что происходит"),
    ("sheet", "Лист персонажа"),
    ("party", "Состав отряда"),
    ("map", "Карта текущей местности"),
    ("new", "Новая кампания (текущая сохранится)"),
    ("help", "Все команды"),
]
# Admins also get this one in their menu.
ADMIN_MENU = MENU + [("requests", "Запросы доступа и игроки"),
                     ("usage", "Расход запросов к модели")]

LIMIT_TEXT = ("🌙 Мастер на сегодня выдохся: у бота кончился дневной запас "
              "запросов к модели. Продолжим завтра — счёт обнуляется в полночь "
              "по Москве.")


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
    "<i>Модуль рассчитан на четверых. Меньше — тоже можно: "
    "мастер ослабит встречи под отряд, но будет опаснее.</i>"
)

HELP = (
    "<b>Команды</b>\n"
    "/start — начать или продолжить игру\n"
    "/games — мои кампании: переключиться или убрать в корзину\n"
    "/new — новая кампания (текущая сохранится)\n"
    "/rename — переименовать текущую кампанию\n"
    "/party — состав отряда\n"
    "/sheet — лист персонажа\n"
    "/map — показать карту текущей местности\n"
    "/recap — краткий пересказ: где мы и что происходит\n"
    "/save — сохранить прямо сейчас (обычно не нужно: бот сохраняет сам)\n"
    "/reset — убрать текущую кампанию в корзину\n"
    "/help — эта справка\n\n"
    "Всё остальное просто пиши текстом — это твой ход. "
    "Можно обобщённо («идём в храм, Дарин осматривает статую») "
    "или репликой от лица персонажа."
)


# ── guards ───────────────────────────────────────────────────────────────
def authorised(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and access.is_allowed(user.id)


REQUEST_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("🙋 Запросить доступ", callback_data="acc:req")]])


async def deny(update: Update):
    """Tell an outsider where they stand, and offer the request button once."""
    st = access.status(update.effective_user.id) if update.effective_user else "none"
    if st == "pending":
        text, kb = ("Запрос уже у владельца. Я напишу, как только придёт ответ.", None)
    elif st == "denied":
        text, kb = ("Владелец пока не открыл тебе доступ.", None)
    else:
        text, kb = ("Этот бот — закрытый стол: играют те, кого пустил владелец. "
                    "Можно попросить доступ.", REQUEST_KB)
    if update.callback_query is not None:
        with contextlib.suppress(Exception):
            await update.callback_query.answer()
    await update.effective_message.reply_text(text, reply_markup=kb)


GROUP_TEXT = ("Я вожу игру только в личных сообщениях — напиши мне напрямую. "
              "Из этого чата я выхожу.")


async def leave_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """One player runs the whole party, so a group has no one to play for.

    Group play would also mean several people steering one campaign and one
    session, which nothing here is built for. Say so once and leave.
    """
    with contextlib.suppress(Exception):
        await context.bot.send_message(chat_id, GROUP_TEXT)
    with contextlib.suppress(Exception):
        await context.bot.leave_chat(chat_id)
    log.info("chat %s: not a private chat, left", chat_id)


async def admitted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """The gate every handler passes: a private chat with an allowed user."""
    chat = update.effective_chat
    if chat is None or chat.type != constants.ChatType.PRIVATE:
        if update.callback_query is not None:
            with contextlib.suppress(Exception):
                await update.callback_query.answer()
        if chat is not None:
            await leave_group(context, chat.id)
        return False
    if not authorised(update):
        await deny(update)
        return False
    return True


async def on_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Leave a group or channel as soon as someone adds the bot to it."""
    change = update.my_chat_member
    if change is None or change.chat.type == constants.ChatType.PRIVATE:
        return
    if change.new_chat_member.status in (constants.ChatMemberStatus.MEMBER,
                                         constants.ChatMemberStatus.ADMINISTRATOR):
        await leave_group(context, change.chat.id)


# ── output helpers ───────────────────────────────────────────────────────
async def send_narration(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                         rolls: list = ()):
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
    await send_rolls(update, context, rolls)

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


async def send_rolls(update: Update, context: ContextTypes.DEFAULT_TYPE, rolls: list):
    """Every roll of the turn, under the narration — see dice_log.py for why."""
    body = dice_log.to_html(rolls)
    if not body:
        return
    chat_id = update.effective_chat.id
    transcript.append(campaign.campaign_dir(chat_id), BOT_SPEAKER, dice_log.text(rolls))
    try:
        await context.bot.send_message(chat_id=chat_id, text=body,
                                       parse_mode=constants.ParseMode.HTML)
    except BadRequest as e:
        # The narration is already out; a refused quote must not cost the turn.
        log.warning("chat %s: dice log refused as HTML (%s), sending plain", chat_id, e)
        await context.bot.send_message(chat_id=chat_id, text=dice_log.text(rolls))


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
    if not usage.can_start_turn():
        await update.effective_message.reply_text(LIMIT_TEXT)
        return
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
                MODULE_DIR, campaign.campaign_dir(chat_id), BACKEND),
        )
    cdir = campaign.campaign_dir(chat_id)
    saved_before = transcript.state_mtime(cdir)
    async with typing(context, chat_id):
        try:
            reply = await session.ask(player_text)
        except usage.LimitReached:
            log.warning("chat %s: daily request budget ran out mid-turn", chat_id)
            await update.effective_message.reply_text(LIMIT_TEXT)
            return
        except Exception:
            # The details are for the log. A player has no use for an exception
            # name, and its message can carry paths, URLs or a provider's error.
            log.exception("chat %s: DM turn failed", chat_id)
            await update.effective_message.reply_text(
                "Мастер поперхнулся — у меня что-то сломалось. "
                "Попробуй повторить ход; если не выйдет, /start.")
            return
    # Only the DeepSeek session collects them; the SDK path has no sandbox.
    rolls = getattr(session, "last_rolls", None) or []
    if reply:
        await send_narration(update, context, reply, rolls)
    else:
        await update.effective_message.reply_text(
            "Мастер промолчал. Повтори ход или уточни, что делаешь.")
        # Dice that were rolled still count, whether or not the DM said so.
        await send_rolls(update, context, rolls)
    # The DM wrote state.md this turn: everything logged so far is covered.
    if transcript.state_mtime(cdir) != saved_before:
        transcript.mark_saved(cdir)


async def save_before_leaving(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              say) -> bool:
    """Have the DM save the active campaign before the chat leaves it.

    Only when there is something to save and a live session that remembers it:
    after a restart the session is gone, and the log tail handed over on resume
    is all that is left to go on anyway. `say` shows a status line. Returns
    whether a save was attempted.
    """
    chat_id = update.effective_chat.id
    session = REGISTRY.get(chat_id)
    cdir = campaign.campaign_dir(chat_id)
    if session is None or not campaign.exists(chat_id) or not transcript.has_unsaved(cdir):
        return False
    await say("💾 Сохраняю кампанию…")
    before = transcript.state_mtime(cdir)
    try:
        async with typing(context, chat_id):
            await asyncio.wait_for(session.ask(SAVE_REQUEST), LEAVE_SAVE_TIMEOUT)
    except Exception as e:                          # noqa: BLE001
        log.warning("chat %s: save before leaving failed: %s: %s",
                    chat_id, type(e).__name__, e)
    if transcript.state_mtime(cdir) != before:
        transcript.mark_saved(cdir)
    else:
        log.warning("chat %s: DM did not write state before leaving; "
                    "the log tail will carry it", chat_id)
    return True


# ── access requests ──────────────────────────────────────────────────────
def _decision_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Пустить", callback_data=f"acc:ok:{user_id}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"acc:no:{user_id}"),
    ]])


async def on_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`acc:*` buttons: a stranger asking in, an admin answering.

    Runs ahead of the usual gate: whoever presses "request access" is by
    definition not allowed yet. Admin buttons check the presser is an admin —
    callback data is client-supplied.
    """
    q = update.callback_query
    chat = update.effective_chat
    if chat is None or chat.type != constants.ChatType.PRIVATE:
        return await admitted(update, context)      # leaves the group
    with contextlib.suppress(Exception):
        await q.answer()
    user = update.effective_user
    data = q.data or ""

    if data == "acc:req":
        if access.is_allowed(user.id):
            await q.edit_message_text("Доступ уже есть — жми /start.")
            return
        if not access.request(user.id, user.full_name or "", user.username or ""):
            return await deny(update)
        await q.edit_message_text(
            "Запрос отправлен владельцу. Я напишу, как только придёт ответ.")
        text = ("🙋 <b>Запрос доступа</b>\n"
                + html.escape(access.label(user.id, {"name": user.full_name,
                                                      "username": user.username})))
        for admin in access.ADMINS:
            # Silent: an admin in the middle of a scene should not get a buzz
            # for it. The request waits in /requests too.
            with contextlib.suppress(Exception):
                await context.bot.send_message(
                    admin, text, parse_mode=constants.ParseMode.HTML,
                    reply_markup=_decision_kb(user.id), disable_notification=True)
        return

    if not access.is_admin(user.id):
        return
    try:
        _, verb, uid = data.split(":", 2)
        uid = int(uid)
    except ValueError:
        return
    if verb == "ok":
        rec = access.approve(uid)
        note = "✅ Пущен"
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                uid, "✅ Владелец открыл тебе доступ. Жми /start — и в путь.")
    elif verb in ("no", "rm"):
        rec = access.refuse(uid)
        note = "❌ Отказано" if verb == "no" else "🚫 Доступ снят"
        if verb == "no":
            with contextlib.suppress(Exception):
                await context.bot.send_message(uid, "Владелец пока не открыл тебе доступ.")
    else:
        return
    await q.edit_message_text(f"{note}: {html.escape(access.label(uid, rec))}",
                              parse_mode=constants.ParseMode.HTML)


async def cmd_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin view: pending requests to decide, players to revoke, recent refusals."""
    if not await admitted(update, context):
        return
    if not access.is_admin(update.effective_user.id):
        await update.effective_message.reply_text(HELP, parse_mode=constants.ParseMode.HTML)
        return
    if not access.ADMINS:
        await update.effective_message.reply_text(
            "TELEGRAM_ALLOWED_USERS не задан — бот открыт всем, запросов нет.")
        return
    lists = access.listing()
    reply = update.effective_message.reply_text
    if not lists["pending"]:
        await reply("Новых запросов нет.")
    for uid, rec in lists["pending"]:
        await reply(f"🙋 {html.escape(access.label(uid, rec))}",
                    parse_mode=constants.ParseMode.HTML, reply_markup=_decision_kb(uid))
    for uid, rec in lists["allowed"]:
        await reply(f"🎲 Играет: {html.escape(access.label(uid, rec))}",
                    parse_mode=constants.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "🚫 Снять доступ", callback_data=f"acc:rm:{uid}")]]))
    for uid, rec in lists["denied"][:10]:
        await reply(f"⛔ Отказано: {html.escape(access.label(uid, rec))}",
                    parse_mode=constants.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "✅ Всё-таки пустить", callback_data=f"acc:ok:{uid}")]]))


def _n(x: int) -> str:
    """12345 -> "12 345"."""
    return f"{x:,}".replace(",", " ")


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin view of the daily request budget: today by player, the last week."""
    if not await admitted(update, context):
        return
    if not access.is_admin(update.effective_user.id):
        await update.effective_message.reply_text(HELP, parse_mode=constants.ParseMode.HTML)
        return
    limit = usage.DAILY_COMPLETIONS
    used = usage.used_today()
    lines = [f"<b>Запросы к модели сегодня</b> ({usage.today()}): {_n(used)}"
             + (f" из {_n(limit)} ({used * 100 // limit}%)" if limit > 0 else " — без лимита")]
    recs = {}
    for section in access.listing().values():
        recs.update(dict(section))
    days = usage.days(7)
    if days and days[0][0] == usage.today():
        top = sorted(days[0][1]["users"].items(), key=lambda kv: kv[1], reverse=True)
        for uid, n in top[:15]:
            who = access.label(int(uid), recs.get(int(uid), {"name": ""}))
            lines.append(f"  {_n(n)} — {html.escape(who)}")
        d = days[0][1]
        lines.append(f"Токены: вход {_n(d['prompt_tokens'])}, выход {_n(d['completion_tokens'])}")
    if len(days) > 1:
        lines.append("\n<b>По дням</b>")
        lines += [f"  {day}: {_n(d['completions'])}" for day, d in days]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=constants.ParseMode.HTML)


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
    if not await admitted(update, context):
        return

    ok, missing = campaign.pack_ready()
    if not ok:
        await update.effective_message.reply_text(
            "Пак модуля не готов — не хватает: " + ", ".join(missing))
        return

    chat_id = update.effective_chat.id
    if not context.chat_data.get(K_STAGE):
        if campaign.exists(chat_id):
            party = campaign.load_party(chat_id)
            names = ", ".join(p["name"] for p in party["party"])
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Продолжить", callback_data="go:continue")],
                [InlineKeyboardButton("➕ Новая кампания", callback_data="go:new"),
                 InlineKeyboardButton("📚 Мои игры", callback_data="games")],
            ])
            await update.effective_message.reply_text(
                f"Сейчас идёт кампания. Отряд: <b>{html.escape(names)}</b>.",
                parse_mode=constants.ParseMode.HTML, reply_markup=kb)
            return
        # Nothing active, but other campaigns exist: let them pick one up.
        if campaign.list_for(update.effective_user.id):
            return await cmd_games(update, context)

    await begin_onboarding(update, context)


async def begin_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           edit: bool = False):
    """Ask for party size. Leaves every existing campaign where it is."""
    context.chat_data.clear()
    context.chat_data[K_STAGE] = "size"
    context.chat_data[K_PARTY] = []
    if edit:
        await update.callback_query.edit_message_text(
            WELCOME, parse_mode=constants.ParseMode.HTML, reply_markup=size_keyboard())
    else:
        await update.effective_message.reply_text(
            WELCOME, parse_mode=constants.ParseMode.HTML, reply_markup=size_keyboard())


# ── several campaigns ────────────────────────────────────────────────────
def games_view(user_id: int, active):
    """Text and keyboard for the campaign list."""
    games = campaign.list_for(user_id)
    if not games:
        return ("Кампаний пока нет.",
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    "➕ Новая кампания", callback_data="go:new")]]))
    lines, rows = ["<b>Твои кампании</b>"], []
    for i, g in enumerate(games, 1):
        mark = " — <i>сейчас играем</i>" if g["id"] == active else ""
        party = ", ".join(f"{p['name']} ({p['klass']})" for p in g["party"])
        when = g["last_played"].strftime("%d.%m %H:%M") if g["last_played"] else "—"
        where = g["location"]
        if len(where) > 90:
            where = where[:89].rstrip() + "…"
        lines.append(f"\n<b>{i}. {html.escape(g['title'])}</b>{mark}\n"
                     f"{html.escape(party)} · {when}"
                     + (f"\n📍 {html.escape(where)}" if where else ""))
        rows.append([
            InlineKeyboardButton(f"{'✅' if g['id'] == active else '▶️'} {i}. {g['title']}"[:40],
                                 callback_data=f"sw:{g['id']}"),
            InlineKeyboardButton(f"🗑 {i}", callback_data=f"rm:{g['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Новая кампания", callback_data="go:new")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def owns(update: Update, campaign_id: str) -> bool:
    """Callback data is client-supplied: check the id names one of this user's.

    Lookups are under the user's own directory, so another player's id simply
    is not there; this only turns "not there" into a clear answer.
    """
    user = update.effective_user
    return bool(user) and campaign.read_party(user.id, campaign_id) is not None


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    text, kb = games_view(update.effective_user.id,
                          campaign.active_id(update.effective_chat.id))
    await update.effective_message.reply_text(
        text, parse_mode=constants.ParseMode.HTML, reply_markup=kb)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    ok, missing = campaign.pack_ready()
    if not ok:
        await update.effective_message.reply_text(
            "Пак модуля не готов — не хватает: " + ", ".join(missing))
        return
    await save_before_leaving(update, context, update.effective_message.reply_text)
    await begin_onboarding(update, context)


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    cid = campaign.active_id(update.effective_chat.id)
    title = " ".join(context.args).strip()[:60] if context.args else ""
    if not cid:
        await update.effective_message.reply_text("Сейчас нет активной кампании. /games")
        return
    if not title:
        await update.effective_message.reply_text(
            "Напиши новое название после команды: /rename Дилион на острове")
        return
    campaign.rename(update.effective_chat.id, cid, title)
    await update.effective_message.reply_text(
        f"Кампания теперь называется <b>{html.escape(title)}</b>.",
        parse_mode=constants.ParseMode.HTML)


async def switch_to(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    q = update.callback_query
    chat_id = update.effective_chat.id
    if cid == campaign.active_id(chat_id) and REGISTRY.get(chat_id) is not None:
        await q.edit_message_text("Эта кампания и так идёт — просто пиши свой ход.")
        return
    await save_before_leaving(update, context, q.edit_message_text)
    # Drop the DM session: it holds the old campaign's history and paths.
    await REGISTRY.close(chat_id)
    campaign.set_active(chat_id, cid)
    context.chat_data.clear()
    title = campaign.summary(chat_id, cid)["title"]
    await q.edit_message_text(f"Переключаюсь на «{title}»…")
    await run_turn(update, context,
                   "Игрок вернулся к игре. Кратко напомни, где отряд "
                   "и что происходит, затем продолжи сцену.")


async def trash_campaign(update: Update, cid: str) -> None:
    chat_id = update.effective_chat.id
    if cid == campaign.active_id(chat_id):
        await REGISTRY.close(chat_id)
        campaign.set_active(chat_id, None)
    where = campaign.trash(chat_id, cid)
    log.info("chat %s: campaign %s moved to %s", chat_id, cid, where)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    q = update.callback_query
    # A press made during a long turn waits in the chat's lane, and Telegram
    # refuses to answer a query that old. The press itself is still good.
    with contextlib.suppress(Exception):
        await q.answer()
    data = q.data or ""

    if data == "games":
        text, kb = games_view(update.effective_user.id,
                              campaign.active_id(update.effective_chat.id))
        await q.edit_message_text(text, parse_mode=constants.ParseMode.HTML,
                                  reply_markup=kb)
        return

    if data.startswith(("sw:", "rm:", "rmy:")):
        action, cid = data.split(":", 1)
        if not owns(update, cid):
            await q.edit_message_text("Такой кампании нет. /games")
            return
        if action == "sw":
            await switch_to(update, context, cid)
        elif action == "rm":
            title = campaign.summary(update.effective_chat.id, cid)["title"]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Да, в корзину", callback_data=f"rmy:{cid}"),
                InlineKeyboardButton("↩️ Назад", callback_data="games"),
            ]])
            await q.edit_message_text(
                f"Убрать «{title}» в корзину? Файлы не стираются — "
                "владелец бота сможет вернуть.", reply_markup=kb)
        else:
            await trash_campaign(update, cid)
            text, kb = games_view(update.effective_user.id,
                                  campaign.active_id(update.effective_chat.id))
            await q.edit_message_text("🗑 Убрано в корзину.\n\n" + text,
                                      parse_mode=constants.ParseMode.HTML,
                                      reply_markup=kb)
        return

    if data.startswith("go:"):
        if data == "go:reset":
            cid = campaign.active_id(update.effective_chat.id)
            if cid:
                await trash_campaign(update, cid)
            await begin_onboarding(update, context, edit=True)
        elif data == "go:new":
            ok, missing = campaign.pack_ready()
            if not ok:
                await q.edit_message_text(
                    "Пак модуля не готов — не хватает: " + ", ".join(missing))
                return
            await save_before_leaving(update, context, q.edit_message_text)
            await begin_onboarding(update, context, edit=True)
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
    if not await admitted(update, context):
        return
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
                prompts.onboarding_summary(saved["party"]), MODULE_DIR, cdir,
                BACKEND))
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
    if not await admitted(update, context):
        return
    await update.effective_message.reply_text(HELP, parse_mode=constants.ParseMode.HTML)


async def cmd_party(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    party = campaign.load_party(update.effective_chat.id)
    if not party:
        await update.effective_message.reply_text("Игра не начата. /start")
        return
    lines = [f"• <b>{html.escape(p['name'])}</b> — {p['race']} {p['klass']}"
             for p in party["party"]]
    await update.effective_message.reply_text(
        "<b>Отряд</b>\n" + "\n".join(lines), parse_mode=constants.ParseMode.HTML)


async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    log_player(update)
    arg = " ".join(context.args).strip() if context.args else ""
    await run_turn(update, context,
                   (f"Покажи текущий лист персонажа {arg}." if arg else
                    "Покажи краткие листы всех персонажей отряда: хиты, КД, "
                    "ключевые броски, ячейки заклинаний, заметный инвентарь.")
                   + " Возьми данные из файлов в characters/, не по памяти. "
                     "Это служебный запрос игрока, а не ход — не двигай сцену.")


async def cmd_map(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    log_player(update)
    if context.args and context.args[0].isdigit():
        await send_map(update, context, int(context.args[0]))
        return
    await run_turn(update, context,
                   "Игрок просит карту текущей местности. Определи, где сейчас "
                   "отряд, и выведи маркер нужной карты. Опиши коротко, что на "
                   "ней видно. Сцену не двигай.")


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    log_player(update)
    await run_turn(update, context,
                   "Сделай краткий пересказ: где отряд, что уже произошло, какие "
                   "зацепки открыты, чего ждут от партии. Пять-семь предложений. "
                   "Сцену не двигай.")


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    log_player(update)
    await run_turn(update, context, SAVE_REQUEST)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admitted(update, context):
        return
    if not campaign.exists(update.effective_chat.id):
        await update.effective_message.reply_text("Сейчас нет активной кампании. /games")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Да, в корзину и начать новую", callback_data="go:reset"),
    ]])
    await update.effective_message.reply_text(
        "Убрать текущую кампанию в корзину и собрать новый отряд? "
        "Другие кампании не пострадают, а эту владелец бота сможет вернуть.",
        reply_markup=kb)


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
    chat = getattr(update, "effective_chat", None)
    if chat is not None and chat.type == constants.ChatType.PRIVATE:
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                chat.id, "Что-то пошло не так. Попробуй ещё раз или /start.")


async def sweep_idle_sessions():
    """Close DM sessions nobody has used for IDLE_CLOSE_MINUTES.

    Safe because resuming does not need them: the next message reopens the
    session from state.md and the raw-log tail. A chat with anything in its
    lane is skipped, and `busy` and `close` run with no await between them, so
    no handler can pick the session up in the gap.
    """
    idle = IDLE_CLOSE_MINUTES * 60
    while True:
        await asyncio.sleep(SWEEP_EVERY)
        try:
            for chat_id in REGISTRY.idle(idle):
                if LANES.busy(chat_id):
                    continue
                await REGISTRY.close(chat_id)
                log.info("chat %s: DM session idle for %s min, closed",
                         chat_id, IDLE_CLOSE_MINUTES)
        except Exception:                           # noqa: BLE001
            log.exception("idle sweep failed")


async def post_init(app: Application):
    if IDLE_CLOSE_MINUTES > 0:
        app.bot_data["sweeper"] = asyncio.create_task(sweep_idle_sessions())
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in MENU])
        for admin in access.ADMINS:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in ADMIN_MENU],
                                          scope=BotCommandScopeChat(admin))
    except Exception as e:                          # noqa: BLE001
        log.warning("could not set the command menu: %s", e)


async def post_shutdown(app: Application):
    sweeper = app.bot_data.get("sweeper")
    if sweeper is not None:
        sweeper.cancel()
    await REGISTRY.close_all()


def main():
    _lock = acquire_single_instance_lock()  # noqa: F841 — held for process life

    campaign.migrate_flat_layout()

    ok, missing = campaign.pack_ready()
    if not ok:
        print(f"⚠  Module pack {MODULE_DIR} is incomplete — missing: {', '.join(missing)}",
              file=sys.stderr)
        print("   The bot will start, but /start will refuse until the pack is built.",
              file=sys.stderr)

    app = (Application.builder()
           .token(token())
           .concurrent_updates(LANES)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("games", cmd_games))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("party", cmd_party))
    app.add_handler(CommandHandler("sheet", cmd_sheet))
    app.add_handler(CommandHandler("map", cmd_map))
    app.add_handler(CommandHandler("recap", cmd_recap))
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CallbackQueryHandler(on_access, pattern=r"^acc:"))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(on_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("DM engine:    %s", engine.describe())
    log.info("Module pack: %s", MODULE_DIR)
    log.info("Players:     %s", campaign.USERS_DIR)
    log.info("Polling…")
    # Pending updates are kept: a move typed while the bot restarts is answered
    # once it is back, instead of vanishing without a reply. A backlog after a
    # long outage is bounded by the per-chat lane (chat_lanes.MAX_QUEUED).
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
