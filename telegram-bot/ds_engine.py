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
)
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

NUDGE = ("Ты не сказал игроку ничего. Опиши результат словами, обычной прозой "
         "по-русски: что произошло, что видит и слышит персонаж, чем кончился "
         "раунд. Не вызывай инструменты — все нужные броски уже сделаны, их "
         "результаты выше. Просто расскажи игроку, что случилось.")

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


class DSSession:
    """A DeepSeek DM agent bound to one chat."""

    def __init__(self, chat_id: int, campaign_dir: pathlib.Path, system_prompt: str):
        self.chat_id = chat_id
        self.campaign_dir = pathlib.Path(campaign_dir)
        self.system_prompt = system_prompt
        self.lock = asyncio.Lock()
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
            if self.client is None:
                await self.start()

            self._turn_marks.append(len(self.history))
            self.history.append({"role": "user", "content": text})
            self._trim()

            narration, preamble = [], []

            for step in range(DS_MAX_STEPS):
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
                    (preamble if calls else narration).append(content)

                if not calls:
                    break

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

                    self.history.append({
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{step}",
                        "content": str(result),
                    })
            else:
                log.warning("chat %s: hit the %s-step ceiling in one turn",
                            self.chat_id, DS_MAX_STEPS)

            # A turn with no narration is a lost turn: the player acted and the
            # world said nothing back. It happens when the model spends the
            # whole reply reasoning, or writes tool syntax where prose belongs,
            # and it is far more common in heavy combat than anywhere else. Ask
            # again rather than hand the player silence.
            for attempt in range(NUDGE_LIMIT):
                if any(c.strip() for c in narration):
                    break
                log.warning("chat %s: empty narration, nudging (%s/%s)",
                            self.chat_id, attempt + 1, NUDGE_LIMIT)
                self.history.append({"role": "user", "content": NUDGE})
                msg = await self._complete()
                text = (msg.get("content") or "").strip()
                self.history.append({"role": "assistant",
                                     "content": msg.get("content") or ""})
                if text:
                    narration.append(text)

            self.turns += 1
            spoken = [c for c in narration if c.strip()] or \
                     [c for c in preamble if c.strip()]
            out = "\n".join(spoken).strip()
            if not out:
                log.error("chat %s: turn produced no narration after %s nudges",
                          self.chat_id, NUDGE_LIMIT)
            return out

    async def _complete(self) -> dict:
        """One chat completion, normalised to a plain dict."""
        resp = await self.client.chat.completions.create(
            model=DS_MODEL,
            messages=self._messages(),
            tools=self.tools,
            tool_choice="auto",
            temperature=DS_TEMPERATURE,
            max_tokens=DS_MAX_TOKENS,
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.total_tokens += getattr(usage, "total_tokens", 0) or 0

        choice = resp.choices[0]
        m = choice.message
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

    async def close(self, chat_id: int):
        s = self._sessions.pop(chat_id, None)
        if s:
            await s.stop()

    async def close_all(self):
        for cid in list(self._sessions):
            await self.close(cid)
