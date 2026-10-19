"""
L14 cheat-guards — a login profile's output never reads as a check's answer.

Every remote check that needs the cluster's tools runs in a login shell
(`snapshot._login_shell`), which reads the user's profile first. A profile that
prints without a final newline would glue its text onto the first line the check
prints, and a check that reads its answer line by line ("EXISTS", "MISSING:<path>",
a scheduler row) would miss it there: a missing input read as present, a staged
image read as absent. Each check's output starts on a line of its own, and a check
that would take any word as its answer (the module listing) reads only what follows
its own marker.

The checks run through a real login bash here, under a profile that prints.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from agent.skills import cluster_jobs, cluster_modules, run_production, stage_apptainer

_REAL_RUN = subprocess.run
_AGENT = Path(__file__).resolve().parents[4] / "agent"


@pytest.fixture
def chatty_profile(tmp_path, monkeypatch):
    """ssh runs its remote command through bash on this machine, under a home whose
    login profile prints a banner with no final newline."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_profile").write_text("printf 'Welcome to the cluster'\n")
    env = {**os.environ, "HOME": str(home)}

    def fake_run(argv, *a, **kw):
        assert argv[0] == "ssh", argv
        return _REAL_RUN(["bash", "-c", argv[-1]], *a, env=env, **kw)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return tmp_path


def _env() -> dict:
    return {"name": "hpc", "type": "ssh", "host": "hpc.example.invalid", "user": "u"}


@pytest.mark.integration
def test_the_profile_glues_its_text_onto_a_bare_check(chatty_profile):
    out = subprocess.run(["ssh", "-n", "hpc", "bash -lc 'echo EXISTS'"],
                         capture_output=True, text=True).stdout
    assert out.splitlines() == ["Welcome to the clusterEXISTS"]


@pytest.mark.integration
def test_a_staged_image_reads_as_there(chatty_profile):
    sif = chatty_profile / "samtools.sif"
    sif.write_bytes(b"sif")
    assert stage_apptainer._remote_sif_exists(_env(), str(sif)) is True


@pytest.mark.integration
def test_a_missing_input_reads_as_missing(chatty_profile):
    gone = str(chatty_profile / "reads.fq.gz")
    out = cluster_jobs.remote_paths_exist(_env(), [gone], timeout=60)
    assert (out["code"], out["missing_paths"]) == ("cluster.inputs_missing", [gone]), out


@pytest.mark.integration
def test_a_missing_index_family_reads_as_missing(chatty_profile):
    gone = str(chatty_profile / "ref.fa")
    assert run_production._remote_prefixes_exist(_env(), [gone], timeout=60)["missing"] == [gone]


@pytest.mark.integration
def test_a_profile_banner_is_never_listed_as_a_module(chatty_profile):
    (chatty_profile / "home" / ".bash_profile").write_text(
        "echo 'Welcome to the cluster. Default: python/3.11'\n"
        "module() { echo '   samtools/1.21    nextflow/25.04.7 (D)' >&2; }\n"
        "printf 'Quota: 80%% used'\n")
    access = chatty_profile / "projects_access.yaml"
    access.write_text(yaml.safe_dump({
        "compute_envs": [_env()],
        "projects": [{"name": "demo_project", "compute_envs": ["hpc"], "directories": []}]}))
    out = cluster_modules.cluster_module_avail("demo_project", "hpc", access_path=str(access))
    assert out["modules"] == ["samtools/1.21", "nextflow/25.04.7"], out


#: A login shell however it is spelled: `bash -lc`, `/bin/bash -l -c`, `bash --login -c`.
_LOGIN_SHELL = re.compile(r"\bbash\s+(?:-[A-Za-z]*l[A-Za-z]*|--login)\b")


def _spells_login_shell(node: ast.AST) -> bool:
    """A string in the code that starts a login shell, or an argv's own `-lc`."""
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and (bool(_LOGIN_SHELL.search(node.value)) or node.value.strip() in ("-lc", "--login")))


@pytest.mark.integration
def test_every_login_shell_check_goes_through_the_one_wrapper():
    """A check that spells its own login shell skips the line the wrapper starts it on.
    Every string in the code is read (docstrings aside), and an argv's own `-lc` too."""
    own = []
    for p in sorted(_AGENT.rglob("*.py")):
        tree = ast.parse(p.read_text())
        skip = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)}
        skip |= {id(c) for f in ast.walk(tree)
                 if isinstance(f, ast.FunctionDef) and p.name == "snapshot.py" and f.name == "_login_shell"
                 for c in ast.walk(f)}
        own += [f"{p.relative_to(_AGENT.parent)}:{n.lineno}" for n in ast.walk(tree)
                if id(n) not in skip and _spells_login_shell(n)]
    assert not own, own


@pytest.mark.integration
@pytest.mark.parametrize("spelling", ['f"bash -lc {q}"', '"/bin/bash -lc " + q', '"bash -l -c " + q',
                                      '"bash --login -c " + q', '["bash", "-lc", q]'])
def test_the_scan_sees_each_spelling(spelling):
    assert any(map(_spells_login_shell, ast.walk(ast.parse(f"def check(q):\n    return {spelling}\n"))))
