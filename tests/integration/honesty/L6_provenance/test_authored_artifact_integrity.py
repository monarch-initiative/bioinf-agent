"""
G1 (honesty-refactor, Tier A): authored_artifact seal-time sha256 re-check.

The respine retired I9 ("re-hash every authored artifact at finalize") because
in Layer 1 (env) the artifact is baked INTO the shipped image, so the image's
bytes ARE the ground truth — 'install==ship' makes re-hashing redundant.

But Layer 2 (workflow) consumes artifacts OUTSIDE the image: driver scripts,
synthetic test inputs, hand-staged BAM/VCF — they live on disk and downstream
steps read the file (not the spec). The 'install==ship' collapse does NOT
cover them. Between `stage_authored_artifact(sha256=X)` at stage time and
`seal_workflow()` at write time, an agent could overwrite the on-disk bytes;
the spec would still record sha256=X but the file would have different bytes.
The shipped record would LIE about the artifact's identity.

CHEAT GUARD LEVEL: L6.b — Authored-artifact mutation after stage.

This test pins seal-time re-hash: a mutated, missing, or unreadable artifact
becomes an I8 violation that `seal_workflow` refuses on.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent.skills.spec_writer import check_invariants


def _record(path: Path, content: bytes) -> dict:
    """Stage-time record shape — sha256 + path + role, as
    stage_authored_artifact would persist it."""
    path.write_bytes(content)
    return {
        "path":   str(path),
        "role":   "driver_script",
        "sha256": hashlib.sha256(content).hexdigest(),
    }


@pytest.mark.integration
def test_unmutated_artifact_passes(tmp_path):
    """Happy path: stage, do nothing, seal — no I8 violation. (Base contract:
    the check doesn't fire when the truth claim still matches the disk.)"""
    spec = {"authored_artifacts": [
        _record(tmp_path / "driver.sh", b"#!/bin/sh\necho ok\n"),
    ]}
    violations = check_invariants(spec)
    artifact_v = [v for v in violations if "authored_artifact" in v.get("invariant", "")]
    assert artifact_v == [], \
        f"unmutated artifact wrongly flagged: {artifact_v}"


@pytest.mark.integration
def test_mutated_artifact_caught(tmp_path):
    """The headline cheat: stage with sha256=X, mutate bytes after, seal sees
    the mismatch. Pre-fix this was silently accepted."""
    path = tmp_path / "driver.sh"
    rec = _record(path, b"#!/bin/sh\necho original\n")
    # The cheat: rewrite the file after staging.
    path.write_bytes(b"#!/bin/sh\necho malicious\n")

    violations = check_invariants({"authored_artifacts": [rec]})
    mutated = [v for v in violations if v["invariant"] == "I8.authored_artifact_mutated"]
    assert len(mutated) == 1, \
        f"mutation not detected: violations={violations}"
    assert mutated[0]["path"] == str(path)
    assert mutated[0]["recorded_sha256"] != mutated[0]["disk_sha256"]


@pytest.mark.integration
def test_deleted_artifact_caught(tmp_path):
    """A staged artifact whose path was deleted from disk by seal time. The
    downstream reference would be a dangling file pointer — caught as missing."""
    path = tmp_path / "drv.py"
    rec = _record(path, b"print('hi')\n")
    path.unlink()

    violations = check_invariants({"authored_artifacts": [rec]})
    missing = [v for v in violations if v["invariant"] == "I8.authored_artifact_missing"]
    assert len(missing) == 1, \
        f"deletion not detected: violations={violations}"


@pytest.mark.integration
def test_legacy_entry_without_sha256_skipped(tmp_path):
    """Back-compat: an authored_artifact record without a `sha256` field is
    skipped (nothing to re-anchor against). Pre-existing specs from before
    sha256 was captured should not retroactively fail seal."""
    path = tmp_path / "old.sh"
    path.write_text("legacy")
    spec = {"authored_artifacts": [
        {"path": str(path), "role": "driver_script"},   # no sha256
    ]}
    violations = check_invariants(spec)
    artifact_v = [v for v in violations if "authored_artifact" in v.get("invariant", "")]
    assert artifact_v == [], \
        f"legacy (no-sha256) entry wrongly flagged: {artifact_v}"


@pytest.mark.integration
def test_check_workflow_invariants_includes_artifact_check(tmp_path):
    """The Layer-2 entry point seal_workflow uses (check_workflow_invariants)
    MUST surface the artifact-integrity violations — otherwise the I8.*
    invariant ID would let seal_workflow refuse for one I8 sub-check but pass
    for another, which would be a contract leak."""
    from agent.skills.spec_writer import check_workflow_invariants

    path = tmp_path / "muted.sh"
    rec = _record(path, b"#!/bin/sh\nexit 0\n")
    path.write_bytes(b"#!/bin/sh\nexit 1\n")   # mutation

    violations = check_workflow_invariants({"authored_artifacts": [rec]})
    mutated = [v for v in violations if v["invariant"] == "I8.authored_artifact_mutated"]
    assert len(mutated) == 1, \
        f"check_workflow_invariants did not surface artifact mutation: {violations}"


@pytest.mark.integration
def test_multiple_artifacts_independently_checked(tmp_path):
    """Each artifact is checked independently — one OK + one mutated + one
    missing should produce exactly the two violations, with the OK one silent."""
    ok = _record(tmp_path / "good.sh", b"a\n")
    mut = _record(tmp_path / "bad.sh",  b"b\n")
    gone = _record(tmp_path / "absent.sh", b"c\n")
    # Mutate the second.
    (tmp_path / "bad.sh").write_bytes(b"BAD\n")
    # Delete the third.
    (tmp_path / "absent.sh").unlink()

    violations = check_invariants({"authored_artifacts": [ok, mut, gone]})
    artifact_v = [v for v in violations if "authored_artifact" in v.get("invariant", "")]
    inv_ids = sorted(v["invariant"] for v in artifact_v)
    assert inv_ids == ["I8.authored_artifact_missing", "I8.authored_artifact_mutated"], \
        f"expected one missing + one mutated violation, got: {inv_ids}"
