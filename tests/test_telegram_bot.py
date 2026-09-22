"""
test_telegram_bot.py — the bot's pure logic: message shaping and tool gating.

Nothing here touches Telegram or Claude. The two things worth testing offline
are the ones that are silently wrong in production otherwise: how DM prose is
turned into messages, and what the agent is allowed to do with a shell.

Run from repo root:
    python3 -m unittest tests.test_telegram_bot -v
"""
import contextlib
import importlib.util
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
BOT = REPO / "telegram-bot"


@contextlib.contextmanager
def _clean_sys_path():
    """Hide `skills/dnd/scripts` from sys.path for the duration of an import.

    That directory contains `calendar.py`, which shadows the standard library
    once anything puts it on sys.path — and sibling tests in this suite do,
    permanently. The failure lands far away and reads as nonsense
    (`cannot import name 'timegm' from 'calendar'`) because stdlib
    `http.cookiejar` is what finally trips over it, so import the SDK with a
    clean path rather than depending on test execution order.
    """
    # Compare resolved paths: sibling tests add this directory in unresolved
    # forms such as `skills/dnd/display/../scripts`, which no suffix match
    # would catch.
    poison = (REPO / "skills" / "dnd" / "scripts").resolve()

    def _is_poison(p):
        try:
            return pathlib.Path(p).resolve() == poison
        except OSError:
            return False

    saved_path = list(sys.path)
    sys.path[:] = [p for p in sys.path if not _is_poison(p)]

    # Dropping the path is not enough once a sibling test has already imported
    # the repo's `calendar` under the stdlib name: the bad module sits in
    # sys.modules and `from calendar import timegm` finds it there. Evict any
    # stdlib-named module that is actually loaded from this repository.
    evicted = {}
    for name in ("calendar",):
        mod = sys.modules.get(name)
        origin = getattr(mod, "__file__", "") or ""
        if mod is not None and str(REPO) in origin:
            evicted[name] = sys.modules.pop(name)
    try:
        yield
    finally:
        sys.path[:] = saved_path
        sys.modules.update(evicted)


def _import(name):
    with _clean_sys_path():
        if str(BOT) not in sys.path:
            sys.path.insert(0, str(BOT))
        spec = importlib.util.spec_from_file_location(name, str(BOT / f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


HAVE_SDK = importlib.util.find_spec("claude_agent_sdk") is not None


class ConfigTests(unittest.TestCase):
    def test_imports_without_a_token(self):
        # Importing settings must never require a live secret, or tests and
        # tooling would need one just to start.
        cfg = _import("config")
        self.assertTrue(callable(cfg.token))
        self.assertEqual(len(cfg.PREGENS), 5)


class FormattingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.f = _import("tg_format")

    def test_map_marker_extracted_and_removed(self):
        clean, maps = self.f.extract_maps("Вы на берегу.\n\n[[map:2]]\n\nЧто дальше?")
        self.assertEqual(maps, [2])
        self.assertNotIn("[[map", clean)

    def test_marker_removal_leaves_no_gap(self):
        clean, _ = self.f.extract_maps("Абзац.\n\n[[map:2]]\n\nВторой.")
        self.assertEqual(clean, "Абзац.\n\nВторой.")

    def test_repeated_marker_yields_one_map(self):
        _clean, maps = self.f.extract_maps("[[map:3]] и снова [[map:3]]")
        self.assertEqual(maps, [3])

    def test_several_markers_keep_order(self):
        _clean, maps = self.f.extract_maps("[[map:5]] потом [[map:1]]")
        self.assertEqual(maps, [5, 1])

    def test_html_is_escaped_before_styling(self):
        # Player or module text containing < > must not become live markup.
        self.assertEqual(self.f.to_html("<b>не тег</b>"),
                         "&lt;b&gt;не тег&lt;/b&gt;")

    def test_bold_and_italic_converted(self):
        self.assertEqual(self.f.to_html("*Рунара* и _шёпот_"),
                         "<b>Рунара</b> и <i>шёпот</i>")

    def test_unmatched_marker_does_not_swallow_the_message(self):
        out = self.f.to_html("бросок 3*4 и дальше текст")
        self.assertNotIn("<b>", out)

    def test_underscore_inside_a_word_is_left_alone(self):
        self.assertEqual(self.f.to_html("файл state_md тут"), "файл state_md тут")

    def test_short_text_is_one_chunk(self):
        self.assertEqual(self.f.chunk("Коротко."), ["Коротко."])

    def test_empty_text_yields_nothing(self):
        self.assertEqual(self.f.chunk(""), [])

    def test_long_text_splits_under_the_limit(self):
        chunks = self.f.chunk("Предложение раз. " * 500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= self.f.CHUNK_TARGET for c in chunks))

    def test_split_prefers_paragraph_boundaries(self):
        text = ("А" * 3000) + "\n\n" + ("Б" * 3000)
        chunks = self.f.chunk(text)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].startswith("А"))
        self.assertTrue(chunks[1].startswith("Б"))

    def test_unbroken_run_is_hard_split_within_telegram_limit(self):
        chunks = self.f.chunk("я" * 9000)
        self.assertTrue(all(len(c) <= self.f.TELEGRAM_LIMIT for c in chunks))
        self.assertEqual("".join(chunks), "я" * 9000)


class TranscriptTests(unittest.TestCase):
    """The append-only play record."""

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.t = _import("transcript")

    def setUp(self):
        import tempfile
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def test_appends_in_speaker_format(self):
        self.t.append(self.dir, "DnD Master", "Вы на причале.")
        self.t.append(self.dir, "Sofrony", "осматриваюсь")
        text = self.t.path_for(self.dir).read_text(encoding="utf-8")
        self.assertEqual(
            text, "> DnD Master:\nВы на причале.\n\n> Sofrony:\nосматриваюсь\n\n")

    def test_blank_input_is_not_recorded(self):
        self.t.append(self.dir, "Sofrony", "   ")
        self.assertFalse(self.t.path_for(self.dir).exists())

    def test_never_raises_on_an_unwritable_target(self):
        # A logger that can stop the game is worse than a gap in the log.
        self.t.append(self.dir / "nope" / "\0bad", "X", "text")

    def test_survives_repeated_appends(self):
        for i in range(50):
            self.t.append(self.dir, "Sofrony", f"ход {i}")
        self.assertEqual(
            self.t.path_for(self.dir).read_text(encoding="utf-8").count("> Sofrony:"), 50)


class CampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.c = _import("campaign")

    def test_cyrillic_names_slug_to_ascii(self):
        self.assertEqual(self.c.slug("Дарин Камнещит"), "darin-kamneschit")

    def test_slug_never_empty(self):
        self.assertEqual(self.c.slug("!!!"), "pc")

    def test_pregen_meta_known_and_unknown(self):
        self.assertEqual(self.c.pregen_meta("rogue-halfling")["klass"], "Плут")
        self.assertIsNone(self.c.pregen_meta("bard-tiefling"))


@unittest.skipUnless(HAVE_SDK, "claude-agent-sdk not installed in this interpreter")
class ToolGateTests(unittest.TestCase):
    """The allowlist standing between internet input and a shell."""

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.eng = _import("dm_engine")
        cls.skill = REPO / "skills" / "dnd"

    def allowed(self, cmd):
        return self.eng.DMSession._bash_allowed(cmd)

    def test_helper_script_allowed(self):
        self.assertTrue(self.allowed(f'python3 {self.skill}/scripts/dice.py d20+5 --label "x"'))

    def test_chaining_refused(self):
        self.assertFalse(self.allowed(f"python3 {self.skill}/scripts/dice.py d20; rm -rf /"))

    def test_pipe_refused(self):
        self.assertFalse(self.allowed(f"python3 {self.skill}/scripts/dice.py d20 | sh"))

    def test_command_substitution_refused(self):
        self.assertFalse(self.allowed(f"python3 {self.skill}/scripts/dice.py $(whoami)"))

    def test_script_outside_the_skill_refused(self):
        self.assertFalse(self.allowed("python3 /tmp/evil.py"))

    def test_path_traversal_refused(self):
        self.assertFalse(self.allowed(f"python3 {self.skill}/scripts/../../../evil.py"))

    def test_unlisted_script_in_skill_refused(self):
        self.assertFalse(self.allowed(f"python3 {self.skill}/scripts/build_srd.py"))

    def test_non_python_command_refused(self):
        self.assertFalse(self.allowed("cat /etc/passwd"))

    def test_empty_command_refused(self):
        self.assertFalse(self.allowed(""))

    def test_unbalanced_quotes_refused(self):
        self.assertFalse(self.allowed(f"python3 {self.skill}/scripts/dice.py 'unterminated"))

    def test_allowed_tools_stays_empty_so_the_gate_is_consulted(self):
        """Regression: naming a tool in allowed_tools pre-approves it.

        The SDK then never calls can_use_tool, and the whole allowlist above
        becomes decorative while still looking present in the source. This
        failed silently in production once; the only symptom was a warning
        on stderr.
        """
        opts = self.eng.DMSession(0, REPO, "x").build_options()
        self.assertEqual(list(opts.allowed_tools), [],
                         "allowed_tools must stay empty or can_use_tool is skipped")
        self.assertIsNotNone(opts.can_use_tool)
        self.assertIn("Bash", list(opts.tools))

    def test_maps_are_tracked_per_session(self):
        """A map shown once must not be re-sent when the DM re-emits it."""
        s = self.eng.DMSession(0, REPO, "x")
        self.assertEqual(s.maps_shown, set())
        s.maps_shown.add(1)
        self.assertIn(1, s.maps_shown)

    def test_tool_surface_is_bounded(self):
        opts = self.eng.DMSession(0, REPO, "x").build_options()
        self.assertNotIn("WebFetch", list(opts.tools))
        self.assertNotIn("WebSearch", list(opts.tools))


if __name__ == "__main__":
    unittest.main()
