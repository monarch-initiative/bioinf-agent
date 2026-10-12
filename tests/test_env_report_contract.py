"""The ENV report must say when its own env FAILED the honesty contract.

WHAT WENT WRONG. `render_env_report_html` drew `BuildContract.coverage` — whether each
clause RAN — and never once drew `BuildContract.violations` — whether any clause FAILED.
The word "violation" did not appear in the module. The status pill was `passed == total`
over the verifications list, which is a different and much weaker question, and for an
adopted env it did not even ask that: `is_adopt` short-circuited straight to a neutral
"Adopted by digest" badge.

Rendered over the real 18-env corpus on 2026-08-07, two envs FAIL `check_build` and
neither page said so:

    talos_v11   WELL_FORMED.shipped_binaries — the record uses the old key dialect, so its
                contents cannot be read without guessing
                -> rendered "✓ Validated in shipped image"
    multiqc     VALIDATED_IN_IMAGE.evidence_shape — the evidence pipes into `head -5`, so
                the recorded `passed` reports HEAD's exit status. In the clause's own
                words: "it would pass in an image without the tool at all"
                -> rendered "Adopted by digest"

This project is judged on three artifacts a human reads, and the user's stated purpose for
this one is "to make sure the programs I requested are what I asked for". A green tick over
a failed honesty contract is the precise failure the whole codebase exists to prevent,
delivered in the one place the user actually looks.

`check_build` is the SAME function `freeze` refuses on, so the page and the gate now give
one answer rather than two.
"""
from __future__ import annotations

import re

import pytest

from agent.skills import env_honesty as H
from agent.skills.env_report_html import render_env_report_html
from env_records import env_record, env_evidence


def _headline(html: str) -> tuple[str, str]:
    """(css class, text) of the status pill. The class is the colour the reader sees;
    a test that reads only the words would pass a red sentence in a green pill."""
    m = re.search(r'<span class="pill ([a-z]+)">([^<]*)</span>', html)
    return (m.group(1), m.group(2)) if m else ("", "")


def _pill(html: str) -> str:
    return _headline(html)[1]


def _piped_evidence() -> list[dict]:
    """multiqc's real recorded evidence, verbatim. A pipeline exits with its LAST stage's
    status, so `passed` here reports `head`, not the tool."""
    return [{"label": "multiqc", "tool": "multiqc",
             "check": "multiqc --version && multiqc --help | head -5",
             "rc": 0, "passed": True, "out": "multiqc, version 1.21"}]


def test_a_record_with_violations_never_renders_a_green_pill():
    rec = env_record(name="broken", verifications=_piped_evidence())
    assert H.check_build(rec), "fixture must actually violate the contract"
    html = render_env_report_html(rec)
    pill = _pill(html)
    assert "FAILS the honesty contract" in pill, f"pill was {pill!r}"
    assert "✓" not in pill
    assert _headline(html)[0] == "bad", _headline(html)


def test_an_ADOPTED_env_that_fails_the_contract_still_says_so():
    """The sharper half. `is_adopt` short-circuited the pill before any contract question
    was asked, so `multiqc` — an adopted image whose evidence would pass in an image
    WITHOUT the tool — was labelled "Adopted by digest" with no qualification anywhere on
    the page. An adopted image is still an image that has to earn its record."""
    rec = env_record(name="multiqc_like", mode="adopt", build_method="adopt-image",
                     verifications=_piped_evidence())
    assert H.check_build(rec)
    html = render_env_report_html(rec)
    assert "FAILS the honesty contract" in _pill(html)
    assert "Adopted by digest" not in _pill(html)
    assert _headline(html)[0] == "bad", _headline(html)


def test_the_violation_text_reaches_the_page():
    """A pill that says "failed" without saying WHAT failed sends the reader to spin up
    the container and root around — which the user explicitly does not want to do."""
    rec = env_record(name="broken", verifications=_piped_evidence())
    html = render_env_report_html(rec)
    v = H.check_build(rec)[0]
    assert v["invariant"] in html
    assert v["where"] in html
    # The message is the part that names the remedy ("drop the pipe … set -o pipefail").
    # Compared through the same escaper the renderer uses: the message quotes the evidence
    # command verbatim, so it carries `'` and `|` and MUST arrive escaped — a violation
    # message is the one string on this page most likely to contain hostile-looking bytes.
    from agent.skills.env_report_html import _e
    assert _e(v["message"])[:60] in html
    assert "set -o pipefail" in html, "the remedy must survive escaping"


def test_the_failure_notice_precedes_the_content_it_undermines():
    """Ordering is the fix, not decoration. The reader's question on opening the page is
    "can I trust this", so the answer NO has to arrive before the tools table it applies
    to — not in a coverage table hundreds of lines down."""
    rec = env_record(name="broken", verifications=_piped_evidence())
    html = render_env_report_html(rec)
    assert html.index("FAILS the honesty contract") < html.index("<h2>Tools")


def test_a_clean_record_still_renders_green():
    """A passing contract is a green pill. The fixture's evidence is `--version`,
    which proves presence, so the green pill is the presence one."""
    rec = env_record(name="fine")
    assert not H.check_build(rec)
    html = render_env_report_html(rec)
    assert _headline(html) == ("ok", "✓ Present in shipped image · not exercised"), _headline(html)


# ---------------------------------------------------------------------------------------
# The headline says what the evidence showed
# ---------------------------------------------------------------------------------------
#
# "✓ Validated in shipped image" over a Tools table whose every row reads "present in
# the image · not exercised" is the page contradicting itself in the one sentence a
# reader takes away. Depth is disclosure, never a gate: both headlines are green and
# freeze registers both; only the words differ, and they must differ in both directions.

def _ran(tool: str = "samtools") -> dict:
    return {"label": tool, "tool": tool,
            "check": f"{tool} sort /t.bam -o /o.bam && test -s /o.bam",
            "rc": 0, "passed": True, "out": ""}


def _present(tool: str = "samtools") -> dict:
    return {"label": tool, "tool": tool, "check": f"command -v {tool}",
            "rc": 0, "passed": True, "out": f"/opt/conda/bin/{tool}"}


def test_the_headline_says_present_when_every_check_is_presence_depth():
    rec = env_record(name="present", verifications=[_present("samtools"), _present("bwa")])
    assert not H.check_build(rec)
    assert _headline(render_env_report_html(rec)) == \
        ("ok", "✓ Present in shipped image · not exercised")


def test_the_headline_says_validated_only_when_a_tool_ran():
    rec = env_record(name="ran", verifications=[_ran("samtools")])
    assert not H.check_build(rec)
    assert _headline(render_env_report_html(rec)) == ("ok", "✓ Validated in shipped image")


def test_one_exercised_tool_among_presence_checks_earns_validated_and_the_tools_line_qualifies_it():
    """Parity with the tools line: the pill reads the same depth list the per-tool
    badges read, so a page can never be `Present` in the headline and `ran on data`
    in the table, or the reverse."""
    rec = env_record(name="mixed", requested_tools=["samtools", "bwa"],
                     verifications=[_present("samtools"), _ran("bwa")])
    html = render_env_report_html(rec)
    assert _headline(html) == ("ok", "✓ Validated in shipped image")
    assert "For 1 of them the check only confirmed the tool is present" in html


def test_a_failed_check_is_not_softened_into_present():
    rec = env_record(name="half", verifications=[_present("samtools"),
                                                  {**_present("bwa"), "passed": False, "rc": 127}])
    html = render_env_report_html(rec)
    assert "Present in shipped image" not in _pill(html)
    assert _headline(html)[0] == "bad"


def test_a_clean_adopted_record_still_reads_adopted():
    rec = env_record(name="fine_adopt", mode="adopt")
    assert not H.check_build(rec)
    assert "Adopted by digest" in _pill(render_env_report_html(rec))


# ---------------------------------------------------------------------------------------
# CHECKED and UNOBSERVED must not render identically
# ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("cls", ["ok", "warn"])
def test_the_coverage_state_classes_are_actually_defined(cls):
    """The coverage table emits BARE `<span class="ok">checked</span>` and
    `<span class="warn">unobserved</span>`. Only the COMPOUND `.pill.ok` and `.badge.ok`
    existed, which a bare span matches neither of, and `.warn` was not defined at all — so
    both states rendered as identical unstyled text while `n/a` WAS greyed, because
    `.muted` happens to exist.

    The governing principle of this codebase is that absence must never round up into a
    verdict. A table that visually collapses UNOBSERVED into CHECKED does exactly that,
    in pixels, in the one table built to separate them."""
    html = render_env_report_html(env_record(name="styled"))
    css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    assert re.search(r"(^|[,\s}])\." + cls + r"\s*[,{]", css, re.M), (
        f".{cls} is emitted by the coverage table but has no bare CSS selector"
    )


def test_checked_and_unobserved_are_visually_distinguishable():
    html = render_env_report_html(env_record(name="styled"))
    css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    ok = re.search(r"(?:^|[,\s}])\.ok\s*\{([^}]*)\}", css, re.M)
    warn = re.search(r"(?:^|[,\s}])\.warn\s*\{([^}]*)\}", css, re.M)
    assert ok and warn and ok.group(1) != warn.group(1), (
        "checked and unobserved resolve to the same styling"
    )


def test_the_report_survives_a_record_whose_contract_cannot_be_evaluated(monkeypatch):
    """A report must never die on its own audit — but it must also not go quiet. If the
    contract cannot be run, the page says UNCHECKED rather than rendering as though it
    passed, which is the same three-state rule every other clause here follows."""
    import agent.skills.env_report_html as M

    def _boom(*a, **k):
        raise RuntimeError("contract exploded")
    monkeypatch.setattr(M, "render_env_report_html", M.render_env_report_html)
    monkeypatch.setattr(H, "evaluate_build", _boom)
    monkeypatch.setattr(H, "check_build", _boom)
    html = M.render_env_report_html(env_record(name="unevaluable"))
    assert "could not be evaluated" in html
    assert "✓ Validated in shipped image" not in _pill(html)


# ---------------------------------------------------------------------------------------
# The pill is blind to EVERY violation class, not just the two in the corpus
# ---------------------------------------------------------------------------------------
#
# Injecting one violation at a time into a clean record and re-rendering, EIGHT of nine
# classes produced a green page before this fix. Only `passed != total` on a BUILD record
# reddened it — and on an ADOPT record even that rendered "Adopted by digest". So this is
# structural blindness, not two unlucky records, and a test that only pinned the two
# corpus cases would have missed most of it.

def _clean() -> dict:
    return env_record(name="clean", shipped_binaries=[])


@pytest.mark.parametrize("label,mutate", [
    ("evidence failed",       lambda r: r.update(verifications=[{**r["verifications"][0],
                                                                "passed": False, "rc": 127}])),
    ("echo-cheat evidence",   lambda r: r.update(verifications=[{**r["verifications"][0],
                                                                "check": "true || samtools"}])),
    ("piped evidence",        lambda r: r.update(verifications=_piped_evidence())),
    ("cuda without toolkit",  lambda r: r.update(accelerator={"type": "cuda"})),
    ("unparseable binaries",  lambda r: r.update(shipped_binaries=[{"command": "x", "name": "y"}])),
])
def test_every_violation_class_reddens_the_pill(label, mutate):
    rec = _clean()
    mutate(rec)
    assert H.check_build(rec), f"fixture {label!r} did not actually violate the contract"
    assert "FAILS the honesty contract" in _pill(render_env_report_html(rec)), label


@pytest.mark.parametrize("mode", ["build", "adopt"])
def test_the_adopt_branch_does_not_short_circuit_the_contract(mode):
    """`is_adopt` was tested FIRST, so an adopted image skipped the question entirely."""
    rec = env_record(name="x", mode=mode, verifications=_piped_evidence())
    assert "FAILS the honesty contract" in _pill(render_env_report_html(rec))


def test_a_clause_that_ran_and_failed_is_not_shown_as_merely_checked():
    """The coverage column answers "did it run", which is a genuinely different question
    from "did it pass". But for talos_v11 the effect was that the ONE mention of
    `WELL_FORMED.shipped_binaries` anywhere on the page was a row marked `checked` — for
    the clause that had just refused the record. That is worse than the violation being
    absent: it reads as a clean result for the thing that failed."""
    rec = env_record(name="x", shipped_binaries=[{"command": "x", "name": "y"}])
    html = render_env_report_html(rec)
    row = re.search(r"<tr><td><code>WELL_FORMED\.shipped_binaries</code></td><td>(.*?)</td>",
                    html[html.index("Contract coverage"):])
    assert row and "FAILED" in row.group(1), "the failing clause still reads as merely checked"


def test_a_passing_clause_is_not_marked_failed():
    html = render_env_report_html(_clean())
    body = html[html.index("Contract coverage"):]
    assert "and FAILED" not in body
