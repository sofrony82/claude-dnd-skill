"""
test_module_prep.py — the deterministic half of the module-prep pipeline.

Covers the parts that decide whether a prepared pack is trustworthy: text
cleanup (the three cascading noise filters), figure-vs-furniture classification,
chapter slicing with coverage accounting, and the pack validator.

Run from repo root:
    python3 -m unittest tests.test_module_prep -v
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "module-prep" / "scripts"


def _import(name):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, str(SCRIPTS / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ScriptClassificationTests(unittest.TestCase):
    """_shared's writing-system logic — the basis of every cleanup pass."""

    @classmethod
    def setUpClass(cls):
        cls.s = _import("_shared")

    def test_dominant_script(self):
        self.assertEqual(self.s.dominant_script("Драконий Покой"), "cyrillic")
        self.assertEqual(self.s.dominant_script("Dragon's Rest"), "latin")

    def test_latin_tolerated_inside_another_script(self):
        # Published translations keep English proper nouns; those must not read
        # as foreign noise.
        self.assertFalse(self.s.is_garbled("Глава 1. Драконий Покой (Dragon's Rest)", "cyrillic"))

    def test_symbol_font_run_is_garbled(self):
        self.assertTrue(self.s.is_garbled("ส฻฼ุ้ใาำ", "cyrillic"))

    def test_combining_marks_count_as_script(self):
        # Bare combining marks carry no isalpha flag but are exactly the noise
        # the classifier exists to catch.
        self.assertTrue(self.s.is_garbled("ฺู", "cyrillic", min_letters=1))

    def test_noise_scripts_finds_the_rare_one(self):
        text = "Драконий Покой " * 200 + "Caves)็"
        self.assertEqual(self.s.noise_scripts(text), {"thai"})

    def test_strip_scripts_removes_fused_ornament(self):
        # The case no token-level filter can see: an ornament glued to a real
        # word, leaving the token mostly-legitimate.
        self.assertEqual(self.s.strip_scripts("Caves)็", {"thai"}), "Caves)")

    def test_noise_scripts_keeps_the_body_script(self):
        text = "Драконий Покой " * 200
        self.assertNotIn("cyrillic", self.s.noise_scripts(text))

    def test_slugify_transliterates(self):
        self.assertEqual(self.s.slugify("Карта 2. Драконий Покой"), "karta-2-drakoniy-pokoy")

    def test_normalise_collapses_pdf_artefacts(self):
        self.assertEqual(self.s.normalise("a­b  c\n\n\n\nd"), "ab c\n\nd")


class PagePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ex = _import("pdf_extract")

    def test_ranges_and_singletons(self):
        self.assertEqual(self.ex.parse_pages("1-3,7", 10), [1, 2, 3, 7])

    def test_open_ended_range_clamps(self):
        self.assertEqual(self.ex.parse_pages("8-", 10), [8, 9, 10])

    def test_empty_spec_is_everything(self):
        self.assertEqual(self.ex.parse_pages("", 3), [1, 2, 3])

    def test_overlap_deduplicates(self):
        self.assertEqual(self.ex.parse_pages("1-3,2-4", 10), [1, 2, 3, 4])


class ModuleBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mb = _import("module_build")

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.build = self.tmp / "build"
        (self.build / "pages").mkdir(parents=True)
        for n in range(1, 7):
            (self.build / "pages" / f"p{n:03d}.txt").write_text(
                f"страница {n} текст текст", encoding="utf-8")
        (self.build / "extract.json").write_text(json.dumps({
            "source": "/tmp/x.pdf", "source_sha256": "a" * 64,
            "pages_in_document": 6, "pages_extracted": list(range(1, 7)),
        }), encoding="utf-8")

    def _build(self, plan):
        planfile = self.tmp / "plan.txt"
        planfile.write_text(plan, encoding="utf-8")
        pack = self.tmp / "pack"
        sys.argv = ["module_build.py", "--build", str(self.build), "--pack", str(pack),
                    "--plan", str(planfile), "--title", "T", "--quiet"]
        self.mb.main()
        return pack, json.loads((pack / "build-manifest.json").read_text(encoding="utf-8"))

    def test_full_coverage(self):
        pack, man = self._build("a|First|1-3\nb|Second|4-6\n")
        self.assertEqual(man["coverage"]["pages_missing"], [])
        self.assertEqual(man["coverage"]["pages_assigned"], 6)
        self.assertTrue((pack / "source" / "a.md").is_file())
        self.assertIn("source/b.md", (pack / "source-index.md").read_text(encoding="utf-8"))

    def test_unassigned_pages_are_reported(self):
        # Silent page loss is the failure this accounting exists to prevent.
        _pack, man = self._build("a|First|1-3\n")
        self.assertEqual(man["coverage"]["pages_missing"], [4, 5, 6])

    def test_duplicate_pages_are_reported(self):
        _pack, man = self._build("a|First|1-4\nb|Second|3-6\n")
        self.assertIn("3", man["coverage"]["pages_duplicated"])

    def test_comments_and_blanks_ignored(self):
        _pack, man = self._build("# comment\n\na|First|1-6\n")
        self.assertEqual(len(man["chapters"]), 1)

    def test_malformed_plan_line_rejected(self):
        with self.assertRaises(SystemExit):
            self._build("a|missing-pages\n")

    def test_chapter_carries_provenance(self):
        pack, _man = self._build("a|First|1-3\n")
        text = (pack / "source" / "a.md").read_text(encoding="utf-8")
        self.assertIn("страницы 1-3", text)
        self.assertIn("<!-- страница 1 -->", text)


class ModuleCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mc = _import("module_check")

    def setUp(self):
        self.pack = pathlib.Path(tempfile.mkdtemp()) / "pack"
        (self.pack / "source").mkdir(parents=True)
        (self.pack / "maps").mkdir()
        (self.pack / "world.md").write_text("мир " * 100, encoding="utf-8")
        (self.pack / "npcs.md").write_text(
            "| Имя | Роль |\n|---|---|\n| Рунара | настоятельница |\n", encoding="utf-8")
        (self.pack / "npcs-full.md").write_text("### Рунара\nбронзовая драконица\n",
                                                encoding="utf-8")
        (self.pack / "arc.md").write_text("source_ref: source/1.1.md\n", encoding="utf-8")
        (self.pack / "source-index.md").write_text("[source/1.1.md](source/1.1.md)\n",
                                                   encoding="utf-8")
        (self.pack / "source" / "1.1.md").write_text("текст " * 200, encoding="utf-8")

    def test_clean_pack_passes(self):
        self.assertTrue(self.mc.check(self.pack).ok)

    def test_missing_required_file_fails(self):
        (self.pack / "world.md").unlink()
        r = self.mc.check(self.pack)
        self.assertFalse(r.ok)
        self.assertTrue(any("world.md" in e for e in r.errors))

    def test_npc_in_index_without_full_entry_fails(self):
        # The DM would voice a character it has no motivation or secret for.
        (self.pack / "npcs.md").write_text(
            "| Имя | Роль |\n|---|---|\n| Рунара | х |\n| Спаркрендер | вирмлинг |\n",
            encoding="utf-8")
        r = self.mc.check(self.pack)
        self.assertFalse(r.ok)
        self.assertTrue(any("Спаркрендер" in e for e in r.errors))

    def test_dangling_arc_source_ref_fails(self):
        (self.pack / "arc.md").write_text("source_ref: source/9.9.md\n", encoding="utf-8")
        r = self.mc.check(self.pack)
        self.assertFalse(r.ok)
        self.assertTrue(any("9.9" in e for e in r.errors))

    def test_load_time_budget_enforced(self):
        (self.pack / "world.md").write_text("мир " * 20000, encoding="utf-8")
        r = self.mc.check(self.pack)
        self.assertFalse(r.ok)
        self.assertTrue(any("load-time budget" in e for e in r.errors))

    def test_legend_without_image_fails(self):
        (self.pack / "maps" / "map-1.md").write_text("# Карта 1\n", encoding="utf-8")
        r = self.mc.check(self.pack)
        self.assertFalse(r.ok)
        self.assertTrue(any("no image" in e for e in r.errors))

    def test_image_without_legend_warns_only(self):
        (self.pack / "maps" / "map-1-p005.jpeg").write_bytes(b"\xff\xd8\xff")
        r = self.mc.check(self.pack)
        self.assertTrue(r.ok)
        self.assertTrue(any("no legend" in w for w in r.warnings))

    def test_replacement_characters_fail(self):
        (self.pack / "world.md").write_text("мир � плохо", encoding="utf-8")
        r = self.mc.check(self.pack)
        self.assertFalse(r.ok)
        self.assertTrue(any("U+FFFD" in e for e in r.errors))

    def test_pregen_name_placeholder_is_not_flagged(self):
        # Deliberate: the player names the character at the table.
        (self.pack / "pregens").mkdir()
        (self.pack / "pregens" / "rogue.md").write_text(
            "# <имя задаёт игрок>\n" + "текст " * 200, encoding="utf-8")
        r = self.mc.check(self.pack)
        self.assertFalse(any("placeholder" in w for w in r.warnings))


if __name__ == "__main__":
    unittest.main()
