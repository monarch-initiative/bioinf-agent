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

import socket
from pathlib import Path

import pytest

_RECORD_DIRS = ("transfer_history", "job_submissions")
_REPO = Path(__file__).resolve().parent.parent

# Contract-clean EnvCache record builders live in tests/env_records.py — importable as
# `from env_records import env_record, env_evidence` from anywhere in the suite.


# --- the hermetic tier must actually be hermetic -----------------------------------
#
# `-m "not live and not integration_docker"` is the tier CI blocks merges on. It is
# supposed to be hermetic, and it was not: eight tests reached real registries
# (anaconda.org, CRAN, api.github.com) without carrying the `live` marker. That costs
# two things, and the second is the expensive one:
#
#   * time — those eight were ~25s of the tier's 98s wall clock, spent waiting;
#   * TRUST — this repo's own resolver docs record what one transient 403 from
#     anaconda.org does: `resolve('samtools')` slid from conda to binary and adopted a
#     different artifact. A CI tier that can go red for that reason teaches people to
#     re-run it, and a suite people re-run on failure is a suite that no longer gates.
#
# The marker existed and was correct; nothing MADE tests carry it. So the fix is the
# mechanism rather than the eight edits: outbound TCP raises here unless the test opted
# in. Same posture as tests/test_generated_artifact_reads.py — the class of defect kept
# arriving one file at a time until something refused it at the boundary.
#
# Scoped narrowly on purpose. AF_UNIX (the Docker socket) and loopback stay open: those
# are local IPC, not the network, and blocking them would break the docker tier and any
# test that binds a scratch port.

_real_connect = socket.socket.connect
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


class NetworkAccessInHermeticTier(RuntimeError):
    """Raised when an unmarked test reaches for the network."""


def _local(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True                      # AF_UNIX path (str) / anything not TCP
    return str(address[0]) in _LOOPBACK


@pytest.fixture(autouse=True)
def _no_network_unless_marked(request):
    """Refuse outbound TCP for any test not marked `live` or `integration_docker`.

    Autouse and root-scoped, for the same reason the record-writer guard above is: an
    opt-in guard is one a new test file can forget, and forgetting is silent — a test
    that quietly depends on PyPI looks exactly like one that does not, right up until
    the registry has a bad afternoon.
    """
    if request.node.get_closest_marker("live") or \
       request.node.get_closest_marker("integration_docker"):
        yield
        return

    def guarded(self, address, *a, **kw):
        if _local(address):
            return _real_connect(self, address, *a, **kw)
        raise NetworkAccessInHermeticTier(
            f"{request.node.nodeid} reached out to {address!r}, but it is not marked "
            f"`live`. The hermetic tier is what CI gates merges on: a test in it must "
            f"not be able to fail because a registry rate-limited us.\n"
            f"  * genuinely needs a real registry -> add @pytest.mark.live (opt-in tier, "
            f"`pytest -m live`), and consider whether tests/live/test_intent_corpus.py "
            f"already covers the same decision;\n"
            f"  * only needs the DECISION, not the registry -> stub the probe and assert "
            f"on the ranking.")

    socket.socket.connect = guarded
    try:
        yield
    finally:
        socket.socket.connect = _real_connect


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
