"""
sandbox.py — the tools the DM may use, and the rules that bound them.

`dm_engine.py` gets its tools from the Claude Agent SDK and only has to *gate*
them. A plain OpenAI-compatible model has no tools at all, so the DeepSeek
backend has to implement them itself — and an implementation is exactly where a
file-reading, shell-running agent exposed to the open internet goes wrong.

So the rules live here, in one place, and they are the same rules the SDK path
enforces:
  * read   — only inside the campaign directory, the module pack, or the skill
  * write  — only inside the campaign directory
  * shell  — only the D&D helper scripts, matched by absolute path

Every path argument is resolved before it is compared, so `..` and symlinks
cannot walk out of an allowed root. Refusals come back as ordinary tool output
in Russian: the DM reads the reason and works around it, rather than the turn
dying on an exception.

Two limits exist to protect the context window rather than the host. `Read`
truncates at MAX_READ_CHARS (npcs-full.md alone is 130 KB, about 51k tokens of
Cyrillic) and tells the model how to ask for the rest; `Grep` caps its match
count. Both say plainly that output was cut, because a model that thinks it saw
a whole file will confidently narrate from the half it got.
"""

import fnmatch
import pathlib
import re
import shlex
import subprocess

from config import DND_SKILL_DIR, MODULE_DIR

# Helper scripts the DM may run. Anything outside this set is refused.
# Kept byte-identical to dm_engine.ALLOWED_SCRIPTS — two backends, one rule.
ALLOWED_SCRIPTS = {
    "dice.py", "xp.py", "combat.py", "tracker.py", "lookup.py",
    "ability-scores.py", "character.py", "calendar.py", "oracle.py",
}

MAX_READ_CHARS = 60_000     # ~24k tokens of Cyrillic
MAX_GREP_HITS = 80
MAX_GLOB_HITS = 200
SCRIPT_TIMEOUT = 30         # a dice roll that hangs must not hang the turn


def _under(path, root: pathlib.Path) -> bool:
    """True if `path` resolves to somewhere inside `root`.

    Resolution happens before comparison, so `../..` and symlinks are followed
    to their real target first — a prefix test on the raw string would not be.
    """
    try:
        pathlib.Path(path).expanduser().resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def bash_allowed(cmd: str) -> bool:
    """True only for a single `python3 <skill>/scripts/<allowed>.py …` call.

    Shell metacharacters are refused outright: chaining is how an allowlist
    keyed on the first token gets walked around.
    """
    if not cmd or re.search(r"[;&|<>`$\n]|\|\|", cmd):
        return False
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if len(parts) < 2 or not parts[0].startswith("python"):
        return False
    script = pathlib.Path(parts[1])
    if script.name not in ALLOWED_SCRIPTS:
        return False
    return _under(str(script), DND_SKILL_DIR)


# ── the tool schemas the model sees ──────────────────────────────────────
# Descriptions are part of the prompt in every practical sense: this is where
# the model learns that dice go through a script and that npcs-full.md is read
# a card at a time. Keep them concrete.
def tool_schemas(campaign_dir: pathlib.Path) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Прочитать файл модуля или кампании. Возвращает текст с номерами строк. "
                    "Большие файлы (npcs-full.md, source/*.md) обрезаются — читай нужный "
                    "фрагмент через offset/limit или найди его через grep_files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Абсолютный путь или путь относительно каталога кампании"},
                        "offset": {"type": "integer", "description": "С какой строки начать (1 = начало)"},
                        "limit": {"type": "integer", "description": "Сколько строк прочитать"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "Записать файл в каталог кампании (state.md, characters/*.md, session-log.md). "
                    "Перезаписывает целиком. Файлы модуля менять нельзя."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": (
                    "Заменить фрагмент текста в файле кампании. old_text должен встречаться "
                    "ровно один раз. Так правят хиты и ресурсы, не перезаписывая весь лист."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob_files",
                "description": "Найти файлы по шаблону, например 'source/*.md' или 'characters/*.md'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "root": {"type": "string", "description": "Где искать; по умолчанию каталог кампании"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep_files",
                "description": (
                    "Искать текст по файлам модуля и кампании. Самый дешёвый способ найти "
                    "карточку NPC, статблок или комнату, не читая файл целиком."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Регулярное выражение или подстрока"},
                        "path": {"type": "string", "description": "Файл или каталог; по умолчанию пак модуля"},
                        "context": {"type": "integer", "description": "Сколько строк контекста вокруг совпадения"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "roll_dice",
                "description": (
                    "ОБЯЗАТЕЛЬНЫЙ бросок костей через скрипт кампании. Любая проверка, атака, "
                    "спасбросок, урон или случайная таблица проходит здесь. Никогда не придумывай "
                    "результат броска сам. Примеры notation: 'd20+5', '2d8+3', 'd20 adv', '4d6kh3'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "notation": {"type": "string", "description": "Кость и модификатор, например d20+5"},
                        "label": {"type": "string", "description": "Что бросаем, например 'Проверка Внимательности'"},
                    },
                    "required": ["notation"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_script",
                "description": (
                    "Запустить вспомогательный скрипт D&D: "
                    + ", ".join(sorted(ALLOWED_SCRIPTS))
                    + ". Для костей используй roll_dice."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": f"Например: python3 {DND_SKILL_DIR}/scripts/xp.py --help",
                        },
                    },
                    "required": ["command"],
                },
            },
        },
    ]


class Sandbox:
    """Executes the DM's tool calls for one campaign, inside the rules above."""

    def __init__(self, campaign_dir: pathlib.Path):
        self.campaign_dir = pathlib.Path(campaign_dir)
        self.calls = 0
        self.denials = 0
        # Every roll the DM made this session, so a test can prove the dice
        # came from the script and were not invented in prose.
        self.rolls: list = []

    # ── path resolution ──────────────────────────────────────────────────
    def _resolve(self, raw: str) -> pathlib.Path:
        """Interpret a model-supplied path; bare names are campaign-relative."""
        p = pathlib.Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = self.campaign_dir / p
        return p

    def _readable(self, p: pathlib.Path) -> bool:
        return (_under(p, self.campaign_dir) or _under(p, MODULE_DIR)
                or _under(p, DND_SKILL_DIR))

    def _writable(self, p: pathlib.Path) -> bool:
        return _under(p, self.campaign_dir)

    # ── dispatch ─────────────────────────────────────────────────────────
    def run(self, name: str, args: dict) -> str:
        """Run one tool call. Always returns a string; never raises."""
        self.calls += 1
        try:
            fn = getattr(self, f"_t_{name}", None)
            if fn is None:
                self.denials += 1
                return f"ОШИБКА: инструмента '{name}' не существует."
            return fn(args or {})
        except Exception as e:                      # noqa: BLE001 — see docstring
            # A tool that raises would end the turn. A tool that reports its
            # failure lets the DM try something else and keep the scene alive.
            return f"ОШИБКА {type(e).__name__}: {e}"

    # ── tools ────────────────────────────────────────────────────────────
    def _t_read_file(self, a: dict) -> str:
        p = self._resolve(a.get("path", ""))
        if not self._readable(p):
            self.denials += 1
            return ("ОТКАЗАНО: чтение вне каталога кампании, пака модуля и скилла "
                    f"запрещено ({p}).")
        if not p.is_file():
            return f"ОШИБКА: файла нет: {p}"

        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        offset = max(1, int(a.get("offset") or 1))
        limit = a.get("limit")
        limit = int(limit) if limit else total
        window = lines[offset - 1: offset - 1 + limit]

        body, cut = [], False
        size = 0
        for i, line in enumerate(window, start=offset):
            if size + len(line) > MAX_READ_CHARS:
                cut = True
                break
            body.append(f"{i}\t{line}")
            size += len(line) + 1

        out = "\n".join(body)
        shown_to = offset + len(body) - 1
        if cut or shown_to < total:
            out += (f"\n\n[показаны строки {offset}–{shown_to} из {total}. "
                    f"Дальше: read_file(path, offset={shown_to + 1}) "
                    f"или grep_files для точного места.]")
        return out or "[файл пуст]"

    def _t_write_file(self, a: dict) -> str:
        p = self._resolve(a.get("path", ""))
        if not self._writable(p):
            self.denials += 1
            return ("ОТКАЗАНО: запись разрешена только в каталог кампании "
                    f"({self.campaign_dir}). Файлы модуля менять нельзя.")
        content = a.get("content", "")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Записано: {p.name} ({len(content)} знаков)"

    def _t_edit_file(self, a: dict) -> str:
        p = self._resolve(a.get("path", ""))
        if not self._writable(p):
            self.denials += 1
            return ("ОТКАЗАНО: правка разрешена только в каталоге кампании "
                    f"({self.campaign_dir}).")
        if not p.is_file():
            return f"ОШИБКА: файла нет: {p}"
        old, new = a.get("old_text", ""), a.get("new_text", "")
        if not old:
            return "ОШИБКА: old_text пуст. Для полной перезаписи используй write_file."
        text = p.read_text(encoding="utf-8")
        n = text.count(old)
        if n == 0:
            return ("ОШИБКА: old_text не найден. Прочитай файл через read_file и "
                    "скопируй фрагмент точно, вместе с отступами.")
        if n > 1:
            return (f"ОШИБКА: old_text встречается {n} раз — правка неоднозначна. "
                    "Возьми фрагмент подлиннее, чтобы он был уникален.")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"Изменено: {p.name}"

    def _t_glob_files(self, a: dict) -> str:
        pattern = a.get("pattern", "*")
        root = self._resolve(a.get("root") or str(self.campaign_dir))
        if not self._readable(root):
            self.denials += 1
            return f"ОТКАЗАНО: поиск вне разрешённых каталогов ({root})."
        if not root.is_dir():
            return f"ОШИБКА: каталога нет: {root}"
        hits = sorted(str(m.relative_to(root)) for m in root.glob(pattern) if m.is_file())
        if not hits:
            return f"[совпадений нет: {pattern} в {root}]"
        out = hits[:MAX_GLOB_HITS]
        tail = "" if len(hits) <= MAX_GLOB_HITS else f"\n[…и ещё {len(hits) - MAX_GLOB_HITS}]"
        return "\n".join(out) + tail

    def _t_grep_files(self, a: dict) -> str:
        pattern = a.get("pattern", "")
        if not pattern:
            return "ОШИБКА: pattern пуст."
        target = self._resolve(a.get("path") or str(MODULE_DIR))
        if not self._readable(target):
            self.denials += 1
            return f"ОТКАЗАНО: поиск вне разрешённых каталогов ({target})."
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = re.compile(re.escape(pattern), re.I)

        ctx = max(0, min(int(a.get("context") or 0), 6))
        files = [target] if target.is_file() else [
            f for f in sorted(target.rglob("*.md")) if f.is_file()]

        out, hits = [], 0
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if not rx.search(line):
                    continue
                hits += 1
                if hits > MAX_GREP_HITS:
                    out.append(f"[…обрезано на {MAX_GREP_HITS} совпадениях — уточни запрос]")
                    return "\n".join(out)
                name = f.name if f == target else str(f.relative_to(target))
                if ctx:
                    lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
                    out.append(f"--- {name}:{i + 1}")
                    out.extend(f"{j + 1}\t{lines[j]}" for j in range(lo, hi))
                else:
                    out.append(f"{name}:{i + 1}\t{line.strip()}")
        return "\n".join(out) if out else f"[совпадений нет: {pattern}]"

    def _t_roll_dice(self, a: dict) -> str:
        notation = str(a.get("notation", "")).strip()
        label = str(a.get("label", "")).strip()
        if not notation:
            return "ОШИБКА: notation пуст, например 'd20+5'."
        # Built from parts, never from model text interpolated into a shell
        # string — there is no shell here at all.
        cmd = [_python(), str(DND_SKILL_DIR / "scripts" / "dice.py"), notation]
        if label:
            cmd += ["--label", label]
        out = self._exec(cmd)
        self.rolls.append({"notation": notation, "label": label, "output": out})
        return out

    def _t_run_script(self, a: dict) -> str:
        cmd = str(a.get("command", ""))
        if not bash_allowed(cmd):
            self.denials += 1
            return ("ОТКАЗАНО: доступны только вспомогательные скрипты D&D "
                    f"({', '.join(sorted(ALLOWED_SCRIPTS))}), "
                    "одной командой без ';', '|', '&' и подстановок. "
                    "Для чтения и записи используй read_file/write_file.")
        return self._exec(shlex.split(cmd))

    # ── process execution ────────────────────────────────────────────────
    def _exec(self, argv: list) -> str:
        """Run an allowlisted script. No shell, hard timeout, output capped."""
        try:
            r = subprocess.run(
                argv, capture_output=True, text=True, timeout=SCRIPT_TIMEOUT,
                cwd=str(self.campaign_dir), shell=False,
            )
        except subprocess.TimeoutExpired:
            return f"ОШИБКА: скрипт не ответил за {SCRIPT_TIMEOUT} с."
        except (OSError, ValueError) as e:
            return f"ОШИБКА запуска: {e}"
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            return f"Скрипт вернул код {r.returncode}.\n{out}\n{err}".strip()
        return (out or "[скрипт ничего не вывел]")[:8000]


def _python() -> str:
    """The interpreter the helper scripts run under — this one."""
    import sys
    return sys.executable or "python3"
