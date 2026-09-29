# Dynamic Climate & Ecological Land Use — Design Spec

- Date: 2026-09-29
- Target: Europa Universalis V 1.3.11 (Jomini / "Caesar")
- Mod folder: `C:\Kimi Workspace\dynamic-climate-land-use\` — identifier prefix `dcl_`
- Status: design approved section-by-section; spec self-reviewed

## 1. Goal and scope

A standalone mod that simulates lazy, formula-driven climate (temperature, humidity, vapour
pressure, wet-bulb), regional macro weather fronts, climate-driven gameplay modifiers for
locations and armies, and a per-location cultivated-vs-wilderness land-use system.

It **replaces** `eu5-ecology-mod`; the README instructs users to disable that mod. No shared
identifiers, no compatibility layer.

### 1.1 Non-negotiable constraints (from brief)

| Constraint | How it is met |
|---|---|
| No daily tick over the map | No `on_daily*` hook anywhere. Monthly/yearly country pulses iterate owned locations only; one global monthly pulse rolls fronts. Audit-enforced. |
| Temperature/humidity lazy | Pure script values, no stored climate state. GUI evaluates them on demand; the monthly pass evaluates once per owned location and caches in a temporary scope value for that pass only. |
| Symmetric AI/player | All logic runs in `monthly_country_pulse` / `yearly_country_pulse` for every country; no `is_ai` branches. |
| Army-scope attrition | `every_army` evaluates climate at `unit_location`; empty locations are never pre-computed for armies. |
| One persistent location variable | `var:cultivation_ratio` only, literal-clamped [0.05, 0.95]. Wilderness = `1 − ratio`, never stored. Climate stress memory is a timed modifier, not a variable. |
| Yearly land drift | `yearly_country_pulse` only. |

### 1.2 Engine-reality substitutions (brief term → real EU5 construct)

EU5 has no `on_daily_tick`/`on_monthly_tick`/`on_yearly_tick`, no triggered-modifier folder, no
latitude/elevation data, no `sin/acos/exp`, and several brief modifier keys do not exist.
Verified replacements:

| Brief | Implemented as |
|---|---|
| `on_monthly_tick` | `monthly_country_pulse` (country scope) + `weather_monthly_pulse` (global, once/month) |
| `on_yearly_tick` | `yearly_country_pulse` |
| `common/triggered_modifiers/` | Static modifiers in `main_menu/common/static_modifiers/`, applied by the monthly pass with `months = 1 mode = replace` |
| `current_day_of_year` | Script value `dcl_day_of_year` = 12-branch `current_month` offset + `current_day` (fallback: +15) |
| `sin`, `cos`, `tan`, `acos`, `exp` | Polynomial/rational approximations (Bhaskara sine, Taylor series, odd-polynomial acos, Lowe 1977 Magnus polynomial) |
| `location_latitude` | Per-climate |φ| proxy, signed by `hemisphere` |
| `location_elevation` | Per-topography elevation proxy (m) |
| `continentality_factor` | Per-climate proxy, −0.3 if `is_coastal` |
| `clamp(x, a, b)` | Script-value `min = a` / `max = b` rows |
| `local_movement_speed −0.40` | `movement_cost +0.40` |
| `local_supply_limit_mult` | `local_supply_limit_modifier` |
| `army_attrition 0.03` | `local_army_attrition = 3` (already_percent) |
| epidemic spread | `local_malaria_growth_modifier`, `local_typhus_growth_modifier` |
| `local_food_spoilage_mult` | `local_food_decay_modifier` |
| `local_food_production_mult` | `local_monthly_food_modifier` |
| building construction cost | `local_construction_speed` + `local_build_buildings_efficiency` (no cost key exists) |
| local tax revenue | `local_production_efficiency` (no local tax key exists) |
| `local_forestry_goods_mult` | `local_lumber_output_modifier` (open item: compare `local_raw_material_output`) |
| `enemy_army_attrition` | `local_hostile_attrition` (already_percent) |
| expeditionary attrition | Unit modifier `land_unit_attrition` via army pass |
| Köppen zone | Vanilla climate key (8 values) |
| terrain Farmlands/Forests/... | Vanilla `vegetation` + `topography` keys |

Brief variable names kept unprefixed: `cultivation_ratio`, `macro_temp_anomaly`,
`macro_humidity_anomaly`.

## 2. Architecture (approved "A")

```
weather_monthly_pulse (global, 1×/month)
  └─ dcl_roll_macro_fronts          → 3–5 random areas get timed anomaly variables

monthly_country_pulse (per country)
  ├─ every_owned_location
  │    ├─ save-upgrade guard: init cultivation_ratio if missing (+ apply land modifiers)
  │    └─ dcl_evaluate_location_climate
  │         cache T, H (temporary scope values) → wet-bulb if T ≥ 31
  │         apply matching climate static modifiers (months = 1, replace)
  │         apply dcl_climate_stress_memory (months = 12) on mud/drought
  └─ every_army
       └─ evaluate at unit_location → unit modifiers (months = 1, replace)

yearly_country_pulse (per country)
  └─ every_owned_location
       ├─ guard init
       ├─ dcl_apply_land_drift     → change ratio, cap by terrain, literal clamp
       └─ dcl_apply_land_modifiers → re-apply size-scaled land modifiers

on_game_start (global)
  ├─ every_country → every_owned_location → init ratio + land modifiers
  └─ every_area → stamp dcl_area_class
on_game_load_after_lobby (global)
  └─ area-class guard (stamp areas missing dcl_area_class)
```

Unowned locations: climate visible in GUI (lazy), no modifiers applied to the location; armies
standing there get mirror unit modifiers.

## 3. File map

All game text files UTF-8 **with BOM**, 4-space indentation.

| Path | Content |
|---|---|
| `.metadata/metadata.json` | Descriptor, `supported_game_version = "1.3.11"` |
| `README.md` | Install, "disable eu5-ecology-mod", feature summary, limitations |
| `in_game/common/script_values/dcl_climate_math_values.txt` | Proxies, day-of-year, T, H, front readout |
| `in_game/common/script_values/dcl_advanced_climate_math.txt` | Declination, trig series, day length, insolation, Magnus, wet-bulb |
| `in_game/common/script_values/dcl_land_use_values.txt` | Initial ratio, max arable, farmer fill, drift delta, capped target, display values, GUI 0/1 flags |
| `in_game/common/scripted_triggers/dcl_climate_triggers.txt` | Parameterised condition triggers, area-fit triggers |
| `in_game/common/scripted_effects/dcl_climate_effects.txt` | Location climate evaluation, army evaluation, front rolling, area stamping |
| `in_game/common/scripted_effects/dcl_land_effects.txt` | Ratio init, drift, land-modifier application |
| `in_game/common/on_action/dcl_climate_macro_fronts.txt` | `weather_monthly_pulse` hook |
| `in_game/common/on_action/dcl_climate_monthly.txt` | `monthly_country_pulse` hook |
| `in_game/common/on_action/dcl_land_init.txt` | `on_game_start`, `on_game_load_after_lobby` hooks |
| `in_game/common/on_action/dcl_land_drift_yearly.txt` | `yearly_country_pulse` hook |
| `main_menu/common/static_modifiers/dcl_climate_gameplay_effects.txt` | 6 location + 5 unit climate modifiers |
| `main_menu/common/static_modifiers/dcl_land_use_effects.txt` | 2 size-scaled land modifiers |
| `in_game/gui/location_window.gui` | Full vanilla copy + "Climate" tab (vanilla touchpoint 1) |
| `in_game/gui/shared/zz_dcl_location_tooltips.gui` | `template tooltip_location_alt_content` redefinition + one row (vanilla touchpoint 2) |
| `main_menu/localization/english/dcl_climate_mod_l_english.yml` | All loc keys |
| `tools/dcl_fit_coefficients.py` | Offline coefficient fitter + error report + reference table |
| `tools/dcl_audit.py` | Static audit (braces, BOM, defs/refs, loc, registry keys, forbidden patterns) |
| `tools/dcl_verify_save.py` | Savegame assertions |
| workspace `sync_mod.py` | Gains optional mod-name argument (default stays `eu5-ecology-mod`) |

No new modifier *types* are defined, so no modifier-icon registration is needed.

## 4. Climate math (approved)

### 4.1 Numeric rules

- Script values are fixed-point with 1e-5 resolution: every coefficient must be ≥ 1e-4 in
  magnitude; rescale inputs where needed (Magnus uses t = T/10).
- No sqrt and no transcendental functions (sin/cos/acos/exp run as fitted polynomial
  surrogates). Verified-available row operators: `abs` (RHS ignored), `pow`, `modulo`,
  `round`, `floor`, `ceiling` (vanilla citations in `tools/dcl_audit.py`). Clamping via
  `min`/`max` rows (`max` caps, `min` floors).
- `fixed_range`/`integer_range` are never used in any formula.
- Script values stay flat where they could render in tooltips; they are not rendered as
  breakdowns in this mod.

### 4.2 Proxies (per location)

| Climate | |φ| (deg) | tan φ | cos φ | continentality | humidity base |
|---|---|---|---|---|---|
| tropical | 8 | 0.14054 | 0.99027 | 0.3 | 80 |
| subtropical | 25 | 0.46631 | 0.90631 | 0.5 | 70 |
| arid | 25 | 0.46631 | 0.90631 | 0.8 | 20 |
| mediterranean | 38 | 0.78129 | 0.78801 | 0.4 | 55 |
| cold_arid | 45 | 1.00000 | 0.70711 | 0.9 | 30 |
| oceanic | 50 | 1.19175 | 0.64279 | 0.2 | 75 |
| continental | 52 | 1.27994 | 0.61566 | 0.9 | 60 |
| arctic | 68 | 2.47509 | 0.37461 | 0.7 | 65 |

- `hemisphere = southern` negates φ (sign applied to tan φ; cos φ is even).
- Coastal: continentality −0.3 (floored at 0), humidity +20.
- Elevation (m) by topography: wetlands 50, flatland 100, hills 500, plateau 1500,
  mountains 2500; other/unknown 100.
- Humidity bases are fixed literals (table above), not fitted; the monthly swing comes from the
  −1.2 × (T − T_mean) term and fronts.
- Documented limitation: a single latitude per climate (e.g. Cusco classifies as continental and
  reads as ≈52°; elevation proxy partly compensates).

### 4.3 Formulas

1. `dcl_day_of_year` = month offset (0, 31, 59, …, 334) + `current_day` (fallback +15).
2. `dcl_thermal_lag_days` = 25 + (1 − continentality) × 15.
3. `calc_solar_declination` (radians) = 0.409 × sin(2π/365 × (doy − 80 − lag)), sine via
   Bhaskara I on each half-cycle (max error ≈ 0.0016).
   - Note: the brief's standalone declination has no lag; the lag enters the temperature path
     by shifting the day argument, as specified in the addendum. A pure `calc_solar_declination`
     (no lag) is also provided for the GUI tooltip; the thermal path uses
     `dcl_lagged_declination`.
4. `tan δ ≈ δ + δ³/3`, `cos δ ≈ 1 − δ²/2 + δ⁴/24` (|δ| ≤ 0.409 ⇒ error < 1e-4).
5. `calc_day_length_factor` = ω_s / π where
   ω_s = acos(clamp(−tan φ · tan δ, −1, 1)), acos(x) ≈ π/2 − x(c1 + c3x² + c5x⁴ + c7x⁶);
   coefficients from the fitter, fitted over the full domain [−1, 1] (target max error
   < 0.04 rad; an odd degree-7 polynomial cannot do better because of the endpoint
   singularity — achieved 0.0393).
6. Insolation Q = day_length_factor × cos φ × cos δ.
7. `dynamic_location_temperature` (°C)
   = T_offset[clim] + G[clim] × (Q − 0.5 × cos φ) + H[clim] × δ² − elevation × 0.0065
   + area `macro_temp_anomaly` (read only behind `area ?= { has_variable = macro_temp_anomaly }`,
   default 0), where δ is the lagged declination (`dcl_lagged_declination`; δ² is already
   computed for cos δ). T_offset, G and H (`dcl_temp_offset`, `dcl_temp_gain`,
   `dcl_temp_decl2_gain`, one per-climate block each) come from the fitter so monthly means
   match per-climate targets within ±2 °C. The δ² term shapes the annual curve (asymmetric
   seasons) that a single Q regressor cannot follow in continental/arctic climates.
8. `dcl_location_temperature_mean` = T_offset[clim] − elevation × 0.0065 (the "base_temp" of the
   brief; no anomaly, no season).
9. `dynamic_location_humidity` (%)
   = base[clim] + 20 (if coastal) − (T − T_mean) × 1.2 + area `macro_humidity_anomaly`,
   floored at 5, capped at 100.
10. `dynamic_saturation_vapor_pressure` (hPa), Lowe 1977 in t = T/10:
    6.107799961 + 4.4365185 t + 1.4289458 t² + 0.26506485 t³ + 0.030312404 t⁴
    + 0.0020340809 t⁵ + 0.00006136821 t⁶ (coefficients pre-scaled by 10ⁿ as listed).
11. `wet_bulb_temperature` (°C): minimax surrogate of Stull (2011) in scaled inputs
    t = T/10, r = RH/100: c0 + c1t + c2r + c3t·r + c4r² + c5t·r² + c6t·r³, fitted over
    T 20–50 °C, RH 5–100 % (target max error < 0.5 °C; achieved ≈ 0.24 °C), then capped
    at T (Tw ≤ T always). Evaluated for gameplay only when T ≥ 31.

Script-value names from the brief are kept verbatim (`dynamic_location_temperature`,
`dynamic_location_humidity`, `calc_solar_declination`, `calc_day_length_factor`,
`dynamic_saturation_vapor_pressure`, `wet_bulb_temperature`) — brief-mandated exceptions to the
`dcl_` prefix. `dcl_location_temperature`/`dcl_location_humidity` are not separate values; the
GUI binds the brief names directly.

### 4.4 Offline fitter (`tools/dcl_fit_coefficients.py`)

Uses numpy if importable, else stdlib least squares. Fits acos, wet-bulb, and T_offset/G/H per
climate (against monthly target curves stated in the script) by least squares refined by
Lawson minimax; prints max errors; emits
the constant blocks pasted into the script-value files; emits a per-climate × month reference
table used by live and save verification.

## 5. Macro weather fronts (approved)

- `weather_monthly_pulse` → `dcl_roll_macro_fronts`.
- Area classification: at `on_game_start` (and on load if missing), `every_area` sets
  `dcl_area_class` (1 warm, 2 temperate, 3 cold) from the climate of the capital location of the
  area's first province. Areas with no land province are never stamped (ocean areas excluded).
  - warm: tropical, subtropical, arid; temperate: mediterranean, oceanic, continental;
    cold: cold_arid, arctic.
- Each month: `random_list` chooses N ∈ {3, 4, 5}; each branch runs `while = { count = N }`
  with a literal N. Each iteration rolls a front type then `random_area` with limit
  `has_variable = dcl_area_class`, `NOT = { has_variable = dcl_front_type }`, climate fit:

| Front | `dcl_front_type` | Eligible classes | temp anomaly | humidity anomaly |
|---|---|---|---|---|
| Heatwave | 1 | warm, temperate | +8 | −20 |
| Polar Vortex | 2 | temperate, cold | −12 | −10 |
| Monsoon / Typhoon | 3 | warm | — | +40 |

- Duration: a `random_list` of literals 30, 35, …, 60 days passed as a scripted-effect parameter
  `$DAYS$` to `set_variable = { name = … value = … days = $DAYS$ }` for all front variables, so
  expiry clears them automatically.
- Fallback (if area-variable expiry proves non-functional in live test): `dcl_front_ttl`
  countdown −30 per pulse, `remove_variable` at ≤ 0.
- Area variables are area-scoped, not location-scoped, so they do not violate the
  one-location-variable rule.
- GUI shows the active front name; remaining days are not displayable.

## 6. Gameplay impact matrix (approved)

### 6.1 Monthly location pass

`dcl_evaluate_location_climate` (location scope, owned locations only):

1. `save_temporary_scope_value_as = { name = dcl_t value = dynamic_location_temperature }`,
   same for `dcl_h`.
2. If `scope:dcl_t >= 31`, cache `dcl_tw` from a pass-specific wet-bulb value reading the
   cached T/H.
3. For each condition, test the shared parameterised scripted trigger and apply the modifier with
   `add_location_modifier = { name = X months = 1 mode = replace }`.
4. On mud or drought, also apply `dcl_climate_stress_memory` with `months = 12 mode = replace`.

Parameterised triggers (shared by pass and GUI):
`dcl_mud_conditions = { T = … H = … }`, `dcl_rot_conditions`, `dcl_drought_conditions`,
`dcl_heat_exhaustion_conditions = { TW = … }`, `dcl_lethal_heat_conditions = { TW = … }`.

### 6.2 Location static modifiers

| Modifier | Trigger | Effects |
|---|---|---|
| `dcl_mud_season` | 1 ≤ T ≤ 8 AND H > 80 AND climate ≠ arctic AND topography ≠ mountains | `movement_cost = 0.40`, `local_supply_limit_modifier = -0.30`, `local_army_attrition = 3` |
| `dcl_tropical_rot` | T > 30 AND H > 85 | `local_malaria_growth_modifier = 0.25`, `local_typhus_growth_modifier = 0.15`, `local_food_decay_modifier = 0.25` |
| `dcl_severe_drought` | T > 32 AND H < 20 | `local_monthly_food_modifier = -0.35`, `local_construction_speed = -0.25`, `local_build_buildings_efficiency = -0.25` |
| `dcl_heat_exhaustion` | 31 ≤ Tw < 35 | `local_population_growth = -0.25`, `local_army_attrition = 5` |
| `dcl_lethal_heat` | Tw ≥ 35 | `local_population_growth = -0.75`, `local_army_attrition = 15` |
| `dcl_climate_stress_memory` | mud or drought fired (12 months) | none (flavour + drift input) |

### 6.3 Army pass

`monthly_country_pulse` → `every_army` → evaluate at `unit_location`
(`add_unit_modifier = { name months = 1 mode = replace }`):

| Unit modifier | Condition at army location | Effects |
|---|---|---|
| `dcl_heat_exhaustion_army` | 31 ≤ Tw < 35 | `land_unit_attrition = 0.10`, `land_morale_attrition_cost = 0.10` |
| `dcl_lethal_heat_army` | Tw ≥ 35 | `land_unit_attrition = 0.30`, `land_morale_attrition_cost = 0.25` |
| `dcl_expeditionary_rot` | rot conditions AND owner capital climate not tropical/subtropical | `land_unit_attrition = 0.15` |
| `dcl_mud_army` | mud conditions AND location unowned | `army_movement_speed = -0.40`, `land_unit_attrition = 0.03` |
| `dcl_rot_army` | rot conditions AND location unowned | `land_unit_attrition = 0.05` |

`has_owner` is used only in location scope (inside the `unit_location` hop).

## 7. Cultivation system (approved with hills+farmland amendment)

### 7.1 Initial ratio — `dcl_initial_cultivation` (first match wins)

| # | Condition | Ratio |
|---|---|---|
| 1 | topography wetlands | 0.05 |
| 2 | vegetation desert or sparse | 0.05 |
| 2b | topography hills AND vegetation farmland or grasslands | 0.30 |
| 3 | topography mountains, hills or plateau | 0.15 |
| 4 | vegetation woods, forest or jungle | 0.25 |
| 5 | vegetation farmland or grasslands | 0.60 |
| — | fallback | 0.30 |

### 7.2 Max arable — `dcl_max_arable` (same precedence)

wetlands 0.40 · desert/sparse 0.20 · hills (any vegetation) 0.60 · mountains 0.35 ·
plateau 0.50 · jungle 0.55 · woods/forest 0.70 · farmland/grasslands 0.95 · fallback 0.70.

### 7.3 Init and guards

- `on_game_start`: `every_country` → `every_owned_location` → `dcl_init_cultivation`
  (`set_variable cultivation_ratio = dcl_initial_cultivation`) + `dcl_apply_land_modifiers`.
- Monthly and yearly passes: `if = { limit = { NOT = { has_variable = cultivation_ratio } } … }`
  → same init. Covers old saves, conquest, colonisation.
- Unowned locations never receive the variable; GUI shows `dcl_cultivation_display`
  (variable if present, else lazy initial value) labelled "(unsettled)".

### 7.4 Yearly drift — `dcl_apply_land_drift`

- Farmer fill = `rgo_workers / max_rgo_workers`; locations with `max_rgo_workers = 0` skip the
  farmer tests.
- `dcl_land_drift_delta`:
  - −0.03 if `has_location_modifier = dcl_climate_stress_memory`;
  - −0.04 if farmer fill < 0.30 (stacks, total ≥ −0.07);
  - +0.02 if farmer fill > 0.80 AND food surplus (`market ?= { market_monthly_food_balance > 0 }`)
    AND no stress memory.
- Target = ratio + delta, capped at `dcl_max_arable` (only when delta > 0 — a location already
  above its cap after re-classification is never pushed up, only allowed to decay), floored at
  0.05; applied with the set-to-N idiom `change_variable = { name = cultivation_ratio add = target − ratio }`,
  then `clamp_variable = { name = cultivation_ratio min = 0.05 max = 0.95 }` (literal bounds).

### 7.5 Land modifiers (size-scaled, permanent)

`add_location_modifier = { name = X mode = replace size = <sv> }` (no duration), applied at init
and after each yearly drift.

| Modifier | Size | Effects at size 1 |
|---|---|---|
| `dcl_cultivated_land` | `var:cultivation_ratio` | `local_monthly_food_modifier = 0.30`, `local_production_efficiency = 0.10` |
| `dcl_wilderness_land` | `1 − var:cultivation_ratio` | `local_lumber_output_modifier = 0.30`, `local_hostile_attrition = 2` |

## 8. GUI and localization (approved with hover-card amendment)

### 8.1 Climate tab (`in_game/gui/location_window.gui`)

- Full vanilla copy + one tab button (`LocationView.Vars.Set('tab','dcl_climate')`) + one page
  gated by `LocationView.Vars.HasValue('tab','dcl_climate')`.
- Page: `scrollarea` → `vbox`; wrapping labels use `text_multi autoresize = yes
  layoutpolicy_horizontal = expanding`; cards use `bg_paper_card` + `bg_cabinet_card_frame`.
- Binding: `[GuiScope.SetRoot(LocationView.GetLocation.MakeScope).ScriptValue('<value>')|<fmt>]`.
- Weather card: temperature (°C, 1 decimal), humidity (%), front name (gated by
  `Between_float` on `dcl_location_front_type`). Tooltip: E_s (hPa), wet-bulb (only if T ≥ 20,
  else "n/a"), latitude/elevation/continentality proxies, anomaly contributions.
- Conditions card: five badges gated by 0/1 script values (`dcl_gui_mud_active`, …) that call the
  shared parameterised triggers with the lazy values; tooltip `[ShowModifierEffect('<id>')]`.
- Land card: gold/amber progressbar (value = cultivated share) over forest-green background
  (textures from `main_menu/gfx/interface/progressbars/`, else `color` tint); text line
  "Cultivated X% · Wilderness Y% (max arable Z%)"; tooltip with scaled land effects and drift
  causes (stress memory, farmer fill, food surplus, projected delta).

### 8.2 Hover card (`in_game/gui/shared/zz_dcl_location_tooltips.gui`)

Redefines `template tooltip_location_alt_content` (vanilla copy + one row):
"T °C · H % · Cultivated X%" plus the front name when active. The data context available inside
the template is confirmed during planning.

### 8.3 Localization

`main_menu/localization/english/dcl_climate_mod_l_english.yml`, UTF-8 BOM, `l_english:` header:
`STATIC_MODIFIER_NAME_/DESC_` for all 13 static modifiers; tab, card headers, front names,
condition badges, tooltip lines, drift causes, "(unsettled)", "n/a". Audit cross-checks all
referenced keys.

## 9. Verification (approved)

1. **Static audit** (`tools/dcl_audit.py`, run before each deploy): brace balance; BOM on all
   `.txt/.gui/.yml`; defs vs refs for script values, scripted triggers/effects, static modifiers,
   variables; loc keys referenced vs defined (and unused); effect/trigger/modifier keys vs
   `docs/*.log` and vanilla `00_modifier_types.txt`; forbidden patterns (`on_daily`,
   `every_location_in_the_world`, non-literal `clamp_variable` bounds, `change_*_modifier_size`,
   `fixed_range`/`integer_range` in formulas, any persistent location variable other than
   `cultivation_ratio`).
2. **Offline math** (`tools/dcl_fit_coefficients.py`): acos < 0.04 rad, Magnus < 0.1 hPa,
   wet-bulb < 0.5 °C (T 20–50, RH 5–100), monthly T means within ±2 °C of targets; emits
   reference table.
3. **Live engine checks** (one debug campaign session):

| Unknown | Check | Fallback |
|---|---|---|
| `current_day` as a value operand | GUI temperature changes within a month | mid-month +15 |
| temporary `scope:` value in triggers/math | modifiers land where expected | inline recomputation |
| `set_variable days =` on area | front vars vanish after 30–60 days | `dcl_front_ttl` countdown |
| GUI `ScriptValue` binding + hover-card context | values render | carrier modifier + `GetModifierValueFixed` |
| `unit_location`, owner capital climate link | unit modifiers appear | alternate capital link |
| `every_area` → first province capital | `dcl_area_class` in save | `random_province_in_area` capital |
| forestry key choice | lumber vs raw material output | pick one, document |

4. **Error-log gate**: new campaign, ≥ 13 months at speed 5; zero `dcl_` lines in `error.log`,
   no "Missing Icon", no missing loc; `Failed to read key reference` ignored as benign; logs
   copied out before relaunching.
5. **Save verification** (`tools/dcl_verify_save.py`): ratio ∈ [0.05, 0.95] and ≤ terrain cap on
   owned locations; no other `dcl_`/`eco_` persistent location variables; 3–5 areas with
   `dcl_front_type` per month-save and expiry across saves; land modifier sizes = ratio and
   1 − ratio; climate modifiers where the reference table predicts (e.g. mud near Novgorod in
   April, drought in the Sahel in summer); drift direction consistent with farmer fill and stress
   memory across consecutive autosaves.
6. **Deploy**: `python sync_mod.py dynamic-climate-land-use`; `debug.log` shows
   `Mounted Data: …/dynamic-climate-land-use/in_game`.

**Acceptance**: 1, 2, 4, 5 pass; every row of 3 verified or switched to its fallback.

## 10. Risks and limitations

- Two vanilla GUI overrides (`location_window.gui`, `tooltip_location_alt_content`) must be
  re-synced on game patches; other mods overriding the same files conflict.
- Latitude/elevation are coarse proxies; climate realism is per-climate, not per-location.
- Unowned locations get no location modifiers (only army mirrors) — intentional for performance.
- Monthly cost: one T/H evaluation per owned location per month (~polynomial arithmetic, no
  iteration); yearly drift one pass per owned location.
- Brief-mandated unprefixed names (`cultivation_ratio`, `macro_*`, brief script values) could
  collide with other mods using identical names.
