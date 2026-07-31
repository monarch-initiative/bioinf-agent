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
