"""
G3 (honesty-refactor, Tier B): universal file-type-agnostic lineage.

The workflow graph already declares each step's inputs and outputs as paths.
I8.composition_coherence enforces PATH lineage (every input traces to a prior
output or external). But that's only HALF the contract: an agent could
silently mutate a file at the same path between Step N and Step N+1, and the
path-only check would still pass — both steps reference '/tmp/reads.bam',
but the bytes Step N+1 read are not the bytes Step N wrote.

The fix: at production time, hash each detected_output and record on the step
as `output_sha256: {path: sha}`. At seal time, walk the graph: for every
consumer input that matches a prior step's output path, re-hash the file on
disk and compare to the producer's recorded hash. Mismatch → mutation.

CHEAT GUARD LEVEL: L11 — universal lineage (file-type-agnostic).

This contract is file-type-AGNOSTIC by design: works for BAM, VCF, parquet,
.weird, anything. The agent's "easy pipeline building from unknown tools"
emergent property is preserved — per-file-type lineage (BAM @PG chain, VCF
sample IDs) remains optional, opt-in per validator.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent.skills.spec_writer import check_invariants


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _step(n: int, *, inputs=None, detected_outputs=None,
          output_sha256=None) -> dict:
    """Build a minimal valid pipeline_step. Avoids per-test scaffold drift."""
    s: dict = {
        "step": n, "tool": "tool", "command": f"tool step {n}",
        "returncode": 0,
        "resource_usage": {"wall_seconds": 0.01, "peak_rss_mb": 1.0,
                            "peak_cpu_pct": 1.0},
    }
    if inputs is not None:
        s["inputs"] = inputs
    if detected_outputs is not None:
        s["detected_outputs"] = detected_outputs
    if output_sha256 is not None:
        s["output_sha256"] = output_sha256
    return s


# ---------------------------------------------------------------------------
# Positive path: matched hashes → no violation
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_matched_hashes_pass(tmp_path):
    """Producer recorded sha=X; consumer's input file on disk has sha=X. The
    workflow's same-path-same-bytes contract holds; no lineage violation."""
    content = b"BAM\x01reads\n"
    p = tmp_path / "reads.bam"
    p.write_bytes(content)

    spec = {"pipeline_steps": [
        _step(1, detected_outputs=[str(p)],
              output_sha256={str(p): _sha(content)}),
        _step(2, inputs=[{"path": str(p)}],
              detected_outputs=[str(tmp_path / "out.vcf")]),
    ]}
    # The consumer's output file doesn't exist on disk — that's fine; the
    # lineage check is about the input lineage, not output validation.
    v = [x for x in check_invariants(spec) if "lineage" in x.get("invariant", "")]
    assert v == [], f"matched hashes wrongly flagged: {v}"


# ---------------------------------------------------------------------------
# The headline cheat: mutation between producer and consumer
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_mutation_between_steps_caught(tmp_path):
    """Producer recorded sha=X; agent silently overwrites the file with bytes
    sha=Y after step 1 but before seal time. Consumer's input path still
    matches producer's output path (I8.composition_coherence sees no orphan),
    but the bytes diverged. L11 catches it."""
    original = b"original BAM bytes\n"
    p = tmp_path / "reads.bam"
    p.write_bytes(original)
    original_sha = _sha(original)

    # The cheat: rewrite the file with different bytes.
    p.write_bytes(b"forged BAM bytes\n")

    spec = {"pipeline_steps": [
        _step(1, detected_outputs=[str(p)],
              output_sha256={str(p): original_sha}),
        _step(2, inputs=[{"path": str(p)}],
              detected_outputs=[str(tmp_path / "out.vcf")]),
    ]}
    v = [x for x in check_invariants(spec) if x["invariant"] == "I8.lineage_mutated"]
    assert len(v) == 1, f"mutation not detected: violations={check_invariants(spec)}"
    assert v[0]["path"] == str(p)
    assert v[0]["producer_step"] == 1
    assert v[0]["producer_sha256"] == original_sha
    assert v[0]["disk_sha256"] == _sha(b"forged BAM bytes\n")


# ---------------------------------------------------------------------------
# Missing file: producer recorded it; by seal time the file is gone
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_consumer_input_missing_caught(tmp_path):
    """Producer step's output is referenced by a consumer step's input, but
    by seal time the file is no longer on disk. The consumer's input is a
    dangling reference."""
    p = tmp_path / "intermediate.bam"
    p.write_bytes(b"x")
    sha = _sha(b"x")
    p.unlink()                # gone before seal

    spec = {"pipeline_steps": [
        _step(1, detected_outputs=[str(p)],
              output_sha256={str(p): sha}),
        _step(2, inputs=[{"path": str(p)}]),
    ]}
    v = [x for x in check_invariants(spec) if x["invariant"] == "I8.lineage_missing"]
    assert len(v) == 1, f"missing input not detected: {check_invariants(spec)}"


# ---------------------------------------------------------------------------
# Back-compat: producer without output_sha256 → check is skipped (no false-positive)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_back_compat_no_output_sha256_skips_check(tmp_path):
    """A recorded run from before output_sha256 was captured (or a run where
    hash_outputs failed for some reason) should NOT retroactively fail at
    seal. The lineage check is an honesty guard, not a coverage gate."""
    p = tmp_path / "anything.txt"
    p.write_text("whatever")

    spec = {"pipeline_steps": [
        # Producer recorded the path but NOT a sha256.
        _step(1, detected_outputs=[str(p)]),
        _step(2, inputs=[{"path": str(p)}]),
    ]}
    v = [x for x in check_invariants(spec) if "lineage" in x.get("invariant", "")]
    assert v == [], f"back-compat (no producer sha256) wrongly flagged: {v}"


# ---------------------------------------------------------------------------
# Multi-hop lineage: hash chain holds through Step 1 → Step 2 → Step 3
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_multi_hop_chain_no_violation(tmp_path):
    """An honest 3-step chain — step 1 produces a, step 2 consumes a and
    produces b, step 3 consumes b — passes lineage as long as both files
    still hold their producer's recorded bytes."""
    a_bytes = b"intermediate A\n"
    b_bytes = b"intermediate B\n"
    a = tmp_path / "a.bam"
    b = tmp_path / "b.vcf"
    a.write_bytes(a_bytes)
    b.write_bytes(b_bytes)

    spec = {"pipeline_steps": [
        _step(1, detected_outputs=[str(a)], output_sha256={str(a): _sha(a_bytes)}),
        _step(2, inputs=[{"path": str(a)}], detected_outputs=[str(b)],
              output_sha256={str(b): _sha(b_bytes)}),
        _step(3, inputs=[{"path": str(b)}]),
    ]}
    v = [x for x in check_invariants(spec) if "lineage" in x.get("invariant", "")]
    assert v == [], f"honest multi-hop chain wrongly flagged: {v}"


@pytest.mark.integration
def test_multi_hop_chain_mid_mutation_caught(tmp_path):
    """The mid-chain mutation cheat: 'b' is honest, but 'a' was silently
    overwritten between step 1 and step 2. The mutation manifests on the
    step 2 input lineage."""
    a_bytes = b"intermediate A\n"
    a = tmp_path / "a.bam"
    b = tmp_path / "b.vcf"
    a.write_bytes(a_bytes)
    b.write_bytes(b"intermediate B\n")
    a_original_sha = _sha(a_bytes)
    # Mutate 'a' after step 1 recorded it.
    a.write_bytes(b"forged A\n")

    spec = {"pipeline_steps": [
        _step(1, detected_outputs=[str(a)], output_sha256={str(a): a_original_sha}),
        _step(2, inputs=[{"path": str(a)}], detected_outputs=[str(b)],
              output_sha256={str(b): _sha(b"intermediate B\n")}),
        _step(3, inputs=[{"path": str(b)}]),
    ]}
    v = [x for x in check_invariants(spec) if x["invariant"] == "I8.lineage_mutated"]
    assert len(v) == 1
    assert v[0]["path"] == str(a)
    assert v[0]["producer_step"] == 1


# ---------------------------------------------------------------------------
# Overwrite semantics: when the same path is produced by two steps, the LATER
# one in step-order is the truth Source of Truth for the consumer.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_path_reproduced_in_later_step_wins(tmp_path):
    """Step 1 produces reads.bam with sha=A; step 3 OVERWRITES reads.bam
    with sha=B (legal, the workflow chose to re-sort); step 4 consumes
    reads.bam with disk sha=B. The lineage clause for step 4 must compare
    against step 3's sha=B (the LAST producer), not step 1's sha=A."""
    p = tmp_path / "reads.bam"
    # Step 1 wrote sha_a, step 3 overwrote with sha_b. By seal time disk = b.
    sha_a = _sha(b"version A\n")
    sha_b = _sha(b"version B\n")
    p.write_bytes(b"version B\n")

    spec = {"pipeline_steps": [
        _step(1, detected_outputs=[str(p)], output_sha256={str(p): sha_a}),
        _step(2),
        _step(3, detected_outputs=[str(p)], output_sha256={str(p): sha_b}),
        _step(4, inputs=[{"path": str(p)}]),
    ]}
    v = [x for x in check_invariants(spec) if "lineage" in x.get("invariant", "")]
    assert v == [], f"later-producer-wins semantics violated: {v}"


# ---------------------------------------------------------------------------
# The wiring (runtime capture): hash_outputs records sha256 on detected_outputs
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_env_manager_hash_outputs_captures_real_bytes(tmp_path):
    """Sanity-pin the runtime capture: env_manager.hash_outputs returns the
    actual sha256 of each on-disk file. Without this, no producer step would
    ever record output_sha256 and the lineage check would be moot."""
    from agent.skills.env_manager import EnvManager

    a = tmp_path / "a.txt"
    b = tmp_path / "b.bam"
    a.write_text("alpha")
    b.write_bytes(b"\x00\x01\x02")

    out = EnvManager.hash_outputs([str(a), str(b), str(tmp_path / "missing")])
    # Missing files are silently skipped (back-compat with steps producing
    # directories or temp files cleaned before this call).
    assert set(out.keys()) == {str(a), str(b)}, out
    assert out[str(a)] == hashlib.sha256(b"alpha").hexdigest()
    assert out[str(b)] == hashlib.sha256(b"\x00\x01\x02").hexdigest()
