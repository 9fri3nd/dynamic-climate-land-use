"""Static audit for the Dynamic Climate & Land Use mod.

Usage: python tools/dcl_audit.py [mod_root] [--docs DIR] [--vanilla DIR]

Prints one ``PASS <check>: N files checked`` line per clean check, or one
``FAIL <check>: <detail>`` line per finding, followed by ``WARN`` lines.
Exit code is 1 when any FAIL was printed, else 0.

Scanned files: ``.txt``/``.gui``/``.yml`` below ``in_game``, ``main_menu`` and
``loading_screen`` (dot-dirs and ``__pycache__`` pruned). Categories come from
``<root>/common/<dir>``: script_values, scripted_triggers, scripted_effects,
on_action(s), static_modifiers, modifier_type_definitions; ``.yml`` is loc,
``.gui`` is gui, anything else is "other" (token checks only).

Registries (``--docs``, default ``<workspace>/docs`` or ``$DCL_DOCS_DIR``):
  effects.log / triggers.log   ``## name`` headings
  event_targets.log            ``### name`` headings (duplicates collapse)
  modifiers.log                ``Tag: name, Categories: ...`` lines
  on_actions.log               the first non-blank ``name:`` line after each
                               ``-----`` separator
Vanilla definitions (``--vanilla``, default Steam install or
``$DCL_VANILLA_ROOT``): column-0 ``name =`` keys in
``{in_game,main_menu}/common/<category dir>/**/*.txt``. A missing docs file is a
registry_keys FAIL. A missing vanilla root is a single WARN: names that could
only be vanilla definitions are then not checked (anything not prefixed
``dcl_`` that is absent from the docs registries is accepted), while ``dcl_``
names are still checked. WARN lines never affect the exit code.

Names containing ``$`` (scripted-effect parameters) are skipped by every
reference, variable and key check.

Checks and their heuristics:
  bom            every scanned file starts with the UTF-8 BOM.
  braces         lenient tokenizer/parser on .txt/.gui; comments and quoted
                 strings are ignored; reports stray '}' and unclosed '{'.
  refs           (1) bare identifiers in value positions (value/add/.../size/
                 days/count with '=' and '< <= > >=' comparisons; '==' and '!='
                 are skipped because they compare enums) must be a script value,
                 event target or trigger; (2) ``dcl_*`` keys below top level must
                 be mod scripted triggers/effects/script values; (3) modifier
                 names in add_/remove_*_modifier and has_*_modifier must be
                 static modifiers; (4) on_actions lists and ``on_action =`` must
                 name known on_actions; (5) ScriptValue('x'),
                 ShowModifierEffect('x'), dcl-prefixed Localize('x'), gui
                 text/tooltip = "dcl_*", yml $dcl_*$ and script
                 desc/custom_tooltip/text/title/tooltip = dcl_* must resolve;
                 (6) each mod static modifier has STATIC_MODIFIER_NAME_/DESC_
                 loc; (7) every variable read (var:x, GetVariable('x'),
                 Var('x'), has_/remove_/clamp_variable) is written by some
                 set_variable/change_variable in the mod.
  registry_keys  scripted effects/triggers, script values and on_action
                 effect/trigger blocks are walked with an effect/trigger/value
                 context; every key must be a registered effect/trigger/event
                 target, a known control keyword, a REGISTRY_GAPS entry
                 (vanilla-used keys missing from the docs dumps), or a
                 mod/vanilla scripted definition. Registered effects with a
                 block value are parameter blocks and are not descended; the
                 block right-hand side of a '< <= > >= == !=' comparison is
                 walked as a script value; root/prev/this/from (any case) act
                 as scope links. Static modifier keys must be vanilla modifier
                 types, modifiers.log tags or mod modifier types.
                 Keys that are quoted, contain '$', start with '@' or 'dcl_' are
                 skipped (dcl_ calls are covered by refs).
  forbidden      (.txt and .yml only; .gui files are not scanned; comments
                 excluded from the token scan) on_daily*,
                 every_location_in_the_world, change_*_modifier_size, is_ai;
                 fixed_range/integer_range outside a set_/change_*variable;
                 clamp_*variable min/max bounds that are not numeric literals
                 (strings such as var:x and block bounds both fail);
                 comments of the form ``# TODO``/``# FIXME``/``# placeholder``
                 (the word must directly follow '#'; also .yml comment lines);
                 in scripted effects and on_actions every set_/change_variable
                 is classified as area or location context: the innermost
                 ancestor matching an area scope key (area, every_area, ... but
                 not *_in_area) or a location scope key (location,
                 every_location_in_area, location:x, ...) decides; with neither,
                 the definition is area context iff it carries a
                 ``# scope: area`` comment (between the previous definition and
                 its own line, or trailing on it). Area context may only write
                 AREA_VARIABLES; location context only cultivation_ratio.
  loc_unused     WARN only: loc keys referenced by no mod file and no other loc
                 value (static-modifier/modifier-type keys of defined ids and
                 ``*_desc`` keys of used keys count as used).
"""
import argparse
import functools
import os
import re
import sys
from pathlib import Path

CHECKS = ("bom", "braces", "refs", "registry_keys", "forbidden")
SCAN_ROOTS = ("in_game", "main_menu", "loading_screen")
EXTS = (".txt", ".gui", ".yml")
DEFAULT_VANILLA = "C:/Program Files (x86)/Steam/steamapps/common/Europa Universalis V/game"
COMMON_CATEGORIES = {
    "script_values": "script_values",
    "scripted_triggers": "scripted_triggers",
    "scripted_effects": "scripted_effects",
    "on_action": "on_action",
    "on_actions": "on_action",
    "static_modifiers": "static_modifiers",
    "modifier_type_definitions": "modifier_types",
}
DEF_CATEGORIES = ("script_values", "scripted_triggers", "scripted_effects",
                  "on_action", "static_modifiers", "modifier_types")

TOKEN_RE = re.compile(r"""
    (?P<nl>\n)
  | (?P<ws>[ \t\r\f\v]+)
  | (?P<comment>\#[^\n]*)
  | (?P<str>"(?:[^"\\\n]|\\.)*"?)
  | (?P<open>\{)
  | (?P<close>\})
  | (?P<op>\?=|<=|>=|!=|==|=|<|>)
  | (?P<word>[^\s{}=<>!?"\#]+)
  | (?P<other>.)
""", re.X)
COLOR_TAGS = {"rgb", "hsv", "hsv360", "hex"}
LOC_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_.\-]+):\d*\s*"(.*)$')
VANILLA_DEF_RE = re.compile(r"^([A-Za-z_]\w*)\s*=", re.M)
NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
BARE_RE = re.compile(r"^[A-Za-z_]\w*$")

CONTROL_EFFECTS = {"if", "else_if", "else", "while", "hidden_effect", "random", "random_list"}
EFFECT_SKIP = {"count", "chance", "modifier"}
LOGIC_TRIGGERS = {"AND", "OR", "NOT", "NOR", "NAND"}
TRIGGER_CONTROL = {"trigger_if", "trigger_else_if", "trigger_else", "calc_true_if"}
TRIGGER_SKIP = {"count", "percent", "amount"}
VALUE_CONTROL = {"if", "else_if", "else"}
# Vanilla-cited script-value row operators: abs (auto_modifiers/country.txt:733, RHS ignored),
# pow (generic_actions/chinese_treasure_voyages.txt:106), modulo (heir_selections/monarchy.txt:1068),
# round (missions/generic_infrastructure_mission_pack.txt.txt:213), floor
# (generic_actions/the_revolution.txt:519), ceiling (disasters/court_and_country.txt:42).
VALUE_OPS = {"value", "add", "subtract", "multiply", "divide", "modulo", "min", "max",
             "abs", "round", "floor", "ceiling", "pow"}
VALUE_SKIP = {"desc", "fixed_range", "integer_range", "save_temporary_value_as", "save_temporary_scope_as"}
ITER_PREFIXES = ("every_", "random_", "ordered_", "any_")
ITER_PARAMS = {"order_by", "position", "max", "min", "check_range_bounds", "count", "percent", "weight",
               "variable", "list", "type"}
SCOPE_KEYWORDS = {"root", "prev", "this", "from"}
TOOLTIP_PARAMS = {"text", "subject"}
LIMIT_KEYS = {"limit", "alternative_limit"}
ON_ACTION_KEYS = {"on_actions", "events", "random_events", "first_valid", "first_valid_on_action",
                  "random_on_action", "trigger", "effect", "weight_multiplier", "fallback"}

VALUE_POS_KEYS = {"value", "add", "subtract", "multiply", "divide", "modulo", "min", "max",
                  "pow", "size", "factor", "days", "months", "years", "count"}
COMPARE_OPS = {"<", "<=", ">", ">="}
BARE_VALUE_SKIP = {"yes", "no", "all"}
LOC_SCRIPT_KEYS = {"desc", "custom_tooltip", "text", "title", "localization_key", "tooltip"}
DCL_KEY_RE = re.compile(r"^dcl_\w*$")
DCL_LOC_RE = re.compile(r"^(dcl|DCL)_[\w.]*$")
ADD_MOD_RE = re.compile(r"^(add|remove)_\w+_modifier$")
HAS_MOD_RE = re.compile(r"^has_\w+_modifier$")
VAR_WRITE_KEYS = {"set_variable", "change_variable"}
VAR_READ_KEYS = {"has_variable", "remove_variable", "clamp_variable"}
VAR_LINK_RE = re.compile(r"\bvar:(\w+)(?![\w$])")
GET_VARIABLE_RE = re.compile(r"(?:GetVariable|\bVar)\(\s*'(\w+)'")
SCRIPT_VALUE_RE = re.compile(r"ScriptValue\(\s*'(\w+)'")
SHOW_MODIFIER_RE = re.compile(r"ShowModifierEffect\(\s*'(\w+)'")
LOCALIZE_RE = re.compile(r"Localize\(\s*'([\w.]+)'")
GUI_TEXT_RE = re.compile(r'(?:text|tooltip|raw_text|raw_tooltip)\s*=\s*"((?:dcl|DCL)_[\w.]+)"')
YML_MACRO_RE = re.compile(r"\$((?:dcl|DCL)_[\w.]+)\$")

LOCATION_VARIABLE = "cultivation_ratio"
AREA_VARIABLES = {"macro_temp_anomaly", "macro_humidity_anomaly", "dcl_front_type", "dcl_area_class",
                  "dcl_front_ttl"}
# Iterators used by vanilla script but missing from the docs/*.log registries
# (any_courtier: game/in_game/common/on_action/settle_the_frontier_monthly.txt).
REGISTRY_GAPS = {"any_courtier"}
AREA_TAG_RE = re.compile(r"#\s*scope:\s*area\b")
AREA_SCOPE_RE = re.compile(r"^(?!.*_in_area$)(\w*_)?area$")
LOCATION_SCOPE_RE = re.compile(r"^((\w+_)?locations?(_in_\w+)?|location:\w+)$")
FORBIDDEN_COMMENT_RE = re.compile(r"#\s*(TODO|FIXME|placeholder)\b", re.I)
MODIFIER_SIZE_RE = re.compile(r"^change_\w*_modifier_size$")
CLAMP_RE = re.compile(r"^clamp_(\w+_)?variable$")
VAR_EFFECT_RE = re.compile(r"^(set|change)_(\w+_)?variable$")
LOC_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")


class Node:
    __slots__ = ("key", "op", "value", "line", "end")

    def __init__(self, key, op, value, line, end=None):
        self.key = key
        self.op = op
        self.value = value
        self.line = line
        self.end = line if end is None else end

    @property
    def is_block(self):
        return isinstance(self.value, list)

    @property
    def is_item(self):
        return self.op is None and self.value is None

    def child(self, key):
        if self.is_block:
            for c in self.value:
                if c.key == key:
                    return c
        return None


def tokenize(text):
    tokens, comments = [], []
    line = 1
    for m in TOKEN_RE.finditer(text):
        kind, tok = m.lastgroup, m.group()
        if kind == "nl":
            line += 1
        elif kind == "ws":
            continue
        elif kind == "comment":
            comments.append((line, tok))
        else:
            tokens.append(("word" if kind == "other" else kind, tok, line))
    return tokens, comments


def parse(tokens):
    """Lenient Paradox-script parser: never raises, records (line, msg) errors."""
    errors, root, stack = [], [], []
    current = root
    n = len(tokens)

    def peek(j):
        return tokens[j] if j < n else (None, None, None)

    def open_block(node):
        nonlocal current
        current.append(node)
        stack.append(node)
        current = node.value

    i = 0
    while i < n:
        kind, text, line = tokens[i]
        if kind == "close":
            if stack:
                stack.pop().end = line
                current = stack[-1].value if stack else root
            else:
                errors.append((line, "unmatched '}'"))
            i += 1
        elif kind == "open":
            open_block(Node(None, None, [], line))
            i += 1
        elif kind == "op":
            i += 1
        else:
            k2, t2, _ = peek(i + 1)
            if k2 == "op":
                k3, t3, _ = peek(i + 2)
                if k3 == "open":
                    open_block(Node(text, t2, [], line))
                    i += 3
                elif k3 in ("word", "str"):
                    if k3 == "word" and t3.lower() in COLOR_TAGS and peek(i + 3)[0] == "open":
                        open_block(Node(text, t2, [], line))
                        i += 4
                    else:
                        current.append(Node(text, t2, t3, line))
                        i += 3
                else:
                    current.append(Node(text, t2, None, line))
                    i += 2
            elif k2 == "open":
                open_block(Node(text, None, [], line))
                i += 2
            else:
                current.append(Node(text, None, None, line))
                i += 1
    for node in stack:
        errors.append((node.line, "unclosed '{'"))
    errors.sort()
    return root, errors


def walk(nodes, ancestors=()):
    for node in nodes:
        yield node, ancestors
        if node.is_block:
            yield from walk(node.value, ancestors + (node,))


def unquote(text):
    if text and len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def categorize(rel, ext):
    if ext == ".yml":
        return "loc"
    if ext == ".gui":
        return "gui"
    parts = rel.split("/")
    if len(parts) >= 4 and parts[1] == "common":
        return COMMON_CATEGORIES.get(parts[2], "other")
    return "other"


class ModFile:
    def __init__(self, root, path):
        raw = path.read_bytes()
        self.rel = path.relative_to(root).as_posix()
        self.has_bom = raw.startswith(b"\xef\xbb\xbf")
        self.text = raw.decode("utf-8-sig", errors="replace")
        self.ext = path.suffix.lower()
        self.category = categorize(self.rel, self.ext)
        self.tokens, self.comments, self.nodes, self.brace_errors = [], [], [], []
        self.loc = {}
        if self.ext == ".yml":
            for lineno, ln in enumerate(self.text.splitlines(), 1):
                if ln.lstrip().startswith("#"):
                    self.comments.append((lineno, ln.strip()))
                    continue
                m = LOC_LINE_RE.match(ln)
                if m:
                    self.loc[m.group(1)] = (lineno, m.group(2))
        else:
            self.tokens, self.comments = tokenize(self.text)
            self.nodes, self.brace_errors = parse(self.tokens)


def scan_mod(root):
    root = Path(root)
    files = []
    for top in SCAN_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
            for name in sorted(filenames):
                if os.path.splitext(name)[1].lower() in EXTS:
                    files.append(ModFile(root, Path(dirpath) / name))
    return files


def collect_defs(files):
    defs = {cat: set() for cat in DEF_CATEGORIES}
    for f in files:
        if f.category not in defs:
            continue
        for node in f.nodes:
            if node.key and not node.key.startswith("@") and not node.is_item:
                defs[f.category].add(node.key)
    return defs


class Registry:
    def __init__(self):
        self.effects = set()
        self.triggers = set()
        self.event_targets = set()
        self.modifier_tags = set()
        self.on_actions = set()
        self.vanilla = {cat: set() for cat in DEF_CATEGORIES}
        self.fails = []
        self.warns = []
        self.vanilla_missing = False


def vanilla_blind(reg, name):
    """True when a lookup miss is inconclusive because vanilla definitions are unavailable."""
    return reg.vanilla_missing and not name.startswith(("dcl_", "DCL_"))


@functools.lru_cache(maxsize=None)
def load_registry(docs, vanilla):
    reg = Registry()
    docs_dir = Path(docs)

    def read(name):
        path = docs_dir / name
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            reg.fails.append("%s: registry file missing" % path.as_posix())
            return ""

    reg.effects = set(re.findall(r"^##[ \t]+(\S+)", read("effects.log"), re.M))
    reg.triggers = set(re.findall(r"^##[ \t]+(\S+)", read("triggers.log"), re.M))
    reg.event_targets = set(re.findall(r"^###[ \t]+(\S+)", read("event_targets.log"), re.M))
    reg.modifier_tags = set(re.findall(r"^Tag:[ \t]*([^,\s]+)", read("modifiers.log"), re.M))
    lines = read("on_actions.log").splitlines()
    for idx, ln in enumerate(lines):
        if ln.startswith("-----"):
            following = [x for x in lines[idx + 1:idx + 4] if x.strip()]
            m = re.match(r"^\s*([A-Za-z0-9_]+):\s*$", following[0]) if following else None
            if m:
                reg.on_actions.add(m.group(1))

    vroot = Path(vanilla)
    if not vroot.is_dir():
        reg.vanilla_missing = True
        reg.warns.append("registry: vanilla root not found: %s (vanilla-defined names not checked)"
                         % vroot.as_posix())
        return reg
    for top in ("in_game", "main_menu"):
        for folder, cat in COMMON_CATEGORIES.items():
            base = vroot / top / "common" / folder
            if not base.is_dir():
                continue
            for path in base.rglob("*.txt"):
                try:
                    text = path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                reg.vanilla[cat].update(VANILLA_DEF_RE.findall(text))
    return reg


def fmt(f, line, msg):
    return "%s:%d: %s" % (f.rel, line, msg)


def check_bom(files):
    return ["%s: missing UTF-8 BOM" % f.rel for f in files if not f.has_bom]


def check_braces(files):
    return [fmt(f, line, msg) for f in files if f.ext in (".txt", ".gui") for line, msg in f.brace_errors]



class KeyChecker:
    """Walks a definition body in an effect/trigger/value context."""

    def __init__(self, reg, defs, f, out):
        self.reg = reg
        self.defs = defs
        self.f = f
        self.out = out
        self.effect_defs = defs["scripted_effects"] | reg.vanilla["scripted_effects"]
        self.trigger_defs = (defs["scripted_triggers"] | reg.vanilla["scripted_triggers"]
                             | defs["script_values"] | reg.vanilla["script_values"])

    def fail(self, node, msg):
        self.out.append(fmt(self.f, node.line, msg))

    def is_iterator(self, key):
        return (key.startswith(ITER_PREFIXES) and key != "random_list"
                and (key in self.reg.effects or key in self.reg.triggers or key in REGISTRY_GAPS))

    def walk_block(self, node, ctx, skip=frozenset()):
        if node.is_block:
            self.walk(node.value, ctx, skip)

    def walk_weighted(self, node):
        for c in node.value:
            if c.key == "trigger":
                self.walk_block(c, "trigger")
            elif c.key == "modifier":
                continue
            elif c.key is None and c.is_block:
                self.walk(c.value, "effect")
            elif c.key is not None:
                self.walk([c], "effect")

    def walk(self, nodes, ctx, skip=frozenset()):
        for node in nodes:
            key = node.key
            if key is None:
                if node.is_block:
                    self.walk(node.value, ctx)
                continue
            if node.is_item or key in skip or key.startswith(('"', "@", "dcl_")) or "$" in key:
                continue
            if NUMERIC_RE.match(key):
                if node.is_block:
                    self.walk_weighted(node)
                continue
            compared = node.is_block and node.op in COMPARE_OPS | {"==", "!="}
            if ":" in key or "." in key:
                head = re.split(r"[.:]", key, maxsplit=1)[0]
                if head not in self.reg.event_targets and head.lower() not in SCOPE_KEYWORDS:
                    self.fail(node, "unknown scope link '%s'" % key)
                else:
                    self.walk_block(node, "value" if compared else ctx)
                continue
            if key.lower() in SCOPE_KEYWORDS:
                self.walk_block(node, ctx)
                continue
            if compared:
                self.walk(node.value, "value")
                continue
            if key in LIMIT_KEYS:
                self.walk_block(node, "trigger")
                continue
            if self.is_iterator(key):
                self.walk_block(node, ctx, ITER_PARAMS)
                continue
            getattr(self, "visit_" + ctx)(node, key)

    def visit_effect(self, node, key):
        reg = self.reg
        if key in CONTROL_EFFECTS:
            self.walk_block(node, "effect")
        elif key == "custom_tooltip":
            self.walk_block(node, "effect", TOOLTIP_PARAMS)
        elif (key in EFFECT_SKIP or key in reg.effects or key in self.effect_defs
              or key in REGISTRY_GAPS):
            return
        elif key in reg.event_targets:
            self.walk_block(node, "effect")
        elif not vanilla_blind(reg, key):
            self.fail(node, "unknown effect key '%s'" % key)

    def visit_trigger(self, node, key):
        reg = self.reg
        if key in LOGIC_TRIGGERS or key in TRIGGER_CONTROL:
            self.walk_block(node, "trigger")
        elif key == "custom_tooltip":
            self.walk_block(node, "trigger", TOOLTIP_PARAMS)
        elif (key in TRIGGER_SKIP or key in reg.triggers or key in self.trigger_defs
              or key in REGISTRY_GAPS):
            return
        elif key in reg.event_targets:
            self.walk_block(node, "trigger")
        elif not vanilla_blind(reg, key):
            self.fail(node, "unknown trigger key '%s'" % key)

    def visit_value(self, node, key):
        if key in VALUE_CONTROL:
            self.walk_block(node, "value")
        elif key in VALUE_OPS:
            self.walk_block(node, "value")
        elif key in VALUE_SKIP:
            return
        elif key in self.reg.event_targets:
            self.walk_block(node, "value")
        else:
            self.fail(node, "unknown script value key '%s'" % key)


def check_registry_keys(files, defs, reg):
    out = []
    known_modifiers = reg.vanilla["modifier_types"] | reg.modifier_tags | defs["modifier_types"]
    for f in files:
        kc = KeyChecker(reg, defs, f, out)
        for node in f.nodes:
            if node.key is None or node.key.startswith("@") or not node.is_block:
                continue
            if f.category == "scripted_effects":
                kc.walk(node.value, "effect")
            elif f.category == "scripted_triggers":
                kc.walk(node.value, "trigger")
            elif f.category == "script_values":
                kc.walk(node.value, "value")
            elif f.category == "on_action":
                for c in node.value:
                    if c.key is None or c.is_item:
                        continue
                    if c.key not in ON_ACTION_KEYS:
                        kc.fail(c, "unknown on_action key '%s'" % c.key)
                    elif c.key == "effect":
                        kc.walk_block(c, "effect")
                    elif c.key == "trigger":
                        kc.walk_block(c, "trigger")
            elif f.category == "static_modifiers":
                for c in node.value:
                    k = c.key
                    if k is None or c.is_item or k == "game_data" or "$" in k or k.startswith("@"):
                        continue
                    if k not in known_modifiers and not vanilla_blind(reg, k):
                        kc.fail(c, "unknown modifier key '%s'" % k)
    out.extend(reg.fails)
    return out


def var_name(node):
    if node.is_block:
        name = node.child("name")
        return name.value if name is not None and isinstance(name.value, str) else None
    return node.value if isinstance(node.value, str) else None


def check_refs(files, defs, reg):
    out = []
    sv = defs["script_values"] | reg.vanilla["script_values"]
    st = defs["scripted_triggers"] | reg.vanilla["scripted_triggers"]
    se = defs["scripted_effects"] | reg.vanilla["scripted_effects"]
    sm = defs["static_modifiers"] | reg.vanilla["static_modifiers"]
    on_actions = defs["on_action"] | reg.on_actions | reg.vanilla["on_action"]
    mod_calls = defs["scripted_triggers"] | defs["scripted_effects"] | defs["script_values"]
    loc_keys = set()
    for f in files:
        loc_keys.update(f.loc)
    writes, reads = set(), {}

    def read(f, line, name):
        reads.setdefault(name, (f, line))

    for f in files:
        if f.ext == ".yml":
            continue
        for kind, text, line in f.tokens:
            if kind in ("word", "str"):
                for name in VAR_LINK_RE.findall(text) + GET_VARIABLE_RE.findall(text):
                    read(f, line, name)
        for node, anc in walk(f.nodes):
            key = node.key
            if key is None:
                continue
            if key in VAR_WRITE_KEYS:
                name = var_name(node)
                if name:
                    writes.add(name)
            elif key in VAR_READ_KEYS:
                name = var_name(node)
                if name and BARE_RE.match(name):
                    read(f, node.line, name)
            if f.ext != ".txt" or f.category in ("static_modifiers", "modifier_types"):
                continue
            value = node.value if isinstance(node.value, str) else None
            if value is not None and (
                    (key in VALUE_POS_KEYS and node.op in ("=", "?=")) or node.op in COMPARE_OPS):
                if (BARE_RE.match(value) and value not in BARE_VALUE_SKIP and value not in sv
                        and value not in reg.event_targets and value not in reg.triggers
                        and not vanilla_blind(reg, value)):
                    out.append(fmt(f, node.line, "undefined value '%s'" % value))
            if DCL_KEY_RE.match(key) and anc and not node.is_item and key not in mod_calls:
                out.append(fmt(f, node.line, "undefined scripted trigger/effect/value '%s'" % key))
            if ADD_MOD_RE.match(key) or HAS_MOD_RE.match(key):
                target = node
                if node.is_block:
                    target = node.child("name") or node.child("modifier")
                mod = target.value if target is not None and isinstance(target.value, str) else None
                if mod and BARE_RE.match(mod) and mod not in sm and not vanilla_blind(reg, mod):
                    out.append(fmt(f, node.line, "undefined static modifier '%s'" % mod))
            if key == "on_actions" and node.is_block:
                for c in node.value:
                    if (c.is_item and "$" not in c.key and c.key not in on_actions
                            and not vanilla_blind(reg, c.key)):
                        out.append(fmt(f, c.line, "undefined on_action '%s'" % c.key))
            if (key == "on_action" and value and "$" not in value and value not in on_actions
                    and not vanilla_blind(reg, value)):
                out.append(fmt(f, node.line, "undefined on_action '%s'" % value))
            if key in LOC_SCRIPT_KEYS and value:
                text = unquote(value)
                if DCL_LOC_RE.match(text) and text not in loc_keys:
                    out.append(fmt(f, node.line, "missing loc key '%s'" % text))

    def text_refs(f, text, base_line, macros):
        for m in SCRIPT_VALUE_RE.finditer(text):
            if m.group(1) not in sv and not vanilla_blind(reg, m.group(1)):
                out.append(fmt(f, base_line(m), "undefined script value '%s'" % m.group(1)))
        for m in SHOW_MODIFIER_RE.finditer(text):
            if m.group(1) not in sm and not vanilla_blind(reg, m.group(1)):
                out.append(fmt(f, base_line(m), "undefined static modifier '%s'" % m.group(1)))
        for rx in (LOCALIZE_RE, macros):
            for m in rx.finditer(text):
                name = m.group(1)
                if DCL_LOC_RE.match(name) and name not in loc_keys:
                    out.append(fmt(f, base_line(m), "missing loc key '%s'" % name))

    for f in files:
        if f.ext == ".gui":
            text_refs(f, f.text, lambda m, t=f.text: t.count("\n", 0, m.start()) + 1, GUI_TEXT_RE)
        elif f.ext == ".yml":
            for key, (line, value) in f.loc.items():
                text_refs(f, value, lambda m, ln=line: ln, YML_MACRO_RE)
                for name in GET_VARIABLE_RE.findall(value):
                    read(f, line, name)

    for name, (f, line) in reads.items():
        if name not in writes:
            out.append(fmt(f, line, "variable '%s' read but never set" % name))

    for f in files:
        if f.category != "static_modifiers":
            continue
        for node in f.nodes:
            if node.key is None or node.key.startswith("@") or node.is_item:
                continue
            for prefix in ("STATIC_MODIFIER_NAME_", "STATIC_MODIFIER_DESC_"):
                if prefix + node.key not in loc_keys:
                    out.append(fmt(f, node.line, "missing loc key '%s%s'" % (prefix, node.key)))
    return out


def check_forbidden(files):
    out = []
    for f in files:
        if f.ext == ".yml":
            for line, text in f.comments:
                if FORBIDDEN_COMMENT_RE.search(text):
                    out.append(fmt(f, line, "forbidden comment: %s" % text.strip()))
            continue
        if f.ext != ".txt":
            continue
        for line, text in f.comments:
            if FORBIDDEN_COMMENT_RE.search(text):
                out.append(fmt(f, line, "forbidden comment: %s" % text.strip()))
        for kind, tok, line in f.tokens:
            if kind != "word":
                continue
            for piece in re.split(r"[.:]", tok):
                if (piece.startswith("on_daily") or piece == "every_location_in_the_world"
                        or MODIFIER_SIZE_RE.match(piece) or piece == "is_ai"):
                    out.append(fmt(f, line, "forbidden '%s'" % tok))
                    break
        for node, ancestors in walk(f.nodes):
            if node.key in ("fixed_range", "integer_range"):
                if not any(a.key and VAR_EFFECT_RE.match(a.key) for a in ancestors):
                    out.append(fmt(f, node.line, "%s used inside a formula (roll once into a variable)"
                                   % node.key))
            elif node.key and node.is_block and CLAMP_RE.match(node.key):
                for bound in ("min", "max"):
                    c = node.child(bound)
                    if c is None:
                        continue
                    if c.is_block:
                        out.append(fmt(f, c.line, "clamp_variable %s bound is not a literal (block)" % bound))
                    elif isinstance(c.value, str) and not NUMERIC_RE.match(c.value):
                        out.append(fmt(f, c.line, "clamp_variable %s bound '%s' is not a literal"
                                       % (bound, c.value)))
        if f.category not in ("scripted_effects", "on_action"):
            continue
        prev_end = 0
        for top in f.nodes:
            tagged = any(prev_end < line <= top.line and AREA_TAG_RE.search(text)
                         for line, text in f.comments)
            if top.is_block:
                for node, ancestors in walk(top.value, (top,)):
                    if node.key not in VAR_WRITE_KEYS:
                        continue
                    name = var_name(node)
                    if not name or "$" in name:
                        continue
                    area = tagged
                    for a in reversed(ancestors[1:]):
                        if not a.key:
                            continue
                        if AREA_SCOPE_RE.match(a.key):
                            area = True
                            break
                        if LOCATION_SCOPE_RE.match(a.key):
                            area = False
                            break
                    if area:
                        if name not in AREA_VARIABLES:
                            out.append(fmt(f, node.line, "area variable '%s' set in '%s' (allowed: %s)"
                                           % (name, top.key, ", ".join(sorted(AREA_VARIABLES)))))
                    elif name != LOCATION_VARIABLE:
                        out.append(fmt(f, node.line,
                                       "location variable '%s' set in '%s' (only %s; tag '# scope: area' "
                                       "for area effects)" % (name, top.key, LOCATION_VARIABLE)))
            prev_end = top.end
    return out


def check_loc_unused(files, defs):
    words = set()
    owners = {}
    loc = {}
    for f in files:
        if f.ext == ".yml":
            loc.update(f.loc)
            continue
        for w in LOC_WORD_RE.findall(f.text):
            words.add(w)
            words.update(w.split("."))
    for key, (_, value) in loc.items():
        for w in LOC_WORD_RE.findall(value):
            for piece in {w} | set(w.split(".")):
                owners.setdefault(piece, set()).add(key)
    derived = set()
    for m in defs["static_modifiers"]:
        derived.update(("STATIC_MODIFIER_NAME_" + m, "STATIC_MODIFIER_DESC_" + m))
    for m in defs["modifier_types"]:
        derived.update(("MODIFIER_TYPE_NAME_" + m, "MODIFIER_TYPE_DESC_" + m))
    used = {k for k in loc if k in words or k in derived or owners.get(k, set()) - {k}}
    for k in loc:
        for suffix in ("_desc", "_DESC"):
            if k.endswith(suffix) and k[:-len(suffix)] in used:
                used.add(k)
    return sorted("loc_unused: %s" % k for k in loc if k not in used)


def main(argv=None):
    here = Path(__file__).resolve()
    p = argparse.ArgumentParser(description="Static audit for the Dynamic Climate & Land Use mod.")
    p.add_argument("mod_root", nargs="?", default=str(here.parents[1]))
    p.add_argument("--docs", default=os.environ.get("DCL_DOCS_DIR") or str(here.parents[2] / "docs"))
    p.add_argument("--vanilla", default=os.environ.get("DCL_VANILLA_ROOT") or DEFAULT_VANILLA)
    args = p.parse_args(argv)
    root = Path(args.mod_root)
    if not root.is_dir():
        p.error("mod root is not a directory: %s" % root)
    files = scan_mod(root)
    reg = load_registry(str(Path(args.docs)), str(Path(args.vanilla)))
    defs = collect_defs(files)
    results = {
        "bom": check_bom(files),
        "braces": check_braces(files),
        "refs": check_refs(files, defs, reg),
        "registry_keys": check_registry_keys(files, defs, reg),
        "forbidden": check_forbidden(files),
    }
    counts = {
        "bom": len(files),
        "braces": sum(1 for f in files if f.ext in (".txt", ".gui")),
        "refs": len(files),
        "registry_keys": len(files),
        "forbidden": sum(1 for f in files if f.ext in (".txt", ".yml")),
    }
    failed = False
    for check in CHECKS:
        if results[check]:
            failed = True
            for detail in results[check]:
                print("FAIL %s: %s" % (check, detail))
        else:
            print("PASS %s: %d files checked" % (check, counts[check]))
    for w in reg.warns + check_loc_unused(files, defs):
        print("WARN %s" % w)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
