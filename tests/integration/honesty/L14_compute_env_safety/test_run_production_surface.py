"""
L14 cheat-guards — run_production_pipeline, the locus-agnostic PRODUCTION verb.

run_production_pipeline runs a frozen env's workflow against the USER'S project
data (a directories[] path), documented like a production run. ONE verb, two
loci, dispatched on env.type:

  local → render a re-runnable run.sh (docker run the frozen image, same-path
          bind mount), launch in the background, write a manifest.
  ssh   → delegate to submit_workflow_job, sourcing modules + slurm from the
          env config, deriving the staged .sif path (refuse if not staged).

These are pure/surface tests — no real docker, no ssh. They pin:
  - the command placeholder contract (I6 parity: every {PLACEHOLDER} declared,
    every path absolute)
  - the rendered run.sh shape (docker run, --platform, same-path mount, limits)
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
# Pure render helpers — no mocking
# ===========================================================================

class TestRenderLocalCommand:
    @pytest.mark.integration
    def test_substitutes_declared_placeholders(self):
        out = run_production._render_local_command(
            "samtools flagstat {IN} > {OUT}",
            inputs={"IN": "/data/x.bam"}, outputs={"OUT": "/data/o.txt"})
        assert out == "samtools flagstat /data/x.bam > /data/o.txt"

    @pytest.mark.integration
    def test_shell_quotes_paths_with_spaces(self):
        out = run_production._render_local_command(
            "cat {IN}", inputs={"IN": "/data/a b.txt"}, outputs={})
        assert "'/data/a b.txt'" in out

    @pytest.mark.integration
    def test_undeclared_placeholder_refused(self):
        with pytest.raises(ValueError) as exc:
            run_production._render_local_command(
                "tool {IN} {MISSING}", inputs={"IN": "/data/x"}, outputs={})
        assert "MISSING" in str(exc.value)

    @pytest.mark.integration
    def test_relative_path_refused(self):
        with pytest.raises(ValueError) as exc:
            run_production._render_local_command(
                "tool {IN}", inputs={"IN": "relative/x"}, outputs={})
        assert "absolute" in str(exc.value)


class TestRenderRunScript:
    @pytest.mark.integration
    def test_script_shape(self):
        s = run_production._render_run_script(
            image="img@sha256:abc", workdir="/proj/run", platform="linux/amd64",
            concrete_command="samtools flagstat /proj/run/x.bam",
            resources={"mem_gb": 4, "cpus": 2})
        assert "docker run --rm --platform" in s
        assert '-v "$WORKDIR":"$WORKDIR"' in s          # same-path bind mount
        assert "--memory 4g" in s and "--cpus 2" in s   # resources honored
        assert "img@sha256:abc" in s
        assert "samtools flagstat /proj/run/x.bam" in s
        assert "set -euo pipefail" in s
        # self-healing pull guard so the script is re-runnable standalone
        assert "docker image inspect" in s and "docker pull" in s

    @pytest.mark.integration
    def test_no_resources_no_limit_flags(self):
        s = run_production._render_run_script(
            image="i", workdir="/w", platform="linux/amd64",
            concrete_command="true", resources={})
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
            workflow_name="w", tool_name="t", command="t {O}",
            inputs={}, outputs={"O": str(tmp_path / "proj" / "o")},
            freeze_request_key="k", workflow_dir=str(tmp_path / "proj"),
            access_path=ap, _env_cache=_FakeCache(record=None))
        assert "error" in r and "no frozen env" in r["error"]

    @pytest.mark.integration
    def test_contract_violation_refused(self, tmp_path):
        ap = _local_access(tmp_path, str(tmp_path / "proj"))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t {O}",
            inputs={}, outputs={"O": str(tmp_path / "proj" / "o")},
            freeze_request_key="k", workflow_dir=str(tmp_path / "proj"),
            access_path=ap,
            _env_cache=_FakeCache(record=None,
                                  violations=[{"clause": "BUILT"}]))
        assert "error" in r and "honesty contract" in r["error"]

    @pytest.mark.integration
    def test_unknown_env_type_refused(self, tmp_path):
        # An env with a type we don't run. Build the yaml by hand so the
        # loader accepts it (type must be ssh|local at load), then can't —
        # so instead assert via a record present + a monkeypatched type.
        ap = _local_access(tmp_path, str(tmp_path / "proj"))
        # Patch get_compute_env to yield an exotic type post-load.
        import agent.skills.compute_access as ca
        orig = ca.get_compute_env
        try:
            ca.get_compute_env = lambda name, access: {"name": "laptop", "type": "batch"}
            r = run_production.run_production_pipeline(
                project_name="demo", compute_env_name="laptop",
                workflow_name="w", tool_name="t", command="t {O}",
                inputs={}, outputs={"O": str(tmp_path / "proj" / "o")},
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
        granted = tmp_path / "granted"
        granted.mkdir()
        # Grant `granted`, but run against a DIFFERENT dir → PermissionDenied.
        other = tmp_path / "other"
        other.mkdir()
        ap = _local_access(tmp_path, str(granted))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t {O}",
            inputs={}, outputs={"O": str(other / "o")},
            freeze_request_key="k", workflow_dir=str(other),
            access_path=ap, _env_cache=_FakeCache(record=_REC),
            _job_manager=_FakeJobManager())
        assert "error" in r and "not authorized" in r["error"]

    @pytest.mark.integration
    def test_missing_workflow_dir_refused(self, tmp_path, monkeypatch):
        import agent.mcp_server as ms
        monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
        monkeypatch.setattr(ms._locus, "daemon_is_remote", lambda: False)
        # Grant a dir path that does NOT exist on disk → no auto-mkdir.
        missing = tmp_path / "does_not_exist"
        ap = _local_access(tmp_path, str(missing))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t {O}",
            inputs={}, outputs={"O": str(missing / "o")},
            freeze_request_key="k", workflow_dir=str(missing),
            access_path=ap, _env_cache=_FakeCache(record=_REC),
            _job_manager=_FakeJobManager())
        assert "error" in r and "does not exist" in r["error"]

    @pytest.mark.integration
    def test_upload_only_dir_refused_for_exec(self, tmp_path, monkeypatch):
        import agent.mcp_server as ms
        monkeypatch.setattr(ms, "_check_docker_available", lambda: None)
        monkeypatch.setattr(ms._locus, "daemon_is_remote", lambda: False)
        d = tmp_path / "proj"
        d.mkdir()
        # upload but NO exec → the run (which writes outputs in-place) is refused.
        ap = _local_access(tmp_path, str(d), perms=("file_name_only", "upload"))
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="w", tool_name="t", command="t {O}",
            inputs={}, outputs={"O": str(d / "o")},
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
        # Route the submission manifest at tmp so we don't touch the repo.
        from agent.skills import transfer
        monkeypatch.setattr(transfer, "_repo_root", lambda: tmp_path)

        d = tmp_path / "proj"
        d.mkdir()
        ap = _local_access(tmp_path, str(d))
        fake_jm = _FakeJobManager()
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="laptop",
            workflow_name="wf", tool_name="samtools",
            command="samtools flagstat {IN} > {OUT}",
            inputs={"IN": str(d / "x.bam")},
            outputs={"OUT": str(d / "flagstat.txt")},
            freeze_request_key="k", workflow_dir=str(d),
            resources={"mem_gb": 2, "cpus": 1},
            access_path=ap, _env_cache=_FakeCache(record=_REC),
            _job_manager=fake_jm)

        assert r.get("success") is True
        assert r.get("locus") == "local"
        # run.sh written into the project dir, re-runnable
        run_sh = d / "wf.run.sh"
        assert run_sh.is_file()
        body = run_sh.read_text()
        assert "docker run --rm --platform" in body
        assert "samtools flagstat" in body
        # background launch happened with `bash <run.sh>`
        assert fake_jm.calls and "wf.run.sh" in fake_jm.calls[0]["command"]
        # manifest written under the (redirected) job_submissions root
        assert Path(r["manifest_path"]).is_file()


# ===========================================================================
# Cluster refusals (fake cache; monkeypatch the .sif existence probe)
# ===========================================================================

class TestClusterRefusals:
    @pytest.mark.integration
    def test_missing_modules_refused(self, tmp_path):
        ap = _ssh_access(tmp_path, with_modules=False)
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="hpc",
            workflow_name="w", tool_name="t", command="t {O}",
            inputs={}, outputs={"O": "/work/demo/o"},
            freeze_request_key="k", workflow_dir="/work/demo/run1",
            resources={"mem_gb": 4, "time": "01:00:00"},
            access_path=ap, _env_cache=_FakeCache(record=_REC))
        assert "error" in r and "module" in r["error"].lower()

    @pytest.mark.integration
    def test_missing_resources_refused(self, tmp_path):
        ap = _ssh_access(tmp_path, with_modules=True)
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="hpc",
            workflow_name="w", tool_name="t", command="t {O}",
            inputs={}, outputs={"O": "/work/demo/o"},
            freeze_request_key="k", workflow_dir="/work/demo/run1",
            resources={},  # no mem/time → a SLURM job can't declare its ask
            access_path=ap, _env_cache=_FakeCache(record=_REC))
        assert "error" in r and "resources" in r["error"]

    @pytest.mark.integration
    def test_sif_not_staged_refused(self, tmp_path, monkeypatch):
        # Modules + resources present, but the .sif isn't staged → refuse,
        # non-composite (tell the user to stage_apptainer_image first).
        monkeypatch.setattr(stage_apptainer, "_remote_sif_exists",
                            lambda *a, **k: False)
        ap = _ssh_access(tmp_path, with_modules=True)
        r = run_production.run_production_pipeline(
            project_name="demo", compute_env_name="hpc",
            workflow_name="w", tool_name="t", command="t {O}",
            inputs={}, outputs={"O": "/work/demo/o"},
            freeze_request_key="k", workflow_dir="/work/demo/run1",
            resources={"mem_gb": 4, "time": "01:00:00"},
            access_path=ap, _env_cache=_FakeCache(record=_REC))
        assert "error" in r and "not staged" in r["error"]
        assert "stage_apptainer_image" in r["error"]
