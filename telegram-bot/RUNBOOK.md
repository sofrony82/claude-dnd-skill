# Runbook — starting, diagnosing and testing the DM bot

Operator's guide. [README.md](README.md) explains *what* the bot is and how the
two backends differ; this file is what you need when you are starting it,
something is broken, or you have changed code and want to know whether you broke
anything.

- [Starting it](#starting-it)
- [Checking it is alive](#checking-it-is-alive)
- [Troubleshooting](#troubleshooting)
- [End-to-end testing](#end-to-end-testing)

---

## Starting it

There are two processes and they are independent. The Telegram bot is the
product; the API server is the same agent reachable over HTTP, for testing
without a chat client. Run either, or both.

### First-time setup

```bash
cd telegram-bot
python3 -m venv .venv

# DeepSeek only (no Claude SDK, no claude CLI):
.venv/bin/pip install -r requirements-deepseek.txt
# …or both backends:
.venv/bin/pip install -r requirements.txt

cp .env.example .env && chmod 600 .env    # then fill it in
```

Minimum `.env` for the DeepSeek backend:

```ini
DND_BACKEND=deepseek
NB_STUDIO_API_KEY=<nebius token factory key>
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_ALLOWED_USERS=<your numeric telegram id>
DND_CAMPAIGN_ROOT=/home/<user>/.claude/dnd
```

`config.py` reads `.env` at import, so nothing needs to export these by hand —
which also means systemd units need no `EnvironmentFile` and no secret in the
unit text. Set `TELEGRAM_ALLOWED_USERS`: without it, anyone who finds the bot
plays on your quota.

The module pack is **not** in this repo — it lives under
`$DND_CAMPAIGN_ROOT/modules/<id>/` and must be built or copied there first. To
move one between hosts:

```bash
rsync -az ~/.claude/dnd/modules/stormwreck-isle/ \
      user@host:~/.claude/dnd/modules/stormwreck-isle/
```

### In the foreground (development)

```bash
.venv/bin/python bot.py                                    # Telegram bot
.venv/bin/uvicorn api_server:app --host 127.0.0.1 --port 8000   # API server
```

Add `--reload` to uvicorn while editing — but **not** in a unit file: a reload
drops every in-memory DM session, and the conversation history is the session.

### As a service (systemd user units)

Units live in `~/.config/systemd/user/`. `dnd-bot.service` runs `bot.py`,
`dnd-api.service` runs uvicorn on the loopback. Both use
`Restart=on-failure`; see [README.md](README.md#running-as-a-service) for the
unit text.

```bash
loginctl enable-linger "$USER"        # or the units die when you log out
systemctl --user daemon-reload
systemctl --user enable --now dnd-api dnd-bot
```

Day to day:

```bash
systemctl --user status  dnd-bot
systemctl --user restart dnd-api
systemctl --user stop    dnd-bot
journalctl --user -u dnd-bot -f              # follow
journalctl --user -u dnd-api --since -15min  # recent
```

**Restarting is not free.** Sessions are held in memory, so a restart makes
every active table lose its conversation. Campaign *state* survives — it is in
files — but the DM will have to re-read them, and the player sees a DM that has
forgotten the last ten minutes. Restart at a lull, and prefer `/save` in the
chat first.

### Reaching the API server

It binds `127.0.0.1` and has no authentication of its own, because it drives an
agent that writes files and runs scripts. Reach it through a tunnel:

```bash
ssh -L 8000:localhost:8000 user@host
curl -s localhost:8000/health | jq
```

If you must expose it, put a reverse proxy with auth in front. Do not change the
bind address and leave it at that.

### One poller per Telegram token

Telegram allows exactly one `getUpdates` consumer per bot token. A second one
does not queue — it knocks the first offline and both end up half-deaf, each
stealing some fraction of the updates. `bot.py` takes an advisory file lock and
refuses to start if another instance on the same host holds it, but a lock
cannot see across machines.

So: **one host per token.** Before starting the bot somewhere new, stop it
wherever it was running. To run two at once (say, Claude on your laptop and
DeepSeek on a VM, to compare them), get a second token from @BotFather.

---

## The live deployment

The bot runs on a Nebius VM, not on a laptop — a Telegram bot that is only up
while your machine is awake is not much of a bot.

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 sofrony@89.169.100.145
```

`IdentitiesOnly=yes` matters: without it ssh offers every key in the agent, and
the VM rejects the connection after too many attempts.

### What lives there

| Path | What | In git? |
|---|---|---|
| `~/claude-dnd-skill/` | the repo, incl. `telegram-bot/` and `skills/dnd/scripts/` | yes |
| `~/claude-dnd-skill/telegram-bot/.venv/` | virtualenv, `requirements-deepseek.txt` | no |
| `~/claude-dnd-skill/telegram-bot/.env` | **secrets**, `chmod 600` | no — gitignored |
| `~/.claude/dnd/modules/stormwreck-isle/` | module pack, 6.2 MB, 118 files | no — data, not code |
| `~/.claude/dnd/campaigns/tg-<chat_id>/` | live campaigns: state, sheets, transcripts | no — data |
| `~/.config/systemd/user/dnd-{bot,api}.service` | the two units | no |
| `~/scenario-raw-log.md` | the recorded session the replay test reads | no |
| `~/raw-log-deepseek.md`, `~/replay-report.json` | last replay's output | no |

The two things that are **not** in git are the two that a fresh clone cannot
run without: `.env` and the module pack. Recreate `.env` from
[.env.example](.env.example); copy the pack with `rsync` as shown above.

`skills/dnd/scripts/` is in the repo and the bot depends on it — `config.py`
resolves `DND_SKILL_DIR` to `<repo>/skills/dnd`, and the dice allowlist matches
scripts by absolute path underneath it. The bot cannot be moved out of the repo
on its own.

### Current state

- Telegram bot: **@DnDMastersBot**, DeepSeek backend, `TELEGRAM_ALLOWED_USERS`
  restricted to one Telegram id.
- API server: `127.0.0.1:8000`, loopback only — reach it with
  `ssh -L 8000:localhost:8000 sofrony@89.169.100.145`.
- Both are systemd **user** units with linger enabled, so they survive reboot
  and logout.

### Deploying a change

```bash
# from the repo root on your machine
rsync -az --exclude '.venv' --exclude '__pycache__' --exclude '.env' \
      -e "ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519" \
      ./telegram-bot/ sofrony@89.169.100.145:/home/sofrony/claude-dnd-skill/telegram-bot/

ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 sofrony@89.169.100.145 \
    'systemctl --user restart dnd-bot dnd-api'
```

Or, once the change is pushed, `git pull` on the VM instead of rsync — cleaner,
and it keeps the VM's working tree honest. Either way **the restart is the part
that matters**: both processes hold the prompt and the agent loop in memory, so
editing files on disk changes nothing until they restart. If you have been
rsyncing into a git working tree, `git status` on the VM will show those files
as modified until you pull; verify the content matches before discarding it.

Always exclude `.env` from an rsync of that directory. A `--delete` without that
exclusion removes the VM's secrets.

---

## Checking it is alive

```bash
# Both services up?
systemctl --user is-active dnd-bot dnd-api

# Backend, module pack, open sessions, in one call:
curl -s localhost:8000/health | jq '{engine, status, module_missing, open_sessions}'

# Telegram actually accepted the token, and which bot it is:
curl -s "https://api.telegram.org/bot$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)/getMe" | jq .result.username

# Is the model reachable and the key valid? (401 = bad key, 200 = fine)
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $NB_STUDIO_API_KEY" \
  https://api.tokenfactory.nebius.com/v1/models
```

A turn in flight shows up as model calls in the API log:

```bash
journalctl --user -u dnd-api --since -5min | grep chat/completions | tail
```

Every completion also logs one line with how it ended and what it cost —
`finish=stop` (spoke), `tool_calls` (rolled or read), `length` (cut by the
token limit), with prompt, completion and reasoning tokens. When a turn went
wrong, this is what tells the causes apart:

```bash
journalctl --user -u dnd-bot --since -30min | grep "chat <id>: completion"
```

Each tool call gets a line too — `tool roll_dice d20 «Спасбросок от смерти» ->
Roll: 11 = 11`, `tool write_file state.md -> Записано…`. To check the dice the
DM narrated against the ones it actually rolled:

```bash
journalctl --user -u dnd-bot --since -30min | grep "tool roll_dice"
```

---

## Troubleshooting

### The bot does not start

| Symptom | Cause | Fix |
|---|---|---|
| `No Telegram token` | none of `$TELEGRAM_BOT_TOKEN`, `.env`, `.telegram_apikey` has one | put it in `.env` |
| `Telegram token is present but malformed` | truncated paste, or quotes/whitespace in `.env` | expected shape is `<digits>:<secret>` |
| `No Nebius token` | `NB_STUDIO_API_KEY` unset on the deepseek backend | `.env`, or `<repo>/.nebius_apikey` |
| `Another bot instance is already running (pid N)` | the single-instance lock | `kill N`, or you genuinely want a second token |
| `Unknown DND_BACKEND=…` | typo | `claude` or `deepseek` |
| `CLINotFoundError` | backend is `claude` but the `claude` CLI is not on this process's `PATH` | install/login, or switch to `deepseek` |
| `ModuleNotFoundError: openai` | backend is `deepseek`, deps not installed | `pip install -r requirements-deepseek.txt` |
| unit dies the moment you log out | linger not enabled | `loginctl enable-linger "$USER"` |

### It starts, then refuses to play

**`Пак модуля не готов — не хватает: …`** — `/start` checks the pack before
opening a table. The named files are missing from
`$DND_CAMPAIGN_ROOT/modules/$DND_MODULE/`. Confirm where it is looking:

```bash
curl -s localhost:8000/health | jq '{module_dir, module_missing}'
```

Usually this is `DND_CAMPAIGN_ROOT` pointing at the wrong home directory —
a service running as a different user than the one you copied the pack for.

**`Этот бот приватный`** — your Telegram id is not in
`TELEGRAM_ALLOWED_USERS`. Ask [@userinfobot](https://t.me/userinfobot) for it.

### It plays, but badly

| Symptom | What is happening | Fix |
|---|---|---|
| DM narrates dice that were never rolled | the prompt named a tool the backend lacks, so the model has no way to roll and improvises | `e2e_test.py --offline` catches this; check the prompt/tool wiring |
| English text in the middle of a scene | reasoning or a tool-call preamble leaking into narration | `ds_engine.py` drops `reasoning_content` and treats text beside a tool call as preamble — check that logic first |
| File paths or `read_file` in the prose | prompt's "don't show the player" rules losing to a long context | trim history (`DND_HISTORY_TURNS`), or restate the rule in `prompts.py` |
| DM forgot the last scene | history was trimmed, or the process restarted | it is in the files, not the conversation: ask it to re-read `state.md`, and `/save` at scene boundaries |
| **«Мастер промолчал»** — the turn produced nothing | the model spent the whole reply reasoning, or wrote tool syntax where prose belongs. Concentrated in heavy combat | `ds_engine.py` nudges up to `NUDGE_LIMIT` times through the full tool loop, so a roll the model still owed gets made; `grep "empty narration, nudging" ` the journal. If it still happens, the round is too complex — say so in the prompt and split it |
| Narration stops mid-sentence | the completion hit `max_tokens`, which counts reasoning too | `finish=length` in the journal. The default is no cap (`DND_DS_MAX_TOKENS=0`); if one is set, the loop asks the model to continue up to `CONTINUE_LIMIT` times |
| `state.md` stale after a long session | the DM skipped scene-boundary saves | after `DND_SAVE_REMIND_TURNS` turns without a write the loop appends a hidden save reminder to the player's message; `grep "unsaved for"` the journal |
| `<｜DSML｜ …>` or `<tool_call>` in the prose | the model wrote its own tool-call markup into `content` instead of returning a structured call | stripped by `_strip_tool_markup`; seeing it means the pattern needs widening |
| DM invents module content | it answered without reading the chapter | `tool_calls` should be non-empty on a scene opening; check `read_file` is not silently refusing |
| Maps never appear | the DM did not emit `[[map:N]]`, or the image is missing | `/map 1` forces it; `ls modules/*/maps/` |
| A map appears twice | the DM re-emitted the marker | already deduped per session in `bot.py`; a restart legitimately resets that |

### Turns are slow

Expect **15–25 s** for a conversational turn and **40 s or more** in combat.
That is structural, not a bug: every tool call is a separate round-trip to the
model, and a fight legitimately costs a dozen — a bestiary lookup, then a roll
for each attack and each damage. `DND_DS_MAX_STEPS` caps the calls per turn.

If it is slower than that:

```bash
journalctl --user -u dnd-api --since -10min | grep -c chat/completions
```

Many calls for one turn means the DM is churning — often re-reading a large file
because a truncated read told it to. Few calls and a long wait means the model
itself is slow; check the endpoint.

To trade prose quality for speed on the Claude backend: `DND_MODEL=claude-sonnet-5`,
`DND_EFFORT=low`.

### Context and cost

One opening turn reads `world.md`, `npcs.md`, `arc.md` and a chapter — roughly
60k tokens of Cyrillic. Three guards keep a long session from growing without
bound, and **all three announce themselves in the tool output**, because a model
that believes it read a whole file will narrate confidently from the half it got:

- `read_file` truncates at `MAX_READ_CHARS` (60k) and says which lines it showed.
- `grep_files` caps hits at `MAX_GREP_HITS`.
- History past `DND_HISTORY_TURNS` player turns keeps its structure but has large
  tool results stubbed out.

Trimming cuts on turn boundaries by necessity: a `tool` message is only valid
directly after the `assistant` message whose `tool_calls` it answers, so slicing
the list anywhere else produces a request the API rejects outright.

### Diagnosing without Telegram

Reproduce any misbehaviour against the API server, where you can see what the
agent actually did:

```bash
curl -s localhost:8000/turn -H 'Content-Type: application/json' \
  -d '{"chat_id": 991700001, "text": "бросаю проверку Внимательности"}' \
  | jq '{narration, rolls, tool_calls, maps, seconds}'
```

`rolls` is the one you cannot get from the chat window: every roll the sandbox
*actually executed*. Comparing it against the dice in `narration` is the only
way to catch a DM that prints `🎲 d20+3 → 17` without rolling anything.

### Gotchas that cost me time

- **`ps`/`pgrep` over ssh, in zsh:** `SSH="ssh …"; $SSH 'cmd'` does not word-split
  in zsh — it tries to run the whole string as one command name and fails. A
  failed probe then looks exactly like "the process finished". Use a shell
  function, and have the remote echo an explicit success marker so you can tell
  "not running" from "could not check".
- **`nohup python … > log &` shows an empty log:** Python buffers stdout when it
  is not a tty. Use `python -u`, or wait for exit.
- **`rsync --info=…` fails on macOS:** the bundled rsync is 2.6.9. Use `-v`.
- **`rsync | grep` exit codes:** `grep` filtering the output means `$?` is
  *grep's* status, and "no lines matched" is 1. It looks like a failed transfer.
  Check `${PIPESTATUS[0]}`, or use `--stats`.

---

## End-to-end testing

Three layers, cheapest first. Run them in order; each is a gate on the next.

| Layer | Command | Cost | Catches |
|---|---|---|---|
| Unit | `python3 -m unittest tests.test_telegram_bot` | free, <1 s | message shaping, path containment, shell allowlist |
| E2E offline | `python3 e2e_test.py --offline` | free, ~1 s | broken wiring: config, secrets, pack, prompt/tool mismatch |
| E2E live | `python3 e2e_test.py` | tokens, ~1 min | the real model: tool calls, dice, files on disk |
| Replay | `python3 replay_log.py --log … --limit 8` | tokens, ~3 min | how a real session actually plays |

### After any change: the two-command gate

```bash
cd telegram-bot
.venv/bin/python e2e_test.py --offline     # ~1s, includes the unit suite
.venv/bin/python e2e_test.py               # ~1min, needs dnd-api running
```

`--offline` runs the unit suite itself and stops before spending tokens if
anything is red. Exit code is 0 only if every FAIL-level check passed, so it
works in a pre-push hook or CI step.

### What the layers actually check

A worked example of why this matters: the first 33-turn replay came back with
**7 of 33 turns empty** — the player acted and the world said nothing back —
and 2 turns where the DM narrated dice it had never rolled. Neither is visible
from reading the code, and only one of them (the dice) would be obvious from
reading the chat. Both now have regression tests.

**Offline** (19 checks): config imports; the backend's secret is present;
the module pack is complete and has pregens and map images; the engine selector
resolves without importing the other backend's dependency; the system prompt
builds and **names tools the chosen backend really exposes**; the sandbox refuses
reads outside its roots, writes outside the campaign, writes to the pack, and
shell chaining, while permitting the helper scripts; `dice.py` executes.

That prompt/tool check is the highest-value one. If the DeepSeek prompt tells
the DM to roll dice with `Bash`, the model has no such tool — and instead of
failing loudly it writes plausible dice into the prose. Nothing else in the
stack notices.

**Live** adds: the server is up and reports the pack ok; a session creates
`party.json`, `state.md`, `session-log.md` and a character sheet; an opening turn
returns non-empty Russian prose *and made tool calls* (a DM that answers without
reading the module is improvising the adventure from memory); an explicit
"roll a Perception check" really reaches `dice.py`; prose never claims more dice
than the sandbox rolled; `/save` leaves content in `state.md`; the transcript and
`/state` endpoints serve it back; the session closes cleanly.

### Writing assertions for a nondeterministic DM

This is the part that decides whether the suite is worth having.

**FAIL only on things that are wrong however the scene went.** Empty narration,
non-Russian narration, a leaked path or tool name, dice claimed but not rolled,
files not written, no tool calls on a scene opening. These are defects under
every reading.

**WARN on anything the DM legitimately decides.** Whether *this* scene needed a
roll. Whether to show a map now. Prose length. Mood. A test that goes red
because the DM chose not to roll this time teaches you to ignore red, which is
worse than having no test.

**Force the behaviour you want to assert.** Do not hope a turn produces dice —
ask for them: `"брось проверку Внимательности и скажи результат"`. Now zero rolls
is unambiguously a broken backend rather than a judgement call.

**Assert on the side effects, not the prose.** `rolls`, `tool_calls`, and the
files on disk are checkable. The narration is not, beyond the negative checks
above. This is why `/turn` returns what Telegram hides.

### Prove the test can fail

A green suite that cannot go red is worse than no suite. Break something on
purpose and confirm it is caught:

```bash
DND_MODULE=does-not-exist .venv/bin/python e2e_test.py --offline
#   FAIL: module pack complete — missing: world.md, npcs.md, …

# and the subtle one — prompt naming a tool the backend lacks:
#   FAIL: prompt names the dice tool the backend has — referenced: []
#   FAIL: prompt does not name SDK-only tools — prompt still says Bash
```

Do this whenever you add a check.

### Replaying a recorded session

The fullest test, and the only one that shows how the DM actually plays.
`replay_log.py` takes a `raw-log.md` from a previous game, extracts just the
player's turns, and feeds them back in order. Prose will differ — different
model, different dice — so it does not diff text. It writes a new transcript to
read side by side and grades every reply.

```bash
.venv/bin/python replay_log.py \
    --log ~/.claude/dnd/campaigns/tg-401712068/raw-log.md \
    --chat-id 991712068 \
    --out /tmp/raw-log-deepseek.md --report /tmp/report.json

.venv/bin/python replay_log.py --log … --limit 8    # smoke test first
```

- Use a `--chat-id` in the `99xxxxxxx` range. **The run resets that campaign
  directory** — point it at a real chat id and you delete a real game.
- A 33-turn replay takes 20–40 minutes. Run it with `python -u` under `nohup`,
  or in tmux, and watch `--report` for the JSON.
- Exit code is non-zero only for `invented_dice`, `error` and `empty`. Style
  flags are reported for reading, not as a gate: one stray heading in 33 turns
  of improvised prose is a note, not a broken build.

### Testing the Telegram layer

`bot.py` is the one part no automated layer covers — the E2E tests deliberately
go through `api_server.py` instead, because driving Telegram needs a live token
and a human. The message-shaping logic it depends on (`tg_format`) is unit
tested; the rest is a manual pass:

```
/start → pick a party → play two turns → /sheet → /map → /save → /recap
```

Watch for: prose arriving un-split or mid-sentence, HTML entities showing
literally, maps missing or doubled, and the typing indicator stopping while the
DM is still thinking.

### If CI is red on an unrelated suite

`tests/test_phone_presence.py` and `tests/test_milestone_counter.py` need
`flask`, which the bot does not. Three errors from those files with
`ModuleNotFoundError: No module named 'flask'` are pre-existing and unrelated to
the bot — `pip install flask`, or scope the run:

```bash
python3 -m unittest tests.test_telegram_bot
```
