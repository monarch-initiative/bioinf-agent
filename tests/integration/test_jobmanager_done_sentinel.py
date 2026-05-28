"""
N6 (batch-3 Apollo3 stress): background jobs MUST drop an atomic `.done`
sentinel file when they transition to a terminal state.

Pre-fix, the only on-disk signal was `{job_id}.status.json`, which is
written at t=0 with state="running". Naive shell polling loops like
`until [ -f X.status.json ]; do sleep 1; done` fired immediately and
proceeded to read state="running" forever (or until the agent later
overwrote it). The CORRECT condition was always `jq '.state' == "exited"`,
but file-existence checks predate that contract. Drop a `.done` file ONLY
on terminal transition so existence checks work the obvious way.

Why integration, not unit: the bug lives in the on-disk transition between
two atomic writes by a real subprocess. A unit test that mocks
`_write_status` confirms the conditional, but cannot prove the SUBPROCESS
actually writes the sentinel after a real spawn/exit cycle. This test
spawns a real (10ms) bash command and checks both files exist.
"""
from __future__ import annotations

import json
import os
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


def _wait_exited(jm: JobManager, jid: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = jm.check(jid, log_tail_lines=0)
        if s.get("state") == "exited":
            return s
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not exit within {timeout}s")


@pytest.mark.integration
def test_done_sentinel_appears_on_terminal_state(tmp_path):
    jm = _jm(tmp_path)
    r = jm.start("true", job_id="quick_ok")
    assert r["state"] == "running"

    status_file = jm.jobs_dir / "quick_ok.status.json"
    done_file   = jm.jobs_dir / "quick_ok.done"

    # status.json exists immediately (from t=0).
    assert status_file.exists(), "status.json missing right after start"
    # .done MUST NOT exist while running.
    assert not done_file.exists(), \
        "completion sentinel leaked into RUNNING state — N6 contract violated"

    s = _wait_exited(jm, "quick_ok")
    assert s["returncode"] == 0

    # .done MUST exist after terminal transition.
    assert done_file.exists(), \
        "completion sentinel missing after exit — N6 polling contract broken"


@pytest.mark.integration
def test_done_sentinel_works_for_failed_jobs_too(tmp_path):
    """A non-zero exit is still a terminal state. The sentinel signals
    'finished', not 'succeeded'; the returncode in status.json is the truth."""
    jm = _jm(tmp_path)
    jm.start("false", job_id="quick_fail")

    s = _wait_exited(jm, "quick_fail")
    assert s["state"] == "exited"
    assert s["returncode"] != 0

    done_file = jm.jobs_dir / "quick_fail.done"
    assert done_file.exists(), \
        "completion sentinel missing after a failed exit — N6 must fire on ANY terminal state"


@pytest.mark.integration
def test_polling_loop_pattern_via_check_job_writes_done(tmp_path):
    """The realistic agent polling pattern: call check() (i.e. check_job at
    the MCP face) in a loop; the sentinel appears once check observes the
    terminal transition. NOTE: a BARE filesystem watch (`until [ -f X.done ]`)
    without ever calling check() will never observe the sentinel — the
    SUBPROCESS does not write .done; the parent does, on the FIRST check()
    after the child exits. This is the contract the test pins; the docstring
    on _done_path slightly oversells the "shell loop" pattern."""
    jm = _jm(tmp_path)
    jm.start("sleep 0.2 && echo hello", job_id="sleepy")
    done_file = jm.jobs_dir / "sleepy.done"

    deadline = time.time() + 5.0
    seen_done = False
    while time.time() < deadline:
        jm.check("sleepy", log_tail_lines=0)        # the side-effect that writes .done
        if done_file.exists():
            seen_done = True
            break
        time.sleep(0.05)
    assert seen_done, "check()-driven polling never observed .done"

    # Once .done exists, status.json is authoritative for state.
    status = json.loads((jm.jobs_dir / "sleepy.status.json").read_text())
    assert status["state"] == "exited"
    assert status["returncode"] == 0
