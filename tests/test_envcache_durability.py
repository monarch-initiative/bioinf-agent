"""EnvCache durability (P1 — front door / honest floor).

The EnvCache is the store that makes 'solve once, pull by digest' real. Two defects
made it able to SILENTLY ERASE ITSELF: the write was non-atomic (`write_text` in
place — a crash mid-write left a truncated file), and the load swallowed a corrupt
file to `{}`. Together: crash mid-write -> truncated file -> next _load() returns {}
-> next register() rewrites the file with only the new key -> every previously frozen
env record gone, no exception ever raised.

The fix: an ATOMIC write (temp file + os.replace) so our own writes can never produce
a truncated file, and a FAIL-LOUD load that raises on a corrupt file rather than
treating it as empty. These tests pin both halves.
"""
from __future__ import annotations

import os

import pytest

from agent.skills.freeze import EnvCache


def _cache(tmp_path) -> EnvCache:
    return EnvCache(tmp_path / "_env_cache.json")


def test_save_load_round_trips(tmp_path):
    c = _cache(tmp_path)
    c._save({"k1": {"content_digest": "d1"}})
    assert c._load() == {"k1": {"content_digest": "d1"}}
    assert c.lookup("k1") == {"content_digest": "d1"}


def test_load_missing_file_is_empty(tmp_path):
    # A file that does not exist is a legitimate empty cache, NOT corruption.
    assert _cache(tmp_path)._load() == {}


def test_load_raises_on_corrupt_json(tmp_path):
    """The compounding-bug root: a corrupt file must NOT be swallowed to {} (which
    would let the next register() overwrite it). It must fail loud."""
    c = _cache(tmp_path)
    (tmp_path / "_env_cache.json").write_text("{ this is not valid json ")
    with pytest.raises(RuntimeError) as exc:
        c._load()
    assert "corrupt" in str(exc.value).lower()


def test_load_raises_on_non_object_json(tmp_path):
    c = _cache(tmp_path)
    (tmp_path / "_env_cache.json").write_text('["a", "list", "not", "an", "object"]')
    with pytest.raises(RuntimeError):
        c._load()


def test_save_is_atomic_leaves_no_tmp_litter(tmp_path):
    c = _cache(tmp_path)
    c._save({"k": {"content_digest": "d"}})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], leftovers


def test_failed_save_preserves_existing_file(tmp_path, monkeypatch):
    """The atomic guarantee: if the replace step fails, the EXISTING cache is left
    whole (never a half-written file), and no temp litter remains."""
    c = _cache(tmp_path)
    c._save({"keep": {"content_digest": "original"}})
    original = (tmp_path / "_env_cache.json").read_text()

    def _boom(*a, **k):
        raise OSError("simulated crash during os.replace")
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        c._save({"new": {"content_digest": "would-clobber"}})

    # The old file is intact — a reader always sees the whole old file, never a
    # truncated one — and the temp file was cleaned up.
    assert (tmp_path / "_env_cache.json").read_text() == original
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], leftovers


def test_register_on_corrupt_cache_raises_rather_than_erasing(tmp_path):
    """The end-to-end regression for the silent-data-loss bug: with prior records on
    disk and the file then corrupted, register() must RAISE (via the fail-loud load)
    instead of silently rewriting the cache down to only the new record."""
    c = _cache(tmp_path)
    c._save({"env_a": {"content_digest": "a"}, "env_b": {"content_digest": "b"}})
    # Corrupt the file (as a crash mid-write once could have).
    (tmp_path / "_env_cache.json").write_text("{ truncated")
    before = (tmp_path / "_env_cache.json").read_text()
    with pytest.raises(RuntimeError):
        c.register("env_c", {"content_digest": "c"})
    # The corrupt file was NOT overwritten — the records are recoverable, not erased.
    assert (tmp_path / "_env_cache.json").read_text() == before
