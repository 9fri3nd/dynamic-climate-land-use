"""Worst-case evaluation cost of the mod's script values.

Usage: python tools/dcl_cost_model.py [mod_root] [--all] [--values a,b,...] [--dir DIR]

Parses every ``in_game/common/script_values/*.txt`` (or ``--dir``), recursively
expands named-value references and prints, per top-level value, the number of
trigger checks and named-value evaluations one evaluation of that value costs.
Without ``--all``/``--values`` only the headline climate values that exist are
printed. A trailing ``FLAT`` section lists every parent that references the same
non-leaf helper more than once (a leaf is a value whose body holds no named-value
reference at all); ``COMPARE`` lists limits that compare a named value.

Cost convention (worst case, no short-circuit):
  * an if/else_if/else chain costs its most expensive branch, where taking a
    branch costs every limit up to and including its own plus its body; ``else``
    costs all limits plus its body; nested chains recurse;
  * a trigger statement costs 1; AND/OR/NOT/NOR/NAND and scope links such as
    ``area ?= { }`` add nothing themselves and sum their children;
  * a named value used as a trigger operand costs 1 check plus that value's cost;
  * a named-value reference costs 1 evaluation plus its recursive cost;
  * numbers, ``scope:``/``var:``/dotted links and engine operands cost 0.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcl_audit import parse, tokenize  # noqa: E402

HEADLINE = (
    "dynamic_location_temperature",
    "dcl_location_temperature_cached",
    "dcl_location_temperature_mean",
    "dynamic_location_humidity",
    "dcl_location_humidity_cached",
    "dynamic_saturation_vapor_pressure",
    "dcl_saturation_vapor_pressure_cached",
    "wet_bulb_temperature",
    "dcl_wet_bulb_cached",
    "calc_day_length_factor",
    "dcl_insolation",
)
LOGIC = {"AND", "OR", "NOT", "NOR", "NAND"}
CHAIN = ("if", "else_if", "else")


class Cost:
    __slots__ = ("checks", "evals")

    def __init__(self, checks=0, evals=0):
        self.checks = checks
        self.evals = evals

    def __add__(self, other):
        return Cost(self.checks + other.checks, self.evals + other.evals)

    def key(self):
        return (self.checks, self.evals)


class CycleError(Exception):
    pass


def load_values(directory):
    values = {}
    for path in sorted(Path(directory).glob("*.txt")):
        nodes, _ = parse(tokenize(path.read_text(encoding="utf-8-sig"))[0])
        for node in nodes:
            if node.key and node.is_block:
                values[node.key] = node.value
    return values


class CostModel:
    def __init__(self, values):
        self.values = values
        self.memo = {}
        self.active = set()

    def is_named(self, word):
        return isinstance(word, str) and word in self.values

    def value(self, name):
        if name in self.memo:
            return self.memo[name]
        if name in self.active:
            raise CycleError(name)
        self.active.add(name)
        cost = self.block(self.values[name])
        self.active.discard(name)
        self.memo[name] = cost
        return cost

    def ref(self, word):
        if isinstance(word, list):
            return self.block(word)
        if self.is_named(word):
            return Cost(0, 1) + self.value(word)
        return Cost()

    def block(self, nodes):
        total = Cost()
        i = 0
        while i < len(nodes):
            node = nodes[i]
            if node.key == "if":
                j = i + 1
                while j < len(nodes) and nodes[j].key in ("else_if", "else"):
                    j += 1
                    if nodes[j - 1].key == "else":
                        break
                total = total + self.chain(nodes[i:j])
                i = j
                continue
            if node.key not in ("desc",) and node.op is not None:
                total = total + self.ref(node.value)
            i += 1
        return total

    def chain(self, branches):
        worst, limits = Cost(), Cost()
        for branch in branches:
            body = [c for c in branch.value if c.key != "limit"]
            if branch.key != "else":
                lim = branch.child("limit")
                if lim is not None and lim.is_block:
                    limits = limits + self.trigger(lim.value)
            taken = limits + self.block(body)
            if taken.key() > worst.key():
                worst = taken
        if limits.key() > worst.key():
            worst = limits
        return worst

    def trigger(self, nodes):
        total = Cost()
        for node in nodes:
            if node.is_block and (node.key in LOGIC or node.op in ("?=", "=", None)):
                total = total + self.trigger(node.value)
                continue
            total = total + Cost(1, 0) + self.ref(node.key) + self.ref(node.value)
        return total

    def refs(self, nodes, out):
        for node in nodes:
            if node.is_block:
                self.refs(node.value, out)
            else:
                for word in (node.key, node.value):
                    if self.is_named(word):
                        out.append((word, node.line))
        return out

    def is_leaf(self, name):
        return not self.refs(self.values[name], [])

    def flat_violations(self):
        found = []
        for name, body in self.values.items():
            counts = {}
            for ref, _ in self.refs(body, []):
                counts[ref] = counts.get(ref, 0) + 1
            for ref, n in sorted(counts.items()):
                if n > 1 and not self.is_leaf(ref):
                    found.append((name, ref, n))
        return found

    def compare_limits(self):
        found = []

        def scan(name, nodes, in_limit):
            for node in nodes:
                if node.is_block:
                    scan(name, node.value, in_limit or node.key == "limit")
                elif in_limit and (self.is_named(node.key) or self.is_named(node.value)):
                    found.append((name, node.key, node.line))

        for name, body in self.values.items():
            scan(name, body, False)
        return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mod_root", nargs="?", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--dir", help="script_values directory (overrides mod_root)")
    ap.add_argument("--all", action="store_true", help="print every top-level value")
    ap.add_argument("--values", help="comma-separated value names to print")
    args = ap.parse_args(argv)

    directory = args.dir or os.path.join(args.mod_root, "in_game", "common", "script_values")
    model = CostModel(load_values(directory))
    if args.values:
        names = [n.strip() for n in args.values.split(",") if n.strip()]
    elif args.all:
        names = list(model.values)
    else:
        names = [n for n in HEADLINE if n in model.values]

    width = max([len(n) for n in names] + [5])
    print(f"{'value':<{width}}  {'checks':>7}  {'evals':>6}")
    status = 0
    for name in names:
        if name not in model.values:
            print(f"{name:<{width}}  {'missing':>7}")
            status = 1
            continue
        try:
            cost = model.value(name)
        except CycleError as exc:
            print(f"{name:<{width}}  cycle through {exc}")
            status = 1
            continue
        print(f"{name:<{width}}  {cost.checks:>7}  {cost.evals:>6}")
    for parent, ref, n in model.flat_violations():
        print(f"FLAT {parent}: non-leaf {ref} referenced {n} times")
    for name, key, line in model.compare_limits():
        print(f"COMPARE {name}:{line}: limit compares {key}")
    return status


if __name__ == "__main__":
    sys.exit(main())
