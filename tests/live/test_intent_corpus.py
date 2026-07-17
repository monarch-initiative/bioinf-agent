"""
The intent corpus — does a user's INPUT reach the outcome they MEANT?

WHY THIS EXISTS. The outcomes dashboard (`docs/outcomes_ledger.json`) maps the OUTPUT
side: every place the code can end, swept from the AST, drift-linted. It answers "what can
the system emit, and has it ever run?" — and it is 509 terminals of genuinely useful truth.

It cannot answer this question. A map of ANSWERS cannot show you a QUESTION with no answer:
an intent that reaches no terminal isn't a dark cell, it isn't a cell at all. Worse, a cell
can be GREEN WITH THE WRONG ANSWER IN IT — `resolve('cellranger')` returns a clean, correct,
fully-tagged `proven` install_call for a CRAN spreadsheet-range parser, when the user meant
10x Genomics' scRNA-seq pipeline. Every terminal did exactly what it was built to do. No
amount of terminal coverage finds that, because nothing is broken except the meaning.

So this corpus maps the INPUT side. Each row is one real user intent, live-probed against
the real resolver, with the outcome that intent SHOULD reach.

THE HISTORY THAT SHAPED IT (read before changing the design). This project already built an
intent map once: `docs/scenario_decision_tree.{md,html,json}`, hand-authored. It rotted, and
tier 7 deleted 2,653 lines of it. What survived from that effort was the outcomes ledger —
and it survived precisely BECAUSE it is derived from code and drift-linted, not authored.
The lesson is not "don't map intent". It is: **a map that isn't executable is prose, and
prose rots.** So this is a test, not a document. The grid renders from it; it does not
render from anyone's memory of it.

WHY LIVE, AND WHY NOT IN THE FAST TIER. The corpus's entire value is that it touched real
registries: PyPI really does normalise `1.0` to `1.0.0`, so `resolve('talos', '1.0.0')`
really can "exactly match" a Keras hyperparameter tuner. Mocking that away rebuilds the very
thing this repo's audit already indicts — 414 monkeypatch sites, 283 lambda doubles, a suite
that "is what lies to you". A mocked intent corpus would assert that our fixtures agree with
our fixtures. So: real network, deliberate tier (`-m live`), never in the fast suite.

THE TWO ASSERTIONS PER ROW, and why they differ:

  1. CHANGE DETECTOR (always on) — does the resolver still do what the corpus recorded?
     Fires on ANY behaviour drift, intended or not. When you intentionally change the
     resolver this goes red; that is correct — re-probe and update the row, deliberately.

  2. CORRECTNESS RATCHET (xfail unless is_correct_today) — does the resolver do the RIGHT
     thing? Most rows do not, today. Those are `xfail`, not failures: they are the roadmap,
     not regressions. As Tier 8 lands, `strict=True` turns each fix into an XPASS FAILURE
     that forces you to promote the row, and the count walks toward zero. That count IS the
     meter, and it is what the grid draws. (Deliberately not written down here — a number
     in prose rots; read it off the grid, which renders it from the corpus.)

THE STABILITY RULE (the one that decides whether anyone trusts this in six months):
assert ONLY on the DECISION — which tier won, `identity.confirmed`, `ambiguous`. NEVER on
`latest`, star counts, or description text. A corpus that goes red because a maintainer cut
a release is a corpus people learn to ignore, and an ignored corpus protects nothing. Rows
whose only interesting fact is volatile carry `expect.assertable: false` and are skipped
here — an honest "we cannot mechanically check this" beats a flaky assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

CORPUS = Path(__file__).resolve().parents[2] / "docs" / "intent_corpus.json"

# NOT a module-level marker. The two PROBING tests hit real registries and are marked
# `live` individually; the INTEGRITY tests below are pure reads of the corpus file and
# must run in the fast tier on every push — they are what stops the corpus from quietly
# rotting into a file nobody can trust, and gating them behind an opt-in flag would mean
# nobody ever runs them.


def _corpus() -> dict:
    if not CORPUS.exists():
        pytest.skip(f"no corpus at {CORPUS} — run scripts/build_intent_corpus.py")
    return json.loads(CORPUS.read_text())


def _rows() -> list[dict]:
    return [r for r in _corpus().get("rows", []) if isinstance(r, dict)]


def _probe(call: dict) -> dict:
    """Run one corpus row's call against the REAL resolver, reduced to the stable decision
    facts. IMPORTED, not reimplemented: the builder that WRITES `actual_today` and the test
    that CHECKS it must ask the resolver the identical question, or the corpus drifts from
    its own harness — which is this codebase's signature defect (`_semantic_versions` forked
    `_resolved_version`'s chain and reported `bcftools: null` while the ENV report said
    `1.23.1`: one fact, two readings, both wrong, disagreeing). Rule 4."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_build_intent_corpus",
        Path(__file__).resolve().parents[2] / "scripts" / "build_intent_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.probe(call)


def _ids(rows) -> list[str]:
    return [r["id"] for r in rows]


# ── 1. the change detector — always on ─────────────────────────────────────────

@pytest.mark.live
@pytest.mark.parametrize("row", _rows() if CORPUS.exists() else [], ids=_ids(_rows()) if CORPUS.exists() else [])
def test_resolver_behaviour_matches_what_the_corpus_recorded(row):
    """Does the resolver still do what we recorded it doing? This is a CHANGE detector,
    not a correctness check — a red here means behaviour moved, which may be exactly what
    you intended. Re-probe and update the row on purpose; never edit it to match.

    It exists because the 49 wrong rows would otherwise be invisible: without this, a
    change that made a wrong answer differently wrong would sail through the xfail."""
    if not row.get("expect", {}).get("assertable", True):
        pytest.skip(f"unassertable: {row['expect'].get('unassertable_reason')}")
    actual = _probe(row["call"])
    recorded = row["actual_today"]
    for field in ("chosen", "identity_confirmed", "ambiguous"):
        if field in recorded:
            assert actual[field] == recorded[field], (
                f"resolver behaviour DRIFTED for {row['id']}:\n"
                f"  call:     {row['call']}\n"
                f"  {field}: recorded {recorded[field]!r}, now {actual[field]!r}\n"
                f"  If this change was intentional, re-probe and update docs/intent_corpus.json "
                f"(and flip is_correct_today if it is now right). Do NOT edit the row to match "
                f"a change you did not mean to make."
            )


# ── 2. the correctness ratchet — the roadmap ───────────────────────────────────

def _correctness_params():
    if not CORPUS.exists():
        return []
    out = []
    for r in _rows():
        if not r.get("expect", {}).get("assertable", True):
            continue
        marks = []
        if not r.get("is_correct_today"):
            marks.append(pytest.mark.xfail(
                strict=True,
                reason=f"[{r.get('gap_class')}] {r.get('expect', {}).get('note', '')[:120]}"))
        out.append(pytest.param(r, marks=marks, id=r["id"]))
    return out


@pytest.mark.live
@pytest.mark.parametrize("row", _correctness_params())
def test_user_intent_reaches_the_outcome_it_deserves(row):
    """THE question: does this user's input reach what they MEANT?

    `strict=True` on the xfails is the ratchet's teeth: when a fix makes a row pass, the
    xfail becomes an XPASS **failure**, so you are forced to promote it (flip
    is_correct_today) rather than let a quiet success rot back into a quiet regression.
    """
    expect, actual = row["expect"], _probe(row["call"])

    # Verdict via `_is_correct` — THE definition, the same one `build_intent_corpus.py`
    # uses to write `is_correct_today`. Not reimplemented here.
    #
    # It was reimplemented here, and the fork detonated on this harness's FIRST live run:
    # `_is_correct` weighed chosen + identity_confirmed + ambiguous; these assertions
    # weighed only the first two. `false-accusation-noise` has expect.ambiguous=False and
    # actual.ambiguous=True, so the builder wrote is_correct_today=False while the test
    # passed — a strict XPASS, red for a reason that had nothing to do with the resolver.
    # The corpus-integrity test could not see it either, because IT calls `_is_correct`
    # too: both copies agreed with each other while the assertions drifted from both.
    # One truth, defined in two places, drifting in one. The same disease, in the harness
    # written to measure it. Rule 4 is not a style note.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_build_intent_corpus",
        Path(__file__).resolve().parents[2] / "scripts" / "build_intent_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not mod._is_correct(expect, actual):
        # The detail below is for the READER; the verdict above is the contract.
        want = ("REFUSE AND ASK (chosen=None + real candidates + a next call that works)"
                if expect["chosen"] is None else f"tier {expect['chosen']!r}")
        got = ("a refusal" if actual["chosen"] is None
               else f"tier {actual['chosen']!r} with an install_call "
                    f"(comment-poisoned={actual['install_call_poisoned']})")
        pytest.fail(
            f"{row['id']} does not reach the user's intent.\n"
            f"  call    : {row['call']}\n"
            f"  intent  : {row.get('intent', '')}\n"
            f"  want    : {want}\n"
            f"  got     : {got}\n"
            f"  expect  : { {k: v for k, v in expect.items() if k not in ('note', 'assertable')} }\n"
            f"  actual  : {actual}\n"
            f"  why     : {expect.get('note', '')}\n"
            f"  NOTE    : a poisoned comment is NOT a refusal — the agent reads the last "
            f"line and runs it. And per feedback-earn-the-refusal, a refusal is only "
            f"legitimate AFTER investigating; it must name reason (a) too-little-input or "
            f"(b) we-looked-and-found-nothing/contradicted.")


# ── 3. the corpus's own integrity ──────────────────────────────────────────────

def test_corpus_is_well_formed_and_dated():
    """The corpus is a claim about a moment. A row probed six months ago is a statement
    about the past — so it carries its probe date, exactly as the outcomes dashboard
    stamps its source commit ('if it is stale, every count above is a confident fiction').
    """
    c = _corpus()
    assert c.get("probed_on"), "the corpus must record WHEN it was probed"
    rows = _rows()
    assert rows, "empty corpus"
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate row ids"
    for r in rows:
        assert r["call"].get("tool"), f"{r['id']}: a row must have a call"
        assert r.get("gap_class") in ("interpretation", "outcome", "report", "none"), (
            f"{r['id']}: gap_class {r.get('gap_class')!r} is not one of the three gaps "
            f"(+none) the grid renders")
        assert "chosen" in r.get("expect", {}), f"{r['id']}: no expectation"


def test_rows_sharing_a_call_do_not_demand_contradictory_outcomes():
    """Two rows on ONE call must be mutually satisfiable, or one of them can never pass.

    Pure — no network — so it runs on every push.

    Multiple rows per call are DELIBERATE and good: the four `cellranger` rows share an
    input and guard different things ("same input, different guard; neither subsumes the
    other, so do not dedupe"). That only works while they AGREE on the decision.

    Three pairs did not, and every one was the same mechanical slip: the verify pass
    rewrote one row's `expect` — with reasoning, in the note, saying "EXPECT REWRITTEN …
    is REFUTED" — and never propagated to its sibling.

        {tool: seurat}                  chosen=null       vs  chosen='cran'
        {tool: limma}                   chosen=null       vs  chosen='bioconductor'
        {tool: bcftools, github_repo:…} chosen=null       vs  chosen='synthesis'

    They were quiet because BOTH were xfail. That is the trap: under `strict=True` the day
    someone fixes `seurat` to return 'cran', the sibling becomes a PERMANENT red that no fix
    can ever clear — and a maintainer who cannot make the suite green by fixing the code
    learns to ignore it. An ignored corpus protects nothing, which is the one failure this
    whole apparatus exists to avoid.

    A review pass found this class and missed three instances (memory records `limma` being
    killed for exactly this, while these three survived). That is the argument for a
    mechanical check: judgement scales badly across 59 rows; a set comparison does not.

    Unassertable rows are exempt — they are never executed, so they cannot collide.
    """
    from collections import defaultdict

    by_call = defaultdict(list)
    for r in _rows():
        if not r.get("expect", {}).get("assertable", True):
            continue
        by_call[json.dumps({k: v for k, v in sorted(r["call"].items())
                            if v is not None})].append(r)

    clashes = []
    for call, rows in by_call.items():
        for field in ("chosen", "identity_confirmed", "ambiguous"):
            # None means "do not check" for identity_confirmed/ambiguous, so only a
            # disagreement between two STATED values is a contradiction. `chosen: None` is
            # itself a demand (REFUSE), so it always counts.
            stated = {json.dumps(r["expect"].get(field)) for r in rows
                      if field == "chosen" or r["expect"].get(field) is not None}
            if len(stated) > 1:
                clashes.append(f"{call} — rows {[r['id'] for r in rows]} disagree on "
                               f"{field}: {sorted(stated)}")
    assert not clashes, (
        "rows sharing a call demand contradictory outcomes; at most one can ever pass:\n  "
        + "\n  ".join(clashes))


def test_every_assertable_row_agrees_with_what_the_harness_can_actually_check():
    """A row must not CLAIM a defect the harness cannot SEE.

    Pure — no network — so it runs even when the live tier is skipped.

    This exists because it caught a real hole on the corpus's first assembly. Six rows
    have `gap_class=report`: the DECISION is already correct (which is all this harness
    reads) and the defect is in what identity REPORTS — e.g. `perl-json` is confirmed on
    the evidence string "bioconda is a bioinformatics-only channel", a proposition its own
    self_description ("JSON encoder/decoder") disproves on the same screen. No assertion
    reads `identity.anchor/.evidence/.note`, so `_is_correct` computed True while the row
    said False. Under `xfail(strict=True)` all six would have XPASSed and turned the suite
    red for a reason with nothing to do with the defect — the corpus's first act would
    have been to cry wolf, which is exactly how a corpus gets ignored.

    They are now `assertable: false` with a reason naming the field Tier 8 must add. That
    is the honest state: we can SEE the report gap and we cannot yet MEASURE it. This test
    keeps the two halves nailed together — add a row whose claim outruns the assertions and
    it fails HERE, loudly, instead of six months later as a mystery XPASS."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_build_intent_corpus",
        Path(__file__).resolve().parents[2] / "scripts" / "build_intent_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    disagree = [
        r["id"] for r in _rows()
        if r.get("expect", {}).get("assertable", True)
        and mod._is_correct(r["expect"], r["actual_today"]) != r.get("is_correct_today")
    ]
    assert not disagree, (
        f"{len(disagree)} assertable row(s) claim a verdict the harness cannot derive from "
        f"their own expect/actual: {disagree}\n"
        f"Either the expectation names a fact no assertion reads (mark it assertable=false "
        f"with the reason, and say which field would fix it), or is_correct_today is simply "
        f"wrong. Do NOT 'fix' it by loosening the expectation to match today's behaviour — "
        f"an expectation edited to match reality is not an expectation.")


def test_known_gaps_are_declared_not_discovered_later():
    """The corpus must state what it CANNOT check. Pure — runs on every push.

    Two are live right now and both are load-bearing:

    1. It cannot tell an EARNED refusal from a LAZY one. 32 rows expect `chosen: null`
       and the harness asserts exactly that — which a refusal that never investigated
       satisfies perfectly. The user's rule is that a refusal is legitimate only AFTER
       investigating, naming reason (a) too-little-input or (b) we-looked-and-came-up-
       empty/contradicted. `resolve('dorado')` proves the gap is real: github discovery
       fires ONLY at a registry dead-end, and PyPI's astronomy squat counts as a hit, so
       nanoporetech/dorado (849★, exact match) is never looked for.
    2. The `report` gap is visible but not measurable (see the test above).

    Declaring them is not bookkeeping. A corpus that shows only what it can measure reads
    as "this is everything", and that is the same lie as a dashboard that doesn't stamp its
    source commit — comfortable, and false. The temptation is to close gap 1 by asserting
    "a refusal must carry candidates"; that is WRONG for the vendor-gated rows, where an
    empty candidate list IS the honest reason (b). Inventing a field to assert against
    would make the corpus agree with itself while drifting from the code.
    """
    gaps = _corpus().get("known_gaps")
    assert gaps, (
        "docs/intent_corpus.json declares no known_gaps. That is almost certainly false, "
        "not admirable — state what the harness cannot check, or the grid claims complete "
        "coverage of a question it only partially answers.")
    for g in gaps:
        for field in ("gap", "detail", "why_not_fixed_here", "what_tier_8_must_do"):
            assert g.get(field), (
                f"known_gap {g.get('gap')!r} has no {field!r} — a gap without a route to "
                f"closing it is a shrug, and shrugs accumulate")


def test_the_ratchet_meter_is_visible():
    """The count of not-yet-correct rows is the meter. Print it so a run states the
    number rather than burying it in xfail noise — the same reason the dashboard leads
    with its real coverage figure instead of a comfortable one."""
    rows = _rows()
    wrong = [r for r in rows if not r.get("is_correct_today")]
    by_gap: dict[str, int] = {}
    for r in wrong:
        by_gap[r.get("gap_class", "?")] = by_gap.get(r.get("gap_class", "?"), 0) + 1
    print(f"\nINTENT CORPUS: {len(rows) - len(wrong)}/{len(rows)} reach the user's intent; "
          f"{len(wrong)} do not — by gap: {by_gap}")
    assert rows
