"""Savegame verifier for Dynamic Climate & Ecological Land Use (spec 9.5).

Usage: python tools/dcl_verify_save.py <save.eu5> [more saves...]
           [--reference-table PATH] [--strict-month] [--max-failures N]

Exit code 1 when any check prints FAIL, else 0. With several saves, consecutive
pairs (by save date) also feed the year-over-year drift check.

Save-format facts relied on (AGENTS.md 4.5/4.8, verified on ecology-project saves):
  * plain text; line 1 is a magic header, then ``metadata={ date=Y.M.D ... }``.
  * variables: ``{ flag=name data={ type=value identity=N } }`` with
    N = value * 100000; a variable at 0 OMITS ``identity`` (entry present, value
    absent => 0). Entries may be single-line or span several lines.
  * timed modifiers: ``{ modifier=X start_date=... size=F }``; size 0 OMITS
    ``size=`` (entry present => size 0).
  * locations and areas are keyed by name; the exact container block names are
    not relied on: records are classified by CONTENT (a record holding
    ``cultivation_ratio`` is a cultivated location; a record holding
    ``dcl_area_class``/``dcl_front_type``/``macro_*`` is an area).

Check-to-proxy binding (temperature/humidity are lazy and NOT persisted, by design):
  ratio_bounds            -> location variable ``cultivation_ratio`` (identity/1e5);
                             terrain cap via vanilla map-data roster / save fields.
  no_foreign_location_vars -> any other dcl_*/macro_*/eco_* variable on a record
                             that carries the mod's location state.
  front_count             -> area variables ``dcl_front_type`` / ``dcl_area_class``.
  land_sizes              -> timed modifiers ``dcl_cultivated_land`` /
                             ``dcl_wilderness_land`` sizes vs the ratio.
  drift                   -> ``cultivation_ratio`` deltas across consecutive saves.
  reference_predictions   -> (a) front anomaly variables vs the spec 5 table;
                             (b) presence/absence of the five condition modifiers
                             (dcl_mud_season, dcl_tropical_rot, dcl_severe_drought,
                             dcl_heat_exhaustion, dcl_lethal_heat) on roster
                             locations vs the reference table's per-month T/H/Tw.
"""

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path

VAR_SCALE = 100000.0

RATIO_MIN = 0.05
RATIO_MAX = 0.95
RATIO_EPS = 2e-5          # fixed-point storage quantum is 1e-5

SIZE_TOL = 5e-4           # modifier size vs ratio tolerance

DRIFT_DOWN_PER_YEAR = 0.07   # spec 7.4: -0.03 stress and -0.04 farmer fill stack
DRIFT_UP_PER_YEAR = 0.02     # spec 7.4: growth term
DRIFT_TOL = 5e-4

# A front lives 30-60 days (spec 5). Monthly pulses are ~30.4 days apart, so at
# most three monthly cohorts can be alive at once (a 60-day front rolled just
# before a short month pair overlaps two later pulses; four cohorts would need
# > 89 days). Cohorts never share an area (the roll requires NOT has_variable =
# dcl_front_type), so active fronts <= 3 * 5 = 15. Spec 9.5's "3-5 per month-save"
# is the common case; 0 is legal (no eligible area, or all expired).
FRONT_MAX_ACTIVE = 15
FRONT_TABLE = {1: (8.0, -20.0), 2: (-12.0, -10.0), 3: (None, 40.0)}  # spec 5
AREA_VAR_NAMES = frozenset({
    "dcl_area_class", "dcl_front_type", "macro_temp_anomaly", "macro_humidity_anomaly",
})
VALID_FRONT_TYPES = frozenset(FRONT_TABLE)
VALID_AREA_CLASSES = frozenset({1, 2, 3})

LAND_MODIFIERS = frozenset({"dcl_cultivated_land", "dcl_wilderness_land"})
CONDITION_MODIFIERS = (
    "dcl_mud_season", "dcl_tropical_rot", "dcl_severe_drought",
    "dcl_heat_exhaustion", "dcl_lethal_heat",
)
DCL_MODIFIERS = frozenset(set(LAND_MODIFIERS) | set(CONDITION_MODIFIERS) | {
    "dcl_climate_stress_memory",
    "dcl_heat_exhaustion_army", "dcl_lethal_heat_army", "dcl_expeditionary_rot",
    "dcl_mud_army", "dcl_rot_army",
})

FOREIGN_VAR_RE = re.compile(r"^(?:dcl_|macro_|eco_)")

# Elevation proxy per topography (spec 4.2); the reference table is computed at
# 100 m. H is elevation-invariant (T - T_mean shifts identically), so only T is
# adjusted. Mirrors dcl_land_use_values.txt / dcl_climate_math_values.txt.
ELEVATION = {"wetlands": 50, "flatland": 100, "hills": 500, "plateau": 1500, "mountains": 2500}
ELEVATION_REF = 100
LAPSE_RATE = 0.0065
HUMIDITY_SLOPE = 1.2
HUMIDITY_MIN = 5.0
HUMIDITY_MAX = 100.0

FALLBACK_BAND_T = 6.0   # worst within-month |T(day) - T(day 15)| is 5.89 (arctic June)


@dataclass(frozen=True)
class RosterEntry:
    """A vanilla-map-verified location used for terrain-cap and modifier checks."""
    key: str
    climate: str
    topography: str
    vegetation: str
    coastal: bool
    southern: bool
    area: str


# Verified against vanilla in_game/map_data/location_templates.txt (climate /
# topography / vegetation / natural_harbor_suitability) and definitions.txt
# (province -> area membership), game version 1.3.11.
ROSTER = (
    RosterEntry("stockholm", "continental", "flatland", "grasslands", True, False, "svealand_area"),
    RosterEntry("reikjavik", "arctic", "hills", "grasslands", True, False, "iceland_area"),
    RosterEntry("paris", "oceanic", "flatland", "farmland", False, False, "ile_de_france_area"),
    RosterEntry("novgorod", "continental", "flatland", "farmland", False, False, "east_novgorod_area"),
    RosterEntry("samarkand", "cold_arid", "plateau", "farmland", False, False, "transoxiana_area"),
    RosterEntry("hangzhou", "subtropical", "flatland", "grasslands", True, False, "zhejiang_area"),
    RosterEntry("cairo", "arid", "flatland", "farmland", False, False, "lower_egypt_area"),
    RosterEntry("timbuktu", "arid", "flatland", "desert", False, False, "timbuktu_area"),
    RosterEntry("qusqu", "arctic", "mountains", "sparse", False, True, "acamama_area"),
)


def max_arable(topography, vegetation):
    """Mirror of dcl_max_arable (spec 7.2), same first-match-wins precedence."""
    if topography == "wetlands":
        return 0.40
    if vegetation in ("desert", "sparse"):
        return 0.20
    if topography == "hills":
        return 0.60
    if topography == "mountains":
        return 0.35
    if topography == "plateau":
        return 0.50
    if vegetation == "jungle":
        return 0.55
    if vegetation in ("woods", "forest"):
        return 0.70
    if vegetation in ("farmland", "grasslands"):
        return 0.95
    return 0.70


# ----------------------------------------------------------------------------- parser

@dataclass
class SaveRecord:
    key: str
    variables: dict = field(default_factory=dict)   # name -> float
    modifiers: dict = field(default_factory=dict)   # dcl_* name -> size float
    fields: dict = field(default_factory=dict)      # opportunistic: climate, topography, ...


@dataclass
class SaveData:
    path: str
    date: tuple = None          # (year, month, day)
    records: dict = field(default_factory=dict)   # block name -> SaveRecord
    lines: int = 0


# Structural containers, never game objects: variable/modifier payloads nest the
# actual record one level up. Numeric keys (province/unit ids) are valid records.
IGNORE_BLOCK_NAMES = frozenset({
    "variables", "variable", "timed_modifiers", "data", "metadata", "database",
    "modifiers", "modifier",
})

TOKEN_RE = re.compile(r"""
    (?P<block>(?<![\w.])(?P<bname>[A-Za-z_]\w*|\d+)\s*=\s*\{)
  | (?P<open>\{)
  | (?P<close>\})
  | (?P<flag>(?<![\w.])flag\s*=\s*(?P<fval>[A-Za-z_]\w*))
  | (?P<identity>(?<![\w.])identity\s*=\s*(?P<ival>-?\d+))
  | (?P<modifier>(?<![\w.])modifier\s*=\s*(?P<mval>[A-Za-z_]\w*))
  | (?P<size>(?<![\w.])size\s*=\s*(?P<sval>-?[\d.]+))
  | (?P<date>(?<![\w.])date\s*=\s*(?P<dy>\d+)\.(?P<dm>\d+)\.(?P<dd>\d+))
  | (?P<fhit>(?<![\w.])(?P<fname>climate|topography|vegetation|area|province_definition)\s*=\s*"?(?P<fval2>\w+)"?)
""", re.VERBOSE)

# Superset pre-filter: any line the tokenizer could care about contains one of
# these patterns. Bare "key=value" scalar lines skip the tokenizer entirely.
FAST_RE = re.compile(
    r"[{}]|flag\s*=|identity\s*=|modifier\s*=|size\s*=|date\s*=|climate\s*="
    r"|topography\s*=|vegetation\s*=|area\s*=|province_definition\s*=")


def parse_save(path):
    """Single streaming pass over a plain-text .eu5 save. Never holds the file."""
    save = SaveData(path=str(path))
    stack = []           # block names (None = anonymous '{')
    pending_var = None   # [name, record_key, seen_data_block]
    pending_mod = None   # [name, record_key]

    def record_key():
        for name in reversed(stack):
            if name is not None and name not in IGNORE_BLOCK_NAMES:
                return name
        return None

    def get_record(key):
        rec = save.records.get(key)
        if rec is None:
            rec = save.records[key] = SaveRecord(key=key)
        return rec

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            save.lines += 1
            if "=" not in line:
                # brace bookkeeping only; a lone '}' may close a pending entry
                for ch in line:
                    if ch == "{":
                        stack.append(None)
                    elif ch == "}":
                        if pending_var is not None:
                            name, key, seen = pending_var
                            if seen:
                                commit_var(save, get_record, key, name, 0.0)
                            pending_var = None
                        if pending_mod is not None:
                            commit_mod(get_record, pending_mod[1], pending_mod[0], 0.0)
                            pending_mod = None
                        if stack:
                            stack.pop()
                continue
            if not FAST_RE.search(line):
                continue
            for m in TOKEN_RE.finditer(line):
                kind = m.lastgroup
                if kind == "block":
                    name = m.group("bname")
                    if pending_var is not None and name == "data":
                        pending_var[2] = True
                    stack.append(name)
                elif kind == "open":
                    stack.append(None)
                elif kind == "close":
                    if pending_var is not None:
                        name, key, seen = pending_var
                        if seen:
                            commit_var(save, get_record, key, name, 0.0)
                        pending_var = None
                    if pending_mod is not None:
                        commit_mod(get_record, pending_mod[1], pending_mod[0], 0.0)
                        pending_mod = None
                    if stack:
                        stack.pop()
                elif kind == "flag":
                    if pending_var is not None:  # previous entry had no identity
                        name, key, seen = pending_var
                        if seen:
                            commit_var(save, get_record, key, name, 0.0)
                    pending_var = [m.group("fval"), record_key(), False]
                elif kind == "identity":
                    if pending_var is not None:
                        name, key, _seen = pending_var
                        commit_var(save, get_record, key, name, int(m.group("ival")) / VAR_SCALE)
                        pending_var = None
                elif kind == "modifier":
                    if pending_mod is not None:  # previous entry had no size
                        commit_mod(get_record, pending_mod[1], pending_mod[0], 0.0)
                    pending_mod = [m.group("mval"), record_key()]
                elif kind == "size":
                    if pending_mod is not None:
                        try:
                            size_value = float(m.group("sval"))
                        except ValueError:  # pathological token; drop the entry
                            pending_mod = None
                            continue
                        commit_mod(get_record, pending_mod[1], pending_mod[0], size_value)
                        pending_mod = None
                elif kind == "date":
                    if save.date is None:
                        save.date = (int(m.group("dy")), int(m.group("dm")), int(m.group("dd")))
                elif kind == "fhit":
                    key = record_key()
                    if key is not None:
                        get_record(key).fields.setdefault(m.group("fname"), m.group("fval2"))
    return save


def commit_var(save, get_record, key, name, value):
    if key is not None:
        get_record(key).variables[name] = value


def commit_mod(get_record, key, name, size):
    if key is not None and name in DCL_MODIFIERS:
        get_record(key).modifiers[name] = size


# ----------------------------------------------------------------- parse views (brief API)

def parse_locations_vars(save):
    """{record key: variables} for records carrying the mod's location variable."""
    return {k: dict(r.variables) for k, r in save.records.items()
            if "cultivation_ratio" in r.variables}


def parse_area_vars(save):
    """{record key: variables} for records carrying any of the mod's area variables."""
    return {k: dict(r.variables) for k, r in save.records.items()
            if AREA_VAR_NAMES & r.variables.keys()}


def parse_modifiers(save):
    """{record key: {dcl_* modifier: size}} for records with mod timed modifiers."""
    return {k: dict(r.modifiers) for k, r in save.records.items() if r.modifiers}


# ----------------------------------------------------------------------------- checks

@dataclass
class CheckResult:
    name: str
    failures: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.failures


def check_ratio_bounds(save, roster=ROSTER):
    """cultivation_ratio in [0.05, 0.95] (hard clamp) and <= terrain cap where known.

    Terrain comes from the save record's own topography/vegetation fields when
    present, else from the vanilla-verified roster. The cap is dcl_max_arable;
    init never exceeds it and drift caps growth at it, so a violation is a bug.
    """
    res = CheckResult("ratio_bounds")
    locs = parse_locations_vars(save)
    terrain = {e.key: e for e in roster}
    capped = 0
    for key, vs in sorted(locs.items()):
        v = vs["cultivation_ratio"]
        if v < RATIO_MIN - RATIO_EPS or v > RATIO_MAX + RATIO_EPS:
            res.failures.append(f"{key}: cultivation_ratio {v:.5f} outside [{RATIO_MIN}, {RATIO_MAX}]")
        rec = save.records[key]
        topo = rec.fields.get("topography")
        veg = rec.fields.get("vegetation")
        if (topo is None or veg is None) and key in terrain:
            topo, veg = terrain[key].topography, terrain[key].vegetation
        if topo is not None and veg is not None:
            cap = max_arable(topo, veg)
            capped += 1
            if v > cap + RATIO_EPS:
                res.failures.append(
                    f"{key}: cultivation_ratio {v:.5f} above terrain cap {cap:.2f} ({topo}/{veg})")
    res.notes.append(f"{len(locs)} locations bounds-checked, {capped} cap-checked")
    return res


def check_no_foreign_location_vars(save):
    """No dcl_*/macro_*/eco_* variable on a record holding mod location state.

    cultivation_ratio is the mod's ONLY location variable (spec 1.1); macro_* and
    dcl_front_type/dcl_area_class belong to area records; eco_* would be
    contamination from the predecessor mod this one replaces.
    """
    res = CheckResult("no_foreign_location_vars")
    loc_keys = set(parse_locations_vars(save))
    loc_keys |= {k for k, r in save.records.items() if LAND_MODIFIERS & r.modifiers.keys()}
    flagged = 0
    for key in sorted(loc_keys):
        bad = sorted(n for n in save.records[key].variables if FOREIGN_VAR_RE.match(n))
        if bad:
            flagged += 1
            res.failures.append(f"{key}: foreign location variable(s): {', '.join(bad)}")
    res.notes.append(f"{len(loc_keys)} location records scanned, {flagged} contaminated")
    return res


def check_front_count(save):
    """Front census: <= 15 active fronts (3 overlapping cohorts x 5), types 1-3.

    Also validates dcl_area_class in {1,2,3}, that front areas are stamped, and
    that a save with cultivated locations has at least one stamped area
    (on_game_start stamps every land area; zero means the stamp effect broke).
    """
    res = CheckResult("front_count")
    areas = parse_area_vars(save)
    fronts = {k: v for k, v in areas.items() if "dcl_front_type" in v}
    if len(fronts) > FRONT_MAX_ACTIVE:
        res.failures.append(
            f"{len(fronts)} areas with dcl_front_type exceed the {FRONT_MAX_ACTIVE}-front bound")
    for key, vs in sorted(fronts.items()):
        ft = int(round(vs["dcl_front_type"]))
        if ft not in VALID_FRONT_TYPES:
            res.failures.append(f"{key}: dcl_front_type {vs['dcl_front_type']:.5f} not in {{1,2,3}}")
        if "dcl_area_class" not in vs:
            res.failures.append(f"{key}: active front on an area without dcl_area_class")
    stamped = 0
    for key, vs in sorted(areas.items()):
        if "dcl_area_class" in vs:
            ac = int(round(vs["dcl_area_class"]))
            if ac not in VALID_AREA_CLASSES:
                res.failures.append(f"{key}: dcl_area_class {vs['dcl_area_class']:.5f} not in {{1,2,3}}")
            else:
                stamped += 1
    if parse_locations_vars(save) and stamped == 0 and not areas:
        res.failures.append("locations are cultivated but no area carries dcl_area_class "
                            "(dcl_stamp_area_class never ran?)")
    res.notes.append(f"{len(fronts)} active fronts, {stamped} stamped areas")
    return res


def check_land_sizes(save):
    """dcl_cultivated_land size == ratio, dcl_wilderness_land size == 1 - ratio.

    Both modifiers are permanent size-scaled (spec 7.5) and re-applied after every
    ratio change, so sizes must track the stored ratio. ratio >= 0.05 guarantees
    neither size is 0, so an omitted-size (0.0) entry is always a mismatch.
    """
    res = CheckResult("land_sizes")
    checked = 0
    for key, rec in sorted(save.records.items()):
        has_ratio = "cultivation_ratio" in rec.variables
        has_land = bool(LAND_MODIFIERS & rec.modifiers.keys())
        if has_ratio and not has_land:
            res.failures.append(f"{key}: cultivation_ratio present but land modifiers missing")
            continue
        if has_land and not has_ratio:
            res.failures.append(f"{key}: land modifier(s) present without cultivation_ratio")
            continue
        if not has_ratio:
            continue
        checked += 1
        ratio = rec.variables["cultivation_ratio"]
        cult = rec.modifiers.get("dcl_cultivated_land")
        wild = rec.modifiers.get("dcl_wilderness_land")
        if cult is None:
            res.failures.append(f"{key}: dcl_cultivated_land missing (ratio {ratio:.5f})")
        elif abs(cult - ratio) > SIZE_TOL:
            res.failures.append(
                f"{key}: dcl_cultivated_land size {cult:.5f} != ratio {ratio:.5f}")
        if wild is None:
            res.failures.append(f"{key}: dcl_wilderness_land missing (ratio {ratio:.5f})")
        elif abs(wild - (1.0 - ratio)) > SIZE_TOL:
            res.failures.append(
                f"{key}: dcl_wilderness_land size {wild:.5f} != 1 - ratio ({1.0 - ratio:.5f})")
    res.notes.append(f"{checked} locations size-checked")
    return res


def check_drift(saves):
    """Year-over-year ratio deltas within drift semantics (spec 7.4).

    Per yearly pulse a location moves at most -0.07 (stress + low farmer fill,
    stacked) or +0.02 (full fields, food surplus, no stress). The pulse count
    between two saves is bounded by floor(days/365.25)+1 (may overestimate, which
    only loosens the bound). Locations present in only one save are skipped
    (conquest/colonisation init is not drift). Directional assertions against
    stress memory are NOT made: the stress modifier may have been applied after
    the yearly pulse ran, so its presence cannot convict a positive delta.
    """
    res = CheckResult("drift")
    dated = sorted((s for s in saves if s.date), key=lambda s: s.date)
    if len(dated) < 2:
        res.notes.append("skipped: fewer than two dated saves")
        return res
    for older, newer in zip(dated, dated[1:]):
        days = (date_cls(*newer.date) - date_cls(*older.date)).days
        if days <= 0:
            res.notes.append(f"{Path(newer.path).name}: same-date pair skipped")
            continue
        pulses = max(1, int(math.floor(days / 365.0)) + 1)
        up = DRIFT_UP_PER_YEAR * pulses + DRIFT_TOL
        down = -DRIFT_DOWN_PER_YEAR * pulses - DRIFT_TOL
        la, lb = parse_locations_vars(older), parse_locations_vars(newer)
        compared = 0
        for key in sorted(la.keys() & lb.keys()):
            delta = lb[key]["cultivation_ratio"] - la[key]["cultivation_ratio"]
            compared += 1
            if delta > up or delta < down:
                res.failures.append(
                    f"{key}: drift {delta:+.5f} between {older.date} and {newer.date} "
                    f"outside [{down:.3f}, {up:.3f}] ({days}d, <= {pulses} pulse(s))")
        res.notes.append(
            f"{Path(older.path).name} -> {Path(newer.path).name}: {compared} locations compared")
    return res


# ------------------------------------------------------- reference predictions

def load_reference_table(path=None):
    """tools/dcl_reference_table.csv -> {(climate, month, coastal_int): row dict}.

    Tw is None where the CSV leaves it empty (monthly-mean T < 20)."""
    if path is None:
        path = Path(__file__).with_name("dcl_reference_table.csv")
    table = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["climate"], int(row["month"]), int(row["coastal"]))
            table[key] = {
                "T": float(row["T"]),
                "T_mid": float(row["T_mid"]),
                "H": float(row["H"]),
                "Tw": float(row["Tw"]) if row["Tw"] not in ("", None) else None,
                "E_s": float(row["E_s"]) if row["E_s"] not in ("", None) else None,
            }
    return table


def default_band_t():
    """Per-cell within-month T uncertainty: max_d |T(day d) - T(day 15)| + slack.

    Computed with the fitter's own model (same arithmetic that emitted the CSV);
    falls back to the global worst case 6.0 C when the fitter is unavailable.
    """
    global _BAND_T_CACHE
    if _BAND_T_CACHE is None:
        bands = {}
        try:
            import dcl_fit_coefficients as fit
            for cl in fit.CLIMATES:
                for coastal in (False, True):
                    for m in range(12):
                        t15 = fit.model_temperature(cl, fit.MONTH_OFFSETS[m] + fit.MID_MONTH_DAY,
                                                    coastal, False, 100.0)
                        spread = max(
                            abs(fit.model_temperature(cl, fit.MONTH_OFFSETS[m] + d,
                                                      coastal, False, 100.0) - t15)
                            for d in range(1, fit.MONTH_DAYS[m] + 1))
                        bands[(cl, m + 1, int(coastal))] = spread + 0.25
        except Exception:
            bands = {}
        _BAND_T_CACHE = bands
    return _BAND_T_CACHE


_BAND_T_CACHE = None


def _band_gt(x, thr, band):
    if x > thr + band:
        return True
    if x <= thr - band:
        return False
    return None


def _band_lt(x, thr, band):
    if x < thr - band:
        return True
    if x >= thr + band:
        return False
    return None


def _band_range(x, lo, hi, band):
    if lo + band <= x <= hi - band:
        return True
    if x < lo - band or x > hi + band:
        return False
    return None


def _band_and(a, b):
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def predict_conditions(entry, row, temp_anom, hum_anom, front_active, band_t):
    """Expected condition modifiers for a roster entry, True/False/None each.

    T/H come from the reference table's T_mid/H adjusted for the entry's
    elevation and the scenario's front anomalies; Tw only without a front (the
    surrogate is not re-fitted here, so heat assertions are skipped mid-front).
    None = indeterminate (prediction inside the uncertainty band of a threshold).
    """
    band_h = HUMIDITY_SLOPE * band_t + 0.2
    band_tw = band_t + 0.5
    elev = ELEVATION.get(entry.topography, ELEVATION_REF)
    t = row["T_mid"] - (elev - ELEVATION_REF) * LAPSE_RATE + temp_anom
    h = min(HUMIDITY_MAX, max(HUMIDITY_MIN,
                              row["H"] - HUMIDITY_SLOPE * temp_anom + hum_anom))
    out = {}
    if entry.climate == "arctic" or entry.topography == "mountains":
        out["dcl_mud_season"] = False   # excluded by dcl_mud_conditions
    else:
        out["dcl_mud_season"] = _band_and(_band_range(t, 1.0, 8.0, band_t),
                                          _band_gt(h, 80.0, band_h))
    out["dcl_tropical_rot"] = _band_and(_band_gt(t, 30.0, band_t), _band_gt(h, 85.0, band_h))
    out["dcl_severe_drought"] = _band_and(_band_gt(t, 32.0, band_t), _band_lt(h, 20.0, band_h))
    tw = None if front_active else row["Tw"]
    if tw is None:
        if front_active:
            out["dcl_heat_exhaustion"] = out["dcl_lethal_heat"] = None
        elif t >= 31.0 - band_t:
            out["dcl_heat_exhaustion"] = out["dcl_lethal_heat"] = None
        else:
            out["dcl_heat_exhaustion"] = out["dcl_lethal_heat"] = False
    else:
        out["dcl_heat_exhaustion"] = _band_range(tw, 31.0, 35.0, band_tw)
        out["dcl_lethal_heat"] = _band_ge(tw, 35.0, band_tw)
    return out


def _band_ge(x, lo, band):
    if x >= lo + band:
        return True
    if x < lo - band:
        return False
    return None


def front_scenarios(save, entry, rec=None):
    """[(temp_anom, hum_anom, front_active), ...] to test a roster entry against.

    Area record found, no front: the single clean scenario. Found with a front:
    that front's anomaly pair (Tw assertions are skipped by front_active). Not
    found: the clean scenario plus every possible front (a finding needs every
    scenario in every candidate month to disagree, so unknown areas can excuse
    but never convict). The area record is looked up by the roster area name,
    then by the location record's own ``area`` field when the save carries one.
    """
    area_key = entry.area or None
    area_rec = save.records.get(area_key) if area_key else None
    if area_rec is None and rec is not None:
        field_area = rec.fields.get("area")
        if field_area and field_area != area_key:
            area_rec = save.records.get(field_area)
    if area_rec is None:
        scenarios = [(0.0, 0.0, False)]
        scenarios += [(ta or 0.0, ha or 0.0, True) for ta, ha in FRONT_TABLE.values()]
        return scenarios, "area record not found (all front scenarios admitted)"
    ft = area_rec.variables.get("dcl_front_type")
    if ft is None:
        return [(0.0, 0.0, False)], "no active front"
    spec = FRONT_TABLE.get(int(round(ft)))
    if spec is None:
        return [(0.0, 0.0, False)], f"invalid dcl_front_type {ft:.5f} (see front_count)"
    return [(spec[0] or 0.0, spec[1] or 0.0, True)], f"front type {int(round(ft))} active"


def check_reference_predictions(save, table=None, roster=ROSTER, band_t_map=None,
                                strict_month=False):
    """(a) front anomaly variables vs the spec 5 table; (b) roster modifiers.

    (a) is exact: dcl_front_type 1 => temp +8 / humidity -20, 2 => -12 / -10,
    3 => humidity +40 and NO temperature anomaly; anomaly variables without an
    active front are contamination.

    (b) binds to the presence/absence of the five condition timed modifiers on
    roster locations vs the reference table for the save month. A save validates
    if ANY (candidate month x front scenario) explains the observed modifiers;
    candidates are the save month plus the previous month (unless --strict-month)
    because the save may precede that month's pulse. Assertions inside the
    uncertainty band of a threshold are indeterminate and never convict.
    """
    res = CheckResult("reference_predictions")
    if table is None:
        table = load_reference_table()
    res.notes.append(f"reference table: {len(table)} rows")

    # (a) front anomaly table (exact, no uncertainty)
    areas = parse_area_vars(save)
    for key, vs in sorted(areas.items()):
        ft = vs.get("dcl_front_type")
        if ft is None:
            if "macro_temp_anomaly" in vs or "macro_humidity_anomaly" in vs:
                res.failures.append(
                    f"{key}: macro_* anomaly present without an active dcl_front_type")
            continue
        spec = FRONT_TABLE.get(int(round(ft)))
        if spec is None:
            continue   # invalid type already reported by front_count
        ta, ha = spec
        temp = vs.get("macro_temp_anomaly")
        if ta is None:
            if temp is not None:
                res.failures.append(
                    f"{key}: front type 3 must not set macro_temp_anomaly (found {temp:.5f})")
        elif temp is None or abs(temp - ta) > 0.5:
            res.failures.append(
                f"{key}: front type {int(round(ft))} expects macro_temp_anomaly {ta:+.0f}, "
                f"found {'absent' if temp is None else f'{temp:.5f}'}")
        hum = vs.get("macro_humidity_anomaly")
        if hum is None or abs(hum - ha) > 0.5:
            res.failures.append(
                f"{key}: front type {int(round(ft))} expects macro_humidity_anomaly {ha:+.0f}, "
                f"found {'absent' if hum is None else f'{hum:.5f}'}")
    res.notes.append(f"front anomaly table checked on {len(areas)} area records")

    # (b) roster location modifier prediction
    if not save.date:
        res.notes.append("roster prediction skipped: no save date")
        return res
    if band_t_map is None:
        band_t_map = default_band_t()
    month = save.date[1]
    months = [month] if strict_month else [month, 12 if month == 1 else month - 1]
    found = skipped = 0
    for entry in roster:
        rec = save.records.get(entry.key)
        if rec is None or "cultivation_ratio" not in rec.variables:
            skipped += 1
            continue
        found += 1
        actual = set(CONDITION_MODIFIERS) & rec.modifiers.keys()
        scenarios, why = front_scenarios(save, entry, rec)
        verdicts = []
        for m in months:
            nm = ((m + 5) % 12) + 1 if entry.southern else m
            row = table.get((entry.climate, nm, int(entry.coastal)))
            if row is None:
                continue
            band_t = band_t_map.get((entry.climate, nm, int(entry.coastal)), FALLBACK_BAND_T) \
                if isinstance(band_t_map, dict) else band_t_map(entry.climate, nm, int(entry.coastal))
            for ta, ha, active in scenarios:
                pred = predict_conditions(entry, row, ta, ha, active, band_t)
                bad = [mod for mod in CONDITION_MODIFIERS
                       if pred[mod] is not None and (mod in actual) != pred[mod]]
                verdicts.append((m, ta, ha, bad))
        if verdicts and all(bad for _m, _ta, _ha, bad in verdicts):
            m, ta, ha, bad = min(verdicts, key=lambda v: len(v[3]))
            res.failures.append(
                f"{entry.key}: modifiers {sorted(actual) or '[]'} contradict prediction for "
                f"{entry.climate} month {m} ({why}): {', '.join(sorted(bad))}")
    res.notes.append(f"roster: {found} locations predicted, {skipped} absent/unowned")
    return res


# ----------------------------------------------------------------------------- CLI

def run_checks(save, table, args):
    return [
        check_ratio_bounds(save),
        check_no_foreign_location_vars(save),
        check_front_count(save),
        check_land_sizes(save),
        check_reference_predictions(save, table=table, strict_month=args.strict_month),
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("saves", nargs="+", help="plain-text .eu5 save(s)")
    ap.add_argument("--reference-table", default=None,
                    help="path to dcl_reference_table.csv (default: alongside this script)")
    ap.add_argument("--strict-month", action="store_true",
                    help="assert the save month only (use for saves taken after the monthly pulse)")
    ap.add_argument("--max-failures", type=int, default=25,
                    help="cap printed failure lines per check (default 25)")
    args = ap.parse_args(argv)

    try:
        table = load_reference_table(args.reference_table)
    except (OSError, ValueError) as exc:
        print(f"FAIL parse: reference table: {exc}")
        return 1
    saves, all_results = [], []
    for raw in args.saves:
        path = Path(raw)
        if not path.is_file():
            print(f"FAIL parse: {path} not found")
            all_results.append(CheckResult("parse", failures=[f"{path} not found"]))
            continue
        with open(path, "rb") as fh:
            magic = fh.read(2)
        if magic in (b"PK", b"\x1f\x8b"):
            print(f"FAIL parse: {path} is a compressed save; re-save as plain text")
            all_results.append(CheckResult("parse", failures=[f"{path} compressed"]))
            continue
        save = parse_save(path)
        saves.append(save)
        locs = len(parse_locations_vars(save))
        areas = len(parse_area_vars(save))
        mods = sum(len(m) for m in parse_modifiers(save).values())
        stamp = ".".join(str(p) for p in save.date) if save.date else "no date"
        print(f"== {path.name} (date {stamp}; {save.lines} lines; "
              f"{locs} cultivated locations, {areas} area records, {mods} dcl modifiers) ==")
        if not locs and not areas and not mods:
            print("   NOTE: no dcl state in this save (mod not active in this campaign?)")
        for res in run_checks(save, table, args):
            all_results.append(res)
            _print_result(res, args.max_failures)

    if len(saves) >= 2:
        res = check_drift(saves)
        all_results.append(res)
        print("== drift across saves ==")
        _print_result(res, args.max_failures)

    failed = [r for r in all_results if not r.ok]
    print(f"\n{'ALL CHECKS PASSED' if not failed else 'CHECKS FAILED: ' + str(len(failed))}")
    return 1 if failed else 0


def _print_result(res, max_failures):
    if res.ok:
        detail = "; ".join(res.notes)
        print(f"   PASS {res.name}" + (f": {detail}" if detail else ""))
        return
    print(f"   FAIL {res.name} ({len(res.failures)} finding(s)):")
    for line in res.failures[:max_failures]:
        print(f"      {line}")
    if len(res.failures) > max_failures:
        print(f"      ... and {len(res.failures) - max_failures} more")
    for note in res.notes:
        print(f"      ({note})")


if __name__ == "__main__":
    sys.exit(main())
