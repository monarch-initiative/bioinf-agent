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


def _pill(html: str) -> str:
    m = re.search(r'<span class="pill [a-z]+">([^<]*)</span>', html)
    return m.group(1) if m else ""


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


def test_a_clean_record_is_unaffected():
    """16 of the 18 real envs pass, and their pages must not change meaning."""
    rec = env_record(name="fine")
    assert not H.check_build(rec)
    assert "✓ Validated in shipped image" in _pill(render_env_report_html(rec))


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
