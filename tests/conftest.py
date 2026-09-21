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

import os
import socket
import tempfile
from pathlib import Path

import pytest

_RECORD_DIRS = ("transfer_history", "job_submissions")
_REPO = Path(__file__).resolve().parent.parent


# --- the workspace redirect has to happen BEFORE the first agent import -------
#
# Several singletons resolve their directory ONCE, at import: `mcp_server._env_cache`,
# `EnvManager.envs_dir`, `JobManager.jobs_dir`, `PipelineState.drafts_dir`. That is
# correct for the server — one process, one workspace, resolved once — but it means a
# fixture cannot redirect them, because by the time any fixture runs the import has
# already happened and the directories have already been mkdir'd.
#
# It is not theoretical: the first run after the workspace split created
# ~/bioinf_agent/{environments,reports,scratch} on the developer's machine, from a
# suite that never intended to touch it.
#
# conftest.py is imported before any test module, so this is the last moment that is
# still "before". THE REAL WORKSPACE IS CAPTURED FIRST — tests/_artifacts.py reads the
# machine's actual sealed artifacts and must not be pointed at the sandbox.
from agent.skills import workspace as _workspace   # noqa: E402

# Handed on through the ENVIRONMENT rather than as an importable name: `conftest`
# is not a unique module (tests/live/conftest.py answers to it too), so a
# `from conftest import ...` resolves to whichever one pytest imported last.
#
# ASK THE RESOLVER ONLY ONCE PER SESSION. xdist workers are spawned with the
# controller's environment, which by then already carries the sandbox — so a
# worker that re-derived "the real workspace" would derive the CONTROLLER'S
# SANDBOX and call it the machine's. Every artifact check would then look in an
# empty temp dir and skip, announcing "nothing sealed on this machine" on a
# machine that had just sealed something. Inheriting the answer is what makes it
# the same answer in all 15 processes.
_inherited = os.environ.get("BIOINF_REAL_WORKSPACE", "").strip()
REAL_WORKSPACE = Path(_inherited) if _inherited else _workspace.workspace_root()
os.environ["BIOINF_REAL_WORKSPACE"] = str(REAL_WORKSPACE)
# The config home is decoupled from the workspace, so it gets the same
# capture-before-redirect treatment — asked once, inherited by every worker.
_inherited_pa = os.environ.get("BIOINF_REAL_PROJECTS_ACCESS", "").strip()
REAL_PROJECTS_ACCESS = (Path(_inherited_pa) if _inherited_pa
                        else _workspace.projects_access_path())
os.environ["BIOINF_REAL_PROJECTS_ACCESS"] = str(REAL_PROJECTS_ACCESS)
os.environ["BIOINF_WORKSPACE"] = tempfile.mkdtemp(prefix="bioinf_suite_ws_")
# The config home is DECOUPLED from the workspace (fixed ~/.bioinf_agent), so
# it needs its own sandbox twin — without this line every test that touches
# the default access path reads (or writes!) the developer's real config.
# Kept at the sandbox workspace root, where the suite's fixtures always put it.
os.environ["BIOINF_PROJECTS_ACCESS"] = os.path.join(
    os.environ["BIOINF_WORKSPACE"], "projects_access.yaml")
os.environ.pop("BIOINF_RESOURCES", None)

# Contract-clean EnvCache record builders live in tests/env_records.py — importable as
# `from env_records import env_record, env_evidence` from anywhere in the suite.
# Real on-disk fixture inputs live in tests/real_inputs.py, same import style: the
# Layer-2 external-source clauses stat and hash what a spec declares, so a fixture that
# names `/abs/x.bam` is describing a workflow the seal is built to refuse.


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
def _quiet_package_family_search(request, monkeypatch):
    """Answer the two RESEARCH-ON-A-HIT endpoints with an empty result, for every hermetic
    test — the anaconda package search, and the github repository name search.

    `probe_package_family` runs on every conda win — it is the research step that stops the
    resolver from treating the first registry hit as the answer. That made four unrelated
    stub helpers reach the network at once: each stubbed `probe_conda` and none knew about a
    probe that did not exist when they were written. The right fix is not four copies of one
    line in four files — that is the hand-copy disease this repo keeps paying for — it is
    one default, here, where the socket guard already lives.

    `name_corroboration` (2026-08-07) is the same shape on the github axis and arrived by
    the same route, so it gets the same default rather than a second round of edits across
    every stub helper in the suite. Its default is "we searched, nothing owns this name
    exactly", which is the quiet answer for the ~all of the suite that is not about
    contested names; a test that IS about one stubs `probe_github_search` directly.

    CHAINED, not replaced: only the search URLs are intercepted, so a test stubbing some
    other fetch is unaffected, and a test that monkeypatches `_fetch_json` in its own body
    (the family tests do) overrides this wholesale. Skipped under `live`, which is allowed
    out.
    """
    if "live" in request.keywords:
        yield
        return
    import urllib.request

    from agent.skills import resolver
    _real_fetch = resolver._fetch_json
    _untouched_urlopen = urllib.request.urlopen

    def guarded(url, timeout=12):
        if "/search?" in url:
            return [], ""          # a family of one is silence — the default for most tools
        if ("api.github.com/search/repositories" in url
                # ...unless the test stubbed the I/O SEAM itself, in which case it has taken
                # responsibility for the network and is very likely testing this exact probe.
                # tests/test_probe_honesty.py drives `probe_github_search` through a sealed
                # `urlopen` to prove a rate limit does not read as "no candidates"; a canned
                # answer in front of it would hide the seam under test. A default that cannot
                # be seen past is a default that rewrites the thing it was meant to quiet.
                and urllib.request.urlopen is _untouched_urlopen):
            return {"items": []}, ""     # nothing else claims this name
        return _real_fetch(url, timeout)

    monkeypatch.setattr(resolver, "_fetch_json", guarded)
    yield


@pytest.fixture(autouse=True)
def _isolate_agent_record_writers(tmp_path: Path, monkeypatch):
    """Point the WHOLE workspace at tmp_path, for EVERY test.

    Autouse + root-scoped is the point: an opt-in guard is one a new test file can forget,
    and forgetting is silent — a leaked record is byte-identical to a real one.

    One knob now covers every writer, because every writer resolves through
    `agent.skills.workspace`. That is the split's dividend: this fixture used to
    redirect exactly one anchor (`transfer._repo_root`) and `submit_workflow` leaked
    precisely because it did not route through it. Redirecting the resolver redirects
    the reports zone, the conda envs, the images, the job state and the drafts at once
    — and a writer that does NOT route through it now fails the lint in
    tests/test_workspace_resolution.py rather than leaking silently.

    $BIOINF_RESOURCES is cleared rather than set: the resources zone follows the
    workspace unless a test asks otherwise, and an inherited value from the
    developer's shell would point a hermetic test at a real reference corpus.
    """
    monkeypatch.setenv("BIOINF_WORKSPACE", str(tmp_path / "_workspace"))
    monkeypatch.setenv("BIOINF_PROJECTS_ACCESS",
                       str(tmp_path / "_workspace" / "projects_access.yaml"))
    monkeypatch.delenv("BIOINF_RESOURCES", raising=False)
    yield


def _live_records() -> set[str]:
    """Every record file in the places a leak would land: the legacy in-checkout
    dirs, and the user's REAL workspace."""
    out: set[str] = set()
    for base in ([_REPO / d for d in _RECORD_DIRS]
                 + [REAL_WORKSPACE / "reports", REAL_WORKSPACE / "scratch"]):
        if base.exists():
            out |= {str(p) for p in base.rglob("*.json")}
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

    This is the writer-agnostic backstop to the fixture above, and it earns its keep:
    the fixture used to redirect one anchor and `submit_workflow` leaked precisely
    because it did NOT route through it. A future writer could hardcode a path the same
    way — the fixture is now much harder to escape, but "harder" is not "impossible",
    and this check does not care how the bytes got there.
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
        print("*** Route the writer through agent.skills.workspace (which the autouse "
              "fixture in tests/conftest.py redirects), then delete the records above.\n")
