"""
Conftest for the L5 (run-honesty) suite.

The tests in this directory drive the agent's pipeline lifecycle through
the MCP server's `_pipeline_state` SINGLETON — `m._pipeline_state.start()`,
`m._pipeline_state.add_install_step()`, `m._pipeline_state.patch()`, etc.
That singleton is bound to `<repo>/data/pipeline_drafts/` at import time,
so each test's draft persists in the working directory until the SAME
test runs again (it calls `discard(pid)` on entry, not on exit). A
crashed / interrupted test leaves its draft on disk forever.

The fix: autouse isolation. For every test in this directory, swap the
singleton for a fresh PipelineState pointed at a `tmp_path`-backed
drafts dir. Drafts created during the test land in `tmp_path` (which
pytest cleans up); the real working-dir drafts dir stays untouched.

This is THE pattern future test files that drive the lifecycle through
the singleton should copy. If you're adding such a test file outside
this directory, either:
  (a) Move the test in here, or
  (b) Copy this conftest into the new test's directory, or
  (c) Construct your own PipelineState pointed at tmp_path directly
      (the test_invariants.py pattern), bypassing the singleton.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_pipeline_drafts(monkeypatch, tmp_path):
    """Redirect the mcp_server `_pipeline_state` singleton to a fresh
    PipelineState rooted at `tmp_path` for the duration of the test.

    Why monkeypatch the singleton instead of just swapping its
    `drafts_dir` attribute: PipelineState loads existing drafts from
    disk in `__init__`. We want a CLEAN state per test — a fresh
    instance gives us that. monkeypatch restores the original singleton
    at teardown (so tests outside this directory keep using the real
    one)."""
    from agent import mcp_server as m
    from agent.skills.pipeline_state import PipelineState

    # No path plumbing: PipelineState resolves its drafts and reports zones from
    # the workspace, which the root conftest has already redirected at tmp_path.
    # What this fixture still buys is a SEPARATE INSTANCE — the live singleton
    # holds in-memory drafts, and a test that mutated it would bleed into the next.
    isolated = PipelineState(m.config)
    monkeypatch.setattr(m, "_pipeline_state", isolated)
    yield isolated
    # monkeypatch teardown automatically restores the original singleton.
