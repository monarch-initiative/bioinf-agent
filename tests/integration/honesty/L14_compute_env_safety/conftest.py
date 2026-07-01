"""L14 test isolation — keep transfer manifests OUT of the real repo.

agent/skills/transfer.py resolves the manifest write directory from
`_repo_root()` (two `parent`s up from the module file), which always
points at the live repo on disk. Without a redirect, every test that
exercises a successful upload/download writes a JSON manifest into
`<repo>/transfer_history/`, polluting the user's workspace with test
fixtures (and worse: leaking pytest tmpdir paths into a "real" record).

This autouse fixture monkeypatches `transfer._repo_root` to the test's
tmp_path for every L14 test. The manifest still gets written (so the
test can assert on it if it wants) — it just lands inside tmp_path
where pytest cleans it up.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_transfer_history(tmp_path: Path, monkeypatch):
    """Redirect transfer.py's manifest-write root to tmp_path. Autouse so
    no L14 test can forget this and quietly pollute the live repo."""
    from agent.skills import transfer
    monkeypatch.setattr(transfer, "_repo_root", lambda: tmp_path)
    yield
