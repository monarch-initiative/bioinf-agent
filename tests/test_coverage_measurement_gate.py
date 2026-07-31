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

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "measure_terminal_coverage.py"


def _script():
    """The script as a module, so the gate can be CALLED.

    Three tests here used to regex this file's source for
    `if r.returncode not in (0, 1):`. Pulling that tuple into a named constant — the
    same logic, spelled better — broke two of them. Tests that punish an improvement
    are how a codebase gets stiff, so the gate became a function
    (`refuse_reason_for_pytest_rc`) and these call it.

    scripts/ is not a package, hence the spec loader rather than an import.
    """
    spec = importlib.util.spec_from_file_location("_mtc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


@pytest.mark.parametrize("rc", [0, 1])
def test_the_codes_that_mean_the_suite_ran_are_allowed_to_publish(rc):
    """0 is clean. 1 means tests failed, which is EXPECTED here — the script
    regenerates the overlay its own ledger test reads, so that test is stale for the
    duration of the run. Both must proceed, or the measurement can never be taken."""
    assert _script().refuse_reason_for_pytest_rc(rc) == "", (
        f"rc={rc} means the suite ran; refusing it makes the script unrunnable")


@pytest.mark.parametrize("rc", [2, 3, 4, 5])
def test_every_code_that_means_the_suite_did_not_run_is_refused_and_explained(rc):
    """2=interrupted 3=internal 4=collection-failed 5=no-tests-collected. Each must
    refuse, and the refusal must name what happened — a bare "aborting" sends the
    reader to the source to find out which of four failures they hit."""
    mod = _script()
    reason = mod.refuse_reason_for_pytest_rc(rc)
    assert reason, f"rc={rc} means zero tests ran; publishing numbers from it is a lie"
    assert str(rc) in reason, f"the refusal does not say which code it saw: {reason!r}"
    assert mod.PYTEST_RC_MEANING[rc].split()[0] in reason, (
        f"rc={rc} is refused but its meaning is not in the message: {reason!r}")


def test_the_coverage_run_disables_parallelism():
    """The one failure mode the exit-code gate CANNOT see.

    pytest.ini sets `addopts = -n auto`. Under xdist the tests run in worker
    subprocesses, and `coverage run` only traces the process it started — so it would
    record almost no executed lines while pytest still exits 0. rc=0 is the honest
    answer to "did the suite run" (it did), so the gate above waves it through, and the
    script publishes a total blackout as a measurement.

    Caught in practice: adding `-n auto` to pytest.ini turned this script into a
    generator of exactly the false finding it was hardened against last round.
    """
    mod = _script()
    assert "-n0" in mod.NO_XDIST or "-p" in mod.NO_XDIST, (
        f"NO_XDIST={mod.NO_XDIST!r} no longer disables parallelism")
    src = SCRIPT.read_text()
    assert "*NO_XDIST" in src or "NO_XDIST" in src.split("def _run_suite_under_coverage")[1][:600], (
        "NO_XDIST is defined but the coverage subprocess does not use it")


def test_the_gate_is_consulted_before_any_artifact_is_written():
    """A check that runs after the write is not a gate.

    This one stays source-anchored — "does the call site come before the writes" is a
    fact about ORDER IN THE FILE, and there is no way to ask it of a running function
    without executing the real script, which shells out to the whole suite for ~4
    minutes. Anchored on the function name and on `.write_text(` rather than on a
    filename: the first mentions of terminal_coverage.json in that file are prose, and
    matching those put the gate 'after' a write that was really a docstring.
    """
    assert _gate_call_offset() < min(_write_offsets()), \
        "the exit-code gate must precede every artifact write"


def test_the_gate_aborts_rather_than_warning():
    """A warning printed into a long run scrolls past; the artifacts still get written.
    Pinned at the call site for the same order-in-file reason as above."""
    src = SCRIPT.read_text()
    after = src[_gate_call_offset():][:600]
    assert "sys.exit(" in after, "the gate warns but does not abort"


_GATE_CALL = "refuse_reason_for_pytest_rc(r.returncode)"


def _gate_call_offset() -> int:
    """Where the gate is CONSULTED. A bare str.index here raises ValueError, which
    fails the test but reports 'substring not found' instead of what went wrong."""
    src = SCRIPT.read_text()
    at = src.find(_GATE_CALL)
    assert at != -1, (
        f"{SCRIPT.name} never calls {_GATE_CALL} — the gate function may still exist "
        f"and pass its own tests while nothing consults it. Publishing coverage numbers "
        f"from a suite that did not run is the failure this guards.")
    return at


def _write_offsets() -> list[int]:
    src = SCRIPT.read_text()
    writes = [m.start() for m in re.finditer(r"\.write_text\(", src)]
    assert writes, "expected the script to write its artifacts with write_text"
    return writes
