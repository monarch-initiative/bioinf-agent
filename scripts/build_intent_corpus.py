#!/usr/bin/env python3
"""
build_intent_corpus — RE-PROBE the intent corpus against the live registries.

`docs/intent_corpus.json` is a claim about a moment: "on this date, `resolve('cellranger',
version='7.0.0')` chose the `cran` tier — a spreadsheet-range parser — when the user meant
10x Genomics' scRNA-seq pipeline." Claims about moments go stale. This script refreshes the
`actual_today` half of every row against what the resolver really does NOW, and re-stamps
`probed_on`.

WHAT IT DOES NOT TOUCH — and this is the whole design:

    call, expect, gap_class, intent, why_hard   <- JUDGEMENT. Human/reviewed. Never auto-written.
    actual_today, probed_on                      <- OBSERVATION. Mechanical. Re-probed here.

The expectations cost 4M tokens of agent work to mine from real registries and survived an
adversarial pass. They are the asset. This script must never overwrite them — a corpus that
rewrites its own expectations to match current behaviour cannot fail, and a test that cannot
fail is decoration. If the resolver's behaviour changes, `actual_today` moves and
`is_correct_today` is RECOMPUTED against the untouched expectation. That is the point.

    python scripts/build_intent_corpus.py            # re-probe (hits the network)
    python scripts/build_intent_corpus.py --check    # report drift, write nothing

Then `python scripts/render_intent_grid.py` to redraw the page.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "docs" / "intent_corpus.json"

sys.path.insert(0, str(ROOT))

# The observable, ASSERTABLE facts. Deliberately NOT `latest`, star counts, or description
# text: a corpus that goes red because a maintainer cut a release is a corpus people learn
# to ignore, and an ignored corpus protects nothing. See tests/live/test_intent_corpus.py.
# `identity_confirmed` was retired 2026-07-17 (reverse-theme-park Phase 2): the resolver no
# longer emits a verdict, it surfaces identity FACTS and the LLM ride judges — so there is
# no confirm/flag boolean to observe. `install_call_poisoned` survives and now means ONLY
# routing-disclosure poisoning (gate_error / unchecked_tiers / not_assessed / prefer_ignored);
# identity no longer poisons the install_call.
_OBSERVED = ("chosen", "ambiguous", "install_call_poisoned", "refusal_reason")


def probe(call: dict) -> dict:
    """One row's call against the REAL resolver, reduced to the stable decision facts.

    MUST stay identical to `tests/live/test_intent_corpus.py::_probe` — two readings of one
    fact is how this codebase's defects start (the `_resolved_version` fork reported
    `bcftools: null` while the ENV report said `1.23.1`). The test imports this function
    rather than reimplementing it."""
    from agent.skills import resolver as R

    d = R.resolve(**{k: v for k, v in call.items() if v is not None})
    install = d.get("install_call") or ""
    req_v = call.get("version")
    # Does the emitted install_call actually PIN the requested version? A stable boolean, so
    # a version-substitution defect the chosen-tier is blind to becomes visible: chosen='binary'
    # is identical for a somalier v0.2.15 (correct) or v0.3.3 (the bug) asset — only THIS fact
    # tells them apart. The version must appear as a delimited token (optionally v-prefixed), so
    # a request for 0.2.1 never counts as pinned by a v0.2.15 URL. Opt-in in _is_correct.
    pins_version = bool(req_v) and re.search(
        r"(?<![\w.])v?" + re.escape(str(req_v)) + r"(?![\w.])", install) is not None
    return {
        "chosen": d.get("chosen"),
        "ambiguous": bool(d.get("ambiguous")),
        # ONLY routing-disclosure poisoning survives Phase 2 (see _OBSERVED). The resolver
        # emits identity FACTS (identity.self_description / .repo_anchored / .channel), never
        # a verdict; judging them is the LLM ride's job, unmeasurable by a resolve()-probe.
        "install_call_poisoned": install.lstrip().startswith("#"),
        "authors_gate": _authors_gate(d),
        # The machine-readable WHY behind a refusal (None on a successful pick). This is what
        # lets a chosen=null row be graded EARNED vs lazy — see _is_correct + known_gaps[0].
        "refusal_reason": d.get("refusal_reason"),
        "pins_requested_version": pins_version,
    }


def _authors_gate(d: dict) -> str:
    """Which of the four things happened to the authors'-path gate. A DECISION, not
    registry state — so it obeys the corpus's stability rule (a row must never go red
    because a maintainer edited a recipe).

    This closes the corpus's declared blind spot #2 ("the report gap is VISIBLE but not
    MEASURABLE"). The 2026-07-17 repo-provenance fix stopped the author tiers — which
    outrank conda — from probing squatters' repos (`Mucephie/DORADO` for dorado,
    `ethereum/trinity` for trinity). Not one row moved, because every assertion the grid
    could make was about `chosen`/`ambiguous`, and neither changed. The grid was
    right to stay flat and right to have declared that it could not see this; the answer
    is to give it eyes, not to trust the fix on faith.

      assessed     — the gate RAN, against a repo something vouched for
      not_assessed — a repo candidate existed but nothing anchored it to this tool, so we
                     declined to look. The third state: NOT "they publish no image"
      errored      — the probe broke. Also not "they publish no image"
      no_repo      — no repo candidate at all; there was nothing to assess
    """
    pr = d.get("probed") or {}
    if pr.get("authors_gate_error"):
        return "errored"
    if pr.get("authors_gate_not_assessed"):
        return "not_assessed"
    if "author_image" in pr or "authors_recipe" in pr:
        return "assessed"
    return "no_repo"


def _is_correct(expect: dict, actual: dict) -> bool:
    """Recomputed from the UNTOUCHED expectation — never asserted by the row itself."""
    if expect.get("chosen") is None:
        if actual.get("chosen") is not None:
            return False
        # EARNED-vs-lazy (feedback-earn-the-refusal): a refusal must name the RIGHT reason,
        # not merely BE a refusal — a resolver that refuses without investigating satisfies
        # chosen=null perfectly. Opt-in on expect (like authors_gate), so annotating one row
        # cannot silently re-grade the rest; and checked ONLY inside this refusal branch, so
        # a non-refusal row (which never gets here) is untouched.
        if expect.get("refusal_reason") is not None:
            if actual.get("refusal_reason") != expect.get("refusal_reason"):
                return False
    elif actual.get("chosen") != expect.get("chosen"):
        return False
    if expect.get("ambiguous") is not None:
        if actual.get("ambiguous") != expect.get("ambiguous"):
            return False
    # Opt-in: only rows whose intent is ABOUT the authors' path carry this, so adding it
    # cannot silently re-grade the other 50. `expect` is reviewed judgement and is never
    # written from `actual` — a corpus that rewrites its expectations to match today's
    # behaviour cannot fail, and a test that cannot fail is decoration.
    if expect.get("authors_gate") is not None:
        if actual.get("authors_gate") != expect.get("authors_gate"):
            return False
    # Opt-in, same discipline as authors_gate: only a row whose intent is byte-level version
    # reproducibility carries this. It reads the ONE fact `chosen` can't — that the install_call
    # pins the REQUESTED version, not another release's bytes under its name (somalier 0.2.15 vs
    # the v0.3.3-latest substitution). Adding it cannot re-grade the other rows.
    if expect.get("pins_requested_version") is not None:
        if actual.get("pins_requested_version") != expect.get("pins_requested_version"):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report drift vs the recorded corpus; write nothing")
    a = ap.parse_args()

    if not CORPUS.exists():
        print(f"no corpus at {CORPUS.relative_to(ROOT)}")
        return 1
    corpus = json.loads(CORPUS.read_text())
    rows = corpus.get("rows", [])

    drift, flipped = [], []
    for r in rows:
        if not r.get("expect", {}).get("assertable", True):
            continue
        try:
            actual = probe(r["call"])
        except Exception as e:                      # a registry outage is not a verdict
            print(f"  !! {r['id']}: probe failed ({e}) — leaving the row untouched")
            continue
        was = {k: r.get("actual_today", {}).get(k) for k in _OBSERVED}
        if any(was.get(k) != actual.get(k) for k in _OBSERVED if k in r.get("actual_today", {})):
            drift.append((r["id"], was, actual))
        correct = _is_correct(r["expect"], actual)
        if correct != r.get("is_correct_today"):
            flipped.append((r["id"], r.get("is_correct_today"), correct))
        if not a.check:
            r["actual_today"] = actual
            r["is_correct_today"] = correct

    for rid, was, now in drift:
        print(f"  DRIFT  {rid}: {was} -> {now}")
    for rid, was, now in flipped:
        arrow = "FIXED" if now else "REGRESSED"
        print(f"  {arrow:9s} {rid}: is_correct_today {was} -> {now}")

    if a.check:
        print(f"\n  {len(drift)} drifted · {len(flipped)} changed correctness (nothing written)")
        return 1 if (drift or flipped) else 0

    corpus["probed_on"] = _dt.date.today().isoformat()
    CORPUS.write_text(json.dumps(corpus, indent=1) + "\n")
    ok = sum(1 for r in rows if r.get("is_correct_today"))
    print(f"\n  wrote {CORPUS.relative_to(ROOT)}  ({len(rows)} scenarios · {ok} reach intent · "
          f"{len(rows) - ok} do not · probed {corpus['probed_on']})")
    print("  next: python scripts/render_intent_grid.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
