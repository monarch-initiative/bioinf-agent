"""F2 — the ENV report claiming more than the contract established.

The Layer-1 half of `test_run_dashboard_shows_what_was_gated.py`. That file
enforces "a field the seal gated on must appear on the page"; this one enforces
the opposite direction, which turned out to be the more expensive one: **the
page must not assert what the contract did not establish.**

The defect, measured on three records on disk during the 2026-08 sea trial:
`env_report_html` carried a hand-written paragraph per `build_method`, emitted
unconditionally, saying "every requested tool re-ran green via plain exec" and
"POLICY_CLEAN — accelerator honesty (I12) and the license firewall (I13)
passed". Directly beneath it, generated from the same record, the coverage table
marked those clauses `unobserved` and `n/a`. The prose is the half a human reads
first, so the page's most prominent claim was its least true one — an absent
observation rounded up into "passed", under the heading "How this was verified",
in the artifact the v1 acceptance criterion names first.

The fix was not better prose. It was that the section has no prose: it renders
`env_honesty.guarantee_verdicts(contract)`. These tests hold that line from both
ends — the derivation is correct, and the renderer cannot grow a second account
of it.
"""
from __future__ import annotations

import re
from pathlib import Path

from agent.skills.env_honesty import (
    ESTABLISHED,
    FAILED,
    GUARANTEE_NOT_APPLICABLE,
    GUARANTEE_VERDICTS,
    LAYER1_GUARANTEES,
    NOT_ESTABLISHED,
    PARTLY_ESTABLISHED,
    evaluate_build,
    guarantee_verdicts,
)
from agent.skills.env_report_html import render_env_report_html

ROOT = Path(__file__).resolve().parents[3]

# A record that earns everything it can: built, arch observed, evidence run and
# discriminating, shape records present.
_GOOD = {
    "name": "good", "image": "x:1@sha256:aa", "image_digest": "sha256:aa",
    "image_arch": "amd64", "platform": "linux/amd64", "requested_tools": ["x"],
    "verifications": [{"label": "x", "tool": "x", "check": "x --version",
                       "passed": True, "control": "discriminating"}],
    "shipped_binaries": [{"tool": "x", "path": "/usr/bin/x", "sha256": "b" * 64,
                          "version": "1.0", "source": "conda"}],
}


def _verdict(rows, name):
    return next(r["verdict"] for r in rows if r["guarantee"] == name)


# -- the derivation ----------------------------------------------------------

def test_every_layer1_guarantee_gets_exactly_one_row():
    """The roster is the promise; a guarantee that renders no row is one the
    reader was never told about. That is the I5/I10 rot, in the renderer."""
    rows = guarantee_verdicts(evaluate_build(_GOOD))
    assert [r["guarantee"] for r in rows] == [n for n, _ in LAYER1_GUARANTEES]


def test_every_verdict_is_one_of_the_declared_five():
    rows = guarantee_verdicts(evaluate_build(_GOOD))
    for r in rows:
        assert r["verdict"] in GUARANTEE_VERDICTS, r


def test_a_guarantee_with_nothing_to_examine_is_not_established_not_passed():
    """THE finding. A record carrying no evidence at all must not produce a
    VALIDATED_IN_IMAGE that reads as satisfied, in any of its spellings."""
    bare = {"name": "bare", "image": "x@sha256:d", "image_digest": "sha256:d",
            "mode": "adopt", "requested_tools": ["x"]}
    rows = guarantee_verdicts(evaluate_build(bare))
    assert _verdict(rows, "VALIDATED_IN_IMAGE") in (NOT_ESTABLISHED, FAILED)


def test_an_unobserved_clause_downgrades_its_guarantee_rather_than_being_dropped():
    """`BUILT` has two clauses. With the architecture unread, the guarantee is
    PARTLY established — not established (which over-claims) and not failed
    (which under-claims). Collapsing the middle state is how the old prose got
    to print an unqualified BUILT over a record that never read the arch."""
    no_arch = {k: v for k, v in _GOOD.items() if k != "image_arch"}
    rows = guarantee_verdicts(evaluate_build(no_arch))
    assert _verdict(rows, "BUILT") == PARTLY_ESTABLISHED
    assert _verdict(rows, "BUILT") != _verdict(guarantee_verdicts(evaluate_build(_GOOD)), "BUILT")


def test_a_fully_checked_guarantee_is_established():
    rows = guarantee_verdicts(evaluate_build(_GOOD))
    assert _verdict(rows, "BUILT") == ESTABLISHED
    assert _verdict(rows, "VALIDATED_IN_IMAGE") == ESTABLISHED


def test_not_applicable_is_not_the_same_verdict_as_not_established():
    """The distinction env_honesty calls "the entire point". A record claiming no
    accelerator and not gated has a POLICY_CLEAN whose precondition is genuinely
    absent; a record whose evidence was never run has one that should exist and
    doesn't. Both are "not established" to a boolean, and they mean opposite
    things to a reader deciding whether to trust the artifact."""
    rows = guarantee_verdicts(evaluate_build(_GOOD))
    assert _verdict(rows, "POLICY_CLEAN") == GUARANTEE_NOT_APPLICABLE
    assert GUARANTEE_NOT_APPLICABLE != NOT_ESTABLISHED


def test_a_violation_makes_its_guarantee_read_failed():
    """A clause that RAN AND OBJECTED must not surface as anything softer. The
    talos_v11 page rendered a green tick over a refused contract."""
    cheat = {**_GOOD,
             "verifications": [{"label": "x", "tool": "x", "check": "true || x",
                                "passed": True}]}
    rows = guarantee_verdicts(evaluate_build(cheat))
    assert _verdict(rows, "VALIDATED_IN_IMAGE") == FAILED


def test_a_failed_guarantee_names_the_clause_that_objected():
    cheat = {**_GOOD,
             "verifications": [{"label": "x", "tool": "x", "check": "true || x",
                                "passed": True}]}
    row = next(r for r in guarantee_verdicts(evaluate_build(cheat))
               if r["guarantee"] == "VALIDATED_IN_IMAGE")
    assert row["failed_clauses"], row
    assert row["notes"], "a failed guarantee with no reason sends the reader hunting"


# -- the page ----------------------------------------------------------------

def test_the_page_prints_the_derived_verdict_for_every_guarantee():
    html = render_env_report_html(_GOOD)
    for name, _ in LAYER1_GUARANTEES:
        assert name in html, f"{name} is promised by the roster and absent from the page"


def test_the_page_does_not_report_unrun_evidence_as_verified():
    """End to end, in the artifact a human opens: the exact record shape that
    used to render "every requested tool re-ran green"."""
    bare = {"name": "bare", "image": "x@sha256:d", "image_digest": "sha256:d",
            "mode": "adopt", "requested_tools": ["x"]}
    html = render_env_report_html(bare)
    assert "no evidence was run in the shipped image" in html
    for lie in ("re-ran green", "(I13) passed", "and passed"):
        assert lie not in html, f"the page still asserts {lie!r} over a record with no evidence"


def test_the_page_and_the_coverage_table_cannot_disagree():
    """They disagreed on three shipped records. They now have one source, so the
    only way back to disagreement is a renderer that recomputes — which is what
    the source-level guard below forbids."""
    bare = {"name": "bare", "image": "x@sha256:d", "image_digest": "sha256:d",
            "mode": "adopt", "requested_tools": ["x"]}
    html = render_env_report_html(bare)
    contract = evaluate_build(bare)
    # every clause the contract could not check is named on the page as such
    for c in contract.unobserved:
        assert c.clause in html, f"{c.clause} was unobserved and the page never says so"


# -- the guard that keeps it that way ----------------------------------------

def test_the_badge_map_renders_every_verdict_state():
    """A verdict state added upstream must not fall through to raw text — or,
    worse, to a badge that means something else. Exhaustiveness is asserted
    rather than assumed because the five states exist precisely so that "not
    established" and "not applicable" cannot be shown as the same thing."""
    from agent.skills.env_report_html import _VERDICT_BADGE
    assert set(_VERDICT_BADGE) == set(GUARANTEE_VERDICTS), (
        f"the ENV report renders {sorted(_VERDICT_BADGE)} but env_honesty declares "
        f"{sorted(GUARANTEE_VERDICTS)}")


def test_the_build_method_cannot_change_what_the_page_says_was_verified():
    """THE MECHANISM, asserted directly. The defect was one authored narrative
    per `build_method`, so the same contract outcome read differently depending
    on how the bytes arrived — and the adopt branch's narrative was the one that
    said "(I13) passed" over a record with no licence observation at all.

    Provenance may still differ between the two (it genuinely IS different).
    What was VERIFIED may not."""
    built = {**_GOOD, "mode": "build", "build_method": "container-native"}
    adopted = {**_GOOD, "mode": "adopt", "build_method": "adopt-image"}

    rows_b = {r["guarantee"]: r["verdict"] for r in guarantee_verdicts(evaluate_build(built))}
    rows_a = {r["guarantee"]: r["verdict"] for r in guarantee_verdicts(evaluate_build(adopted))}
    assert rows_b == rows_a, "the contract's verdict moved with the build method"

    html_b, html_a = render_env_report_html(built), render_env_report_html(adopted)
    for name, _ in LAYER1_GUARANTEES:
        for badge in ("established", "not applicable", "FAILED", "partly established"):
            marker = f"<b>{name}</b>"
            in_b = marker in html_b and badge in html_b.split(marker, 1)[1][:400]
            in_a = marker in html_a and badge in html_a.split(marker, 1)[1][:400]
            assert in_b == in_a, (
                f"{name} renders {badge!r} for build_method={built['build_method']} "
                f"but not for {adopted['build_method']}, over an identical contract")


def test_the_renderer_reads_the_verdict_rather_than_recomputing_it():
    src = (ROOT / "agent" / "skills" / "env_report_html.py").read_text()
    assert "guarantee_verdicts(" in src, (
        "the ENV report must derive its guarantee verdicts from env_honesty, not "
        "re-derive them from coverage rows — one reading per field")


# ---------------------------------------------------------------------------
# F11 — the headline that was not qualified.
#
# `N/N validated in image` is the line a reader takes away, and the S4a specimen
# printed it over an env whose tool cannot import its own plotting module: the
# evidence was `--help`, which argparse answers before any dependency is touched.
# The per-tool table already badged depth (`⚠ version`); the header did not. The
# page qualified its small print and not its headline.
#
# Note what is NOT done here. The contract's verdict is untouched, and
# VALIDATED_IN_IMAGE still reads `established` for that env — correctly, because
# the contract established exactly what it checks. `evidence_depth` is a
# structural reading of command TEXT that env_honesty's own comment records
# under-reporting a command which runs `DESeq()` and asserts on the result;
# hanging a guarantee verdict on it would trade a quiet over-claim for a loud
# wrong one. Disclosure, not a gate — the same ruling depth has always had here.
# ---------------------------------------------------------------------------

_PRESENCE_ONLY = {**_GOOD,
                  "verifications": [{"label": "x", "tool": "x", "check": "command -v x",
                                     "passed": True, "control": "discriminating"}]}
_FUNCTIONAL = {**_GOOD,
               "verifications": [{"label": "x", "tool": "x",
                                  "check": "x sort in.bam -o out.bam && test -s out.bam",
                                  "passed": True, "control": "discriminating"}]}


def test_a_presence_only_env_does_not_get_an_unqualified_validated_headline():
    html = render_env_report_html(_PRESENCE_ONLY)
    assert "1/1 validated in image" in html, "the count itself is still true and stays"
    assert "presence/version probe rather than a functional run" in html


def test_an_env_whose_evidence_runs_the_tool_is_not_qualified():
    """The negative case, so the qualifier cannot be a constant."""
    html = render_env_report_html(_FUNCTIONAL)
    assert "1/1 validated in image" in html
    assert "presence/version probe rather than a functional run" not in html


def test_the_headline_and_the_per_tool_badge_read_depth_the_same_way():
    """They were two spellings of one question, which is why one got qualified
    and the other did not."""
    from agent.skills.env_report_html import _shallow_evidence_tools
    assert _shallow_evidence_tools(_PRESENCE_ONLY) == ["x"]
    assert _shallow_evidence_tools(_FUNCTIONAL) == []


def test_failed_evidence_is_not_also_reported_as_shallow():
    """A check that did not pass is already refusing; calling it shallow on top
    answers a question nobody asked and inflates the qualifier's count."""
    from agent.skills.env_report_html import _shallow_evidence_tools
    failed = {**_GOOD,
              "verifications": [{"label": "x", "tool": "x", "check": "command -v x",
                                 "passed": False}]}
    assert _shallow_evidence_tools(failed) == []


def test_the_contract_verdict_is_deliberately_not_downgraded_by_depth():
    """Pinned as a DECISION, not an oversight. If someone later wires depth into
    the guarantee verdict, this fails and they have to read the reasoning above
    before overriding it."""
    rows = guarantee_verdicts(evaluate_build(_PRESENCE_ONLY))
    assert _verdict(rows, "VALIDATED_IN_IMAGE") == ESTABLISHED


def test_no_coverage_clause_is_orphaned_from_the_bullets():
    """A clause whose top-level name is not in the roster would render in the
    detail table and in NO bullet — present in the small print, absent from the
    summary a reader actually reads. The registry lint catches this at the source
    level; this catches it at runtime, over a record that exercises every clause."""
    contract = evaluate_build(_GOOD)
    rows = guarantee_verdicts(contract)
    claimed = {c for r in rows for c in r["clauses"]}
    orphans = sorted({c.clause for c in contract.coverage} - claimed)
    assert not orphans, (
        f"{orphans} appear in the coverage table and under no guarantee bullet")


# ---------------------------------------------------------------------------
# THE RECURRENCE. Caught by an audit hours after the fix above shipped, in the
# SAME commit that shipped it.
#
# The "Provenance of the bytes" block sits below the guarantee bullets and says
# where the image came from. Its non-adopt branch read: "installed and VALIDATED
# inside the image that ships … the bytes VALIDATED are the bytes that run on
# HPC" — authored, branched on build method, emitted unconditionally, over a
# record that may carry zero verifications. That is F2 exactly, at one-tenth the
# size, reintroduced by the fix for F2.
#
# It slipped because the guards above check the guarantee bullets: the badge map
# is exhaustive over verdicts, and the build-method test compares BADGES. Neither
# looks at prose that is legitimately allowed to differ by build method. So the
# lesson is not "add another string to a ban-list" — it is that a section allowed
# to branch needs its own rule about what it may SAY.
#
# The rule: provenance describes where bytes CAME FROM. Any sentence about what
# was checked in them belongs to the contract, which renders itself.
# ---------------------------------------------------------------------------

_PROVENANCE_HEAD = "<h3 style=\"margin-top:1.2rem\">Provenance of the bytes</h3>"

#: Outcome verbs. In the guarantee bullets these come from the contract and are
#: fine; in a hand-written provenance sentence every one of them is a claim
#: nobody checked. Narrow list, narrow scope — that is what makes it usable.
_OUTCOME_WORDS = ("validated", "verified", "proven", "passed", "re-ran green", "checked")


def _provenance_block(html: str) -> str:
    assert _PROVENANCE_HEAD in html, "the provenance block moved — update this guard deliberately"
    block = html[html.index(_PROVENANCE_HEAD):].split("</ul>", 1)[0]
    # Drop the heading (the word "Provenance" contains "proven") and the tagged
    # cross-reference to the contract bullet, which is wanted — pointing AT the
    # answer is the opposite of asserting it.
    block = block.replace(_PROVENANCE_HEAD, "")
    return re.sub(r"<code>[^<]*</code>", "", block)


def test_the_provenance_block_makes_no_claim_about_what_was_checked():
    """Both branches, because the defect was in the one nobody was looking at."""
    for rec, what in ((_GOOD, "built"),
                      ({**_GOOD, "mode": "adopt", "build_method": "adopt-image"}, "adopted")):
        scanned = _provenance_block(render_env_report_html(rec)).lower()
        offenders = [w for w in _OUTCOME_WORDS
                     if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", scanned)]
        assert not offenders, (
            f"the {what} provenance bullet asserts {offenders} — that is an outcome, and "
            f"outcomes are rendered from the contract, not typed here")


def test_a_record_with_no_evidence_gets_no_validation_claim_from_provenance():
    """End to end on the record shape that makes the claim false: zero
    verifications, on the BUILT branch where the recurrence lived.

    Deliberately scoped to the provenance block rather than the whole page. A
    first attempt banned the literal "the bytes validated are the bytes" document-
    wide and failed — because that phrase is part of LAYER1_GUARANTEES' own
    DEFINITION of what VALIDATED_IN_IMAGE means, rendered directly beside a FAILED
    badge and the words "no evidence was run in the shipped image". Saying what a
    guarantee would establish and saying that it did are different acts, and a
    guard that cannot tell them apart would push the page toward explaining less.
    """
    bare = {"name": "bare", "image": "x@sha256:d", "image_digest": "sha256:d",
            "mode": "build", "build_method": "container-native", "requested_tools": ["x"]}
    html = render_env_report_html(bare)
    assert "no evidence was run in the shipped image" in html

    scanned = _provenance_block(html).lower()
    offenders = [w for w in _OUTCOME_WORDS
                 if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", scanned)]
    assert not offenders, (
        f"provenance asserts {offenders} over a record whose contract established "
        f"nothing — the exact shape of F2, in the block below the fix for F2")
