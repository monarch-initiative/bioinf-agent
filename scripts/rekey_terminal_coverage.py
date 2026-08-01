#!/usr/bin/env python
"""rekey_terminal_coverage — carry a coverage measurement across a LINE-MOTION diff.

WHY THIS EXISTS
---------------
`docs/terminal_coverage.json` joins to `docs/outcomes_ledger.json` on `where`, which is
literally `f"{rel}:{node.lineno}"` (scripts/extract_outcomes.py). So inserting one comment
above one terminal moves its key, the join misses, and the dashboard's `_cover_state` maps
the miss to "dark". Re-measuring is the only fix today and it costs a full serial suite run
under coverage: MEASURED at 202s (2314 passed / 102 skipped, 2026-07-31), roughly 7x the
28s parallel fast tier. That is long enough that people stop running it, which is how a
stale overlay ends up being read as a real regression.

The measured shape of the problem says a re-key is enough. Across the last three commits,
104/104, 56/62 and 138/165 key changes were the SAME terminal moving lines. And pairing
those terminals across the two full measurements, the number whose coverage verdict
actually DIFFERED was:

    0 of 548,   0 of 548,   0 of 521.

Zero. On real history a re-key would have been perfectly faithful every time.

WHAT MAKES THIS HONEST RATHER THAN LAUNDERING
---------------------------------------------
Carrying a measurement across a code change is exactly the move this repo distrusts. Three
rules, borrowed from `measure_freeze_tier_coverage.carry_forward()` which had to learn them
first, plus one this codebase's own history demanded:

  1. PAIR ON MEANING, NOT POSITION. `(file, func, code, outcome, source, ordinal)` — never
     the line. Ordinal disambiguates a function emitting the same code twice.
  2. REFUSE WHEN THE CODE ACTUALLY CHANGED. If a terminal's enclosing function is not
     byte-identical between the two revisions, its verdict is not transferable.
  3. REFUSE WHEN THE TEST SUITE SHRANK. This is the rule the freeze-tier precedent does
     NOT have, and it is the one that matters most here: coverage is a property of the
     SUITE, not of the function. Deleting or editing a test flips a terminal's verdict with
     its enclosing function untouched. Tests that are only ADDED are safe in one direction
     — they can only make reality better than what we carry — so those are allowed, and
     the under-report is disclosed rather than hidden.
  4. NEVER CLAIM TO HAVE MEASURED. `measured_sha` keeps naming the revision the numbers
     were actually observed at, and a `carried_forward` block states what was carried,
     from where, and what could not be.

A genuinely NEW terminal has no measurement to carry. It is written `false` (dark), which
UNDER-reports — the safe direction for a coverage meter, which must never claim more
coverage than it has — and every such key is listed in `carried_forward.assumed_dark` so
the understatement is auditable rather than silent.

USAGE
-----
    python scripts/extract_outcomes.py        # refresh the ledger for the new code
    python scripts/rekey_terminal_coverage.py # carry the overlay onto it  (< 2s)

    python scripts/rekey_terminal_coverage.py --check   # report, write nothing

Refuses and tells you to run the real measurement whenever rule 2 or 3 trips.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "outcomes_ledger.json"
OVERLAY = ROOT / "docs" / "terminal_coverage.json"

#: The revision the committed overlay was measured against. The committed ledger and the
#: committed overlay are written and committed together, so HEAD's ledger is the one the
#: committed overlay joins to — that is the baseline we pair against.
BASELINE_REV = "HEAD"

#: Past this share of paired terminals sitting in edited functions, a re-key stops being
#: a shortcut and starts being a page that mostly discloses its own ignorance. Refuse and
#: send the caller to the real measurement instead.
MAX_UNMEASURED_FRACTION = 0.20


# --------------------------------------------------------------------------- git


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def _show(rev: str, rel: str) -> str | None:
    """File contents at a revision, or None if it did not exist there."""
    r = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=str(ROOT),
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


# --------------------------------------------------------------------------- pairing


def pair_key(entry: dict, ordinal: int) -> tuple:
    """The stable identity of a terminal — everything about it EXCEPT where it sits.

    `code` alone collides 19x in the current ledger and file+func+code still collides 12x,
    because one function legitimately emits the same code from several branches. The
    ordinal (its Nth occurrence under an otherwise-identical key, in ledger order)
    disambiguates those. Note the ordinal makes the composite unique by CONSTRUCTION —
    that is not evidence of anything, and the property that matters is whether it PAIRS
    the same terminal across two revisions, which is what `--check` reports.
    """
    return (entry.get("file") or entry["where"].split(":", 1)[0],
            entry.get("func"),
            entry.get("code"),
            entry.get("outcome"),
            entry.get("source"),
            ordinal)


def _sortable(key: tuple) -> tuple:
    """`sorted()` over pair_keys, made total.

    A `raw` terminal (a bare `return {...}` the extractor could not tag) carries
    `code: None`, so two keys that agree on file+func but differ there compare
    None against str and TypeError out. That only fires when the added/gone sets
    are non-empty AND span such a pair — which is why it sat here unhit until a
    commit both moved lines and introduced new terminals. Sort on a stringified
    view; the pairing itself still uses the real tuple, so None stays distinct
    from the empty string where it matters.
    """
    return tuple("" if x is None else str(x) for x in key)


def keyed(ledger: list[dict]) -> dict[tuple, dict]:
    seen: dict[tuple, int] = {}
    out: dict[tuple, dict] = {}
    for e in ledger:
        base = pair_key(e, 0)[:-1]
        n = seen.get(base, 0)
        seen[base] = n + 1
        out[pair_key(e, n)] = e
    return out


# --------------------------------------------------------------------- the two gates


def _functions(src: str | None) -> dict[str, str]:
    """{qualified-ish function name: its exact source bytes} for one module."""
    if not src:
        return {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    lines = src.splitlines()
    out: dict[str, str] = {}

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                seg = "\n".join(lines[child.lineno - 1:(child.end_lineno or child.lineno)])
                out[name] = seg
                walk(child, prefix=f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix=f"{prefix}{child.name}.")
    walk(tree)
    return out


def changed_functions(rev: str, files: set[str]) -> dict[str, set[str]]:
    """{file: {function names whose bytes differ between `rev` and the worktree}}.

    A function that moved wholesale but is byte-identical is NOT changed — that is the
    entire class of diff this script exists to carry across.
    """
    out: dict[str, set[str]] = {}
    for rel in sorted(files):
        old = _functions(_show(rev, rel))
        new_src = (ROOT / rel).read_text() if (ROOT / rel).exists() else None
        new = _functions(new_src)
        diff = {name for name in set(old) | set(new)
                if old.get(name) != new.get(name)}
        if diff:
            out[rel] = diff
    return out


def _module_pieces(src: str | None) -> dict[str, str]:
    """{piece name: exact source bytes} for every top-level construct in a module.

    Functions and methods by qualified name, plus each module-level non-def statement
    keyed by position. Enough to answer "is everything that was here before still here,
    unchanged?" without caring where it now sits in the file.
    """
    if not src:
        return {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # An unparseable file is not something to reason about — treat as fully changed.
        return {"«unparseable»": src}
    lines = src.splitlines()
    pieces = dict(_functions(src))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # Keyed BY ITS OWN BYTES, never by position. Keying module-level statements on
        # their index in `tree.body` looks reasonable and is wrong: appending one test
        # function renumbers every statement after it, so an untouched constant reports
        # as "removed or edited" and the gate refuses every real PR. Caught by running
        # it — the first `--check` refused on `FULLY_TAGGED = [`, which nothing touched.
        seg = "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
        pieces[f"«module»{seg}"] = seg
    return pieces


def suite_risk(rev: str) -> tuple[str, list[str]]:
    """('' | reason, details) — did the TEST SUITE shrink or shift since `rev`?

    Coverage is a property of the suite. An added test can only make the truth better than
    what we carry (we under-report, which is safe). A deleted or EDITED test can flip a
    terminal from covered to dark with its enclosing function untouched, and carrying then
    over-reports. The measured 0/548 verdict drift across three commits does not license
    skipping this: one of those three was the PR that rewrote suite timing, and it came out
    clean by luck rather than by any property we can lean on.

    Deliberately finer than a file-level `git diff --name-status`: appending a new test
    function to an existing file marks that file `M`, and refusing on that would reject
    the single most common shape of a real PR. So the comparison is per-CONSTRUCT — every
    piece that existed at `rev` must still exist, byte-identical. New pieces are free.
    """
    changed_files = [ln.strip() for ln in
                     _git("diff", "--name-only", rev, "--", "tests/").splitlines()
                     if ln.strip()]
    risky: list[str] = []
    safe: list[str] = []
    for rel in sorted(set(changed_files)):
        if not rel.endswith(".py"):
            # A fixture file, a data file, a marker config — cannot be reasoned about
            # construct-wise, and any of them can change what the suite reaches.
            risky.append(f"non-python test asset changed: {rel}")
            continue
        old = _module_pieces(_show(rev, rel))
        new_src = (ROOT / rel).read_text() if (ROOT / rel).exists() else None
        if new_src is None:
            risky.append(f"deleted: {rel}")
            continue
        new = _module_pieces(new_src)
        lost = [n for n, body in old.items() if new.get(n) != body]
        if lost:
            risky.append(f"{rel}: {len(lost)} construct(s) removed or edited "
                         f"({', '.join(sorted(lost)[:3])})")
        else:
            safe.append(f"{rel}: +{len(set(new) - set(old))} new construct(s)")
    for path in _git("ls-files", "--others", "--exclude-standard", "tests/").splitlines():
        if path.strip():
            safe.append(f"new file: {path.strip()}")

    if risky:
        return ("the test suite changed in a way that can REMOVE coverage", risky)
    return ("", safe)


# --------------------------------------------------------------------------- main


def rekey(check_only: bool = False) -> int:
    if not LEDGER.exists() or not OVERLAY.exists():
        print("  ! need both docs/outcomes_ledger.json and docs/terminal_coverage.json",
              file=sys.stderr)
        return 1

    new_ledger = json.loads(LEDGER.read_text())
    old_raw = _show(BASELINE_REV, "docs/outcomes_ledger.json")
    if old_raw is None:
        print(f"  ! no ledger at {BASELINE_REV} to pair against", file=sys.stderr)
        return 1
    old_ledger = json.loads(old_raw)
    overlay_doc = json.loads(OVERLAY.read_text())
    old_by_where = overlay_doc.get("by_where") or {}

    old_keyed, new_keyed = keyed(old_ledger), keyed(new_ledger)
    baseline_sha = _git("rev-parse", "--short", BASELINE_REV).strip()

    paired = sorted(set(old_keyed) & set(new_keyed), key=_sortable)
    added = sorted(set(new_keyed) - set(old_keyed), key=_sortable)
    gone = sorted(set(old_keyed) - set(new_keyed), key=_sortable)

    # Nothing to do is worth saying out loud rather than writing an identical file.
    moved = [k for k in paired if old_keyed[k]["where"] != new_keyed[k]["where"]]
    print(f"  baseline {baseline_sha}: {len(old_ledger)} terminals · "
          f"worktree: {len(new_ledger)}")
    print(f"    paired {len(paired)}  ({len(moved)} moved lines) · "
          f"new {len(added)} · gone {len(gone)}")
    sys.stdout.flush()   # so a refusal on stderr lands AFTER the summary, not before it

    # ---- GATE 1: the test suite must not have shrunk -------------------------
    reason, detail = suite_risk(BASELINE_REV)
    if reason:
        print(f"\n  ! REFUSING to carry: {reason}.", file=sys.stderr)
        for d in detail[:8]:
            print(f"      {d}", file=sys.stderr)
        print(f"    Coverage is a property of the SUITE, not of the code under it — a "
              f"deleted or\n    edited test can flip a terminal to dark with its function "
              f"untouched, and carrying\n    the old verdict would over-report. Run "
              f"`python scripts/measure_terminal_coverage.py`\n    (~3.5 min).",
              file=sys.stderr)
        return 2
    if detail:
        print(f"    tests added ({len(detail)}) — safe: added tests can only make the "
              f"truth better\n    than what is carried, so this UNDER-reports.")

    # ---- GATE 2: no paired terminal's enclosing function may have changed ----
    touched = {new_keyed[k].get("file") or new_keyed[k]["where"].split(":", 1)[0]
               for k in paired}
    changed = changed_functions(BASELINE_REV, touched)
    blocked = [k for k in paired
               if (new_keyed[k].get("func") or "")
               in changed.get(new_keyed[k].get("file")
                              or new_keyed[k]["where"].split(":", 1)[0], set())]
    # NOT a refusal — a classification. Refusing the whole re-key because SIX terminals
    # sit in an edited function throws away 548 perfectly good verdicts, and the first
    # real diff this script met was exactly that shape. An unknown verdict is written
    # DARK, which UNDER-reports (a coverage meter may never claim more than it has), and
    # every such key is named in the disclosure so the understatement is auditable.
    if blocked:
        print(f"    {len(blocked)} terminal(s) sit in a function edited since "
              f"{baseline_sha} — unmeasured, counted dark")

    # Past some fraction, carrying is not buying anything and the page would be mostly
    # a disclosure. Measure instead.
    if paired and len(blocked) / len(paired) > MAX_UNMEASURED_FRACTION:
        print(f"\n  ! REFUSING to carry: {len(blocked)} of {len(paired)} paired "
              f"terminals ({100*len(blocked)//len(paired)}%) are in edited functions, "
              f"over the {int(100*MAX_UNMEASURED_FRACTION)}% ceiling.\n    Too much of "
              f"this page would be disclosure rather than measurement. Run "
              f"`python scripts/measure_terminal_coverage.py` (~3.5 min).",
              file=sys.stderr)
        return 2

    # ---- carry ---------------------------------------------------------------
    blocked_set = set(blocked)
    new_by_where: dict[str, bool] = {}
    carried = 0
    needs_measurement = []
    for k in paired:
        old_where, new_where = old_keyed[k]["where"], new_keyed[k]["where"]
        if k in blocked_set:
            new_by_where[new_where] = False
            needs_measurement.append(new_where)
        elif old_where in old_by_where:
            new_by_where[new_where] = old_by_where[old_where]
            carried += 1
    assumed_dark = []
    for k in added:
        w = new_keyed[k]["where"]
        if w not in new_by_where:
            new_by_where[w] = False
            assumed_dark.append(w)

    # Every ledger row must have an entry or the CI join test fails — and a row we simply
    # forgot would read as dark anyway, so make the invariant explicit here.
    missing = {e["where"] for e in new_ledger} - set(new_by_where)
    if missing:
        print(f"\n  ! internal: {len(missing)} ledger terminal(s) got no entry "
              f"({sorted(missing)[:3]}) — refusing to write a partial overlay.",
              file=sys.stderr)
        return 3

    verified = exercised = dark = 0
    for e in new_ledger:
        if new_by_where.get(e["where"]) is not True:
            dark += 1
        elif e.get("named_in_test"):
            verified += 1
        else:
            exercised += 1

    prior = overlay_doc.get("carried_forward") or {}
    out = {
        "measured_terminals": len(new_ledger),
        "verified": verified, "exercised": exercised, "dark": dark,
        "by_where": new_by_where,
        # THE DISCLOSURE. `measured_sha` names where the numbers were actually observed
        # and is preserved across any number of re-keys — a re-key is not a measurement
        # and must never be able to claim it was one.
        "carried_forward": {
            "measured_sha": prior.get("measured_sha") or baseline_sha,
            "rekeyed_from": baseline_sha,
            "rekeyed_at": _git("rev-parse", "--short", "HEAD").strip(),
            "carried": carried,
            "moved_lines": len(moved),
            "unmeasured": sorted(needs_measurement + assumed_dark),
            "needs_measurement": sorted(needs_measurement),
            "assumed_dark": assumed_dark,
            "dropped": [old_keyed[k]["where"] for k in gone],
            "note": ("verdicts carried across a line-motion diff, not re-measured. "
                     "`needs_measurement` sit in functions edited since the measurement; "
                     "`assumed_dark` are new terminals. Both are counted DARK, which "
                     "under-reports — never over-reports. Run "
                     "measure_terminal_coverage.py for an observed number."),
            "rekeys": int(prior.get("rekeys") or 0) + 1,
        },
    }

    print(f"\n  carried {carried} verdict(s); "
          f"{len(needs_measurement)} edited + {len(assumed_dark)} new = "
          f"{len(needs_measurement) + len(assumed_dark)} counted dark unmeasured")
    print(f"    verified {verified} · exercised {exercised} · dark {dark} "
          f"(of {len(new_ledger)})")
    if check_only:
        print("  --check: nothing written")
        return 0
    OVERLAY.write_text(json.dumps(out, indent=2))
    print(f"  wrote {OVERLAY.relative_to(ROOT)} "
          f"(measured at {out['carried_forward']['measured_sha']}, carried since)")
    return 0


def main() -> int:
    return rekey(check_only="--check" in sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
