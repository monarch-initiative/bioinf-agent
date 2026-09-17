"""Seam A mutation red-team — every historical war-story shape for pipeline_steps
must be UNCONSTRUCTIBLE.

The typed-records program's per-seam step 4: the model is only worth the walk
clauses it retired if the shapes those clauses (and their neighbors) used to
catch — plus the shapes nothing caught — cannot come into existence. Each test
here is one defect class with its origin named. Violating records are built as
raw dicts ON PURPOSE (the workflow_records rule: never build one by accident).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.models.core_data import (
    PipelineStep, ResourceUsage, StepInput, ValidationRecord, is_path_like,
)
from workflow_records import pipeline_step, resource_usage


def _valid() -> dict:
    return pipeline_step()


# ---------------------------------------------------------------------------
# Key-dialect drift — the shipped_binaries defect class, one layer up
# ---------------------------------------------------------------------------

def test_a_typod_field_cannot_vanish_into_extras():
    """extra="allow" let `detected_ouputs` (typo) ride silently while I3 read
    `detected_outputs` and found nothing — the vanish-into-extras trap that
    motivated declaring remote_outputs long before this seam."""
    bad = _valid()
    bad["detected_ouputs"] = bad.pop("detected_outputs")
    with pytest.raises(ValidationError, match="detected_ouputs"):
        PipelineStep.model_validate(bad)


def test_a_resource_usage_key_dialect_cannot_exist():
    """`peak_cpu_percent` lived in a committed fixture for months — a key no
    producer has ever emitted, green under the walk because the walk only
    looked for wall_seconds/peak_rss_mb."""
    with pytest.raises(ValidationError, match="peak_cpu_percent"):
        ResourceUsage.model_validate({"wall_seconds": 1.0, "peak_rss_mb": 2.0,
                                      "peak_cpu_percent": 3.0})


def test_a_validation_record_without_passed_cannot_exist():
    """`{"valid": True}` lived in a committed fixture — a record I3 counted as
    coverage AND as not-failed, i.e. a validation that can never fail. A record
    that cannot say pass/fail is not a validation."""
    with pytest.raises(ValidationError, match="passed"):
        ValidationRecord.model_validate({"valid": True, "expected_type": "txt"})


def test_a_step_input_with_an_undeclared_key_cannot_exist():
    with pytest.raises(ValidationError, match="pth"):
        StepInput.model_validate({"pth": "/abs/in.bam"})


# ---------------------------------------------------------------------------
# Fabricating defaults / silent absences
# ---------------------------------------------------------------------------

def test_a_step_without_a_returncode_cannot_exist():
    """rc used to be Optional and status defaulted to "validated" — a step
    nobody observed running read as a clean one. Every producer observes an
    exit code (env_manager returns -1 even when the spawn fails)."""
    bad = _valid()
    del bad["returncode"]
    with pytest.raises(ValidationError, match="returncode"):
        PipelineStep.model_validate(bad)


def test_status_is_derived_from_returncode_not_asserted():
    """The producer cannot claim "validated" over a failing rc."""
    rec = PipelineStep.model_validate({**_valid(), "returncode": 1,
                                       "status": "validated"})
    assert rec.status == "failed"


def test_outputs_no_longer_fabricates_an_empty_list():
    """`outputs: list[str] = []` stamped an empty list into every sealed spec
    while the truth lived in detected_outputs — the named counter-example the
    typed-records plan opens with. Absence now dumps as absence."""
    rec = PipelineStep.model_validate(_valid())
    assert rec.outputs is None
    assert "outputs" not in rec.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# The absorbed walk clauses (I6.absolute_paths, I7.resource_usage_recorded)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("inputs", [{"path": "data/rel/in.bam"}]),
    ("detected_outputs", ["rel/out.bam"]),
    ("remote_outputs", ["scratch/wf/out.tsv"]),
])
def test_relative_paths_are_unconstructible_in_every_path_channel(field, value):
    bad = {**_valid(), field: value}
    with pytest.raises(ValidationError, match="absolute"):
        PipelineStep.model_validate(bad)


def test_placeholders_and_bare_tokens_are_not_paths():
    """The rule is scoped exactly as the walk clause was: `{INPUT_VCF}`, `$HOME`,
    `<stdin>` and bare tokens pass through — is_path_like is the ONE reading."""
    ok = {**_valid(),
          "detected_outputs": ["{OUTPUT_DIR}/x.bam", "$TMPDIR/y.txt", "token"]}
    PipelineStep.model_validate(ok)   # must not raise
    assert not is_path_like("{OUTPUT_DIR}/x.bam")
    assert not is_path_like("token")
    assert is_path_like("rel/path.bam")


def test_rc0_without_resource_usage_is_unconstructible():
    bad = _valid()
    del bad["resource_usage"]
    with pytest.raises(ValidationError, match="resource_usage"):
        PipelineStep.model_validate(bad)


def test_a_failed_step_needs_no_resource_usage():
    """The cluster failure recorder states rc=-1 with no monitor observation —
    a step that never ran cannot be asked for cost data."""
    rec = PipelineStep.model_validate({
        "step": 1, "tool": "fastp", "command": "fastp --version",
        "returncode": -1,
        "attempted_inputs": [{"path": "/abs/in.fq"}],
        "detected_outputs": [],
        "validation_locus": "cluster",
        "failure_code": "run_cluster.job_died",
        "failure_error": "TIMEOUT",
    })
    assert rec.status == "failed"
    assert rec.inputs == []   # not an I8 graph node


# ---------------------------------------------------------------------------
# The producer constructor
# ---------------------------------------------------------------------------

def test_produce_strips_the_funnel_owned_step_number():
    rec = PipelineStep.produce(
        tool="samtools", command="samtools view /a/in.bam",
        returncode=1, inputs=["/a/in.bam"])
    assert "step" not in rec
    assert rec["status"] == "failed"
    assert rec["inputs"] == [{"path": "/a/in.bam", "references": []}]


def test_produce_refuses_what_the_model_refuses():
    with pytest.raises(ValidationError, match="resource_usage"):
        PipelineStep.produce(tool="samtools", command="c", returncode=0,
                             detected_outputs=["/a/out.bam"])


def test_produce_drops_absent_fields_instead_of_stamping_defaults():
    """The dump a producer hands the funnel carries what was OBSERVED — None
    fields drop out, so a local step gains no cluster keys and no outputs=[]."""
    rec = PipelineStep.produce(
        tool="samtools", command="samtools view /a/in.bam",
        returncode=0, inputs=[], detected_outputs=["/a/out.bam"],
        resource_usage=resource_usage())
    assert "outputs" not in rec
    assert "cluster_job_id" not in rec
    assert "remote_outputs" not in rec


# ---------------------------------------------------------------------------
# Pre-flight: a bad input refuses BEFORE the command executes
# ---------------------------------------------------------------------------

def test_a_bad_input_refuses_before_the_command_runs(monkeypatch):
    """Inputs are the one caller-controlled channel into the record. Without
    the pre-flight, a relative path executes the whole command and then
    crashes record construction — run output, rc and validations all
    discarded (audit finding, Seam A). The refusal must arrive with the
    command NEVER run."""
    from agent import mcp_server as ms
    from agent.mcp_tools import run_tools as R
    ran = []
    monkeypatch.setattr(ms._env_mgr, "run_in_env",
                        lambda *a, **k: ran.append(1), raising=False)
    out = R.run_pipeline_step(env_name="x", command="samtools view rel/in.bam",
                              pipeline_id="p", inputs=["rel/in.bam"])
    assert out["code"] == "run_pipeline_step.invalid_inputs"
    assert not ran, "the command executed despite the input refusal"

    out = R.run_pipeline_step(env_name="x", command="c", pipeline_id="p",
                              inputs=[{"path": "/abs/in.bam", "note": "extra key"}])
    assert out["code"] == "run_pipeline_step.invalid_inputs"
    assert not ran


def test_run_in_env_gates_inputs_only_when_recording(monkeypatch):
    """No pipeline_id → no step record → no shape contract to satisfy."""
    from agent import mcp_server as ms
    from agent.mcp_tools import run_tools as R
    monkeypatch.setattr(ms._env_mgr, "run_in_env",
                        lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": ""},
                        raising=False)
    out = R.run_in_env(env_name="x", command="c", inputs=["rel/in.bam"])
    assert out.get("code") != "run_in_env.invalid_inputs"
    out = R.run_in_env(env_name="x", command="c", inputs=["rel/in.bam"],
                       pipeline_id="p")
    assert out["code"] == "run_in_env.invalid_inputs"


# ---------------------------------------------------------------------------
# Wild-shape parity — the records real producers wrote still construct
# ---------------------------------------------------------------------------

def test_the_real_cluster_step_shape_still_constructs():
    """The success shape run_step_on_cluster emits (mirrors the committed
    cluster fixture / the sealed cluster spec), so extra="forbid" provably
    declared every key the cluster producer writes."""
    PipelineStep.model_validate({
        "step": 1, "tool": "fastp", "subcommand": None,
        "purpose": "cluster run of fastp",
        "command": "fastp -i {R1} ...", "status": "validated",
        "validation_status": None, "returncode": 0,
        "inputs": [{"path": "/work/scratch/proj/wf/in_R1.fastq.gz",
                    "references": []}],
        "outputs": [], "depends_on": [], "runtime_seconds": None,
        "output_size_bytes": None,
        "validation": {"/local/out/trimmed.fastq.gz": {
            "passed": True, "validation_method": "tool", "num_seqs": 9911}},
        "resource_usage": {
            "wall_seconds": 14.0, "peak_rss_mb": 350.0, "max_cpu_percent": 133.0,
            "locus": "cluster", "sacct_job_id": "1383460",
            "sacct_rows": [{"job_id": "1383460", "elapsed": "00:00:14",
                            "max_rss": "", "ave_cpu": "", "total_cpu": "00:18.617"}],
            "i7_authoritative": True},
        "output_sha256": {"/local/out/trimmed.fastq.gz": "ab" * 32},
        "detected_outputs": ["/local/out/trimmed.fastq.gz"],
        "remote_outputs": ["/work/scratch/proj/wf/trimmed.fastq.gz"],
        "ran_in_container": True,
        "container_image": "/containers/env.sif",
        "container_image_digest": "sha256:" + "0" * 64,
        "cluster_sif_sha256": "cd" * 32,
        "cluster_image_verified": True,
        "cluster_image_digest_match": True,
        "validation_locus": "cluster",
        "cluster_job_id": "1383460",
        "cluster_workflow_dir": "/work/scratch/proj/wf",
        "cluster_node": "c1131",
        "cluster_state": "COMPLETED",
        "cluster_exit_code": "0:0",
        "cluster_job_verdict": "succeeded",
        "cluster_apptainer_module": "apptainer/1.4.3",
        "cluster_nextflow_module": "nextflow/25.04.6",
        "cluster_slurm": {"time": "00:20:00", "mem": "4G", "cpus": 2},
        "cluster_rendered_files": {"launcher.sh": "#!/bin/bash ..."},
        "gpu_placement": {"gpus": 0, "partition": None, "qos": None,
                          "state": "not_applicable"},
    })


def test_the_failure_recorder_forensics_shape_still_constructs():
    """The extra= keys _record_failed_cluster_step merges for a DIED job."""
    PipelineStep.model_validate({
        "step": 2, "tool": "fastp", "command": "fastp ...", "returncode": -1,
        "attempted_inputs": [{"path": "/work/in.fq"}],
        "detected_outputs": [], "validation_locus": "cluster",
        "failure_code": "run_cluster.job_died",
        "failure_error": "the cluster job did not succeed: TIMEOUT",
        "cluster_job_id": "999", "cluster_workflow_dir": "/work/wf",
        "container_image": "/containers/env.sif",
        "container_image_digest": "sha256:" + "0" * 64,
        "cluster_sif_sha256": None,
        "cluster_job_state": "TIMEOUT",
        "cluster_exit_code": "0:0",
        "cluster_sacct_reason": "TimeLimit",
        "cluster_stdout_log": "/work/wf/wf-999.out",
        "cluster_stderr_log": "/work/wf/wf-999.err",
    })
