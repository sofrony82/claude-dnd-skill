"""
chat_lanes.py — one lane per chat: chats run side by side, a chat runs in order.

python-telegram-bot handles updates one at a time unless told otherwise, and a
DM turn takes 20–60 seconds. With more than one player that is a queue for the
whole bot: everyone waits for whoever pressed Enter first.

Plain `concurrent_updates=True` fixes that and breaks something else. A chat's
own updates would then race each other — a button pressed during a turn, a
second message typed before the first one is answered, onboarding steps that
read and write `chat_data`. So updates are run concurrently *across* chats and
strictly in arrival order *within* one, which is exactly how the bot behaved
for a single player before.

A lane also caps how much a chat may queue. Every waiting message becomes a DM
turn of its own, so five messages typed while the DM thinks cost five turns;
past MAX_QUEUED the extra ones are refused with a note instead.
"""

import asyncio
import contextlib
import logging

from telegram import Update
from telegram.ext import BaseUpdateProcessor

log = logging.getLogger("lanes")

# Updates a chat may have in its lane, the running one included.
MAX_QUEUED = 3

BUSY_TEXT = ("⏳ Мастер ещё отвечает на прошлые ходы. "
             "Дождись ответа, потом пиши дальше.")


def _chat_id(update: object):
    chat = getattr(update, "effective_chat", None)
    return chat.id if chat is not None else None


class ChatLanes(BaseUpdateProcessor):
    """Concurrent across chats, sequential within a chat, bounded per chat."""

    def __init__(self, max_concurrent_updates: int = 256, max_queued: int = MAX_QUEUED):
        super().__init__(max_concurrent_updates)
        self.max_queued = max_queued
        self._locks: dict = {}
        self._depth: dict = {}      # chat id -> updates in the lane, running included

    def busy(self, chat_id: int) -> bool:
        """True while the chat has anything running or waiting.

        Checked and acted on without an `await` in between, this is how the
        idle-session sweep avoids closing a session a handler is about to use.
        """
        return self._depth.get(chat_id, 0) > 0

    async def do_process_update(self, update: object, coroutine) -> None:
        chat_id = _chat_id(update)
        if chat_id is None:
            await coroutine
            return

        if self._depth.get(chat_id, 0) >= self.max_queued:
            # Never awaited, so close it or Python warns about it on exit.
            with contextlib.suppress(AttributeError):
                coroutine.close()
            log.info("chat %s: lane full, update refused", chat_id)
            await _refuse(update)
            return

        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        self._depth[chat_id] = self._depth.get(chat_id, 0) + 1
        try:
            async with lock:
                await coroutine
        finally:
            self._depth[chat_id] -= 1
            if not self._depth[chat_id]:
                # Nobody holds or waits on the lock, so it can go.
                del self._depth[chat_id]
                self._locks.pop(chat_id, None)

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


async def _refuse(update: object) -> None:
    if not isinstance(update, Update):
        return
    with contextlib.suppress(Exception):
        if update.callback_query is not None:
            await update.callback_query.answer(BUSY_TEXT, show_alert=False)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(BUSY_TEXT)
