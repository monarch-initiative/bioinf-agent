"""A step never writes beside its inputs: it runs IN its watched directory.

With no `watch_dir`, `run_pipeline_step` and `run_step_in_container` give the command a
fresh directory under the scratch zone and use it as both the working directory and the
watched directory; a named `watch_dir` is the working directory too. A relative output
lands where it is detected, and nothing lands in the shared test-data corpus the inputs
came from — on the host or in the container, whose data directory stays mounted at its
own path for the absolute input paths. The response names the directory, so the agent
can pass it on as the next step's input. `run_in_env` behaves the same, except that an
explicit `working_dir` is honoured as the working directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import mcp_server as m
from agent.mcp_tools import run_tools as R
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


def test_a_named_watch_dir_is_also_the_working_directory(monkeypatch, pid, tmp_path):
    captured: dict = {}
    monkeypatch.setattr(m._env_mgr, "run_in_env", _stub(captured))
    out = tmp_path / "step_out"
    resp = m.run_pipeline_step(env_name="e", command="true", pipeline_id=pid, watch_dir=str(out))
    assert captured == {"working_dir": str(out), "watch_dir": str(out)}
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


def test_run_in_env_runs_in_a_named_watch_dir_when_no_working_dir_is_given(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr(m._env_mgr, "run_in_env", _stub(captured))
    m.run_in_env(env_name="e", command="true", watch_dir=str(tmp_path))
    assert captured == {"working_dir": str(tmp_path), "watch_dir": str(tmp_path)}


# ---------------------------------------------------------------------------
# the same rule inside the frozen image
# ---------------------------------------------------------------------------

@pytest.fixture()
def container(monkeypatch):
    """run_step_in_container with the daemon, the image checks and the validator stubbed;
    the mount and working-directory computation is real. Returns a callable
    (**kwargs) → (response, what the container runner was given)."""
    import uuid
    pid = m.start_pipeline(f"step_dir_container_{uuid.uuid4().hex[:6]}", description="x")["pipeline_id"]
    rec = {"image": "img:1", "platform": "linux/amd64", "image_digest": "sha256:" + "a" * 64}
    monkeypatch.setattr(m._env_cache, "lookup_verified", lambda key: (rec, []))
    monkeypatch.setattr(m, "_check_docker_available", lambda: None)
    monkeypatch.setattr(m._locus, "daemon_is_remote", lambda: False)
    monkeypatch.setattr(m._docker, "_run", lambda *a, **k: {"returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(m._docker, "image_digest", lambda image: rec["image_digest"])
    monkeypatch.setattr(m._validator, "validate",
                        lambda path, etype, env_name="": {"passed": True, "expected_type": etype, "path": path})
    monkeypatch.setattr(m._env_mgr, "hash_outputs", lambda paths: {})
    seen: dict = {}

    def _fake_run(image, command, mounts=None, workdir=None, platform=None, timeout=None, **kw):
        seen.update(mounts=list(mounts or []), workdir=workdir, image=image)
        return {"returncode": 0, "stdout": "", "stderr": "",
                "resource_usage": {"wall_seconds": 0.01, "peak_rss_mb": 1.0, "max_cpu_percent": 1.0}}
    monkeypatch.setattr(m._docker, "run_in_container", _fake_run)

    def _run(**kw):
        return R.run_step_in_container(pipeline_id=pid, freeze_request_key="k", command="tool", **kw), seen
    _run.rec = rec
    return _run


def test_run_step_in_container_defaults_to_a_fresh_step_directory_as_its_cwd(container, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    resp, seen = container(data_dir=str(data))
    wd = Path(seen["workdir"])
    assert wd.is_dir() and wd.parent == workspace.scratch_dir("step_outputs")
    assert wd != data.resolve(), "the data directory is never the working directory"
    assert (str(wd), str(wd)) in seen["mounts"], "the step directory is mounted at its own path"
    assert (str(data.resolve()), str(data.resolve())) in seen["mounts"], "the data directory stays mounted"
    assert resp["watch_dir"] == str(wd) and resp["watch_dir_created"] == str(wd)


def test_run_step_in_container_runs_in_a_named_watch_dir(container, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    out = tmp_path / "step_out"
    resp, seen = container(data_dir=str(data), watch_dir=str(out))
    assert seen["workdir"] == str(out.resolve()) and out.is_dir(), "created, and the cwd"
    assert resp["watch_dir"] == str(out.resolve()) and resp["watch_dir_created"] == str(out.resolve())


def test_the_frozen_image_runs_by_the_digest_its_record_pins(container, tmp_path, monkeypatch):
    """A tag is a name: it can move, and the daemon has failed to resolve a short one for
    an image it held. The step runs the content the record pins."""
    resp, seen = container(data_dir=str(tmp_path))
    assert seen["image"] == container.rec["image_digest"] and resp.get("success"), resp


def test_an_image_built_here_that_is_gone_is_named_and_never_pulled(container, tmp_path, monkeypatch):
    calls = []

    def daemon(argv, **kw):
        calls.append(argv)
        return {"returncode": 1, "stdout": "", "stderr": "Error: No such image"}
    monkeypatch.setattr(m._docker, "_run", daemon)
    resp, _ = container(data_dir=str(tmp_path))
    assert (resp["outcome"], resp["code"]) == ("broke", "run_container.image_missing"), resp
    assert "img:1" in resp["error"] and "freeze" in resp["remedy"]
    assert all(argv[:2] != ["docker", "pull"] for argv in calls), "an image built here has no registry"


def test_an_adopted_image_is_pulled_by_its_pinned_ref(container, tmp_path, monkeypatch):
    pinned = "quay.io/biocontainers/samtools@sha256:" + "b" * 64
    container.rec["image"] = pinned
    calls = []

    def daemon(argv, **kw):
        calls.append(argv)
        return {"returncode": 1 if argv[:3] == ["docker", "image", "inspect"] else 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(m._docker, "_run", daemon)
    resp, seen = container(data_dir=str(tmp_path))
    assert ["docker", "pull", "--platform", "linux/amd64", pinned] in calls
    assert seen["image"] == pinned


def test_one_rule_names_the_frozen_image_everywhere_it_runs():
    from agent.models.core_data import frozen_image_ref
    assert frozen_image_ref("q.io/x@sha256:" + "b" * 64, "sha256:" + "c" * 64) == "q.io/x@sha256:" + "b" * 64
    assert frozen_image_ref("x:latest", "sha256:" + "c" * 64) == "sha256:" + "c" * 64


def test_a_pruned_registry_image_is_pulled_by_its_pinned_ref(container, tmp_path, monkeypatch):
    pinned = "ghcr.io/lab/tool@sha256:" + "d" * 64
    container.rec["image"] = "ghcr.io/lab/tool:1.2"
    container.rec["image_by_digest"] = pinned
    calls = []

    def daemon(argv, **kw):
        calls.append(argv)
        return {"returncode": 1 if argv[:3] == ["docker", "image", "inspect"] else 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(m._docker, "_run", daemon)
    resp, seen = container(data_dir=str(tmp_path))
    assert ["docker", "pull", "--platform", "linux/amd64", pinned] in calls and seen["image"] == pinned


def test_a_missing_image_with_its_saved_archive_says_load_it_not_refreeze_it(container, tmp_path, monkeypatch):
    """A new freeze makes a new digest and orphans every workflow sealed on this one;
    the archive saved at freeze brings back the very image."""
    from agent.skills import freeze
    archive = tmp_path / "tool.tar"
    archive.write_bytes(b"x")
    monkeypatch.setattr(freeze, "tarball_path", lambda rec: str(archive))
    monkeypatch.setattr(m._docker, "_run", lambda argv, **kw: {"returncode": 1, "stdout": "", "stderr": "No such image"})
    resp, _ = container(data_dir=str(tmp_path))
    assert resp["code"] == "run_container.image_missing" and resp["remedy"].startswith(f"docker load -i {archive}")
