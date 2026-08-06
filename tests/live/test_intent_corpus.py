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
assert ONLY on the DECISION — which tier won (`chosen`), `ambiguous`, `authors_gate`. NEVER
on `latest`, star counts, or description text. A corpus that goes red because a maintainer
cut a release is a corpus people learn to ignore, and an ignored corpus protects nothing.
Rows whose only interesting fact is volatile carry `expect.assertable: false` and are skipped
here — an honest "we cannot mechanically check this" beats a flaky assertion.

(`identity.confirmed` used to be a graded decision here; the reverse-theme-park Phase 2
(2026-07-17) DELETED the resolver's identity verdict — it now surfaces identity FACTS and the
LLM ride judges — so the corpus no longer grades it. Whether that judgment lands is a declared
known_gap: it needs an LLM-in-the-loop eval, not a resolve()-probe.)
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
    for field in ("chosen", "ambiguous", "refusal_reason"):
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
    # `_is_correct` weighed chosen + ambiguous (+ identity_confirmed, before Phase 2 deleted
    # the resolver's verdict); these assertions weighed only chosen. `false-accusation-noise`
    # has expect.ambiguous=False and
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
        for field in ("chosen", "ambiguous", "refusal_reason"):
            # None means "do not check" for ambiguous/refusal_reason, so only a disagreement
            # between two STATED values is a contradiction. `chosen: None` is itself a demand
            # (REFUSE), so it always counts.
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

    This exists because it caught a real hole on the corpus's first assembly. Several rows
    have `gap_class=report`: the DECISION is already correct (which is all this harness
    reads) and the defect was in what identity REPORTED — e.g. `perl-json` was confirmed on
    the evidence string "bioconda is a bioinformatics-only channel", a proposition its own
    self_description ("JSON encoder/decoder") disproved on the same screen. No assertion
    read `identity.anchor/.evidence/.note`, so `_is_correct` computed True while the row
    said False. Under `xfail(strict=True)` those would have XPASSed and turned the suite
    red for a reason with nothing to do with the defect — the corpus's first act would
    have been to cry wolf, which is exactly how a corpus gets ignored.

    They stay `assertable: false`. Phase 2 (2026-07-17) DELETED the resolver's identity
    verdict (anchor/evidence/note/confirmed) — so the fabricated-report defect is dissolved,
    not pending a field — but whether the resolver now reliably SURFACES the facts, and
    whether the LLM ride's judgment lands, is not yet measured here (see the report-gap
    entry in known_gaps). This test keeps the two halves nailed together — add a row whose
    claim outruns the assertions and it fails HERE, loudly, instead of six months later as a
    mystery XPASS."""
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


def test_deferred_rows_are_a_clean_third_state_not_a_hidden_failure():
    """The domain-decoy rows are graded by the LLM identity eval, not the resolve-probe
    (Decision 1, 2026-07-18). Pure — runs on every push.

    resolve() cannot OBJECTIVELY refuse a valid, repo-real, wrong-domain package (CRAN's
    `cellranger` is a real spreadsheet parser at a real version); refusing it is a DOMAIN
    judgment the LLM ride makes by reading the `identity.self_description` resolve surfaces,
    which a resolve()-probe structurally cannot observe. So those rows are neither a resolve
    success nor a resolve failure — a THIRD state. This test nails that state down so it
    cannot rot into a silent pass (green with the wrong tool) or a silent red (claiming
    resolve failed a job that was never resolve's).

    DO NOT EMPTY THIS SET BY TABLE. On 2026-08-06 a `resolver.VENDOR_GATED` dict — seven
    proprietary names whose vendor gates the runnable bytes — promoted eight of these rows
    for one day, and was removed the same day. The reasoning it rested on is sound (10x
    does not publish Cell Ranger to CRAN, so a CRAN hit under that name is necessarily
    another project) and it is exactly the reasoning THE RIDE should apply. Encoding it as
    a name list made `resolver.py` the one place in `agent/` where a hardcoded tool name
    changed behaviour, and bought eight green rows with a list that can only rot: the
    seventh entry being right never made the eighth's absence honest. These rows are
    deferred because grading them needs a reader with world knowledge, and the fix is to
    build that reader — not to enumerate the cases it would have gotten right.

    A deferred row MUST:
      - carry graded_by == 'llm_identity_eval'  — the discriminator the grid buckets on
      - be assertable=false                     — the resolve-probe does NOT grade it, so it
                                                  is skipped by the change detector + ratchet
      - carry a non-empty unassertable_reason   — mark with a reason, never silently
      - have is_correct_today is None           — not True, not False; nobody has graded it
      - PRESERVE expect.chosen is None          — the reviewed target the eval will hit; Rule
                                                  3, expect is never rewritten to match today
    """
    deferred = [r for r in _rows()
                if r.get("expect", {}).get("graded_by") == "llm_identity_eval"]
    assert deferred, (
        "no rows are deferred to the identity eval — expected the domain-decoy set "
        "(cellranger/dorado/guppy/dragen/talos-bare). Did a re-probe silently drop graded_by?")
    bad = []
    for r in deferred:
        e = r["expect"]
        if e.get("assertable", True) is not False:
            bad.append((r["id"], "assertable is not False — the probe would grade it"))
        if not e.get("unassertable_reason"):
            bad.append((r["id"], "no unassertable_reason — a silent deferral"))
        if r.get("is_correct_today") is not None:
            bad.append((r["id"], f"is_correct_today={r.get('is_correct_today')!r}, want None"))
        if e.get("chosen") is not None:
            bad.append((r["id"], f"expect.chosen={e.get('chosen')!r}, want None (target kept)"))
    assert not bad, (
        "deferred rows are malformed — a deferred row that isn't a clean third state either "
        "hides a failure or fakes a pass:\n" + "\n".join(f"  {i}: {w}" for i, w in bad))


def test_known_gaps_are_declared_not_discovered_later():
    """The corpus must state what it CANNOT check. Pure — runs on every push.

    Three are declared; all are load-bearing:

    1. Earned-vs-lazy is now GRADED (`decision.refusal_reason`, forwarded by probe() and
       asserted inside the chosen==null branch of `_is_correct`; all 29 assertable refusal
       rows carry `expect.refusal_reason`). The RESIDUAL is narrower: the fourth reason,
       `investigation_incomplete`, is token-dependent — without a GITHUB_TOKEN a
       github-backed probe rate-limits, so an earned refusal (needs_user_input /
       contradicted) can come back wearing an unfinished one's face, and the corpus cannot
       separate the two in a token-less run. `resolve('dorado')` is still the standing
       example of the CLIMB the grading now enforces: discovery fires only at a registry
       dead-end and PyPI's astronomy squat counts as a hit, so nanoporetech/dorado is
       never looked for — dorado picks pip instead of refusing, a red interpretation row.
    2. The `report` gap's identity half is DISSOLVED (Phase 2 deleted the fabricated
       verdict), and its successor — does the resolver surface the facts + does the LLM
       ride's judgment land — is not yet measured (see the test above).

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


def test_an_ungraded_row_never_carries_a_verdict():
    """A row the harness does not grade must hold `is_correct_today: None`. Pure — every push.

    The sibling of the deferred-rows test above, generalised, and it was bought with a real
    miscount. `test_deferred_rows_...` already pinned None for the 12 domain decoys; nothing
    pinned it for the OTHER 12 unassertable rows, and they sat on a `False` written back when
    they were still assertable. `build_intent_corpus.py` skips them — correctly, they name
    facts no assertion reads — so nothing revisited that False for a year, and
    `render_intent_grid.py` folded every one into "gradeable, do not reach intent". The
    headline read **16 failures** when the measured number was **4**. Nine of the twelve
    carried an `unassertable_reason` stating, in their own words, that the DECISION is already
    correct; the page printed "12 unassertable" as its own figure a few pixels from the 16.

    Absence rounded up into a verdict, inside the meter this project uses to decide whether v1
    is done — and it flattered us, which is the direction that gets believed. False is a
    CLAIM ("we measured this and the resolver was wrong"); None is the truth ("nobody
    measured this"). A harness that cannot tell them apart cannot be trusted about the rows it
    CAN grade either."""
    liars = [(r["id"], r.get("is_correct_today")) for r in _rows()
             if not r.get("expect", {}).get("assertable", True)
             and r.get("is_correct_today") is not None]
    assert not liars, (
        f"{len(liars)} unassertable row(s) carry a verdict nothing computed:\n"
        + "\n".join(f"  {i}: is_correct_today={v!r}, want None" for i, v in liars)
        + "\nRun `python scripts/build_intent_corpus.py` to clear them, or make the row "
          "assertable and let the probe earn the verdict. Do not hand-write one.")


def test_the_ratchet_meter_is_visible():
    """The count of not-yet-correct rows is the meter. Print it so a run states the
    number rather than burying it in xfail noise — the same reason the dashboard leads
    with its real coverage figure instead of a comfortable one."""
    rows = _rows()
    # FOUR states. The deferred rows (graded by the LLM identity eval, not the resolve-probe)
    # and the unassertable rows (naming a fact no assertion here reads) both hold
    # is_correct_today=None. Neither may be counted as "do not reach intent" — the meter is
    # over what the resolve-probe actually graded. Each is reported as its own figure.
    deferred = [r for r in rows if r.get("expect", {}).get("graded_by") == "llm_identity_eval"]
    unassertable = [r for r in rows if not r.get("expect", {}).get("assertable", True)
                    and r not in deferred]
    graded = [r for r in rows if r not in deferred and r not in unassertable]
    wrong = [r for r in graded if not r.get("is_correct_today")]
    by_gap: dict[str, int] = {}
    for r in wrong:
        by_gap[r.get("gap_class", "?")] = by_gap.get(r.get("gap_class", "?"), 0) + 1
    print(f"\nINTENT CORPUS: {len(graded) - len(wrong)}/{len(graded)} graded rows reach the "
          f"user's intent; {len(wrong)} measured wrong — by gap: {by_gap}; "
          f"{len(deferred)} deferred → LLM identity eval; "
          f"{len(unassertable)} the harness cannot check")
    assert rows


# ── 4. the WRITER's own honesty — the builder must not manufacture a red ───────
#
# Sections 1-3 police the corpus file. This one polices the script that rewrites it, which
# is the only thing with write access to the meter. Pure — runs on every push.

def _builder():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_build_intent_corpus",
        Path(__file__).resolve().parents[2] / "scripts" / "build_intent_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_committed_corpus_is_in_the_form_the_builder_writes():
    """Running the builder must produce a diff you can READ. It used to write `indent=1`
    with \\u-escaped em-dashes over a file committed at `indent=2` UTF-8, so every re-probe
    rewrote all ~1,800 lines and a two-line behaviour change arrived as a whole-file diff.
    A meter whose updates are unreviewable is a meter nobody reviews.

    Break it: change the `json.dumps` kwargs in build_intent_corpus.py without reformatting
    the file, or hand-edit the corpus in a different style."""
    text = CORPUS.read_text()
    assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n", (
        "docs/intent_corpus.json is not in the canonical form build_intent_corpus.py "
        "writes — the next re-probe will reformat the whole file and bury the real change.")


def _row(correct, chosen="conda"):
    return {"id": "r", "call": {"tool": "t"}, "gap_class": "g", "intent": "i",
            "expect": {"assertable": True, "chosen": chosen},
            "actual_today": {"chosen": chosen}, "is_correct_today": correct}


def _run(tmp_path, monkeypatch, row, answers):
    """Drive the builder's main() over a one-row corpus with a scripted probe."""
    mod = _builder()
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps({"probed_on": "2026-01-01", "rows": [row],
                             "known_gaps": []}, indent=2) + "\n")
    monkeypatch.setattr(mod, "CORPUS", p)
    monkeypatch.setattr(mod, "ROOT", tmp_path)          # main() prints CORPUS.relative_to(ROOT)
    seq = list(answers)
    monkeypatch.setattr(mod, "probe", lambda call: {"chosen": seq.pop(0)})
    monkeypatch.setattr(mod.sys, "argv", ["build_intent_corpus.py"])
    mod.main()
    return json.loads(p.read_text())["rows"][0], seq


def test_a_flip_to_red_is_confirmed_before_it_is_written(tmp_path, monkeypatch, capsys):
    """THE DEFECT THIS EXISTS FOR. A green->red flip is the corpus's one destructive write
    and the one most easily manufactured by weather: a rate-limited github probe returns a
    clean `investigation_incomplete`, indistinguishable from the resolver genuinely having
    stopped finding the repo. Written unconfirmed, the transient becomes the NEXT run's
    baseline, so it never surfaces as a transient at all — it surfaces as a regression
    nobody can reproduce. (One such false red was caught by hand in the 2026-08-06 re-probe.)

    Break it: record the first observation without re-probing."""
    row, left = _run(tmp_path, monkeypatch, _row(True), ["pip", "conda"])
    assert row["is_correct_today"] is True, "an unreproduced red must not be recorded"
    assert row["actual_today"]["chosen"] == "conda", "nor may the row keep the bad reading"
    assert not left, "the confirming probe never ran"
    assert "UNSTABLE" in capsys.readouterr().out, "and it must SAY so, not silently pass"


def test_a_reproducible_regression_is_still_recorded(tmp_path, monkeypatch):
    """The confirmation must not become a way to never go red — that would be the same lie
    in the other direction. Two independent noes ARE a verdict.

    Break it: `continue` on every flip instead of only on an unreproduced one."""
    row, _ = _run(tmp_path, monkeypatch, _row(True), ["pip", "pip"])
    assert row["is_correct_today"] is False
    assert row["actual_today"]["chosen"] == "pip"


def test_a_flip_to_green_is_taken_at_face_value(tmp_path, monkeypatch):
    """Only the DESTRUCTIVE direction is double-checked. A red->green flip costs one probe
    and is the outcome the ratchet exists to catch and promote; making it slower and rarer
    would be paying the same price for the opposite of the problem.

    Break it: confirm every flip and the second probe is consumed here too."""
    row, left = _run(tmp_path, monkeypatch, _row(False), ["conda", "unused"])
    assert row["is_correct_today"] is True
    assert left == ["unused"], "a green flip must not spend a second probe"


def test_the_builder_clears_a_stale_verdict_it_will_never_recompute(tmp_path, monkeypatch):
    """An unassertable row is SKIPPED, not graded — so the builder must not leave it wearing a
    verdict from a life it no longer leads. Twelve rows did exactly that, and the grid read
    them as failures (see `test_an_ungraded_row_never_carries_a_verdict`).

    The skip itself is right: these rows name facts no assertion reads, or are a function of
    the invisible github quota. What was wrong was skipping them SILENTLY over a value nothing
    would ever revisit. Note the probe list is empty — clearing must cost no network call, so
    a row parked for being unstable is never re-probed just to be nulled.

    Break it: `continue` before the clear."""
    stale = _row(False)
    stale["expect"] = {"assertable": False, "unassertable_reason": "names a fact nothing reads"}
    row, _ = _run(tmp_path, monkeypatch, stale, [])
    assert row["is_correct_today"] is None, (
        "an unassertable row kept a verdict the builder never recomputed — the next render "
        "will charge it to the failure column")
    assert row["actual_today"] == {"chosen": "conda"}, (
        "clearing the verdict must not touch the OBSERVATION — that is the last real reading "
        "this row has, and overwriting it with nothing loses history for no gain")
