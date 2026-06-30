"""
Conftest for the L4 (mode-honesty) suite.

The tests in this directory drive `m.freeze()` (and sometimes the
attestation rendering path) for policy-firewall + early-gate
behavior. `freeze()` writes to the EnvCache SINGLETON on success
(and even before some refusals, if the marker is thrown after the
cache write). The singleton is bound to
`<repo>/env_reports/_env_cache.json` at import time, so any record
written during a test PERSISTS into the user's production cache —
where it becomes the canonical record for that request_key, and its
test-fixture `env_name` (e.g. `bioinf_test_ungated`) flows
downstream into every subsequent live drive's `.sif` filename and
`apptainer pull` example (caught 2026-07-12 driving Path 4 against
hpc_cluster — the cluster's .sif was named `bioinf_test_ungated_*.sif`
because a leaked freeze record had `name="bioinf_test_ungated"`).

The fix mirrors the L5 conftest: autouse isolation. For every test
in this directory, swap the singleton's `path` to a `tmp_path`-backed
file. Records written during the test land in `tmp_path` (pytest
cleans up); the real working-dir cache stays untouched.

If you're adding a test outside L4 that calls `m.freeze()` directly
(rather than constructing an `EnvCache(tmp_path/...)` instance like
`tests/test_invariants.py` does), either:
  (a) Move the test in here, or
  (b) Copy this conftest into the new test's directory, or
  (c) Add a per-test `monkeypatch.setattr(m._env_cache, "path", ...)`
      (the `test_end_to_end_seal.py` pattern).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_env_cache(monkeypatch, tmp_path):
    """Redirect the mcp_server `_env_cache` singleton's persistence
    path to a fresh file under `tmp_path` for the duration of the
    test.

    Why monkeypatch the singleton's `path` attribute instead of
    swapping the whole instance: EnvCache reads from disk every
    `lookup()` / writes every `register()` — it doesn't hold state in
    memory across calls. Pointing `.path` at `tmp_path/_env_cache.json`
    means lookups return empty (clean state) and registers write to
    tmp. monkeypatch restores the original path at teardown so tests
    outside this directory keep using the real one."""
    from agent import mcp_server as m
    monkeypatch.setattr(m._env_cache, "path", tmp_path / "_env_cache.json")
