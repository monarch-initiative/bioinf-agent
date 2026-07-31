"""
A cluster job that DIED used to return `proven`.

The poller read sacct's State column only to decide whether the job was over,
and then took the exit code as the verdict:

    rc = _parse_exit_code(final_status.get("exit_code", ""))   # "0:9" -> 0

SLURM reports a signal death as `<rc>:<signal>` with rc ZERO, so every job the
SCHEDULER killed — as opposed to one whose tool exited non-zero — arrived at
that line looking exactly like a clean run. Measured against the repo's own
parsers before the fix, six of eight death shapes took the success path:

    State          ExitCode   verdict
    COMPLETED      0:0        SUCCESS PATH   (correct)
    FAILED         1:0        failure        (correct — the only one caught)
    FAILED         0:9        SUCCESS PATH   <-- SIGKILL, e.g. a cgroup OOM
    OUT_OF_MEMORY  0:125      SUCCESS PATH   <-- the accounted OOM
    CANCELLED      0:15       SUCCESS PATH   <-- scancel
    TIMEOUT        0:0        SUCCESS PATH   <-- wall-clock kill
    NODE_FAIL      0:0        SUCCESS PATH   <-- the node died under it
    PREEMPTED      0:0        SUCCESS PATH   <-- preempted

WHY IT MATTERS, precisely. A dead job whose outputs are ABSENT was already
caught downstream: the download loop errors, `detected_outputs` lands empty,
and I3 refuses the seal. The hole is the job killed MID-WRITE — a TIMEOUT
during a BAM write leaves a file that exists, is non-empty, and validates. That
is what sealed green, and no invariant looks at the job that produced a cluster
step, so nothing else was going to catch it.

There is a second, quieter bug in the same three lines: sacct writes
`CANCELLED by 12345`, and the membership test was against the bare name, so a
user-initiated cancel never read as terminal at all and polled to the timeout.
"""
from __future__ import annotations

import pytest

from agent.skills import cluster_jobs as cj


def _row(state: str, exit_code: str = "0:0", reason: str = "") -> dict:
    return {"job_id": "58025583", "state": state, "elapsed": "00:10:00",
            "exit_code": exit_code, "nodelist": "c-1", "reason": reason,
            "start": "", "end": ""}


# ---------------------------------------------------------------------------
# The verdict is the State's to give.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state,exit_code", [
    ("FAILED",        "0:9"),     # SIGKILL — cgroup OOM on a cluster with no OOM accounting
    ("OUT_OF_MEMORY", "0:125"),   # the accounted OOM
    ("CANCELLED",     "0:15"),    # scancel
    ("TIMEOUT",       "0:0"),     # wall-clock kill
    ("NODE_FAIL",     "0:0"),
    ("PREEMPTED",     "0:0"),
    ("BOOT_FAIL",     "0:0"),
    ("DEADLINE",      "0:0"),
    ("REVOKED",       "0:0"),
])
def test_a_scheduler_killed_job_is_a_death_however_zero_its_exit_code(state, exit_code):
    """Every one of these reached the success path before. The rc is zero in all
    of them, which is exactly why the rc cannot be the verdict."""
    verdict, why = cj.classify_sacct_row(_row(state, exit_code))
    assert verdict == cj.DIED, f"{state} {exit_code} must not read as success"
    assert why and state.lower().replace("_", " ") or why, "a death must say why"


def test_the_one_death_that_was_already_caught_is_still_caught():
    assert cj.classify_sacct_row(_row("FAILED", "1:0"))[0] == cj.DIED


def test_a_clean_run_is_still_a_clean_run():
    assert cj.classify_sacct_row(_row("COMPLETED", "0:0"))[0] == cj.SUCCEEDED


def test_an_unrecorded_exit_code_on_a_completed_job_is_success():
    """sacct can return an empty ExitCode for a job it accounts as COMPLETED.
    Treating that as a death would refuse real, finished work — the conservatism
    rule cuts both ways."""
    assert cj.classify_sacct_row(_row("COMPLETED", ""))[0] == cj.SUCCEEDED


def test_completed_with_a_nonzero_exit_code_is_a_contradiction_not_a_success():
    """The scheduler says clean, the exit code says otherwise. Success is not the
    safe reading of a contradiction."""
    verdict, why = cj.classify_sacct_row(_row("COMPLETED", "1:0"))
    assert verdict == cj.DIED
    assert "contradiction" in why


@pytest.mark.parametrize("state", ["RUNNING", "PENDING", "CONFIGURING", "COMPLETING", ""])
def test_a_job_still_going_is_neither_dead_nor_done(state):
    assert cj.classify_sacct_row(_row(state))[0] == cj.RUNNING


# ---------------------------------------------------------------------------
# The decorated state — the bug that made a deliberate cancel invisible.
# ---------------------------------------------------------------------------

def test_sacct_decorates_cancelled_and_the_bare_name_never_matched():
    assert cj.normalize_state("CANCELLED by 123456") == "CANCELLED"
    assert "CANCELLED by 123456" not in cj.TERMINAL_STATES, \
        "the raw value really is not in the set — this is why normalization is needed"
    assert cj.normalize_state("CANCELLED by 123456") in cj.TERMINAL_STATES
    assert cj.classify_sacct_row(_row("CANCELLED by 123456", "0:15"))[0] == cj.DIED


@pytest.mark.parametrize("raw,want", [
    ("completed", "COMPLETED"), ("  TIMEOUT  ", "TIMEOUT"),
    ("CANCELLED by 9", "CANCELLED"), ("", ""), (None, ""),
])
def test_normalize_state(raw, want):
    assert cj.normalize_state(raw) == want


# ---------------------------------------------------------------------------
# One definition of the terminal set.
# ---------------------------------------------------------------------------

def test_the_terminal_state_set_has_exactly_one_definition():
    """It was a literal in the poller AND a literal in the poller's test, asserted
    by equality — so the test could only prove the set had not changed, never that
    it was right, and any fix had to edit two files in lockstep."""
    from agent.skills import run_cluster_step as rcs
    assert rcs._TERMINAL_STATES is cj.TERMINAL_STATES


def test_the_death_reasons_cover_every_terminal_state_that_is_not_success():
    """A death that reports 'the job ended in state X' has told the user nothing
    they could not read off sacct themselves."""
    uncovered = sorted(cj.TERMINAL_STATES - {"COMPLETED"} - set(cj._DEATH_REASON))
    assert not uncovered, f"no plain-English reason for {uncovered}"


# ---------------------------------------------------------------------------
# The production window.
# ---------------------------------------------------------------------------

def test_a_real_sacct_line_for_a_wall_clock_kill_classifies_as_death():
    """A verbatim sacct row for the most common production death. Nothing about
    it looks wrong until you read the State: the exit code is literally `0:0`.

    (That the row reaches a caller WITH its verdict attached is pinned end-to-end
    in the L14 command surface, where the ssh/auth fixture lives.)"""
    sacct = ("58025583|TIMEOUT|04:00:11|0:0|c1|TimeLimit|2026-07-30T01:00:00|"
             "2026-07-30T05:00:11")
    rows = cj._parse_sacct_output(sacct)
    assert len(rows) == 1 and rows[0]["exit_code"] == "0:0"
    verdict, why = cj.classify_sacct_row(rows[0])
    assert verdict == cj.DIED
    assert "wall-clock" in why and "TimeLimit" in why


def test_the_verdict_is_additive_and_leaves_the_raw_columns_alone():
    """Every existing consumer reads state/exit_code/reason; the verdict is an
    extra key, not a replacement, so nothing downstream had to change."""
    row = {"job_id": "1", "state": "FAILED", "exit_code": "0:9", "reason": "None"}
    before = dict(row)
    verdict, why = cj.classify_sacct_row(row)
    assert row == before, "classify must not mutate the row it is given"
    assert verdict == cj.DIED
