"""1f — reference databases are pinned by CONTENT, not just name+URL.

`download_reference_database` writes a `<local_path>.source.sha256` sidecar
holding the hash of the bytes the URL served. At seal, `_refresh_reference_
databases` folds that sidecar hash (plus available/size from disk) into the
WorkflowSpec so a re-run can verify it got the same bytes. A bundle named
"vep_cache_111_hg38" is otherwise not self-verifying — the URL can serve
different bytes over time.
"""
from __future__ import annotations

import hashlib

import pytest

from agent.mcp_tools.workflow_tools import _refresh_reference_databases


def test_sidecar_sha256_folded_in_at_seal(tmp_path):
    db = tmp_path / "vep_cache"
    db.write_bytes(b"reference-bundle-bytes")
    real_sha = hashlib.sha256(db.read_bytes()).hexdigest()
    (tmp_path / "vep_cache.source.sha256").write_text(real_sha + "  vep_cache\n")

    out = _refresh_reference_databases([{
        "name": "vep_cache_111_hg38", "version": "111",
        "source_url": "https://example.org/vep_cache.tar.gz",
        "local_path": str(db), "available": False,
    }])
    assert len(out) == 1
    rec = out[0]
    assert rec["sha256"] == real_sha, "the sidecar hash must be folded into the record"
    assert rec["available"] is True, "available is re-derived from disk existence"
    assert rec["size_bytes"] == len(b"reference-bundle-bytes")


def test_missing_sidecar_leaves_sha_absent_never_fabricated(tmp_path):
    """No sidecar ⇒ we do NOT invent a hash. Honest absence beats a fake pin."""
    db = tmp_path / "bundle"
    db.write_bytes(b"xyz")
    out = _refresh_reference_databases([{
        "name": "db", "version": "1", "source_url": "u",
        "local_path": str(db),
    }])
    assert "sha256" not in out[0] or out[0].get("sha256") is None
    assert out[0]["available"] is True


def test_absent_local_path_marks_unavailable(tmp_path):
    out = _refresh_reference_databases([{
        "name": "db", "version": "1", "source_url": "u",
        "local_path": str(tmp_path / "never_downloaded"),
    }])
    assert out[0]["available"] is False
    assert "sha256" not in out[0] or out[0].get("sha256") is None
