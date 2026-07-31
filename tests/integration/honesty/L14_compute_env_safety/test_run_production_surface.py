"""
L14 cheat-guards — run_production_pipeline, the locus-agnostic PRODUCTION verb.

run_production_pipeline runs a frozen env's workflow against the USER'S project
data (a directories[] path), documented like a production run. ONE verb, two
loci, dispatched on env.type:

  local → render a re-runnable run.sh (docker run the frozen image, same-path
          bind mounts), launch in the background, write a manifest.
  ssh   → delegate to submit_workflow_job, sourcing modules + slurm from the
          env config, deriving the staged .sif path (refuse if not staged).

These are pure/surface tests — no real docker, no ssh. They pin:
  - the command contract, IDENTICAL to the cluster path so one command string
    runs on both loci: ${name} placeholders (not {name}), inputs ABSOLUTE,
    outputs BARE filenames written into workflow_dir
  - the rendered run.sh shape (docker run, --platform, same-path mounts, limits)
  - resources → slurm / docker-flag mapping
  - dispatch refusals (unknown type, no frozen env, contract violation)
  - the local auth wall (workflow_dir must be a granted upload+exec dir; must
    exist — no auto-mkdir in the user's territory)
  - the cluster refusals (missing env modules, missing resources, .sif not
    staged)
  - the local happy path with fakes: run.sh written, manifest written, proven
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent.skills import run_production, stage_apptainer


# ===========================================================================
# Pure render helpers — no mocking. The command contract MATCHES the cluster.
# ===========================================================================

class TestRenderLocalCommand:
    @pytest.mark.integration
    def test_substitutes_dollar_brace_placeholders(self):
        # ${name} — the SAME syntax workflow_render uses, so one command runs
        # on both loci. inputs absolute; outputs bare filenames.
        out = run_production._render_local_command(
            "samtools flagstat ${IN} > ${OUT}",
            inputs={"IN": "/data/x.bam"}, outputs={"OUT": "o.txt"})
        assert out == "samtools flagstat /data/x.bam > o.txt"

    @pytest.mark.integration
    def test_curly_only_is_NOT_a_placeholder(self):
        # A bare {IN} (no $) is left untouched — only ${IN} substitutes. This is
        # what makes the syntax identical to the cluster's.
        out = run_production._render_local_command(
            "echo {IN}", inputs={}, outputs={})
        assert out == "echo {IN}"

    @pytest.mark.integration
    def test_shell_quotes_input_paths_with_spaces(self):
        out = run_production._render_local_command(
            "cat ${IN}", inputs={"IN": "/data/a b.txt"}, outputs={})
        assert "'/data/a b.txt'" in out

    @pytest.mark.integration
    def test_undeclared_placeholder_refused(self):
        with pytest.raises(ValueError) as exc:
            run_production._render_local_command(
                "tool ${IN} ${MISSING}", inputs={"IN": "/data/x"}, outputs={})
        assert "MISSING" in str(exc.value)

    @pytest.mark.integration
    def test_relative_input_refused(self):
        with pytest.raises(ValueError) as exc:
            run_production._render_local_command(
                "tool ${IN}", inputs={"IN": "relative/x"}, outputs={})
        assert "absolute" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", ["/abs/out.txt", "sub/out.txt", "..", ""])
    def test_non_bare_output_refused(self, bad):
        # outputs must be BARE filenames (land in workflow_dir) — same as cluster.
        with pytest.raises(ValueError) as exc:
            run_production._render_local_command(
                "tool ${OUT}", inputs={}, outputs={"OUT": bad})
        assert "bare filename" in str(exc.value).lower()


class TestRenderRunScript:
    @pytest.mark.integration
    def test_script_shape(self):
        s = run_production._render_run_script(
            image="img@sha256:abc", workdir="/proj/run", platform="linux/amd64",
            concrete_command="samtools flagstat /proj/run/x.bam > o.txt",
            resources={"mem_gb": 4, "cpus": 2},
            mounts=["/proj/run", "/refs"])
        assert "docker run --rm --platform" in s
        assert "-v /proj/run:/proj/run" in s          # same-path bind mount
        assert "-v /refs:/refs" in s                  # input-parent mount
        assert "--memory 4g" in s and "--cpus 2" in s
        assert "img@sha256:abc" in s
        assert "samtools flagstat /proj/run/x.bam" in s
        assert "set -euo pipefail" in s
        assert "docker image inspect" in s and "docker pull" in s

    @pytest.mark.integration
    def test_no_resources_no_limit_flags(self):
        s = run_production._render_run_script(
            image="i", workdir="/w", platform="linux/amd64",
            concrete_command="true", resources={}, mounts=["/w"])
        assert "--memory" not in s and "--cpus" not in s


class TestResourceMapping:
    @pytest.mark.integration
    def test_resources_to_slurm(self):
        out = run_production._resources_to_slurm(
            {"mem_gb": 8, "time": "02:00:00", "cpus": 4, "gpus": 0})
        assert out == {"mem": "8g", "time": "02:00:00", "cpus": 4, "gpus": 0}

    @pytest.mark.integration
    def test_docker_flags(self):
        assert run_production._docker_resource_flags({"mem_gb": 2, "cpus": 1}) == \
            ["--memory", "2g", "--cpus", "1"]
        assert run_production._docker_resource_flags({}) == []


# ===========================================================================
# Fakes + access builders
# ===========================================================================

class _FakeCache:
    """Stands in for EnvCache. lookup_verified(key) -> (record, violations)."""
    def __init__(self, record=None, violations=None):
        self._record = record
        self._violations = violations or []
    def lookup_verified(self, key):
        return self._record, self._violations


class _FakeJobManager:
    def __init__(self):
        self.calls = []
    def start(self, command, env_name="", job_id="", working_dir=""):
        self.calls.append({"command": command, "job_id": job_id,
                           "working_dir": working_dir})
        return {"job_id": job_id or "j1", "state": "running"}


_REC = {"name": "myenv", "image": "quay.io/x@sha256:deadbeef",
        "image_digest": "sha256:deadbeef", "content_digest": "sha256:cafef00dbabe"}


def _write(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


def _local_access(tmp_path: Path, granted_dir: str,
                  perms=("file_name_only", "upload", "download", "exec")) -> str:
    return _write(tmp_path, {
        "compute_envs": [{
            "name": "laptop", "type": "local",
            "container_upload_target": None,
        }],
        "projects": [{
            "name": "demo", "compute_envs": ["laptop"],
            "directories": [{"env": "laptop", "path": granted_dir,
                             "permissions": list(perms),
                             "description": "x"}],
        }],
    })


def _ssh_access(tmp_path: Path, *, with_modules: bool) -> str:
    env = {"name": "hpc", "type": "ssh", "host": "h.example.edu", "user": "u",
           "container_upload_target": {"path": "/scratch/containers",
                                       "permissions": ["upload"]}}
    if with_modules:
        env["apptainer_module"] = "apptainer/1.5.0"
        env["nextflow_module"] = "nextflow/25.04.7"
    return _write(tmp_path, {
        "compute_envs": [env],
        "projects": [{
            "name": "demo", "compute_envs": ["hpc"],
            "directories": [{"env": "hpc", "path": "/work/demo",
                             "permissions": ["upload", "exec"],
                             "description": "x"}],
        }],
    })


# ===========================================================================
# Dispatch-level refusals (fake cache, no docker)
# ===========================================================================

class TestDispatchRefusals:
    @pytest.mark.integration
    def test_no_frozen_env_refused(self, tmp_path):
        ap = _local_access(tmp_path, str(tmp_path / "proj"))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t ${O}",
            inputs={}, outputs={"O": "o"},
            freeze_request_key="k", workflow_dir=str(tmp_path / "proj"),
            access_path=ap, _env_cache=_FakeCache(record=None))
        assert "error" in r and "no frozen env" in r["error"]

    @pytest.mark.integration
    def test_contract_violation_refused(self, tmp_path):
        ap = _local_access(tmp_path, str(tmp_path / "proj"))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t ${O}",
            inputs={}, outputs={"O": "o"},
            freeze_request_key="k", workflow_dir=str(tmp_path / "proj"),
            access_path=ap,
            _env_cache=_FakeCache(record=None,
                                  violations=[{"clause": "BUILT"}]))
        assert "error" in r and "honesty contract" in r["error"]

    @pytest.mark.integration
    def test_unknown_env_type_refused(self, tmp_path):
        ap = _local_access(tmp_path, str(tmp_path / "proj"))
        import agent.skills.compute_access as ca
        orig = ca.get_compute_env
        try:
            ca.get_compute_env = lambda name, access: {"name": "laptop", "type": "batch"}
            r = run_production.run_production_pipeline(
                project_name="demo", compute_env_name="laptop",
                workflow_name="w", tool_name="t", command="t ${O}",
                inputs={}, outputs={"O": "o"},
                freeze_request_key="k", workflow_dir=str(tmp_path / "proj"),
                access_path=ap, _env_cache=_FakeCache(record=_REC))
        finally:
            ca.get_compute_env = orig
        assert "error" in r and "type='batch'" in r["error"]


# ===========================================================================
# Local auth wall
# ===========================================================================

class TestLocalAuthWall:
    @pytest.mark.integration
    def test_unauthorized_dir_refused(self, tmp_path, monkeypatch):
        import agent.mcp_server as ms
        monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
        monkeypatch.setattr(ms._locus, "daemon_is_remote", lambda: False)
        granted = tmp_path / "granted"; granted.mkdir()
        other = tmp_path / "other"; other.mkdir()
        ap = _local_access(tmp_path, str(granted))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t ${O}",
            inputs={}, outputs={"O": "o"},
            freeze_request_key="k", workflow_dir=str(other),
            access_path=ap, _env_cache=_FakeCache(record=_REC),
            _job_manager=_FakeJobManager())
        assert "error" in r and "not authorized" in r["error"]

    @pytest.mark.integration
    def test_missing_workflow_dir_refused(self, tmp_path, monkeypatch):
        import agent.mcp_server as ms
        monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
        monkeypatch.setattr(ms._locus, "daemon_is_remote", lambda: False)
        missing = tmp_path / "does_not_exist"
        ap = _local_access(tmp_path, str(missing))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t ${O}",
            inputs={}, outputs={"O": "o"},
            freeze_request_key="k", workflow_dir=str(missing),
            access_path=ap, _env_cache=_FakeCache(record=_REC),
            _job_manager=_FakeJobManager())
        assert "error" in r and "does not exist" in r["error"]

    @pytest.mark.integration
    def test_upload_only_dir_refused_for_exec(self, tmp_path, monkeypatch):
        import agent.mcp_server as ms
        monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
        monkeypatch.setattr(ms._locus, "daemon_is_remote", lambda: False)
        d = tmp_path / "proj"; d.mkdir()
        ap = _local_access(tmp_path, str(d), perms=("file_name_only", "upload"))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t ${O}",
            inputs={}, outputs={"O": "o"},
            freeze_request_key="k", workflow_dir=str(d),
            access_path=ap, _env_cache=_FakeCache(record=_REC),
            _job_manager=_FakeJobManager())
        assert "error" in r


# ===========================================================================
# Local happy path (fakes: docker "available", fake JobManager)
# ===========================================================================

class TestLocalHappyPath:
    @pytest.mark.integration
    def test_renders_and_launches(self, tmp_path, monkeypatch):
        import agent.mcp_server as ms
        monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
        monkeypatch.setattr(ms._locus, "daemon_is_remote", lambda: False)
        from agent.skills import transfer
        monkeypatch.setattr(transfer, "_repo_root", lambda: tmp_path)

        d = tmp_path / "proj"; d.mkdir()
        ap = _local_access(tmp_path, str(d))
        fake_jm = _FakeJobManager()
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="wf", tool_name="samtools",
            command="samtools flagstat ${IN} > ${OUT}",
            inputs={"IN": str(d / "x.bam")},
            outputs={"OUT": "flagstat.txt"},
            freeze_request_key="k", workflow_dir=str(d),
            resources={"mem_gb": 2, "cpus": 1},
            access_path=ap, _env_cache=_FakeCache(record=_REC),
            _job_manager=fake_jm)

        assert r.get("success") is True
        assert r.get("locus") == "local"
        run_sh = d / "wf.run.sh"
        assert run_sh.is_file()
        body = run_sh.read_text()
        assert "docker run --rm --platform" in body
        assert "samtools flagstat" in body
        assert f"-v {d}:{d}" in body                  # workflow_dir mounted same-path
        assert "flagstat.txt" in body                 # bare output name in the command
        assert fake_jm.calls and "wf.run.sh" in fake_jm.calls[0]["command"]
        assert Path(r["manifest_path"]).is_file()

    @pytest.mark.integration
    def test_input_outside_workflow_dir_is_mounted(self, tmp_path, monkeypatch):
        # An input living OUTSIDE workflow_dir gets its parent mounted, so the
        # absolute path resolves in-container — local parity with the cluster
        # reading an absolute path anywhere on its filesystem.
        import agent.mcp_server as ms
        monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
        monkeypatch.setattr(ms._locus, "daemon_is_remote", lambda: False)
        from agent.skills import transfer
        monkeypatch.setattr(transfer, "_repo_root", lambda: tmp_path)

        d = tmp_path / "proj"; d.mkdir()
        refs = tmp_path / "refs"; refs.mkdir()
        ap = _local_access(tmp_path, str(d))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="wf", tool_name="t",
            command="t ${REF} ${IN} > ${OUT}",
            inputs={"REF": str(refs / "genome.fa"), "IN": str(d / "x.bam")},
            outputs={"OUT": "out.txt"},
            freeze_request_key="k", workflow_dir=str(d),
            access_path=ap, _env_cache=_FakeCache(record=_REC),
            _job_manager=_FakeJobManager())
        assert r.get("success") is True
        body = (d / "wf.run.sh").read_text()
        assert f"-v {d}:{d}" in body
        assert f"-v {refs}:{refs}" in body            # external input parent mounted


# ===========================================================================
# Cluster refusals (fake cache; monkeypatch the .sif existence probe)
# ===========================================================================

class TestClusterRefusals:
    @pytest.mark.integration
    def test_missing_modules_refused(self, tmp_path):
        ap = _ssh_access(tmp_path, with_modules=False)
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="hpc",
            workflow_name="w", tool_name="t", command="t ${O}",
            inputs={}, outputs={"O": "o"},
            freeze_request_key="k", workflow_dir="/work/demo/run1",
            resources={"mem_gb": 4, "time": "01:00:00"},
            access_path=ap, _env_cache=_FakeCache(record=_REC))
        assert "error" in r and "module" in r["error"].lower()

    @pytest.mark.integration
    def test_missing_resources_refused(self, tmp_path):
        ap = _ssh_access(tmp_path, with_modules=True)
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="hpc",
            workflow_name="w", tool_name="t", command="t ${O}",
            inputs={}, outputs={"O": "o"},
            freeze_request_key="k", workflow_dir="/work/demo/run1",
            resources={},
            access_path=ap, _env_cache=_FakeCache(record=_REC))
        assert "error" in r and "resources" in r["error"]

    @pytest.mark.integration
    def test_sif_not_staged_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stage_apptainer, "_remote_sif_exists",
                            lambda *a, **k: False)
        ap = _ssh_access(tmp_path, with_modules=True)
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="hpc",
            workflow_name="w", tool_name="t", command="t ${O}",
            inputs={}, outputs={"O": "o"},
            freeze_request_key="k", workflow_dir="/work/demo/run1",
            resources={"mem_gb": 4, "time": "01:00:00"},
            access_path=ap, _env_cache=_FakeCache(record=_REC))
        assert "error" in r and "not staged" in r["error"]
        assert "stage_apptainer_image" in r["error"]
