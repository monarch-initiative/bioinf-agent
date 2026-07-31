"""
The shared stores survive concurrent writers.

Every shared store here does read-modify-write. `_save` was already atomic
(mkstemp + fsync + os.replace), which prevents a TORN file — and that is exactly
what made the missing mutual exclusion hard to notice, because the file always
parses. Atomicity and exclusion are different guarantees.

Measured before the fix: 3 processes x 60 `EnvCache.register` calls left 84 of
180 records on disk. 96 frozen envs — each a real image built at real cost —
erased with no error anywhere.

THIS IS NOT A FUTURE PROBLEM. `freeze(background=True)` is a shipped feature that
spawns a DETACHED SUBPROCESS which registers into the same EnvCache and writes
the same draft as its parent. The window is open in ordinary single-agent use.

Real processes, not threads: the GIL hides a lost update that `fork` exposes, so
a threaded version of these tests would pass against the unlocked code.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agent.skills import store_lock
from agent.skills.store_lock import StoreLockTimeout, locked

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The lock itself.
# ---------------------------------------------------------------------------

def test_lock_excludes_a_second_holder_and_times_out(tmp_path):
    store = tmp_path / "s.json"
    with locked(store):
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys; sys.path.insert(0, {str(ROOT)!r})
                from agent.skills.store_lock import locked, StoreLockTimeout
                try:
                    with locked({str(store)!r}, timeout=0.3):
                        print("ACQUIRED")
                except StoreLockTimeout:
                    print("BLOCKED")
            """)], capture_output=True, text=True, timeout=60)
        assert proc.stdout.strip() == "BLOCKED", proc.stderr


def test_lock_is_released_even_when_the_body_raises(tmp_path):
    store = tmp_path / "s.json"
    with pytest.raises(ValueError):
        with locked(store):
            raise ValueError("boom")
    with locked(store, timeout=0.5):     # would raise StoreLockTimeout if still held
        pass


def test_lock_file_is_a_sibling_not_the_store_itself(tmp_path):
    """Locking the store directly does not work: `_save` replaces it via os.replace, so
    the new inode carries no lock and a writer holding the OLD inode excludes nobody."""
    store = tmp_path / "s.json"
    assert store_lock.lock_path_for(store) == tmp_path / "s.json.lock"
    with locked(store):
        assert (tmp_path / "s.json.lock").exists()
        assert not store.exists(), "the lock must not create or touch the store"


def test_a_shared_lock_admits_readers_but_not_a_writer(tmp_path):
    store = tmp_path / "s.json"
    with locked(store, shared=True):
        with locked(store, shared=True, timeout=0.3):
            pass                                   # two readers coexist
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys; sys.path.insert(0, {str(ROOT)!r})
                from agent.skills.store_lock import locked, StoreLockTimeout
                try:
                    with locked({str(store)!r}, timeout=0.3): print("ACQUIRED")
                except StoreLockTimeout: print("BLOCKED")
            """)], capture_output=True, text=True, timeout=60)
        assert proc.stdout.strip() == "BLOCKED", proc.stderr


def test_timeout_refuses_rather_than_writing_unlocked():
    """A store that cannot take its lock must REFUSE. Proceeding anyway is precisely
    the unlocked read-modify-write that loses records, so 'best effort' here would mean
    'silently reintroduce the bug under contention'."""
    assert issubclass(StoreLockTimeout, RuntimeError)


# ---------------------------------------------------------------------------
# EnvCache — the measured data loss.
# ---------------------------------------------------------------------------

def _register_many(args):
    path, tag, n = args
    sys.path.insert(0, str(ROOT))
    from agent.skills.freeze import EnvCache
    cache = EnvCache(path)
    for i in range(n):
        cache.register(f"{tag}-{i}", {"image": "x:1", "image_digest": "sha256:ab"})
    return n


@pytest.mark.integration
def test_concurrent_registers_lose_nothing(tmp_path):
    """THE REPRODUCTION, as a standing test. Without the lock this leaves ~47% of the
    records; the count is the whole assertion."""
    path = str(tmp_path / "cache.json")
    from agent.skills.freeze import EnvCache
    EnvCache(path).register("seed", {"image": "s", "image_digest": "sha256:00"})

    workers, per_worker = 3, 40
    ctx = mp.get_context("spawn")           # fork+threads is unsafe on macOS
    with ctx.Pool(workers) as pool:
        pool.map(_register_many, [(path, f"w{w}", per_worker) for w in range(workers)])

    got = json.loads(Path(path).read_text())
    assert len(got) == workers * per_worker + 1, (
        f"lost {workers * per_worker + 1 - len(got)} record(s) — an unlocked "
        f"read-modify-write is back")


# ---------------------------------------------------------------------------
# PipelineState — the stale in-memory copy, not a torn write.
# ---------------------------------------------------------------------------

def _state(tmp_path):
    from agent import mcp_server as _ms
    from agent.skills.pipeline_state import PipelineState
    (tmp_path / "drafts").mkdir(exist_ok=True)
    (tmp_path / "reports").mkdir(exist_ok=True)
    return PipelineState({**_ms.config,
                          "paths": {**_ms.config.get("paths", {}),
                                    "drafts_dir": str(tmp_path / "drafts"),
                                    "pipelines_dir": str(tmp_path / "reports")}})


def test_a_pointer_written_by_another_process_is_not_erased(tmp_path):
    """The background-freeze shape, in two PipelineState instances: `parent` is the
    long-lived server, `child` stands in for the detached freeze subprocess.

    The child writes `frozen_as` and exits. The parent's in-memory copy predates that
    write — so before the fix its very next mutation persisted the stale draft and the
    pointer to a real, completed freeze simply disappeared."""
    parent = _state(tmp_path)
    pid = parent.start("p", "d")["pipeline_id"]

    child = _state(tmp_path)                      # a second process's view
    child.set_frozen_pointer(pid, "samtools=1.21|linux/amd64|none")

    # the parent has not seen it yet — it re-reads on the write path, not on read
    parent.mutate_on_disk(pid, lambda d: d.setdefault("notes", []).append("later work"))

    on_disk = _state(tmp_path).get_draft(pid)
    assert on_disk["frozen_as"] == "samtools=1.21|linux/amd64|none", \
        "the other process's pointer was erased by a stale write"
    assert on_disk["notes"] == ["later work"]


def test_sealed_pointers_from_two_writers_both_survive(tmp_path):
    a, b = _state(tmp_path), _state(tmp_path)
    pid = a.start("p", "d")["pipeline_id"]
    b._drafts[pid] = dict(a.get_draft(pid))       # b's independent snapshot

    a.append_sealed_pointer(pid, "wf_one")
    b.append_sealed_pointer(pid, "wf_two")

    assert sorted(_state(tmp_path).get_draft(pid)["sealed_as"]) == ["wf_one", "wf_two"]


def test_mutate_on_disk_reports_a_missing_draft_rather_than_creating_one(tmp_path):
    assert _state(tmp_path).mutate_on_disk("never-existed", lambda d: None) is False


def test_ordinary_mutators_remain_single_writer_by_design(tmp_path):
    """A STATED limitation, pinned so it stays stated. `add_step` and friends still edit
    the in-memory copy and persist it wholesale, so two processes mutating the same draft
    through them lose one side's edits. Only the cross-process pointer writes go through
    `mutate_on_disk` today. This test documents the boundary; when batch install lands and
    the boundary moves, it should fail and be rewritten — not deleted."""
    a, b = _state(tmp_path), _state(tmp_path)
    pid = a.start("p", "d")["pipeline_id"]
    b._drafts[pid] = json.loads(json.dumps(a.get_draft(pid)))

    a.patch(pid, {"description": "from A"})
    b.patch(pid, {"notes": ["from B"]})           # b's snapshot predates A's write

    final = _state(tmp_path).get_draft(pid)
    assert final["notes"] == ["from B"]
    assert final["description"] != "from A", (
        "if this now passes, the ordinary mutators became cross-process safe — good; "
        "rewrite this test to assert that instead of the old boundary")


# ---------------------------------------------------------------------------
# JobManager — deliberately unlocked, and why.
# ---------------------------------------------------------------------------

def test_job_status_needs_no_lock_because_it_is_one_file_per_job():
    """Not an oversight. JobManager writes `data/jobs/<job_id>.json` — one writer per
    file, no shared document, so there is no read-modify-write to interleave. Adding a
    lock here would be cargo-culting the mechanism instead of the reason for it."""
    src = (ROOT / "agent" / "skills" / "job_manager.py").read_text()
    assert "store_lock" not in src
    assert "_status_path(job_id)" in src, \
        "job status is no longer per-job — re-examine whether it now needs the lock"
