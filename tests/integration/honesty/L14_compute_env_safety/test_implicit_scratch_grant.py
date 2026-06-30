"""
L14 — env-implicit scratch grant in check_permission.

The auth model: any path under `<env.agent_scratch_target>/<project>/`
is automatically authorized for that project with the env's scratch
target permissions — no per-project `directories[]` declaration
required. Symmetric with how `upload_to_scratch` already works.

These tests pin:
  - the implicit grant fires for paths under the project's scratch zone
  - the grant respects required permission (upload vs download vs exec)
  - it isolates projects: project A's scratch zone is NOT authorized
    for project B
  - falling outside the scratch zone falls through to Phase-1
    directories[] (no surprise auth)
  - submit_workflow_job + project_path primitives accept scratch paths
    via this implicit grant (the user-facing wiring)
  - submit_workflow_job auto-derives workflow_dir = scratch/<project>/
    <workflow_name> when workflow_dir is empty
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import compute_access, project_path, submit_workflow


def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _ssh_env_with_scratch(tmp_path):
    """An env with agent_scratch_target declared + a project that has
    access to it but NO directories[] entries for scratch paths."""
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
            "name": "demo",
            "compute_env_access": [{
                "compute_env": "fakehpc",
                "directories": [],   # intentionally empty
            }],
        }],
    })


# ===========================================================================
# Core auth-gate behavior
# ===========================================================================

class TestImplicitScratchGrant:
    @pytest.mark.integration
    def test_scratch_path_authorized_under_project_zone(self, tmp_path):
        access_path = _ssh_env_with_scratch(tmp_path)
        access = compute_access.load_access(access_path)
        project = compute_access.get_project("demo", access)
        env = compute_access.get_compute_env("fakehpc", access)

        # Path under /work/u/CLAUDE_SCRATCH/demo/... is authorized via
        # the env-implicit grant, even though directories[] is empty.
        entry = compute_access.check_permission(
            project, "fakehpc",
            "/work/u/CLAUDE_SCRATCH/demo/run_001/launcher.sh",
            "upload_to_project_path",
            env=env)
        assert entry.get("_synthetic") is True
        assert entry.get("_source") == "agent_scratch_target"

    @pytest.mark.integration
    def test_grant_includes_submit_workflow_job_exec_perm(self, tmp_path):
        # submit_workflow_job requires `exec`; verify the grant passes.
        access_path = _ssh_env_with_scratch(tmp_path)
        access = compute_access.load_access(access_path)
        project = compute_access.get_project("demo", access)
        env = compute_access.get_compute_env("fakehpc", access)

        compute_access.check_permission(
            project, "fakehpc",
            "/work/u/CLAUDE_SCRATCH/demo/anywhere",
            "submit_workflow_job",
            env=env)
        compute_access.check_permission(
            project, "fakehpc",
            "/work/u/CLAUDE_SCRATCH/demo/anywhere",
            "download_from_project_path",
            env=env)

    @pytest.mark.integration
    def test_other_project_scratch_zone_NOT_authorized(self, tmp_path):
        # /work/u/CLAUDE_SCRATCH/OTHER_PROJECT/... must NOT be
        # authorized for project=demo. Isolation between projects.
        access_path = _ssh_env_with_scratch(tmp_path)
        access = compute_access.load_access(access_path)
        project = compute_access.get_project("demo", access)
        env = compute_access.get_compute_env("fakehpc", access)

        with pytest.raises(compute_access.PermissionDenied):
            compute_access.check_permission(
                project, "fakehpc",
                "/work/u/CLAUDE_SCRATCH/other_proj/file",
                "upload_to_project_path",
                env=env)

    @pytest.mark.integration
    def test_path_outside_scratch_falls_through_to_phase1(self, tmp_path):
        # A path NOT under the scratch zone must fall through to the
        # Phase-1 directories[] check — and be denied if no entry
        # covers it (empty directories[] here).
        access_path = _ssh_env_with_scratch(tmp_path)
        access = compute_access.load_access(access_path)
        project = compute_access.get_project("demo", access)
        env = compute_access.get_compute_env("fakehpc", access)

        with pytest.raises(compute_access.PermissionDenied):
            compute_access.check_permission(
                project, "fakehpc",
                "/work/u/PROJECT_WORKSPACE/file",
                "upload_to_project_path",
                env=env)

    @pytest.mark.integration
    def test_grant_requires_perm_on_scratch_target(self):
        # Defense-in-depth: even though the schema validator enforces
        # that agent_scratch_target.permissions ⊇ {upload, download,
        # exec}, the implicit-grant helper checks the perm itself in
        # case future schema relaxation lets a partial-perm scratch
        # through. A perm not present on the target must raise — never
        # silently fall through to Phase-1 (that'd be a hidden
        # escalation surface).
        scratch_partial = {
            "path": "/work/u/CLAUDE_SCRATCH",
            "permissions": ["file_name_only", "upload"],   # no exec
        }
        env = {"name": "fakehpc", "type": "ssh",
               "agent_scratch_target": scratch_partial}
        project = {"name": "demo"}

        # upload: granted
        out = compute_access._scratch_implicit_grant(
            project, env, "/work/u/CLAUDE_SCRATCH/demo/x", "upload")
        assert out is not None and out["_synthetic"] is True

        # exec: missing → raises (not silent None)
        with pytest.raises(compute_access.PermissionDenied) as exc:
            compute_access._scratch_implicit_grant(
                project, env, "/work/u/CLAUDE_SCRATCH/demo/x", "exec")
        assert "exec" in str(exc.value)


# ===========================================================================
# submit_workflow_job's scratch-by-default auto-derivation
# ===========================================================================

class TestSubmitWorkflowScratchDefault:
    @pytest.mark.integration
    def test_empty_workflow_dir_auto_derives_scratch_path(
            self, tmp_path, monkeypatch):
        access_path = _ssh_env_with_scratch(tmp_path)

        captured_dirs: list[str] = []

        from agent.skills import project_path as _pp
        def fake_upload(**kw):
            captured_dirs.append(kw["abs_path"])
            return {"success": True, "compute_env": kw["compute_env_name"],
                    "remote_path": kw["abs_path"], "sha256": "0"*64,
                    "bytes": 1, "duration_s": 0.001,
                    "transferred_at": "t"}
        monkeypatch.setattr(_pp, "upload_to_project_path", fake_upload)

        def fake_run(*a, **kw):
            m = MagicMock(); m.returncode = 0; m.stdout = "12345\n"
            m.stderr = ""
            return m
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = submit_workflow.submit_workflow_job(
            project_name="demo",
            compute_env_name="fakehpc",
            workflow_dir="",   # auto-derive
            workflow_name="my_run_001",
            tool_name="samtools",
            command="samtools view ${input_bam} > ${output_bam}",
            inputs={"input_bam": "/work/u/data/in.bam"},
            outputs={"output_bam": "out.bam"},
            apptainer_sif="/work/u/c/x.sif",
            apptainer_module="apptainer/1.4.1",
            nextflow_module="nextflow/25.04.7",
            slurm={"queue": "general", "time": "00:05:00",
                   "mem": "2G", "cpus": 1},
            access_path=str(access_path))
        assert "error" not in result, result
        # All uploads landed under <scratch>/<project>/<workflow_name>/
        for d in captured_dirs:
            assert d.startswith(
                "/work/u/CLAUDE_SCRATCH/demo/my_run_001/"), d
        # The returned workflow_dir is the auto-derived path
        assert result["workflow_dir"] == \
            "/work/u/CLAUDE_SCRATCH/demo/my_run_001"

    @pytest.mark.integration
    def test_no_scratch_target_refuses_clean(self, tmp_path):
        # Env with no scratch target + empty workflow_dir → clean refusal.
        access_path = _write_access(tmp_path, {
            "compute_envs": [{
                "name": "fakehpc", "type": "ssh",
                "host": "x", "user": "u",
                "container_upload_target": None,
                # No agent_scratch_target.
            }],
            "projects": [{
                "name": "demo",
                "compute_env_access": [{
                    "compute_env": "fakehpc", "directories": []}],
            }],
        })
        result = submit_workflow.submit_workflow_job(
            project_name="demo", compute_env_name="fakehpc",
            workflow_dir="",
            workflow_name="run1", tool_name="x",
            command="x ${o}", inputs={}, outputs={"o": "y.bam"},
            apptainer_sif="/x.sif",
            apptainer_module="apptainer/1.4.1",
            nextflow_module="nextflow/25.04.7",
            slurm={"queue": "g", "time": "00:01:00",
                   "mem": "1G", "cpus": 1},
            access_path=str(access_path))
        assert "error" in result
        assert "no workflow_dir supplied" in result["error"]
        assert "no agent_scratch_target" in result["error"]
