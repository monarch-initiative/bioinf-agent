"""A container step sees what the call declared: every declared input's directory and
the watch_dir are mounted at their own host paths, without an `extra_mounts` for each.

A cohort run declared its authored R script and its counts directory as inputs — both
under the workspace, outside data_dir — and the container answered "cannot open file".
The caller had said exactly where the files were; the run had only to look.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import mcp_server as m
from agent.mcp_tools import run_tools as R


@pytest.fixture
def _container_run(monkeypatch, tmp_path):
    """run_step_in_container with the daemon, the image checks and the validator stubbed;
    the mount computation, the step record and the draft are real. Returns a callable
    (inputs, watch_dir, data_dir, extra_mounts) → (result, mounts the container got)."""
    pid = "p_mounts"
    m._pipeline_state.discard(pid)
    m._pipeline_state.start(pid, "mount test")
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
        seen["mounts"] = list(mounts or [])
        return {"returncode": 0, "stdout": "", "stderr": "",
                "resource_usage": {"wall_seconds": 0.01, "peak_rss_mb": 1.0, "max_cpu_percent": 1.0}}
    monkeypatch.setattr(m._docker, "run_in_container", _fake_run)

    def _run(inputs, watch_dir, data_dir="", extra_mounts=()):
        out = R.run_step_in_container(pipeline_id=pid, freeze_request_key="k", command="tool",
                                      inputs=list(inputs), watch_dir=str(watch_dir), data_dir=data_dir,
                                      extra_mounts=list(extra_mounts))
        return out, seen.get("mounts", [])
    return _run


def test_a_declared_inputs_directory_and_the_watch_dir_are_mounted(_container_run, tmp_path):
    script_dir = tmp_path / "scratch" / "deseq2_cohort"
    script_dir.mkdir(parents=True)
    (script_dir / "run_deseq2.R").write_text("cat(1)")
    counts = tmp_path / "common_data" / "counts"
    counts.mkdir(parents=True)
    results = tmp_path / "results"
    out, mounts = _container_run([str(script_dir / "run_deseq2.R"), {"path": str(counts)}], results,
                                 data_dir=str(tmp_path / "data"))
    assert out["code"] == "run_container.step_ran", out
    host_sides = [Path(h) for h, _ in mounts]
    assert all(h == c for h, c in mounts), "every mount is same-path, so host paths work verbatim"
    assert script_dir.resolve() in host_sides, mounts     # the script's directory
    assert counts.resolve() in host_sides, mounts         # the input directory itself
    assert results.resolve() in host_sides, mounts        # the watch_dir, created for the run


def test_a_path_already_under_a_mount_adds_no_second_mount(_container_run, tmp_path):
    data = tmp_path / "data"
    (data / "reads").mkdir(parents=True)
    (data / "reads" / "r1.fq").write_text("@r")
    out, mounts = _container_run([str(data / "reads" / "r1.fq")], data / "out", data_dir=str(data))
    assert out["code"] == "run_container.step_ran", out
    assert [Path(h) for h, _ in mounts] == [data.resolve()], mounts


def test_a_missing_input_is_not_a_mount_to_invent(tmp_path):
    assert R._declared_dirs([str(tmp_path / "nowhere" / "x.txt"), "relative/x", 3]) == []
    (tmp_path / "d").mkdir()
    assert R._declared_dirs([str(tmp_path / "d" / "f.txt"), str(tmp_path / "d")]) == [(tmp_path / "d").resolve()]
