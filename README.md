# Dynamic Climate & Ecological Land Use

A standalone Europa Universalis V (1.3.11) mod that simulates lazy, formula-driven
climate — temperature, humidity, vapour pressure, wet-bulb — plus regional macro
weather fronts, climate-driven gameplay modifiers for locations and armies, and a
per-location cultivated-vs-wilderness land-use system.

> **Disable `eu5-ecology-mod` ("Ecology Simulation") before enabling this mod.**
> This mod **replaces** it. The two simulate the same systems, write overlapping
> state and are not compatible — there is no shared identifier and no compatibility
> layer. Running both will double-apply climate effects and corrupt benchmark data.

## Features

- **Lazy climate model** — per-location temperature and relative humidity are pure
  script values computed on demand from climate, topography, coast, calendar day and
  active weather fronts. No stored climate state anywhere; the monthly pass evaluates
  each owned location once and caches the result for that pass only.
- **Full physics chain** — solar declination (Bhaskara sine), day length (fitted acos
  polynomial), insolation, thermally lagged seasonal temperature, saturation vapour
  pressure (Lowe 1977 Magnus polynomial) and wet-bulb temperature (fitted Stull 2011
  surrogate, capped at the dry-bulb value). All transcendental functions run as fitted
  polynomials because EU5 script has no `sin`/`acos`/`exp`.
- **Macro weather fronts** — once per month a global pulse rolls 3–5 fronts across
  climate-classified areas: heatwaves (+8 °C, −20 % humidity), polar vortices
  (−12 °C, −10 %) and monsoons/typhoons (+40 % humidity), each lasting 30–60 days.
- **Climate gameplay modifiers** — mud season, tropical rot, severe drought, heat
  exhaustion and lethal heat apply monthly-refreshed location modifiers; armies get
  mirror unit modifiers (attrition, morale, movement) evaluated at their position,
  including expeditionary rot for troops campaigning far from a tropical homeland.
- **Climate-stress memory** — mud and drought leave a 12-month marker on the land that
  feeds the land-use drift (no extra variables: it is a timed modifier).
- **Cultivation vs wilderness** — every owned location carries one persistent variable,
  `cultivation_ratio` (literal-clamped to 0.05–0.95, initialised from terrain, capped
  by terrain). Size-scaled permanent modifiers reward cultivated land (food,
  production efficiency) and wilderness (lumber, hostile attrition). Wilderness share
  is always derived, never stored.
- **Yearly land drift** — climate-stressed or under-farmed land reverts to wilderness
  (−0.03 / −0.04 per year, stacking); well-farmed land with a market food surplus
  expands cultivation (+0.02).
- **Climate tab** in the location window: current temperature/humidity, active front,
  hazard badges with live effect tooltips, and a cultivated/wilderness progress bar
  with drift causes — plus a one-line climate row in the map hover tooltip.
- **Symmetric simulation** — all logic runs in the country pulses for every country;
  there are no `is_ai` branches. Unowned locations intentionally receive no location
  modifiers (armies there still get mirror modifiers) to bound the monthly cost.

## Installation

1. Copy (or deploy) this folder as
   `Documents/Paradox Interactive/Europa Universalis V/mod/dynamic-climate-land-use/`.
   From this workspace the deploy command is:
   `python C:\Kimi Workspace\sync_mod.py dynamic-climate-land-use`
2. **Disable "Ecology Simulation" (eu5-ecology-mod)** in the launcher — see the
   warning above.
3. Enable "Dynamic Climate & Ecological Land Use" in the launcher and start a
   campaign. Works on existing saves: cultivation and area classes initialise
   themselves through monthly/yearly guards.

## Engine-reality substitutions

EU5 1.3.11 has no `on_daily_tick`/`on_monthly_tick`, no triggered-modifier folder, no
latitude/elevation data and no transcendental math. The brief's concepts map onto real
engine constructs as follows (design spec §1.2):

| Brief | Implemented as |
|---|---|
| `on_monthly_tick` | `monthly_country_pulse` (country scope) + `weather_monthly_pulse` (global, once/month) |
| `on_yearly_tick` | `yearly_country_pulse` |
| `common/triggered_modifiers/` | Static modifiers in `main_menu/common/static_modifiers/`, applied by the monthly pass with `months = 1 mode = replace` |
| `current_day_of_year` | Script value `dcl_day_of_year` = 12-branch `current_month` offset + `current_day` (fallback: +15) |
| `sin`, `cos`, `tan`, `acos`, `exp` | Polynomial/rational approximations (Bhaskara sine, Taylor series, odd-polynomial acos, Lowe 1977 Magnus polynomial) |
| `location_latitude` | Per-climate \|φ\| proxy, signed by `hemisphere` |
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
| `local_forestry_goods_mult` | `local_lumber_output_modifier` (see below) |
| `enemy_army_attrition` | `local_hostile_attrition` (already_percent) |
| expeditionary attrition | Unit modifier `land_unit_attrition` via army pass |
| Köppen zone | Vanilla climate key (8 values) |
| terrain Farmlands/Forests/... | Vanilla `vegetation` + `topography` keys |

**Forestry key decision:** wilderness buffs `local_lumber_output_modifier`, not the
generic `local_raw_material_output`. Both keys exist; lumber is the goods-specific one,
so wilderness rewards standing woodland only. The generic key would also buff mines
and every other raw RGO — the wrong fantasy and a broader balance footprint.

## Limitations (design spec §10)

- Two vanilla GUI overrides (`location_window.gui`, `tooltip_location_alt_content`)
  must be re-synced on game patches; other mods overriding the same files conflict.
- Latitude/elevation are coarse per-climate proxies; climate realism is per-climate,
  not per-location.
- Unowned locations get no location modifiers (only army mirrors) — intentional, for
  performance.
- Monthly cost: one temperature/humidity evaluation per owned location per month
  (~polynomial arithmetic, no iteration); yearly drift is one pass per owned location.
- Brief-mandated unprefixed names (`cultivation_ratio`, `macro_*`, brief script values)
  could collide with other mods using identical names.

## Live-verification checklist (design spec §9.3–9.5)

One debug campaign session covers the engine unknowns; each row has a fallback if the
check fails:

| Unknown | Check | Fallback |
|---|---|---|
| `current_day` as a value operand | GUI temperature changes within a month | mid-month +15 |
| temporary `scope:` value in triggers/math | modifiers land where expected | inline recomputation |
| `set_variable days =` on area | front vars vanish after 30–60 days | `dcl_front_ttl` countdown |
| GUI `ScriptValue` binding + hover-card context | values render | carrier modifier + `GetModifierValueFixed` |
| `unit_location`, owner capital climate link | unit modifiers appear | alternate capital link |
| `every_area` → first province capital | `dcl_area_class` in save | `random_province_in_area` capital |
| forestry key choice | lumber vs raw material output | decided: lumber (documented above) |

Error-log gate:

- [ ] New campaign, ≥ 13 months at speed 5.
- [ ] Zero `dcl_` lines in `error.log`; no "Missing Icon"; no missing localization.
- [ ] `Failed to read key reference` lines ignored (benign engine noise).
- [ ] Logs copied out before relaunching (they are overwritten on every launch).
- [ ] Deploy actually loaded: `debug.log` contains `Mounted Data: .../dynamic-climate-land-use/in_game`
      (and `/main_menu`) and a `successfully matched game version` line for the mod — a
      stale/misnamed deploy is otherwise invisible behind a clean error.log.

Save verification (`tools/dcl_verify_save.py` against autosaves):

- [ ] `cultivation_ratio` ∈ [0.05, 0.95] and ≤ terrain cap on every owned location.
- [ ] No persistent location variables other than `cultivation_ratio`.
- [ ] 3–5 fresh fronts per month (up to ~15 areas with `dcl_front_type` alive counting
      30–60-day overlap); fronts expire across saves.
- [ ] Land modifier sizes equal the ratio and 1 − ratio.
- [ ] Climate modifiers present where the reference table predicts them (e.g. mud near
      Novgorod in April, drought in the Sahel in summer).
- [ ] Drift direction matches farmer fill and stress memory across consecutive autosaves.

## Development tooling (repo only, not shipped to the game)

- `tools/dcl_fit_coefficients.py` — offline fitter for every polynomial constant and
  per-climate temperature curve; emits the constant blocks and the reference table.
- `tools/dcl_audit.py` — static audit (braces, BOM, defs/refs, loc keys, registry keys,
  forbidden patterns). Must be fully green before any deploy.
- `tools/dcl_fix_bom.py` — re-prepends the UTF-8 BOM that editors strip from game files.
- `tools/dcl_verify_save.py` — savegame assertions for the checklist above.
- `python -m unittest discover tools` — offline test suite.
