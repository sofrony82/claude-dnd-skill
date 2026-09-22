# Telegram DM bot — «Драконы острова Штормокрушений»

A Telegram front end for the `dnd` skill's DM engine. One person plays the whole
party in a private chat; the bot runs a Dungeon Master agent, in Russian, from a
**prepared module pack** — so the agent never parses the source PDF during play.

Two interchangeable DM backends, chosen with `DND_BACKEND`:

| `DND_BACKEND` | Engine | Who owns the agent loop |
|---|---|---|
| `claude` (default) | [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) — `dm_engine.py` | the SDK |
| `deepseek` | `deepseek-ai/DeepSeek-V4.1-Flash` on Nebius Token Factory — `ds_engine.py` | us |

An OpenAI-compatible endpoint has no tools and no agent loop, so the DeepSeek
path implements both: `sandbox.py` provides the tools and the rules that bound
them, and `ds_engine.py` runs the call/result cycle. Both backends present the
same session interface, so `bot.py` and `api_server.py` do not know which one
they are driving.

```
Telegram ──▶ bot.py ──┐
                      ├──▶ engine.py ──▶ dm_engine.py  (Claude Agent SDK)
HTTP ──▶ api_server.py┘                   ds_engine.py  (DeepSeek + sandbox.py)
                                             │
                    ├─ system prompt: DM standards + pack layout (prompts.py)
                    ├─ tools: read/write/edit/glob/grep + dice, allowlisted
                    ├─ reads:  ~/.claude/dnd/modules/stormwreck-isle/  (pack, read-only)
                    └─ writes: ~/.claude/dnd/campaigns/tg-<chat_id>/   (state, sheets, log)
```

> **Operating it?** [RUNBOOK.md](RUNBOOK.md) covers starting and stopping the
> services, a symptom-to-fix troubleshooting table, and the layered end-to-end
> test procedure to run after a change.

## Prerequisites

- Python 3.11+
- For `DND_BACKEND=claude`: the `claude` CLI, logged in (`claude` → `/login`).
  The SDK drives it; billing follows your Claude subscription.
- For `DND_BACKEND=deepseek`: a Nebius Token Factory key in `NB_STUDIO_API_KEY`.
  No CLI and no Claude SDK needed — install `requirements-deepseek.txt`.
- A built module pack. See the `module-prep` skill; check with:
  ```bash
  python3 ../skills/module-prep/scripts/module_check.py --pack ~/.claude/dnd/modules/stormwreck-isle
  ```

## Setup

```bash
cd telegram-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env     # then fill in TELEGRAM_BOT_TOKEN
```

The token is read from `$TELEGRAM_BOT_TOKEN`, then `telegram-bot/.env`, then
`<repo>/.telegram_apikey`. All three are gitignored.

**Set `TELEGRAM_ALLOWED_USERS`.** Without it anyone who finds the bot can play
on your Claude quota. Put your numeric Telegram user id there (ask
[@userinfobot](https://t.me/userinfobot)); comma-separate several.

## Run

```bash
.venv/bin/python bot.py
```

Then open the bot in Telegram and send `/start`.

## DeepSeek backend

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-deepseek.txt
```

In `.env`:

```
DND_BACKEND=deepseek
NB_STUDIO_API_KEY=<nebius token factory key>
DND_DS_MODEL=deepseek-ai/DeepSeek-V4.1-Flash
```

The key is read from `$NB_STUDIO_API_KEY`, then `telegram-bot/.env`, then
`<repo>/.nebius_apikey`. All gitignored.

DeepSeek is a reasoning model: replies carry `reasoning_content` beside
`content`. That text is the model thinking out loud — English, out of character,
and liable to quote the module's DM-only sections — so `ds_engine.py` drops it
from both the player's view and the conversation history.

Because it is one model call per tool round rather than a managed session,
context is the thing to watch. `read_file` truncates at 60k characters,
`grep_files` caps its hits, and history older than `DND_HISTORY_TURNS` player
turns has its large tool results stubbed out. All three say so in the output:
a DM that believes it read a whole file will narrate confidently from the half
it got.

## Direct-query API (no Telegram)

`api_server.py` exposes the same agent over HTTP, for testing a DM without
typing into a chat client and squinting at prose:

```bash
.venv/bin/uvicorn api_server:app --host 127.0.0.1 --port 8000
```

```bash
curl -s localhost:8000/health | jq                      # backend, pack, sessions
curl -s localhost:8000/session -H 'Content-Type: application/json' \
     -d '{"chat_id": 991712068, "party": [{"id":"wizard-elf","name":"sofrony"}]}'
curl -s localhost:8000/turn -H 'Content-Type: application/json' \
     -d '{"chat_id": 991712068, "text": "сразу наверх"}' | jq -r .narration
curl -s localhost:8000/state/991712068 | jq             # what the DM wrote to disk
```

A turn returns what Telegram hides: `narration`, the `maps` its markers asked
for, `tool_calls`, and **`rolls`** — every roll the sandbox actually executed.
That last one is the point. A model will happily print `🎲 d20+3 → 17` without
calling anything, and comparing the prose against `rolls` is the only way to
catch it from outside.

**Bind to the loopback.** This endpoint drives an agent that writes files and
runs helper scripts; it has no authentication of its own. Reach it over a tunnel:

```bash
ssh -L 8000:localhost:8000 user@host
```

### Replaying a recorded session

`replay_log.py` takes a `raw-log.md` from a previous game, extracts just the
player's turns, and feeds them back in order. Prose will differ — different
model, different dice — so it does not diff text; it writes a new transcript to
read side by side and grades each reply on what is true either way: invented
dice, non-Russian replies, leaked paths, leaked tool names, headings in prose,
raw room codes, empty turns.

```bash
.venv/bin/python replay_log.py \
    --log ~/.claude/dnd/campaigns/tg-401712068/raw-log.md \
    --chat-id 991712068 \
    --out /tmp/raw-log-deepseek.md --report /tmp/report.json
.venv/bin/python replay_log.py --log … --limit 8      # smoke test
```

Use a `--chat-id` that is not a real chat: the run resets that campaign
directory.

## Playing

`/start` asks how many characters you want (1–5), then walks you through picking
each from the module's five pregenerated characters and naming them. The module
is written for four; fewer is playable and more dangerous.

| Command | |
|---|---|
| `/start` | begin, or resume an existing campaign |
| `/games` | your campaigns — switch to one, or move one to the trash |
| `/new` | start another campaign; the current one is kept |
| `/rename <title>` | rename the current campaign |
| `/party` | who is in the party |
| `/sheet [name]` | character sheet, read from the files |
| `/map [n]` | show a map — the current one, or map `n` |
| `/recap` | where you are and what is going on |
| `/save` | save right now — rarely needed, see below |
| `/reset` | move the current campaign to the trash and start over |

The same commands, minus `/save`, are behind the **Menu** button next to the
input field; the bot registers them on startup.

Everything else you type is your turn. Write freely, for the whole party
(«идём в храм, Дарин осматривает статую») or in character.

Dice are rolled by `scripts/dice.py`, never imagined by the model, and the
arithmetic is shown. Maps arrive as pictures when you first reach a location.

Play is in a private chat only: one person runs the whole party, and the bot
leaves any group or channel it is added to. Several players can play at once —
each chat is its own lane (`chat_lanes.py`), so one player's long DM turn does
not hold up anyone else. Within a chat, messages are handled in order; typing
more than two while the DM is still answering gets a "wait" note instead of a
queue of paid turns. A DM session nobody has used for `DND_IDLE_CLOSE_MINUTES`
is closed; the next message reopens it from the files and the log.

## Configuration

| Variable | Default | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | required |
| `TELEGRAM_ALLOWED_USERS` | *(empty — open to all)* | comma-separated user ids |
| `DND_MODULE` | `stormwreck-isle` | which pack under `modules/` to run |
| `DND_MODEL` | `claude-opus-5` | `claude-sonnet-5` is faster and cheaper |
| `DND_EFFORT` | `medium` | `low` \| `medium` \| `high` |
| `DND_CAMPAIGN_ROOT` | `~/.claude/dnd` | data root for packs and campaigns |
| `DND_BACKEND` | `claude` | `claude` \| `deepseek` |
| `NB_STUDIO_API_KEY` | — | required for `deepseek` |
| `NB_BASE_URL` | `https://api.tokenfactory.nebius.com/v1` | OpenAI-compatible endpoint |
| `DND_DS_MODEL` | `deepseek-ai/DeepSeek-V4.1-Flash` | |
| `DND_DS_TEMPERATURE` | `0.8` | prose warmth; below ~0.5 the DM repeats itself |
| `DND_DS_MAX_TOKENS` | `0` | cap on one reply, reasoning included; `0` = no cap |
| `DND_DS_MAX_STEPS` | `24` | tool calls allowed within one player turn |
| `DND_HISTORY_TURNS` | `12` | player turns kept verbatim before trimming |
| `DND_SAVE_REMIND_TURNS` | `4` | player turns without a `state.md` write before the DM is told to save |
| `DND_IDLE_CLOSE_MINUTES` | `30` | close a DM session unused this long; `0` keeps them forever |

## Security

The bot takes input from the open internet and hands it to an agent that can run
shell commands, so tool use is gated by an explicit allowlist rather than run in
a bypass mode. Both backends enforce the same rules — `dm_engine.py` gates the
tools the SDK provides, `sandbox.py` is the only implementation the DeepSeek
path has:

- **Read** — only inside the campaign directory, the module pack, and the skill.
- **Write / Edit** — only inside the campaign directory. The pack is read-only.
- **Bash** — only `python3 <skill>/scripts/<allowed>.py …`, matched by resolved
  absolute path, with shell metacharacters (`;`, `|`, `&`, backticks, `$`)
  refused outright so the allowlist cannot be walked around by chaining. A
  `--campaign` argument (in any spelling argparse accepts: `-cX`, `--camp X`, …)
  must resolve to the chat's own campaign directory; tracker/xp/calendar/oracle
  would otherwise read and write whichever campaign the name points at.
- Everything else is denied with a reason the agent can read and route around.

Every path argument is resolved before it is compared, so `..` and symlinks are
followed to their real target rather than prefix-matched as text. Refusals come
back as ordinary tool output in Russian: the DM reads the reason and works
around it instead of the turn dying.

Still: run it as a normal user, keep `TELEGRAM_ALLOWED_USERS` set, bind
`api_server.py` to the loopback, and do not expose the host.

## Running as a service

`systemd` user units, so both survive logout and restart on failure:

```ini
# ~/.config/systemd/user/dnd-bot.service
[Service]
WorkingDirectory=/path/to/claude-dnd-skill/telegram-bot
ExecStart=/path/to/telegram-bot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=10
```

```ini
# ~/.config/systemd/user/dnd-api.service
[Service]
WorkingDirectory=/path/to/claude-dnd-skill/telegram-bot
ExecStart=/path/to/telegram-bot/.venv/bin/uvicorn api_server:app \
    --host 127.0.0.1 --port 8000 --timeout-keep-alive 900
Restart=on-failure
```

```bash
loginctl enable-linger "$USER"        # or the units die at logout
systemctl --user daemon-reload
systemctl --user enable --now dnd-api dnd-bot
journalctl --user -u dnd-bot -f
```

Secrets stay in `telegram-bot/.env` at `chmod 600` — `config.py` reads it at
import, so the units need no `EnvironmentFile` and no secret in the unit text.

**One poller per token.** Telegram allows exactly one `getUpdates` consumer per
bot token; a second one does not queue, it knocks the first offline and both go
half-deaf. `bot.py` takes a file lock and refuses to start if another instance
holds it. Running the bot on two hosts means two tokens.

## Saving

There is nothing the player has to do. Three layers, each covering the one
above it:

1. **The DM saves** `state.md` and the sheets at scene boundaries, as the prompt
   asks. On DeepSeek the loop also reminds it after `DND_SAVE_REMIND_TURNS`
   turns without a save.
2. **Before leaving a campaign** — switching in `/games`, or `/new` — the bot
   asks the DM to save if anything was played since the last save, and shows
   «💾 Сохраняю кампанию…» while it does.
3. **On resume**, the DM is handed the log of everything played after the last
   save. Every turn in which the DM wrote `state.md` moves a save point
   (`.saved.json`: how far into `raw-log.md` the save reaches); what lies past
   it goes into the system prompt as «ЧТО БЫЛО ПОСЛЕ ПОСЛЕДНЕГО СОХРАНЕНИЯ»,
   and the DM continues from the end of it. So a crash or a restart loses
   nothing that reached the log, even if the DM never saved.

Campaigns from before save points have no `.saved.json` until their first
saving turn. For them, a `state.md` written within five minutes of the last
log entry counts as current; otherwise the DM gets the end of the log, marked
as possibly overlapping what `state.md` already holds.

## State on disk

```
~/.claude/dnd/campaigns/<campaign_id>/
    party.json        who is at the table, owner, title
    state.md          current scene, quests, world state
    session-log.md    what happened
    raw-log.md        verbatim transcript
    .saved.json       how far into raw-log.md the last save reaches
    characters/*.md   one sheet per character
~/.claude/dnd/campaigns/.trash/<campaign_id>-<stamp>/
                      campaigns deleted from the bot
~/.claude/dnd/chats/<chat_id>.json
                      {"active": "<campaign_id>"} — what the chat is playing
```

New campaigns are named `<date>-<first character>` (`20260922-merri`). Ones made
before multi-campaign support are `tg-<chat_id>` with no owner field; they stay
where they are and belong to the user in the `chat_id` their party.json records
(a private chat's id is its user's id). So a backup copied back under any
`tg-…` name shows up in that user's `/games` and can be resumed. The API
server keeps using `tg-<chat_id>` for its test chats, and those never show in
anyone's `/games`.

Deleting from the bot never erases: the directory moves to `.trash/`. To
restore one, move it back and strip the stamp:

    mv ~/.claude/dnd/campaigns/.trash/20260922-merri-20260922-174711 \
       ~/.claude/dnd/campaigns/20260922-merri

Empty the trash by hand. The module pack is untouched by play, so several
campaigns can run the same adventure independently.

## Testing and troubleshooting

See [RUNBOOK.md](RUNBOOK.md) — startup failures, misbehaviour at the table,
latency and cost, and the three-layer test procedure:

```bash
.venv/bin/python e2e_test.py --offline     # ~1s, free, includes the unit suite
.venv/bin/python e2e_test.py               # ~1min, live model
```

The quick ones:

**«Пак модуля не готов»** — the pack is missing files; check
`curl -s localhost:8000/health | jq .module_missing`.

**`CLINotFoundError`** — backend is `claude` but the CLI is not on `PATH`.

**`Another bot instance is already running`** — one `getUpdates` consumer per
token. Stop the other one, or use a second token.

**The DM forgets something** — it is in the files, not the conversation. Ask it
to re-read `state.md`, or `/save` more often at scene boundaries.
