"""A jar whose recorded verify exits non-zero is degraded, never proven.

The host-side verify of install_jar_tool is non-gating: freeze is the authoritative locus,
and it re-runs the same command inside the shipped image. The record must say what the host
run saw, because the freeze fails on exactly that command: `degraded
env_manager.jar_verify_failed`, the exit code, and the output of both streams. A verify that
exits 0 keeps the install proven and is cached as a verification; a failed one verifies
nothing; no verify at all leaves the install proven with nothing recorded.
"""
from __future__ import annotations

import pytest

import agent.mcp_server as ms
from agent.mcp_tools import env_tools


@pytest.fixture
def jar_route(monkeypatch):
    cached: list = []
    monkeypatch.setattr(ms._env_mgr, "install_jar_tool", lambda **kw: {
        "success": True, "tool_name": kw["tool_name"], "wrapper_path": "/w", "jar_path": "/j.jar",
        "jar_url": kw["jar_url"], "share_dir": "/s", "java_flags": kw["java_flags"], "log": [],
        "outcome": "proven", "code": "env_manager.jar_installed"})
    monkeypatch.setattr(ms._pipeline_state, "add_install_step", lambda *a, **k: 0)
    monkeypatch.setattr(ms._pipeline_state, "cache_verification", lambda *a, **k: cached.append(a))

    def run(verify_command: str, rc: int, stdout: str = "", stderr: str = ""):
        calls: list = []

        def _run(*a, **k):
            calls.append(a)
            return {"returncode": rc, "stdout": stdout, "stderr": stderr}
        monkeypatch.setattr(ms._env_mgr, "run_in_env", _run)
        r = env_tools.install_jar_tool(env_name="e", tool_name="picard",
                                       jar_url="https://github.com/x/picard/releases/download/3.5.0/picard.jar",
                                       verify_command=verify_command, pipeline_id="p")
        return r, cached, calls
    return run


def test_a_verify_that_exits_non_zero_is_degraded_with_both_streams(jar_route):
    r, cached, _ = jar_route("picard --version", 1,
                             stderr="'--version' is not a valid command. See PicardCommandLine -h")
    assert (r["outcome"], r["code"]) == ("degraded", "env_manager.jar_verify_failed")
    assert r["success"] is True and r["verify_returncode"] == 1
    assert "not a valid command" in r["verify_output"]
    assert "`picard --version` exited 1" in r["error"] and "freeze re-runs this command" in r["error"]
    assert cached == []                      # a failed smoke is not a verification


def test_a_verify_that_exits_zero_stays_proven_and_is_cached(jar_route):
    r, cached, _ = jar_route("bash -c 'picard 2>&1 | grep -q FastqToSam'", 0, stdout="ok")
    assert (r["outcome"], r["code"]) == ("proven", "env_manager.jar_installed")
    assert r["verify_returncode"] == 0 and "error" not in r
    assert len(cached) == 1


def test_no_verify_leaves_the_install_proven_with_nothing_recorded(jar_route):
    r, cached, calls = jar_route("", 7)
    assert (r["outcome"], r["code"]) == ("proven", "env_manager.jar_installed")
    assert "verify_returncode" not in r and cached == [] and calls == []
