"""A step never writes beside its inputs.

With no `watch_dir`, `run_pipeline_step` gives the command a fresh directory under the
scratch zone and uses it as both the working directory and the watched directory: a
relative output lands where it is detected, and nothing lands in the shared test-data
corpus the inputs came from. The response names the directory, so the agent can pass
it on as the next step's input. `run_in_env` behaves the same when it is given neither
a working_dir nor a watch_dir.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import mcp_server as m
from agent.skills import workspace


def _stub(captured: dict):
    def _run(env_name, command, working_dir=None, timeout=1800, inputs=None, watch_dir=None):
        captured.update(working_dir=working_dir, watch_dir=watch_dir)
        return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "success": True,
                "detected_outputs": [], "inputs": inputs or [], "runtime_seconds": 0.01,
                "resource_usage": {"wall_seconds": 0.01, "peak_rss_mb": 1.0, "max_cpu_percent": 1.0}}
    return _run


@pytest.fixture()
def pid():
    return m.start_pipeline("step_dir_default", description="x")["pipeline_id"]


def test_run_pipeline_step_defaults_to_a_fresh_step_directory(monkeypatch, pid):
    captured: dict = {}
    monkeypatch.setattr(m._env_mgr, "run_in_env", _stub(captured))
    resp = m.run_pipeline_step(env_name="e", command="echo hi > out.txt", pipeline_id=pid)
    wd = Path(captured["watch_dir"])
    assert captured["working_dir"] == captured["watch_dir"], "the step's cwd IS its output directory"
    assert wd.is_dir()
    assert wd.parent == workspace.scratch_dir("step_outputs")
    assert resp["watch_dir"] == str(wd)
    assert resp["watch_dir_created"] == str(wd)


def test_two_steps_get_two_directories(monkeypatch, pid):
    seen = []
    for _ in range(2):
        captured: dict = {}
        monkeypatch.setattr(m._env_mgr, "run_in_env", _stub(captured))
        m.run_pipeline_step(env_name="e", command="true", pipeline_id=pid)
        seen.append(captured["watch_dir"])
    assert len(set(seen)) == 2, seen


def test_a_named_watch_dir_is_used_and_the_cwd_is_left_alone(monkeypatch, pid, tmp_path):
    captured: dict = {}
    monkeypatch.setattr(m._env_mgr, "run_in_env", _stub(captured))
    out = tmp_path / "step_out"
    resp = m.run_pipeline_step(env_name="e", command="true", pipeline_id=pid, watch_dir=str(out))
    assert captured["watch_dir"] == str(out) and captured["working_dir"] is None
    assert out.is_dir(), "a missing watch_dir is created"
    assert resp["watch_dir"] == str(out) and resp["watch_dir_created"] == str(out)


def test_the_no_output_hint_names_the_directory_that_was_watched(monkeypatch, pid):
    captured: dict = {}
    monkeypatch.setattr(m._env_mgr, "run_in_env", _stub(captured))
    resp = m.run_pipeline_step(env_name="e", command="true", pipeline_id=pid,
                               output_types={".bam": "bam"})
    assert captured["watch_dir"] in resp["output_detection_hint"]


def test_run_in_env_defaults_the_same_way_when_given_neither_directory(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(m._env_mgr, "run_in_env", _stub(captured))
    resp = m.run_in_env(env_name="e", command="true")
    assert captured["working_dir"] == captured["watch_dir"]
    assert Path(captured["watch_dir"]).parent == workspace.scratch_dir("step_outputs")
    assert resp["watch_dir"] == captured["watch_dir"]


def test_run_in_env_keeps_a_caller_working_dir_as_the_watched_directory(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr(m._env_mgr, "run_in_env", _stub(captured))
    resp = m.run_in_env(env_name="e", command="true", working_dir=str(tmp_path))
    assert captured == {"working_dir": str(tmp_path), "watch_dir": str(tmp_path)}
    assert resp["watch_dir"] == str(tmp_path)
