"""The setup surface answers "where is conda?" with ONE fixed path and NO search.

WHY THIS FILE EXISTS. Four scripts each carried their own hand-maintained list of
conda locations, and two had DIVERGED on order — the systems check could report
PASS for a conda that setup never used. The first fix unified the search into
scripts/_env.sh; the second (README review, 2026-09-21) DELETED the search: THE
conda is the repo-private ./.miniforge, installed unconditionally by setup.sh,
because a machine's own conda carries that machine's variance and every clone of
this system is meant to bootstrap identically. So the lint now refuses a conda
SEARCH anywhere in scripts/ — _env.sh included — and the resolution itself is
DRIVEN: miniforge-or-nothing, whatever condas the environment dangles.

The failure mode guarded is not malice but convenience: the next person who needs
a conda path will "helpfully" fall back to PATH, and nothing else in the build
would notice that one machine now resolves differently from every other.

SCOPE: scripts/ only. agent/ resolves conda through shutil.which at runtime (PATH
is set up by the launcher, which puts ./.miniforge there), a different mechanism
and deliberately not policed here.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
ENV_SH = SCRIPTS / "_env.sh"

#: Substrings that only ever appear in a hand-rolled conda/interpreter SEARCH list.
#: Deliberately not ".miniforge" (the repo-local install TARGET, which setup.sh must
#: name to create it) and not "Miniforge3-" (the installer download URL).
_SEARCH_TOKENS = (
    "miniconda3",
    "anaconda3",
    "/opt/conda",
    "/opt/homebrew/opt/miniforge3",
    "$HOME/miniforge3",
)

#: Constructing the runtime interpreter's path, as opposed to merely naming
#: ".conda_runtime" in a message to the user (which any script may do).
_INTERPRETER_PATH = ("conda_runtime/bin", '".conda_runtime" / "bin"')


def _surface_files() -> list[Path]:
    # _env.sh is NOT excluded: since the search was deleted (2026-09-21) the
    # resolver itself may not hold a search list either.
    return sorted(p for p in SCRIPTS.iterdir()
                  if p.is_file() and p.suffix in {".sh", ".py"})


def _drive_find_conda(tmp_path, *, with_miniforge: bool) -> subprocess.CompletedProcess:
    """Run bioinf_find_conda from a COPY of _env.sh rooted in a temp repo, inside
    an environment that dangles every kind of machine conda — $CONDA_EXE, one on
    PATH, one at ~/miniforge3. The only thing allowed to matter is whether the
    temp repo's own ./.miniforge exists."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "_env.sh").write_text(ENV_SH.read_text())
    home = tmp_path / "home"
    for fake in (tmp_path / "onpath" / "conda",
                 tmp_path / "condaexe" / "conda",
                 home / "miniforge3" / "condabin" / "conda"):
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text("#!/bin/sh\necho fake\n")
        fake.chmod(0o755)
    if with_miniforge:
        private = repo / ".miniforge" / "condabin" / "conda"
        private.parent.mkdir(parents=True)
        private.write_text("#!/bin/sh\necho private\n")
        private.chmod(0o755)
    return subprocess.run(
        ["bash", "-c", f'source "{repo}/scripts/_env.sh" && bioinf_find_conda'],
        capture_output=True, text=True, timeout=30,
        env={"HOME": str(home),
             "PATH": f"{tmp_path / 'onpath'}:{os.environ['PATH']}",
             "CONDA_EXE": str(tmp_path / "condaexe" / "conda")})


def test_find_conda_is_miniforge_or_nothing(tmp_path):
    """The driven half of the no-search rule: with machine condas on PATH, in
    $CONDA_EXE and at ~/miniforge3, resolution still FAILS when the repo has no
    ./.miniforge — and returns exactly ./.miniforge when it does."""
    r = _drive_find_conda(tmp_path, with_miniforge=False)
    assert r.returncode != 0 and not r.stdout.strip(), (
        f"a machine conda was resolved despite no ./.miniforge: {r.stdout!r}")

    r = _drive_find_conda(tmp_path, with_miniforge=True)
    assert r.returncode == 0
    assert r.stdout.strip() == str(tmp_path / "repo" / ".miniforge" / "condabin" / "conda")


@pytest.mark.parametrize("path", _surface_files(), ids=lambda p: p.name)
def test_no_second_conda_search_list_in_the_setup_surface(path):
    text = path.read_text()
    found = [t for t in _SEARCH_TOKENS if t in text]
    assert not found, (
        f"{path.name} spells out conda search locations {found}.\n\n"
        f"That is a SECOND answer to a question scripts/_env.sh already answers, and "
        f"the last time two answers existed they disagreed on order — the doctor "
        f"reported PASS on a conda setup had not used.\n"
        f"Fix: shell — `source \"$(dirname \"${{BASH_SOURCE[0]}}\")/_env.sh\"` then call "
        f"bioinf_find_conda / bioinf_conda_on_path. Python — shell out to "
        f"`bash scripts/_env.sh conda`, the way doctor.py does.")


@pytest.mark.parametrize("path", _surface_files(), ids=lambda p: p.name)
def test_the_runtime_interpreter_path_is_not_respelled(path):
    """`.conda_runtime/bin/python` is a literal any script could retype, and a rename
    would then leave one caller pointed at nothing. doctor.py is allowed exactly one —
    its documented fallback for a checkout where _env.sh itself is missing."""
    text = path.read_text()
    hits = sum(text.count(t) for t in _INTERPRETER_PATH)
    allowed = 1 if path.name == "doctor.py" else 0
    assert hits <= allowed, (
        f"{path.name} constructs the runtime interpreter path {hits} time(s) "
        f"(allowed: {allowed}). Use $BIOINF_RUNTIME_PY after sourcing _env.sh, or ask "
        f"`bash scripts/_env.sh runtime-python` — it prints the path whether or not the "
        f"env exists, so a missing runtime can still be named.")


@pytest.mark.parametrize(
    "path", [p for p in _surface_files() if p.suffix == ".sh"], ids=lambda p: p.name)
def test_every_conda_using_shell_script_sources_the_resolver(path):
    text = path.read_text()
    if "conda" not in text:
        pytest.skip(f"{path.name} does not touch conda")
    assert "_env.sh" in text, (
        f"{path.name} uses conda but never sources scripts/_env.sh, so it is resolving "
        f"conda some other way. Add: "
        f"source \"$(cd \"$(dirname \"${{BASH_SOURCE[0]}}\")\" && pwd)/_env.sh\"")


def test_the_doctor_delegates_rather_than_searching():
    """The doctor is the row a user reads to decide the machine is healthy, so it is
    the one place a divergent answer does the most damage."""
    text = (SCRIPTS / "doctor.py").read_text()
    assert "_env.sh" in text and "conda" in text, (
        "doctor.py must ask scripts/_env.sh for conda, not carry its own search")
