import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dcl_verify_save as vs  # noqa: E402


def var(name, ident=None, multiline=False):
    """A variable entry; identity omitted when ident is None (value 0 on disk)."""
    if multiline:
        lines = ["{", f"flag={name}", "data={", "type=value"]
        if ident is not None:
            lines.append(f"identity={ident}")
        lines += ["}", "}"]
        return "\n".join(lines)
    inner = f" type=value identity={ident} " if ident is not None else " type=value "
    return "{ flag=" + name + " data={" + inner + "} }"


def mod(name, size=None):
    inner = f" start_date=1337.1.1 size={size} " if size is not None else " start_date=1337.1.1 "
    return "{ modifier=" + name + inner + "}"


def location_block(key, vars=(), mods=(), extra_fields=""):
    lines = [f"{key}={{", "variables={"]
    lines += vars
    lines += ["}", "timed_modifiers={"]
    lines += mods
    lines += ["}"]
    if extra_fields:
        lines.append(extra_fields)
    lines.append("}")
    return "\n".join(lines)


def area_block(key, vars=()):
    return "\n".join([f"{key}={{", "variables={", *vars, "}", "}"])


def save_text(date="1338.7.15", locations=(), areas=(), tail=""):
    parts = ["SAV0200TEST", "metadata={", f"date={date}", "playthrough_id=\"x\"", "}",
             "locations={", *locations, "}", "areas={", *areas, "}", tail]
    return "\n".join(parts) + "\n"


def write_save(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def ident(value):
    return int(round(value * 100000))


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def parse(self, text):
        return vs.parse_save(write_save(self.dir, "t.eu5", text))

    def test_identity_present_compact(self):
        save = self.parse(save_text(locations=[location_block(
            "paris", vars=[var("cultivation_ratio", ident(0.6))])]))
        self.assertAlmostEqual(save.records["paris"].variables["cultivation_ratio"], 0.6)

    def test_identity_omitted_is_zero_no_bleed(self):
        # zero-valued var (no identity) followed by another entry: the pending
        # read must flush as 0 and not bleed the next entry's identity into it
        save = self.parse(save_text(locations=[location_block(
            "paris", vars=[var("zero_thing"), var("cultivation_ratio", ident(0.6))])]))
        vs_ = save.records["paris"].variables
        self.assertEqual(vs_["zero_thing"], 0.0)
        self.assertAlmostEqual(vs_["cultivation_ratio"], 0.6)

    def test_identity_omitted_multiline_closes_on_brace(self):
        save = self.parse(save_text(locations=[location_block(
            "paris", vars=[var("zero_thing", multiline=True),
                           var("cultivation_ratio", ident(0.25), multiline=True)])]))
        vs_ = save.records["paris"].variables
        self.assertEqual(vs_["zero_thing"], 0.0)
        self.assertAlmostEqual(vs_["cultivation_ratio"], 0.25)

    def test_bare_flag_without_data_is_not_a_variable(self):
        save = self.parse(save_text(tail="country={\nflags={\n{ flag=plain_flag }\n}\n}"))
        self.assertNotIn("plain_flag", save.records.get("country", vs.SaveRecord("x")).variables)

    def test_modifier_size_present_and_omitted(self):
        save = self.parse(save_text(locations=[location_block(
            "paris",
            vars=[var("cultivation_ratio", ident(0.6))],
            mods=[mod("dcl_cultivated_land", 0.6), mod("dcl_wilderness_land")])]))
        ms = save.records["paris"].modifiers
        self.assertAlmostEqual(ms["dcl_cultivated_land"], 0.6)
        self.assertEqual(ms["dcl_wilderness_land"], 0.0)  # size omitted == 0

    def test_modifier_zero_flushed_by_next_modifier(self):
        save = self.parse(save_text(locations=[location_block(
            "paris",
            vars=[var("cultivation_ratio", ident(0.6))],
            mods=[mod("dcl_wilderness_land"), mod("dcl_cultivated_land", 0.6)])]))
        ms = save.records["paris"].modifiers
        self.assertEqual(ms["dcl_wilderness_land"], 0.0)
        self.assertAlmostEqual(ms["dcl_cultivated_land"], 0.6)

    def test_non_dcl_modifier_ignored(self):
        save = self.parse(save_text(locations=[location_block(
            "paris", mods=[mod("some_vanilla_modifier", 3.5)])]))
        self.assertNotIn("paris", vs.parse_modifiers(save))

    def test_date_from_metadata_not_start_date(self):
        text = save_text(date="1444.11.11",
                         locations=[location_block("paris", mods=[mod("dcl_mud_season", 1.0)])])
        save = self.parse(text)
        self.assertEqual(save.date, (1444, 11, 11))

    def test_numeric_record_keys(self):
        text = save_text(tail="provinces={\ndatabase={\n12345={\nvariables={\n"
                                + var("cultivation_ratio", ident(0.3), multiline=True) + "\n}\n}\n}\n}")
        save = self.parse(text)
        self.assertAlmostEqual(save.records["12345"].variables["cultivation_ratio"], 0.3)

    def test_parse_views(self):
        save = self.parse(save_text(
            locations=[location_block("paris", vars=[var("cultivation_ratio", ident(0.6))],
                                      mods=[mod("dcl_cultivated_land", 0.6)]),
                       location_block("wilderness")],
            areas=[area_block("ile_de_france_area", vars=[var("dcl_area_class", ident(2))])]))
        self.assertEqual(set(vs.parse_locations_vars(save)), {"paris"})
        self.assertEqual(set(vs.parse_area_vars(save)), {"ile_de_france_area"})
        self.assertEqual(set(vs.parse_modifiers(save)), {"paris"})


class CheckTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def parse(self, text):
        return vs.parse_save(write_save(self.dir, "t.eu5", text))


def cultivated(key, ratio, cult_size=None, wild_size=None, extra_vars=(), extra_mods=()):
    """A location record with the ratio and (by default) matching land modifiers."""
    cult_size = ratio if cult_size is None else cult_size
    wild_size = 1.0 - ratio if wild_size is None else wild_size
    return location_block(
        key,
        vars=[var("cultivation_ratio", ident(ratio)), *extra_vars],
        mods=[mod("dcl_cultivated_land", cult_size), mod("dcl_wilderness_land", wild_size),
              *extra_mods])


class RatioBoundsTest(CheckTestBase):
    def test_in_bounds_passes(self):
        save = self.parse(save_text(locations=[cultivated("paris", 0.6), cultivated("novgorod", 0.05)]))
        res = vs.check_ratio_bounds(save)
        self.assertEqual(res.failures, [])

    def test_below_min_fails(self):
        save = self.parse(save_text(locations=[cultivated("paris", 0.049)]))
        res = vs.check_ratio_bounds(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("outside", res.failures[0])

    def test_above_max_fails(self):
        save = self.parse(save_text(locations=[cultivated("elsewhere", 0.96)]))
        res = vs.check_ratio_bounds(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("outside", res.failures[0])

    def test_above_max_on_roster_location_fails_twice(self):
        # paris is farmland -> cap 0.95; 0.96 violates the clamp AND the cap
        save = self.parse(save_text(locations=[cultivated("paris", 0.96)]))
        res = vs.check_ratio_bounds(save)
        self.assertEqual(len(res.failures), 2)

    def test_boundary_values_pass(self):
        save = self.parse(save_text(locations=[cultivated("paris", 0.05), cultivated("novgorod", 0.95)]))
        res = vs.check_ratio_bounds(save)
        self.assertEqual(res.failures, [])

    def test_roster_terrain_cap(self):
        # timbuktu is desert -> cap 0.20 (spec 7.2)
        save = self.parse(save_text(locations=[cultivated("timbuktu", 0.25)]))
        res = vs.check_ratio_bounds(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("cap", res.failures[0])
        save = self.parse(save_text(locations=[cultivated("timbuktu", 0.15)]))
        self.assertEqual(vs.check_ratio_bounds(save).failures, [])

    def test_save_carried_terrain_cap(self):
        save = self.parse(save_text(locations=[location_block(
            "elsewhere",
            vars=[var("cultivation_ratio", ident(0.3))],
            mods=[mod("dcl_cultivated_land", 0.3), mod("dcl_wilderness_land", 0.7)],
            extra_fields="topography=mountains\nvegetation=farmland")]))
        res = vs.check_ratio_bounds(save)
        self.assertEqual(res.failures, [])  # mountains cap 0.35, ratio 0.3 is fine
        self.assertIn("1 cap-checked", res.notes[0])

    def test_save_carried_terrain_cap_violation(self):
        save = self.parse(save_text(locations=[location_block(
            "elsewhere",
            vars=[var("cultivation_ratio", ident(0.5))],
            mods=[mod("dcl_cultivated_land", 0.5), mod("dcl_wilderness_land", 0.5)],
            extra_fields="topography=wetlands\nvegetation=farmland")]))
        res = vs.check_ratio_bounds(save)
        self.assertEqual(len(res.failures), 1)  # wetlands cap 0.40
        self.assertIn("0.40", res.failures[0])


class ForeignVarTest(CheckTestBase):
    def test_clean_location_passes(self):
        save = self.parse(save_text(locations=[cultivated("paris", 0.6)]))
        self.assertEqual(vs.check_no_foreign_location_vars(save).failures, [])

    def test_dcl_var_on_location_fails(self):
        save = self.parse(save_text(locations=[cultivated(
            "paris", 0.6, extra_vars=[var("dcl_debug", ident(1))])]))
        res = vs.check_no_foreign_location_vars(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("dcl_debug", res.failures[0])

    def test_macro_and_eco_vars_fail(self):
        save = self.parse(save_text(locations=[
            cultivated("paris", 0.6, extra_vars=[var("macro_temp_anomaly", ident(8))]),
            cultivated("novgorod", 0.6, extra_vars=[var("eco_gdd_acc_wheat", ident(500))])]))
        res = vs.check_no_foreign_location_vars(save)
        self.assertEqual(len(res.failures), 2)

    def test_macro_vars_on_area_records_not_flagged(self):
        save = self.parse(save_text(
            locations=[cultivated("timbuktu", 0.05)],
            areas=[area_block("timbuktu_area", vars=[
                var("dcl_area_class", ident(1)), var("dcl_front_type", ident(3)),
                var("macro_humidity_anomaly", ident(40))])]))
        self.assertEqual(vs.check_no_foreign_location_vars(save).failures, [])

    def test_foreign_var_via_land_modifier_record(self):
        # a record with land modifiers but no ratio is still a mod location record
        save = self.parse(save_text(locations=[location_block(
            "paris", vars=[var("dcl_stray", ident(1))], mods=[mod("dcl_cultivated_land", 0.6)])]))
        res = vs.check_no_foreign_location_vars(save)
        self.assertEqual(len(res.failures), 1)


class FrontCountTest(CheckTestBase):
    def area_with_front(self, key, ftype, cls=1, anomalies=True):
        vars_ = [var("dcl_area_class", ident(cls)), var("dcl_front_type", ident(ftype))]
        if anomalies:
            ta, ha = vs.FRONT_TABLE[ftype]
            if ta is not None:
                vars_.append(var("macro_temp_anomaly", ident(ta)))
            vars_.append(var("macro_humidity_anomaly", ident(ha)))
        return area_block(key, vars=vars_)

    def test_five_fronts_pass(self):
        save = self.parse(save_text(
            locations=[cultivated("paris", 0.6)],
            areas=[self.area_with_front(f"area{i}_area", 1) for i in range(5)]))
        self.assertEqual(vs.check_front_count(save).failures, [])

    def test_sixteen_fronts_fail(self):
        save = self.parse(save_text(
            locations=[cultivated("paris", 0.6)],
            areas=[self.area_with_front(f"area{i}_area", 1) for i in range(16)]))
        res = vs.check_front_count(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("15", res.failures[0])

    def test_invalid_front_type_fails(self):
        save = self.parse(save_text(
            locations=[cultivated("paris", 0.6)],
            areas=[area_block("a_area", vars=[var("dcl_area_class", ident(1)),
                                              var("dcl_front_type", ident(4))])]))
        res = vs.check_front_count(save)
        self.assertTrue(any("not in {1,2,3}" in f for f in res.failures))

    def test_front_without_area_class_fails(self):
        save = self.parse(save_text(
            locations=[cultivated("paris", 0.6)],
            areas=[area_block("a_area", vars=[var("dcl_front_type", ident(1)),
                                              var("macro_temp_anomaly", ident(8)),
                                              var("macro_humidity_anomaly", ident(-20))])]))
        res = vs.check_front_count(save)
        self.assertTrue(any("without dcl_area_class" in f for f in res.failures))

    def test_invalid_area_class_fails(self):
        save = self.parse(save_text(
            locations=[cultivated("paris", 0.6)],
            areas=[area_block("a_area", vars=[var("dcl_area_class", ident(7))])]))
        res = vs.check_front_count(save)
        self.assertTrue(any("dcl_area_class" in f for f in res.failures))

    def test_locations_but_no_stamped_area_fails(self):
        save = self.parse(save_text(locations=[cultivated("paris", 0.6)]))
        res = vs.check_front_count(save)
        self.assertTrue(any("dcl_stamp_area_class" in f for f in res.failures))

    def test_zero_fronts_is_legal(self):
        save = self.parse(save_text(
            locations=[cultivated("paris", 0.6)],
            areas=[area_block("a_area", vars=[var("dcl_area_class", ident(2))])]))
        self.assertEqual(vs.check_front_count(save).failures, [])


class LandSizesTest(CheckTestBase):
    def test_matching_sizes_pass(self):
        save = self.parse(save_text(
            locations=[cultivated("paris", 0.6), cultivated("novgorod", 0.05)],
            areas=[area_block("a_area", vars=[var("dcl_area_class", ident(2))])]))
        self.assertEqual(vs.check_land_sizes(save).failures, [])

    def test_wilderness_mismatch_fails(self):
        save = self.parse(save_text(locations=[cultivated("paris", 0.6, wild_size=0.5)]))
        res = vs.check_land_sizes(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("dcl_wilderness_land", res.failures[0])

    def test_cultivated_mismatch_fails(self):
        save = self.parse(save_text(locations=[cultivated("paris", 0.6, cult_size=0.61)]))
        res = vs.check_land_sizes(save)
        self.assertEqual(len(res.failures), 1)

    def test_missing_modifiers_fail(self):
        save = self.parse(save_text(locations=[location_block(
            "paris", vars=[var("cultivation_ratio", ident(0.6))])]))
        res = vs.check_land_sizes(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("missing", res.failures[0])

    def test_orphan_modifier_fails(self):
        save = self.parse(save_text(locations=[location_block(
            "paris", mods=[mod("dcl_cultivated_land", 0.6)])]))
        res = vs.check_land_sizes(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("without cultivation_ratio", res.failures[0])


class DriftTest(CheckTestBase):
    def two_saves(self, ratio_a, ratio_b, date_a="1338.4.1", date_b="1339.4.1", key="paris"):
        a = self.parse(save_text(date=date_a, locations=[cultivated(key, ratio_a)]))
        b = self.parse(save_text(date=date_b, locations=[cultivated(key, ratio_b)]))
        return a, b

    def test_within_bounds_passes(self):
        a, b = self.two_saves(0.60, 0.53)   # -0.07 over one year
        res = vs.check_drift([a, b])
        self.assertEqual(res.failures, [])
        a, b = self.two_saves(0.60, 0.62)   # +0.02 over one year
        self.assertEqual(vs.check_drift([a, b]).failures, [])

    def test_too_fast_decay_fails(self):
        a, b = self.two_saves(0.60, 0.45)   # -0.15 exceeds the two-pulse bound (-0.14)
        res = vs.check_drift([a, b])
        self.assertEqual(len(res.failures), 1)
        self.assertIn("drift", res.failures[0])

    def test_too_fast_growth_fails(self):
        a, b = self.two_saves(0.60, 0.65)   # +0.05 exceeds the two-pulse bound (+0.04)
        res = vs.check_drift([a, b])
        self.assertEqual(len(res.failures), 1)

    def test_multi_pulse_gap(self):
        # 400 days can hold at most two yearly pulses (sound bound, no pulse-date
        # assumption), so -0.14 passes but -0.15 is convicted
        a, b = self.two_saves(0.60, 0.46, date_b="1339.5.6")
        self.assertEqual(vs.check_drift([a, b]).failures, [])
        a, b = self.two_saves(0.60, 0.45, date_b="1339.5.6")
        self.assertEqual(len(vs.check_drift([a, b]).failures), 1)

    def test_same_date_pair_skipped(self):
        a, b = self.two_saves(0.60, 0.10, date_b="1338.4.1")
        res = vs.check_drift([a, b])
        self.assertEqual(res.failures, [])
        self.assertTrue(any("same-date" in n for n in res.notes))

    def test_new_location_skipped(self):
        a = self.parse(save_text(date="1338.4.1", locations=[cultivated("paris", 0.6)]))
        b = self.parse(save_text(date="1339.4.1", locations=[cultivated("paris", 0.6),
                                                             cultivated("novgorod", 0.05)]))
        self.assertEqual(vs.check_drift([a, b]).failures, [])

    def test_single_save_skips(self):
        a = self.parse(save_text(locations=[cultivated("paris", 0.6)]))
        res = vs.check_drift([a])
        self.assertEqual(res.failures, [])
        self.assertTrue(any("skipped" in n for n in res.notes))


TIGHT_BANDS = lambda *a: 0.5  # noqa: E731


def fake_table(climate, months):
    """{(climate, month, 0): row} from {month: (T_mid, H)} with Tw absent."""
    return {(climate, m, 0): {"T": t, "T_mid": t, "H": h, "Tw": None, "E_s": None}
            for m, (t, h) in months.items()}


class ReferenceFrontTableTest(CheckTestBase):
    def check(self, save):
        return vs.check_reference_predictions(save, table={}, roster=[], band_t_map=TIGHT_BANDS)

    def test_correct_anomalies_pass(self):
        save = self.parse(save_text(
            locations=[cultivated("paris", 0.6)],
            areas=[area_block("a_area", vars=[var("dcl_area_class", ident(1)),
                                              var("dcl_front_type", ident(1)),
                                              var("macro_temp_anomaly", ident(8)),
                                              var("macro_humidity_anomaly", ident(-20))]),
                   area_block("b_area", vars=[var("dcl_area_class", ident(3)),
                                              var("dcl_front_type", ident(2)),
                                              var("macro_temp_anomaly", ident(-12)),
                                              var("macro_humidity_anomaly", ident(-10))]),
                   area_block("c_area", vars=[var("dcl_area_class", ident(1)),
                                              var("dcl_front_type", ident(3)),
                                              var("macro_humidity_anomaly", ident(40))])]))
        self.assertEqual(self.check(save).failures, [])

    def test_wrong_temp_anomaly_fails(self):
        save = self.parse(save_text(areas=[area_block("a_area", vars=[
            var("dcl_area_class", ident(1)), var("dcl_front_type", ident(1)),
            var("macro_temp_anomaly", ident(5)), var("macro_humidity_anomaly", ident(-20))])]))
        res = self.check(save)
        self.assertTrue(any("macro_temp_anomaly +8" in f for f in res.failures))

    def test_monsoon_must_not_carry_temp_anomaly(self):
        save = self.parse(save_text(areas=[area_block("a_area", vars=[
            var("dcl_area_class", ident(1)), var("dcl_front_type", ident(3)),
            var("macro_temp_anomaly", ident(8)), var("macro_humidity_anomaly", ident(40))])]))
        res = self.check(save)
        self.assertTrue(any("must not set macro_temp_anomaly" in f for f in res.failures))

    def test_orphan_anomaly_fails(self):
        save = self.parse(save_text(areas=[area_block("a_area", vars=[
            var("dcl_area_class", ident(1)), var("macro_temp_anomaly", ident(8))])]))
        res = self.check(save)
        self.assertTrue(any("without an active dcl_front_type" in f for f in res.failures))


class RosterPredictionTest(CheckTestBase):
    ROSTER = (
        vs.RosterEntry("testopolis", "testhot", "flatland", "farmland", False, False, "test_area"),
        vs.RosterEntry("hillside", "testmild", "hills", "farmland", False, False, "test_area"),
        vs.RosterEntry("southpole", "testmud", "flatland", "grasslands", False, True, "south_area"),
    )
    TABLE = {
        **fake_table("testhot", {m: (40.0, 10.0) for m in range(1, 13)}),   # drought all year
        **fake_table("testmild", {m: (34.0, 10.0) for m in range(1, 13)}),   # drought only at 100 m
        **fake_table("testmud", {12: (5.0, 90.0), 1: (5.0, 90.0),
                                 **{m: (30.0, 10.0) for m in range(2, 12)}}),
    }

    def check(self, save, **kw):
        kw.setdefault("table", self.TABLE)
        kw.setdefault("roster", self.ROSTER)
        kw.setdefault("band_t_map", TIGHT_BANDS)
        return vs.check_reference_predictions(save, **kw)

    def stamped_area(self, key="test_area", vars_=None):
        return area_block(key, vars=vars_ if vars_ is not None else [var("dcl_area_class", ident(1))])

    def test_predicted_modifier_present_passes(self):
        save = self.parse(save_text(
            locations=[cultivated("testopolis", 0.6, extra_mods=[mod("dcl_severe_drought")])],
            areas=[self.stamped_area()]))
        self.assertEqual(self.check(save).failures, [])

    def test_predicted_modifier_absent_fails(self):
        # testhot predicts drought every month, so neither candidate month excuses
        save = self.parse(save_text(
            locations=[cultivated("testopolis", 0.6)],
            areas=[self.stamped_area()]))
        res = self.check(save)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("dcl_severe_drought", res.failures[0])

    def test_impossible_modifier_fails(self):
        save = self.parse(save_text(
            locations=[cultivated("testopolis", 0.6, extra_mods=[mod("dcl_severe_drought"),
                                                                 mod("dcl_mud_season")])],
            areas=[self.stamped_area()]))
        res = self.check(save)
        self.assertTrue(any("dcl_mud_season" in f for f in res.failures))

    def test_elevation_applied(self):
        # testmild at 100 m predicts drought (T 34 > 32); hillside is hills (500 m),
        # so T = 34 - 2.6 = 31.4 and drought must NOT be present
        save = self.parse(save_text(
            locations=[cultivated("hillside", 0.6, extra_mods=[mod("dcl_severe_drought")])],
            areas=[self.stamped_area()]))
        res = self.check(save)
        self.assertTrue(any("dcl_severe_drought" in f for f in res.failures))
        save = self.parse(save_text(
            locations=[cultivated("hillside", 0.6)],
            areas=[self.stamped_area()]))
        self.assertEqual(self.check(save).failures, [])

    def test_southern_hemisphere_month_shift(self):
        # July in the south reads the January row (mud: T 5, H 90)
        save = self.parse(save_text(
            date="1338.7.15",
            locations=[cultivated("southpole", 0.6, extra_mods=[mod("dcl_mud_season")])],
            areas=[self.stamped_area("south_area")]))
        self.assertEqual(self.check(save).failures, [])
        save = self.parse(save_text(
            date="1338.7.15",
            locations=[cultivated("southpole", 0.6)],
            areas=[self.stamped_area("south_area")]))
        res = self.check(save)  # Dec + Jan rows both predict mud -> absence convicts
        self.assertTrue(any("dcl_mud_season" in f for f in res.failures))

    def test_front_adjusts_prediction(self):
        # heatwave on test_area: T 40 + 8 = 48, H 10 - 1.2*8 - 20 = -19.6 -> clamp 5;
        # drought still holds (T 48 > 32, H 5 < 20)
        front = [var("dcl_area_class", ident(1)), var("dcl_front_type", ident(1)),
                 var("macro_temp_anomaly", ident(8)), var("macro_humidity_anomaly", ident(-20))]
        save = self.parse(save_text(
            locations=[cultivated("testopolis", 0.6, extra_mods=[mod("dcl_severe_drought")])],
            areas=[self.stamped_area(vars_=front)]))
        self.assertEqual(self.check(save).failures, [])

    def test_monsoon_flips_drought_expectation(self):
        # monsoon on test_area: H 10 + 40 = 50 -> drought must NOT be present
        front = [var("dcl_area_class", ident(1)), var("dcl_front_type", ident(3)),
                 var("macro_humidity_anomaly", ident(40))]
        save = self.parse(save_text(
            locations=[cultivated("testopolis", 0.6, extra_mods=[mod("dcl_severe_drought")])],
            areas=[self.stamped_area(vars_=front)]))
        res = self.check(save)
        self.assertTrue(any("dcl_severe_drought" in f for f in res.failures))
        save = self.parse(save_text(
            locations=[cultivated("testopolis", 0.6)],
            areas=[self.stamped_area(vars_=front)]))
        self.assertEqual(self.check(save).failures, [])

    def test_unowned_roster_location_skipped(self):
        # record exists but carries no cultivation_ratio -> not owned, no prediction
        save = self.parse(save_text(
            locations=[location_block("testopolis")],
            areas=[self.stamped_area()]))
        res = self.check(save)
        self.assertEqual(res.failures, [])
        self.assertTrue(any("absent/unowned" in n for n in res.notes))


class RealTableTest(CheckTestBase):
    """Wiring against the shipped reference CSV and the vanilla-verified roster."""

    @classmethod
    def setUpClass(cls):
        cls.table = vs.load_reference_table()

    def test_table_shape(self):
        self.assertEqual(len(self.table), 192)  # 8 climates x 12 months x 2 coastal

    def check(self, save):
        return vs.check_reference_predictions(save, table=self.table, band_t_map=TIGHT_BANDS)

    def july_timbuktu(self, extra_mods=(), front_vars=None):
        area_vars = front_vars if front_vars is not None else [var("dcl_area_class", ident(1))]
        return self.parse(save_text(
            date="1338.7.15",
            locations=[cultivated("timbuktu", 0.05, extra_mods=extra_mods)],
            areas=[area_block("timbuktu_area", vars=area_vars)]))

    def test_sahel_july_drought_predicted(self):
        # arid July inland: T_mid 33.27, H 11.5 -> drought (spec 9.5 example class)
        save = self.july_timbuktu(extra_mods=[mod("dcl_severe_drought")])
        self.assertEqual(self.check(save).failures, [])

    def test_sahel_july_mud_convicted(self):
        save = self.july_timbuktu(extra_mods=[mod("dcl_severe_drought"), mod("dcl_mud_season")])
        res = self.check(save)
        self.assertTrue(any("dcl_mud_season" in f for f in res.failures))

    def test_sahel_july_absent_drought_excused_by_pulse_timing(self):
        # June's row is inside the uncertainty band of the drought threshold, so a
        # save that may precede the July pulse cannot be convicted for lacking it
        save = self.july_timbuktu()
        self.assertEqual(self.check(save).failures, [])

    def test_sahel_july_monsoon_front_clears_drought(self):
        front = [var("dcl_area_class", ident(1)), var("dcl_front_type", ident(3)),
                 var("macro_humidity_anomaly", ident(40))]
        save = self.july_timbuktu(extra_mods=[mod("dcl_severe_drought")], front_vars=front)
        res = self.check(save)
        self.assertTrue(any("dcl_severe_drought" in f for f in res.failures))
        save = self.july_timbuktu(front_vars=front)
        self.assertEqual(self.check(save).failures, [])


class CliTest(CheckTestBase):
    def setUp(self):
        super().setUp()
        # keep the CLI off the fitter import path: empty band map -> fallback band
        self._bands = vs._BAND_T_CACHE
        vs._BAND_T_CACHE = {}

    def tearDown(self):
        vs._BAND_T_CACHE = self._bands

    def clean_save_text(self):
        return save_text(
            date="1338.7.15",
            locations=[cultivated("timbuktu", 0.05, extra_mods=[mod("dcl_severe_drought")]),
                       cultivated("paris", 0.6)],
            areas=[area_block("timbuktu_area", vars=[var("dcl_area_class", ident(1))]),
                   area_block("ile_de_france_area", vars=[var("dcl_area_class", ident(2))])])

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = vs.main(argv)
        return code, out.getvalue()

    def test_clean_save_exit_zero(self):
        path = write_save(self.dir, "clean.eu5", self.clean_save_text())
        code, out = self.run_cli([path])
        self.assertEqual(code, 0, out)
        self.assertIn("ALL CHECKS PASSED", out)

    def test_dirty_save_exit_one(self):
        dirty = save_text(locations=[cultivated("paris", 0.6, extra_vars=[var("dcl_stray", ident(1))])],
                          areas=[area_block("a_area", vars=[var("dcl_area_class", ident(2))])])
        path = write_save(self.dir, "dirty.eu5", dirty)
        code, out = self.run_cli([path])
        self.assertEqual(code, 1)
        self.assertIn("FAIL no_foreign_location_vars", out)

    def test_missing_file_exit_one(self):
        code, out = self.run_cli([os.path.join(self.dir, "nope.eu5")])
        self.assertEqual(code, 1)
        self.assertIn("not found", out)

    def test_two_saves_run_drift(self):
        a = write_save(self.dir, "a.eu5", save_text(
            date="1338.4.1", locations=[cultivated("paris", 0.6)],
            areas=[area_block("ile_de_france_area", vars=[var("dcl_area_class", ident(2))])]))
        b = write_save(self.dir, "b.eu5", save_text(
            date="1339.4.1", locations=[cultivated("paris", 0.62)],
            areas=[area_block("ile_de_france_area", vars=[var("dcl_area_class", ident(2))])]))
        code, out = self.run_cli([a, b])
        self.assertEqual(code, 0, out)
        self.assertIn("drift across saves", out)


if __name__ == "__main__":
    unittest.main()


class RobustnessTest(CheckTestBase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = vs.main(argv)
        return code, out.getvalue()

    def test_year_boundary_pair_allows_two_pulses(self):
        # exactly 365 days between pulse-aligned saves can hold two yearly pulses
        a = self.parse(save_text(date="1338.1.1", locations=[cultivated("paris", 0.60)]))
        b = self.parse(save_text(date="1339.1.1", locations=[cultivated("paris", 0.46)]))
        self.assertEqual(vs.check_drift([a, b]).failures, [])
        # but -0.15 still exceeds the two-pulse bound
        b = self.parse(save_text(date="1339.1.1", locations=[cultivated("paris", 0.45)]))
        self.assertEqual(len(vs.check_drift([a, b]).failures), 1)

    def test_malformed_size_token_does_not_crash(self):
        save = self.parse(save_text(locations=[location_block(
            "paris", vars=[var("cultivation_ratio", ident(0.6))],
            mods=["{ modifier=dcl_cultivated_land start_date=1337.1.1 size=1.2.3 }"])]))
        # entry dropped rather than crashing the parser
        self.assertNotIn("dcl_cultivated_land", save.records["paris"].modifiers)

    def test_compressed_save_rejected(self):
        path = write_save(self.dir, "zip.eu5", "PK\x03\x04 binary noise\n")
        code, out = self.run_cli([path])
        self.assertEqual(code, 1)
        self.assertIn("compressed save", out)

    def test_missing_reference_table_clean_exit(self):
        path = write_save(self.dir, "clean.eu5", save_text())
        code, out = self.run_cli([path, "--reference-table",
                                  os.path.join(self.dir, "nope.csv")])
        self.assertEqual(code, 1)
        self.assertIn("reference table", out)
