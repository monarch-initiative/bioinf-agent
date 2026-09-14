"""
The S7 sea-trial fix sites (findings F18–F21 + the report reorder), pinned.

Every test here encodes a defect measured on the real cluster drive of
2026-08-31, so a later change has to read the reasoning before reverting it:

  F18  filename→type inference existed TWICE (run_tools last-suffix,
       run_cluster_step joined-suffix) and the copies disagreed on
       `x.sorted.bam` — the standard samtools naming convention — so a valid
       cluster BAM recorded `passed: False` under a txt probe. One reading now,
       living next to the validator dispatch it mirrors; unknown returns "any"
       (the gate teaches at seal), never a fabricated concrete type.
  F19  the I4 runner probe consulted only the image TAG; when containerd GC
       dropped the tag mapping the seal degraded with a reason claiming two
       probes ("neither a frozen env image nor a host conda env") of which one
       never ran and the other was false — the digest resolved throughout.
  F20  run_step_on_cluster was the one resource_usage producer not stamping
       i7_authoritative, so genuine sacct measurements — the only budgetable
       numbers in the corpus — rendered as "unknown authority" under a false
       recorded-before-capture explanation.
  F21  the archive-mode get_image advice said "then on the HPC: apptainer
       build …" — verbatim the head-node build the standing rule forbids, and
       not what stage_apptainer_image does (build locally, ship the .sif).
  Reorder: the reviewer reads WHAT they are about to run (env) and WHAT it
       consumed (inputs) before HOW it ran — user ruling 2026-08-31.
"""
from __future__ import annotations

import pytest

from agent.validators.output_validator import infer_validator_type


# ───────────────────────── F18 — one reading of filename→type ──────────────

@pytest.mark.parametrize("filename,expected", [
    ("HG00096_chr22_10K.sorted.bam", "bam"),     # the measured defect, verbatim
    ("x.filtered.vcf", "vcf"),
    ("x.markdup.bam", "bam"),
    ("calls.vcf.gz", "vcf"),
    ("reads.fastq.gz", "fastq"),
    ("reads.fq", "fastq"),
    ("genome.fa", "fasta"),
    ("plain.bam", "bam"),
    ("report.html", "html"),
])
def test_dotted_infixes_resolve_to_the_real_type(filename, expected):
    assert infer_validator_type(filename) == expected


def test_unknown_extension_is_any_never_a_fabricated_concrete_type():
    # "any" routes to the validator's existence/non-empty check at run time and
    # is refused by I3 at seal with its own remedy (declare via output_types).
    # A fabricated "txt" expectation manufactured a FAILED verdict over a valid
    # binary file — a wrong confident answer where "I don't know" was available.
    assert infer_validator_type("mystery.xyz") == "any"
    assert infer_validator_type("no_extension") == "any"


def test_both_runners_share_the_one_reading():
    # The defect was DUPLICATION: two implementations of one question, drifted.
    # Import identity — not equal behavior on sampled inputs — is the guard, so
    # a re-forked copy fails even if it starts out byte-identical.
    from agent.skills import run_cluster_step
    from agent.mcp_tools import run_tools
    from agent.validators import output_validator
    assert run_cluster_step.infer_validator_type is output_validator.infer_validator_type
    assert run_tools._infer_validator_type is output_validator.infer_validator_type
    assert not hasattr(run_cluster_step, "_infer_etype"), (
        "_infer_etype is the second copy F18 deleted; it must not come back")


# ───────────────────────── F19 — the runner probe tries both spellings ──────

def _runner_inputs(tmp_path):
    r1 = tmp_path / "r1.fastq"
    r1.write_text("@r\nACGT\n+\nIIII\n")
    fr = {"image": "s2_align:latest",
          "image_digest": "sha256:" + "51" * 32,
          "platform": "linux-64"}
    draft = {"usage": {
        "command_template": "bwa mem {R1} > {OUTPUT_DIR}/out.sam",
        "trials": [{"name": "t", "substitutions": {"R1": str(r1)}}],
    }}
    return fr, draft


def test_runner_falls_back_to_the_pinned_digest_when_the_tag_rotted(tmp_path, monkeypatch):
    from agent.mcp_tools import workflow_tools
    from agent import mcp_server as _ms
    fr, draft = _runner_inputs(tmp_path)

    def image_digest(ref):
        # The measured daemon state: inspect-by-tag says "No such image" while
        # the digest resolves — same artifact, two spellings.
        return ref if ref.startswith("sha256:") else None

    monkeypatch.setattr(_ms._docker, "image_digest", image_digest)
    runner = workflow_tools._image_usage_runner(fr, draft)
    assert runner is not None
    assert runner.image == fr["image_digest"]


def test_runner_is_none_when_neither_spelling_resolves(tmp_path, monkeypatch):
    from agent.mcp_tools import workflow_tools
    from agent import mcp_server as _ms
    fr, draft = _runner_inputs(tmp_path)
    monkeypatch.setattr(_ms._docker, "image_digest", lambda ref: None)
    assert workflow_tools._image_usage_runner(fr, draft) is None


def test_the_no_runner_reason_no_longer_asserts_probes_nobody_ran():
    from agent.skills import spec_writer
    result = spec_writer.self_test_usage(
        {"usage": {"command_template": "tool {X} > {OUTPUT_DIR}/y",
                   "inputs": [], "outputs": [{"name": "y", "files": ["y"]}]}},
        env_manager=None)
    reason = result.get("reason", "")
    assert "neither a frozen env image nor a host" not in reason, (
        "the old sentence claimed two probes this code cannot see (F19)")
    assert "conda_env" in reason  # names the host-fallback condition it CAN see


# ───────────────────────── F21 — delivery advice never targets the head node ─

def test_archive_delivery_advice_builds_locally_never_on_the_hpc():
    from agent.skills.freeze import apptainer_delivery
    hpc = apptainer_delivery(mode="build", sif_name="x.sif", tarball="x.tar")
    assert "then on the HPC" not in hpc["get_image"]
    assert "LOCALLY" in hpc["get_image"]
    # the runnable command itself survives — advice changed, mechanism did not
    assert "apptainer build x.sif docker-archive://x.tar" in hpc["get_image"]


def test_pull_delivery_advice_names_a_compute_node_not_the_head_node():
    from agent.skills.freeze import apptainer_delivery
    hpc = apptainer_delivery(mode="adopt", sif_name="x.sif",
                             image_by_digest="quay.io/x@sha256:" + "ab" * 32)
    assert "never on the head node" in hpc["get_image"]


# ───────────────────────── dashboard: reorder + staged .sif + script panel ──

def _cluster_spec(**step_extra) -> dict:
    step = {
        "step": 1, "tool": "bwa", "returncode": 0,
        "command": "bwa mem ${ref} ${r1} ${r2} | samtools sort -o ${bam} -",
        "validation_locus": "cluster",
        "cluster_job_id": "66114197", "cluster_node": "c0413",
        "cluster_sif_sha256": "e9e0" + "0" * 60,
        "cluster_image_verified": True,
        "container_image": "/work/CLAUDE_CONTAINERS/s2_align_8ea7288a03a8.sif",
        "cluster_slurm": {"time": "00:15:00", "mem": "8g"},
        "resource_usage": {"wall_seconds": 13.0, "peak_rss_mb": 469.9,
                           "max_cpu_percent": 223.1, "locus": "cluster",
                           "i7_authoritative": True},
        "validation": {"out.bam": {"passed": True, "validation_method": "tool"}},
    }
    step.update(step_extra)
    return {
        "workflow_name": "s7_fix_site_probe",
        "description": "S7 fix-site rendering probe",
        "created_at": "2026-08-31T00:00:00+00:00",
        "env_request_key": "bwa=0.7.19|linux-64|none",
        "env_content_digest": "sha256:" + "8e" * 32,
        "env_image": "s2_align:latest",
        "env_hpc_delivery": {"get_image": "# transfer x.tar … then on the HPC:\n"
                                          "apptainer build x.sif docker-archive://x.tar"},
        "validated_in_shipped_image": True,
        "usage_verified": True,
        "usage": {"command_template": "bwa mem {R1} > {OUTPUT_DIR}/out.sam",
                  "description": "align", "inputs": [], "outputs": []},
        "pipeline_steps": [step],
        "reference_databases": [], "authored_artifacts": [],
        "test_data": {"r1": "data/x/r1.fastq.gz",
                      "content_anchors": {"r1": {"kind": "file", "sha256": "ab" * 32,
                                                 "size_bytes": 1}}},
    }


def _render(spec):
    from agent.skills.run_dashboard_html import render_run_dashboard_html
    return render_run_dashboard_html(spec)


def test_env_and_inputs_render_before_the_evidence():
    html = _render(_cluster_spec())
    env = html.index("<h2>Environment")
    inputs = html.index("Inputs &amp; external sources")
    evidence = html.index("Does it run?")
    howto = html.index("How to run it")
    assert env < inputs < evidence < howto


def test_a_staged_sif_outranks_and_suppresses_the_stored_head_node_advice():
    html = _render(_cluster_spec())
    assert "Staged .sif (cluster)" in html
    assert "never built on the head node" in html
    # the stored F21 text on a pre-fix record must not reach the page when the
    # .sif is already on the cluster — moot advice, and forbidden advice
    assert "then on the HPC" not in html
    assert "Get the image (HPC)" not in html


def test_without_a_staged_sif_the_stored_advice_still_renders():
    spec = _cluster_spec()
    step = spec["pipeline_steps"][0]
    for k in ("cluster_sif_sha256", "cluster_image_verified", "container_image"):
        step.pop(k, None)
    html = _render(spec)
    assert "Get the image (HPC)" in html
    assert "Staged .sif (cluster)" not in html


def test_submitted_files_render_verbatim_when_captured():
    launcher = "#!/bin/bash\n#SBATCH --time=00:15:00\n#SBATCH --mem=8g\n"
    main_nf = "params.ref = '/work/chr22.fa'\nprocess bwa { }\n"
    html = _render(_cluster_spec(cluster_rendered_files={
        "launcher.sh": launcher, "main.nf": main_nf}))
    assert "Submitted files" in html
    assert "#SBATCH --mem=8g" in html
    assert "params.ref" in html
    assert "unrecorded" not in html.split("Submitted files")[1].split("</section>")[0]


def test_uncaptured_submission_is_stated_never_reconstructed():
    html = _render(_cluster_spec())  # no cluster_rendered_files on the step
    assert "Submitted files: <b>unrecorded</b>" in html
    # no fabricated script text: no collapsible file panel appears from thin air
    assert "<summary><code>launcher.sh</code></summary>" not in html


def test_authoritative_cluster_numbers_carry_no_authority_warning():
    html = _render(_cluster_spec())
    assert "unknown authority" not in html
    assert "recorded before the runtime captured whether" not in html


def test_cluster_producer_stamps_sacct_numbers_authoritative():
    # The stamp itself (F20) — read off the producer's source rather than
    # driving the full ssh flow: the success-branch resource_usage must carry
    # i7_authoritative True. A source-level pin, chosen over a mock-heavy
    # re-drive of run_step_on_cluster; the render half is covered above.
    import inspect
    from agent.skills import run_cluster_step
    src = inspect.getsource(run_cluster_step)
    assert '"i7_authoritative": True' in src, (
        "run_step_on_cluster must stamp sacct measurements authoritative (F20)")
