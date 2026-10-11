"""The authors'-recipe build names the Dockerfile it builds, or says which ones exist.

A repository keeps its Dockerfile where it likes (`docker/Dockerfile`), and a caller who
leaves `recipe` empty gets the checkout's one recipe; a checkout with several, or a
`recipe` that is not there, is refused with the list, before any build runs. A build that
dies of memory says so with the Docker VM's size, so the remedy is read, not guessed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from freeze_fixtures import FakeCache, mock_docker

from agent.skills import container_build as CB
from agent.skills import freeze_from_image as F

_DF = "FROM debian:bookworm-slim\nRUN apt-get update && apt-get install -y curl\n"


def _mock_build(monkeypatch, files: dict[str, str], build_rc: int = 0, build_err: str = ""):
    """`git clone` materialises `files` into the checkout; `docker buildx build` answers
    `build_rc`. Returns the argv list of every shell call."""
    calls: list[list[str]] = []

    def fake_sh(argv, timeout=300, **kw):
        calls.append(list(argv))
        if argv[:2] == ["git", "clone"]:
            root = Path(argv[-1])
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            (root / ".git" / "Dockerfile").parent.mkdir(parents=True, exist_ok=True)
            (root / ".git" / "Dockerfile").write_text("not a recipe: lives under .git")
            return {"rc": 0, "out": "", "err": ""}
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return {"rc": 0, "out": "c" * 40 + "\n", "err": ""}
        if argv[:3] == ["docker", "buildx", "build"]:
            return {"rc": build_rc, "out": "", "err": build_err}
        return {"rc": 1, "out": "", "err": "not mocked"}
    monkeypatch.setattr(F, "_sh", fake_sh)
    return calls


def _build(tmp_path, monkeypatch, files, recipe="", **kw):
    mock_docker(monkeypatch)
    calls = _mock_build(monkeypatch, files, **kw)
    cache = FakeCache()
    out = F.build_from_authors_recipe(repo="org/tool", ref="v1", recipe=recipe, name="tool", version="1",
                                      tools=[{"name": "tool", "evidence": "tool --help"}],
                                      env_cache=cache, env_dir=tmp_path)
    built = [c for c in calls if c[:3] == ["docker", "buildx", "build"]]
    return out, cache, built


def _dockerfile_arg(call: list[str]) -> str:
    return call[call.index("-f") + 1]


def test_the_single_recipe_below_the_root_is_built_and_recorded(tmp_path, monkeypatch):
    out, cache, built = _build(tmp_path, monkeypatch, {"docker/Dockerfile": _DF, "README.md": "x"})
    assert out["outcome"] == "proven", out
    assert len(built) == 1 and _dockerfile_arg(built[0]).endswith("/docker/Dockerfile")
    assert cache.registered[out["request_key"]]["dockerfile_source"]["recipe_path"] == "docker/Dockerfile"


def test_a_root_dockerfile_wins_over_variants_elsewhere(tmp_path, monkeypatch):
    out, cache, built = _build(tmp_path, monkeypatch, {"Dockerfile": _DF, "docker/CPG_Dockerfile": _DF,
                                                        "tests/Dockerfile": _DF})
    assert out["outcome"] == "proven", out
    assert _dockerfile_arg(built[0]).endswith("/Dockerfile") and "/docker/" not in _dockerfile_arg(built[0])
    assert cache.registered[out["request_key"]]["dockerfile_source"]["recipe_path"] == "Dockerfile"


def test_several_recipes_and_none_at_the_root_refuse_naming_them_before_any_build(tmp_path, monkeypatch):
    out, _, built = _build(tmp_path, monkeypatch, {"docker/Dockerfile": _DF, "docker/CPG_Dockerfile": _DF})
    assert (out["outcome"], out["code"]) == ("refused", "authors_recipe.recipe_ambiguous")
    assert out["candidates"] == ["docker/Dockerfile", "docker/CPG_Dockerfile"]
    assert "docker/Dockerfile, docker/CPG_Dockerfile" in out["error"] and "recipe=" in out["remedy"]
    assert built == [], "nothing is built until a recipe is named"


def test_a_named_recipe_that_is_not_there_is_refused_with_what_is(tmp_path, monkeypatch):
    out, _, built = _build(tmp_path, monkeypatch, {"docker/Dockerfile": _DF}, recipe="Dockerfile")
    assert (out["outcome"], out["code"]) == ("refused", "authors_recipe.recipe_not_found")
    assert "'Dockerfile' is not a file" in out["error"] and "docker/Dockerfile" in out["error"]
    assert out["candidates"] == ["docker/Dockerfile"] and built == []


def test_a_repository_with_no_recipe_at_all_says_so(tmp_path, monkeypatch):
    out, _, built = _build(tmp_path, monkeypatch, {"setup.py": "", "environment.yml": "name: x"})
    assert (out["outcome"], out["code"]) == ("refused", "authors_recipe.recipe_not_found")
    assert out["candidates"] == [] and "no Dockerfile, Containerfile or .def file" in out["error"]
    assert "another tier" in out["remedy"] and built == []


def test_a_named_recipe_that_exists_is_built_as_named(tmp_path, monkeypatch):
    out, _, built = _build(tmp_path, monkeypatch, {"docker/Dockerfile": _DF, "docker/CPG_Dockerfile": _DF},
                           recipe="docker/CPG_Dockerfile")
    assert out["outcome"] == "proven", out
    assert _dockerfile_arg(built[0]).endswith("/docker/CPG_Dockerfile")


_OOM = ("ERROR: failed to build: failed to solve: ResourceExhausted: process \"/bin/sh -c conda env create "
        "-f /environment.yml\" did not complete successfully: cannot allocate memory")


def test_a_build_that_dies_of_memory_names_the_cause_and_the_vm_size(tmp_path, monkeypatch):
    monkeypatch.setattr(CB, "docker_memory_gib", lambda: 7.6)
    out, _, _ = _build(tmp_path, monkeypatch, {"Dockerfile": _DF}, build_rc=1, build_err=_OOM)
    assert (out["outcome"], out["code"]) == ("broke", "authors_recipe.build_failed")
    assert out["cause"] == "out_of_memory"
    assert "7.6 GiB" in out["hint"] and "memory limit" in out["hint"]
    assert "cannot allocate memory" in out["error"]


def test_a_build_that_fails_for_another_reason_carries_no_memory_hint(tmp_path, monkeypatch):
    out, _, _ = _build(tmp_path, monkeypatch, {"Dockerfile": _DF}, build_rc=1,
                       build_err="error: git: command not found")
    assert out["code"] == "authors_recipe.build_failed" and "hint" not in out and "cause" not in out


@pytest.mark.parametrize("stderr, expect", [
    ("process did not complete successfully: exit code: 137", "out_of_memory"),
    ("Killed", "out_of_memory"),
    ("npm ERR! code ELIFECYCLE", None),
])
def test_build_failure_fields_reads_the_stderr(monkeypatch, stderr, expect):
    monkeypatch.setattr(CB, "docker_memory_gib", lambda: None)
    fields = CB.build_failure_fields(stderr)
    assert fields.get("cause") == expect
    if expect:
        assert "an unknown amount of memory" in fields["hint"]
