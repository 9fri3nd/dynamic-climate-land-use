import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dcl_cost_model as cm  # noqa: E402

MOD_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIXTURE = """leaf_chain = {
    value = 0
    if = { limit = { climate = arid } value = 1 }
    else_if = { limit = { climate = tropical } value = 2 }
    else_if = { limit = { climate = arctic } value = 3 }
    else = { value = 4 }
}
twice = {
    value = leaf_chain
    add = leaf_chain
}
helper = {
    value = leaf_chain
    multiply = 2
}
twice_helper = {
    value = helper
    add = helper
}
compare = {
    value = 0
    if = { limit = { helper > 3 } value = 1 }
}
logic = {
    value = 0
    if = {
        limit = {
            OR = { climate = arid climate = arctic }
            area ?= { has_variable = x }
            NOT = { is_coastal = yes }
        }
        value = 1
        if = { limit = { current_month >= 6 } add = 1 }
    }
}
engine = {
    value = scope:dcl_t
    add = var:foo
    add = 3
    abs = 0
    pow = 2
}
"""

CYCLE = """a_val = {
    value = b_val
}
b_val = {
    value = a_val
}
"""


class CostModelFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "fixture.txt"), "w", encoding="utf-8") as fh:
            fh.write(FIXTURE)
        self.model = cm.CostModel(cm.load_values(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cost(self, name):
        return self.model.value(name).key()

    def test_leaf_chain_costs_all_limits_on_else(self):
        self.assertEqual(self.cost("leaf_chain"), (3, 0))

    def test_parent_referencing_leaf_twice(self):
        self.assertEqual(self.cost("twice"), (6, 2))

    def test_limit_comparing_named_value(self):
        self.assertEqual(self.cost("compare"), (4, 2))

    def test_logic_and_scope_links_sum_children(self):
        self.assertEqual(self.cost("logic"), (5, 0))

    def test_engine_operands_are_free(self):
        self.assertEqual(self.cost("engine"), (0, 0))

    def test_leaf_detection(self):
        self.assertTrue(self.model.is_leaf("leaf_chain"))
        self.assertFalse(self.model.is_leaf("helper"))

    def test_flat_violations_skip_leaves(self):
        found = self.model.flat_violations()
        self.assertIn(("twice_helper", "helper", 2), found)
        self.assertFalse([f for f in found if f[0] == "twice"])

    def test_compare_limits(self):
        found = self.model.compare_limits()
        self.assertEqual([(n, k) for n, k, _ in found], [("compare", "helper")])

    def test_cycle_raises(self):
        with open(os.path.join(self.tmp, "fixture.txt"), "w", encoding="utf-8") as fh:
            fh.write(CYCLE)
        model = cm.CostModel(cm.load_values(self.tmp))
        with self.assertRaises(cm.CycleError):
            model.value("a_val")

    def test_main_reports_missing_name(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = cm.main(["--dir", self.tmp, "--values", "twice,nope"])
        self.assertEqual(status, 1)
        self.assertIn("missing", out.getvalue())


class CostModelRealMod(unittest.TestCase):
    LAND_USE_GUI = {
        "dcl_land_drift_delta",
        "dcl_cultivation_target",
        "dcl_gui_mud_active",
        "dcl_gui_rot_active",
        "dcl_gui_drought_active",
        "dcl_gui_heat_exhaustion_active",
        "dcl_gui_lethal_heat_active",
        "dcl_gui_wet_bulb_available",
    }

    @classmethod
    def setUpClass(cls):
        directory = os.path.join(MOD_ROOT, "in_game", "common", "script_values")
        cls.model = cm.CostModel(cm.load_values(directory))

    def checks(self, name):
        return self.model.value(name).checks

    def test_temperature_budget(self):
        self.assertLessEqual(self.checks("dynamic_location_temperature"), 300)

    def test_humidity_budget(self):
        self.assertLessEqual(
            self.checks("dynamic_location_humidity"),
            self.checks("dynamic_location_temperature") + 40,
        )

    def test_wet_bulb_budget(self):
        self.assertLessEqual(
            self.checks("wet_bulb_temperature"),
            6 * self.checks("dynamic_location_temperature"),
        )

    def test_cached_twins_do_not_recompute_temperature(self):
        for name in (
            "dcl_location_humidity_cached",
            "dcl_saturation_vapor_pressure_cached",
            "dcl_wet_bulb_cached",
        ):
            self.assertLess(self.checks(name), self.checks("dynamic_location_temperature"), name)

    def test_climate_files_have_no_computed_limits(self):
        found = [f for f in self.model.compare_limits() if f[0] not in self.LAND_USE_GUI]
        self.assertEqual(found, [])

    def test_no_flat_violations_outside_allowlist(self):
        found = [f for f in self.model.flat_violations() if f[0] not in self.LAND_USE_GUI]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
