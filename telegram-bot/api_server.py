#!/usr/bin/env python3
"""
api_server.py — the same DM agent, without Telegram in the way.

Testing a DM through a chat client is slow and unrepeatable: you type, you wait,
you squint at the prose, and nothing is recorded in a form you can diff. This
server exposes the identical agent path — `campaign`, `prompts`, `engine`,
`tg_format`, `transcript`, the same module pack and the same sandbox — over
HTTP, so a scenario can be replayed turn by turn and the output compared.

It deliberately reuses those modules rather than reimplementing a thinner
version. A test harness that talks to its own simplified copy of the agent
proves things about the copy.

What a turn returns that Telegram hides:

    narration   what the player would have seen, map markers stripped
    maps        which maps the marker asked for, in order
    rolls       every roll the sandbox actually executed this turn
    tool_calls  names of the tools used, in order

`rolls` is the point. The DM is asked to roll through a script; a model can
print "🎲 d20+3 → 17" without calling anything, and the only way to tell the
difference from the outside is to compare the prose against what the sandbox
really ran.

Binds 127.0.0.1 by default. This endpoint drives an agent that writes files and
executes helper scripts, so reaching it should cost an SSH tunnel:

    ssh -L 8000:localhost:8000 sofrony@<vm>
    curl -s localhost:8000/health | jq

Run:  uvicorn api_server:app --host 127.0.0.1 --port 8000
"""

import logging
import pathlib
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import campaign
import engine
import prompts
import tg_format
import transcript
from config import BACKEND, MODULE_DIR, PREGENS

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("api")

app = FastAPI(
    title="D&D DM agent — direct query API",
    description="The Telegram bot's DM agent, driven without Telegram.",
    version="1.0.0",
)

REGISTRY = engine.new_registry()
BOT_SPEAKER = transcript.BOT_SPEAKER


# ── payloads ─────────────────────────────────────────────────────────────
class PartyMember(BaseModel):
    id: str = Field(..., description="pregen id, e.g. wizard-elf")
    name: str = Field(..., description="character name the player chose")


class NewSession(BaseModel):
    chat_id: int = Field(..., description="campaign namespace; dir is tg-<chat_id>")
    party: list[PartyMember] = Field(..., min_length=1, max_length=5)
    reset: bool = Field(True, description="wipe an existing campaign for this id")


class Turn(BaseModel):
    chat_id: int
    text: str = Field(..., description="what the player types; one turn")


# ── helpers ──────────────────────────────────────────────────────────────
def _system_prompt(chat_id: int) -> str:
    party = campaign.load_party(chat_id)
    if not party:
        raise HTTPException(404, f"no campaign for chat_id {chat_id}; POST /session first")
    return prompts.build_system_prompt(
        prompts.onboarding_summary(party["party"]),
        MODULE_DIR, campaign.campaign_dir(chat_id), BACKEND)


async def _session(chat_id: int):
    s = REGISTRY.get(chat_id)
    if s is None:
        s = await REGISTRY.open(chat_id, campaign.campaign_dir(chat_id),
                                _system_prompt(chat_id))
    return s


# ── routes ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    ok, missing = campaign.pack_ready()
    return {
        "status": "ok" if ok else "module-pack-incomplete",
        "engine": engine.describe(),
        "backend": BACKEND,
        "module_dir": str(MODULE_DIR),
        "module_missing": missing,
        "open_sessions": sorted(getattr(REGISTRY, "_sessions", {}).keys()),
        "pregens": [{"id": p[0], "klass": p[1], "race": p[2]} for p in PREGENS],
    }


@app.post("/session")
async def create_session(req: NewSession):
    """Create a campaign and open a DM session. Mirrors /start onboarding."""
    ok, missing = campaign.pack_ready()
    if not ok:
        raise HTTPException(503, f"module pack incomplete: {', '.join(missing)}")

    party = []
    for m in req.party:
        meta = campaign.pregen_meta(m.id)
        if not meta:
            raise HTTPException(
                422, f"unknown pregen {m.id!r}; see /health for the list")
        party.append({"id": meta["id"], "name": m.name,
                      "klass": meta["klass"], "race": meta["race"]})

    await REGISTRY.close(req.chat_id)
    if req.reset or not campaign.exists(req.chat_id):
        # A fixed id per chat_id, so a reset replaces the test campaign rather
        # than minting a new one every run.
        cdir = campaign.create(req.chat_id, party,
                               campaign_id=campaign.legacy_id(req.chat_id))
    else:
        cdir = campaign.campaign_dir(req.chat_id)

    saved = campaign.load_party(req.chat_id)
    await REGISTRY.open(req.chat_id, cdir, _system_prompt(req.chat_id))
    log.info("chat %s: session opened (%s)", req.chat_id, engine.describe())
    return {
        "chat_id": req.chat_id,
        "campaign_dir": str(cdir),
        "engine": engine.describe(),
        "party": saved["party"],
    }


@app.post("/turn")
async def turn(req: Turn):
    """Inject one player turn; return the narration and what the agent did."""
    session = await _session(req.chat_id)
    cdir = campaign.campaign_dir(req.chat_id)

    transcript.append(cdir, "Player", req.text)
    saved_before = transcript.state_mtime(cdir)

    used: list = []
    sandbox = getattr(session, "sandbox", None)
    rolls_before = len(sandbox.rolls) if sandbox else 0

    async def on_progress(name: str):
        used.append(name)

    t0 = time.monotonic()
    try:
        raw = await session.ask(req.text, on_progress=on_progress)
    except Exception as e:                          # noqa: BLE001
        log.exception("chat %s: turn failed", req.chat_id)
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e
    elapsed = time.monotonic() - t0

    narration, maps = tg_format.extract_maps(raw)
    if narration:
        transcript.append(cdir, BOT_SPEAKER, narration)
    if transcript.state_mtime(cdir) != saved_before:
        transcript.mark_saved(cdir)

    new_rolls = sandbox.rolls[rolls_before:] if sandbox else []
    return {
        "chat_id": req.chat_id,
        "narration": narration,
        "maps": maps,
        "rolls": new_rolls,
        "tool_calls": used,
        "chars": len(narration),
        "seconds": round(elapsed, 2),
        "turn": getattr(session, "turns", None),
        "total_tokens": getattr(session, "total_tokens", None),
        # Telegram splits long prose; a caller testing message shaping wants to
        # see the same split without running a bot.
        "chunks": len(tg_format.chunk(narration)),
    }


@app.get("/transcript/{chat_id}")
async def get_transcript(chat_id: int, tail: int = 0):
    """The campaign's raw log — the same file the Telegram bot writes."""
    p = transcript.path_for(campaign.campaign_dir(chat_id))
    if not p.is_file():
        raise HTTPException(404, f"no transcript at {p}")
    text = p.read_text(encoding="utf-8")
    if tail > 0:
        text = "\n".join(text.splitlines()[-tail:])
    return {"chat_id": chat_id, "path": str(p), "text": text}


@app.get("/state/{chat_id}")
async def get_state(chat_id: int):
    """Campaign files the DM maintains — proof that state actually got written."""
    cdir = campaign.campaign_dir(chat_id)
    if not cdir.is_dir():
        raise HTTPException(404, f"no campaign dir {cdir}")
    out = {}
    for rel in ("state.md", "session-log.md"):
        f = cdir / rel
        out[rel] = f.read_text(encoding="utf-8") if f.is_file() else None
    sheets = {}
    for f in sorted((cdir / "characters").glob("*.md")):
        sheets[f.name] = f.read_text(encoding="utf-8")
    out["characters"] = sheets
    return {"chat_id": chat_id, "campaign_dir": str(cdir), "files": out}


@app.delete("/session/{chat_id}")
async def close_session(chat_id: int):
    """Close the agent session. Campaign files stay on disk."""
    await REGISTRY.close(chat_id)
    return {"chat_id": chat_id, "closed": True}


@app.on_event("shutdown")
async def _shutdown():
    await REGISTRY.close_all()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
