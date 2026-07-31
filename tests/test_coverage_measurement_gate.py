"""`measure_terminal_coverage.py` must refuse to publish numbers from a suite that
never ran.

The script tolerates pytest rc=1 on purpose — it regenerates the very overlay that
`test_coverage_overlay_keys_resolve_against_the_ledger` reads, so that test is always
stale during the run. Its only other guard was "did coverage produce a datafile",
and that guard cannot see the failure mode that matters: `coverage run` creates the
datafile as soon as the process starts, so a conftest that fails to import exits 4
WITH a datafile present. The script then measured a run in which zero tests executed
and wrote `docs/terminal_coverage.json` + `docs/outcomes_dashboard.html`, both
committed — publishing "everything is dark" as a finding rather than a broken run.

Measured, not reasoned: the rc=4 + datafile-present combination below is reproduced
here rather than asserted, because that combination is the whole reason the old
guard was insufficient.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "measure_terminal_coverage.py"


def test_a_collection_error_still_leaves_a_coverage_datafile(tmp_path):
    """The premise of the gate, reproduced — this is why 'datafile exists' is not
    evidence that anything ran.

    `coverage` is a developer tool, not a runtime dependency, so it is absent in CI.
    Skip rather than fail: the three tests below read the gate off the source and DO
    run everywhere, so the gate itself stays covered. (Written without this guard, it
    failed CI immediately — a test that assumes a dev-only dependency.)
    """
    pytest.importorskip("coverage",
                        reason="coverage is a dev tool, not installed in CI; the "
                               "source-level gate tests below still run here")
    (tmp_path / "conftest.py").write_text("import a_module_that_does_not_exist\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def test_x(): assert True\n")
    datafile = tmp_path / ".cov"

    r = subprocess.run(
        [sys.executable, "-m", "coverage", "run", f"--data-file={datafile}",
         "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True)

    assert r.returncode == 4, r.stdout[-2000:]
    assert datafile.exists(), (
        "if this ever stops holding the gate is belt-and-braces rather than "
        "load-bearing — but do not remove it on that basis alone")


def test_the_script_refuses_every_rc_that_means_the_suite_did_not_run():
    """rc=1 is tolerated; 2/3/4/5 must abort BEFORE anything is written.

    Read off the source rather than executed: running the real script takes ~4
    minutes and shells out to the whole suite. What is pinned is the decision — the
    tolerated set — because that is the thing that was wrong.
    """
    src = SCRIPT.read_text()
    m = re.search(r"if r\.returncode not in \(([^)]*)\):", src)
    assert m, "the exit-code gate is gone from measure_terminal_coverage.py"
    tolerated = {int(x) for x in re.findall(r"\d+", m.group(1))}
    assert tolerated == {0, 1}, (
        f"tolerated pytest exit codes are {sorted(tolerated)}; only 0 (clean) and "
        f"1 (tests failed — expected, the overlay is stale mid-run) may pass. "
        f"2=interrupted 3=internal 4=collection-failed 5=no-tests-collected all "
        f"mean the numbers would describe nothing.")
    # ...and it must exit, not merely warn.
    after = src[m.end():m.end() + 800]
    assert "sys.exit(" in after, "the gate warns but does not abort"


def test_the_gate_sits_before_the_artifacts_are_written():
    """A check that runs after the write is not a gate.

    Anchored on the WRITE CALL, not on a mention of the filename — the first two
    occurrences of `terminal_coverage.json` in this file are the module docstring
    and the gate's own comment, so a naive substring search puts the gate 'after'
    a write that is really prose. (That is how this test failed when first written.)
    """
    src = SCRIPT.read_text()
    gate = src.index("if r.returncode not in (")
    writes = [m.start() for m in re.finditer(r"\.write_text\(", src)]
    assert writes, "expected the script to write its artifacts with write_text"
    assert gate < min(writes), "the exit-code gate must precede every artifact write"


@pytest.mark.parametrize("rc", [2, 3, 4, 5])
def test_every_refused_code_is_explained_to_whoever_hits_it(rc):
    """A refusal that does not say which failure it saw sends the reader to the
    source. Each refused code carries its meaning in the message table."""
    src = SCRIPT.read_text()
    table = re.search(r"_PYTEST_RC = \{(.*?)\}", src, re.S)
    assert table, "the exit-code explanation table is gone"
    assert re.search(rf"\b{rc}:", table.group(1)), f"rc={rc} is refused but unexplained"
