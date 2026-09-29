"""Offline coefficient fitter for Dynamic Climate & Ecological Land Use (spec 4.2-4.4).

Every model evaluation below uses the same surrogates the engine will run (Bhaskara sine,
truncated tan/cos series, fitted odd-polynomial acos, clamp) with constants rounded to the
5-decimal fixed-point grid, so every reported error is the error of the emitted constants.

Usage: python tools/dcl_fit_coefficients.py [--csv PATH]
Exit code 1 if any acceptance threshold fails.
"""

import argparse
import csv
import math
import os
import sys

try:
    import numpy as np
except ImportError:
    np = None

DECIMALS = 5
MIN_MAGNITUDE = 1e-4

CLIMATES = (
    "tropical",
    "subtropical",
    "arid",
    "mediterranean",
    "cold_arid",
    "oceanic",
    "continental",
    "arctic",
)

# climate: (|phi| deg, tan phi, cos phi, continentality, humidity base)  -- spec 4.2 verbatim
PROXIES = {
    "tropical": (8, 0.14054, 0.99027, 0.3, 80),
    "subtropical": (25, 0.46631, 0.90631, 0.5, 70),
    "arid": (25, 0.46631, 0.90631, 0.8, 20),
    "mediterranean": (38, 0.78129, 0.78801, 0.4, 55),
    "cold_arid": (45, 1.00000, 0.70711, 0.9, 30),
    "oceanic": (50, 1.19175, 0.64279, 0.2, 75),
    "continental": (52, 1.27994, 0.61566, 0.9, 60),
    "arctic": (68, 2.47509, 0.37461, 0.7, 65),
}

ELEVATION = {"wetlands": 50, "flatland": 100, "hills": 500, "plateau": 1500, "mountains": 2500}
ELEVATION_DEFAULT = 100

# Monthly mean temperature targets, degC, Jan..Dec, northern, non-coastal, 100 m (station normals).
TARGET_CURVES = {
    "continental": (-6.5, -6.7, -1.0, 6.7, 13.2, 17.0, 19.2, 17.0, 11.3, 5.6, -1.2, -5.2),  # Moscow
    "oceanic": (5.2, 5.3, 7.6, 9.9, 13.3, 16.5, 18.7, 18.5, 15.7, 12.0, 8.0, 5.5),  # London
    "mediterranean": (12.5, 12.5, 14.0, 16.0, 19.5, 23.5, 26.5, 27.0, 24.5, 20.5, 16.5, 13.5),  # Palermo
    "arid": (14.9, 16.9, 20.8, 25.9, 30.2, 32.4, 32.6, 32.4, 30.4, 26.8, 20.8, 16.3),  # Luxor (inland Egypt)
    "tropical": (26.5, 27.0, 27.5, 28.0, 28.2, 28.0, 27.6, 27.5, 27.4, 27.3, 26.8, 26.4),  # Malacca
    "subtropical": (4.6, 6.8, 10.8, 16.8, 21.9, 25.3, 29.1, 28.4, 24.1, 18.8, 12.7, 6.8),  # Hangzhou
    "cold_arid": (0.2, 2.2, 7.6, 14.4, 19.6, 24.6, 26.6, 24.7, 19.5, 12.6, 6.8, 2.3),  # Samarkand
    "arctic": (-17.6, -17.2, -11.2, -5.4, 1.4, 8.6, 13.4, 10.9, 6.3, -0.6, -8.4, -13.8),  # Naryan-Mar
}

MONTH_OFFSETS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
MID_MONTH_DAY = 15

DAYS_PER_YEAR = 365
EQUINOX_DOY = 80
LAG_BASE = 25
LAG_SPAN = 15
DECL_AMPLITUDE = 0.409
BHASKARA_NUM = 16
BHASKARA_DEN_A = 5
BHASKARA_DEN_B = 4
TAN_C3 = 0.33333
COS_C2 = 0.5
COS_C4 = 0.04167
LAPSE_RATE = 0.0065
HUMIDITY_SLOPE = 1.2
COASTAL_HUMIDITY = 20
COASTAL_CONTINENTALITY = 0.3
HUMIDITY_MIN = 5
HUMIDITY_MAX = 100
WB_T_SCALE = 0.1
WB_RH_SCALE = 0.01

LOWE_SPEC = (6.107799961, 4.4365185, 1.4289458, 0.26506485, 0.030312404, 0.0020340809, 0.00006136821)
LOWE_EMITTED = tuple(round(c, DECIMALS) for c in LOWE_SPEC[:6])
LOWE_C6_DIVISOR = round(1.0 / LOWE_SPEC[6])

THRESHOLDS = {
    "bhaskara": 0.002,
    "acos": 0.04,
    "acos_day": 0.04,
    "magnus": 0.1,
    "wet_bulb": 0.5,
    "temperature": 2.0,
}

ACOS_GRID = 4001
LAWSON_ITERATIONS_NUMPY = 3000
LAWSON_ITERATIONS_STDLIB = 400
WB_LAWSON_ITERATIONS = 500
WB_TERMS = ("1", "t", "r", "t r", "r^2", "t r^2", "t r^3")


def q(x):
    """Round to the engine's 5-decimal fixed-point grid."""
    return round(x, DECIMALS) + 0.0


def frange(lo, hi, n):
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


# ---------------------------------------------------------------- linear algebra


def solve_linear(a, b):
    """Gaussian elimination with partial pivoting (stdlib fallback)."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-300:
            raise ValueError("singular normal equations")
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for k in range(col, n + 1):
                    m[r][k] -= f * m[col][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def weighted_lstsq(rows, ys, weights):
    if np is not None:
        a = np.asarray(rows, dtype=float)
        y = np.asarray(ys, dtype=float)
        sw = np.sqrt(np.asarray(weights, dtype=float))
        c, *_ = np.linalg.lstsq(a * sw[:, None], y * sw, rcond=None)
        return [float(v) for v in c]
    n = len(rows[0])
    ata = [[0.0] * n for _ in range(n)]
    aty = [0.0] * n
    for row, y, w in zip(rows, ys, weights):
        for i in range(n):
            wi = w * row[i]
            aty[i] += wi * y
            for j in range(i, n):
                ata[i][j] += wi * row[j]
    for i in range(n):
        for j in range(i):
            ata[i][j] = ata[j][i]
    return solve_linear(ata, aty)


def minimax_fit(rows, ys, iterations=None):
    """Discrete minimax (Chebyshev) fit by Lawson's iteratively reweighted least squares."""
    if iterations is None:
        iterations = LAWSON_ITERATIONS_NUMPY if np is not None else LAWSON_ITERATIONS_STDLIB
    weights = [1.0] * len(ys)
    best, best_err = None, float("inf")
    for _ in range(iterations):
        c = weighted_lstsq(rows, ys, weights)
        errs = [abs(sum(ci * ri for ci, ri in zip(c, row)) - y) for row, y in zip(rows, ys)]
        err = max(errs)
        if err < best_err:
            best, best_err = c, err
        weights = [w * e for w, e in zip(weights, errs)]
        total = sum(weights)
        if total <= 0:
            break
        weights = [w / total for w in weights]
    return best


# ---------------------------------------------------------------- surrogates


def bhaskara_phase(p):
    """sin(2*pi*p) via Bhaskara I on each half-cycle; p is a cycle fraction."""
    p %= 1.0
    if p < 0.5:
        u, sign = 2.0 * p, 1.0
    else:
        u, sign = 2.0 * p - 1.0, -1.0
    k = u * (1.0 - u)
    return sign * BHASKARA_NUM * k / (BHASKARA_DEN_A - BHASKARA_DEN_B * k)


def bhaskara_sin(x_rad):
    return bhaskara_phase(x_rad / (2.0 * math.pi))


def acos_poly(x, c):
    c1, c3, c5, c7 = c
    x2 = x * x
    return math.pi / 2.0 - x * (c1 + x2 * (c3 + x2 * (c5 + x2 * c7)))


def day_length_poly(x, d):
    """omega_s / pi with pre-divided constants d_i = c_i / pi (what the script evaluates)."""
    d1, d3, d5, d7 = d
    x2 = x * x
    return 0.5 - x * (d1 + x2 * (d3 + x2 * (d5 + x2 * d7)))


def magnus_lowe_horner(T, coeffs=LOWE_EMITTED, c6_divisor=LOWE_C6_DIVISOR):
    """Lowe (1977) saturation vapour pressure, hPa, in t = T/10, Horner form of the rounded polynomial."""
    t = T / 10.0
    acc = 0.0
    for c in reversed(coeffs):
        acc = acc * t + c
    return acc + t ** 6 / c6_divisor


def _poly_roots(monic):
    """Durand-Kerner roots of a monic polynomial given lowest-degree-first (stdlib only)."""
    n = len(monic) - 1

    def ev(z):
        acc = 0j
        for c in reversed(monic):
            acc = acc * z + c
        return acc

    roots = [(0.4 + 0.9j) ** k for k in range(n)]
    for _ in range(500):
        new = []
        for i, z in enumerate(roots):
            den = 1 + 0j
            for j, w in enumerate(roots):
                if i != j:
                    den *= z - w
            new.append(z - ev(z) / den)
        roots = new
    return roots


def lowe_factors():
    """Spec 4.3.10 in factored form: P(t) = prod_i ((t + p_i)^2 + q_i) / D, D^3 = c6_divisor.

    The rounded Lowe sextic has three conjugate root pairs -p_i +- j sqrt(q_i); every factor is
    positive, so the product never cancels. Returns (((p1, q1), (p2, q2), (p3, q3)), D) rounded.
    """
    key = "lowe_factors"
    if key not in _cache:
        monic = [c * LOWE_C6_DIVISOR for c in LOWE_EMITTED] + [1.0]
        pairs = sorted((-z.real, z.imag ** 2) for z in _poly_roots(monic) if z.imag > 0)
        _cache[key] = (tuple((q(pp), q(qq)) for pp, qq in pairs), q(LOWE_C6_DIVISOR ** (1.0 / 3.0)))
    return _cache[key]


def magnus_lowe(T):
    """Lowe (1977) saturation vapour pressure, hPa, as the script evaluates it (factored, rounded)."""
    t = T / 10.0
    pairs, d = lowe_factors()
    acc = 1.0
    for pp, qq in pairs:
        acc *= ((t + pp) ** 2 + qq) / d
    return acc


def magnus_exact(T):
    """Goff-Gratch (1946) saturation vapour pressure over water, hPa (the curve Lowe fitted)."""
    tk = T + 273.15
    ts = 373.15
    log10_es = (
        -7.90298 * (ts / tk - 1.0)
        + 5.02808 * math.log10(ts / tk)
        - 1.3816e-7 * (10.0 ** (11.344 * (1.0 - tk / ts)) - 1.0)
        + 8.1328e-3 * (10.0 ** (-3.49149 * (ts / tk - 1.0)) - 1.0)
        + math.log10(1013.246)
    )
    return 10.0 ** log10_es


def stull_wet_bulb(T, RH):
    """Stull (2011) wet-bulb temperature, degC, from T (degC) and RH (%)."""
    return (
        T * math.atan(0.151977 * math.sqrt(RH + 8.313659))
        + math.atan(T + RH)
        - math.atan(RH - 1.676331)
        + 0.00391838 * RH ** 1.5 * math.atan(0.023101 * RH)
        - 4.686035
    )


def _wb_row(T, RH):
    t = T * WB_T_SCALE
    r = RH * WB_RH_SCALE
    return [1.0, t, r, t * r, r * r, t * r * r, t * r * r * r]


def wb_surrogate(T, RH, c):
    """Spec 4.3.11 surrogate in scaled inputs t = T/10, r = RH/100; capped at T (Tw <= T)."""
    tw = sum(ci * ri for ci, ri in zip(c, _wb_row(T, RH)))
    return min(tw, T)


def wb_constants(c):
    """Completed-square constants for the script form: A(r) = c4 (r + a_s)^2 + a_o, C(r) = c6 (r - c_s)^2 + c_o."""
    return {
        "wb_a_shift": q(c[2] / (2.0 * c[4])),
        "wb_a_off": q(c[0] - c[2] ** 2 / (4.0 * c[4])),
        "wb_c_shift": q(-c[5] / (2.0 * c[6])),
        "wb_c_off": q(c[3] - c[5] ** 2 / (4.0 * c[6])),
    }


def wb_script(T, RH, c):
    """Spec 4.3.11 as the script evaluates it: Tw = min(t (c1 + r C(r)) + A(r), T)."""
    k = wb_constants(c)
    t = T * WB_T_SCALE
    r = RH * WB_RH_SCALE
    a = c[4] * (r + k["wb_a_shift"]) ** 2 + k["wb_a_off"]
    cc = c[6] * (r - k["wb_c_shift"]) ** 2 + k["wb_c_off"]
    return min(t * (r * cc + c[1]) + a, T)


# ---------------------------------------------------------------- fits


_cache = {}


def fit_acos():
    """Minimax odd degree-7 fit of acos over [-1, 1]. Returns rounded c1..c7 + max err."""
    key = "acos"
    if key not in _cache:
        xs = frange(-1.0, 1.0, ACOS_GRID)
        rows = [[x, x ** 3, x ** 5, x ** 7] for x in xs]
        ys = [math.pi / 2.0 - math.acos(x) for x in xs]
        raw = minimax_fit(rows, ys)
        c = tuple(q(v) for v in raw)
        d = tuple(q(v / math.pi) for v in raw)
        err = max(abs(acos_poly(x, c) - math.acos(x)) for x in xs)
        err_d = max(abs(math.pi * day_length_poly(x, d) - math.acos(x)) for x in xs)
        _cache[key] = (c, d, err, err_d)
    c, _, err, _ = _cache[key]
    return (*c, err)


def acos_day_constants():
    """Pre-divided d_i = c_i / pi (the form the engine runs) + max err of pi * dlf vs acos."""
    fit_acos()
    _, d, _, err_d = _cache["acos"]
    return d, err_d


def _wb_grid():
    return [(t, rh) for t in frange(20.0, 50.0, 61) for rh in frange(5.0, 100.0, 96)]


def fit_wet_bulb():
    """Minimax fit of the spec 4.3.11 surrogate to Stull over T 20..50, RH 5..100.

    Returns (rounded coefficients in WB_TERMS order, max err of the script form wb_script).
    """
    key = "wb"
    if key not in _cache:
        fine = _wb_grid()
        train = fine if np is not None else fine[::7]
        raw = minimax_fit([_wb_row(t, rh) for t, rh in train], [stull_wet_bulb(t, rh) for t, rh in train],
                          iterations=WB_LAWSON_ITERATIONS if np is not None else None)
        c = tuple(q(v) for v in raw)
        err = max(abs(wb_script(t, rh, c) - stull_wet_bulb(t, rh)) for t, rh in fine)
        _cache[key] = (c, err)
    return _cache[key]


def _continentality(climate, coastal):
    cont = PROXIES[climate][3]
    if coastal:
        cont = max(0.0, cont - COASTAL_CONTINENTALITY)
    return cont


def thermal_lag_days(climate, coastal):
    return LAG_BASE + (1.0 - _continentality(climate, coastal)) * LAG_SPAN


def lagged_declination(climate, doy, coastal):
    phase = (doy - EQUINOX_DOY - thermal_lag_days(climate, coastal)) / DAYS_PER_YEAR
    return DECL_AMPLITUDE * bhaskara_phase(phase)


def insolation(climate, doy, coastal, southern):
    _, tan_phi, cos_phi, _, _ = PROXIES[climate]
    if southern:
        tan_phi = -tan_phi
    dec = lagged_declination(climate, doy, coastal)
    tan_dec = dec + dec ** 3 * TAN_C3
    cos_dec = 1.0 - COS_C2 * dec ** 2 + COS_C4 * dec ** 4
    x = max(-1.0, min(1.0, -tan_phi * tan_dec))
    d, _ = acos_day_constants()
    return day_length_poly(x, d) * cos_phi * cos_dec


def _regressor(climate, doy, coastal, southern):
    return insolation(climate, doy, coastal, southern) - 0.5 * PROXIES[climate][2]


def _monthly_regressors(climate, coastal=False, southern=False):
    out = []
    for offset, days in zip(MONTH_OFFSETS, MONTH_DAYS):
        out.append(sum(_regressor(climate, offset + day, coastal, southern) for day in range(1, days + 1)) / days)
    return out


def _monthly_decl2(climate, coastal=False):
    out = []
    for offset, days in zip(MONTH_OFFSETS, MONTH_DAYS):
        out.append(sum(lagged_declination(climate, offset + day, coastal) ** 2 for day in range(1, days + 1)) / days)
    return out


def _temperature_params(climate):
    """Least squares refined by Lawson minimax on the 12 monthly means: rows [1, Q - 0.5 cos_phi, delta^2]."""
    key = ("temp", climate)
    if key not in _cache:
        rs = _monthly_regressors(climate)
        d2 = _monthly_decl2(climate)
        ys = [t + ELEVATION_DEFAULT * LAPSE_RATE for t in TARGET_CURVES[climate]]
        raw = minimax_fit([[1.0, r, d] for r, d in zip(rs, d2)], ys)
        _cache[key] = tuple(q(v) for v in raw)
    return _cache[key]


def model_temperature(climate, doy, coastal, southern, elev):
    t_offset, g, h = _temperature_params(climate)
    dec = lagged_declination(climate, doy, coastal)
    return t_offset + g * _regressor(climate, doy, coastal, southern) + h * dec ** 2 - elev * LAPSE_RATE


def temperature_mean(climate, elev):
    return _temperature_params(climate)[0] - elev * LAPSE_RATE


def monthly_means(climate, coastal=False, southern=False, elev=float(ELEVATION_DEFAULT)):
    t_offset, g, h = _temperature_params(climate)
    return [t_offset + g * r + h * d - elev * LAPSE_RATE
            for r, d in zip(_monthly_regressors(climate, coastal, southern), _monthly_decl2(climate, coastal))]


def fit_temperature(climate):
    t_offset, g, h = _temperature_params(climate)
    means = monthly_means(climate)
    err = max(abs(m - t) for m, t in zip(means, TARGET_CURVES[climate]))
    return t_offset, g, h, err


def model_humidity(climate, T, coastal, elev):
    base = PROXIES[climate][4]
    h = base + (COASTAL_HUMIDITY if coastal else 0) - HUMIDITY_SLOPE * (T - temperature_mean(climate, elev))
    return max(HUMIDITY_MIN, min(HUMIDITY_MAX, h))


# ---------------------------------------------------------------- script forms
# The engine evaluates the model at mid-month from flat, current_month-keyed leaf tables with
# algebraically regrouped formulas (odd Bhaskara, completed squares); these functions are that
# exact arithmetic with the rounded constants, so the printed leaves are what the script runs.


def _mid_month_phase(m):
    return (MONTH_OFFSETS[m] + MID_MONTH_DAY - EQUINOX_DOY) / DAYS_PER_YEAR


def lag_phase(climate, coastal):
    return q(thermal_lag_days(climate, coastal) / DAYS_PER_YEAR)


def lagged_phase_center():
    lags = [lag_phase(cl, co) for cl in CLIMATES for co in (False, True)]
    return (min(lags) + max(lags)) / 2.0


def month_phase_table(center):
    """Mid-month phase per month, wrapped into [center - 0.5, center + 0.5) so no modulo is needed."""
    out = []
    for m in range(12):
        p = _mid_month_phase(m)
        while p >= center + 0.5:
            p -= 1.0
        while p < center - 0.5:
            p += 1.0
        out.append(q(p))
    return tuple(out)


def odd_bhaskara(a):
    """sin(pi a) for a in [-1, 1]: 16 a (1 - |a|) / (4 (|a| - 0.5)^2 + 4)."""
    return BHASKARA_NUM * a * (1.0 - abs(a)) / (BHASKARA_DEN_B * (abs(a) - 0.5) ** 2 + BHASKARA_DEN_B)


def cos_constants():
    """cos_delta = COS_C4 (delta^2 - cos_s)^2 + cos_o (completed square of 1 - C2 d^2 + C4 d^4)."""
    s = q(COS_C2 / (2.0 * COS_C4))
    return s, q(1.0 - COS_C4 * s * s)


def day_q_constants():
    """d3 + d5 y + d7 y^2 = d7 (y - dq_s)^2 + dq_o."""
    d, _ = acos_day_constants()
    _, d3, d5, d7 = d
    return q(-d5 / (2.0 * d7)), q(d3 - d5 * d5 / (4.0 * d7))


def script_lagged_declination(climate, m, coastal):
    """The generated table value: odd Bhaskara on the rounded phases, itself 5-decimal rounded."""
    mp = month_phase_table(lagged_phase_center())[m]
    return q(DECL_AMPLITUDE * odd_bhaskara(2.0 * (mp - lag_phase(climate, coastal))))


def script_solar_declination(m):
    return DECL_AMPLITUDE * odd_bhaskara(2.0 * month_phase_table(0.0)[m])


def script_day_length(tan_phi, dec):
    """Day-length factor as the script evaluates it: 0.5 - x (d1 + y (d7 (y - dq_s)^2 + dq_o)).

    x = clamp(-tan_phi * tan_dec, -1, 1); y = x^2 via `pow = 2` (identical to min(u^2, 1)).
    """
    d, _ = acos_day_constants()
    dq_s, dq_o = day_q_constants()
    tan_dec = dec * (1.0 + dec * dec * TAN_C3)
    x = max(-1.0, min(1.0, -tan_phi * tan_dec))
    y = x * x
    return 0.5 - x * (d[0] + y * (d[3] * (y - dq_s) ** 2 + dq_o))


def script_insolation(climate, m, coastal, southern):
    _, tan_phi, cos_phi, _, _ = PROXIES[climate]
    if southern:
        tan_phi = -tan_phi
    dec = script_lagged_declination(climate, m, coastal)
    cos_s, cos_o = cos_constants()
    cos_dec = COS_C4 * (dec * dec - cos_s) ** 2 + cos_o
    return script_day_length(tan_phi, dec) * cos_phi * cos_dec


def script_temperature(climate, m, coastal, southern, elev):
    t_offset, g, h = _temperature_params(climate)
    dec = script_lagged_declination(climate, m, coastal)
    q_term = script_insolation(climate, m, coastal, southern) - 0.5 * PROXIES[climate][2]
    return t_offset + g * q_term + h * dec * dec - elev * LAPSE_RATE


# ---------------------------------------------------------------- outputs


def emitted_constants():
    consts = {
        "days_per_year": DAYS_PER_YEAR,
        "equinox_doy": EQUINOX_DOY,
        "lag_base": LAG_BASE,
        "lag_span": LAG_SPAN,
        "decl_amplitude": DECL_AMPLITUDE,
        "bhaskara_num": BHASKARA_NUM,
        "bhaskara_den_a": BHASKARA_DEN_A,
        "bhaskara_den_b": BHASKARA_DEN_B,
        "tan_c3": TAN_C3,
        "cos_c2": COS_C2,
        "cos_c4": COS_C4,
        "lapse_rate": LAPSE_RATE,
        "humidity_slope": HUMIDITY_SLOPE,
        "coastal_humidity": COASTAL_HUMIDITY,
        "coastal_continentality": COASTAL_CONTINENTALITY,
        "wb_t_scale": WB_T_SCALE,
        "wb_rh_scale": WB_RH_SCALE,
        "lowe_c6_divisor": LOWE_C6_DIVISOR,
    }
    c1, c3, c5, c7, _ = fit_acos()
    d, _ = acos_day_constants()
    for name, v in zip(("c1", "c3", "c5", "c7"), (c1, c3, c5, c7)):
        consts[f"acos_{name}"] = v
    for name, v in zip(("d1", "d3", "d5", "d7"), d):
        consts[f"acos_{name}"] = v
    dq_s, dq_o = day_q_constants()
    consts["acos_dq_s"] = dq_s
    consts["acos_dq_o"] = dq_o
    cos_s, cos_o = cos_constants()
    consts["cos_s"] = cos_s
    consts["cos_o"] = cos_o
    for i, v in enumerate(LOWE_EMITTED):
        consts[f"lowe_c{i}"] = v
    pairs, lowe_d = lowe_factors()
    for i, (pp, qq) in enumerate(pairs, 1):
        consts[f"lowe_p{i}"] = pp
        consts[f"lowe_q{i}"] = qq
    consts["lowe_factor_divisor"] = lowe_d
    wb, _ = fit_wet_bulb()
    for i, v in enumerate(wb):
        consts[f"wb_c{i}"] = v
    consts.update(wb_constants(wb))
    for climate in CLIMATES:
        _, tan_phi, cos_phi, cont, base = PROXIES[climate]
        t_offset, g, h, _ = fit_temperature(climate)
        consts[f"{climate}_tan_phi"] = tan_phi
        consts[f"{climate}_cos_phi"] = cos_phi
        consts[f"{climate}_continentality"] = cont
        consts[f"{climate}_humidity_base"] = base
        consts[f"{climate}_temp_offset"] = t_offset
        consts[f"{climate}_temp_gain"] = g
        consts[f"{climate}_temp_decl2_gain"] = h
    for topo, elev in ELEVATION.items():
        consts[f"elevation_{topo}"] = elev
    return consts


def reference_table():
    wb, _ = fit_wet_bulb()
    elev = float(ELEVATION_DEFAULT)
    rows = []
    for climate in CLIMATES:
        for coastal in (False, True):
            means = monthly_means(climate, coastal=coastal, elev=elev)
            for m, t in enumerate(means):
                t_mid = model_temperature(climate, MONTH_OFFSETS[m] + MID_MONTH_DAY, coastal, False, elev)
                h = model_humidity(climate, t, coastal, elev)
                rows.append({
                    "climate": climate,
                    "month": m + 1,
                    "coastal": int(coastal),
                    "T": round(t, 2),
                    "T_mid": round(t_mid, 2),
                    "H": round(h, 1),
                    "Tw": round(wb_surrogate(t, h, wb), 2) if t >= 20.0 else None,
                    "E_s": round(magnus_lowe(t), 2),
                })
    return rows


def write_csv(path, rows):
    fields = ["climate", "month", "coastal", "T", "T_mid", "H", "Tw", "E_s"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row[k] is None else row[k]) for k in fields})


def fmt(v):
    if isinstance(v, int):
        return str(v)
    return f"{v:.5f}".rstrip("0").rstrip(".") if v != 0 else "0"


def error_report():
    results = {}
    results["bhaskara"] = max(abs(bhaskara_sin(x) - math.sin(x)) for x in frange(0.0, 2.0 * math.pi, 20001))
    results["acos"] = fit_acos()[4]
    results["acos_day"] = acos_day_constants()[1]
    results["magnus"] = max(abs(magnus_lowe(t) - magnus_exact(t)) for t in frange(-40.0, 50.0, 901))
    results["wet_bulb"] = fit_wet_bulb()[1]
    results["temperature"] = max(fit_temperature(c)[3] for c in CLIMATES)
    return results


def print_blocks(out):
    c1, c3, c5, c7, _ = fit_acos()
    d, err_d = acos_day_constants()
    wb, _ = fit_wet_bulb()
    consts = emitted_constants()
    p = lambda s="": print(s, file=out)  # noqa: E731

    p("# ===== DCL constant blocks (generated by tools/dcl_fit_coefficients.py) =====")
    p("# --- calendar / declination (spec 4.3.1-4.3.4) ---")
    p("# lagged declination is emitted as a GENERATED month x climate x coastal table")
    p("# (print_script_tables below); the un-lagged calc_solar_declination stays a live")
    p("# odd-Bhaskara chain on dcl_solar_month_phase:")
    p("#   a = 2 * phase ; sin = 16 a (1 - |a|) / (4 (|a| - 0.5)^2 + 4) ; delta = 0.409 * sin")
    p("# tan_delta = delta (1 + delta^2 * 0.33333)")
    p("# cos_delta = 0.04167 (delta^2 - cos_s)^2 + cos_o  (completed square of 1 - d^2/2 + d^4/24)")
    p(f"dcl_cos_s = {fmt(consts['cos_s'])}")
    p(f"dcl_cos_o = {fmt(consts['cos_o'])}")
    p()
    p("# --- acos / day length (spec 4.3.5) ---")
    p("# x = clamp(-tan_phi * tan_delta, -1, 1) ; y = x^2 (pow = 2)")
    p("# acos(x) ~= pi/2 - x (c1 + c3 x^2 + c5 x^4 + c7 x^6)")
    p(f"dcl_acos_c1 = {fmt(c1)}")
    p(f"dcl_acos_c3 = {fmt(c3)}")
    p(f"dcl_acos_c5 = {fmt(c5)}")
    p(f"dcl_acos_c7 = {fmt(c7)}")
    p("# calc_day_length_factor = omega_s / pi = 0.5 - x (d1 + y (d7 (y - dq_s)^2 + dq_o)), d_i = c_i / pi")
    p(f"dcl_acos_d1 = {fmt(d[0])}")
    p(f"dcl_acos_d7 = {fmt(d[3])}")
    p(f"dcl_acos_dq_s = {fmt(consts['acos_dq_s'])}")
    p(f"dcl_acos_dq_o = {fmt(consts['acos_dq_o'])}")
    p(f"# max |pi * dlf - acos| over [-1, 1] with rounded d_i: {err_d:.5f} rad")
    p()
    p("# --- saturation vapour pressure, Lowe 1977 (spec 4.3.10), t = clamp(T, -50, 50) / 10 ---")
    p("# Factored sextic (no t^6 overflow): E_s = prod_i ((t + p_i)^2 + q_i) / D, D^3 = c6_divisor")
    pairs, lowe_d = lowe_factors()
    for i, (pp, qq) in enumerate(pairs, 1):
        p(f"dcl_lowe_p{i} = {fmt(pp)}")
        p(f"dcl_lowe_q{i} = {fmt(qq)}")
    p(f"dcl_lowe_factor_divisor = {fmt(lowe_d)}  # {fmt(lowe_d)}^3 = {lowe_d ** 3:.5f} ~ {LOWE_C6_DIVISOR}")
    p()
    p("# --- wet-bulb surrogate of Stull 2011 (spec 4.3.11), t = T / 10, r = RH / 100 ---")
    p("# Tw = t (c1 + r C(r)) + A(r), A(r) = c4 (r + a_s)^2 + a_o, C(r) = c6 (r - c_s)^2 + c_o")
    p("# (completed squares of c0 + c1 t + c2 r + c3 t r + c4 r^2 + c5 t r^2 + c6 t r^3); then max = T")
    p(f"dcl_wb_c1 = {fmt(wb[1])}")
    p(f"dcl_wb_c4 = {fmt(wb[4])}")
    p(f"dcl_wb_c6 = {fmt(wb[6])}")
    for key in ("wb_a_shift", "wb_a_off", "wb_c_shift", "wb_c_off"):
        p(f"dcl_{key} = {fmt(consts[key])}")
    p()
    p("# --- per-climate temperature fit (spec 4.3.7): T = T_offset + G (Q - 0.5 cos_phi) + H delta^2 - elev * 0.0065 ---")
    p("# delta = lagged declination (spec 4.3.2); G = dcl_temp_gain, H = dcl_temp_decl2_gain")
    ordered = [c for c in CLIMATES if c != "oceanic"] + ["oceanic"]
    for key, field in (("dcl_temp_offset", "temp_offset"), ("dcl_temp_gain", "temp_gain"),
                       ("dcl_temp_decl2_gain", "temp_decl2_gain")):
        p(f"{key} = {{")
        for i, climate in enumerate(ordered):
            if climate == "oceanic":
                p(f"    else = {{ value = {fmt(consts[f'{climate}_{field}'])} }}")
            else:
                kw = "if" if i == 0 else "else_if"
                p(f"    {kw} = {{ limit = {{ climate = {climate} }} value = {fmt(consts[f'{climate}_{field}'])} }}")
        p("}")


def _month_rows(values, indent):
    """if/else_if/else current_month chain rows at the given indent (December is else)."""
    pad = " " * indent
    lines = []
    for m, v in enumerate(values, 1):
        if m == 1:
            lines.append(f"{pad}if = {{ limit = {{ current_month = 1 }} value = {fmt(v)} }}")
        elif m < 12:
            lines.append(f"{pad}else_if = {{ limit = {{ current_month = {m} }} value = {fmt(v)} }}")
        else:
            lines.append(f"{pad}else = {{ value = {fmt(v)} }}")
    return lines


def print_script_tables(out):
    """Emit the generated leaf tables that the script_values files paste verbatim."""
    p = lambda s="": print(s, file=out)  # noqa: E731
    center = lagged_phase_center()
    lags = {cl: (lag_phase(cl, False), lag_phase(cl, True)) for cl in CLIMATES}

    p("# ===== GENERATED LEAF TABLES (paste into dcl_advanced_climate_math.txt) =====")
    p("# dcl_lagged_declination: thermally lagged declination (radians) by climate x coastal x month.")
    p("# delta = 0.409 * odd_bhaskara(2 * (month_phase - lag_phase)), month_phase wrapped around the")
    p(f"# lag midpoint {center:.5f}; odd_bhaskara(a) = 16 a (1 - |a|) / (4 (|a| - 0.5)^2 + 4).")
    p("# lag_phase = (25 + (1 - continentality) * 15) / 365 per climate (inland / coastal):")
    p("#   " + " ; ".join(f"{cl} {lags[cl][0]:.5f}/{lags[cl][1]:.5f}" for cl in CLIMATES))
    p("dcl_lagged_declination = {")
    ordered = [c for c in CLIMATES if c != "oceanic"] + ["oceanic"]
    for i, climate in enumerate(ordered):
        if i == 0:
            p(f"    if = {{ limit = {{ climate = {climate} }}")
        elif climate == "oceanic":
            p("    else = {  # oceanic fallback")
        else:
            p(f"    else_if = {{ limit = {{ climate = {climate} }}")
        for coastal in (True, False):
            values = [script_lagged_declination(climate, m, coastal) for m in range(12)]
            if coastal:
                p("        if = { limit = { is_coastal = yes }")
            else:
                p("        else = {")
            p("\n".join(_month_rows(values, 12)))
            p("        }")
        p("    }")
    p("}")
    p()
    p("# dcl_solar_month_phase: mid-month phase ((offset + 15 - 80) / 365) wrapped to [-0.5, 0.5).")
    p("dcl_solar_month_phase = {")
    p("\n".join(_month_rows(month_phase_table(0.0), 4)))
    p("}")


def print_report(results, out):
    p = lambda s="": print(s, file=out)  # noqa: E731
    p("# ===== error report =====")
    p(f"# least squares backend: {'numpy ' + np.__version__ if np is not None else 'stdlib'}")
    labels = {
        "bhaskara": ("Bhaskara sine vs sin, [0, 2pi]", ""),
        "acos": ("acos poly vs acos, [-1, 1]", " rad"),
        "acos_day": ("day-length form pi*(0.5 - x*poly(d_i)) vs acos, [-1, 1]", " rad"),
        "magnus": ("Lowe (emitted) vs Goff-Gratch, -40..50 C", " hPa"),
        "wet_bulb": ("wet-bulb surrogate vs Stull, T 20..50, RH 5..100", " C"),
        "temperature": ("worst monthly-mean deviation, all climates", " C"),
    }
    for key, (label, unit) in labels.items():
        status = "PASS" if results[key] < THRESHOLDS[key] else "FAIL"
        p(f"{status}  {label}: max err {results[key]:.5f}{unit} (threshold < {THRESHOLDS[key]})")
    p("# per-climate temperature fit (monthly means vs target):")
    for climate in CLIMATES:
        t_offset, g, h, err = fit_temperature(climate)
        means = monthly_means(climate)
        resid = " ".join(f"{m - t:+.2f}" for m, t in zip(means, TARGET_CURVES[climate]))
        p(f"#   {climate:<13} T_offset={fmt(t_offset):>9} G={fmt(g):>9} H={fmt(h):>9} max_err={err:.3f}  residuals: {resid}")
    bad = [n for n, v in emitted_constants().items() if v != 0 and abs(v) < MIN_MAGNITUDE]
    p(f"{'PASS' if not bad else 'FAIL'}  every emitted constant |c| >= 1e-4 or 0" + (f": {bad}" if bad else ""))
    return bad


def print_informational(out):
    p = lambda s="": print(s, file=out)  # noqa: E731
    p("# ===== informational (NOT acceptance checks) =====")
    worst_x = max(
        min(1.0, abs(PROXIES[cl][1] * (DECL_AMPLITUDE + DECL_AMPLITUDE ** 3 * TAN_C3)))
        for cl in CLIMATES if cl != "arctic"
    )
    p(f"# largest |x| reached by non-arctic climates: {worst_x:.3f} (arctic reaches the clamp)")
    spread = max(
        abs(model_temperature(cl, MONTH_OFFSETS[m] + MID_MONTH_DAY, False, False, 100.0) - monthly_means(cl)[m])
        for cl in CLIMATES for m in range(12)
    )
    p(f"# max |T(day 15) - monthly mean| over climates/months: {spread:.3f} C")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "dcl_reference_table.csv"))
    args = parser.parse_args(argv)
    out = sys.stdout
    results = error_report()
    bad = print_report(results, out)
    print(file=out)
    print_blocks(out)
    print(file=out)
    print_script_tables(out)
    print(file=out)
    print_informational(out)
    rows = reference_table()
    write_csv(args.csv, rows)
    print(f"\n# wrote {len(rows)} rows to {args.csv}", file=out)
    failed = [k for k, v in results.items() if v >= THRESHOLDS[k]]
    if bad:
        failed.append("coefficient_magnitude")
    if failed:
        print(f"# THRESHOLD FAILURES: {', '.join(failed)}", file=out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
