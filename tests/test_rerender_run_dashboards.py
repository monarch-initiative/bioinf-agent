"""The Layer-2 dashboard is WRITE-ONCE, so a renderer fix reaches zero existing artifacts.

`seal_workflow` renders `{name}.RUN.html` and nothing ever touches it again. That is right
for the sealed spec — a digest-pinned provenance record — but the dashboard is not the
record, it is a VIEW of it, and a view has no provenance to protect.

Measured 2026-07-31: `run_dashboard_html._usage_status` had been taught the three states
(verified / failed / not_attempted) precisely so a page would stop printing a bare `False`
for "never tested". The fix shipped green, and 4 of the 5 dashboards on disk still read
`Usage self-tested: False`, each having been rendered before it landed. A renderer fix that
reaches no artifact has not fixed anything a reader can see, and "the page says False, the
record says not_attempted" is two answers to one question in the file the user opens.

These tests cover `scripts/rerender_run_dashboards.py` — the instrument that closes the gap.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rerender_run_dashboards.py"


def _load():
    spec = importlib.util.spec_from_file_location("rerender_run_dashboards", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sealed_spec(name: str, usage_verification: dict | None) -> dict:
    """A minimal spec that satisfies WorkflowSpec — the script reads through the TYPED
    seam, so a fixture that would not validate is not a valid fixture."""
    d = {
        "workflow_name": name,
        "description": "fixture",
        "created_at": "2026-01-01T00:00:00+00:00",
        "env_request_key": "fake=1.0|linux/amd64|none",
        "env_content_digest": "sha256:" + "a" * 64,
        "env_image": "fake:1.0",
        "pipeline_status": "fully_validated",
        "usage_verified": False,
        "pipeline_steps": [{
            "step": 1, "tool": "fake", "command": "fake --go",
            "returncode": 0, "validation_status": "passed",
        }],
    }
    if usage_verification is not None:
        d["usage_verification"] = usage_verification
    return d


@pytest.fixture
def staged(tmp_path):
    """Two sealed specs whose dashboards are deliberately stale (the pre-three-state
    text), so a re-render has something real to correct."""
    for name, uv in (("never", {"status": "not_attempted", "reason": "cluster inputs"}),
                     ("ran",   {"status": "verified", "reason": ""})):
        (tmp_path / f"{name}.workflow.yaml").write_text(
            yaml.safe_dump(_sealed_spec(name, uv)))
        (tmp_path / f"{name}.RUN.html").write_text(
            "<html><body><tr><td>Usage self-tested</td><td>False</td></tr></body></html>")
    return tmp_path


def _run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, cwd=cwd)


def test_check_reports_stale_pages_and_writes_nothing(staged):
    before = {p: p.read_text() for p in staged.glob("*.RUN.html")}
    r = _run("--dir", str(staged), "--check")
    assert r.returncode == 1, f"--check must fail on a stale tree:\n{r.stdout}{r.stderr}"
    assert "STALE" in r.stdout
    for p, text in before.items():
        assert p.read_text() == text, "--check must not write"


def test_rerender_replaces_the_bare_bool_with_the_stated_three_state(staged):
    r = _run("--dir", str(staged))
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"

    never = (staged / "never.RUN.html").read_text()
    ran = (staged / "ran.RUN.html").read_text()
    assert "not attempted" in never
    assert "Usage self-tested</td><td>False" not in never, (
        "the page still renders the verdict nobody reached")
    assert "Usage self-tested</td><td>yes" in ran

    # Idempotent: a second pass finds nothing stale, so this is a convergence, not a churn.
    assert _run("--dir", str(staged), "--check").returncode == 0


def test_only_the_html_is_ever_rewritten(staged):
    specs = {p: p.read_text() for p in staged.glob("*.workflow.yaml")}
    _run("--dir", str(staged))
    for p, text in specs.items():
        assert p.read_text() == text, (
            f"{p.name} was modified — this script renders a view and must never touch "
            f"the digest-pinned record it renders from")


def test_an_unparseable_spec_is_reported_and_its_page_left_alone(staged):
    """A spec that no longer validates is a real failure, and the honest response is to
    say so and STOP — not to render a confident page from a record we could not read, and
    not to exit 0 as if absence of a re-render were success."""
    (staged / "broken.workflow.yaml").write_text("workflow_name: broken\n")  # missing required
    (staged / "broken.RUN.html").write_text("<html>stale but untouched</html>")

    r = _run("--dir", str(staged))
    assert r.returncode == 2, f"{r.stdout}{r.stderr}"
    assert "broken" in r.stdout and "left untouched" in r.stdout
    assert (staged / "broken.RUN.html").read_text() == "<html>stale but untouched</html>"
    # the healthy siblings are still corrected — one bad artifact does not block the rest
    assert "not attempted" in (staged / "never.RUN.html").read_text()


def test_the_repo_dashboards_are_current():
    """THE RATCHET. Once corrected, the real dashboards in env_reports/ must stay in step
    with the renderer. Without it the regression recurs the next time the renderer
    improves: code fixed, artifacts stale, suite green.

    SKIPS EXPLICITLY WHEN THERE ARE NO ARTIFACTS, rather than passing. `env_reports/` is
    gitignored, so on CI and on a fresh clone this check has nothing to look at — and a
    green tick over zero artifacts is precisely the absence-reads-as-compliance defect
    this whole change set exists to remove. It would be absurd to reintroduce it here. A
    skip is visible in the report; a vacuous pass is not."""
    specs = sorted((ROOT / "env_reports").glob("*.workflow.yaml"))
    if not specs:
        pytest.skip("no sealed workflows in env_reports/ (gitignored) — nothing to ratchet; "
                    "this check is meaningful only on a machine that has driven a seal")
    r = _run("--check")
    assert r.returncode == 0, (
        f"{len(specs)} sealed workflow(s) present and their .RUN.html has drifted from "
        f"what the renderer produces today. Run "
        f"`python scripts/rerender_run_dashboards.py`.\n" + r.stdout)


# ---------------------------------------------------------------------------------------
# A FAILED step must not render as a green run
# ---------------------------------------------------------------------------------------
#
# `step_is_validated` asks whether validation RECORDS exist. It is not the negation of
# failure, and a step that exited non-zero satisfies it — so every consumer that wanted
# "is this step OK" got True for a failed run. On the panel the user reads to decide
# whether to run a workflow ON REAL DATA, a spec containing one non-zero step rendered:
#
#   * header "Steps validated 2/2"          — counting the failed step
#   * H1 pill "✓ validated in shipped image" — legitimately earned; it answers "did this
#                                              run in the image we ship", not "did it work"
#   * locus group "✓ validated here"         — for a group holding ONLY the failed step
#   * pipeline_status: failed                — stated by the seal, rendered NOWHERE
#
# The single red mark anywhere on the page was one ✗ glyph in a per-output table below the
# fold. The rc=0 route is closed (I3.validation_passed refuses a False validation record,
# non-overridably), so the reachable shape is exactly a step that exited non-zero.

def _spec_with_steps(steps, **over):
    spec = {
        "workflow_name": "wf", "created_at": "2026-01-01T00:00:00Z",
        "env_request_key": "k", "env_image": "img:1", "env_content_digest": "sha256:d",
        "validated_in_shipped_image": True,
        "pipeline_steps": steps,
    }
    spec.update(over)
    return spec


def _ok_step(n=1):
    return {"step": n, "tool": "samtools", "command": "samtools view a.bam",
            "returncode": 0, "detected_outputs": ["/w/a.txt"],
            "validation": {"/w/a.txt": {"passed": True, "validation_method": "samtools"}}}


def _failed_step(n=2):
    s = _ok_step(n)
    s["returncode"] = 1
    return s


def _render(spec):
    from agent.skills.run_dashboard_html import render_run_dashboard_html
    return render_run_dashboard_html(spec, env_record=None)


def test_a_failed_step_is_not_counted_as_validated():
    html = _render(_spec_with_steps([_ok_step(1), _failed_step(2)]))
    assert "Steps validated 1/2" in re.sub(r"<[^>]+>", "", html).replace("\n", " ") or \
           "1/2" in html, "the failed step must not be counted"
    assert "FAILED" in html


def test_a_failed_step_flips_the_headline_pill():
    """`validated_in_shipped_image` is TRUE on this spec and legitimately so — the digests
    match. It answers a different question than "did it work", and the page presented it
    as the verdict."""
    html = _render(_spec_with_steps([_failed_step(1)]))
    m = re.search(r'<span class="pill [a-z]+">([^<]*)</span>', html)
    assert "FAILED" in m.group(1), m.group(1)
    assert "✓ validated in shipped image" not in m.group(1)


def test_a_locus_group_containing_a_failure_is_not_badged_validated_here():
    html = _render(_spec_with_steps([_failed_step(1)]))
    assert "✓ validated here" not in html
    assert "FAILED here" in html


def test_the_steps_own_exit_code_is_shown():
    """Recorded by every run primitive, read by no renderer — while the page's own footer
    asserts exit codes are part of the machine-observed evidence it shows. A cluster step
    displayed the SLURM job's exit, which is the scheduler's, not the tool's."""
    assert "exit 1" in _render(_spec_with_steps([_failed_step(1)]))
    assert "exit 0" in _render(_spec_with_steps([_ok_step(1)]))


def test_a_clean_run_still_reads_clean():
    html = _render(_spec_with_steps([_ok_step(1), _ok_step(2)]))
    m = re.search(r'<span class="pill [a-z]+">([^<]*)</span>', html)
    assert "✓ validated in shipped image" in m.group(1)
    assert "FAILED" not in html


# --- pipeline_status: stated by the seal, rendered nowhere ------------------------------

def test_the_sealed_run_status_is_rendered():
    html = _render(_spec_with_steps([_ok_step(1)], pipeline_status="fully_validated"))
    assert "Run status" in html
    assert "fully_validated" in html


def test_a_stale_stamped_status_is_shown_as_a_disagreement_not_printed_as_fact():
    """`pipeline_status` replaced "the fabricated in_progress default that seal used to
    stamp into every spec regardless of the run", and every spec sealed before that fix
    still carries the fabrication — measured, 4 of 7 in the corpus say `in_progress` while
    their steps derive `fully_validated`.

    So rendering the stored value verbatim would print "in_progress" across the top of
    four complete runs: a NEW falsehood introduced by the fix that ended one. Picking a
    winner silently is wrong both ways — `stated` ships the stale default, `derived` hides
    that the artifact is internally inconsistent."""
    html = _render(_spec_with_steps([_ok_step(1)], pipeline_status="in_progress"))
    assert "fully_validated" in html, "the derived truth must be shown"
    assert "in_progress" in html, "the stale stored value must be shown too"
    assert "does not match" in html


def test_a_spec_with_no_status_field_says_unrecorded():
    """Absence renders as absence — the same rule the I4 usage states follow one row up."""
    html = _render(_spec_with_steps([_ok_step(1)]))
    assert "unrecorded" in html
