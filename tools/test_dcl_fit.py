import io
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dcl_fit_coefficients as fit  # noqa: E402


def frange(lo, hi, n):
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


class BhaskaraTest(unittest.TestCase):
    def test_max_error_over_full_cycle(self):
        err = max(abs(fit.bhaskara_sin(x) - math.sin(x)) for x in frange(0.0, 2.0 * math.pi, 20001))
        self.assertLess(err, 0.002)


class AcosTest(unittest.TestCase):
    def test_fit_within_0_04_rad_over_unit_interval(self):
        c1, c3, c5, c7, _ = fit.fit_acos()
        c = (c1, c3, c5, c7)
        err = max(abs(fit.acos_poly(x, c) - math.acos(x)) for x in frange(-1.0, 1.0, 4001))
        self.assertLess(err, 0.04)

    def test_day_length_form_within_0_04_rad(self):
        d, _ = fit.acos_day_constants()
        err = max(abs(math.pi * fit.day_length_poly(x, d) - math.acos(x)) for x in frange(-1.0, 1.0, 4001))
        self.assertLess(err, 0.04)


class MagnusTest(unittest.TestCase):
    def test_lowe_matches_exact_within_0_1_hpa(self):
        err = max(abs(fit.magnus_lowe(t) - fit.magnus_exact(t)) for t in frange(-40.0, 50.0, 901))
        self.assertLess(err, 0.1)

    def test_lowe_reference_point(self):
        self.assertAlmostEqual(fit.magnus_lowe(0.0), 6.1078, delta=1e-3)
        self.assertAlmostEqual(fit.magnus_lowe(20.0), 23.37, delta=0.05)


class WetBulbTest(unittest.TestCase):
    def test_surrogate_within_0_5_c(self):
        c, _ = fit.fit_wet_bulb()
        self.assertEqual(len(c), len(fit.WB_TERMS))
        err = max(
            abs(fit.wb_surrogate(t, rh, c) - fit.stull_wet_bulb(t, rh))
            for t in frange(20.0, 50.0, 61)
            for rh in frange(5.0, 100.0, 96)
        )
        self.assertLess(err, 0.5)

    def test_surrogate_never_exceeds_air_temperature(self):
        c = fit.fit_wet_bulb()[0]
        for t in frange(20.0, 50.0, 31):
            for rh in frange(5.0, 100.0, 20):
                self.assertLessEqual(fit.wb_surrogate(t, rh, c), t)


class TemperatureTest(unittest.TestCase):
    def test_every_climate_monthly_means_within_2_c(self):
        for climate in fit.CLIMATES:
            with self.subTest(climate=climate):
                means = fit.monthly_means(climate, coastal=False, southern=False, elev=100.0)
                target = fit.TARGET_CURVES[climate]
                err = max(abs(m - t) for m, t in zip(means, target))
                self.assertLess(err, 2.0)

    def test_southern_hemisphere_inverts_season(self):
        for climate in ("continental", "oceanic", "mediterranean"):
            north_jan = fit.model_temperature(climate, 15, False, False, 100.0)
            south_jan = fit.model_temperature(climate, 15, False, True, 100.0)
            self.assertGreater(south_jan, north_jan + 5.0)

    def test_elevation_lapse_rate(self):
        low = fit.model_temperature("continental", 196, False, False, 100.0)
        high = fit.model_temperature("continental", 196, False, False, 2500.0)
        self.assertAlmostEqual(low - high, 2400.0 * 0.0065, delta=1e-9)


class EmittedConstantsTest(unittest.TestCase):
    def test_every_coefficient_representable(self):
        constants = fit.emitted_constants()
        self.assertTrue(constants)
        for name, value in constants.items():
            with self.subTest(name=name):
                self.assertTrue(value == 0 or abs(value) >= 1e-4, f"{name} = {value}")

    def test_every_climate_emits_temperature_terms(self):
        constants = fit.emitted_constants()
        for climate in fit.CLIMATES:
            for suffix in ("temp_offset", "temp_gain", "temp_decl2_gain"):
                with self.subTest(climate=climate, term=suffix):
                    self.assertIn(f"{climate}_{suffix}", constants)

    def test_reference_table_rows(self):
        rows = fit.reference_table()
        self.assertEqual(len(rows), len(fit.CLIMATES) * 12 * 2)
        for row in rows:
            if row["Tw"] is not None:
                self.assertLessEqual(row["Tw"], row["T"])
            self.assertGreaterEqual(row["H"], 5.0)
            self.assertLessEqual(row["H"], 100.0)

    def test_blocks_use_four_space_indentation(self):
        buf = io.StringIO()
        fit.print_blocks(buf)
        text = buf.getvalue()
        self.assertNotIn("\t", text)
        # per-climate chains emit the 7 named climates as if/else_if, oceanic as a final else
        self.assertIn("\n    if = { limit = { climate = tropical }", text)
        self.assertIn("\n    else = { value = ", text)
        self.assertNotIn("else_if = { limit = { climate = oceanic }", text)


class StdlibFallbackTest(unittest.TestCase):
    def test_every_threshold_passes_without_numpy(self):
        saved_np, saved_cache = fit.np, fit._cache
        fit.np = None
        fit._cache = {}
        try:
            results = fit.error_report()
        finally:
            fit.np, fit._cache = saved_np, saved_cache
        self.assertEqual(set(results), set(fit.THRESHOLDS))
        for key, value in results.items():
            with self.subTest(check=key):
                self.assertLess(value, fit.THRESHOLDS[key])


if __name__ == "__main__":
    unittest.main()
