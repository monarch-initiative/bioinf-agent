"""Phase 7 — the user-facing layer: a toggleable report theme + a Talos-style guide.

Two deliverables, two honesty contracts:

  THEME (env_report_html / run_dashboard_html). The two reports share ONE stylesheet
  and now carry TWO palettes (cyberpunk default + a professional/light theme) switched
  by an in-page toggle. The tidy discipline that makes the toggle WORK is testable: no
  raw hex may live below the :root palette blocks, because a stray literal is a colour
  that silently won't switch. That guard is the whole point of the refactor.

  GUIDE (user_guide.render_user_guide). The guide is a copy-pasteable WALKTHROUGH of how
  to run the tool on the compute resource it was validated against — the Talos shape,
  generated honestly. The SKELETON (prerequisites, the `srun` line, `module load`, the
  ordered commands) is DERIVED from the verified record; the `srun` placement is the one
  the run RECORDED, or a clearly-labelled example when none was. The NARRATIVE (overview,
  traps) is agent-AUTHORED and OPTIONAL — it appears only when supplied and is never
  fabricated. `executed_commands` stays the single honesty hook: a step that didn't pass
  can't appear.
"""
from __future__ import annotations

import re

import pytest

from env_records import env_record

from agent.skills.env_report_html import _CSS, render_env_report_html
from agent.skills.run_dashboard_html import render_run_dashboard_html
from agent.skills.user_guide import render_user_guide


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


def _cluster_spec(cluster_slurm=None) -> dict:
    """A sealed spec whose one step ran on the cluster — carries the placement/modules
    a compute-resource walkthrough is built from."""
    step = {
        "step": 1, "tool": "samtools",
        "command": "samtools flagstat /data/in.bam > /data/out.txt",
        "returncode": 0, "validation_status": "passed",
        "detected_outputs": ["/data/out.txt"],
        "validation_locus": "cluster",
        "cluster_node": "c0315",
        "cluster_apptainer_module": "apptainer",
        "cluster_nextflow_module": "nextflow",
    }
    if cluster_slurm is not None:
        step["cluster_slurm"] = cluster_slurm
    return {"workflow_name": "cluster_wf", "pipeline_steps": [step],
            "usage": {"outputs": [{"name": "stats", "files": ["out.txt"]}]}}


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
        assert ':root[data-theme="cyber"]' in html   # cyberpunk (default)
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


# ---------------------------------------------------------------------------
# GUIDE — the Talos walkthrough, generated honestly
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_guide_is_a_walkthrough_with_the_talos_sections_in_order():
    md = render_user_guide(_cluster_spec({"time": "00:30:00", "mem": "8g"}), _freeze_record())
    for a, b in (("What you need before you start", "## Run it"),
                 ("## Run it", "## TL;DR")):
        assert a in md and b in md and md.index(a) < md.index(b)


@pytest.mark.integration
def test_guide_srun_uses_the_recorded_placement():
    md = render_user_guide(_cluster_spec({"time": "00:30:00", "mem": "8g", "cpus_per_task": "2"}),
                           _freeze_record())
    assert "srun --time=00:30:00 --mem=8g --cpus-per-task=2 --pty bash" in md
    assert "# example" not in md          # a real placement is NOT an example
    assert "module load apptainer" in md  # recorded module, not invented


@pytest.mark.integration
def test_guide_labels_an_example_when_no_placement_was_recorded():
    md = render_user_guide(_cluster_spec(cluster_slurm=None), _freeze_record())
    # cluster step but no recorded slurm → a clearly-labelled example, never passed off
    # as the validated placement
    assert "srun " in md and "# example" in md


@pytest.mark.integration
def test_guide_authored_slots_appear_only_when_supplied():
    spec, fr = _cluster_spec({"time": "1:00:00"}), _freeze_record()
    with_narr = render_user_guide(spec, fr, narrative={
        "overview": "It counts alignment records.",
        "traps": ["Index the BAM first."]})
    assert "## What cluster_wf does" in with_narr
    assert "It counts alignment records." in with_narr
    assert "## Traps to avoid" in with_narr and "Index the BAM first." in with_narr
    # absent narrative → the authored sections simply do not exist (never fabricated)
    bare = render_user_guide(spec, fr)
    assert "## What " not in bare.replace("## What you need", "")   # the overview heading only
    assert "## Traps to avoid" not in bare


@pytest.mark.integration
def test_guide_honesty_hook_hides_a_step_that_did_not_pass():
    """executed_commands is the single source of run commands: a failed (rc!=0) or
    unvalidated step cannot surface its command — the guide can only show what ran green."""
    spec = _run_spec(steps=[
        {"step": 1, "tool": "good", "command": "GOODCMD --in a --out b",
         "returncode": 0, "validation_status": "passed", "detected_outputs": ["/b"]},
        {"step": 2, "tool": "bad", "command": "BADCMD_failed --in a",
         "returncode": 1, "validation_status": "failed"},
        {"step": 3, "tool": "unval", "command": "UNVALIDATED_CMD --in a",
         "returncode": 0},   # ran clean but has NO validation record
    ])
    md = render_user_guide(spec, _freeze_record())
    assert "GOODCMD" in md
    assert "BADCMD_failed" not in md
    assert "UNVALIDATED_CMD" not in md


@pytest.mark.integration
def test_guide_titles_a_sealed_spec_by_its_workflow_name():
    """A sealed WorkflowSpec has workflow_name (a draft has pipeline_name); the guide
    renders from either and must not fall back to the generic 'pipeline'."""
    md = render_user_guide({"workflow_name": "my_sealed_wf", "pipeline_steps": []},
                           _freeze_record())
    assert md.startswith("# my_sealed_wf — how to run it")


@pytest.mark.integration
def test_the_meter_is_visible():
    pinned = 11
    print(f"\nPHASE-7 REPORTS+GUIDE: {pinned} behaviours pinned "
          f"(theme toggle · two palettes · no-stray-hex · one-stylesheet · determinism · "
          f"Talos-shape · recorded-srun · example-label · authored-slots · honesty-hook · name)")
    assert pinned == 11
