"""
ds_engine.py — the DM as a DeepSeek agent, one long-lived session per chat.

`dm_engine.py` hands the table to the Claude Agent SDK, which owns the agent
loop: it decides when to call a tool, feeds the result back, and stops when the
model is done talking. An OpenAI-compatible endpoint owns none of that. So this
module is that loop, written out, against `deepseek-ai/DeepSeek-V4.1-Flash` on
Nebius Token Factory.

It presents the same surface as `DMSession`/`SessionRegistry` — `start`, `ask`,
`stop`, `maps_shown` — so `bot.py` and the FastAPI server drive either backend
without knowing which one they got.

Three things about this model shape the code:

*Tools are the only honest dice.* The model is perfectly happy to write
"🎲 d20+3 → 17" without rolling anything. The system prompt forbids it and
`roll_dice` exists so it doesn't have to; `Sandbox.rolls` records what actually
ran, which is what the replay test checks.

*It reasons out loud.* Responses carry `reasoning_content` next to `content`.
That text is the model thinking — English, out of character, sometimes quoting
the module's DM-only sections. It never reaches the player and it never goes
back into the history, which is also what DeepSeek's own guidance says to do.

*Text next to a tool call is a preamble.* "Сначала прочитаю модуль" is the
model narrating to itself. Only content from a message that calls no tools is
spoken at the table — the same rule the Claude path uses, for the same reason.
"""

import asyncio
import json
import logging
import pathlib
import time
import re

from openai import AsyncOpenAI

from config import (
    DS_BASE_URL,
    DS_KEY,
    DS_MAX_STEPS,
    DS_MAX_TOKENS,
    DS_MODEL,
    DS_TEMPERATURE,
    HISTORY_TURNS,
    SAVE_REMIND_TURNS,
)
import dice_log
import usage
from sandbox import Sandbox, tool_schemas

log = logging.getLogger("dm.ds")

# Tool output kept verbatim in history. Older, larger results are stubbed out
# when the window is trimmed: the DM needs to remember that it read world.md,
# not to carry all 21k tokens of it for the rest of the session.
STUB_OVER = 2_000

# Times a turn that produced no narration is nudged before giving up. A DM that
# silently returns nothing costs the player their action — in a 33-turn replay
# this happened on 7 turns, all of them heavy combat rounds.
NUDGE_LIMIT = 2

# The nudge runs through the full tool loop, not a bare completion: the empty
# turn is sometimes a model that still meant to roll (a death save, the damage
# of a hit it already announced), and a nudge that discards its tool calls
# turns that roll into silence or into invented dice.
NUDGE = ("Ты не сказал игроку ничего. Опиши результат словами, обычной прозой "
         "по-русски: что произошло, что видит и слышит персонаж, чем кончился "
         "раунд. Если нужный бросок ещё не сделан — сделай его через roll_dice, "
         "но закончи ход рассказом игроку.")

# Added to the nudge when the turn has already rolled. Without it a retry
# starts from scratch and rolls again: in the 2026-09-22 session a zombie's
# critical hit and a ghoul's claw were rolled, the reply then ran out of tokens
# reasoning, and the narration that finally came told the player it was their
# turn — those two rolls, which would have dropped the character, never landed.
ROLLS_MADE = ("\n\nВ этом ходу уже брошено — это окончательные результаты, "
              "игрок увидит их под твоим ответом:\n{rolls}\n"
              "Не бросай их заново: расскажи, чем они кончились.")

# A reply cut off by the token limit ends mid-sentence in front of the player.
# Ask for the rest, a bounded number of times, and glue it on.
CONTINUE_LIMIT = 2

CONTINUE = ("Твой ответ оборвался на полуслове. Продолжи ровно с места обрыва: "
            "не повторяй уже написанное и не начинай заново.")

# Appended to the player's message once the DM has gone SAVE_REMIND_TURNS turns
# without writing state.md. It rides on a real turn rather than costing one of
# its own, so the player waits a few seconds longer instead of a whole round.
SAVE_REMINDER = ("[Служебно, игроку не показывать: состояние кампании не "
                 "записывалось уже {n} ходов. Прежде чем отвечать, обнови "
                 "state.md (сцена и локация, хиты, потраченные ячейки и ресурсы, "
                 "добыча, квесты) и лист персонажа, затем веди ход как обычно. "
                 "О сохранении в ответе не упоминай.]")

# The model occasionally writes its own tool-call syntax into `content` instead
# of returning a structured tool call — `<｜DSML｜ invoke name="roll_dice">…`.
# That is not narration and must never reach a player, so it is stripped and the
# turn is treated as having produced nothing.
_PAIRED_MARKUP = re.compile(
    r"<\s*(tool_call|function_call|invoke|antml:\w+)[^>]*>.*?<\s*/\s*\1\s*>",
    re.I | re.S)
_DSML_START = re.compile(r"<[｜|]\s*DSML\s*[｜|]")
_LONE_MARKUP = re.compile(
    r"<\s*/?\s*(?:tool_call|function_call|invoke|parameter|antml:\w+)[^>]*>", re.I)


def _strip_tool_markup(text):
    """(clean_text, leaked?) — remove tool-call syntax the model wrote as prose.

    The tags' *contents* go too, not just the tags: what sits inside is the
    arguments it meant to pass, and half a serialised argument list reads worse
    to a player than the tags did.
    """
    if not text:
        return "", False
    cleaned = _PAIRED_MARKUP.sub("", text)
    # The DSML block is emitted as a trailing run, so everything from its first
    # marker onward is argument soup rather than narration.
    m = _DSML_START.search(cleaned)
    if m:
        cleaned = cleaned[:m.start()]
    cleaned = _LONE_MARKUP.sub("", cleaned)
    return cleaned.strip(), cleaned.strip() != text.strip()


def _glue(head: str, tail: str) -> str:
    """Join a reply that was cut off to its continuation.

    Both halves arrive stripped, so the space at the cut is gone; put it back
    unless the continuation opens with punctuation that belongs to the head.
    """
    if tail[:1] in ".,;:!?…»)":
        return head + tail
    return f"{head} {tail}"


def _describe_call(name: str, args: dict, result) -> str:
    """One journal line per tool call: what was asked, what came back.

    Dice get their notation, label and full result — that is what lets an
    operator check a roll the DM narrated against the one it actually made.
    Everything else gets its target and the first line of the result.
    """
    if name == "roll_dice":
        # Every line: an advantage roll's second die is on the second line.
        secret = (" (тайный)" if str(args.get("hidden", "")).strip().lower()
                  in ("true", "1", "yes") else "")
        return (f"{name} {args.get('notation', '')} «{args.get('label', '')}»{secret}"
                f" -> {' | '.join(str(result).strip().splitlines())}")
    else:
        what = str(args.get("path") or args.get("pattern") or args.get("command") or "")
    first = (str(result).strip().splitlines() or [""])[0]
    if len(first) > 160:
        first = first[:160] + "…"
    return f"{name} {what} -> {first}"


class DSSession:
    """A DeepSeek DM agent bound to one chat."""

    def __init__(self, chat_id: int, campaign_dir: pathlib.Path, system_prompt: str):
        self.chat_id = chat_id
        self.campaign_dir = pathlib.Path(campaign_dir)
        self.system_prompt = system_prompt
        self.lock = asyncio.Lock()
        # When the chat last had this session do anything; see Registry.idle().
        self.last_used = time.monotonic()
        self.turns = 0
        self.total_cost = 0.0          # kept for interface parity; Nebius bills elsewhere
        self.total_tokens = 0
        self.maps_shown: set = set()
        self.sandbox = Sandbox(self.campaign_dir)
        self.tools = tool_schemas(self.campaign_dir)
        self.history: list = []
        self.client: AsyncOpenAI | None = None
        # Where each player turn starts in `history`, for whole-turn trimming.
        self._turn_marks: list = []
        # Player turns since the DM last wrote state.md.
        self.turns_unsaved = 0
        # Completions left in the current player turn, nudges included.
        self._steps_left = 0
        # Every roll the last turn made, for the dice log under the narration.
        self.last_rolls: list = []

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self):
        if not DS_KEY:
            raise SystemExit(
                "No Nebius token. Set NB_STUDIO_API_KEY in the environment or in "
                "telegram-bot/.env")
        self.client = AsyncOpenAI(
            base_url=DS_BASE_URL, api_key=DS_KEY,
            timeout=300.0, max_retries=4,
        )
        log.info("chat %s: DeepSeek DM session ready (%s)", self.chat_id, DS_MODEL)

    async def stop(self):
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as e:                  # noqa: BLE001
                log.warning("chat %s: client close failed: %s", self.chat_id, e)
            self.client = None

    # ── history window ───────────────────────────────────────────────────
    def _messages(self) -> list:
        return [{"role": "system", "content": self.system_prompt}] + self.history

    def _trim(self):
        """Keep the last HISTORY_TURNS player turns whole; stub big old results.

        Trimming has to cut on turn boundaries. A `tool` message is only valid
        directly after the `assistant` message whose `tool_calls` it answers, so
        slicing the list at an arbitrary point produces a request the API
        rejects outright.
        """
        if len(self._turn_marks) > HISTORY_TURNS:
            cut = self._turn_marks[-HISTORY_TURNS]
            self.history = self.history[cut:]
            self._turn_marks = [m - cut for m in self._turn_marks[-HISTORY_TURNS:]]

        keep_from = self._turn_marks[-2] if len(self._turn_marks) >= 2 else 0
        for i, m in enumerate(self.history):
            if i >= keep_from:
                break
            if m.get("role") == "tool" and len(m.get("content") or "") > STUB_OVER:
                m["content"] = (m["content"][:400]
                                + f"\n[…результат обрезан при сжатии истории; "
                                  f"было {len(m['content'])} знаков. "
                                  f"Прочитай файл заново, если нужны детали.]")

    # ── one turn ─────────────────────────────────────────────────────────
    async def ask(self, text: str, on_progress=None) -> str:
        """Send one player turn, return the DM's narration."""
        async with self.lock:
            self.last_used = time.monotonic()
            if self.client is None:
                await self.start()

            self._turn_marks.append(len(self.history))
            self.history.append({"role": "user",
                                 "content": self._with_save_reminder(text)})
            self._trim()

            writes_before = len(self.sandbox.writes)
            rolls_before = len(self.sandbox.rolls)
            self.last_rolls = []
            narration, preamble = [], []
            self._steps_left = DS_MAX_STEPS
            await self._run(narration, preamble, on_progress)

            # A turn with no narration is a lost turn: the player acted and the
            # world said nothing back. It happens when the model spends the
            # whole reply reasoning, or writes tool syntax where prose belongs,
            # and it is far more common in heavy combat than anywhere else. Ask
            # again rather than hand the player silence.
            for attempt in range(NUDGE_LIMIT):
                if any(c.strip() for c in narration) or self._steps_left <= 0:
                    break
                log.warning("chat %s: empty narration, nudging (%s/%s)",
                            self.chat_id, attempt + 1, NUDGE_LIMIT)
                made = self.sandbox.rolls[rolls_before:]
                nudge = NUDGE + (ROLLS_MADE.format(rolls=dice_log.for_dm(made))
                                 if made else "")
                self.history.append({"role": "user", "content": nudge})
                await self._run(narration, preamble, on_progress)

            self.last_rolls = self.sandbox.rolls[rolls_before:]

            if "state.md" in self.sandbox.writes[writes_before:]:
                self.turns_unsaved = 0
            else:
                self.turns_unsaved += 1

            self.turns += 1
            spoken = [c for c in narration if c.strip()] or \
                     [c for c in preamble if c.strip()]
            out = "\n".join(spoken).strip()
            if not out:
                log.error("chat %s: turn produced no narration after %s nudges",
                          self.chat_id, NUDGE_LIMIT)
            return out

    def _with_save_reminder(self, text: str) -> str:
        if SAVE_REMIND_TURNS <= 0 or self.turns_unsaved < SAVE_REMIND_TURNS:
            return text
        log.info("chat %s: state.md unsaved for %s turns, reminding the DM",
                 self.chat_id, self.turns_unsaved)
        return f"{text}\n\n{SAVE_REMINDER.format(n=self.turns_unsaved)}"

    async def _run(self, narration: list, preamble: list, on_progress=None):
        """The tool loop: complete, run the calls, repeat until the DM speaks.

        Spends `self._steps_left`, which is shared by the first pass and any
        nudges so one turn cannot multiply its ceiling by retrying.
        """
        glue = False          # next narration continues a reply cut mid-sentence
        continues = 0
        while self._steps_left > 0:
            self._steps_left -= 1
            msg = await self._complete()

            content = (msg.get("content") or "").strip()
            calls = msg.get("tool_calls") or []

            # Only `content` and `tool_calls` go back — `reasoning_content`
            # is deliberately dropped here.
            entry = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                entry["tool_calls"] = calls
            self.history.append(entry)

            if content:
                if calls:
                    preamble.append(content)
                elif glue and narration:
                    narration[-1] = _glue(narration[-1], content)
                else:
                    narration.append(content)
            glue = False

            if not calls:
                # Cut with nothing written means the whole reply went on
                # reasoning, which is never sent back — "continue from where
                # you stopped" then has nowhere to continue from, and the model
                # starts the turn over. Return empty-handed; the nudge in `ask`
                # tells it which dice it already rolled.
                if msg.get("finish_reason") == "length" and not content:
                    log.warning("chat %s: reply spent on reasoning, nothing said",
                                self.chat_id)
                    return
                if msg.get("finish_reason") == "length" and continues < CONTINUE_LIMIT:
                    continues += 1
                    log.warning("chat %s: reply cut by the token limit, asking to "
                                "continue (%s/%s)", self.chat_id, continues,
                                CONTINUE_LIMIT)
                    self.history.append({"role": "user", "content": CONTINUE})
                    glue = bool(content)
                    continue
                return

            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name") or "?"
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except json.JSONDecodeError:
                    args = {}
                    result = ("ОШИБКА: аргументы не разобраны как JSON. "
                              "Повтори вызов с корректным JSON.")
                else:
                    if on_progress is not None:
                        with_label = args.get("label") or args.get("path") \
                            or args.get("pattern") or args.get("notation") or ""
                        try:
                            await on_progress(f"{name} {with_label}".strip())
                        except Exception:       # noqa: BLE001 — cosmetic only
                            pass
                    result = await asyncio.to_thread(self.sandbox.run, name, args)

                log.info("chat %s: tool %s", self.chat_id,
                         _describe_call(name, args, result))
                self.history.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{len(self.history)}",
                    "content": str(result),
                })
        else:
            log.warning("chat %s: hit the %s-step ceiling in one turn",
                        self.chat_id, DS_MAX_STEPS)

    async def _complete(self) -> dict:
        """One chat completion, normalised to a plain dict."""
        # The daily budget is checked before every request, not only per turn:
        # turns in several chats at once can spend the per-turn reserve together.
        usage.check()
        kwargs = {}
        if DS_MAX_TOKENS > 0:
            kwargs["max_tokens"] = DS_MAX_TOKENS
        resp = await self.client.chat.completions.create(
            model=DS_MODEL,
            messages=self._messages(),
            tools=self.tools,
            tool_choice="auto",
            temperature=DS_TEMPERATURE,
            **kwargs,
        )
        tokens = getattr(resp, "usage", None)
        if tokens is not None:
            self.total_tokens += getattr(tokens, "total_tokens", 0) or 0
        # The chat id is the player's id: private chats only.
        usage.record(self.chat_id, getattr(tokens, "prompt_tokens", 0),
                     getattr(tokens, "completion_tokens", 0))

        choice = resp.choices[0]
        m = choice.message
        # One line per completion. When a turn goes wrong, this is what tells
        # "cut by the limit" from "chose to say nothing" from "spent it all
        # reasoning" — the three look identical from the chat.
        details = getattr(tokens, "completion_tokens_details", None)
        log.info("chat %s: completion finish=%s tools=%d content=%d chars "
                 "tokens prompt=%s completion=%s reasoning=%s",
                 self.chat_id, choice.finish_reason, len(m.tool_calls or []),
                 len(m.content or ""),
                 getattr(tokens, "prompt_tokens", None),
                 getattr(tokens, "completion_tokens", None),
                 getattr(details, "reasoning_tokens", None))
        if choice.finish_reason not in ("stop", "tool_calls", None):
            log.warning("chat %s: completion ended with finish_reason=%s",
                        self.chat_id, choice.finish_reason)
        calls = []
        for c in (m.tool_calls or []):
            calls.append({
                "id": c.id,
                "type": "function",
                "function": {"name": c.function.name,
                             "arguments": c.function.arguments},
            })
        content, leaked = _strip_tool_markup(m.content)
        if leaked:
            log.warning("chat %s: model wrote tool markup as text instead of "
                        "calling a tool", self.chat_id)
        return {"content": content, "tool_calls": calls,
                "finish_reason": choice.finish_reason, "markup_leak": leaked}


class DSRegistry:
    """Chat id -> DSSession, created on demand. Mirrors SessionRegistry."""

    def __init__(self):
        self._sessions: dict = {}

    def get(self, chat_id: int):
        return self._sessions.get(chat_id)

    async def open(self, chat_id: int, campaign_dir: pathlib.Path,
                   system_prompt: str) -> DSSession:
        await self.close(chat_id)
        s = DSSession(chat_id, campaign_dir, system_prompt)
        await s.start()
        self._sessions[chat_id] = s
        return s

    def idle(self, seconds: float) -> list:
        """Chats whose session has not been used for `seconds` and is not mid-turn."""
        now = time.monotonic()
        return [cid for cid, s in self._sessions.items()
                if now - s.last_used >= seconds and not s.lock.locked()]

    async def close(self, chat_id: int):
        s = self._sessions.pop(chat_id, None)
        if s:
            await s.stop()

    async def close_all(self):
        for cid in list(self._sessions):
            await self.close(cid)
