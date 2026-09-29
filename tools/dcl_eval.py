"""Offline interpreter for the mod's script_values (mirror-check harness).

Parses ``in_game/common/script_values/*.txt`` with dcl_audit's lenient parser and
evaluates any script value against a mock location scope, so the emitted script
rows can be compared numerically against the fitter's model functions
(tools/dcl_fit_coefficients.py). All script values are pure, so results are
memoised per evaluation for speed without changing semantics.

Supported rows: value/add/subtract/multiply/divide/modulo/min/max/pow/abs/
round/floor/ceiling, if/else_if/else chains with limit, desc (skipped).
Supported operands: numbers, named script values, var:X, area.var:X, scope:X,
rgo_workers, max_rgo_workers. Supported triggers: climate/topography/
vegetation/is_coastal/hemisphere/current_month/has_variable/
has_location_modifier, named-value comparisons, the engine operands above,
market_monthly_food_balance, AND/OR/NOT/NOR/NAND and the area ?= / market ?=
scope links.

Usage: python tools/dcl_eval.py <value> [climate month coastal southern topography]
"""

import math
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_audit import parse, tokenize  # noqa: E402

CHAIN = ("if", "else_if", "else")
LOGIC = {"AND", "OR", "NOR", "NAND", "NOT"}
COMPARE_OPS = ("=", "<", "<=", ">", ">=", "!=")


def load_values(directory):
    values = {}
    for path in sorted(Path(directory).glob("*.txt")):
        nodes, errors = parse(tokenize(path.read_text(encoding="utf-8-sig"))[0])
        if errors:
            raise ValueError(f"{path}: parse errors {errors}")
        for node in nodes:
            if node.key and node.is_block:
                values[node.key] = node.value
    return values


class MockScope:
    """Everything a location-scope script value in this mod may read."""

    def __init__(self, climate="oceanic", topography="flatland", vegetation="grasslands",
                 coastal=False, southern=False, month=1, variables=None, area_vars=None,
                 rgo_workers=0, max_rgo_workers=0, market_food_balance=0,
                 location_modifiers=(), scope_values=None):
        self.climate = climate
        self.topography = topography
        self.vegetation = vegetation
        self.coastal = coastal
        self.southern = southern
        self.month = month
        self.variables = dict(variables or {})
        self.area_vars = dict(area_vars or {})
        self.rgo_workers = rgo_workers
        self.max_rgo_workers = max_rgo_workers
        self.market_food_balance = market_food_balance
        self.location_modifiers = set(location_modifiers)
        self.scope_values = dict(scope_values or {})


def _number(word):
    try:
        return float(word)
    except (TypeError, ValueError):
        return None


class Evaluator:
    def __init__(self, values, scope):
        self.values = values
        self.scope = scope
        self.memo = {}

    # ------------------------------------------------------------ values
    def value(self, name):
        if name not in self.values:
            raise KeyError(f"unknown script value {name}")
        if name not in self.memo:
            self.memo[name] = self.rows(self.values[name], 0.0)
        return self.memo[name]

    def operand(self, word):
        num = _number(word)
        if num is not None:
            return num
        if word in self.values:
            return self.value(word)
        if word.startswith("var:"):
            return float(self.scope.variables.get(word[4:], 0.0))
        if word.startswith("area.var:"):
            return float(self.scope.area_vars.get(word[9:], 0.0))
        if word.startswith("scope:"):
            return float(self.scope.scope_values.get(word[6:], 0.0))
        if word == "rgo_workers":
            return float(self.scope.rgo_workers)
        if word == "max_rgo_workers":
            return float(self.scope.max_rgo_workers)
        raise KeyError(f"unknown operand {word}")

    def rows(self, nodes, running):
        i = 0
        while i < len(nodes):
            node = nodes[i]
            if node.key == "if":
                j = i + 1
                while j < len(nodes) and nodes[j].key in ("else_if", "else"):
                    j += 1
                running = self.chain(nodes[i:j], running)
                i = j
                continue
            if node.key in ("else_if", "else"):
                raise ValueError(f"{node.key} without if at line {node.line}")
            running = self.row(node, running)
            i += 1
        return running

    def chain(self, branches, running):
        for branch in branches:
            if branch.key == "else":
                return self.rows([c for c in branch.value if c.key != "limit"], running)
            lim = branch.child("limit")
            if lim is None or self.trigger_block(lim.value):
                return self.rows([c for c in branch.value if c.key != "limit"], running)
        return running

    def row(self, node, running):
        key, op, word = node.key, node.op, node.value
        if key in ("desc",) or node.is_block:
            return running
        if key == "value":
            return self.operand(word)
        if key == "abs":
            return abs(running)
        if key == "round":
            return float(round(running))
        if key == "floor":
            return float(math.floor(running))
        if key == "ceiling":
            return float(math.ceil(running))
        arg = self.operand(word)
        if key == "add":
            return running + arg
        if key == "subtract":
            return running - arg
        if key == "multiply":
            return running * arg
        if key == "divide":
            return running / arg
        if key == "modulo":
            return running % arg
        if key == "min":
            return max(running, arg)  # min = X floors the running value
        if key == "max":
            return min(running, arg)  # max = X caps the running value
        if key == "pow":
            return running ** arg
        raise KeyError(f"unknown row {key} at line {node.line}")

    # ------------------------------------------------------------ triggers
    def trigger_block(self, nodes):
        return all(self.trigger(node) for node in nodes)

    def compare(self, left, op, right):
        if op == "=":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        raise KeyError(f"unknown comparison {op}")

    def truth(self, word):
        return str(word).lower() in ("yes", "1", "true")

    def trigger(self, node):
        key, op, word = node.key, node.op, node.value
        if node.is_block:
            if key == "AND":
                return self.trigger_block(word)
            if key == "OR":
                return any(self.trigger(c) for c in word)
            if key == "NOT":
                return not self.trigger_block(word)
            if key == "NOR":
                return not any(self.trigger(c) for c in word)
            if key == "NAND":
                return not self.trigger_block(word)
            if key == "area":
                return all(self.area_trigger(c) for c in word)
            if key == "market":
                return all(self.market_trigger(c) for c in word)
            raise KeyError(f"unknown trigger block {key} at line {node.line}")
        if key == "climate":
            return self.compare(self.scope.climate, op, word)
        if key == "topography":
            return self.compare(self.scope.topography, op, word)
        if key == "vegetation":
            return self.compare(self.scope.vegetation, op, word)
        if key == "is_coastal":
            return self.scope.coastal == self.truth(word)
        if key == "hemisphere":
            return (word == "southern") == self.scope.southern
        if key == "current_month":
            return self.compare(float(self.scope.month), op, float(word))
        if key == "has_variable":
            return word in self.scope.variables
        if key == "has_location_modifier":
            return word in self.scope.location_modifiers
        if key in ("rgo_workers", "max_rgo_workers", "market_monthly_food_balance"):
            return self.compare(float(getattr(self.scope, key)), op, float(word))
        if key in self.values or _number(word) is not None:
            return self.compare(self.operand(key), op, self.operand(word))
        raise KeyError(f"unknown trigger {key} at line {node.line}")

    def area_trigger(self, node):
        if node.key == "has_variable":
            return node.value in self.scope.area_vars
        return self.trigger(node)

    def market_trigger(self, node):
        if node.key == "market_monthly_food_balance":
            return self.compare(float(self.scope.market_food_balance), node.op, float(node.value))
        return self.trigger(node)


MOD_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print(__doc__)
        return 1
    name = args[0]
    scope = MockScope(
        climate=args[1] if len(args) > 1 else "oceanic",
        month=int(args[2]) if len(args) > 2 else 1,
        coastal=bool(int(args[3])) if len(args) > 3 else False,
        southern=bool(int(args[4])) if len(args) > 4 else False,
        topography=args[5] if len(args) > 5 else "flatland",
    )
    directory = os.path.join(MOD_ROOT, "in_game", "common", "script_values")
    print(Evaluator(load_values(directory), scope).value(name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
