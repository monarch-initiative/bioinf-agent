"""A failure names its cause, and the two seams that wrote into the user's data stop.

Three agents in one corpus pass spent an hour each on the same tool because the build's
answer was its own code: the image had built and only the in-image evidence had failed, or
the real refusal sat one level down under `build`. The same pass left sidecar files inside
the read-only corpus and rewrote one of its files in place. Each test here is one of those
walls, with the answer the caller now gets.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.mcp_tools import freeze_tools as ft
from agent.skills import synthesis


# ---------------------------------------------------------------------------
# freeze: the failed build as the caller must read it
# ---------------------------------------------------------------------------

def _built_br(**over) -> dict:
    return {"name": "gab", "image": "gab:latest", "image_digest": "sha256:" + "a" * 64,
            "verifications": [{"tool": "gab", "check": "python /opt/gab/run.py -h", "rc": 2,
                               "passed": False, "out": "python: can't open file '/opt/gab/run.py'"}],
            "honesty_violations": [{"invariant": "VALIDATED_IN_IMAGE.evidence_passed",
                                    "where": "verifications[gab]",
                                    "message": "gab: evidence 'python /opt/gab/run.py -h' did not pass (rc=2)"}],
            "success": False, **over}


def test_an_image_that_built_and_failed_its_evidence_is_an_evidence_failure():
    r = ft._build_failure(_built_br(), draft={"x": 1}, conda_deps=[], rkey="gab|linux/amd64|none",
                          adopt={"found": False}, extra={})
    assert r["outcome"] == "broke" and r["code"] == "freeze.evidence_failed" and r["stage"] == "evidence"
    assert r["error"] == "gab: evidence 'python /opt/gab/run.py -h' did not pass (rc=2)"
    assert r["verifications"][0]["out"].startswith("python: can't open file")
    assert r["violations"][0]["invariant"] == "VALIDATED_IN_IMAGE.evidence_passed"
    assert r["build"]["image_digest"].startswith("sha256:")


def test_two_violations_are_both_in_the_error():
    br = _built_br()
    br["honesty_violations"].append({"invariant": "VALIDATED_IN_IMAGE.discriminates",
                                     "message": "numba: evidence passes in a control image without the tool"})
    r = ft._build_failure(br, {}, [], "k", {}, {})
    assert "did not pass (rc=2) | numba: evidence passes in a control image" in r["error"]


def test_a_build_that_failed_before_an_image_exists_keeps_its_stage_and_error():
    br = {"success": False, "stage": "install", "code": "env_build.install_failed",
          "error": "bash: line 1: gcc: command not found", "stderr": "+ gcc x.c\nbash: line 1: gcc: command not found\n"}
    r = ft._build_failure(br, {"x": 1}, ["numba"], "k", {}, {})
    assert r["code"] == "freeze.container_build_failed" and r["stage"] == "container_build"
    assert r["error"] == "bash: line 1: gcc: command not found"       # derive_error reads the inner error
    assert "engine_coupled: true" in r["hint"]


def test_the_hint_names_the_change_for_each_wall():
    no_draft = {"success": False, "stage": "declare", "error": "genome_assembly_booster *."}
    assert "no draft records how these tools were installed" in ft._build_failure_hint(no_draft, None, [])
    assert ft._build_failure_hint(no_draft, {"pipeline_name": "x"}, []) == ""
    conda_line = {"success": False, "stage": "install", "error": "bash: line 1: conda: command not found"}
    assert "install_conda_packages" in ft._build_failure_hint(conda_line, {"x": 1}, [])
    pip_line = {"success": False, "stage": "install", "stderr": "+ pip install numba\nbash: line 1: pip: command not found\n"}
    assert "`pip` is not in the build image" in ft._build_failure_hint(pip_line, {"x": 1}, ["python"])
    python_with_env = {"success": False, "stage": "install", "error": "bash: line 1: python: command not found"}
    assert "engine_coupled: true" in ft._build_failure_hint(python_with_env, {"x": 1}, ["numba", "python"])
    python_no_env = {"success": False, "stage": "install", "error": "bash: line 1: python: command not found"}
    assert "declare the package that provides it" in ft._build_failure_hint(python_no_env, {"x": 1}, [])
    assert ft._build_failure_hint({"success": False, "stage": "freeze", "error": "disk full"}, {"x": 1}, []) == ""


def test_the_hint_rides_only_when_there_is_one():
    r = ft._build_failure({"success": False, "stage": "freeze", "error": "disk full"}, {}, [], "k", {}, {"buildkit_prune": {}})
    assert "hint" not in r and r["buildkit_prune"] == {} and r["error"] == "disk full"


# ---------------------------------------------------------------------------
# the evidence runner keeps what the command said
# ---------------------------------------------------------------------------

def test_the_evidence_runner_keeps_the_end_of_stderr_when_stdout_is_empty():
    import agent.skills.container_build as cbmod
    cb = cbmod.ContainerBuild()
    answers = {"a": (2, "", "python: can't open file '/opt/gab/run.py': [Errno 2] No such file or directory\n"),
               "b": (0, "usage: run.py [-h]\n", "warning: ignored\n")}
    def fake_sh(args, timeout=1800):
        rc, out, err = next(v for k, v in answers.items() if args[-1].endswith(k))
        return {"returncode": rc, "stdout": out, "stderr": err}
    cb._sh = fake_sh
    res = cb.validate_in_image("img@sha256:d", ["a", "b"], probe_tools=[])
    assert res["checks"]["a"] == {"rc": 2, "out": "python: can't open file '/opt/gab/run.py': [Errno 2] No such file or directory"}
    assert res["checks"]["b"] == {"rc": 0, "out": "usage: run.py [-h]"}


# ---------------------------------------------------------------------------
# synth_build: what it refuses before it fetches anything
# ---------------------------------------------------------------------------

def test_package_manager_lines_are_found_by_their_command_word():
    found = synthesis.package_manager_lines([
        {"command": "git clone https://x /opt/t"},
        {"command": "conda install numba"},
        {"command": "cd /opt/t && python3 -m pip install -e ."},
        {"command": "DEBIAN_FRONTEND=noninteractive apt-get install -y git; sudo pip3 install x"},
        {"command": "/opt/conda/bin/mamba create -n x"},
        {"command": "make && make install"},
        {"command": "python setup.py build"},
    ])
    assert [(o["manager"], o["command"][:12]) for o in found] == [
        ("conda", "conda instal"), ("pip", "cd /opt/t &&"), ("pip3", "DEBIAN_FRONTE"[:12]), ("mamba", "/opt/conda/b")]
    assert synthesis.package_manager_lines([]) == [] and synthesis.package_manager_lines([{"source": "x"}]) == []


@pytest.fixture
def no_fetch(monkeypatch):
    from agent.mcp_tools import env_tools
    monkeypatch.setattr(env_tools, "_workflow_gate_or_none", lambda *a, **k: None)
    def _must_not_fetch(*a, **k):
        raise AssertionError("synth_build fetched before its preflight refused")
    monkeypatch.setattr(env_tools._ms._env_mgr, "fetch_build_source", _must_not_fetch)
    return env_tools


def test_synth_build_without_a_draft_is_refused_before_any_fetch(no_fetch):
    r = no_fetch.synth_build(env_name="x", repo_url="https://github.com/a/b", tool_name="b",
                             commands=[{"command": "make", "source": "agent_authored"}])
    assert r["outcome"] == "refused" and r["code"] == "install.synth_no_draft" and r["stage"] == "draft"
    assert "start_pipeline" in r["error"] and "pipeline_id" in r["error"]


def test_synth_build_refuses_package_manager_lines_with_the_declaration_named(no_fetch):
    r = no_fetch.synth_build(env_name="x", repo_url="https://github.com/a/b", tool_name="b", pipeline_id="p",
                             commands=[{"command": "git clone https://github.com/a/b /opt/b", "source": "agent_authored"},
                                       {"command": "conda install numba", "source": "extracted", "origin_file": "README.md"},
                                       {"command": "pip install -e /opt/b", "source": "agent_authored"}])
    assert r["outcome"] == "refused" and r["code"] == "install.synth_package_manager_line"
    assert [o["manager"] for o in r["commands"]] == ["conda", "pip"]
    assert "install_conda_packages" in r["error"] and "install_pip_package" in r["error"]


# ---------------------------------------------------------------------------
# nothing of ours is written into the corpus
# ---------------------------------------------------------------------------

def test_stage_authored_artifact_refuses_a_path_under_the_resources_corpus(monkeypatch, tmp_path):
    from agent.mcp_tools import workflow_tools as wt
    corpus = tmp_path / "resources"
    (corpus / "ds").mkdir(parents=True)
    sample = corpus / "ds" / "samples.csv"
    sample.write_text("a,b\n")
    monkeypatch.setattr(wt._workspace, "resources_root", lambda: corpus)
    before = sample.read_bytes(), sample.stat().st_mtime_ns
    r = wt.stage_authored_artifact("p", str(sample), "samplesheet", "the sheet", content="a,b\n")
    assert r["outcome"] == "refused" and r["code"] == "stage_artifact.path_in_resources"
    assert "select_test_data" in r["error"] and (sample.read_bytes(), sample.stat().st_mtime_ns) == before
    r = wt.stage_authored_artifact("p", str(sample), "samplesheet", "the sheet", generated_by="cat x")
    assert r["code"] == "stage_artifact.path_in_resources"
    r = wt.stage_authored_artifact("p", str(corpus / "new.txt"), "note", "a note", content="x")
    assert r["code"] == "stage_artifact.path_in_resources" and not (corpus / "new.txt").exists()
