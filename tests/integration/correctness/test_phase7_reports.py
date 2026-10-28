"""Phase 7 — the report theme: ONE stylesheet, TWO palettes, an in-page toggle.

The two reports (env_report_html / run_dashboard_html) share one stylesheet and carry a
cyber default plus a professional/light theme switched by an in-page toggle. The tidy
discipline that makes the toggle WORK is testable: no raw hex may live below the :root
palette blocks, because a stray literal is a colour that silently won't switch.
"""
from __future__ import annotations

import re

import pytest

from env_records import env_record

from agent.skills.env_report_html import _CSS, render_env_report_html
from agent.skills.run_dashboard_html import render_run_dashboard_html


# ---------------------------------------------------------------------------
# fixtures — minimal-yet-realistic records
# ---------------------------------------------------------------------------

def _run_spec(steps=None, **extra) -> dict:
    spec = {
        "workflow_name": "demo_wf",
        "pipeline_steps": steps if steps is not None else [
            {"step": 1, "tool": "samtools",
             "command": "samtools view -bo /data/out.bam /data/in.sam",
             "returncode": 0, "validation_status": "passed",
             "detected_outputs": ["/data/out.bam"]},
        ],
    }
    spec.update(extra)
    return spec


def _freeze_record() -> dict:
    return {
        "image": "quay.io/biocontainers/samtools@sha256:deadbeef",
        "image_digest": "sha256:deadbeef", "content_digest": "sha256:c0ffee",
        "build_method": "adopt-image",
        "hpc_delivery": {
            "get_image": "apptainer pull demo.sif docker://quay.io/biocontainers/samtools@sha256:deadbeef",
            "run_example": "apptainer exec --bind /scratch/$USER/data:/data demo.sif <command>",
            "source_note": "adopted public BioContainer — pulled by immutable digest",
            "mode": "adopt",
        },
    }


# ---------------------------------------------------------------------------
# THEME — one stylesheet, two palettes, a toggle, and NO stray hex
# ---------------------------------------------------------------------------

def _both_reports() -> tuple[str, str]:
    env = render_env_report_html(env_record())
    run = render_run_dashboard_html(_run_spec(), _freeze_record())
    return env, run


@pytest.mark.integration
def test_both_reports_carry_the_toggle_and_two_palettes():
    for html in _both_reports():
        assert ':root[data-theme="cyber"]' in html   # cyber (default)
        assert ':root[data-theme="light"]' in html   # professional
        assert 'class="theme-toggle"' in html         # the control
        assert "__toggleTheme" in html                # the flip
        assert "bioinf-theme" in html                 # persisted choice


@pytest.mark.integration
def test_no_raw_hex_below_the_palette_blocks():
    """The tidy discipline that makes the toggle work: every colour flows through a
    variable, so NO raw hex may appear below the two :root palette blocks. A stray
    literal is a colour that won't switch themes — this test is that rule."""
    rest = re.sub(r":root[^{]*\{[^}]*\}", "", _CSS)   # drop the palette blocks
    assert re.findall(r"#[0-9a-fA-F]{3,6}\b", rest) == []


@pytest.mark.integration
def test_both_reports_are_one_visual_family():
    """The run dashboard imports the env report's _CSS — so the ENTIRE stylesheet is
    byte-identical in both. One theme, edited in one place."""
    env, run = _both_reports()
    assert _CSS in env and _CSS in run


@pytest.mark.integration
def test_reports_still_render_deterministically():
    assert render_env_report_html(env_record()) == render_env_report_html(env_record())
    r = _freeze_record()
    assert render_run_dashboard_html(_run_spec(), r) == render_run_dashboard_html(_run_spec(), r)
