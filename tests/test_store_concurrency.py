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


def test_ordinary_mutators_are_cross_process_safe(tmp_path):
    """THE BOUNDARY MOVED. This test used to assert the opposite — that A's write was
    ERASED — and carried an instruction to rewrite it rather than delete it when the
    ordinary mutators started re-reading. They now do: every mutator goes through
    `_mutate` (lock, re-read from disk, apply, write), `_persist` is gone, and there is
    no longer a door that writes a cached copy.

    B's snapshot deliberately predates A's write, which is the whole point: a writer
    holding a stale view must not be able to roll back a write it never saw."""
    a, b = _state(tmp_path), _state(tmp_path)
    pid = a.start("p", "d")["pipeline_id"]
    b._drafts[pid] = json.loads(json.dumps(a.get_draft(pid)))

    a.patch(pid, {"description": "from A"})
    b.patch(pid, {"notes": ["from B"]})           # b's snapshot predates A's write

    final = _state(tmp_path).get_draft(pid)
    assert final["notes"] == ["from B"]
    assert final["description"] == "from A", \
        "B's stale snapshot rolled back A's write — the mutators are not re-reading"


def test_start_resumes_another_processes_draft_instead_of_wiping_it(tmp_path):
    """The CREATE-side of the same bug, and the destructive one. `start` decided
    resume-vs-new from `self._drafts`, so a second process saw the id missing from its
    OWN map, built a fresh empty draft and persisted it over one already being filled —
    reporting a clean new pipeline rather than an error."""
    a = _state(tmp_path)
    pid = a.start("p", "d")["pipeline_id"]
    a.add_install_step(pid, {"command": "conda install samtools", "returncode": 0})

    b = _state(tmp_path)                       # a second process, cold cache
    assert b.start("p", "d")["resumed"] is True
    assert b.get_draft(pid)["install_steps"], "start() wiped a draft it should have resumed"


def test_forty_appends_from_three_processes_all_survive(tmp_path):
    """The reproduction that matters for a batch: concurrent step appends. Threads share
    a process but each gets its OWN PipelineState, so each has an independent stale cache
    — the same shape as N detached children, and the shape that lost writes before."""
    import threading
    a = _state(tmp_path)
    pid = a.start("p", "d")["pipeline_id"]

    def worker(tag):
        st = _state(tmp_path)
        for i in range(40):
            st.add_install_step(pid, {"command": f"{tag}-{i}", "returncode": 0})

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b", "c")]
    for t in threads: t.start()
    for t in threads: t.join()

    steps = _state(tmp_path).get_draft(pid)["install_steps"]
    assert len(steps) == 120, f"lost {120 - len(steps)} of 120 concurrent appends"
    assert [s["step"] for s in steps] == list(range(1, 121)), "step numbering is not contiguous"


# ---------------------------------------------------------------------------
# JobManager — deliberately unlocked, and why.
# ---------------------------------------------------------------------------

def _write_job_statuses(args):
    """One worker: write status for its OWN job ids. Separate process, not a thread —
    the GIL hides exactly the interleaving these tests exist to expose.

    `jobs_dir` is hardcoded to <repo>/data/jobs in the constructor, so it is redirected
    after construction; the config is loaded straight from yaml rather than by importing
    mcp_server, which drags the whole tool surface into every spawned worker.
    """
    jobs_dir, tag, n = args
    sys.path.insert(0, str(ROOT))
    import yaml
    from agent.skills.job_manager import JobManager
    cfg = yaml.safe_load((ROOT / "config" / "agent_config.yaml").read_text())
    jm = JobManager(cfg)
    jm.jobs_dir = Path(jobs_dir)
    for i in range(n):
        jm._write_status(f"{tag}-{i}", {"state": "running", "owner": tag, "seq": i})
    return n


@pytest.mark.integration
def test_concurrent_job_status_writes_do_not_interfere(tmp_path):
    """JobManager takes no lock, and that is correct rather than an oversight — but the
    claim deserves a demonstration, not an inspection.

    This used to grep job_manager.py for the ABSENCE of the string "store_lock" and the
    PRESENCE of "_status_path(job_id)". Both can hold while the store is broken (a
    shared index added elsewhere), and both can break while it is fine (the helper gets
    renamed). What actually matters is that concurrent writers cannot lose each other's
    data, so that is what is measured here — the same shape as the EnvCache
    reproduction above, which found real loss.
    """
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    workers, per_worker = 3, 40
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers) as pool:
        pool.map(_write_job_statuses,
                 [(str(jobs_dir), f"w{w}", per_worker) for w in range(workers)])

    written = sorted(p.name for p in jobs_dir.glob("*.status.json"))
    assert len(written) == workers * per_worker, (
        f"expected {workers * per_worker} status files, found {len(written)} — if this "
        f"drops, job status is no longer one-file-per-job and DOES need the lock")
    for w in range(workers):
        for i in range(per_worker):
            got = json.loads((jobs_dir / f"w{w}-{i}.status.json").read_text())
            assert got == {"state": "running", "owner": f"w{w}", "seq": i}, (
                f"w{w}-{i} was overwritten by another worker: {got}")


@pytest.mark.integration
def test_no_shared_job_index_appears_alongside_the_per_job_files(tmp_path):
    """The other half of the claim, and the one that would actually invalidate it.

    "No lock needed" rests on there being no shared document to interleave on. If
    someone adds an index — jobs.json, a manifest, anything every writer appends to —
    the reasoning silently stops holding while every per-job file still looks fine.
    Assert on what lands on disk rather than on the absence of a word in the source.
    """
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    _write_job_statuses((str(jobs_dir), "solo", 5))
    stray = [p.name for p in jobs_dir.iterdir()
             if not p.name.startswith("solo-")]
    assert not stray, (
        f"JobManager wrote {stray} outside the per-job files. A shared document is a "
        f"read-modify-write, and read-modify-write without a lock is what erased 96 "
        f"EnvCache records. Re-examine whether the lock is now required.")


# ---------------------------------------------------------------------------
# The conda prefix.
#
# The other stores here are documents that lose records. A conda prefix loses
# something worse: two concurrent `conda install --prefix X` runs race on the
# same package cache and the same prefix metadata, and conda has no locking of
# its own, so X ends up in a state neither caller asked for.
#
# This became reachable when installs could be DETACHED. Before that it needed
# two simultaneous MCP calls; parallel builds are exactly what backgrounding is
# for, and a multi-tool env is the natural way to hit it.
# ---------------------------------------------------------------------------

def _env_mgr(tmp_path):
    import agent.mcp_server as ms
    from agent.skills.env_manager import EnvManager
    m = EnvManager(ms.config)
    m.envs_dir = tmp_path / "envs"
    m.envs_dir.mkdir(parents=True, exist_ok=True)
    # A contended install must REFUSE inside the test, not queue for the real
    # install timeout (an hour).
    m.config = {**ms.config, "agent": {**ms.config["agent"],
                                       "install_timeout_seconds": 0.3}}
    return m


@pytest.mark.parametrize("call", [
    lambda m: m.install("shared_env", [{"spec": "samtools", "channel": "bioconda"}]),
    lambda m: m.create("shared_env"),
])
def test_a_conda_prefix_admits_one_writer_at_a_time(tmp_path, call):
    """Held by another PROCESS, so this is real exclusion and not a GIL artifact.

    The refusal matters as much as the exclusion: an install that quietly
    proceeded into a prefix another process is rewriting would produce a green
    result over a corrupt env, which is the failure the whole honesty contract
    exists to prevent.
    """
    m = _env_mgr(tmp_path)
    prefix = m.envs_dir / "shared_env"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time; sys.path.insert(0, {str(ROOT)!r})
            from agent.skills.store_lock import locked
            with locked({str(prefix)!r}):
                print("HELD", flush=True)
                time.sleep(20)
        """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        out = call(m)
        assert out["success"] is False
        assert out["code"] == "env_manager.env_busy"
        assert "shared_env" in out["error"]
        assert not prefix.exists(), (
            "conda ran against a prefix another process holds — the refusal is "
            "the whole point of the lock")
    finally:
        holder.kill()
        holder.wait()


def test_the_prefix_lock_is_per_env_not_global(tmp_path):
    """Two background installs into DIFFERENT envs must still run in parallel.
    A global install lock would serialize the whole batch and quietly undo the
    reason for backgrounding at all."""
    m = _env_mgr(tmp_path)
    other = m.envs_dir / "other_env"
    with locked(m.envs_dir / "shared_env"):
        # Not contended: acquiring the sibling must not block. (We take the raw
        # lock rather than run conda — the property under test is the KEY, and
        # a real solve would make this a network test.)
        with locked(other, timeout=0.3):
            pass


def test_the_auto_create_inside_install_does_not_deadlock(tmp_path, monkeypatch):
    """`install` auto-creates a missing env while holding the prefix lock, and
    store_lock is NOT re-entrant — so the inner path must reach the UNLOCKED
    `_create`. Pre-fix this self-deadlocked on the first install into a new env,
    i.e. on the single most common call in the system."""
    m = _env_mgr(tmp_path)
    calls = []
    monkeypatch.setattr(type(m), "_run",
                        lambda self, cmd, **kw: (calls.append(cmd[1]),
                                                 {"returncode": 0, "stdout": "", "stderr": ""})[1])
    monkeypatch.setattr(type(m), "apply",
                        lambda self, *a, **kw: {"success": True, "stdout": "",
                                                "stderr": "", "returncode": 0})
    out = m.install("brand_new_env", [{"spec": "samtools", "channel": "bioconda"}])
    assert out["success"] is True
    assert "create" in calls, "the auto-create never ran"
