"""An install into a conda env that does not exist is REFUSED with the one call that
creates it — on every route. pip and R used to run anyway and report conda's
EnvironmentLocationNotFound as a broken install; the git, jar, binary, perl, cargo and go
routes refused. The env's runtime (which Python, which R) is the agent's recorded choice,
so no install creates the env it targets.

And a `broke` install always names its cause: `error` is the last line of stderr when the
route did not set one.
"""
from __future__ import annotations

import agent.mcp_server as ms
from agent.mcp_tools import env_tools
from agent.skills import workspace


def _no_envs(monkeypatch, tmp_path):
    monkeypatch.setattr(ms._env_mgr, "envs_dir", tmp_path / "envs")
    calls = []
    monkeypatch.setattr(ms._env_mgr, "run_in_env",
                        lambda *a, **kw: calls.append(a) or {"returncode": 0, "stdout": "", "stderr": ""})
    return calls


def test_pip_into_a_missing_env_is_refused_before_anything_runs(monkeypatch, tmp_path):
    ran = _no_envs(monkeypatch, tmp_path)
    r = env_tools.install_pip_package(env_name="no_such_env", name="pysam")
    assert r["outcome"] == "refused" and r["code"] == "env_manager.pip_env_missing"
    assert "install_conda_packages(env_name='no_such_env'" in r["remedy"] and "python=" in r["remedy"]
    assert ran == []


def test_r_into_a_missing_env_is_refused_before_anything_runs(monkeypatch, tmp_path):
    ran = _no_envs(monkeypatch, tmp_path)
    r = env_tools.install_r_package(env_name="no_such_env", name="DESeq2", source="bioconductor")
    assert r["outcome"] == "refused" and r["code"] == "env_manager.r_env_missing"
    assert "install_conda_packages(env_name='no_such_env'" in r["remedy"] and "r-base=" in r["remedy"]
    assert ran == []


def test_an_existing_env_is_left_alone_by_the_gate(monkeypatch, tmp_path):
    ran = _no_envs(monkeypatch, tmp_path)
    (tmp_path / "envs" / "e").mkdir(parents=True)
    r = env_tools.install_pip_package(env_name="e", name="pysam")
    assert r["code"] == "env_manager.pip_installed" and ran


def test_a_broke_install_names_its_cause():
    r = env_tools._install_outcome({"returncode": 1, "stdout": "", "stderr": "collecting\nERROR: no matching distribution\n"},
                                   "x.ok", "x.failed")
    assert r["outcome"] == "broke" and r["error"] == "ERROR: no matching distribution"
    r = env_tools._install_outcome({"returncode": 2, "stdout": "", "stderr": ""}, "x.ok", "x.failed")
    assert r["error"] == "exit 2 with nothing on stderr"
    r = env_tools._install_outcome({"returncode": 1, "stderr": "x\n", "error": "already said"}, "x.ok", "x.failed")
    assert r["error"] == "already said"
    assert env_tools._install_outcome({"returncode": 0, "stderr": "warn\n"}, "x.ok", "x.failed")["outcome"] == "proven"
