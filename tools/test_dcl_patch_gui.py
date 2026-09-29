import difflib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dcl_patch_gui as gen  # noqa: E402

BOM = b"\xef\xbb\xbf"

FAKE_WINDOW = (
    "window = {\n"
    "\theader_secondary_tabs = {\n"
    "\t\tblockoverride \"content\" {\n"
    "\t\t\tbutton_secondary_tab_alt = {\n"
    "\t\t\t\taction_tooltip = {\n"
    "\t\t\t\t\ttitle = \"OPEN_TAB\"\n"
    "\t\t\t\t\ton_action = \"[LocationView.Vars.Set( 'tab', 'demography' )]\"\n"
    "\t\t\t\t}\n"
    + gen.TAB_ANCHOR
    + "\t\t}\n"
    "\t}\n"
    "\tvbox = {\n"
    "\t\t# INFRASTRUCTURE\n"
    "\t\tvbox = {\n"
    "\t\t}\n"
    "\n"
    + gen.PAGE_ANCHOR
    + "\t\t}\n"
    "\t}\n"
    "}\n"
)

FAKE_TOOLTIPS = (
    "template other_template {\n"
    "    x = 1\n"
    "}\n"
    "\n"
    "template tooltip_location_alt_content {\n"
    "    visible = \"[Location.IsLand]\"\n"
    "\n"
    "    TooltipTextBlock = {\n"
    "        blockoverride \"text\" {\n"
    "            text = \"FOO_KEY\"\n"
    "        }\n"
    "    }\n"
    "}\n"
    "\n"
    "template another_template {\n"
    "}\n"
)


class PatchGuiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dcl_patch_gui_"))
        self.vanilla = self.tmp / "vanilla"
        self.mod = self.tmp / "mod"
        (self.vanilla / "in_game/gui/shared").mkdir(parents=True)
        (self.vanilla / "in_game/gui/location_window.gui").write_text(
            FAKE_WINDOW, encoding="utf-8")
        (self.vanilla / "in_game/gui/shared/location_tooltips.gui").write_text(
            FAKE_TOOLTIPS, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_gen(self):
        return gen.generate(self.vanilla, self.mod)

    def window_out(self):
        return (self.mod / "in_game/gui/location_window.gui").read_bytes()

    def tooltip_out(self):
        return (self.mod / "in_game/gui/shared/zz_dcl_location_tooltips.gui").read_bytes()

    def test_generates_both_outputs_with_bom(self):
        written = self.run_gen()
        self.assertEqual(2, len(written))
        for data in (self.window_out(), self.tooltip_out()):
            self.assertTrue(data.startswith(BOM), "missing UTF-8 BOM")
            self.assertNotIn(b"\r", data, "CR bytes in output")

    def test_window_insertions_present(self):
        self.run_gen()
        text = self.window_out().decode("utf-8-sig")
        self.assertIn("LocationView.Vars.Set( 'tab', 'dcl_climate' )", text)
        self.assertIn("LocationView.Vars.HasValue( 'tab', 'dcl_climate' )", text)
        self.assertIn('text = "DCL_WEATHER_CARD"', text)
        self.assertIn('text = "DCL_LAND_SUMMARY"', text)
        self.assertIn("template dcl_cultivation_progressbar {", text)
        self.assertIn("progress_bar_goldish.dds", text)
        self.assertIn("progress_bar_green.dds", text)

    def test_window_diff_is_insertions_only(self):
        self.run_gen()
        text = self.window_out().decode("utf-8-sig")
        diff = list(difflib.ndiff((gen.WINDOW_HEADER + FAKE_WINDOW).splitlines(),
                                  text.splitlines()))
        removals = [d for d in diff if d.startswith("- ")]
        self.assertEqual([], removals, "vanilla lines were removed or changed")
        self.assertTrue(any(d.startswith("+ ") for d in diff), "no insertions found")

    def test_tooltip_file_contains_only_redefined_template(self):
        self.run_gen()
        text = self.tooltip_out().decode("utf-8-sig")
        body = text[len(gen.TOOLTIP_HEADER):]
        self.assertTrue(body.startswith("\ntemplate tooltip_location_alt_content {"))
        self.assertTrue(body.endswith("}\n"))
        self.assertNotIn("other_template", body)
        self.assertNotIn("another_template", body)
        self.assertIn('text = "DCL_HOVER_ROW"', body)
        self.assertIn('text = "DCL_FRONT_HEATWAVE"', body)
        self.assertIn('text = "DCL_FRONT_POLAR_VORTEX"', body)
        self.assertIn('text = "DCL_FRONT_MONSOON"', body)
        self.assertNotIn("DCL_FRONT_NONE", body)
        self.assertIn("GuiScope.SetRoot(Location.MakeScope).ScriptValue('dcl_location_front_type')",
                      body)
        self.assertEqual(1, body.count("template tooltip_location_alt_content {"))

    def test_generated_files_brace_balance(self):
        import dcl_audit
        self.run_gen()
        for rel in ("in_game/gui/location_window.gui",
                    "in_game/gui/shared/zz_dcl_location_tooltips.gui"):
            text = (self.mod / rel).read_bytes().decode("utf-8-sig")
            tokens, _ = dcl_audit.tokenize(text)
            _, errors = dcl_audit.parse(tokens)
            self.assertEqual([], errors, "%s: brace errors" % rel)

    def test_idempotent(self):
        self.run_gen()
        first = (self.window_out(), self.tooltip_out())
        self.run_gen()
        self.assertEqual(first, (self.window_out(), self.tooltip_out()))

    def test_missing_tab_anchor_fails(self):
        (self.vanilla / "in_game/gui/location_window.gui").write_text(
            FAKE_WINDOW.replace(gen.TAB_ANCHOR, "\t\t\t\t\t}\n"), encoding="utf-8")
        with self.assertRaises(gen.PatchError):
            self.run_gen()

    def test_ambiguous_tab_anchor_fails(self):
        (self.vanilla / "in_game/gui/location_window.gui").write_text(
            FAKE_WINDOW + gen.TAB_ANCHOR, encoding="utf-8")
        with self.assertRaises(gen.PatchError):
            self.run_gen()

    def test_missing_page_anchor_fails(self):
        (self.vanilla / "in_game/gui/location_window.gui").write_text(
            FAKE_WINDOW.replace(gen.PAGE_ANCHOR, "\t\tvbox = {\n"), encoding="utf-8")
        with self.assertRaises(gen.PatchError):
            self.run_gen()

    def test_missing_template_fails(self):
        (self.vanilla / "in_game/gui/shared/location_tooltips.gui").write_text(
            "template something_else {\n}\n", encoding="utf-8")
        with self.assertRaises(gen.PatchError):
            self.run_gen()

    def test_duplicate_template_fails(self):
        (self.vanilla / "in_game/gui/shared/location_tooltips.gui").write_text(
            FAKE_TOOLTIPS + FAKE_TOOLTIPS, encoding="utf-8")
        with self.assertRaises(gen.PatchError):
            self.run_gen()

    def test_missing_vanilla_file_fails(self):
        os.remove(self.vanilla / "in_game/gui/location_window.gui")
        with self.assertRaises(gen.PatchError):
            self.run_gen()


@unittest.skipUnless(
    Path(gen.DEFAULT_VANILLA, "in_game/gui/location_window.gui").is_file(),
    "vanilla game directory not present")
class RealVanillaAnchorTest(unittest.TestCase):
    def test_anchors_occur_exactly_once_in_real_vanilla(self):
        root = Path(gen.DEFAULT_VANILLA)
        window = gen.read_normalized(root / gen.WINDOW_REL)
        self.assertEqual(1, window.count(gen.TAB_ANCHOR))
        self.assertEqual(1, window.count(gen.PAGE_ANCHOR))
        self.assertTrue(window.endswith("}\n"))
        tooltips = gen.read_normalized(root / gen.TOOLTIPS_REL)
        template = gen.extract_template(tooltips, gen.TOOLTIP_TEMPLATE)
        self.assertTrue(template.endswith("}\n"))

    def test_real_run_into_temp_mod(self):
        tmp = Path(tempfile.mkdtemp(prefix="dcl_patch_gui_real_"))
        try:
            written = gen.generate(gen.DEFAULT_VANILLA, tmp)
            for path in written:
                self.assertTrue(path.read_bytes().startswith(BOM))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
