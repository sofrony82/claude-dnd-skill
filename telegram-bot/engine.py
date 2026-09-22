"""
engine.py — pick the DM backend once, at import.

Two engines present the same surface (`open`, `get`, `close`, `close_all`, and
sessions with `ask`/`maps_shown`), so callers work with either:

    DND_BACKEND=claude     dm_engine.SessionRegistry  — Claude Agent SDK
    DND_BACKEND=deepseek   ds_engine.DSRegistry       — DeepSeek on Nebius

The import is conditional rather than unconditional because each backend pulls
a heavy, optional dependency: `claude-agent-sdk` is not installed on the VM that
runs DeepSeek, and `openai` need not be installed for the SDK path. Importing
both would make each host carry the other's requirements.
"""

import logging

from config import BACKEND

log = logging.getLogger("engine")

BACKENDS = ("claude", "deepseek")


def _registry_class():
    if BACKEND == "deepseek":
        from ds_engine import DSRegistry
        return DSRegistry
    if BACKEND == "claude":
        from dm_engine import SessionRegistry
        return SessionRegistry
    raise SystemExit(
        f"Unknown DND_BACKEND={BACKEND!r}. Use one of: {', '.join(BACKENDS)}")


def new_registry():
    """A fresh session registry for the configured backend."""
    cls = _registry_class()
    log.info("DM backend: %s (%s)", BACKEND, cls.__name__)
    return cls()


def describe() -> str:
    """One line naming the backend and model, for logs and /health."""
    if BACKEND == "deepseek":
        from config import DS_MODEL
        return f"deepseek:{DS_MODEL}"
    from config import MODEL
    return f"claude:{MODEL}"
