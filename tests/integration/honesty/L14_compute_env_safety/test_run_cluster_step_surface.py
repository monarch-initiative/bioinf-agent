"""
L14 cheat-guards — run_step_on_cluster orchestration surface.

run_step_on_cluster composes stage_apptainer_image + submit_workflow_job
+ cluster_job_status + cluster_job_resources + download_from_project_path
+ validator into a single Layer-2 step record. These tests pin the
orchestration logic — the underlying primitives have their own
exhaustive cheat-guards.

Pinned here:
  - pipeline_id is required (refuses with clear error)
  - sacct Elapsed parser (HH:MM:SS and D-HH:MM:SS)
  - MaxRSS parser (K/M/G suffix → MB)
  - terminal-state set matches SLURM's reality
  - exit_code parser ("0:0" → 0; bad input → -1)
  - happy-path: all 5 underlying primitives invoked, pipeline_step
    record built with cluster-locus fields, validations called per
    output, draft updated
  - phase failures preserve prior phases' results on the error dict
    (stage failed → error mentions it; submit failed → stage_result
    preserved; poll failed → submit_result preserved)
"""
from __future__ import annotations

import pytest

from agent.skills import cluster_jobs, run_cluster_step


# ===========================================================================
# Parsers — small but seal-blocking if wrong
# ===========================================================================

class TestElapsedParser:
    @pytest.mark.integration
    @pytest.mark.parametrize("inp,want", [
        ("00:00:00",   0.0),
        ("00:00:05",   5.0),
        ("00:01:30",   90.0),
        ("01:00:00",   3600.0),
        ("01-00:00:00", 86400.0),
        ("01-12:00:00", 86400.0 + 12*3600),
        ("00:00:01.5", 1.5),
    ])
    def test_parses_well(self, inp, want):
        assert cluster_jobs._parse_hhmmss(inp) == want

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", ["", "abc", "1:2", "x:y:z", None])
    def test_refuses_garbage(self, bad):
        assert cluster_jobs._parse_hhmmss(bad) == 0.0


class TestMaxRssParser:
    @pytest.mark.integration
    @pytest.mark.parametrize("inp,want", [
        ("0",        0.0),
        ("1024K",    1.0),
        ("512K",     0.5),
        ("100M",     100.0),
        ("1G",       1024.0),
        ("1.5G",     1536.0),
        ("2T",       1024.0 * 1024 * 2),
        ("",         0.0),
    ])
    def test_parses(self, inp, want):
        assert cluster_jobs._parse_max_rss_mb(inp) == want


class TestExitCodeParser:
    @pytest.mark.integration
    @pytest.mark.parametrize("inp,want", [
        ("0:0",   0),
        ("1:0",   1),
        ("137:9", 137),
        ("",      -1),
        ("abc",   -1),
        ("0",     -1),
    ])
    def test_parses(self, inp, want):
        assert run_cluster_step._parse_exit_code(inp) == want


class TestTerminalStateSet:
    @pytest.mark.integration
    def test_includes_canonical_slurm_terminals(self):
        # If we add a new state name, fail loudly here so the change
        # gets reviewed (silently widening this set risks treating a
        # transient state as terminal and never polling further).
        assert run_cluster_step._TERMINAL_STATES == {
            "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
            "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL",
            "DEADLINE", "REVOKED",
        }


# ===========================================================================
# Basic refusals
# ===========================================================================

class TestBasicRefusals:
    @pytest.mark.integration
    def test_no_pipeline_id_refused(self):
        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="",
            freeze_request_key="x",
            project_name="p", compute_env_name="e",
            workflow_dir="/work/u/p/run", workflow_name="w",
            tool_name="t", command="t",
            inputs={}, outputs={},
            download_local_dir="/tmp/x",
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={})
        assert "error" in r and "pipeline_id" in r["error"]


# ===========================================================================
# Happy path — all primitives mocked, orchestration verified
# ===========================================================================

class _FakePipelineState:
    """A pipeline_state stub that records adds for assertion."""
    def __init__(self):
        self.steps: list = []
        self.validations: list = []
    def add_step(self, pipeline_id, step_data, replace_step=None):
        self.steps.append((pipeline_id, step_data))
        return len(self.steps) - 1
    def add_validation(self, pipeline_id, idx, basename, v):
        self.validations.append((pipeline_id, idx, basename, v))


class _FakeValidator:
    def __init__(self):
        self.calls: list = []
    def validate(self, path, etype):
        self.calls.append((path, etype))
        return {"valid": True, "type": etype, "path": path}


class _FakeEnvMgr:
    def hash_outputs(self, paths):
        return {p: "deadbeef" for p in paths}


class TestHappyPath:
    @pytest.mark.integration
    def test_orchestrates_and_records(self, monkeypatch, tmp_path):
        from agent.skills import (
            cluster_jobs as _cj, project_path as _pp,
            stage_apptainer as _sa, submit_workflow as _sw,
        )

        # Stage returns a sif path.
        def fake_stage(**kw):
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "mode": "adopt",
                    "sif_path": "/work/u/CLAUDE_CONTAINERS/samtools_abc.sif",
                    "image_digest": "sha256:abcdef",
                    "request_key": kw["freeze_request_key"],
                    "skipped": False, "staged_at": "t"}
        monkeypatch.setattr(_sa, "stage_apptainer_image", fake_stage)

        # Submit returns a job_id.
        def fake_submit(**kw):
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "job_id": "987654",
                    "workflow_dir": kw["workflow_dir"],
                    "files_uploaded": [
                        f"{kw['workflow_dir']}/main.nf",
                        f"{kw['workflow_dir']}/nextflow.config",
                        f"{kw['workflow_dir']}/launcher.sh"],
                    "submitted_at": "t", "upload_started": "t"}
        monkeypatch.setattr(_sw, "submit_workflow_job", fake_submit)

        # cluster_job_status returns terminal COMPLETED on first call.
        def fake_status(**kw):
            return {"compute_env": kw["compute_env_name"],
                    "job_id": kw["job_id"],
                    "jobs": [{
                        "job_id": kw["job_id"], "state": "COMPLETED",
                        "elapsed": "00:00:18", "exit_code": "0:0",
                        "nodelist": "c151402", "reason": "None",
                        "start": "t", "end": "t",
                    }],
                    "captured_at": "t"}
        monkeypatch.setattr(_cj, "cluster_job_status", fake_status)

        # cluster_job_resources returns clean I7 evidence.
        def fake_resources(**kw):
            return {"wall_seconds": 18.0, "peak_rss_mb": 12.3,
                    "max_cpu_percent": 87.5, "locus": "cluster",
                    "sacct_job_id": kw["job_id"],
                    "sacct_rows": [{"job_id": kw["job_id"]}]}
        monkeypatch.setattr(_cj, "cluster_job_resources", fake_resources)

        # download_from_project_path writes a fake output file each call.
        def fake_download(**kw):
            local = kw["local_path"]
            from pathlib import Path
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_bytes(b"fake bam content")
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_path": kw["abs_path"], "local_path": local,
                    "sha256": "deadbeef", "bytes": 16,
                    "duration_s": 0.1, "fetched_at": "t"}
        monkeypatch.setattr(_pp, "download_from_project_path", fake_download)

        state = _FakePipelineState()
        validator = _FakeValidator()
        env_mgr = _FakeEnvMgr()

        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1",
            freeze_request_key="samtools|linux/amd64|none",
            project_name="phase_b_samtools_demo",
            compute_env_name="hpc_cluster",
            workflow_dir="/work/u/p/run_001",
            workflow_name="samtools_view_run_001",
            tool_name="samtools",
            command="samtools view -b -h -F 4 ${input_bam} > ${output_bam}",
            inputs={"input_bam": "/work/u/p/inputs/test.bam"},
            outputs={"output_bam": "filtered.bam"},
            download_local_dir=str(tmp_path / "downloads"),
            apptainer_module="apptainer/1.4.1",
            nextflow_module="nextflow/25.04.7",
            slurm={"queue": "general", "time": "00:30:00",
                   "mem": "4G", "cpus": 2},
            poll_interval=0,  # zero sleep in tests
            _pipeline_state=state,
            _validator=validator,
            _env_mgr=env_mgr)

        # End-to-end orchestration
        assert "error" not in r, r
        assert r["success"] is True
        assert r["returncode"] == 0
        assert r["job_id"] == "987654"
        assert r["sif_path"].endswith("samtools_abc.sif")
        # I7 evidence
        assert r["resource_usage"]["wall_seconds"] == 18.0
        assert r["resource_usage"]["peak_rss_mb"] == 12.3
        assert r["resource_usage"]["locus"] == "cluster"
        assert r["resource_usage"]["sacct_job_id"] == "987654"
        # output captured + validated
        assert len(r["detected_outputs"]) == 1
        assert r["detected_outputs"][0].endswith("filtered.bam")
        assert "filtered.bam" in r["validations"]
        assert validator.calls and validator.calls[0][1] == "bam"

        # Draft updated with cluster-locus pipeline_step
        assert len(state.steps) == 1
        pid, step = state.steps[0]
        assert pid == "P1"
        assert step["validation_locus"] == "cluster"
        assert step["cluster_job_id"] == "987654"
        assert step["cluster_state"] == "COMPLETED"
        assert step["cluster_node"] == "c151402"
        assert step["ran_in_container"] is True
        assert step["container_image"].endswith("samtools_abc.sif")
        assert step["container_image_digest"] == "sha256:abcdef"
        # validation hooked
        assert len(state.validations) == 1
        assert state.validations[0][2] == "filtered.bam"


# ===========================================================================
# Phase failures — prior phases' results preserved on the error dict
# ===========================================================================

class TestPhaseFailurePreservation:
    @pytest.mark.integration
    def test_stage_failure_surfaces_clean(self, monkeypatch, tmp_path):
        from agent.skills import stage_apptainer as _sa
        monkeypatch.setattr(_sa, "stage_apptainer_image",
                            lambda **kw: {"error": "ssh not connected"})

        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="p", compute_env_name="e",
            workflow_dir="/work/u/p/run", workflow_name="w",
            tool_name="t", command="t",
            inputs={}, outputs={},
            download_local_dir=str(tmp_path),
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={}, poll_interval=0,
            _pipeline_state=_FakePipelineState(),
            _validator=_FakeValidator(),
            _env_mgr=_FakeEnvMgr())
        assert "error" in r and "stage_apptainer_image failed" in r["error"]
        assert "stage_result" in r

    @pytest.mark.integration
    def test_submit_failure_keeps_stage_result(self, monkeypatch, tmp_path):
        from agent.skills import (stage_apptainer as _sa,
                                   submit_workflow as _sw)
        monkeypatch.setattr(_sa, "stage_apptainer_image",
                            lambda **kw: {"success": True,
                                           "sif_path": "/x.sif",
                                           "image_digest": "sha256:x"})
        monkeypatch.setattr(_sw, "submit_workflow_job",
                            lambda **kw: {"error": "sbatch refused"})

        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="p", compute_env_name="e",
            workflow_dir="/work/u/p/run", workflow_name="w",
            tool_name="t", command="t ${o}",
            inputs={}, outputs={"o": "out.bam"},
            download_local_dir=str(tmp_path),
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={}, poll_interval=0,
            _pipeline_state=_FakePipelineState(),
            _validator=_FakeValidator(),
            _env_mgr=_FakeEnvMgr())
        assert "error" in r and "submit_workflow_job failed" in r["error"]
        assert "stage_result" in r
        assert "submit_result" in r

    @pytest.mark.integration
    def test_poll_timeout_surfaces_clean(self, monkeypatch, tmp_path):
        from agent.skills import (stage_apptainer as _sa,
                                   submit_workflow as _sw,
                                   cluster_jobs as _cj)
        monkeypatch.setattr(_sa, "stage_apptainer_image",
                            lambda **kw: {"success": True,
                                           "sif_path": "/x.sif",
                                           "image_digest": "sha256:x"})
        monkeypatch.setattr(_sw, "submit_workflow_job",
                            lambda **kw: {"success": True,
                                           "job_id": "1",
                                           "workflow_dir": kw["workflow_dir"],
                                           "files_uploaded": []})
        # poll always returns PENDING (never terminal)
        monkeypatch.setattr(_cj, "cluster_job_status",
                            lambda **kw: {"jobs": [{
                                "state": "PENDING", "elapsed": "00:00:00",
                                "exit_code": "0:0", "job_id": "1"}]})

        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="p", compute_env_name="e",
            workflow_dir="/work/u/p/run", workflow_name="w",
            tool_name="t", command="t ${o}",
            inputs={}, outputs={"o": "out.bam"},
            download_local_dir=str(tmp_path),
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={}, poll_interval=0, max_polls=3,
            _pipeline_state=_FakePipelineState(),
            _validator=_FakeValidator(),
            _env_mgr=_FakeEnvMgr())
        assert "error" in r and "polling timed out" in r["error"]
