"""
L14 cheat-guards — submit_workflow_job's refuse-to-emit + parsing surface.

submit_workflow_job orchestrates render → upload → sbatch. These tests
pin the contract:

  - workflow_dir validation (abs path, no `..`, no shell metachars)
    fires BEFORE any I/O
  - auth gate (project on env + dir has both `upload` and `exec`)
    fires before any ssh
  - sbatch --parsable parser correctly extracts job_ids from clean
    and federated stdouts; refuses anything else with `sbatch_stdout`
    surfaced for debugging
  - render failures (e.g. undeclared placeholder) surface as ValueError
    without uploading anything
  - upload failure mid-stream returns the partial files_uploaded
    list so the caller knows what landed
  - sbatch nonzero rc returns `error` with files_uploaded + launcher
    path
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import submit_workflow


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _good_access(tmp_path: Path) -> Path:
    """A full project + env + directory grant that satisfies both
    `upload` and `exec` on the workflow_dir."""
    return _write_access(tmp_path, {
        "compute_envs": [{
            "name": "fakehpc", "type": "ssh",
            "host": "fake.example.edu", "user": "u",
            "container_upload_target": None,
        }],
        "projects": [{
            "name": "demo",
            "compute_env_access": [{
                "compute_env": "fakehpc",
                "directories": [{
                    "path": "/work/u/demo/run_001",
                    "permissions": ["upload", "exec", "download"],
                }],
            }],
        }],
    })


_DEMO_SLURM = {"time": "00:30:00",
               "mem": "4G", "cpus": 2}
_DEMO_KW = dict(
    project_name="demo",
    compute_env_name="fakehpc",
    workflow_dir="/work/u/demo/run_001",
    workflow_name="demo_run",
    tool_name="samtools",
    command="samtools view -b ${input_bam} > ${output_bam}",
    inputs={"input_bam": "/work/u/demo/inputs/test.bam"},
    outputs={"output_bam": "filtered.bam"},
    apptainer_sif="/work/u/CLAUDE_GENOMES/samtools.sif",
    apptainer_module="apptainer/1.4.1",
    nextflow_module="nextflow/25.04.7",
    slurm=_DEMO_SLURM,
)


# ===========================================================================
# 1. workflow_dir validation
# ===========================================================================

class TestWorkflowDirValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("good", [
        "/work/u/demo/run_001",
        "/work/u/demo/run_001/",
        "/x",
    ])
    def test_accepts_good_dirs(self, good):
        assert submit_workflow._validate_workflow_dir(good).startswith("/")

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "relative/path",
        "/work/../etc/shadow",
        "/work/u/demo$(id)",
        "/work/u/demo;rm",
        "/work/u/demo & evil",
        "",
        "/work with spaces/x",
        "/path/with\nnewline",
        "/path|pipe",
    ])
    def test_refuses_bad_dirs(self, bad):
        with pytest.raises(ValueError):
            submit_workflow._validate_workflow_dir(bad)


# ===========================================================================
# 2. sbatch --parsable parser
# ===========================================================================

class TestSbatchParsableParser:
    @pytest.mark.integration
    @pytest.mark.parametrize("stdout,want", [
        ("12345\n",          "12345"),
        ("12345;rc1",        "12345"),
        ("123456789012\n",   "123456789012"),
        ("  98765  \n",      "98765"),
    ])
    def test_extracts_job_id(self, stdout, want):
        assert submit_workflow._parse_sbatch_parsable(stdout) == want

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "",
        "Submitted batch job 12345\n",
        "abc",
        "0".rjust(13, "9"),   # 13-digit (over the cap)
        "12345garbage",
    ])
    def test_refuses_unparseable(self, bad):
        assert submit_workflow._parse_sbatch_parsable(bad) is None


# ===========================================================================
# 3. Auth gate — both `upload` AND `exec` required on the workflow_dir
# ===========================================================================

class TestAuthGate:
    @pytest.mark.integration
    def test_dir_missing_exec_refused_no_ssh(self, tmp_path, monkeypatch):
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": None,
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc",
                    "directories": [{
                        "path": "/work/u/demo/run_001",
                        "permissions": ["upload"],
                    }],
                }],
            }],
        })
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})
        assert "error" in result and "PermissionDenied" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_dir_missing_upload_refused_no_ssh(self, tmp_path, monkeypatch):
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "fake.example.edu", "user": "u",
                "container_upload_target": None,
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc",
                    "directories": [{
                        "path": "/work/u/demo/run_001",
                        "permissions": ["exec"],
                    }],
                }],
            }],
        })
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})
        assert "error" in result and "PermissionDenied" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_unknown_project_refused_no_ssh(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "project_name": "outsider",
               "access_path": str(access_path)})
        assert "error" in result and "KeyError" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_local_env_refused_no_ssh(self, tmp_path, monkeypatch):
        access_path = _write_access(tmp_path, {
            "compute_envs": [{"name": "laptop", "type": "local",
                              "container_upload_target": None}],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "laptop",
                    "directories": [{
                        "path": "/work/u/demo/run_001",
                        "permissions": ["upload", "exec"],
                    }],
                }],
            }],
        })
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "compute_env_name": "laptop",
               "access_path": str(access_path)})
        assert "error" in result and "only supports ssh" in result["error"]
        assert called == []


# ===========================================================================
# 4. Render-time failure surfaces before any ssh
# ===========================================================================

class TestRenderRefusal:
    @pytest.mark.integration
    def test_undeclared_placeholder_refused_no_ssh(
            self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW,
               "command": "samtools view ${input_bam} > ${undeclared}",
               "access_path": str(access_path)})
        assert "error" in result and "ValueError" in result["error"]
        assert "undeclared" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_slurm_typo_refused_no_ssh(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW,
               "slurm": {**_DEMO_SLURM, "mem_gb": 4},  # typo
               "access_path": str(access_path)})
        assert "error" in result and "unknown keys" in result["error"]
        assert called == []


# ===========================================================================
# 5. Happy path with mocked ssh — every subprocess invocation patched
# ===========================================================================

class TestHappyPath:
    @pytest.mark.integration
    def test_returns_job_id_with_files_uploaded(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)

        # Track every subprocess.run invocation so we can assert
        # ordering: 3 uploads (each: test-e, mkdir, scp, sha256), then
        # 1 sbatch. The test doesn't pin the exact internal sequence
        # of upload_to_project_path — it just confirms that we eventually
        # see an sbatch invocation and get a job_id back.
        sbatch_calls = []

        def fake_run(argv, *args, **kwargs):
            mock = MagicMock()
            cmd = " ".join(argv) if isinstance(argv, list) else str(argv)
            # The sbatch step lands the literal job_id on stdout.
            if "sbatch --parsable" in cmd:
                sbatch_calls.append(cmd)
                mock.returncode = 0
                mock.stdout = "987654\n"
                mock.stderr = ""
                return mock
            # The remote-existence pre-check — pretend nothing exists.
            if "test -e" in cmd:
                mock.returncode = 0
                mock.stdout = "OK\n"
                mock.stderr = ""
                return mock
            # mkdir -p, scp, and remote sha256 — all succeed.
            if "mkdir -p" in cmd:
                mock.returncode = 0
                mock.stdout = ""
                mock.stderr = ""
                return mock
            if "sha256sum" in cmd:
                # Compute the actual sha for the file scp'd to the
                # "remote" — but since we don't actually move bytes,
                # echo the local sha back. upload_to_project_path
                # computes the local sha, scp's, then checks remote
                # sha matches.
                mock.returncode = 0
                # The local path is somewhere in the bash invocation.
                mock.stdout = (kwargs.get("_local_sha") or
                               "0" * 64) + "  remote\n"
                mock.stderr = ""
                return mock
            # Default — scp invocation. Pretend success.
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            return mock

        # Bypass the sha256 round-trip enforcement by monkeypatching
        # upload_to_project_path directly to a thin stub — keeps this
        # test focused on submit_workflow_job, not on scp wire details
        # (those are already covered by L14's project_path tests).
        from agent.skills import transfer as _tr

        def fake_upload(**kw):
            return {
                "success": True,
                "compute_env": kw["compute_env_name"],
                "remote_abs_path": kw["remote_abs_path"],
                "zone": "project_path",
                "provider": "scp_head_node",
                "local_sha256": "0" * 64,
                "bytes": 1,
                "duration_s": 0.001,
                "verified_method": "sha256_round_trip",
                "manifest": "/tmp/fake_manifest.json",
            }
        monkeypatch.setattr(_tr, "upload", fake_upload)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})

        assert "error" not in result, result
        assert result["success"] is True
        assert result["job_id"] == "987654"
        assert result["workflow_dir"] == "/work/u/demo/run_001"
        # Three files were uploaded (main.nf, nextflow.config, launcher.sh).
        assert len(result["files_uploaded"]) == 3
        assert any("launcher.sh" in p for p in result["files_uploaded"])
        # And exactly one sbatch was invoked.
        assert len(sbatch_calls) == 1
        # The sbatch line cd's into the workflow_dir first. shlex.quote
        # leaves a quote-free string when no metachars are present.
        assert "cd /work/u/demo/run_001" in sbatch_calls[0]
        assert "sbatch --parsable launcher.sh" in sbatch_calls[0]


# ===========================================================================
# 6. sbatch failure paths
# ===========================================================================

class TestSbatchFailures:
    @pytest.mark.integration
    def test_sbatch_nonzero_returns_error_with_files_uploaded(
            self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        from agent.skills import transfer as _tr

        def fake_upload(**kw):
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_abs_path": kw["remote_abs_path"],
                    "zone": "project_path",
                    "provider": "scp_head_node",
                    "local_sha256": "0"*64,
                    "bytes": 1, "duration_s": 0.001,
                    "verified_method": "sha256_round_trip",
                    "manifest": "/tmp/fake_manifest.json"}

        def fake_run(*a, **kw):
            mock = MagicMock()
            mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "sbatch: error: invalid partition\n"
            return mock

        monkeypatch.setattr(_tr, "upload", fake_upload)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})
        assert "error" in result
        assert "sbatch failed" in result["error"]
        assert "invalid partition" in result["error"]
        assert result["files_uploaded"] and len(result["files_uploaded"]) == 3

    @pytest.mark.integration
    def test_sbatch_succeeds_but_stdout_unparseable(
            self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        from agent.skills import transfer as _tr

        def fake_upload(**kw):
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_abs_path": kw["remote_abs_path"],
                    "zone": "project_path",
                    "provider": "scp_head_node",
                    "local_sha256": "0"*64,
                    "bytes": 1, "duration_s": 0.001,
                    "verified_method": "sha256_round_trip",
                    "manifest": "/tmp/fake_manifest.json"}

        def fake_run(*a, **kw):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "Submitted batch job 12345\n"  # not --parsable
            mock.stderr = ""
            return mock

        monkeypatch.setattr(_tr, "upload", fake_upload)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})
        assert "error" in result
        assert "parseable" in result["error"]
        assert result["sbatch_stdout"] == "Submitted batch job 12345"
        assert result["files_uploaded"] and len(result["files_uploaded"]) == 3

    @pytest.mark.integration
    def test_upload_failure_returns_partial_progress(
            self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        from agent.skills import transfer as _tr

        # Succeed on the first upload, fail on the second.
        calls = {"n": 0}

        def fake_upload(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"success": True, "compute_env": kw["compute_env_name"],
                        "remote_abs_path": kw["remote_abs_path"],
                    "zone": "project_path",
                    "provider": "scp_head_node",
                    "verified_method": "sha256_round_trip",
                    "manifest": "/tmp/fake_manifest.json",
                    "local_sha256": "0"*64,
                        "bytes": 1, "duration_s": 0.001,
                        "transferred_at": "2026-06-05T00:00:00+00:00"}
            return {"error": "remote path already exists: ..."}

        sbatch_called = []
        def fake_run(*a, **kw):
            sbatch_called.append(a)
            mock = MagicMock(); mock.returncode = 0; mock.stdout = "1\n"
            mock.stderr = ""
            return mock

        monkeypatch.setattr(_tr, "upload", fake_upload)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})
        assert "error" in result
        assert "upload of" in result["error"]
        # Only the first file landed.
        assert len(result["files_uploaded"]) == 1
        # And sbatch was never called.
        assert sbatch_called == []


# ===========================================================================
# 7. workflow_dir is REQUIRED — this is the production primitive
# ===========================================================================

class TestWorkflowDirRequired:
    """submit_workflow_job is the production submission primitive and
    runs against user-declared project workspace paths. It does NOT
    auto-derive a scratch path on missing workflow_dir — that's
    run_step_on_cluster's job. An empty workflow_dir is a clean refusal
    (caller passed the wrong primitive)."""

    @pytest.mark.integration
    def test_empty_workflow_dir_refused_no_ssh(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "workflow_dir": "",
               "access_path": str(access_path)})
        assert "error" in result
        assert "workflow_dir is required" in result["error"]
        assert "run_step_on_cluster" in result["error"]
        assert called == []


# ===========================================================================
# 8. Submission manifest — the user-visible deliverable for production runs
# ===========================================================================

class TestSubmissionManifest:
    """The agent will be cut off long before a real production job
    finishes, so submit_workflow_job writes a local manifest the user
    can use to find the job later. Path:
        job_submissions/<project>/<workflow_name>_<job_id>.submission.json
    """

    @pytest.mark.integration
    def test_manifest_written_to_canonical_path(self, tmp_path, monkeypatch):
        access_path = _good_access(tmp_path)

        # Stub upload + ssh subprocess.
        from agent.skills import transfer as _tr

        def fake_upload(**kw):
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_abs_path": kw["remote_abs_path"],
                    "zone": "project_path",
                    "provider": "scp_head_node",
                    "verified_method": "sha256_round_trip",
                    "manifest": "/tmp/fake_manifest.json",
                    "local_sha256": "0"*64,
                    "bytes": 1, "duration_s": 0.001,
                    "transferred_at": "t"}

        def fake_run(*a, **kw):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "555000\n"
            mock.stderr = ""
            return mock

        monkeypatch.setattr(_tr, "upload", fake_upload)
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.chdir(tmp_path)  # so the manifest lands in tmp_path

        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})
        assert "error" not in result, result

        manifest_path = result["manifest_path"]
        expected = ("job_submissions/demo/"
                    "demo_run_555000.submission.json")
        assert manifest_path == expected
        assert (tmp_path / manifest_path).exists()

    @pytest.mark.integration
    def test_manifest_records_everything_user_needs(
            self, tmp_path, monkeypatch):
        import json
        access_path = _good_access(tmp_path)
        from agent.skills import transfer as _tr

        def fake_upload(**kw):
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_abs_path": kw["remote_abs_path"],
                    "zone": "project_path",
                    "provider": "scp_head_node",
                    "verified_method": "sha256_round_trip",
                    "manifest": "/tmp/fake_manifest.json",
                    "local_sha256": "0"*64,
                    "bytes": 1, "duration_s": 0.001,
                    "transferred_at": "t"}

        def fake_run(*a, **kw):
            mock = MagicMock(); mock.returncode = 0
            mock.stdout = "1234567\n"; mock.stderr = ""
            return mock

        monkeypatch.setattr(_tr, "upload", fake_upload)
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.chdir(tmp_path)

        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})
        assert "error" not in result, result

        manifest = json.loads((tmp_path / result["manifest_path"]).read_text())
        # The manifest carries everything needed to re-find this job
        # WITHOUT the agent: who, what, where, how to follow up.
        assert manifest["project_name"] == "demo"
        assert manifest["compute_env"] == "fakehpc"
        assert manifest["job_id"] == "1234567"
        assert manifest["workflow_name"] == "demo_run"
        assert manifest["tool_name"] == "samtools"
        assert manifest["workflow_dir"] == "/work/u/demo/run_001"
        assert manifest["command"] == _DEMO_KW["command"]
        assert manifest["inputs"] == _DEMO_KW["inputs"]
        assert manifest["outputs"] == _DEMO_KW["outputs"]
        assert manifest["apptainer_sif"] == _DEMO_KW["apptainer_sif"]
        assert manifest["slurm"] == _DEMO_SLURM
        assert len(manifest["files_uploaded"]) == 3
        # Follow-up hints reference the job_id so a future agent
        # invocation can pick up the trail.
        assert "1234567" in manifest["follow_up"]["poll"]

    @pytest.mark.integration
    def test_manifest_not_written_on_failure(self, tmp_path, monkeypatch):
        # If sbatch fails, NO manifest is written — the "submission"
        # didn't happen. Avoids polluting job_submissions/ with
        # phantom entries.
        access_path = _good_access(tmp_path)
        from agent.skills import transfer as _tr

        def fake_upload(**kw):
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_abs_path": kw["remote_abs_path"],
                    "zone": "project_path",
                    "provider": "scp_head_node",
                    "verified_method": "sha256_round_trip",
                    "manifest": "/tmp/fake_manifest.json",
                    "local_sha256": "0"*64,
                    "bytes": 1, "duration_s": 0.001, "transferred_at": "t"}

        def fake_run(*a, **kw):
            mock = MagicMock(); mock.returncode = 1; mock.stdout = ""
            mock.stderr = "sbatch: error: bad queue\n"
            return mock

        monkeypatch.setattr(_tr, "upload", fake_upload)
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.chdir(tmp_path)

        result = submit_workflow.submit_workflow_job(
            **{**_DEMO_KW, "access_path": str(access_path)})
        assert "error" in result
        # No manifest on failure.
        assert not (tmp_path / "job_submissions").exists()
