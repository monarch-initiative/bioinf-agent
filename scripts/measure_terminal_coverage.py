#!/usr/bin/env python3
"""
measure_terminal_coverage — the REAL coverage signal for the health panel.

The ledger's `named_in_test` field is only a grep (does a test mention the code
string). That undercounts true coverage ~6x, because an integration test that
actually RUNS a tool and triggers a terminal rarely spells out the code string.
This script measures what actually ran:

  1. run the whole test suite under coverage.py  (`coverage run -m pytest`)
  2. read which lines of agent/ executed
  3. for each terminal in docs/outcomes_ledger.json, mark `executed` True/False
     by testing whether ANY line in the terminal's [line, end_line] span ran
  4. write docs/terminal_coverage.json (the measured overlay the dashboard fuses)
  5. re-render docs/outcomes_dashboard.html

The dashboard then classifies every terminal as:
  - verified   — executed AND named_in_test  (a test triggers it and asserts its code)
  - exercised  — executed, not named         (runs, but no code-specific assertion)
  - dark       — never executed              (the real hardening worklist)

Run this after changing code or tests, then commit the refreshed overlay:

    python scripts/measure_terminal_coverage.py

It is SLOW (runs the full suite). extract_outcomes.py stays fast + static and
does NOT run this — the overlay is an explicit, opt-in measurement step.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "outcomes_ledger.json"
OVERLAY = ROOT / "docs" / "terminal_coverage.json"

# The one file with pre-existing machine-specific failures (a bash-glob probe);
# deselect so a red there doesn't abort the coverage run. Coverage still records
# whatever executed regardless of pass/fail, so other failures are tolerated too.
_DESELECT = "tests/integration/honesty/L1_tool_install/test_n1_conda_meta_probe_docker.py"


def _run_suite_under_coverage() -> dict:
    """Run pytest under coverage, return {rel_path: set(executed_lines)}."""
    with tempfile.TemporaryDirectory() as td:
        datafile = str(Path(td) / ".coverage")
        covjson = str(Path(td) / "coverage.json")
        env_run = [sys.executable, "-m", "coverage", "run",
                   f"--data-file={datafile}", "--source=agent",
                   "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
                   "--deselect", _DESELECT]
        print("  running suite under coverage (this takes ~1 min)…")
        r = subprocess.run(env_run, cwd=str(ROOT), capture_output=True, text=True)
        # pytest may exit non-zero on the (tolerated) failures; only bail if it
        # produced no coverage data at all.
        tail = "\n".join(r.stdout.strip().splitlines()[-2:])
        print(f"    pytest: {tail or r.stderr.strip()[-200:]}")
        if not Path(datafile).exists():
            print("  ! coverage produced no data — aborting", file=sys.stderr)
            sys.exit(2)
        subprocess.run([sys.executable, "-m", "coverage", "json",
                        f"--data-file={datafile}", "-o", covjson],
                       cwd=str(ROOT), check=True, capture_output=True, text=True)
        files = json.loads(Path(covjson).read_text())["files"]
    execmap: dict[str, set] = {}
    for f, d in files.items():
        p = Path(f)
        rel = str(p.resolve().relative_to(ROOT)) if p.is_absolute() else f
        execmap[rel] = set(d["executed_lines"])
    return execmap


_STMT_START_CACHE: dict[str, dict[int, int]] = {}


def _stmt_starts(rel: str) -> dict[int, int]:
    """Map each source line to its ENCLOSING simple-statement start line.

    Coverage attributes a multi-line statement — e.g.
        return _summarize(proven("freeze.cache_hit", **{...}))   # stmt starts here
                          ^ the extractor anchors the terminal at the inner call/
                            dict, a LOWER line than the statement start
    — to the STATEMENT's first line. So a terminal anchored at the inner node can
    have its whole [line, end_line] span miss the one line coverage recorded. We
    expand each terminal's span DOWN to its enclosing statement start (which only
    executes on that terminal's own branch — NOT an adjacent sibling statement, so
    this does not reintroduce the happy-path-adjacent overcounting exact-span
    avoided). Innermost stmt wins (a simple stmt can't nest another)."""
    if rel in _STMT_START_CACHE:
        return _STMT_START_CACHE[rel]
    mapping: dict[int, int] = {}
    path = ROOT / rel
    try:
        tree = ast.parse(path.read_text(), filename=rel)
    except (OSError, SyntaxError):
        _STMT_START_CACHE[rel] = mapping
        return mapping
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        for ln in range(start, end + 1):
            # innermost (smallest span) enclosing stmt wins → the simple stmt,
            # not the compound if/for that contains it.
            prev = mapping.get(ln)
            if prev is None or start > prev:
                mapping[ln] = start
    _STMT_START_CACHE[rel] = mapping
    return mapping


def _executed(entry: dict, execmap: dict) -> bool | None:
    """True if any line in the terminal's span ran; None if the file wasn't
    measured at all. The span is expanded DOWN to the enclosing statement start so
    a multi-line `return wrapper(proven(...))` isn't undercounted."""
    rel, ln = entry["where"].rsplit(":", 1)
    lines = execmap.get(rel)
    if lines is None:
        return None
    anchor = int(ln)
    start = _stmt_starts(rel).get(anchor, anchor)   # enclosing-stmt start ≤ anchor
    end = int(entry.get("end_line") or anchor)
    return any(l in lines for l in range(start, end + 1))


def main() -> int:
    if not LEDGER.exists():
        print("  ! run scripts/extract_outcomes.py first", file=sys.stderr)
        return 1
    ledger = json.loads(LEDGER.read_text())
    execmap = _run_suite_under_coverage()

    overlay = {}
    verified = exercised = dark = 0
    for e in ledger:
        ex = _executed(e, execmap)
        overlay[e["where"]] = ex
        # ex is True only if a line in the terminal's span actually ran. False =
        # file measured but span didn't run; None = module never imported by any
        # test. Both non-True cases are DARK — never executed. (This mirrors the
        # dashboard's _cover_state exactly, so the counts can't disagree.)
        if ex is not True:
            dark += 1
        elif e.get("named_in_test"):
            verified += 1
        else:
            exercised += 1

    total = len(ledger)
    OVERLAY.write_text(json.dumps(
        {"measured_terminals": total,
         "verified": verified, "exercised": exercised, "dark": dark,
         "by_where": overlay}, indent=2))
    print(f"\n  REAL terminal coverage — {total} terminals:")
    print(f"    verified  (executed + named): {verified:3d}  ({round(100*verified/total)}%)")
    print(f"    exercised (executed only):    {exercised:3d}  ({round(100*exercised/total)}%)")
    print(f"    dark      (never executed):   {dark:3d}  ({round(100*dark/total)}%)")
    print(f"    → covered (verified+exercised): {verified+exercised} "
          f"({round(100*(verified+exercised)/total)}%)")
    print(f"  wrote {OVERLAY.relative_to(ROOT)}")

    # re-render the dashboard against the fresh overlay
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "render_outcomes_dashboard", ROOT / "scripts" / "render_outcomes_dashboard.py")
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)
    rd.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
