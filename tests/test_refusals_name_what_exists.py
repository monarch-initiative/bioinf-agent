"""A refusal for a name nobody knows lists the names that ARE known.

"unknown job_id" / "no frozen env" / "unknown pipeline_id" / "env not found" with no
listing leaves the caller guessing at a spelling. Each site here names the ids that
exist, newest first where there is an order, and keeps its stable code.
"""
import json

import pytest

from agent.skills.job_manager import JobManager


@pytest.fixture
def jm(tmp_path):
    """A JobManager over a scratch jobs dir and a real EnvManager over a scratch envs
    dir, built without the constructors so no conda or workspace is consulted."""
    from agent.skills.env_manager import EnvManager
    em = EnvManager.__new__(EnvManager)
    em.config = {}
    em.envs_dir = tmp_path / "envs"
    m = JobManager.__new__(JobManager)
    m.config = {}
    m.jobs_dir = tmp_path / "jobs"
    m.jobs_dir.mkdir()
    m._procs = {}
    m._env_mgr = em
    return m


def _status(jm, job_id, start):
    (jm.jobs_dir / f"{job_id}.status.json").write_text(json.dumps(
        {"job_id": job_id, "state": "exited", "returncode": 0, "start_time_iso": start}))


def test_unknown_job_check_lists_known_jobs_newest_first(jm):
    _status(jm, "older", "2026-10-01T00:00:00")
    _status(jm, "newer", "2026-10-09T00:00:00")
    r = jm.check("nope", log_tail_lines=0)
    assert r["outcome"] == "refused" and r["code"] == "job_manager.unknown_job_check"
    assert r["job_id"] == "nope"
    assert r["known_jobs"] == ["newer", "older"]
    assert "newer" in r["error"] and "unknown job_id: nope" in r["error"]


def test_unknown_job_cancel_lists_known_jobs(jm):
    _status(jm, "only", "2026-10-09T00:00:00")
    r = jm.cancel("nope")
    assert r["code"] == "job_manager.unknown_job_cancel"
    assert r["known_jobs"] == ["only"]


def test_unknown_job_with_no_jobs_says_so(jm):
    r = jm.check("nope", log_tail_lines=0)
    assert r["known_jobs"] == [] and "no job has been started" in r["error"]


def test_start_into_missing_env_names_existing_envs_and_keeps_job_id(jm, tmp_path):
    (tmp_path / "envs" / "have_this").mkdir(parents=True)
    r = jm.start("echo hi", env_name="not_this", job_id="j1")
    assert r["outcome"] == "refused" and r["code"] == "job_manager.env_missing"
    assert r["job_id"] == "j1" and r["env_name"] == "not_this"
    assert r["existing_envs"] == ["have_this"]


def test_run_step_in_container_lists_frozen_envs_when_key_unknown(tmp_path, monkeypatch):
    from agent import mcp_server as ms
    from agent.mcp_tools import run_tools
    from agent.skills.freeze import EnvCache

    cache = EnvCache(tmp_path / "cache.json")
    cache._save({"samtools|linux/amd64|1.21": {"image": "x"}, "bcftools|linux/amd64|1.20": {"image": "y"}})
    monkeypatch.setattr(ms, "_env_cache", cache)
    monkeypatch.setattr(cache, "lookup_verified", lambda key: (None, []))
    r = run_tools.run_step_in_container(
        freeze_request_key="nothing|linux/amd64|0", command="true", pipeline_id="p", inputs=[])
    assert r["code"] == "run_container.no_frozen_env"
    assert r["frozen_envs"] == ["bcftools|linux/amd64|1.20", "samtools|linux/amd64|1.21"]
    assert "samtools|linux/amd64|1.21" in r["error"]


def test_verify_service_dependency_unknown_pipeline_lists_open_drafts(tmp_path, monkeypatch):
    from agent import mcp_server as ms
    from agent.mcp_tools import service_tools
    from agent.skills.pipeline_state import PipelineState

    ps = PipelineState({})
    monkeypatch.setattr(ps, "get_draft", lambda pid: None)
    monkeypatch.setattr(ps, "all_drafts", lambda: {"open_one": {}})
    monkeypatch.setattr(ms, "_pipeline_state", ps)
    r = service_tools.verify_service_dependency(pipeline_id="nope", service_name="db", env_name="e")
    assert r["code"] == "service.unknown_pipeline_id" and r["success"] is False
    assert r["open_drafts"] == ["open_one"] and "open_one" in r["error"]


def test_docker_preflight_refusals_carry_error(monkeypatch):
    import subprocess
    from agent import mcp_server as ms
    monkeypatch.delenv("BIOINF_SKIP_DOCKER_PREFLIGHT", raising=False)

    def _raise(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(subprocess, "run", _raise)
    r = ms._check_docker_available()
    assert r["outcome"] == "refused" and r["code"] == "docker.not_installed"
    assert r["stage"] == "docker_preflight" and r["success"] is False
    assert r["error"] == r["message"]
