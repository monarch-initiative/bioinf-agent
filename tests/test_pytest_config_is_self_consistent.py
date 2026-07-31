"""pytest.ini makes two promises that break silently if the rest of the repo drifts.

Both were live traps while this config was being written, and neither shows up as a
test failure — one kills the suite before collection, the other lets it pass while a
guarantee quietly stops holding.
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INI = REPO / "pytest.ini"
DEV_REQS = REPO / "requirements-dev.txt"


def _addopts() -> str:
    cp = configparser.ConfigParser()
    cp.read(INI)
    return cp.get("pytest", "addopts", fallback="")


def test_parallel_addopts_have_their_plugin_declared():
    """`addopts = -n auto` without pytest-xdist installed is not a slow suite, it is NO
    suite: pytest exits on `unrecognized arguments: -n` before collecting anything.

    Reproduced before writing this — `pytest -p no:xdist` on this repo dies exactly
    that way. It would have been red on the first CI run of the branch that introduced
    the flag, because the plugin was installed locally and declared nowhere.
    """
    if "-n" not in _addopts():
        return                          # no parallel default; nothing to require
    assert re.search(r"^\s*pytest-xdist\b", DEV_REQS.read_text(), re.M), (
        "pytest.ini passes -n but requirements-dev.txt does not list pytest-xdist. "
        "A fresh clone and CI both die before collecting a single test.")


def test_xdist_group_markers_are_actually_binding():
    """`@pytest.mark.xdist_group` is INERT unless the run uses `--dist loadgroup`. There
    is no warning: the marker is accepted, ignored, and the grouped tests scatter.

    That matters here for one specific pair. The two tests in
    tests/integration/correctness/test_n5_reaper_not_on_import.py share the real
    /tmp/bioinf_services — the first spawns a cold subprocess that cannot see a
    monkeypatch, the second calls a reaper that walks the whole directory. Split across
    workers, the second deletes the first's file and the first fails with "reaper ran on
    module import", accusing the code of the exact defect that file exists to catch.

    So dropping loadgroup does not produce a slower suite or an honest flake. It
    produces a confident, plausible, false bug report.
    """
    grouped = [p for p in (REPO / "tests").rglob("*.py")
               if "xdist_group" in p.read_text() and p.name != Path(__file__).name]
    if not grouped:
        return
    assert "--dist loadgroup" in _addopts(), (
        f"{[p.name for p in grouped]} use @pytest.mark.xdist_group, but pytest.ini "
        f"does not pass --dist loadgroup, so the marker does nothing and those tests "
        f"can land on different workers.")
