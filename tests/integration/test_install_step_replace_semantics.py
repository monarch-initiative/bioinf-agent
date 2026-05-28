"""
N4 (batch-3 Apollo3 stress): install_step replace_step=N MUST always remove
the prior entry at slot N — the caller's contract is 'this IS my step N,
throw away whatever was there'. Pre-fix, the smart-replace-on-failure path
APPENDED the new entry without removing the prior failed entry, leaving
both rc=1 and rc=0 records in install_steps; freeze replay then saw two
installs at the same name and required hand-editing the draft yaml.

Why integration, not unit: the bug was in the DISK-BACKED draft list, not in
a constructed dict. A unit test using a mocked persistence layer would miss
the case where `_persist` survived the smart-replace branch with stale list
state. Here we use a real PipelineState pointed at tmp_path, exercise the
real branch, and re-load from disk to confirm the persisted state matches
the in-memory state.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills.pipeline_state import PipelineState


def _state(tmp_path: Path) -> PipelineState:
    cfg = {"paths": {
        "pipelines_dir": str(tmp_path / "env_reports"),
        "drafts_dir":    str(tmp_path / "drafts"),
    }}
    return PipelineState(cfg)


@pytest.mark.integration
def test_replace_step_after_failure_drops_failed_entry_and_appends_at_end(tmp_path):
    """The smart-replace branch: prior step failed, new succeeds → new lands
    at end, prior is GONE (not duplicated)."""
    ps = _state(tmp_path)
    ps.start("p1", "test")

    # Step 1 failed.
    ps.add_install_step("p1", {"tool": "samtools", "returncode": 1,
                                "stderr": "no version found"})
    # Step 2 succeeded (some other tool).
    ps.add_install_step("p1", {"tool": "bwa", "returncode": 0})
    # Retry of step 1 — caller asserts replace_step=1.
    ps.add_install_step("p1", {"tool": "samtools", "returncode": 0},
                        replace_step=1)

    steps = ps.get_draft("p1")["install_steps"]
    # Two entries (the failed samtools entry is gone), not three.
    assert len(steps) == 2, f"expected 2 entries, got {len(steps)}: {steps}"
    # Both entries succeeded — no zombie failed entry left behind.
    assert all(s["returncode"] == 0 for s in steps), f"failed entry not removed: {steps}"
    # The successful retry lands at the END (after bwa).
    assert steps[-1]["tool"] == "samtools"
    # `step` fields are renumbered 1..N (contiguous, 1-based).
    assert [s["step"] for s in steps] == [1, 2]


@pytest.mark.integration
def test_replace_step_when_prior_succeeded_edits_in_place(tmp_path):
    """The edit-in-place branch: prior step also succeeded → new merges over
    the prior at the same slot (e.g. agent bumping a version)."""
    ps = _state(tmp_path)
    ps.start("p1", "test")
    ps.add_install_step("p1", {"tool": "samtools", "version": "1.20", "returncode": 0})
    ps.add_install_step("p1", {"tool": "samtools", "version": "1.21", "returncode": 0},
                        replace_step=1)

    steps = ps.get_draft("p1")["install_steps"]
    assert len(steps) == 1
    assert steps[0]["version"] == "1.21"
    assert steps[0]["step"] == 1


@pytest.mark.integration
def test_persisted_draft_matches_in_memory_after_smart_replace(tmp_path):
    """After smart-replace, re-loading the draft from disk must reflect the
    same shape — the bug surface was a stale persisted list."""
    ps = _state(tmp_path)
    ps.start("p1", "test")
    ps.add_install_step("p1", {"tool": "samtools", "returncode": 1})
    ps.add_install_step("p1", {"tool": "samtools", "returncode": 0},
                        replace_step=1)

    # Force a fresh load from disk (constructor scans drafts_dir).
    ps2 = _state(tmp_path)
    steps = ps2.get_draft("p1")["install_steps"]
    assert len(steps) == 1
    assert steps[0]["returncode"] == 0
    assert steps[0]["tool"] == "samtools"
