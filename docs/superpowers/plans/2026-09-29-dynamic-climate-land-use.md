# Dynamic Climate & Ecological Land Use — Implementation Plan

> **For agentic workers:** execute task-by-task. Each task has checkbox steps; tick them as you go.
> The spec is the source of truth for every value — read the cited section, do not re-derive.
> Read `C:\Kimi Workspace\AGENTS.md` (§4 engine knowledge base) before touching game script.

- **Goal:** ship the standalone EU5 1.3.11 mod `dynamic-climate-land-use` (lazy climate math,
  macro fronts, climate modifiers for locations and armies, cultivation system, GUI, loc) plus
  offline fitter, static audit and save verifier.
- **Architecture:** spec §2 — `weather_monthly_pulse` rolls fronts; `monthly_country_pulse`
  evaluates owned locations + armies and applies 1-month static modifiers; `yearly_country_pulse`
  drifts `var:cultivation_ratio` and re-applies size-scaled land modifiers; `on_game_start`
  initialises ratios and stamps area classes.
- **Tech stack:** Jomini script (script values, scripted triggers/effects, on_actions, static
  modifiers), PdxGui, Paradox YAML loc; Python 3 stdlib (numpy optional) for tools.
- **Spec:** `docs/specs/2026-09-29-dynamic-climate-land-use-design.md`
- **Mod root:** `C:\Kimi Workspace\dynamic-climate-land-use\` (below: paths relative to it).
- **Vanilla root:** `C:\Program Files (x86)\Steam\steamapps\common\Europa Universalis V\game\`
- **Engine registries:** `C:\Kimi Workspace\docs\{effects,triggers,modifiers,on_actions,event_targets,custom_localization}.log`

## Global constraints

1. Every `.txt/.gui/.yml` file is UTF-8 **with BOM**, 4-space indentation. Edit/Write strip the
   BOM — Task 12 re-prepends it; the audit fails without it.
2. No `on_daily*`, no `every_location_in_the_world`, no `change_*_modifier_size`, no
   `fixed_range`/`integer_range` inside formulas, `clamp_variable` bounds literal only.
3. The only persistent **location** variable is `cultivation_ratio`. Area variables
   (`macro_temp_anomaly`, `macro_humidity_anomaly`, `dcl_front_type`, `dcl_area_class`) are allowed.
4. No `is_ai` branches anywhere.
5. Every effect/trigger/modifier key used must exist in the registries or vanilla
   `main_menu/common/modifier_type_definitions/00_modifier_types.txt`; grep before use.
6. Every coefficient magnitude ≥ 1e-4 (fixed-point 1e-5).
7. No `# TODO` / placeholder comments; architectural comments are welcome.
8. Generator/patch scripts go into `.py` files (no long heredocs). Workspace is not a git repo;
   the mod repo has no git identity configured — do **not** commit or set identities.
9. Do not modify `eu5-ecology-mod` or vanilla files.

## Review focus

1. Scope correctness: `has_owner` only in location scope; `every_army` / `every_owned_location`
   only in country scope; `unit_location` hop before location triggers in the army pass.
2. Variable safety: no `var:` read before existence is guaranteed (area anomalies behind
   `area ?= { has_variable = … }`; `cultivation_ratio` behind the init guard).
3. Numeric fidelity: script-value constants match fitter output exactly; errors within spec §9.2.
4. Key existence: every modifier/effect/trigger key traced to a registry line (audit proves it).
5. GUI override hygiene: vanilla copies byte-identical except for the marked `dcl` insertions.

---

## Task 1 — Offline coefficient fitter

**Files:** create `tools/dcl_fit_coefficients.py`, `tools/test_dcl_fit.py`, output
`tools/dcl_reference_table.csv`.

**Interfaces:**
- `bhaskara_sin(x_rad) -> float`, `acos_poly(x, c) -> float`, `magnus_lowe(T) -> float`,
  `magnus_exact(T) -> float`, `stull_wet_bulb(T, RH) -> float`, `wb_surrogate(T, RH, c) -> float`
- `fit_acos() -> (c1, c3, c5, c7, max_err)`; `fit_wet_bulb() -> (c0..c5, max_err)`;
  `fit_temperature(climate) -> (T_offset, G, max_err)`; `model_temperature(climate, doy, coastal, southern, elev)`
- CLI: `python tools/dcl_fit_coefficients.py` prints error report + Jomini constant blocks and
  writes the CSV (climate × month × coastal → T, H, Tw, E_s); exit code 1 if any threshold fails.

**Steps:**
- [ ] Write `tools/test_dcl_fit.py` (unittest): Bhaskara max err < 0.002 over [0, 2π];
      acos fit < 0.01 rad over [−1, 1]; Lowe vs exact Magnus < 0.1 hPa over −40…50 °C
      (rescaled t = T/10 coefficients from spec §4.3.10); wet-bulb surrogate < 0.5 °C over
      T 20–50, RH 5–100; every climate's monthly means within ±2 °C of the target table;
      every emitted coefficient |c| ≥ 1e-4 or exactly 0. Run → fails (module missing).
- [ ] Implement the model exactly as spec §4.2/§4.3 (proxies table, lag, lagged declination,
      tan/cos series, ω_s/π, Q, T = T_offset + G·(Q − 0.5·cosφ) − elev·0.0065, humidity formula).
      Monthly target curves (°C, Jan..Dec, northern, non-coastal, elevation 100 m) are constants in
      the script — pick climatologically plausible curves per climate (e.g. continental ≈ Moscow,
      oceanic ≈ London, mediterranean ≈ Palermo, arid ≈ Cairo-inland, tropical ≈ 26–28 flat,
      subtropical ≈ Hangzhou, cold_arid ≈ Samarkand-high, arctic ≈ Arkhangelsk-north).
      If ±2 °C is unreachable with (T_offset, G) for a flat tropical curve, G may go small but
      must stay ≥ 1e-4.
- [ ] Least squares via numpy if importable, else stdlib normal equations (small Gaussian solver).
- [ ] Run tests → pass. Run CLI → paste its printed constant blocks into Task 3.

## Task 2 — Static audit tool

**Files:** create `tools/dcl_audit.py`, `tools/test_dcl_audit.py`.

**Interfaces:** `python tools/dcl_audit.py [mod_root]` → prints `PASS`/`FAIL <check>: <detail>`
lines, exit 1 on any failure. Functions: `check_bom`, `check_braces` (ignores braces in
comments/strings), `collect_defs` (top-level keys per folder), `check_refs` (script values,
scripted triggers/effects, static modifiers, loc keys incl. `STATIC_MODIFIER_NAME_/DESC_`),
`check_registry_keys` (effect/trigger keys vs `docs/effects.log`/`triggers.log`, modifier keys in
static modifiers vs `00_modifier_types.txt` + `docs/modifiers.log`), `check_forbidden`
(Global constraint 2 patterns; `set_variable`/`change_variable` names in location contexts other
than `cultivation_ratio` — heuristically: any `set_variable` whose enclosing scripted effect is not
tagged area-scope via a `# scope: area` header comment), `check_loc_unused` (warning only).

**Steps:**
- [ ] Write `tools/test_dcl_audit.py` with tmp-dir fixtures: missing BOM detected; unbalanced
      braces detected; undefined script value detected; unknown modifier key detected;
      `on_daily_pulse` detected; `clamp_variable = { … min = some_value }` detected; clean fixture
      passes. Run → fails.
- [ ] Implement; parse registries once (names are lines like `name:` / `## name` — inspect the
      log format first with Read). Run tests → pass.

## Task 3 — Script values

**Files:** create `in_game/common/script_values/dcl_climate_math_values.txt`,
`dcl_advanced_climate_math.txt`, `dcl_land_use_values.txt`.

**Interfaces (names are contracts used by later tasks and the GUI):**
- Proxies: `dcl_lat_tan` (signed), `dcl_lat_cos`, `dcl_lat_deg_display`, `dcl_elevation`,
  `dcl_continentality`, `dcl_humidity_base`, `dcl_temp_offset`, `dcl_temp_gain`.
- Calendar: `dcl_day_of_year`, `dcl_thermal_lag_days`.
- Advanced: `calc_solar_declination` (no lag), `dcl_lagged_declination`, `dcl_tan_declination`,
  `dcl_cos_declination`, `dcl_sunset_cos_arg` (clamped −1…1), `calc_day_length_factor`,
  `dcl_insolation`, `dynamic_saturation_vapor_pressure`, `wet_bulb_temperature` (lazy, reads the
  two brief values), `dcl_wet_bulb_cached` (reads `scope:dcl_t` / `scope:dcl_h`).
- Climate: `dynamic_location_temperature`, `dcl_location_temperature_mean`,
  `dynamic_location_humidity`, `dcl_temp_anomaly`, `dcl_humidity_anomaly` (0 when area var absent),
  `dcl_location_front_type` (0–3).
- Land: `dcl_initial_cultivation`, `dcl_max_arable`, `dcl_farmer_fill`, `dcl_land_drift_delta`,
  `dcl_cultivation_target`, `dcl_cultivation_set_delta` (target − ratio), `dcl_wilderness_ratio`,
  `dcl_cultivation_display`, `dcl_cultivation_percent`, `dcl_wilderness_percent`,
  `dcl_max_arable_percent`, GUI flags `dcl_gui_mud_active`, `dcl_gui_rot_active`,
  `dcl_gui_drought_active`, `dcl_gui_heat_exhaustion_active`, `dcl_gui_lethal_heat_active`,
  `dcl_gui_wet_bulb_available` (T ≥ 20).

**Steps:**
- [ ] Resolve unknowns by grep before writing: `current_day` / `current_month` in
      `docs/triggers.log` (value-operand usability — if `current_day` is trigger-only, use
      spec fallback +15 and note it); `hemisphere`, `climate`, `topography`, `vegetation`,
      `is_coastal`, `rgo_workers`, `max_rgo_workers`, `market_monthly_food_balance`,
      `area` link, `has_variable` scope support. Record findings for the final AGENTS.md snippet.
- [ ] Write the three files: flat `value = … add/multiply/…` rows with `if = { limit = … }`
      branches for per-climate/topography proxies (spec §4.2, §7.1, §7.2 tables verbatim),
      fitter constants from Task 1, clamping via `min`/`max` rows, polynomials via repeated
      `multiply` on sub-values (square/cube helpers allowed as separate script values).
- [ ] Mirror-check: run `tools/dcl_fit_coefficients.py` model on 3 sample inputs and hand-trace
      the script value rows for one (document trace in the task report, not in files).
- [ ] Run `tools/dcl_audit.py` (BOM failures expected until Task 12; brace/ref/key checks must pass
      for these files).

## Task 4 — Scripted triggers and effects

**Files:** create `in_game/common/scripted_triggers/dcl_climate_triggers.txt`,
`in_game/common/scripted_effects/dcl_climate_effects.txt`,
`in_game/common/scripted_effects/dcl_land_effects.txt`.

**Interfaces:**
- Triggers (location scope, `$T$`/`$H$`/`$TW$` parameters): `dcl_mud_conditions`,
  `dcl_rot_conditions`, `dcl_drought_conditions`, `dcl_heat_exhaustion_conditions`,
  `dcl_lethal_heat_conditions` (spec §6.2 thresholds); area triggers `dcl_area_fits_heatwave`,
  `dcl_area_fits_polar_vortex`, `dcl_area_fits_monsoon`; `dcl_owner_capital_is_tropical`.
- Effects: `dcl_evaluate_location_climate` (spec §6.1), `dcl_evaluate_army_climate` (spec §6.3,
  army scope), `dcl_roll_macro_fronts` (spec §5), `dcl_start_front = { TYPE DAYS }` (area scope),
  `dcl_stamp_area_class` (area scope), `dcl_init_cultivation`, `dcl_ensure_cultivation`
  (guard), `dcl_apply_land_drift`, `dcl_apply_land_modifiers`.

**Steps:**
- [ ] Resolve unknowns by grep: parameter substitution in scripted triggers (vanilla
      `in_game/common/scripted_triggers/` examples with `$X$` used as a comparison operand);
      `save_temporary_scope_value_as`; `set_variable` `days` key; `random_area`, `every_area`,
      `every_province_in_area` / `random_province_in_area`, province `capital`;
      `unit_location`, `add_unit_modifier`, `add_location_modifier` signatures; `random_list`
      and `while = { count = }`. If `$T$` cannot take a `scope:` value, pass script-value names
      instead (GUI passes `dynamic_location_temperature`, pass passes `scope:dcl_t`) — whichever
      form vanilla demonstrates; if neither is demonstrated, inline the conditions twice
      (documented fallback, spec §9.3 row 2).
- [ ] Write triggers, then effects. Front rolling: `random_list` over N ∈ {3,4,5} → literal
      `while = { count = N }` → `random_list` over front type → `random_area` with fit limit and
      `NOT = { has_variable = dcl_front_type }` → `random_list` over days 30,35,…,60 calling
      `dcl_start_front = { TYPE = … DAYS = … }`.
- [ ] Drift: exactly spec §7.4 (set-to-N idiom then literal `clamp_variable`), then
      `dcl_apply_land_modifiers`.
- [ ] Audit (non-BOM checks) passes.

## Task 5 — on_actions

**Files:** create `in_game/common/on_action/dcl_climate_macro_fronts.txt`,
`dcl_climate_monthly.txt`, `dcl_land_init.txt`, `dcl_land_drift_yearly.txt`.

**Steps:**
- [ ] Confirm folder name (`on_action` vs `on_actions`) in vanilla `in_game/common/` and the
      additive re-declaration pattern (`<pulse> = { effect = { … } }` or `on_actions = { … }`) via
      a vanilla example; confirm hook names in `docs/on_actions.log`.
- [ ] `weather_monthly_pulse` → `dcl_roll_macro_fronts`. `monthly_country_pulse` →
      `every_owned_location = { dcl_ensure_cultivation dcl_evaluate_location_climate }`,
      `every_army = { dcl_evaluate_army_climate }`. `yearly_country_pulse` →
      `every_owned_location = { dcl_ensure_cultivation dcl_apply_land_drift }`.
      `on_game_start` → country/location init + `every_area` stamping (only areas with a land
      province). `on_game_load_after_lobby` → stamp areas lacking `dcl_area_class`.
- [ ] If the pulse supports only `on_actions = {}` lists (no inline `effect`), create a named
      `dcl_*` on_action carrying the effect and reference it.
- [ ] Audit passes (non-BOM).

## Task 6 — Static modifiers

**Files:** create `main_menu/common/static_modifiers/dcl_climate_gameplay_effects.txt`,
`main_menu/common/static_modifiers/dcl_land_use_effects.txt`.

**Steps:**
- [ ] Copy the `game_data = { category = … }` shape from a vanilla location static modifier
      and a vanilla unit static modifier (grep vanilla `main_menu/common/static_modifiers/`).
- [ ] Write the 6 location modifiers (spec §6.2), 5 unit modifiers (spec §6.3), 2 land modifiers
      (spec §7.5) with the exact magnitudes. Decide forestry key: grep
      `local_lumber_output_modifier` vs `local_raw_material_output` in `00_modifier_types.txt`;
      keep lumber if it exists, document choice in README.
- [ ] Audit key checks pass.

## Task 7 — Localization

**Files:** create `main_menu/localization/english/dcl_climate_mod_l_english.yml`.

**Steps:**
- [ ] Keys: `STATIC_MODIFIER_NAME_/DESC_` for all 13 modifiers (descriptions state trigger
      thresholds and, for land, the scaling and drift causes); tab label; card headers; front
      names (`DCL_FRONT_NONE/HEATWAVE/POLAR_VORTEX/MONSOON`); badge labels; tooltip lines for
      E_s, wet-bulb, proxies, anomalies; drift-cause lines; `DCL_UNSETTLED`, `DCL_NOT_AVAILABLE`;
      hover-row text. Dynamic values use the same `GuiScope…ScriptValue(…)` bindings as the GUI
      where rendered in GUI tooltips.
- [ ] `l_english:` header, `KEY:0 "text"` form. Audit loc cross-check passes (after Task 8).

## Task 8 — GUI

**Files:** create `in_game/gui/location_window.gui` (full vanilla copy + patch),
`in_game/gui/shared/zz_dcl_location_tooltips.gui`; create `tools/dcl_patch_gui.py` (generator).

**Steps:**
- [ ] Resolve by grep in vanilla gui: an existing `ScriptValue(` usage and its scope form
      (`GuiScope.SetRoot(...)`), the location-window tab button + page idiom, the data context
      name inside `template tooltip_location_alt_content` (e.g. `Location.` vs
      `LocationView.`), `progressbar` widget shape. Textures: amber
      `gfx/interface/progressbars/progress_bar_goldish.dds`, green background
      `gfx/interface/progressbars/progress_bar_green.dds` (both exist in vanilla main_menu).
- [ ] `tools/dcl_patch_gui.py`: copies the vanilla files, inserts the dcl blocks at
      unique anchors with occurrence-count assertions (P&P pattern), writes UTF-8 BOM outputs.
      The tooltip file contains ONLY the redefined template (vanilla template body + one row).
- [ ] Page content per spec §8.1 (weather card, conditions card with 5 badges gated by
      `dcl_gui_*_active`, land card with dual progressbar and summary line); wrapping text via
      `text_multi` + `autoresize = yes` + `layoutpolicy_horizontal = expanding`.
- [ ] Run the generator; diff vanilla vs output shows only dcl insertions; audit passes.

## Task 9 — Descriptor and README

**Files:** create `.metadata/metadata.json`, `README.md`.

- [ ] Descriptor fields per AGENTS.md §5.1 (`supported_game_version: "1.3.11"`, tags, empty id).
- [ ] README: install, "disable eu5-ecology-mod", features, engine substitutions table
      (spec §1.2), limitations (spec §10), live-verification checklist (spec §9.3–9.5).

## Task 10 — sync_mod.py mod-name argument

**Files:** modify `C:\Kimi Workspace\sync_mod.py`.

- [ ] `python sync_mod.py [mod_name]`, default `eu5-ecology-mod`; SRC/DST derived from the name;
      docstring updated. Verify `python sync_mod.py --help`-free behaviour by dry reasoning only;
      real run happens in Task 14.

## Task 11 — Save verifier

**Files:** create `tools/dcl_verify_save.py`, `tools/test_dcl_verify_save.py`.

**Interfaces:** `python tools/dcl_verify_save.py <save.eu5> [more saves…]`; functions
`parse_locations_vars`, `parse_area_vars`, `parse_modifiers`, `check_ratio_bounds`,
`check_no_foreign_location_vars`, `check_front_count`, `check_land_sizes`, `check_drift`
(consecutive saves), `check_reference_predictions` (uses `tools/dcl_reference_table.csv`).
Save format per AGENTS.md §4.5/§4.8 (value × 100000 identities, omitted zero sizes).

- [ ] Tests with small synthetic save snippets (both present/omitted identity and size cases).
- [ ] Implement; tests pass. Live run is deferred to the user's campaign.

## Task 12 — BOM fix-up

- [ ] Python one-shot: every `.txt/.gui/.yml` under the mod root gets `\xef\xbb\xbf` prepended if
      missing (never doubled). Re-verify byte-level.

## Task 13 — Full audit + fitter + unit tests

- [ ] `python -m unittest discover tools` → all pass.
- [ ] `python tools/dcl_fit_coefficients.py` → exit 0.
- [ ] `python tools/dcl_audit.py` → exit 0 (only loc-unused warnings allowed).

## Task 14 — Deploy

- [ ] `python C:\Kimi Workspace\sync_mod.py dynamic-climate-land-use`; confirm the deployed
      tree matches the workspace (file count + one BOM spot-check). In-game checks (spec §9.3–9.5)
      are handed to the user.
