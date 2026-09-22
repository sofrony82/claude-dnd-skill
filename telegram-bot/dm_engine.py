"""
dm_engine.py — one long-lived Claude agent per chat, acting as the DM.

The agent keeps its conversation across turns, so the table has continuity
without the bot replaying a transcript on every message. Campaign facts that
must survive a restart live in files, not in the conversation.

Safety note. This bot takes input from the open internet and hands it to an
agent that can run shell commands, so tool use is gated by an explicit
allowlist (`_can_use_tool`) rather than run in a bypass mode:
  * file reads  — only inside the campaign directory and the module pack
  * file writes — only inside the campaign directory
  * shell       — only the D&D helper scripts, matched by absolute path, and
                  only about this chat's own campaign
The rules themselves live in `sandbox.py`, shared with the DeepSeek backend.
Anything else is denied with a reason the agent can read and work around.
"""

import asyncio
import logging
import pathlib
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from config import DND_SKILL_DIR, EFFORT, MAX_TURNS, MODEL, MODULE_DIR
from sandbox import (  # noqa: F401 — ALLOWED_SCRIPTS is re-exported
    ALLOWED_SCRIPTS, _under, bash_allowed, check_command, pattern_escapes,
    script_root)

log = logging.getLogger("dm")


class DMSession:
    """A DM agent bound to one Telegram chat."""

    def __init__(self, chat_id: int, campaign_dir: pathlib.Path, system_prompt: str):
        self.chat_id = chat_id
        self.campaign_dir = campaign_dir
        self.system_prompt = system_prompt
        self.client: ClaudeSDKClient | None = None
        self.lock = asyncio.Lock()
        # When the chat last had this session do anything; see Registry.idle().
        self.last_used = time.monotonic()
        self.turns = 0
        self.total_cost = 0.0
        # Maps already sent this session. The system prompt asks the DM to
        # show each one once, but a prompt is a request; this is the rule.
        self.maps_shown: set[int] = set()

    # ── tool gating ──────────────────────────────────────────────────────
    async def _can_use_tool(self, tool: str, params: dict, ctx):
        if tool in ("Read", "Glob", "Grep"):
            target = params.get("file_path") or params.get("path") or str(self.campaign_dir)
            if tool == "Glob" and pattern_escapes(params.get("pattern", "")):
                return PermissionResultDeny(
                    message="Шаблон Glob не может выходить за каталог поиска ('..').")
            if _under(target, self.campaign_dir) or _under(target, MODULE_DIR) \
                    or _under(target, DND_SKILL_DIR):
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Чтение вне кампании и пака запрещено: {target}")

        if tool in ("Write", "Edit", "NotebookEdit"):
            target = params.get("file_path", "")
            if _under(target, self.campaign_dir):
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=(f"Запись разрешена только в каталог кампании "
                         f"({self.campaign_dir}). Файлы модуля менять нельзя."))

        if tool == "Bash":
            why = check_command(params.get("command", ""), self.campaign_dir)
            if why is None:
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=(f"Из Bash {why} "
                         "Для всего остального пользуйся Read/Write."))

        return PermissionResultDeny(message=f"Инструмент {tool} недоступен в этой игре.")

    # The shape-only half of the rule, kept as a method for the tests.
    _bash_allowed = staticmethod(bash_allowed)

    # ── lifecycle ────────────────────────────────────────────────────────
    def build_options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            model=MODEL,
            effort=EFFORT,
            cwd=str(self.campaign_dir),
            add_dirs=[str(MODULE_DIR), str(DND_SKILL_DIR)],
            # `tools` bounds which tools exist at all; `allowed_tools` must stay
            # EMPTY. Naming a tool there pre-approves it and the SDK then skips
            # can_use_tool entirely — the allowlist below would silently never
            # run. With nothing pre-approved, every call falls through to it.
            tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
            allowed_tools=[],
            can_use_tool=self._can_use_tool,
            max_turns=MAX_TURNS,
            # The table's rules come from the system prompt above; a stray
            # CLAUDE.md from the host machine has no business at this table.
            setting_sources=[],
            env={
                "DND_CAMPAIGN_ROOT": str(script_root(self.campaign_dir)),
                "DND_DICE_PHYSICAL": "0",   # no phone dice server behind a bot
                "CLAUDE_SKILL_DIR": str(DND_SKILL_DIR),
            },
        )

    async def start(self):
        options = self.build_options()
        self.client = ClaudeSDKClient(options)
        await self.client.connect()
        log.info("chat %s: DM session connected (%s)", self.chat_id, MODEL)

    async def stop(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                log.warning("chat %s: disconnect failed: %s", self.chat_id, e)
            self.client = None

    # ── one turn ─────────────────────────────────────────────────────────
    async def ask(self, text: str, on_progress=None) -> str:
        """Send one player turn, return the DM's narration.

        `on_progress` is called with a short status string whenever the agent
        starts a tool call, so the chat can show something is happening during
        a long turn instead of going silent.
        """
        async with self.lock:
            self.last_used = time.monotonic()
            if self.client is None:
                await self.start()

            await self.client.query(text)
            # Text that shares a message with a tool call is a preamble the
            # model writes to itself ("I'll read the module first"), not table
            # narration — and it comes out in English regardless of the game's
            # language. Only text from messages that call no tools is spoken to
            # the player; the preambles are a fallback in case a turn produces
            # nothing else.
            narration, preamble = [], []
            async for msg in self.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    uses_tools = any(isinstance(b, ToolUseBlock) for b in msg.content)
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            (preamble if uses_tools else narration).append(block.text)
                        elif isinstance(block, ThinkingBlock):
                            continue
                        elif on_progress is not None:
                            name = getattr(block, "name", None)
                            if name:
                                await on_progress(name)
                elif isinstance(msg, ResultMessage):
                    self.turns += 1
                    cost = getattr(msg, "total_cost_usd", None)
                    if cost:
                        self.total_cost += cost

            spoken = [c for c in narration if c.strip()]
            if not spoken:
                spoken = [c for c in preamble if c.strip()]
            return "\n".join(spoken).strip()


class SessionRegistry:
    """Chat id -> DMSession, created on demand."""

    def __init__(self):
        self._sessions: dict[int, DMSession] = {}

    def get(self, chat_id: int) -> DMSession | None:
        return self._sessions.get(chat_id)

    async def open(self, chat_id: int, campaign_dir: pathlib.Path,
                   system_prompt: str) -> DMSession:
        await self.close(chat_id)
        s = DMSession(chat_id, campaign_dir, system_prompt)
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
