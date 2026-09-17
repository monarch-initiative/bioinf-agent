"""The cold-start round-2 honesty-surface fixes, each pinned to its finding.

Round 2 drove a clean clone through setup → freeze → seal with a reader who had
only the README, and filed ~20 findings. The cluster fixed here is the one the
v1 work order calls "the honesty surface": places where a durable artifact or a
refusal message said LESS or OTHER than what the record beside it knew.

  CS14/CS62  `degraded` was the word the README teaches and no durable artifact
             printed it — the outcome tag lived only in the freeze return value.
  CS20       a `degraded` seal rendered a green `fully_validated` headline, and
             the remedy sentence never reached the page.
  CS63       a brand-new recipe said it "was frozen before the build transcript
             was captured" and prescribed a re-freeze that reproduces the warning.
  CS16       the evidence refusal asserted "the tool is not provably present" over
             a record whose own `out` field held the tool's banner; the shape
             refusal's remedy (`… > /dev/null`) was a trap for stderr-printing
             tools, and its message hardcoded `head`/`grep` from someone else's
             command.
  CS61       the ENV report's empty long-tail state said "pure conda env" on a
             pip-built env, contradicting its own Mode and Install-tier rows.
  CS17       list_jobs dropped `returncode`, sorted alphabetically while its doc
             said newest-first, and truncated every backgrounded tool's command
             to the same interpreter-path stub.
  CS19       evidence depth graded a bare banner probe `functional` off its own
             capture redirect (pinned in test_evidence_shape_contract.py).
  CS21       the inputs table read as exhaustive while deliberately excluding
             index sidecars, with nothing saying the exclusion is a design choice.
  CS22       a step whose command begins `mkdir -p … && samtools …` was titled
             "mkdir" — the fallback tool was the command's first token.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from env_records import env_record  # noqa: E402
from workflow_records import pipeline_step  # noqa: E402

from agent.models.core_data import default_step_tool  # noqa: E402
from agent.skills import env_honesty as H  # noqa: E402
from agent.skills.env_recipe_render import render_recipe_markdown  # noqa: E402
from agent.skills.env_report_html import render_env_report_html  # noqa: E402
from agent.skills.run_dashboard_html import render_run_dashboard_html  # noqa: E402


# ---------------------------------------------------------------------------
# CS14 / CS62 — the ENV report prints the outcome tag + the coverage advisory
# ---------------------------------------------------------------------------

def test_env_report_prints_degraded_and_the_advisory_for_an_unobserved_record():
    rec = env_record()   # clean contract, several UNOBSERVED clauses
    assert not H.check_build(rec)
    assert H.evaluate_build(rec).unobserved, "fixture must have coverage gaps"
    html = render_env_report_html(rec)
    assert ">degraded<" in html, "the README's word must appear on the deliverable"
    # the advisory is the SAME sentence coverage_disclosure writes into the freeze
    # return — the page and the chat answer must be checkable against each other
    assert "coverage tag, not a failure" in html


def test_env_report_prints_no_outcome_tag_over_a_failed_contract():
    """proven/degraded is vocabulary for REGISTERED envs. A record that fails the
    contract gets the ⛔ section; printing either tag next to it would soften it."""
    rec = env_record(verifications=[{
        "label": "multiqc", "tool": "multiqc",
        "check": "multiqc --help | head -5", "rc": 0, "passed": True, "out": "x"}])
    assert H.check_build(rec), "fixture must violate the contract"
    html = render_env_report_html(rec)
    assert ">degraded<" not in html
    assert ">proven<" not in html


# ---------------------------------------------------------------------------
# CS20 — the RUN dashboard states the seal outcome, with the recorded reason
# ---------------------------------------------------------------------------

def _sealed_spec(usage_verification=None, **over) -> dict:
    step = pipeline_step(returncode=0, detected_outputs=["/tmp/out/x.bam"],
                         validation={"x.bam": {"passed": True}})
    step["step"] = 1
    spec = {
        "workflow_name": "wf", "pipeline_steps": [step],
        "validated_in_shipped_image": True,
        "pipeline_status": "fully_validated",
    }
    if usage_verification is not None:
        spec["usage_verification"] = usage_verification
    spec.update(over)
    return spec


def test_a_degraded_seal_no_longer_renders_an_unqualified_green_headline():
    reason = ("no usage block was authored on the draft, so I4 ran nothing — add "
              "patch_pipeline(usage={...}) and re-seal to earn a verified how-to.")
    html = render_run_dashboard_html(_sealed_spec(
        usage_verification={"status": "not_attempted", "reason": reason}))
    assert "degraded — how-to unproven" in html
    # the remedy the seal recorded reaches the page instead of dying in the return
    assert "re-seal to earn a verified how-to" in html


def test_a_proven_seal_renders_proven_not_degraded():
    html = render_run_dashboard_html(_sealed_spec(
        usage_verification={"status": "verified", "trials": [], "locus": "image"}))
    assert ">proven</span>" in html
    assert "degraded" not in html.lower()


def test_the_proven_sentence_claims_only_the_howto_never_the_run():
    """FD3 shape: failed iteration steps + a verified I4. The Run status row says
    failed; the seal-outcome row must not say "the run is validated" one line
    below it (audit finding on the first cut of this row)."""
    failed_step = pipeline_step(
        returncode=1, detected_outputs=["/data/out/bad.bam"],
        validation={"/data/out/bad.bam": {"passed": False,
                                          "validation_method": "tool"}},
        resource_usage=None)
    failed_step["step"] = 1
    ok_step = pipeline_step(returncode=0, detected_outputs=["/tmp/out/x.bam"],
                            validation={"x.bam": {"passed": True}})
    ok_step["step"] = 2
    spec = {
        "workflow_name": "wf", "pipeline_steps": [failed_step, ok_step],
        "validated_in_shipped_image": True, "pipeline_status": "failed",
        "usage_verification": {"status": "verified", "trials": [], "locus": "image"},
    }
    html = render_run_dashboard_html(spec)
    assert ">proven</span>" in html
    assert "the run is validated" not in html


def test_an_unrecorded_seal_outcome_renders_absence_not_a_verdict():
    """A spec sealed before the producer stated an outcome gets NO retroactive
    verdict — same rule as a Layer-1 UNOBSERVED clause."""
    html = render_run_dashboard_html(_sealed_spec(usage_verification=None))
    assert "degraded" not in html.lower()
    assert ">unrecorded</span>" in html


# ---------------------------------------------------------------------------
# CS63 — the recipe warning states the observable fact, not a false age
# ---------------------------------------------------------------------------

def _longtail_recipe(**over) -> dict:
    rec = {
        "recipe_version": 3, "name": "demo", "version": "1.0",
        "platform": "linux/amd64", "build_method": "container-native-build",
        "primary_tools": ["demo"], "conda_deps": [], "conda_lock": {},
        "apt_snapshot": "", "content_digest": "sha256:x", "built_commands": [],
        "install_steps": [{"tool": "demo", "returncode": 0, "installed_packages": [
            {"name": "demo", "version": "1.0",
             "install_method": {"type": "pip", "package": "demo",
                                "version": "1.0"}}]}],
    }
    rec.update(over)
    return rec


def test_recipe_warning_no_longer_asserts_an_age_or_an_unworkable_remedy():
    md = render_recipe_markdown(_longtail_recipe(), env_record())
    assert "Derived, not recorded" in md, "fixture must reach the warning branch"
    assert "was frozen before the build transcript was captured" not in md
    assert "Re-freeze to get the verbatim transcript" not in md
    assert "no verbatim build transcript" in md


def test_recipe_warning_names_the_lock_as_the_anchor_when_one_is_carried():
    md = render_recipe_markdown(
        _longtail_recipe(conda_lock={"pixi.lock": "version: 6"}), env_record())
    assert "Derived, not recorded" in md, "fixture must reach the warning branch"
    assert "pinned by the lock" in md


# ---------------------------------------------------------------------------
# CS16 — the refusal must not assert what the record beside it disproves
# ---------------------------------------------------------------------------

def test_evidence_passed_refusal_points_at_the_command_when_output_was_captured():
    """bwa RAN and printed its banner; the evidence command's redirect order was
    broken. The old message said "the tool is not provably present/runnable in
    what we ship" — an image-level conclusion the same JSON object disproved."""
    rec = env_record(verifications=[{
        "label": "bwa", "tool": "bwa",
        "check": "set -o pipefail; bwa 2>&1 > /tmp/h.txt; grep -q 'Program: bwa' /tmp/h.txt",
        "rc": 1, "passed": False,
        "out": "\nProgram: bwa (alignment via Burrows-Wheeler transformation)\n"}])
    msgs = [v["message"] for v in H.check_build(rec)
            if v["invariant"] == "VALIDATED_IN_IMAGE.evidence_passed"]
    assert msgs, "an unpassed evidence must still refuse"
    assert "CAPTURED OUTPUT" in msgs[0]
    assert "Program: bwa" in msgs[0]          # the disproof is quoted, not buried
    assert "not provably present" not in msgs[0]


def test_evidence_passed_refusal_keeps_the_image_diagnosis_when_nothing_ran():
    rec = env_record(verifications=[{
        "label": "ghost", "tool": "ghost", "check": "ghost --check /tmp/x.txt",
        "rc": 127, "passed": False, "out": ""}])
    msgs = [v["message"] for v in H.check_build(rec)
            if v["invariant"] == "VALIDATED_IN_IMAGE.evidence_passed"]
    assert msgs and "not provably present" in msgs[0]


def test_pipe_shape_refusal_names_the_actual_last_stage_and_a_safe_remedy():
    msg = H.evidence_shape_violation("bwa 2>&1 | grep -q 'Program: bwa'", "bwa")
    assert msg is not None
    assert "`grep`" in msg                       # the real last stage, not `head`/`grep`
    assert "head" not in msg
    # the remedy is the capture idiom with the ORDER called out — not the bare
    # `> /dev/null` that traps every stderr-printing tool
    assert "> /tmp/out.txt 2>&1" in msg
    assert "stderr" in msg


# ---------------------------------------------------------------------------
# CS61 — the empty long-tail state derives from the record's own package kinds
# ---------------------------------------------------------------------------

def test_a_pip_built_env_is_not_called_pure_conda():
    rec = env_record(mode="build",
                     packages=[{"name": "pyphetools", "version": "0.9.119",
                                "kind": "pypi", "license": ""}])
    html = render_env_report_html(rec)
    assert "pure conda env" not in html
    assert "Not a pure-conda env" in html
    assert "pyphetools" in html


def test_a_conda_only_env_still_says_so():
    rec = env_record(mode="build",
                     packages=[{"name": "samtools", "version": "1.21",
                                "kind": "conda", "license": "MIT"}])
    html = render_env_report_html(rec)
    assert "every install came through the conda layer" in html


# ---------------------------------------------------------------------------
# CS17 — the job ledger can answer "which of my jobs failed"
# ---------------------------------------------------------------------------

def test_list_jobs_reports_returncode_tool_and_newest_first(tmp_path):
    import json

    from agent.skills.job_manager import JobManager
    jm = JobManager.__new__(JobManager)
    jm.jobs_dir = tmp_path
    rows = [
        ("a_older", "2026-09-16T05:02:23+00:00", 1, "freeze"),
        ("z_newest", "2026-09-16T05:05:00+00:00", 0, "seal_workflow"),
        ("m_middle", "2026-09-16T05:03:00+00:00", 0, ""),
    ]
    for jid, iso, rc, tool in rows:
        (tmp_path / f"{jid}.status.json").write_text(json.dumps({
            "job_id": jid, "state": "exited", "command": "x" * 200, "tool": tool,
            "returncode": rc, "start_time_iso": iso, "elapsed_seconds": 1.0}))
    out = jm.list_jobs(include_terminated=True)
    assert [r["job_id"] for r in out] == ["z_newest", "m_middle", "a_older"]
    assert out[2]["returncode"] == 1          # a refused job is distinguishable
    assert out[0]["tool"] == "seal_workflow"
    assert out[1]["tool"] == ""               # absence stays absence on old files


# ---------------------------------------------------------------------------
# CS21 — the inputs table discloses what it deliberately does not cover
# ---------------------------------------------------------------------------

def test_inputs_table_discloses_the_sidecar_exclusion():
    spec = _sealed_spec(test_data={"r1": "/data/r1.fq",
                                   "content_anchors": {"r1": {"sha256": "0" * 64}}})
    html = render_run_dashboard_html(spec)
    assert "deliberately" in html and "not content-pinned" in html


# ---------------------------------------------------------------------------
# CS22 — a step is titled by its work, not its shell prelude
# ---------------------------------------------------------------------------

def test_default_step_tool_skips_shell_prelude():
    assert default_step_tool(
        "mkdir -p /w/out && samtools flagstat /w/a.bam") == "samtools"
    assert default_step_tool("set -o pipefail; bwa mem ref.fa") == "bwa"
    assert default_step_tool("env FOO=1 bcftools view a.vcf") == "bcftools"
    assert default_step_tool("/usr/bin/time -v seqkit stats a.fq") == "seqkit"


def test_default_step_tool_skips_a_wrapper_flags_value_not_just_the_flag():
    """`nice -n 10 samtools …` must land on samtools, not on "10" (audit finding:
    popping only `-` tokens left the flag's VALUE as the command word). And a flag
    that takes no value must not eat the command — `time -v STAR` is STAR."""
    assert default_step_tool("nice -n 10 samtools sort /w/a.bam") == "samtools"
    assert default_step_tool("ionice -c 3 bwa mem ref.fa") == "bwa"
    assert default_step_tool("env -u DISPLAY fastqc /w/a.fq") == "fastqc"
    assert default_step_tool("time -v STAR --runMode alignReads") == "STAR"


def test_default_step_tool_states_the_truth_of_a_prelude_only_command():
    assert default_step_tool("mkdir -p /w/out") == "mkdir"
    # a bare wrapper falls back to its own name, never to ""
    assert default_step_tool("env") == "env"
    assert default_step_tool("") == ""
