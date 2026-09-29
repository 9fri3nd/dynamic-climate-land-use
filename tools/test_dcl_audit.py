import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dcl_audit as audit  # noqa: E402

BOM = "\ufeff"

EFFECTS_LOG = """# Effect Documentation

## if
Executes enclosed effects if limit criteria are met
Supported Scopes: none

## else
Supported Scopes: none

## hidden_effect
Supported Scopes: none

## set_variable
Supported Scopes: none

## change_variable
Supported Scopes: none

## clamp_variable
Supported Scopes: none

## add_location_modifier
Supported Scopes: location

## every_location_in_province
Supported Scopes: province
"""

TRIGGERS_LOG = """# Trigger Documentation

## has_variable
Supported Scopes: none

## current_month
Supported Scopes: none

## climate
Supported Scopes: location

## trigger_if
Supported Scopes: none
"""

EVENT_TARGETS_LOG = """# Event Target Documentation

### province
Supported Scopes: location

### area
Supported Scopes: location

### var
Supported Scopes: none

### scope
Supported Scopes: none
"""

MODIFIERS_LOG = """Printing Modifier Definitions:
Tag: local_monthly_food_modifier, Categories: location
Tag: global_tax_modifier, Categories: country
"""

ON_ACTIONS_LOG = """--------------------
monthly_country_pulse:
From Code: Yes
Expected Scope: country
--------------------
weather_monthly_pulse:
From Code: Yes
Expected Scope: none
"""

VANILLA_MODIFIER_TYPES = """local_tax_modifier={
\tcolor = good
\tgame_data={
\t\tcategory=location
\t}
}
"""

SCRIPT_VALUES = """dcl_temp_offset = {
    value = 2
    if = {
        limit = { current_month >= 6 }
        add = 1
    }
}

dcl_base_temp = {
    value = 10
    add = dcl_temp_offset
}

dcl_cultivation_read = {
    value = var:cultivation_ratio
    max = 1
}
"""

SCRIPTED_EFFECTS = """# scope: location
dcl_update_location = {
    set_variable = { name = cultivation_ratio value = dcl_base_temp }
    clamp_variable = { name = cultivation_ratio min = 0 max = 1 }
    add_location_modifier = { modifier = dcl_climate_mod mode = replace }
}

# scope: area
dcl_update_area = {
    set_variable = { name = macro_temp_anomaly value = 0 }
    if = {
        limit = { has_variable = macro_temp_anomaly }
        change_variable = { name = macro_temp_anomaly add = 1 }
    }
}
"""

STATIC_MODIFIERS = """dcl_climate_mod = {
    game_data = { category = location }
    local_tax_modifier = 0.1
    local_monthly_food_modifier = -0.05
}
"""

ON_ACTIONS = """monthly_country_pulse = {
    on_actions = { dcl_monthly }
}

dcl_monthly = {
    effect = {
        dcl_update_area = yes
    }
}
"""

LOC = """l_english:
 STATIC_MODIFIER_NAME_dcl_climate_mod:0 "Climate"
 STATIC_MODIFIER_DESC_dcl_climate_mod:0 "Climate effects."
"""


def write(path, text, bom=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write((BOM if bom else "") + text)


class AuditFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dcl_audit_test_")
        self.docs = os.path.join(self.tmp, "docs")
        self.vanilla = os.path.join(self.tmp, "vanilla")
        self.mod = os.path.join(self.tmp, "mod")
        write(os.path.join(self.docs, "effects.log"), EFFECTS_LOG, bom=False)
        write(os.path.join(self.docs, "triggers.log"), TRIGGERS_LOG, bom=False)
        write(os.path.join(self.docs, "event_targets.log"), EVENT_TARGETS_LOG, bom=False)
        write(os.path.join(self.docs, "modifiers.log"), MODIFIERS_LOG, bom=False)
        write(os.path.join(self.docs, "on_actions.log"), ON_ACTIONS_LOG, bom=False)
        write(os.path.join(self.vanilla, "main_menu", "common", "modifier_type_definitions",
                           "00_modifier_types.txt"), VANILLA_MODIFIER_TYPES)
        os.makedirs(self.mod)
        self.files = {
            "sv": ("in_game/common/script_values/dcl_values.txt", SCRIPT_VALUES),
            "se": ("in_game/common/scripted_effects/dcl_effects.txt", SCRIPTED_EFFECTS),
            "sm": ("in_game/common/static_modifiers/dcl_modifiers.txt", STATIC_MODIFIERS),
            "oa": ("in_game/common/on_action/dcl_on_actions.txt", ON_ACTIONS),
            "loc": ("main_menu/localization/english/dcl_l_english.yml", LOC),
        }
        for rel, text in self.files.values():
            self.put(rel, text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, rel, text, bom=True):
        write(os.path.join(self.mod, *rel.split("/")), text, bom=bom)

    def patch(self, key, old, new):
        rel, text = self.files[key]
        self.assertIn(old, text)
        self.put(rel, text.replace(old, new, 1))

    def run_audit(self, mod=None, vanilla=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = audit.main([mod or self.mod, "--docs", self.docs, "--vanilla", vanilla or self.vanilla])
        lines = out.getvalue().splitlines()
        return code, lines

    def fails(self, lines, check):
        return [ln for ln in lines if ln.startswith("FAIL " + check + ":")]

    def assertFails(self, check, needle):
        code, lines = self.run_audit()
        hits = [ln for ln in self.fails(lines, check) if needle in ln]
        self.assertTrue(hits, "expected FAIL %s mentioning %r, got:\n%s" % (check, needle, "\n".join(lines)))
        self.assertEqual(code, 1)
        return lines


class CleanFixtureTest(AuditFixture):
    def test_clean_fixture_passes(self):
        code, lines = self.run_audit()
        self.assertEqual([ln for ln in lines if ln.startswith("FAIL")], [], "\n".join(lines))
        self.assertEqual(code, 0)
        for check in ("bom", "braces", "refs", "registry_keys", "forbidden"):
            self.assertTrue(any(ln.startswith("PASS " + check) for ln in lines), check)

    def test_empty_mod_root_passes(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        code, lines = self.run_audit(empty)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertIn("PASS bom: 0 files checked", lines)

    def test_collect_defs_groups_top_level_keys_by_folder(self):
        defs = audit.collect_defs(audit.scan_mod(self.mod))
        self.assertEqual(defs["script_values"], {"dcl_temp_offset", "dcl_base_temp", "dcl_cultivation_read"})
        self.assertEqual(defs["scripted_effects"], {"dcl_update_location", "dcl_update_area"})
        self.assertEqual(defs["static_modifiers"], {"dcl_climate_mod"})
        self.assertIn("dcl_monthly", defs["on_action"])


class BomTest(AuditFixture):
    def test_missing_bom_detected(self):
        rel, text = self.files["sv"]
        self.put(rel, text, bom=False)
        self.assertFails("bom", "dcl_values.txt")


class BracesTest(AuditFixture):
    def test_unbalanced_braces_detected(self):
        self.patch("sv", "dcl_base_temp = {\n", "dcl_base_temp = {\n    if = {\n")
        self.assertFails("braces", "dcl_values.txt")

    def test_stray_closing_brace_detected(self):
        self.patch("sv", "    max = 1\n}\n", "    max = 1\n}\n}\n")
        self.assertFails("braces", "dcl_values.txt")

    def test_braces_in_comments_and_strings_ignored(self):
        self.patch("sv", "    value = 2\n", "    value = 2 # { unbalanced in comment\n    desc = \"a } b\"\n")
        code, lines = self.run_audit()
        self.assertEqual(self.fails(lines, "braces"), [], "\n".join(lines))


class RefsTest(AuditFixture):
    def test_undefined_script_value_detected(self):
        self.patch("sv", "add = dcl_temp_offset", "add = dcl_missing_value")
        self.assertFails("refs", "dcl_missing_value")

    def test_undefined_bare_value_identifier_detected(self):
        self.patch("sv", "add = dcl_temp_offset", "add = some_undefined_value")
        self.assertFails("refs", "some_undefined_value")

    def test_undefined_static_modifier_detected(self):
        self.patch("se", "modifier = dcl_climate_mod", "modifier = dcl_ghost_mod")
        self.assertFails("refs", "dcl_ghost_mod")

    def test_missing_static_modifier_desc_detected(self):
        self.patch("loc", ' STATIC_MODIFIER_DESC_dcl_climate_mod:0 "Climate effects."\n', "")
        self.assertFails("refs", "STATIC_MODIFIER_DESC_dcl_climate_mod")

    def test_unset_variable_read_detected(self):
        self.patch("sv", "var:cultivation_ratio", "var:dcl_ghost_var")
        self.assertFails("refs", "dcl_ghost_var")

    def test_undefined_scripted_effect_call_detected(self):
        self.patch("oa", "dcl_update_area = yes", "dcl_update_nowhere = yes")
        self.assertFails("refs", "dcl_update_nowhere")

    def test_undefined_on_action_detected(self):
        self.patch("oa", "on_actions = { dcl_monthly }", "on_actions = { dcl_nowhere }")
        self.assertFails("refs", "dcl_nowhere")


class RegistryKeysTest(AuditFixture):
    def test_unknown_modifier_key_detected(self):
        self.patch("sm", "local_tax_modifier = 0.1", "local_bogus_modifier = 0.1")
        self.assertFails("registry_keys", "local_bogus_modifier")

    def test_unknown_effect_key_detected(self):
        self.patch("se", "    clamp_variable", "    bogus_effect = yes\n    clamp_variable")
        self.assertFails("registry_keys", "bogus_effect")

    def test_unknown_trigger_key_detected(self):
        self.patch("sv", "current_month >= 6", "bogus_trigger = yes")
        self.assertFails("registry_keys", "bogus_trigger")


class ForbiddenTest(AuditFixture):
    def test_on_daily_pulse_detected(self):
        self.patch("oa", "monthly_country_pulse = {", "on_daily_pulse = {")
        self.assertFails("forbidden", "on_daily_pulse")

    def test_clamp_variable_script_value_bound_detected(self):
        self.patch("se", "min = 0 max = 1", "min = some_value max = 1")
        self.assertFails("forbidden", "clamp_variable")

    def test_forbidden_word_in_comment_ignored(self):
        self.patch("oa", "monthly_country_pulse = {", "# never use on_daily_pulse here\nmonthly_country_pulse = {")
        code, lines = self.run_audit()
        self.assertEqual(self.fails(lines, "forbidden"), [], "\n".join(lines))

    def test_fixed_range_in_formula_detected(self):
        self.patch("sv", "    max = 1\n", "    add = { fixed_range = { min = 0 max = 1 } }\n    max = 1\n")
        self.assertFails("forbidden", "fixed_range")

    def test_modifier_size_effect_detected(self):
        self.patch("se", "    clamp_variable", "    change_location_modifier_size = { name = x }\n    clamp_variable")
        self.assertFails("forbidden", "change_location_modifier_size")

    def test_location_variable_other_than_cultivation_ratio_detected(self):
        self.patch("se", "name = cultivation_ratio value", "name = dcl_location_state value")
        self.assertFails("forbidden", "dcl_location_state")

    def test_is_ai_detected(self):
        self.patch("se", "limit = { has_variable", "limit = { is_ai = no has_variable")
        self.assertFails("forbidden", "is_ai")


class LocUnusedTest(AuditFixture):
    def test_unused_loc_key_warns_without_failing(self):
        self.patch("loc", ' STATIC_MODIFIER_NAME', ' dcl_orphan_key:0 "Nobody reads me"\n STATIC_MODIFIER_NAME')
        code, lines = self.run_audit()
        self.assertIn("WARN loc_unused: dcl_orphan_key", lines)
        self.assertEqual(code, 0, "\n".join(lines))


TRIGGERS_REL = "in_game/common/scripted_triggers/dcl_triggers.txt"
GUI_REL = "in_game/gui/dcl_window.gui"
GUI_TEMPLATE = """window = {
    text_single = {
        raw_text = "[ScriptValue('%s')]"
        tooltip = "[ShowModifierEffect('%s')]"
    }
}
"""


class ScriptedTriggerAndGuiRefsTest(AuditFixture):
    def setUp(self):
        super().setUp()
        self.put(TRIGGERS_REL, "dcl_is_warm = {\n    current_month >= 6\n}\n")

    def test_defined_scripted_trigger_reference_passes(self):
        self.patch("se", "limit = { has_variable", "limit = { dcl_is_warm = yes has_variable")
        code, lines = self.run_audit()
        self.assertEqual([ln for ln in lines if ln.startswith("FAIL")], [], "\n".join(lines))
        self.assertEqual(code, 0)

    def test_undefined_scripted_trigger_reference_detected(self):
        self.patch("se", "limit = { has_variable", "limit = { dcl_is_cold = yes has_variable")
        self.assertFails("refs", "dcl_is_cold")

    def test_gui_references_to_defined_names_pass(self):
        self.put(GUI_REL, GUI_TEMPLATE % ("dcl_base_temp", "dcl_climate_mod"))
        code, lines = self.run_audit()
        self.assertEqual([ln for ln in lines if ln.startswith("FAIL")], [], "\n".join(lines))
        self.assertEqual(code, 0)

    def test_gui_undefined_script_value_detected(self):
        self.put(GUI_REL, GUI_TEMPLATE % ("dcl_missing_sv", "dcl_climate_mod"))
        self.assertFails("refs", "undefined script value 'dcl_missing_sv'")

    def test_gui_undefined_static_modifier_detected(self):
        self.put(GUI_REL, GUI_TEMPLATE % ("dcl_base_temp", "dcl_ghost_mod"))
        self.assertFails("refs", "undefined static modifier 'dcl_ghost_mod'")

    def test_gui_var_read_of_unset_variable_detected(self):
        self.put(GUI_REL, 'window = {\n    text_single = {\n'
                          '        raw_text = "[Location.MakeScope.Var(\'dcl_ghost_var\').GetValue]"\n'
                          '    }\n}\n')
        self.assertFails("refs", "variable 'dcl_ghost_var' read but never set")


class RegistryGapsTest(AuditFixture):
    def test_registry_gap_key_accepted(self):
        self.patch("se", "limit = { has_variable",
                   "limit = { any_courtier = { has_variable = macro_temp_anomaly } has_variable")
        code, lines = self.run_audit()
        self.assertEqual(self.fails(lines, "registry_keys"), [], "\n".join(lines))


class ForbiddenFixRoundTest(AuditFixture):
    def test_clamp_block_bound_detected(self):
        self.patch("se", "min = 0 max = 1 }", "min = 0 max = { value = var:cultivation_ratio } }")
        self.assertFails("forbidden", "clamp_variable max bound is not a literal")

    def test_clamp_variable_link_bound_detected(self):
        self.patch("se", "min = 0 max = 1 }", "min = 0 max = var:cultivation_ratio }")
        self.assertFails("forbidden", "clamp_variable max bound 'var:cultivation_ratio' is not a literal")

    def test_area_variable_outside_whitelist_detected(self):
        self.patch("se", "name = macro_temp_anomaly value = 0", "name = dcl_area_bogus value = 0")
        self.assertFails("forbidden", "area variable 'dcl_area_bogus'")

    def test_whitelisted_area_variable_passes(self):
        self.patch("se", "name = macro_temp_anomaly value = 0", "name = dcl_front_ttl value = 0")
        code, lines = self.run_audit()
        self.assertEqual(self.fails(lines, "forbidden"), [], "\n".join(lines))

    def test_area_scope_key_routes_to_area_whitelist(self):
        self.patch("se", "    clamp_variable",
                   "    area = { set_variable = { name = macro_humidity_anomaly value = 0 } }\n"
                   "    clamp_variable")
        code, lines = self.run_audit()
        self.assertEqual(self.fails(lines, "forbidden"), [], "\n".join(lines))

    def test_area_scope_key_write_outside_whitelist_detected(self):
        self.patch("se", "    clamp_variable",
                   "    every_area = { set_variable = { name = dcl_area_bogus value = 0 } }\n"
                   "    clamp_variable")
        self.assertFails("forbidden", "area variable 'dcl_area_bogus'")

    def test_location_scope_inside_area_effect_routes_to_location_rule(self):
        self.patch("se", "    if = {\n",
                   "    every_location_in_area = { set_variable = { name = macro_temp_anomaly value = 0 } }\n"
                   "    if = {\n")
        self.assertFails("forbidden", "location variable 'macro_temp_anomaly'")

    def test_area_tag_does_not_leak_into_next_definition(self):
        rel, text = self.files["se"]
        self.put(rel, text + "\ndcl_after_area = {\n    set_variable = { name = macro_temp_anomaly value = 1 }\n}\n")
        self.assertFails("forbidden", "location variable 'macro_temp_anomaly' set in 'dcl_after_area'")

    def test_placeholder_prose_comment_passes(self):
        self.patch("sv", "    value = 2\n", "    value = 2 # this is not a placeholder here\n")
        code, lines = self.run_audit()
        self.assertEqual(self.fails(lines, "forbidden"), [], "\n".join(lines))

    def test_todo_comment_detected(self):
        self.patch("sv", "    value = 2\n", "    value = 2 # TODO fix\n")
        self.assertFails("forbidden", "TODO fix")

    def test_yml_todo_comment_detected(self):
        self.patch("loc", "l_english:\n", "l_english:\n # TODO translate\n")
        self.assertFails("forbidden", "TODO translate")


class VanillaMissingTest(AuditFixture):
    def missing(self):
        return os.path.join(self.tmp, "no_such_vanilla")

    def test_missing_vanilla_root_single_warn_exit_zero(self):
        code, lines = self.run_audit(vanilla=self.missing())
        self.assertEqual([ln for ln in lines if ln.startswith("FAIL")], [], "\n".join(lines))
        self.assertEqual(code, 0)
        warns = [ln for ln in lines if ln.startswith("WARN") and "vanilla root not found" in ln]
        self.assertEqual(len(warns), 1, "\n".join(lines))

    def test_missing_vanilla_root_still_checks_dcl_names(self):
        self.patch("sv", "add = dcl_temp_offset", "add = dcl_missing_value")
        code, lines = self.run_audit(vanilla=self.missing())
        self.assertTrue([ln for ln in self.fails(lines, "refs") if "dcl_missing_value" in ln], "\n".join(lines))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
