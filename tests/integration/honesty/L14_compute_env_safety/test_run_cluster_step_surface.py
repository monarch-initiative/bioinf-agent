"""
L14 cheat-guards — run_step_on_cluster orchestration surface.

run_step_on_cluster is the Path-4 keystone. It runs a workflow step IN
THE AGENT'S SCRATCH SANDBOX, polls to completion, fetches outputs,
validates them, and records a pipeline_step with `validation_locus:
"cluster"` for seal_workflow's consumption.

The wall this pins: SCRATCH ONLY.
  - workflow_dir is computed internally as
    <env.agent_scratch_target.path>/<project_name>/<workflow_name>/
  - There is no caller knob for the workflow_dir.
  - If the env has no scratch target, the primitive hard-fails.
  - If the scratch target doesn't have `exec`, it hard-fails. (The
    schema validator enforces this in production; we test the
    primitive-level guard directly.)
  - Production runs (against user-declared project directories[])
    go through submit_workflow_job, not this primitive.

Also pinned:
  - sacct Elapsed parser (HH:MM:SS and D-HH:MM:SS)
  - MaxRSS parser (K/M/G suffix → MB)
  - terminal-state set matches SLURM's reality
  - exit_code parser ("0:0" → 0; bad input → -1)
  - happy-path: stage + render + upload×3 + sbatch + poll + resources
    + download×N + validation primitives all invoked, pipeline_step
    record built with cluster-locus fields, draft updated
  - phase failures preserve prior phases' results on the error dict
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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
# Fixtures
# ===========================================================================

def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _good_access(tmp_path: Path) -> Path:
    """Env with a scratch target that has [upload, download, exec] +
    a project that has access to the env. NO directories[] entries —
    cluster validation needs none."""
    return _write_access(tmp_path, {
        "compute_envs": [{
            "name": "fakehpc", "type": "ssh",
            "host": "fake.example.edu", "user": "u",
            "container_upload_target": None,
            "agent_scratch_target": {
                "path": "/work/u/CLAUDE_SCRATCH",
                "permissions": ["file_name_only", "upload",
                                "download", "exec"],
            },
        }],
        "projects": [{
            "name": "phase_b_samtools_demo",
            "compute_env_access": [{
                "compute_env": "fakehpc",
                "directories": [],
            }],
        }],
    })


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
            workflow_name="w",
            tool_name="t", command="t",
            inputs={}, outputs={},
            download_local_dir="/tmp/x",
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={})
        assert "error" in r and "pipeline_id" in r["error"]

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "", "name with space", "name;rm", "name/sub", "name$x",
        "a" * 65,  # over 64-char cap
    ])
    def test_bad_workflow_name_refused(self, bad):
        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="p", compute_env_name="e",
            workflow_name=bad,
            tool_name="t", command="t",
            inputs={}, outputs={},
            download_local_dir="/tmp/x",
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={})
        assert "error" in r and "workflow_name" in r["error"]


# ===========================================================================
# Scratch-only wall — these tests pin that this primitive ONLY works
# against an env that has agent_scratch_target with exec, and that
# it ALWAYS lands at <scratch>/<project>/<workflow_name>/.
# ===========================================================================

class TestScratchOnlyWall:
    @pytest.mark.integration
    def test_no_scratch_target_refuses(self, tmp_path):
        # Env without an agent_scratch_target → hard refusal.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "x", "user": "u",
                "container_upload_target": None,
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })
        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="demo", compute_env_name="fakehpc",
            workflow_name="w1",
            tool_name="t", command="t ${o}",
            inputs={}, outputs={"o": "out.bam"},
            download_local_dir=str(tmp_path / "downloads"),
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={"queue": "g", "time": "00:01:00", "mem": "1G", "cpus": 1},
            poll_interval=0,
            access_path=str(access_path))
        assert "error" in r
        assert "agent_scratch_target" in r["error"]

    @pytest.mark.integration
    def test_scratch_missing_exec_refuses_at_auth_gate(self):
        # Defense-in-depth: even though the schema validator enforces
        # that agent_scratch_target.permissions ⊇ {upload, download,
        # exec} at config-load time, the auth gate inside
        # run_step_on_cluster ALSO checks. A partial-perm scratch
        # bypassed via future schema relaxation must still be refused.
        from agent.skills import compute_access
        scratch_partial = {
            "path": "/work/u/CLAUDE_SCRATCH",
            "permissions": ["file_name_only", "upload"],  # no exec
        }
        env = {"name": "fakehpc", "type": "ssh",
               "agent_scratch_target": scratch_partial}
        project = {"name": "demo",
                   "compute_env_access": [{"compute_env": "fakehpc"}]}

        with pytest.raises(compute_access.PermissionDenied) as exc:
            compute_access.check_env_target_capability(
                project, "fakehpc", scratch_partial,
                "run_step_on_cluster", "agent_scratch_target")
        assert "exec" in str(exc.value)

    @pytest.mark.integration
    def test_local_env_refused(self, tmp_path):
        # type: local can't be the target of a cluster run.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{"name": "laptop", "type": "local",
                              "container_upload_target": None}],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "laptop", "directories": []}],
            }],
        })
        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="demo", compute_env_name="laptop",
            workflow_name="w1",
            tool_name="t", command="t ${o}",
            inputs={}, outputs={"o": "out.bam"},
            download_local_dir=str(tmp_path / "downloads"),
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={"queue": "g", "time": "00:01:00", "mem": "1G", "cpus": 1},
            poll_interval=0,
            access_path=str(access_path))
        assert "error" in r and "only supports ssh" in r["error"]


# ===========================================================================
# Happy path — primitives mocked, orchestration verified
# ===========================================================================

class TestHappyPath:
    @pytest.mark.integration
    def test_orchestrates_and_records(self, monkeypatch, tmp_path):
        access_path = _good_access(tmp_path)

        from agent.skills import (
            cluster_jobs as _cj, scratch as _sc,
            stage_apptainer as _sa, submit_workflow as _sw,
        )

        # 1. stage returns a sif path.
        def fake_stage(**kw):
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "mode": "adopt",
                    "sif_path": "/work/u/CLAUDE_CONTAINERS/samtools_abc.sif",
                    "image_digest": "sha256:abcdef",
                    "request_key": kw["freeze_request_key"],
                    "skipped": False, "staged_at": "t"}
        monkeypatch.setattr(_sa, "stage_apptainer_image", fake_stage)

        # 2. upload_to_scratch — succeeds 3 times (main.nf / config /
        # launcher.sh). Asserts the auto-prefixed path looks right.
        uploads: list = []
        def fake_upload_to_scratch(**kw):
            uploads.append(kw)
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_path":
                        f"/work/u/CLAUDE_SCRATCH/{kw['project_name']}/"
                        f"{kw['remote_subpath']}",
                    "sha256": "0"*64, "bytes": 1, "duration_s": 0.001,
                    "transferred_at": "t"}
        monkeypatch.setattr(_sc, "upload_to_scratch", fake_upload_to_scratch)

        # 3. sbatch_via_ssh returns a job_id.
        def fake_sbatch(env, workflow_dir, *, timeout=300):
            return {"job_id": "987654", "launcher": f"{workflow_dir}/launcher.sh",
                    "sbatch_command": f"cd {workflow_dir} && sbatch"}
        monkeypatch.setattr(_sw, "sbatch_via_ssh", fake_sbatch)

        # 4. cluster_job_status returns terminal COMPLETED.
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

        # 5. cluster_job_resources returns clean I7 evidence.
        def fake_resources(**kw):
            return {"wall_seconds": 18.0, "peak_rss_mb": 12.3,
                    "max_cpu_percent": 87.5, "locus": "cluster",
                    "sacct_job_id": kw["job_id"],
                    "sacct_rows": [{"job_id": kw["job_id"]}]}
        monkeypatch.setattr(_cj, "cluster_job_resources", fake_resources)

        # 6. download_from_scratch writes a fake output file each call.
        def fake_download(**kw):
            local = kw["local_path"]
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_bytes(b"fake bam content")
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_path":
                        f"/work/u/CLAUDE_SCRATCH/{kw['project_name']}/"
                        f"{kw['remote_subpath']}",
                    "local_path": local,
                    "sha256": "deadbeef", "bytes": 16,
                    "duration_s": 0.1, "fetched_at": "t"}
        monkeypatch.setattr(_sc, "download_from_scratch", fake_download)

        state = _FakePipelineState()
        validator = _FakeValidator()
        env_mgr = _FakeEnvMgr()

        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1",
            freeze_request_key="samtools|linux/amd64|none",
            project_name="phase_b_samtools_demo",
            compute_env_name="fakehpc",
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
            poll_interval=0,
            access_path=str(access_path),
            _pipeline_state=state,
            _validator=validator,
            _env_mgr=env_mgr)

        # End-to-end orchestration
        assert "error" not in r, r
        assert r["success"] is True
        assert r["returncode"] == 0
        assert r["job_id"] == "987654"
        assert r["sif_path"].endswith("samtools_abc.sif")

        # The wall: workflow_dir landed in scratch under the project + name
        expected_dir = ("/work/u/CLAUDE_SCRATCH/phase_b_samtools_demo/"
                        "samtools_view_run_001")
        assert r["workflow_dir"] == expected_dir

        # I7 evidence
        assert r["resource_usage"]["wall_seconds"] == 18.0
        assert r["resource_usage"]["peak_rss_mb"] == 12.3
        assert r["resource_usage"]["locus"] == "cluster"

        # All 3 rendered files uploaded via upload_to_scratch (NOT
        # upload_to_project_path); each via remote_subpath that's
        # workflow_name-prefixed (so auto-prefix lands them in the
        # scratch project zone under the workflow_name dir).
        assert len(uploads) == 3
        sub_names = {u["remote_subpath"] for u in uploads}
        assert sub_names == {
            "samtools_view_run_001/main.nf",
            "samtools_view_run_001/nextflow.config",
            "samtools_view_run_001/launcher.sh"}
        for u in uploads:
            assert u["project_name"] == "phase_b_samtools_demo"

        # Output captured + validated
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
        assert step["cluster_workflow_dir"] == expected_dir
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
        access_path = _good_access(tmp_path)
        from agent.skills import stage_apptainer as _sa
        monkeypatch.setattr(_sa, "stage_apptainer_image",
                            lambda **kw: {"error": "ssh not connected"})

        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="phase_b_samtools_demo",
            compute_env_name="fakehpc",
            workflow_name="w1",
            tool_name="t", command="t ${o}",
            inputs={}, outputs={"o": "out.bam"},
            download_local_dir=str(tmp_path / "downloads"),
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={"queue": "g", "time": "00:01:00", "mem": "1G", "cpus": 1},
            poll_interval=0,
            access_path=str(access_path),
            _pipeline_state=_FakePipelineState(),
            _validator=_FakeValidator(),
            _env_mgr=_FakeEnvMgr())
        assert "error" in r and "stage_apptainer_image failed" in r["error"]
        assert "stage_result" in r

    @pytest.mark.integration
    def test_sbatch_failure_keeps_stage_result(self, monkeypatch, tmp_path):
        access_path = _good_access(tmp_path)
        from agent.skills import (scratch as _sc,
                                   stage_apptainer as _sa,
                                   submit_workflow as _sw)
        monkeypatch.setattr(_sa, "stage_apptainer_image",
                            lambda **kw: {"success": True,
                                           "sif_path": "/x.sif",
                                           "image_digest": "sha256:x"})
        monkeypatch.setattr(_sc, "upload_to_scratch",
                            lambda **kw: {"success": True,
                                           "remote_path": "/x",
                                           "sha256": "0"*64,
                                           "bytes": 1, "duration_s": 0.001,
                                           "transferred_at": "t",
                                           "compute_env":
                                               kw["compute_env_name"]})
        monkeypatch.setattr(_sw, "sbatch_via_ssh",
                            lambda env, dir, timeout=300:
                                {"error": "sbatch refused"})

        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="phase_b_samtools_demo",
            compute_env_name="fakehpc",
            workflow_name="w1",
            tool_name="t", command="t ${o}",
            inputs={}, outputs={"o": "out.bam"},
            download_local_dir=str(tmp_path / "downloads"),
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={"queue": "g", "time": "00:01:00", "mem": "1G", "cpus": 1},
            poll_interval=0,
            access_path=str(access_path),
            _pipeline_state=_FakePipelineState(),
            _validator=_FakeValidator(),
            _env_mgr=_FakeEnvMgr())
        assert "error" in r and "sbatch failed" in r["error"]
        assert "stage_result" in r
        assert "sbatch_result" in r

    @pytest.mark.integration
    def test_poll_timeout_surfaces_clean(self, monkeypatch, tmp_path):
        access_path = _good_access(tmp_path)
        from agent.skills import (cluster_jobs as _cj,
                                   scratch as _sc,
                                   stage_apptainer as _sa,
                                   submit_workflow as _sw)
        monkeypatch.setattr(_sa, "stage_apptainer_image",
                            lambda **kw: {"success": True,
                                           "sif_path": "/x.sif",
                                           "image_digest": "sha256:x"})
        monkeypatch.setattr(_sc, "upload_to_scratch",
                            lambda **kw: {"success": True,
                                           "remote_path": "/x",
                                           "sha256": "0"*64,
                                           "bytes": 1, "duration_s": 0.001,
                                           "transferred_at": "t",
                                           "compute_env":
                                               kw["compute_env_name"]})
        monkeypatch.setattr(_sw, "sbatch_via_ssh",
                            lambda env, dir, timeout=300:
                                {"job_id": "1",
                                 "launcher": f"{dir}/launcher.sh"})
        # poll always returns PENDING (never terminal)
        monkeypatch.setattr(_cj, "cluster_job_status",
                            lambda **kw: {"jobs": [{
                                "state": "PENDING", "elapsed": "00:00:00",
                                "exit_code": "0:0", "job_id": "1"}]})

        r = run_cluster_step.run_step_on_cluster(
            pipeline_id="P1", freeze_request_key="x",
            project_name="phase_b_samtools_demo",
            compute_env_name="fakehpc",
            workflow_name="w1",
            tool_name="t", command="t ${o}",
            inputs={}, outputs={"o": "out.bam"},
            download_local_dir=str(tmp_path / "downloads"),
            apptainer_module="apptainer/1", nextflow_module="nextflow/2",
            slurm={"queue": "g", "time": "00:01:00", "mem": "1G", "cpus": 1},
            poll_interval=0, max_polls=3,
            access_path=str(access_path),
            _pipeline_state=_FakePipelineState(),
            _validator=_FakeValidator(),
            _env_mgr=_FakeEnvMgr())
        assert "error" in r and "polling timed out" in r["error"]
        assert r["job_id"] == "1"
        assert r["workflow_dir"].endswith("/phase_b_samtools_demo/w1")
