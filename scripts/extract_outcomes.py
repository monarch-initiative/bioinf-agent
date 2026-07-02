#!/usr/bin/env python3
"""
extract_outcomes — harvest the system's terminal decision surface straight from
the code, so the model is DERIVED from reality and can't silently drift (see
docs/scenario_decision_tree.md and agent/skills/outcomes.py).

It sweeps every primitive module and finds three kinds of terminal:

  1. TAGGED helper calls — proven()/refused()/broke()/vanished()/degraded()/loop().
     The first string arg is the `code`; the function name is the outcome class.
  2. TAGGED invariant records — dict literals carrying an "invariant" key (these
     are refusal reasons; outcome = refused).
  3. UNTAGGED terminals — a raw `return {...}` whose dict has an "error" or
     "success" key. These are terminals the model doesn't know how to classify
     yet: model blind spots. The rollout burns this count down to zero, and
     tests/test_outcome_tags.py keeps already-tagged functions from regressing.

For each TAGGED code it grep-checks the test suite for a reference (`tested`) —
a refusal nothing tests is a coverage blind spot.

Writes docs/outcomes_ledger.json and prints:
  - the per-outcome tally,
  - the UNTAGGED terminals (the rollout worklist),
  - the tagged-but-UNTESTED terminals (the coverage holes).
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = {"proven", "refused", "broke", "vanished", "degraded", "loop"}
TERMINAL_KEYS = {"error", "success"}   # a raw dict with one of these IS a terminal

# Sweep every primitive/skill/validator module.
SWEEP_DIRS = [
    ROOT / "agent" / "mcp_tools",
    ROOT / "agent" / "skills",
    ROOT / "agent" / "validators",
]
# Files that are pure infrastructure with no honesty-terminals worth modelling.
SKIP = {"__init__.py", "outcomes.py"}


def _str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dict_keys(node: ast.Dict) -> set[str]:
    return {_str(k) for k in node.keys if _str(k) is not None}


def _enclosing_func(path_stack: list) -> str:
    for n in reversed(path_stack):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return n.name
    return "<module>"


def harvest(path: Path) -> list[dict]:
    rel = str(path.relative_to(ROOT))
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[dict] = []

    # walk with a parent stack so we can name the enclosing function
    stack: list = []

    def visit(node):
        stack.append(node)
        # (1) outcome-helper call sites (tagged)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in HELPERS and node.args:
            code = _str(node.args[0])
            if code:
                found.append({"code": code, "outcome": node.func.id, "source": "helper",
                              "tagged": True, "func": _enclosing_func(stack),
                              "where": f"{rel}:{node.lineno}"})
        # (2) invariant violation records (tagged refusals)
        elif isinstance(node, ast.Dict):
            if "invariant" in _dict_keys(node):
                for k, v in zip(node.keys, node.values):
                    if _str(k) == "invariant" and _str(v):
                        found.append({"code": _str(v), "outcome": "refused",
                                      "source": "invariant", "tagged": True,
                                      "func": _enclosing_func(stack),
                                      "where": f"{rel}:{node.lineno}"})
                        break
        # (3) untagged raw terminal returns
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            if _dict_keys(node.value) & TERMINAL_KEYS:
                found.append({"code": None, "outcome": "untagged", "source": "raw",
                              "tagged": False, "func": _enclosing_func(stack),
                              "where": f"{rel}:{node.lineno}"})
        for child in ast.iter_child_nodes(node):
            visit(child)
        stack.pop()

    visit(tree)
    return found


def test_corpus() -> str:
    # `tested` = a BEHAVIORAL test references the code. Exclude the meta-tests
    # that enumerate codes by design (test_outcome_tags.py) — otherwise every
    # code counts as "tested" just for being listed there. NOTE: grep is a coarse
    # proxy (mentions != triggers); real coverage would need instrumentation.
    parts = []
    for p in (ROOT / "tests").rglob("*.py"):
        if p.name == "test_outcome_tags.py":
            continue
        try:
            parts.append(p.read_text())
        except Exception:
            pass
    return "\n".join(parts)


def sweep() -> list[dict]:
    files = []
    for d in SWEEP_DIRS:
        files += [p for p in sorted(d.glob("*.py")) if p.name not in SKIP]
    entries = []
    for f in files:
        entries += harvest(f)
    return entries


def main() -> int:
    corpus = test_corpus()
    entries = sweep()
    for e in entries:
        e["tested"] = (e["code"] in corpus) if e["code"] else None

    ledger = sorted(entries, key=lambda e: (not e["tagged"], e["outcome"], e["where"]))
    (ROOT / "docs" / "outcomes_ledger.json").write_text(json.dumps(ledger, indent=2))

    tagged = [e for e in entries if e["tagged"]]
    untagged = [e for e in entries if not e["tagged"]]
    untested = [e for e in tagged if e["tested"] is False]

    by_outcome: dict[str, int] = {}
    for e in tagged:
        by_outcome[e["outcome"]] = by_outcome.get(e["outcome"], 0) + 1

    print(f"\n  SYSTEM DECISION SURFACE — {len(entries)} terminals across "
          f"{len({e['where'].split(':')[0] for e in entries})} files")
    print(f"  tagged={len(tagged)}  untagged={len(untagged)}  "
          + " ".join(f"{k}={v}" for k, v in sorted(by_outcome.items())))

    # --- the rollout worklist: untagged terminals grouped by file --------------
    print(f"\n  UNTAGGED terminals (the rollout worklist) — {len(untagged)}:")
    by_file: dict[str, list[str]] = {}
    for e in untagged:
        f, ln = e["where"].rsplit(":", 1)
        by_file.setdefault(f, []).append(ln)
    for f in sorted(by_file):
        lines = ", ".join(by_file[f])
        print(f"      {f}  (lines {lines})")

    # --- coverage holes: tagged terminals no test references -------------------
    print(f"\n  TAGGED but UNTESTED (coverage holes) — {len(untested)}:")
    for e in sorted(untested, key=lambda e: e["code"]):
        print(f"      ✗ {e['code']:<34} {e['where']}")

    print(f"\n  wrote docs/outcomes_ledger.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
