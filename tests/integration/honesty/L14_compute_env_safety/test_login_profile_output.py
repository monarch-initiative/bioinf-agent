"""
L14 cheat-guards — a login profile's output never reads as a check's answer.

Every remote check that needs the cluster's tools runs in a login shell
(`snapshot._login_shell`), which reads the user's profile first. A profile that
prints without a final newline would glue its text onto the first line the check
prints, and a check that reads its answer line by line ("EXISTS", "MISSING:<path>",
a scheduler row) would miss it there: a missing input read as present, a staged
image read as absent. Each check's output starts on a line of its own.

The checks run through a real login bash here, under a profile that prints.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from agent.skills import cluster_jobs, run_production, stage_apptainer

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
def test_every_login_shell_check_goes_through_the_one_wrapper():
    """A check that spells its own `bash -lc` skips the line the wrapper starts it on."""
    own = [f"{p.relative_to(_AGENT.parent)}:{n}"
           for p in sorted(_AGENT.rglob("*.py")) if p.name != "snapshot.py"
           for n, line in enumerate(p.read_text().splitlines(), 1)
           if re.search(r"""["']bash -lc\b""", line)]
    assert not own, own
