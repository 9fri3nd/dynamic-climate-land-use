import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dcl_eval as ev  # noqa: E402
import dcl_fit_coefficients as fit  # noqa: E402

MOD_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALUES_DIR = os.path.join(MOD_ROOT, "in_game", "common", "script_values")

FIXTURE = """ops_test = {
    value = 7
    add = 2
    subtract = 1
    multiply = 3
    divide = 4
    min = 10
    max = 5
}
pow_abs_test = {
    value = -3
    abs = 0
    pow = 2
}
chain_test = {
    value = 0
    if = { limit = { climate = arid } value = 1 }
    else_if = { limit = { climate = arctic } value = 2 }
    else = { value = 3 }
    if = { limit = { is_coastal = yes } add = 10 }
}
logic_test = {
    value = 0
    if = {
        limit = {
            OR = { climate = arid climate = arctic }
            NOT = { is_coastal = yes }
        }
        value = 1
    }
}
var_read_test = {
    value = var:cultivation_ratio
    add = area.var:macro_temp_anomaly
    add = scope:dcl_t
}
var_limit_test = {
    value = 0
    if = { limit = { area ?= { has_variable = macro_temp_anomaly } } value = 1 }
}
compare_test = {
    value = 0
    if = { limit = { pow_abs_test >= 9 } value = 42 }
}
divide_named_test = {
    value = 8
    divide = pow_abs_test
}
"""


class EvalFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "fixture.txt"), "w", encoding="utf-8") as fh:
            fh.write(FIXTURE)
        self.values = ev.load_values(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def evaluate(self, name, **scope_kwargs):
        return ev.Evaluator(self.values, ev.MockScope(**scope_kwargs)).value(name)

    def test_row_ops_and_min_max_semantics(self):
        # (((7 + 2 - 1) * 3) / 4 = 6, floored at 10, capped at 5) -> 5
        self.assertEqual(self.evaluate("ops_test"), 5.0)

    def test_abs_and_pow(self):
        self.assertEqual(self.evaluate("pow_abs_test"), 9.0)

    def test_chain_branches_and_second_if(self):
        self.assertEqual(self.evaluate("chain_test", climate="arid"), 1.0)
        self.assertEqual(self.evaluate("chain_test", climate="arctic"), 2.0)
        self.assertEqual(self.evaluate("chain_test", climate="tropical"), 3.0)
        self.assertEqual(self.evaluate("chain_test", climate="tropical", coastal=True), 13.0)

    def test_logic_blocks(self):
        self.assertEqual(self.evaluate("logic_test", climate="arid"), 1.0)
        self.assertEqual(self.evaluate("logic_test", climate="arid", coastal=True), 0.0)
        self.assertEqual(self.evaluate("logic_test", climate="tropical"), 0.0)

    def test_operands(self):
        scope = dict(variables={"cultivation_ratio": 0.4},
                     area_vars={"macro_temp_anomaly": 8}, scope_values={"dcl_t": 21.5})
        self.assertAlmostEqual(self.evaluate("var_read_test", **scope), 29.9)

    def test_area_link_limit(self):
        self.assertEqual(self.evaluate("var_limit_test", area_vars={"macro_temp_anomaly": 8}), 1.0)
        self.assertEqual(self.evaluate("var_limit_test"), 0.0)

    def test_named_value_in_limit_and_divide(self):
        self.assertEqual(self.evaluate("compare_test"), 42.0)
        self.assertAlmostEqual(self.evaluate("divide_named_test"), 8.0 / 9.0)


class RealModMirror(unittest.TestCase):
    """Emitted script rows vs the fitter's script-form models over the full input grid."""

    @classmethod
    def setUpClass(cls):
        cls.values = ev.load_values(VALUES_DIR)
        cls.wb = fit.fit_wet_bulb()[0]

    def evaluate(self, name, **scope_kwargs):
        return ev.Evaluator(self.values, ev.MockScope(**scope_kwargs)).value(name)

    def test_declination_table_routes(self):
        for climate in fit.CLIMATES:
            for coastal in (False, True):
                for m in range(12):
                    got = self.evaluate("dcl_lagged_declination", climate=climate, month=m + 1,
                                   coastal=coastal)
                    want = fit.script_lagged_declination(climate, m, coastal)
                    self.assertAlmostEqual(got, want, 9, f"{climate} m{m + 1} coastal={coastal}")

    def test_solar_declination_chain(self):
        for m in range(12):
            got = self.evaluate("calc_solar_declination", month=m + 1)
            self.assertAlmostEqual(got, fit.script_solar_declination(m), 9, f"month {m + 1}")
        # solstices: max at June (northern summer), min at December
        june = self.evaluate("calc_solar_declination", month=6)
        december = self.evaluate("calc_solar_declination", month=12)
        self.assertGreater(june, 0.39)
        self.assertLess(december, -0.39)

    def test_grid_temperature_humidity_wet_bulb(self):
        worst = {"T": 0.0, "H": 0.0, "Tw": 0.0, "Es": 0.0, "Q": 0.0, "dlf": 0.0}
        for climate in fit.CLIMATES:
            _, tan_phi, _, _, base = fit.PROXIES[climate]
            for coastal in (False, True):
                for southern in (False, True):
                    for topo in ("flatland", "mountains"):
                        elev = float(fit.ELEVATION[topo])
                        for m in range(12):
                            scope = dict(climate=climate, month=m + 1, coastal=coastal,
                                         southern=southern, topography=topo)
                            t_want = fit.script_temperature(climate, m, coastal, southern, elev)
                            t_got = self.evaluate("dynamic_location_temperature", **scope)
                            worst["T"] = max(worst["T"], abs(t_got - t_want))
                            mean = fit.temperature_mean(climate, elev)
                            h_want = max(5.0, min(100.0, base + (20 if coastal else 0)
                                                  - fit.HUMIDITY_SLOPE * (t_want - mean)))
                            h_got = self.evaluate("dynamic_location_humidity", **scope)
                            worst["H"] = max(worst["H"], abs(h_got - h_want))
                            tw_want = fit.wb_script(t_want, h_want, self.wb)
                            tw_got = self.evaluate("wet_bulb_temperature", **scope)
                            worst["Tw"] = max(worst["Tw"], abs(tw_got - tw_want))
                            es_want = fit.magnus_lowe(max(-50.0, min(50.0, t_want)))
                            es_got = self.evaluate("dynamic_saturation_vapor_pressure", **scope)
                            worst["Es"] = max(worst["Es"], abs(es_got - es_want))
                            q_want = fit.script_insolation(climate, m, coastal, southern)
                            q_got = self.evaluate("dcl_insolation", **scope)
                            worst["Q"] = max(worst["Q"], abs(q_got - q_want))
                            tan_signed = -tan_phi if southern else tan_phi
                            dec = fit.script_lagged_declination(climate, m, coastal)
                            dlf_want = fit.script_day_length(tan_signed, dec)
                            dlf_got = self.evaluate("calc_day_length_factor", **scope)
                            worst["dlf"] = max(worst["dlf"], abs(dlf_got - dlf_want))
        for key, gap in worst.items():
            self.assertLess(gap, 1e-6, f"{key} mirror gap {gap}")
        print(f"\nmirror max gaps: {worst}")

    def test_area_anomalies(self):
        base_scope = dict(climate="continental", month=7, topography="flatland")
        t0 = self.evaluate("dynamic_location_temperature", **base_scope)
        h0 = self.evaluate("dynamic_location_humidity", **base_scope)
        front = dict(base_scope, area_vars={"macro_temp_anomaly": 8,
                                            "macro_humidity_anomaly": -20,
                                            "dcl_front_type": 1})
        self.assertAlmostEqual(self.evaluate("dynamic_location_temperature", **front), t0 + 8, 9)
        # the anomaly also cools H through the temperature deviation term: -1.2 * 8
        h_want = max(5.0, min(100.0, h0 - 20 - 1.2 * 8))
        self.assertAlmostEqual(self.evaluate("dynamic_location_humidity", **front), h_want, 9)
        self.assertEqual(self.evaluate("dcl_location_front_type", **front), 1.0)
        self.assertEqual(self.evaluate("dcl_temp_anomaly", **base_scope), 0.0)

    def test_cached_twins(self):
        for t, h in ((35.0, 90.0), (22.5, 40.0), (45.0, 12.0)):
            scope = dict(scope_values={"dcl_t": t, "dcl_h": h})
            self.assertEqual(self.evaluate("dcl_location_temperature_cached", **scope), t)
            self.assertEqual(self.evaluate("dcl_location_humidity_cached", **scope), h)
            self.assertAlmostEqual(self.evaluate("dcl_wet_bulb_cached", **scope),
                                   fit.wb_script(t, h, self.wb), 9)
            self.assertAlmostEqual(self.evaluate("dcl_saturation_vapor_pressure_cached", **scope),
                                   fit.magnus_lowe(max(-50.0, min(50.0, t))), 9)

    def test_land_use_values(self):
        self.assertEqual(self.evaluate("dcl_initial_cultivation", topography="flatland",
                                  vegetation="farmland"), 0.60)
        self.assertEqual(self.evaluate("dcl_initial_cultivation", topography="wetlands",
                                  vegetation="farmland"), 0.05)
        self.assertEqual(self.evaluate("dcl_max_arable", topography="flatland",
                                  vegetation="farmland"), 0.95)
        scope = dict(variables={"cultivation_ratio": 0.4})
        self.assertAlmostEqual(self.evaluate("dcl_wilderness_ratio", **scope), 0.6)
        self.assertAlmostEqual(self.evaluate("dcl_cultivation_percent", **scope), 40.0)
        stress = dict(scope, location_modifiers={"dcl_climate_stress_memory"},
                      rgo_workers=10, max_rgo_workers=100)
        self.assertAlmostEqual(self.evaluate("dcl_land_drift_delta", **stress), -0.07)
        growth = dict(scope, rgo_workers=90, max_rgo_workers=100, market_food_balance=5)
        self.assertAlmostEqual(self.evaluate("dcl_land_drift_delta", **growth), 0.02)
        self.assertAlmostEqual(self.evaluate("dcl_cultivation_target", **growth), 0.42)
        self.assertAlmostEqual(self.evaluate("dcl_cultivation_set_delta", **growth), 0.02)
        # re-wilding never drops below 0.05
        floor = dict(variables={"cultivation_ratio": 0.06},
                     location_modifiers={"dcl_climate_stress_memory"})
        self.assertAlmostEqual(self.evaluate("dcl_cultivation_target", **floor), 0.05)

    def test_gui_flags(self):
        tropical = dict(climate="tropical", month=7, coastal=True, topography="flatland")
        self.assertEqual(self.evaluate("dcl_gui_wet_bulb_available", **tropical), 1.0)
        arctic_winter = dict(climate="arctic", month=1, topography="flatland")
        self.assertEqual(self.evaluate("dcl_gui_wet_bulb_available", **arctic_winter), 0.0)
        # flag logic self-consistency: flags match their threshold conditions
        for climate in fit.CLIMATES:
            for m in (1, 7):
                scope = dict(climate=climate, month=m, coastal=True, topography="flatland")
                t = self.evaluate("dynamic_location_temperature", **scope)
                h = self.evaluate("dynamic_location_humidity", **scope)
                tw = self.evaluate("wet_bulb_temperature", **scope)
                mud = 1.0 if (1 <= t <= 8 and h > 80 and climate != "arctic") else 0.0
                self.assertEqual(self.evaluate("dcl_gui_mud_active", **scope), mud, (climate, m))
                rot = 1.0 if (t > 30 and h > 85) else 0.0
                self.assertEqual(self.evaluate("dcl_gui_rot_active", **scope), rot, (climate, m))
                drought = 1.0 if (t > 32 and h < 20) else 0.0
                self.assertEqual(self.evaluate("dcl_gui_drought_active", **scope), drought, (climate, m))
                heat = 1.0 if (31 <= tw < 35) else 0.0
                self.assertEqual(self.evaluate("dcl_gui_heat_exhaustion_active", **scope), heat,
                                 (climate, m))
                lethal = 1.0 if tw >= 35 else 0.0
                self.assertEqual(self.evaluate("dcl_gui_lethal_heat_active", **scope), lethal,
                                 (climate, m))


if __name__ == "__main__":
    unittest.main()
