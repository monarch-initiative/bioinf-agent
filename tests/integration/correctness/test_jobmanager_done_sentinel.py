"""The `.done` completion sentinel: written BY THE JOB, so an external wait works.

Two contracts have lived on this file, and the difference is the whole point.

ORIGINAL (N6): `.done` was touched by the PARENT, inside `_write_status`, the
first time `check()` observed the terminal transition. That made a
file-existence wait correct only for a caller who was ALSO polling check() —
which is to say, a caller who did not need the file.

CURRENT (cold-start finding CS8): the job writes its own sentinel from a bash
EXIT trap installed by `start()`. A driven agent, blocked by its harness from
sleeping, ran `until [ -f <job>.done ]; do sleep 5; done` in a background shell
and ended its turn waiting on it. The install had succeeded in 40 seconds; the
wait could never finish, because nothing was calling check(). The tool surface
hands that exact path to the caller in the spawn receipt's `done_marker` field,
so the file had to be made true rather than the callers made careful — this is
the second independent agent to have read the receipt the obvious way.

What the sentinel still does NOT mean is SUCCEEDED. status.json's state +
returncode remain the outcome, so every test here reads one after waiting.

Integration, not unit: the bug lives in the on-disk transition made by a real
subprocess. Mocking `_write_status` confirms a conditional and proves nothing
about what the child actually wrote.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.skills.job_manager import JobManager


def _jm(tmp_path: Path) -> JobManager:
    """JobManager wired to tmp_path. We don't need a real conda env for the
    no-env_name code path — EnvManager just constructs."""
    cfg = {"paths": {"conda_envs_prefix": str(tmp_path / "envs")}}
    jm = JobManager(cfg)
    # JobManager hard-codes jobs_dir to project_root/data/jobs in __init__.
    # Redirect onto tmp_path so the test doesn't litter the real dir.
    jm.jobs_dir = tmp_path / "jobs"
    jm.jobs_dir.mkdir(parents=True, exist_ok=True)
    return jm


def _wait_file(p: Path, timeout: float = 10.0) -> bool:
    """The caller's pattern, verbatim: watch the file, call nothing."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.exists():
            return True
        time.sleep(0.05)
    return False


def _wait_exited(jm: JobManager, jid: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = jm.check(jid, log_tail_lines=0)
        if s.get("state") == "exited":
            return s
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not exit within {timeout}s")


@pytest.mark.integration
def test_a_bare_file_wait_completes_without_anyone_calling_check(tmp_path):
    """THE CS8 REGRESSION TEST. No check() call anywhere in this test until the
    wait has already returned — exactly the situation that used to spin forever."""
    jm = _jm(tmp_path)
    r = jm.start("sleep 0.3 && echo hello", job_id="waited_on")
    done_file = Path(r["done_marker"])

    assert _wait_file(done_file), (
        "a bare `until [ -f X.done ]` wait never completed — the job is not writing "
        "its own sentinel, so CS8 is back and any caller who waits on `done_marker` "
        "(which the spawn receipt hands them) hangs forever")

    # The sentinel says FINISHED; the outcome still comes from status.json.
    s = jm.check("waited_on", log_tail_lines=0)
    assert s["state"] == "exited"
    assert s["returncode"] == 0


@pytest.mark.integration
def test_the_spawn_receipt_names_the_sentinel_it_creates(tmp_path):
    """`done_marker` on the receipt must be the path the job actually writes —
    a receipt naming a file nobody creates is how CS8 was handed to the caller."""
    jm = _jm(tmp_path)
    r = jm.start("true", job_id="receipt")
    assert r["done_marker"] == str(jm.done_path("receipt"))
    assert _wait_file(jm.done_path("receipt"))


@pytest.mark.integration
def test_the_sentinel_is_absent_while_the_job_is_genuinely_running(tmp_path):
    """FINISHED must not fire early. Uses a job long enough that observing it
    mid-flight is not a race."""
    jm = _jm(tmp_path)
    jm.start("sleep 3", job_id="slow")
    done_file = jm.jobs_dir / "slow.done"

    time.sleep(0.5)
    assert not done_file.exists(), \
        "completion sentinel leaked into RUNNING state — .done no longer means finished"
    assert jm.check("slow", log_tail_lines=0)["state"] == "running"

    jm.cancel("slow", force=True)


@pytest.mark.integration
def test_a_stale_sentinel_from_a_reused_job_id_is_cleared(tmp_path):
    """Re-using a job_id must not inherit the previous run's sentinel: a waiter
    would return instantly, before the new job had produced anything."""
    jm = _jm(tmp_path)
    jm.start("true", job_id="reused")
    assert _wait_file(jm.done_path("reused"))
    jm.check("reused", log_tail_lines=0)

    jm.start("sleep 3", job_id="reused")
    assert not jm.done_path("reused").exists(), \
        "start() left the previous run's .done in place — a file-wait on the new " \
        "job returns immediately"

    jm.cancel("reused", force=True)


@pytest.mark.integration
def test_the_sentinel_fires_for_a_failed_job_too(tmp_path):
    """A non-zero exit is still terminal. The sentinel signals 'finished', not
    'succeeded'; a waiter that never woke on failure would be the same hang."""
    jm = _jm(tmp_path)
    jm.start("false", job_id="quick_fail")

    assert _wait_file(jm.done_path("quick_fail")), \
        "completion sentinel missing after a failed exit — it must fire on ANY terminal state"
    s = _wait_exited(jm, "quick_fail")
    assert s["state"] == "exited"
    assert s["returncode"] != 0


@pytest.mark.integration
def test_the_trap_does_not_change_the_jobs_exit_code(tmp_path):
    """The sentinel is installed as a bash EXIT trap in front of the caller's
    command. Bash preserves $? across an EXIT trap — if that ever stopped being
    true, every job would report the touch's exit code instead of its own."""
    jm = _jm(tmp_path)
    jm.start("exit 42", job_id="rc42")
    s = _wait_exited(jm, "rc42")
    assert s["returncode"] == 42, \
        f"the done-sentinel trap swallowed the job's exit code (got {s['returncode']})"


@pytest.mark.integration
def test_check_still_writes_the_sentinel_when_the_trap_could_not_run(tmp_path):
    """The backstop in _write_status. A SIGKILLed job never runs its EXIT trap,
    so the parent must still leave the sentinel when it observes the exit —
    otherwise a waiter on a killed job hangs exactly as CS8 did."""
    jm = _jm(tmp_path)
    jm.start("sleep 30", job_id="killed")
    time.sleep(0.3)
    jm.cancel("killed", force=True)

    assert jm.done_path("killed").exists(), \
        "no sentinel after a forced kill — the trap cannot run and the backstop did not fire"
    assert json.loads((jm.jobs_dir / "killed.status.json").read_text())["state"] == "cancelled"


@pytest.mark.integration
def test_a_zombie_child_reads_as_finished_not_running(tmp_path):
    """The other half of CS8. `os.kill(pid, 0)` answers for a zombie — a child
    that has exited but not been reaped — so liveness alone reported a finished
    job as 'running' indefinitely. check() must reap it and recover the code."""
    psutil = pytest.importorskip("psutil", reason="the zombie test needs psutil")
    jm = _jm(tmp_path)
    jm.start("exit 7", job_id="zombie")
    assert _wait_file(jm.done_path("zombie"))

    # Drop the Popen handle so check() takes the restart branch, leaving the
    # exited child unreaped — the exact state the drive found via `ps`.
    proc = jm._procs.pop("zombie")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            if psutil.Process(proc.pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            pytest.skip("the child was reaped before we could observe it as a zombie")
        time.sleep(0.05)

    assert JobManager._is_pid_alive(proc.pid) is False, \
        "a zombie pid reads as alive — check_job reports 'running' forever (CS8)"
    s = jm.check("zombie", log_tail_lines=0)
    assert s["state"] == "exited"
    assert s["returncode"] == 7, \
        f"the restart branch did not reap the child to recover its code (got {s.get('returncode')})"


@pytest.mark.integration
def test_check_right_after_the_sentinel_never_says_running(tmp_path):
    """THE RACE BEHIND AN INTERMITTENT RED. The sentinel is written from a bash
    EXIT trap, and a trap runs while the shell is STILL ALIVE — so between
    `touch X.done` and the process becoming reapable there is a window in which
    poll() returns None and check() reported `running` for a job that had just
    told the world it finished.

    That is the sequence `done_path`'s own docstring prescribes ("wait on .done,
    then call check_job once") landing on the wrong answer, so a caller who uses
    `done_marker` as intended is sent back to polling — the affordance's entire
    purpose.

    NOTE THE TIGHT SPIN, it is the point. `_wait_file` sleeps 50ms between looks,
    which is long enough for the process to finish exiting, so a wait built on it
    steps over the window and this test passes with or without the fix. Spinning
    catches the sentinel within microseconds of its creation, which is where the
    race lives: measured 53/60 before the fix, 0/60 after. The one intermittent
    full-suite failure that started this was the same race, widened by load.
    """
    jm = _jm(tmp_path)
    for i in range(25):
        jid = f"teardown_{i}"
        jm.start("true", job_id=jid)
        done = jm.done_path(jid)
        deadline = time.time() + 10.0
        while not done.exists() and time.time() < deadline:
            pass                      # no sleep — see the docstring
        assert done.exists(), f"{jid} never wrote its sentinel"

        s = jm.check(jid, log_tail_lines=0)
        assert s["state"] == "exited", (
            f"{jid}: check() said {s['state']!r} the instant the completion "
            f"sentinel appeared. The job is over — its own EXIT trap said so — "
            f"and a caller following the documented wait-then-check sequence "
            f"must not be told it is still running")
        assert s["returncode"] == 0
