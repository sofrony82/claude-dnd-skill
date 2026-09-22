# Telegram DM bot — «Драконы острова Штормокрушений»

A Telegram front end for the `dnd` skill's DM engine. One person plays the whole
party in a private chat; the bot runs a [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
session as Dungeon Master, in Russian, from a **prepared module pack** — so the
agent never parses the source PDF during play.

```
Telegram ──▶ bot.py ──▶ ClaudeSDKClient (one per chat)
                         ├─ system prompt: DM standards + pack layout (prompts.py)
                         ├─ tools: Read / Write / Edit / Glob / Grep / Bash
                         │         gated by an allowlist (dm_engine.py)
                         ├─ reads:  ~/.claude/dnd/modules/stormwreck-isle/   (pack, read-only)
                         └─ writes: ~/.claude/dnd/campaigns/tg-<chat_id>/    (state, sheets, log)
```

## Prerequisites

- Python 3.11+
- The `claude` CLI, logged in (`claude` → `/login`). The SDK drives it; billing
  follows your Claude subscription.
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

## Playing

`/start` asks how many characters you want (1–5), then walks you through picking
each from the module's five pregenerated characters and naming them. The module
is written for four; fewer is playable and more dangerous.

| Command | |
|---|---|
| `/start` | begin, or resume an existing campaign |
| `/party` | who is in the party |
| `/sheet [name]` | character sheet, read from the files |
| `/map [n]` | show a map — the current one, or map `n` |
| `/recap` | where you are and what is going on |
| `/save` | flush state and sheets to disk |
| `/reset` | wipe the campaign and start over |

Everything else you type is your turn. Write freely, for the whole party
(«идём в храм, Дарин осматривает статую») or in character.

Dice are rolled by `scripts/dice.py`, never imagined by the model, and the
arithmetic is shown. Maps arrive as pictures when you first reach a location.

## Configuration

| Variable | Default | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | required |
| `TELEGRAM_ALLOWED_USERS` | *(empty — open to all)* | comma-separated user ids |
| `DND_MODULE` | `stormwreck-isle` | which pack under `modules/` to run |
| `DND_MODEL` | `claude-opus-5` | `claude-sonnet-5` is faster and cheaper |
| `DND_EFFORT` | `medium` | `low` \| `medium` \| `high` |
| `DND_CAMPAIGN_ROOT` | `~/.claude/dnd` | data root for packs and campaigns |

## Security

The bot takes input from the open internet and hands it to an agent that can run
shell commands, so tool use is gated by an explicit allowlist rather than run in
a bypass mode (`dm_engine.py`):

- **Read** — only inside the campaign directory, the module pack, and the skill.
- **Write / Edit** — only inside the campaign directory. The pack is read-only.
- **Bash** — only `python3 <skill>/scripts/<allowed>.py …`, matched by resolved
  absolute path, with shell metacharacters (`;`, `|`, `&`, backticks, `$`)
  refused outright so the allowlist cannot be walked around by chaining.
- Everything else is denied with a reason the agent can read and route around.

Still: run it as a normal user, keep `TELEGRAM_ALLOWED_USERS` set, and do not
expose the host.

## State on disk

```
~/.claude/dnd/campaigns/tg-<chat_id>/
    party.json        who is at the table
    state.md          current scene, quests, world state
    session-log.md    what happened
    characters/*.md   one sheet per character
```

Delete the directory (or `/reset`) to start over. The module pack is untouched
by play, so several chats can run the same adventure independently.

## Troubleshooting

**«Пак модуля не готов»** — the pack is missing files. Run `module_check.py`.

**`CLINotFoundError`** — the `claude` CLI is not on `PATH` for this process.

**Turns are slow** — Opus writing long Russian prose and reading chapter files.
Set `DND_MODEL=claude-sonnet-5` and/or `DND_EFFORT=low`.

**The DM forgets something** — it is in the files, not the conversation. Ask it
to re-read `state.md`, or `/save` more often at scene boundaries.
