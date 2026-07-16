"""Repo-wide test isolation — no test may write into the user's live audit trail.

Two of the agent's record-writers anchor their output to the REPO ROOT (deliberately —
these are durable deliverables that must be findable later, not CWD-relative scratch):

  - `transfer.py`        → <repo>/transfer_history/<project>/<date>/*.json
  - `submit_workflow.py` → <repo>/job_submissions/<project>/*.submission.json

Both resolve through `transfer._repo_root()`, which is `__file__`-derived and therefore
ALWAYS points at the live repo. `monkeypatch.chdir` cannot help. Any test that drives a
transfer or a submission — successful OR refused, since refusals are journaled too —
writes a real-looking record into the user's workspace unless this fixture redirects it.

This is not hypothetical. Before this file existed (audit 2026-07-16) the live repo held
`job_submissions/demo/demo_run_987654.submission.json` (host: fake.example.edu) and 60
records under `transfer_history/c4_nonexistent_project_zzz/`, left by tests OUTSIDE the
L14-scoped conftest that already did this for L14. So the guard belongs at the root,
where no test file can sit outside it. That was the lesson of the earlier pipeline-draft
leak, relearned.

The records still get written (a test can assert on them) — they just land in tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_RECORD_DIRS = ("transfer_history", "job_submissions")
_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_agent_record_writers(tmp_path: Path, monkeypatch):
    """Redirect every repo-root-anchored record writer at tmp_path, for EVERY test.

    Autouse + root-scoped is the point: an opt-in guard is one a new test file can forget,
    and forgetting is silent — a leaked record is byte-identical to a real one.
    """
    from agent.skills import transfer
    monkeypatch.setattr(transfer, "_repo_root", lambda: tmp_path)
    yield


def _live_records() -> set[str]:
    out: set[str] = set()
    for d in _RECORD_DIRS:
        base = _REPO / d
        if base.exists():
            out |= {str(p.relative_to(_REPO)) for p in base.rglob("*.json")}
    return out


def pytest_sessionstart(session):
    session.stash_live_records = _live_records()


def pytest_sessionfinish(session, exitstatus):
    """Alarm on the guard: report any record the SUITE created in the live repo.

    A snapshot diff, not a name heuristic — "looks like a test project" would false-flag
    the real `phase_b_samtools_demo`, and would miss anything named plausibly. Anything
    that appears while the suite runs was written by the suite, by definition.

    FAILS the session rather than printing: a warning in a 1200-test run scrolls past
    unseen, which is how 60 junk records accumulated in the first place. It reports the
    paths but does not delete them — deciding what to remove from a user's audit trail is
    the user's call, not the test suite's.

    This is the writer-agnostic backstop to the fixture above, and it earns its keep: the
    fixture redirects `transfer._repo_root`, but `submit_workflow` leaked precisely
    because it did NOT route through that anchor. A future writer could do the same.
    """
    before = getattr(session, "stash_live_records", None)
    if before is None:
        return
    leaked = sorted(_live_records() - before)
    if leaked:
        session.exitstatus = 1
        print(f"\n*** TEST LEAKAGE: the suite wrote {len(leaked)} record(s) into the live "
              f"repo. These are indistinguishable from real agent output:")
        for s in leaked[:20]:
            print(f"      {s}")
        if len(leaked) > 20:
            print(f"      … and {len(leaked) - 20} more")
        print("*** Route the writer through transfer._repo_root (which the autouse fixture "
              "in tests/conftest.py redirects), then delete the records above.\n")
