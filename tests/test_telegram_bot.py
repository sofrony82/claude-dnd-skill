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


class SavePointTests(unittest.TestCase):
    """What a resuming DM is told about play that state.md may not hold."""

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.t = _import("transcript")
        cls.p = _import("prompts")

    def setUp(self):
        import tempfile
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "state.md").write_text("## Current Situation\n", encoding="utf-8")

    def say(self, who, text):
        self.t.append(self.dir, who, text)

    def age_state(self, seconds=60):
        import os
        import time
        t = time.time() - seconds
        os.utime(self.dir / "state.md", (t, t))

    def test_fresh_campaign_has_nothing_pending(self):
        self.t.mark_saved(self.dir)
        self.say("DnD Master", "Отряд собран.")
        self.assertIsNone(self.t.pending(self.dir))

    def test_turns_after_the_save_are_pending(self):
        self.say("Sofrony", "иду вверх")
        self.say("DnD Master", "Тропа крутая.")
        self.t.mark_saved(self.dir)
        self.say("Sofrony", "стреляю лучом холода")
        self.say("DnD Master", "Гарпия теряет 6 хитов.")
        tail = self.t.pending(self.dir)
        self.assertEqual(tail["player_turns"], 1)
        self.assertTrue(tail["exact"])
        self.assertNotIn("иду вверх", tail["text"])
        self.assertIn("> Sofrony:\nстреляю лучом холода", tail["text"])
        self.assertIn("Гарпия теряет 6 хитов.", tail["text"])
        self.assertTrue(self.t.has_unsaved(self.dir))

    def test_marking_again_clears_it(self):
        self.say("Sofrony", "ход")
        self.t.mark_saved(self.dir)
        self.assertFalse(self.t.has_unsaved(self.dir))

    def test_lookups_are_not_play(self):
        self.t.mark_saved(self.dir)
        self.say("Sofrony", "/sheet")
        self.say("DnD Master", "Хиты 7 из 7.")
        self.assertIsNone(self.t.pending(self.dir))

    def test_no_save_point_and_state_newer_than_log(self):
        self.say("Sofrony", "ход")
        import os
        import time
        t = time.time() + 5
        os.utime(self.dir / "state.md", (t, t))
        self.assertIsNone(self.t.pending(self.dir))

    def test_no_save_point_and_state_written_during_the_last_turn(self):
        # The DM saves mid-turn; the narration lands in the log seconds later.
        self.say("Sofrony", "ход")
        self.age_state(15)
        self.assertIsNone(self.t.pending(self.dir))

    def test_no_save_point_and_state_older_takes_the_end_as_a_guess(self):
        self.say("Sofrony", "ход")
        self.age_state(3600)
        tail = self.t.pending(self.dir)
        self.assertFalse(tail["exact"])
        self.assertIn("ход", tail["text"])

    def test_save_point_past_the_end_is_ignored(self):
        self.say("Sofrony", "ход")
        (self.dir / self.t.CHECKPOINT).write_text('{"raw_log_bytes": 999999}')
        self.age_state(3600)
        self.assertFalse(self.t.pending(self.dir)["exact"])

    def test_tail_is_capped_on_whole_entries(self):
        self.t.mark_saved(self.dir)
        for i in range(40):
            self.say("Sofrony", f"ход {i} " + "я" * 200)
        tail = self.t.pending(self.dir, limit=2000)
        self.assertTrue(tail["truncated"])
        self.assertEqual(tail["player_turns"], 40)
        self.assertLessEqual(len(tail["text"]), 2000)
        self.assertTrue(tail["text"].startswith("> Sofrony:\nход "))
        self.assertIn("ход 39", tail["text"])

    def test_prompt_carries_the_tail_only_when_there_is_one(self):
        self.t.mark_saved(self.dir)
        prompt = self.p.build_system_prompt("- X", self.dir, self.dir, "deepseek")
        self.assertNotIn("ПОСЛЕ ПОСЛЕДНЕГО СОХРАНЕНИЯ", prompt)
        self.say("Sofrony", "открываю сундук")
        prompt = self.p.build_system_prompt("- X", self.dir, self.dir, "deepseek")
        self.assertIn("ПОСЛЕ ПОСЛЕДНЕГО СОХРАНЕНИЯ", prompt)
        self.assertIn("открываю сундук", prompt)


class PromptTests(unittest.TestCase):
    """What the DM is told depends on the table it is running."""

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.p = _import("prompts")

    def build(self, n):
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp())
        party = "\n".join(f"- P{i} — Эльф Волшебник (лист: characters/p{i}.md)"
                          for i in range(n))
        return self.p.build_system_prompt(party, d, d, "deepseek")

    def test_small_party_gets_scaling_rules(self):
        """A solo wizard met the four-player harpy fight as printed and fell twice."""
        solo = self.build(1)
        self.assertIn("МАЛЫЙ ОТРЯД — 1 ИЗ 4", solo)
        self.assertIn("Одиночке", solo)
        self.assertIn("МАЛЫЙ ОТРЯД — 3 ИЗ 4", self.build(3))
        self.assertNotIn("МАЛЫЙ ОТРЯД", self.build(4))

    def test_combat_checklist_closes_the_prompt(self):
        self.assertTrue(self.build(1).rstrip().endswith(
            self.p.COMBAT_CHECKLIST.strip()))

    def test_player_is_told_the_dice_are_shown(self):
        self.assertIn("hidden=true", self.build(2))


class DiceLogTests(unittest.TestCase):
    """The dice log the bot puts under every DM turn — see dice_log.py."""

    @classmethod
    def setUpClass(cls):
        cls.d = _import("dice_log")

    def roll(self, output, label="Атака", hidden=False):
        return {"notation": "d20", "label": label, "output": output, "hidden": hidden}

    def test_summaries_of_every_dice_py_shape(self):
        s = self.d.summarize
        self.assertEqual(s("Roll: 9 + 3 = 12"), "9 + 3 = 12")
        self.assertEqual(s("Roll: 20 = 20  *** CRITICAL HIT (nat 20)! ***"),
                         "20 — натуральная 20")
        self.assertEqual(s("Roll: 1 - 1 = 0  *** FUMBLE (nat 1)! ***"),
                         "1 - 1 = 0 — натуральная 1")
        self.assertEqual(s("Rolls: [4, 1] + 2 = 7"), "4 + 1 + 2 = 7")
        self.assertEqual(s("Rolls: [7] = 7"), "7")
        self.assertEqual(s("[ADV] Roll A: [14] + 3 = 17\n[ADV] Roll B: [6] + 3 = 9\n"
                           "Takes roll A → Total: 17"),
                         "14 и 6 (преимущество) + 3 → 17")
        self.assertEqual(s("[DIS] Roll A: [19] = 19\n[DIS] Roll B: [2] = 2\n"
                           "Takes roll B → Total: 2"), "19 и 2 (помеха) → 2")
        self.assertEqual(s("Rolls: [6, 5, 3, 1]  (dropped: [1])\n"
                           "Kept (kh3): [6 + 5 + 3] = 14"), "6 + 5 + 3 = 14")
        self.assertEqual(s("ОШИБКА: notation пуст"), "бросок не удался")

    def test_real_advantage_output_reads_left_to_right(self):
        """dice.py used to print `[14] = 17 + 3`; the DM copied it as nonsense."""
        import re
        import subprocess
        out = subprocess.run(
            [sys.executable, str(REPO / "skills" / "dnd" / "scripts" / "dice.py"),
             "d20+3 adv"], capture_output=True, encoding="utf-8",
            env={"DND_DICE_PHYSICAL": "0", "PYTHONIOENCODING": "utf-8",
                 "PATH": "/usr/bin:/bin"}).stdout
        self.assertRegex(out, r"Roll A: \[\d+\] \+ 3 = \d+")
        self.assertRegex(self.d.summarize(out), r"^\d+ и \d+ \(преимущество\) \+ 3 → \d+$")

    def test_secret_rolls_show_that_they_happened_and_nothing_else(self):
        lines = self.d.lines([self.roll("Roll: 3 + 2 = 5", "Скрытность упыря", True)])
        self.assertEqual(lines, [self.d.HIDDEN])
        self.assertIn("Скрытность упыря", self.d.for_dm(
            [self.roll("Roll: 3 + 2 = 5", "Скрытность упыря", True)]))

    def test_html_is_escaped_and_collapsed(self):
        out = self.d.to_html([self.roll("Roll: 9 + 3 = 12", "<b>Атака</b>")])
        self.assertTrue(out.startswith("<blockquote expandable>"))
        self.assertIn("&lt;b&gt;Атака&lt;/b&gt;: 9 + 3 = 12", out)

    def test_no_rolls_no_message(self):
        self.assertEqual(self.d.to_html([]), "")
        self.assertEqual(self.d.text([]), "")

    def test_a_runaway_turn_is_capped(self):
        many = [self.roll("Roll: 5 = 5")] * (self.d.MAX_LINES + 7)
        lines = self.d.lines(many)
        self.assertEqual(len(lines), self.d.MAX_LINES + 1)
        self.assertEqual(lines[-1], "…и ещё 7")


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


class CampaignSwitchTests(unittest.TestCase):
    """Several campaigns per player, one active, deletion to trash."""

    USER = 401712068

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.c = _import("campaign")

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._saved = {k: getattr(self.c, k) for k in
                       ("USERS_DIR", "LEGACY_CAMPAIGNS_DIR", "LEGACY_CHATS_DIR",
                        "MODULE_DIR")}
        self.c.USERS_DIR = root / "users"
        self.c.LEGACY_CAMPAIGNS_DIR = root / "campaigns"
        self.c.LEGACY_CHATS_DIR = root / "chats"
        self.c.MODULE_DIR = root / "module"
        self.c.MODULE_DIR.mkdir()
        (self.c.MODULE_DIR / "state-seed.md").write_text(
            "# Заготовка состояния\n\n*Стартовый срез для НОВОГО прохождения.*\n\n---\n\n"
            "## Current Situation\n- **Location:** Причал\n", encoding="utf-8")
        self.first = self.c.create(self.USER, self.party("Дилион")).name
        (self.c.campaigns_root(self.USER) / self.first / "state.md").write_text(
            "## Current Situation\n- **Location:** Обломки «Розы Ветров»\n",
            encoding="utf-8")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.c, k, v)
        self._tmp.cleanup()

    def party(self, name="Торин"):
        return [{"id": "cleric-dwarf", "name": name, "klass": "Жрец",
                 "race": "Холмовой дварф"}]

    def test_campaigns_live_under_the_player(self):
        cdir = self.c.campaign_dir(self.USER)
        self.assertEqual(cdir.parent, self.c.USERS_DIR / str(self.USER) / "campaigns")
        self.assertEqual(self.c.read_party(self.USER, self.first)["owner"], self.USER)

    def test_new_campaign_leaves_the_old_one_and_becomes_active(self):
        cdir = self.c.create(self.USER, self.party())
        self.assertEqual(self.c.active_id(self.USER), cdir.name)
        self.assertTrue(cdir.name.endswith("-torin"))
        ids = [g["id"] for g in self.c.list_for(self.USER)]
        self.assertCountEqual(ids, [cdir.name, self.first])

    def test_switching_back_and_forth(self):
        new = self.c.create(self.USER, self.party()).name
        self.c.set_active(self.USER, self.first)
        self.assertEqual(self.c.campaign_dir(self.USER).name, self.first)
        self.c.set_active(self.USER, new)
        self.assertEqual(self.c.campaign_dir(self.USER).name, new)

    def test_ids_do_not_clash(self):
        a = self.c.create(self.USER, self.party()).name
        b = self.c.create(self.USER, self.party()).name
        self.assertNotEqual(a, b)
        self.assertEqual(b, a + "-2")

    def test_players_do_not_see_each_other(self):
        theirs = self.c.create(555, self.party("Чужой")).name
        self.assertEqual([g["title"] for g in self.c.list_for(self.USER)], ["Дилион"])
        self.assertIsNone(self.c.read_party(self.USER, theirs))
        self.assertIsNone(self.c.summary(self.USER, theirs))

    def test_list_is_most_recent_first_and_carries_location(self):
        import os
        import time
        new = self.c.create(self.USER, self.party())
        old = time.time() - 3600
        for f in (self.c.campaigns_root(self.USER) / self.first).iterdir():
            os.utime(f, (old, old))
        games = self.c.list_for(self.USER)
        self.assertEqual(games[0]["id"], new.name)
        self.assertEqual(games[0]["location"], "Причал")
        self.assertEqual(games[1]["location"], "Обломки «Розы Ветров»")

    def test_trash_moves_rather_than_erases(self):
        dst = self.c.trash(self.USER, self.first)
        self.assertTrue((dst / "party.json").is_file())
        self.assertEqual(dst.parent, self.c.trash_dir(self.USER))
        self.assertIsNone(self.c.active_id(self.USER))
        self.assertEqual(self.c.list_for(self.USER), [])

    def test_pointer_to_a_trashed_campaign_resolves_to_none(self):
        new = self.c.create(self.USER, self.party()).name
        self.c.trash(self.USER, new)
        self.assertIsNone(self.c.active_id(self.USER))
        self.assertFalse(self.c.exists(self.USER))

    def test_explicit_id_replaces_in_place(self):
        cid = "tg-991700002"
        self.c.create(991700002, self.party("Раз"), campaign_id=cid)
        self.c.create(991700002, self.party("Два"), campaign_id=cid)
        self.assertEqual(self.c.read_party(991700002, cid)["party"][0]["name"], "Два")
        self.assertFalse(self.c.trash_dir(991700002).exists())

    def test_rename(self):
        self.c.rename(self.USER, self.first, "Дилион на острове")
        self.assertEqual(self.c.list_for(self.USER)[0]["title"], "Дилион на острове")

    def test_hostile_ids_refused(self):
        for bad in ("../etc", ".trash", "a/b", "", "X" * 5):
            self.assertFalse(self.c.valid_id(bad), bad)
            self.assertIsNone(self.c.read_party(self.USER, bad))
        with self.assertRaises(ValueError):
            self.c.path(self.USER, "../../x")

    def test_seed_note_is_not_copied_into_state(self):
        cdir = self.c.create(self.USER, self.party())
        state = (cdir / "state.md").read_text(encoding="utf-8")
        self.assertNotIn("НОВОГО прохождения", state)
        self.assertIn("## Current Situation", state)


class FlatLayoutMigrationTests(unittest.TestCase):
    """Campaigns from the flat campaigns/ + chats/ layout move under users/."""

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.c = _import("campaign")

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self._saved = {k: getattr(self.c, k) for k in
                       ("USERS_DIR", "LEGACY_CAMPAIGNS_DIR", "LEGACY_CHATS_DIR")}
        self.c.USERS_DIR = self.root / "users"
        self.c.LEGACY_CAMPAIGNS_DIR = self.root / "campaigns"
        self.c.LEGACY_CHATS_DIR = self.root / "chats"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.c, k, v)
        self._tmp.cleanup()

    def flat(self, rel, party):
        import json
        d = self.c.LEGACY_CAMPAIGNS_DIR / rel
        d.mkdir(parents=True)
        if party is not None:
            (d / "party.json").write_text(json.dumps(party), encoding="utf-8")
        (d / "state.md").write_text("состояние", encoding="utf-8")
        return d

    def test_moves_by_owner_then_chat_then_name(self):
        self.flat("20260922-merri", {"owner": 7, "chat_id": 8, "party": []})
        self.flat("tg-9", {"chat_id": 9, "party": []})
        self.flat("tg-10", None)
        self.c.migrate_flat_layout()
        u = self.c.USERS_DIR
        self.assertTrue((u / "7/campaigns/20260922-merri/state.md").is_file())
        self.assertTrue((u / "9/campaigns/tg-9/state.md").is_file())
        self.assertTrue((u / "10/campaigns/tg-10/state.md").is_file())
        self.assertFalse(self.c.LEGACY_CAMPAIGNS_DIR.exists())

    def test_trash_and_pointers_follow(self):
        self.flat(".trash/20260901-a-20260922-174711", {"owner": 7, "party": []})
        self.c.LEGACY_CHATS_DIR.mkdir()
        (self.c.LEGACY_CHATS_DIR / "7.json").write_text('{"active": "x"}', encoding="utf-8")
        self.c.migrate_flat_layout()
        self.assertTrue((self.c.trash_dir(7) / "20260901-a-20260922-174711").is_dir())
        self.assertTrue((self.c.USERS_DIR / "7" / "active.json").is_file())
        self.assertFalse(self.c.LEGACY_CHATS_DIR.exists())

    def test_the_active_campaign_survives_the_move(self):
        self.flat("20260922-merri", {"owner": 7, "party": [{"name": "М"}]})
        self.c.LEGACY_CHATS_DIR.mkdir()
        (self.c.LEGACY_CHATS_DIR / "7.json").write_text(
            '{"active": "20260922-merri"}', encoding="utf-8")
        self.c.migrate_flat_layout()
        self.assertEqual(self.c.active_id(7), "20260922-merri")

    def test_unknown_owner_is_left_in_place(self):
        d = self.flat("mystery", {"party": []})
        self.c.migrate_flat_layout()
        self.assertTrue(d.is_dir())

    def test_second_run_does_nothing(self):
        self.flat("tg-9", {"chat_id": 9, "party": []})
        self.assertEqual(len(self.c.migrate_flat_layout()), 1)
        self.assertEqual(self.c.migrate_flat_layout(), [])


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


class SandboxTests(unittest.TestCase):
    """The DeepSeek backend's tools — the only implementation it has.

    `dm_engine` only has to *gate* tools the SDK implements, and those tests
    skip without the SDK installed. `sandbox` implements the tools itself, so
    a hole here is a hole in the product on the host that runs DeepSeek — and
    it needs no backend to test.
    """

    @classmethod
    def setUpClass(cls):
        cls.cfg = _import("config")
        cls.sb = _import("sandbox")
        cls.skill = REPO / "skills" / "dnd"

    def setUp(self):
        import tempfile
        self.campaign = pathlib.Path(tempfile.mkdtemp())
        self.box = self.sb.Sandbox(self.campaign)

    # ── shell allowlist ──────────────────────────────────────────────────
    def test_helper_script_allowed(self):
        self.assertTrue(self.sb.bash_allowed(
            f'python3 {self.skill}/scripts/dice.py d20+5 --label "x"'))

    def test_metacharacters_refused(self):
        for cmd in (f"python3 {self.skill}/scripts/dice.py d20; rm -rf /",
                    f"python3 {self.skill}/scripts/dice.py d20 | sh",
                    f"python3 {self.skill}/scripts/dice.py d20 && curl x",
                    f"python3 {self.skill}/scripts/dice.py $(whoami)",
                    f"python3 {self.skill}/scripts/dice.py `id`",
                    f"python3 {self.skill}/scripts/dice.py d20 > /tmp/x"):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.sb.bash_allowed(cmd))

    def test_script_outside_the_skill_refused(self):
        self.assertFalse(self.sb.bash_allowed("python3 /tmp/evil.py"))

    def test_path_traversal_refused(self):
        self.assertFalse(self.sb.bash_allowed(
            f"python3 {self.skill}/scripts/../../../evil.py"))

    def test_unlisted_script_in_skill_refused(self):
        self.assertFalse(self.sb.bash_allowed(
            f"python3 {self.skill}/scripts/build_srd.py"))

    def test_non_python_and_empty_refused(self):
        self.assertFalse(self.sb.bash_allowed("cat /etc/passwd"))
        self.assertFalse(self.sb.bash_allowed(""))

    def test_allowlist_matches_the_sdk_path(self):
        """Two backends, one set of runnable scripts.

        If these drift, a script refused on one backend is permitted on the
        other and the security story stops being reviewable in one place.
        """
        if not HAVE_SDK:
            self.skipTest("claude-agent-sdk not installed")
        eng = _import("dm_engine")
        self.assertEqual(self.sb.ALLOWED_SCRIPTS, eng.ALLOWED_SCRIPTS)

    # ── path containment ─────────────────────────────────────────────────
    def test_read_outside_roots_refused(self):
        for path in ("/etc/passwd", "../../../etc/passwd", "~/.ssh/id_ed25519"):
            with self.subTest(path=path):
                self.assertIn("ОТКАЗАНО", self.box.run("read_file", {"path": path}))

    def test_write_outside_the_campaign_refused(self):
        out = self.box.run("write_file", {"path": "/tmp/evil.txt", "content": "x"})
        self.assertIn("ОТКАЗАНО", out)
        self.assertFalse(pathlib.Path("/tmp/evil.txt").exists())

    def test_module_pack_is_read_only(self):
        target = self.cfg.MODULE_DIR / "world.md"
        out = self.box.run("write_file", {"path": str(target), "content": "x"})
        self.assertIn("ОТКАЗАНО", out)

    def test_write_and_read_inside_the_campaign(self):
        self.assertIn("Записано", self.box.run(
            "write_file", {"path": "state.md", "content": "хиты: 8"}))
        self.assertIn("хиты: 8", self.box.run("read_file", {"path": "state.md"}))

    def test_relative_paths_land_in_the_campaign(self):
        self.box.run("write_file", {"path": "characters/x.md", "content": "лист"})
        self.assertTrue((self.campaign / "characters" / "x.md").is_file())

    # ── behaviour that protects the turn, not the host ────────────────────
    def test_unknown_tool_reports_instead_of_raising(self):
        self.assertIn("не существует", self.box.run("no_such_tool", {}))

    def test_edit_refuses_an_ambiguous_match(self):
        self.box.run("write_file", {"path": "s.md", "content": "хиты\nхиты"})
        out = self.box.run("edit_file",
                           {"path": "s.md", "old_text": "хиты", "new_text": "хп"})
        self.assertIn("2 раз", out)

    def test_edit_reports_a_missing_match(self):
        self.box.run("write_file", {"path": "s.md", "content": "хиты: 8"})
        out = self.box.run("edit_file",
                           {"path": "s.md", "old_text": "нету", "new_text": "x"})
        self.assertIn("не найден", out)

    def test_truncated_read_says_so(self):
        """A model that thinks it saw a whole file narrates from the half it got."""
        big = "строка текста " * 6000
        self.box.run("write_file", {"path": "big.md", "content": big})
        out = self.box.run("read_file", {"path": "big.md"})
        self.assertLess(len(out), self.sb.MAX_READ_CHARS + 2000)

    def test_denials_are_counted(self):
        self.box.run("read_file", {"path": "/etc/passwd"})
        self.assertEqual(self.box.denials, 1)

    def test_rolls_are_recorded_for_audit(self):
        """`rolls` is how a replay proves the dice were not invented in prose."""
        self.box.run("roll_dice", {"notation": "d20+3", "label": "Проверка"})
        self.assertEqual(len(self.box.rolls), 1)
        self.assertEqual(self.box.rolls[0]["notation"], "d20+3")

    def test_hidden_roll_is_marked_and_only_when_asked(self):
        """A string "false" from model-written JSON must not hide a roll."""
        self.box.run("roll_dice", {"notation": "d20+2", "label": "Скрытность",
                                   "hidden": True})
        self.box.run("roll_dice", {"notation": "d20", "label": "Атака",
                                   "hidden": "false"})
        self.box.run("roll_dice", {"notation": "d20", "label": "Спасбросок"})
        self.assertEqual([r["hidden"] for r in self.box.rolls], [True, False, False])

    def test_tool_schemas_expose_no_general_shell(self):
        names = {s["function"]["name"] for s in self.sb.tool_schemas(self.campaign)}
        self.assertIn("roll_dice", names)
        for forbidden in ("bash", "shell", "exec", "python"):
            self.assertNotIn(forbidden, names)


class CampaignPinTests(unittest.TestCase):
    """A helper script may only ever be pointed at the caller's own campaign.

    The scripts take `--campaign NAME` and resolve it under their data root,
    so before this rule a player could talk the DM into `tracker.py -c <someone
    else's campaign> clear --all`. Two layers now: the scripts' root is the
    player's own directory, and the name must be the chat's campaign.
    """

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.sb = _import("sandbox")
        cls.scripts = REPO / "skills" / "dnd" / "scripts"

    def setUp(self):
        import tempfile
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.mine = self.root / "users" / "1" / "campaigns" / "mine"
        self.other = self.root / "users" / "1" / "campaigns" / "other"
        self.theirs = self.root / "users" / "2" / "campaigns" / "theirs"
        for d in (self.mine, self.other, self.theirs):
            d.mkdir(parents=True)
        self.box = self.sb.Sandbox(self.mine)

    def why(self, args):
        return self.sb.check_command(
            f"python3 {self.scripts}/tracker.py {args}", self.mine)

    def test_own_campaign_allowed(self):
        for args in ("-c mine status", "--campaign mine status",
                     "--campaign=mine status", "-cmine status", "-c=mine status"):
            with self.subTest(args=args):
                self.assertIsNone(self.why(args))

    def test_scripts_without_a_campaign_unaffected(self):
        self.assertIsNone(self.sb.check_command(
            f"python3 {self.scripts}/dice.py d20+5", self.mine))

    def test_other_campaign_refused_however_spelled(self):
        for args in ("-c other status", "--campaign other status",
                     "--campaign=other status", "-cother status",
                     "-c=other status", "--camp other status",
                     "--c other status", "-c mine -c other status",
                     "-c ../../2/campaigns/theirs status", "-c .. status",
                     "-c ../../.. status", "-c '' status", "status -c"):
            with self.subTest(args=args):
                self.assertIsNotNone(self.why(args))

    def test_refusal_names_the_right_campaign(self):
        self.assertIn("--campaign mine", self.why("-c other status"))

    def test_other_campaign_untouched_end_to_end(self):
        (self.theirs / "tracker.json").write_text('{"x": 1}', encoding="utf-8")
        out = self.box.run("run_script", {
            "command": f"python3 {self.scripts}/tracker.py -c theirs clear --all"})
        self.assertIn("ОТКАЗАНО", out)
        self.assertEqual((self.theirs / "tracker.json").read_text(encoding="utf-8"),
                         '{"x": 1}')

    def test_scripts_are_rooted_at_the_player(self):
        self.assertEqual(self.sb.script_root(self.mine), (self.root / "users" / "1").resolve())

    def test_own_campaign_runs_end_to_end(self):
        out = self.box.run("run_script", {
            "command": f"python3 {self.scripts}/calendar.py -c mine init --date '1 Month 1'"})
        self.assertNotIn("ОТКАЗАНО", out)
        self.assertTrue((self.mine / "calendar.json").is_file(), out)

    # ── other ways out: arguments that are paths, and glob patterns ──────
    def test_xp_character_name_cannot_be_a_path(self):
        """--campaign was pinned, but --characters went into the path as is."""
        (self.mine / "characters").mkdir()
        (self.theirs / "characters").mkdir()
        sheet = self.theirs / "characters" / "bob.md"
        sheet.write_text("**Level:** 1\n**XP:** 0 / 300\n", encoding="utf-8")
        out = self.box.run("run_script", {"command": (
            f"python3 {self.scripts}/xp.py award --campaign mine "
            "--characters ../../../../2/campaigns/theirs/characters/bob "
            "--type noncombat --difficulty easy")})
        self.assertIn("Invalid character name", out)
        self.assertIn("**XP:** 0 / 300", sheet.read_text(encoding="utf-8"), out)

    def test_glob_cannot_climb_out_of_its_root(self):
        (self.theirs / "state.md").write_text("x", encoding="utf-8")
        for pattern in ("../../../*/campaigns/*/*", "../*", "**/../../*",
                        "/etc/*", "~/*"):
            with self.subTest(pattern=pattern):
                out = self.box.run("glob_files", {"pattern": pattern})
                self.assertIn("ОТКАЗАНО", out)
                self.assertNotIn("theirs", out)

    def test_glob_does_not_list_through_a_symlink(self):
        (self.theirs / "state.md").write_text("x", encoding="utf-8")
        (self.mine / "link").symlink_to(self.theirs)
        self.assertNotIn("state.md", self.box.run("glob_files", {"pattern": "link/*"}))

    def test_grep_does_not_read_through_a_symlink(self):
        (self.theirs / "state.md").write_text("тайна", encoding="utf-8")
        (self.mine / "link").symlink_to(self.theirs)
        out = self.box.run("grep_files", {"pattern": "тайна", "path": str(self.mine)})
        self.assertNotIn("тайна", out.replace("[совпадений нет: тайна]", ""))

    def test_grep_finds_within_the_campaign(self):
        (self.mine / "state.md").write_text("хиты: 8\nместо: пещера", encoding="utf-8")
        out = self.box.run("grep_files", {"pattern": "пещ", "path": str(self.mine)})
        self.assertEqual(out, "state.md:2\tместо: пещера")

    def test_runaway_regex_is_killed(self):
        """A backtracking pattern must cost the turn seconds, not a core forever."""
        (self.mine / "state.md").write_text("a" * 60, encoding="utf-8")
        old, self.sb.GREP_TIMEOUT = self.sb.GREP_TIMEOUT, 1
        try:
            out = self.box.run("grep_files", {"pattern": "(a|a)*b", "path": str(self.mine)})
        finally:
            self.sb.GREP_TIMEOUT = old
        self.assertIn("не уложился", out)

    def test_scripts_do_not_see_the_secrets(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t",
                                          "NB_STUDIO_API_KEY": "k"}):
            env = self.sb._script_env(self.mine)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", set(env))
        self.assertNotIn("NB_STUDIO_API_KEY", set(env))
        self.assertIn("PATH", env)
        self.assertEqual(env["DND_CAMPAIGN_ROOT"], str(self.sb.script_root(self.mine)))


class NarrationHygieneTests(unittest.TestCase):
    """Text on its way to a player, from the DeepSeek loop.

    Both regressions here were found by a 33-turn replay, not by reasoning about
    the code: 7 of 33 turns came back with no narration at all, and the model
    was separately observed writing its own tool-call syntax into `content`
    instead of returning a structured call.
    """

    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("openai") is None:
            raise unittest.SkipTest("openai not installed in this interpreter")
        _import("config")
        cls.ds = _import("ds_engine")

    def strip(self, s):
        return self.ds._strip_tool_markup(s)

    def test_plain_prose_is_untouched(self):
        text = "Дрейк бросается на тебя, и когти скрежещут по камню."
        self.assertEqual(self.strip(text), (text, False))

    def test_legitimate_dice_prose_is_not_mistaken_for_markup(self):
        text = "🎲 d20+3 → 14+3 = 17 против СЛ 15 — успех. *Рунара* кивает."
        self.assertEqual(self.strip(text), (text, False))

    def test_dsml_block_is_removed_with_its_arguments(self):
        raw = ('<｜DSML｜ calls>\n<｜DSML｜ invoke name="roll_dice">\n'
               '<｜DSML｜ parameter name="dice">d20</｜DSML｜ parameter>')
        clean, leaked = self.strip(raw)
        self.assertTrue(leaked)
        self.assertEqual(clean, "")
        self.assertNotIn("d20", clean, "argument soup must not reach the player")

    def test_prose_before_a_markup_block_survives(self):
        clean, leaked = self.strip('Он падает.\n<｜DSML｜ invoke name="roll_dice">')
        self.assertTrue(leaked)
        self.assertEqual(clean, "Он падает.")

    def test_paired_tool_call_tags_are_removed_with_contents(self):
        clean, leaked = self.strip("<tool_call>roll_dice</tool_call>Он падает.")
        self.assertTrue(leaked)
        self.assertEqual(clean, "Он падает.")

    def test_empty_input(self):
        self.assertEqual(self.strip(""), ("", False))
        self.assertEqual(self.strip(None), ("", False))

    def test_a_lost_turn_gets_retried(self):
        """An empty turn costs the player their action, so the loop nudges."""
        self.assertGreaterEqual(self.ds.NUDGE_LIMIT, 1)
        self.assertIn("roll_dice", self.ds.NUDGE)

    def test_cut_reply_is_glued_with_a_space(self):
        self.assertEqual(self.ds._glue("Она заносит", "кость."), "Она заносит кость.")
        self.assertEqual(self.ds._glue("Кость", ", и всё"), "Кость, и всё")


def _msg(content="", calls=(), finish="stop"):
    return {"content": content, "finish_reason": finish, "markup_leak": False,
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": n, "arguments": a}}
                           for i, (n, a) in enumerate(calls)]}


class DSLoopTests(unittest.TestCase):
    """The DeepSeek turn loop against a scripted model.

    Each case is a failure from the 2026-09-22 session in chat 401712068: a
    nudge that dropped the model's tool calls, a reply that ended mid-word at
    the token cap, and a whole session in which state.md was never written.
    """

    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("openai") is None:
            raise unittest.SkipTest("openai not installed in this interpreter")
        _import("config")
        cls.ds = _import("ds_engine")

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.s = self.ds.DSSession(1, pathlib.Path(self._tmp.name), "system")
        self.s.client = object()          # never used: _complete is scripted
        # Completions are counted against the daily budget; count them here,
        # not in the live ~/.claude/dnd/usage.json.
        u = self.ds.usage
        saved = (u.USAGE_FILE, u.LOCK_FILE, u.DAILY_COMPLETIONS)
        u.USAGE_FILE = pathlib.Path(self._tmp.name) / "usage.json"
        u.LOCK_FILE = pathlib.Path(self._tmp.name) / "usage.lock"

        def restore():
            u.USAGE_FILE, u.LOCK_FILE, u.DAILY_COMPLETIONS = saved
        self.addCleanup(restore)

    def script(self, *replies):
        queue = list(replies)
        seen = []

        async def fake():
            seen.append([dict(m) for m in self.s.history])
            return queue.pop(0)

        self.s._complete = fake
        return seen

    def ask(self, text="ход"):
        import asyncio
        return asyncio.run(self.s.ask(text))

    def test_nudge_runs_the_tool_calls_it_gets(self):
        self.script(_msg(""),
                    _msg("", calls=[("roll_dice", '{"notation": "d20"}')]),
                    _msg("Ты бросаешь спасбросок от смерти."))
        out = self.ask()
        self.assertEqual(out, "Ты бросаешь спасбросок от смерти.")
        self.assertEqual(len(self.s.sandbox.rolls), 1, "the nudge's roll was dropped")
        roles = [m["role"] for m in self.s.history]
        self.assertIn("tool", roles)

    def test_rolls_are_logged_with_label_and_result(self):
        """A narrated roll has to be checkable against the one actually made."""
        self.script(_msg("", calls=[("roll_dice", '{"notation": "d20", '
                                     '"label": "Спасбросок от смерти"}')]),
                    _msg("Ты держишься."))
        with self.assertLogs("dm.ds", level="INFO") as logs:
            self.ask()
        line = next(l for l in logs.output if "tool roll_dice" in l)
        self.assertIn("d20 «Спасбросок от смерти» -> Roll:", line)

    def test_reply_cut_by_the_limit_is_continued(self):
        seen = self.script(_msg("Она нависает над тобой. Кость", finish="length"),
                           _msg("опускается на доски рядом с головой."))
        out = self.ask()
        self.assertEqual(out, "Она нависает над тобой. Кость опускается на доски "
                              "рядом с головой.")
        self.assertEqual(seen[1][-1]["content"], self.ds.CONTINUE)

    def test_reply_spent_on_reasoning_is_nudged_with_its_rolls(self):
        """Nothing written is nothing to continue: the retry gets the dice instead.

        2026-09-22 19:38: a zombie crit and a ghoul claw were rolled, three
        completions ran out reasoning, "continue from where you stopped" twice
        had nowhere to continue from, and the narration skipped both rolls.
        """
        seen = self.script(
            _msg("", calls=[("roll_dice", '{"notation": "d20+3", '
                             '"label": "Атака зомби"}')]),
            _msg("", finish="length"),
            _msg("Кулак зомби врезается тебе в плечо."))
        out = self.ask()
        self.assertEqual(out, "Кулак зомби врезается тебе в плечо.")
        nudge = seen[2][-1]["content"]
        self.assertNotEqual(nudge, self.ds.CONTINUE)
        self.assertIn("Атака зомби", nudge)
        self.assertIn("окончательные", nudge)
        self.assertEqual(len(self.s.sandbox.rolls), 1, "the retry rolled again")

    def test_last_rolls_are_this_turns_only(self):
        self.script(_msg("", calls=[("roll_dice", '{"notation": "d20", "label": "первый"}')]),
                    _msg("Раз."),
                    _msg("", calls=[("roll_dice", '{"notation": "d6", "label": "второй"}')]),
                    _msg("Два."))
        self.ask()
        self.assertEqual([r["label"] for r in self.s.last_rolls], ["первый"])
        self.ask()
        self.assertEqual([r["label"] for r in self.s.last_rolls], ["второй"])

    def test_continuation_is_bounded(self):
        cut = [_msg("часть", finish="length")] * (self.ds.CONTINUE_LIMIT + 1)
        self.script(*cut)
        out = self.ask()
        self.assertEqual(out, " ".join(["часть"] * (self.ds.CONTINUE_LIMIT + 1)))

    def test_state_reminder_after_unsaved_turns(self):
        n = self.ds.SAVE_REMIND_TURNS
        if n <= 0:
            self.skipTest("reminder disabled by DND_SAVE_REMIND_TURNS")
        self.script(*[_msg("сцена")] * (n + 1))
        for _ in range(n):
            self.ask()
        self.assertEqual(self.s.turns_unsaved, n)
        self.ask("иду дальше")
        last_player = [m for m in self.s.history if m["role"] == "user"][-1]
        self.assertIn("state.md", last_player["content"])
        self.assertTrue(last_player["content"].startswith("иду дальше"))

    def test_writing_state_resets_the_counter(self):
        self.script(_msg("сцена"),
                    _msg("", calls=[("write_file",
                                     '{"path": "state.md", "content": "x"}')]),
                    _msg("Записал."))
        self.ask()
        self.assertEqual(self.s.turns_unsaved, 1)
        self.ask()
        self.assertEqual(self.s.turns_unsaved, 0)
        self.assertEqual(self.s.sandbox.writes, ["state.md"])

    def test_no_token_cap_by_default(self):
        """Uncapped unless configured: the cap counts reasoning and cuts prose."""
        import asyncio
        from types import SimpleNamespace as NS
        captured = {}

        async def create(**kw):
            captured.update(kw)
            return NS(usage=NS(total_tokens=5, prompt_tokens=3, completion_tokens=2,
                               completion_tokens_details=NS(reasoning_tokens=1)),
                      choices=[NS(finish_reason="stop",
                                  message=NS(content="ok", tool_calls=None))])

        self.s.client = NS(chat=NS(completions=NS(create=create)))
        with self.assertLogs("dm.ds", level="INFO") as logs:
            msg = asyncio.run(self.s._complete())
        self.assertEqual(msg["finish_reason"], "stop")
        if self.ds.DS_MAX_TOKENS <= 0:
            self.assertNotIn("max_tokens", captured)
        self.assertTrue(any("finish=stop" in line and "reasoning=1" in line
                            for line in logs.output))
        self.assertEqual(self.ds.usage.used_today(), 1)

    def test_no_request_once_the_day_is_spent(self):
        import asyncio

        async def create(**kw):
            raise AssertionError("request made past the daily limit")

        from types import SimpleNamespace as NS
        self.s.client = NS(chat=NS(completions=NS(create=create)))
        self.ds.usage.DAILY_COMPLETIONS = 1
        self.ds.usage.record(1)
        with self.assertRaises(self.ds.usage.LimitReached):
            asyncio.run(self.s._complete())


if __name__ == "__main__":
    unittest.main()


class ChatLaneTests(unittest.TestCase):
    """Chats run side by side; one chat's updates run in order, and not too many.

    Before lanes, python-telegram-bot ran every update of every chat one after
    another, so one player's minute-long DM turn stalled everybody else.
    """

    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("telegram") is None:
            raise unittest.SkipTest("python-telegram-bot not installed in this interpreter")
        cls.lanes = _import("chat_lanes")

    @staticmethod
    def upd(chat_id):
        from types import SimpleNamespace
        return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))

    def test_other_chats_are_not_blocked_by_a_long_turn(self):
        import asyncio

        async def go():
            lanes = self.lanes.ChatLanes()
            release, done = asyncio.Event(), []

            async def long_turn():
                await release.wait()
                done.append("A")

            async def quick():
                done.append("B")

            a = asyncio.create_task(lanes.process_update(self.upd(1), long_turn()))
            await asyncio.sleep(0)
            await asyncio.wait_for(lanes.process_update(self.upd(2), quick()), 1)
            self.assertEqual(done, ["B"])
            self.assertTrue(lanes.busy(1))
            release.set()
            await a
            self.assertEqual(done, ["B", "A"])
            self.assertFalse(lanes.busy(1))
            self.assertEqual(lanes._locks, {})

        asyncio.run(go())

    def test_one_chat_runs_in_order_without_overlap(self):
        import asyncio

        async def go():
            lanes = self.lanes.ChatLanes()
            log, running = [], [0]

            async def step(n):
                running[0] += 1
                self.assertEqual(running[0], 1, "two updates of one chat overlapped")
                await asyncio.sleep(0.01)
                log.append(n)
                running[0] -= 1

            await asyncio.gather(*(lanes.process_update(self.upd(7), step(n))
                                   for n in range(3)))
            self.assertEqual(log, [0, 1, 2])

        asyncio.run(go())

    def test_a_full_lane_refuses_instead_of_queueing(self):
        import asyncio

        async def go():
            lanes = self.lanes.ChatLanes(max_queued=2)
            release, ran = asyncio.Event(), []

            async def turn(n):
                await release.wait()
                ran.append(n)

            tasks = [asyncio.create_task(lanes.process_update(self.upd(5), turn(n)))
                     for n in range(4)]
            await asyncio.sleep(0.01)
            release.set()
            await asyncio.gather(*tasks)
            self.assertEqual(ran, [0, 1])

        asyncio.run(go())

    def test_updates_without_a_chat_pass_straight_through(self):
        import asyncio
        from types import SimpleNamespace

        async def go():
            ran = []

            async def work():
                ran.append(1)

            await self.lanes.ChatLanes().process_update(
                SimpleNamespace(effective_chat=None), work())
            self.assertEqual(ran, [1])

        asyncio.run(go())


class IdleSessionTests(unittest.TestCase):
    """Which sessions the idle sweep may close."""

    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("openai") is None:
            raise unittest.SkipTest("openai not installed in this interpreter")
        _import("config")
        cls.ds = _import("ds_engine")

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg = self.ds.DSRegistry()
        for cid in (1, 2):
            self.reg._sessions[cid] = self.ds.DSSession(
                cid, pathlib.Path(self._tmp.name), "system")

    def test_only_sessions_past_the_limit_are_idle(self):
        self.reg._sessions[1].last_used -= 3600
        self.assertEqual(self.reg.idle(1800), [1])

    def test_a_session_mid_turn_is_never_idle(self):
        import asyncio

        async def go():
            s = self.reg._sessions[1]
            s.last_used -= 3600
            async with s.lock:
                self.assertEqual(self.reg.idle(1800), [])

        asyncio.run(go())


class AccessTests(unittest.TestCase):
    """Who gets in: admins always, others once an admin approves."""

    ADMIN, STRANGER = 1, 42

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.a = _import("access")

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (self.a.ACCESS_FILE, self.a.ADMINS)
        self.a.ACCESS_FILE = pathlib.Path(self._tmp.name) / "access.json"
        self.a.ADMINS = {self.ADMIN}

    def tearDown(self):
        self.a.ACCESS_FILE, self.a.ADMINS = self._saved
        self._tmp.cleanup()

    def test_admin_in_stranger_out(self):
        self.assertTrue(self.a.is_allowed(self.ADMIN))
        self.assertFalse(self.a.is_allowed(self.STRANGER))
        self.assertEqual(self.a.status(self.STRANGER), "none")

    def test_no_admins_means_open(self):
        self.a.ADMINS = set()
        self.assertTrue(self.a.is_allowed(self.STRANGER))

    def test_request_is_recorded_once(self):
        self.assertTrue(self.a.request(self.STRANGER, "Мерри", "merri"))
        self.assertFalse(self.a.request(self.STRANGER, "Мерри", "merri"))
        self.assertEqual(self.a.status(self.STRANGER), "pending")
        self.assertFalse(self.a.is_allowed(self.STRANGER))

    def test_approve_lets_in_without_restart(self):
        self.a.request(self.STRANGER, "Мерри", "merri")
        rec = self.a.approve(self.STRANGER)
        self.assertEqual(rec["name"], "Мерри")
        self.assertTrue(self.a.is_allowed(self.STRANGER))
        self.assertEqual([u for u, _ in self.a.listing()["allowed"]], [self.STRANGER])
        self.assertEqual(self.a.listing()["pending"], [])

    def test_refused_cannot_re_request(self):
        self.a.request(self.STRANGER, "Мерри", "")
        self.a.refuse(self.STRANGER)
        self.assertFalse(self.a.request(self.STRANGER, "Мерри", ""))
        self.assertEqual(self.a.status(self.STRANGER), "denied")

    def test_revoke_shuts_out(self):
        self.a.approve(self.STRANGER)
        self.a.refuse(self.STRANGER)
        self.assertFalse(self.a.is_allowed(self.STRANGER))

    def test_broken_file_keeps_strangers_out(self):
        self.a.ACCESS_FILE.write_text("{not json", encoding="utf-8")
        self.assertFalse(self.a.is_allowed(self.STRANGER))
        self.assertTrue(self.a.is_allowed(self.ADMIN))

    def test_invitation_lets_in_on_first_contact_once(self):
        res = self.a.invite(["@Merri_Brandybuck"])
        self.assertEqual(res["added"], ["merri_brandybuck"])
        self.assertFalse(self.a.is_allowed(self.STRANGER))
        # Usernames are case-insensitive; the stored one keeps its case.
        self.assertTrue(self.a.redeem(self.STRANGER, "Мерри", "merri_BRANDYBUCK"))
        self.assertTrue(self.a.is_allowed(self.STRANGER))
        self.assertEqual(self.a.invitations(), [])
        # Used up: a revoke sticks, the name does not let them back in.
        self.a.refuse(self.STRANGER)
        self.assertFalse(self.a.redeem(self.STRANGER, "Мерри", "merri_brandybuck"))
        self.assertFalse(self.a.is_allowed(self.STRANGER))

    def test_invitation_needs_the_right_username(self):
        self.a.invite(["merri_b"])
        self.assertFalse(self.a.redeem(self.STRANGER, "Пиппин", "pippin_t"))
        self.assertFalse(self.a.redeem(self.STRANGER, "Без ника", ""))
        self.assertEqual([n for n, _ in self.a.invitations()], ["merri_b"])

    def test_invite_sorts_names(self):
        self.a.approve(self.STRANGER)
        data = self.a.load()
        data["allowed"][str(self.STRANGER)]["username"] = "Merri_B"
        self.a._save(data)
        res = self.a.invite(["merri_b", "pippin_t", "@Pippin_T", "x", "bad name!"])
        self.assertEqual(res, {"added": ["pippin_t"], "playing": ["merri_b"],
                               "invalid": ["x", "bad name!"]})
        self.assertTrue(self.a.uninvite("@PIPPIN_T"))
        self.assertFalse(self.a.uninvite("pippin_t"))

    def test_invitations_survive_other_decisions(self):
        self.a.invite(["merri_b"])
        self.a.request(self.STRANGER, "Пиппин", "")
        self.a.approve(self.STRANGER)
        self.assertEqual([n for n, _ in self.a.invitations()], ["merri_b"])


class DailyBudgetTests(unittest.TestCase):
    """The bot-wide daily budget of Chat Completions requests."""

    @classmethod
    def setUpClass(cls):
        _import("config")
        cls.u = _import("usage")

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._saved = (self.u.USAGE_FILE, self.u.LOCK_FILE, self.u.DAILY_COMPLETIONS,
                       self.u.TURN_RESERVE)
        self.u.USAGE_FILE, self.u.LOCK_FILE = root / "usage.json", root / "usage.lock"
        self.u.DAILY_COMPLETIONS, self.u.TURN_RESERVE = 10, 4

    def tearDown(self):
        (self.u.USAGE_FILE, self.u.LOCK_FILE, self.u.DAILY_COMPLETIONS,
         self.u.TURN_RESERVE) = self._saved
        self._tmp.cleanup()

    def spend(self, n, user=1):
        for _ in range(n):
            self.u.record(user, 100, 10)

    def test_counts_persist_per_day_and_user(self):
        self.spend(3, user=1)
        self.spend(2, user=2)
        self.assertEqual(self.u.used_today(), 5)
        (day, d), = self.u.days()
        self.assertEqual(day, self.u.today())
        self.assertEqual(d["users"], {"1": 3, "2": 2})
        self.assertEqual(d["prompt_tokens"], 500)

    def test_a_turn_starts_only_if_a_whole_one_fits(self):
        self.spend(6)
        self.assertTrue(self.u.can_start_turn())      # 4 left = reserve
        self.spend(1)
        self.assertFalse(self.u.can_start_turn())
        self.u.check()                                # but a running turn goes on

    def test_hard_stop_at_the_limit(self):
        self.spend(10)
        with self.assertRaises(self.u.LimitReached):
            self.u.check()

    def test_zero_means_no_limit(self):
        self.u.DAILY_COMPLETIONS = 0
        self.spend(20)
        self.assertTrue(self.u.can_start_turn())
        self.assertIsNone(self.u.remaining())
        self.u.check()

    def test_yesterday_does_not_count(self):
        import json
        self.u.USAGE_FILE.write_text(json.dumps(
            {"days": {"2000-01-01": {"completions": 999, "prompt_tokens": 0,
                                     "completion_tokens": 0, "users": {}}}}),
            encoding="utf-8")
        self.assertEqual(self.u.used_today(), 0)
        self.assertTrue(self.u.can_start_turn())
