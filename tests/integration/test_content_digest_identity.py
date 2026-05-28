"""
The CONTENT DIGEST is the artifact's stable identity — 'what was actually GOT'.
Two builds with the same inputs (lock + long-tail commands + platform + engine
+ base image) must produce identical digests. Different inputs must produce
different digests. This is the reproducible anchor that verify-by-rebuild
exercises and that the EnvCache uses as the secondary key (after request_key).

Contract:
  1. Stable across process runs and key order.
  2. Different lock → different digest (any package version change).
  3. Different long-tail (binary install command, etc.) → different digest.
  4. Different platform → different digest.
  5. Different base image → different digest (audit#2 fix; pre-fix the digest
     was OS-layer-blind, so two builds on different bases collided).
  6. apt_snapshot, when set, contributes; absent, doesn't.

Why integration, not unit: digest determinism is a critical reproducibility
property the trust paradigm rests on. A regression that collapses ANY
discriminating field (the audit#2 base-image was exactly this) makes two
genuinely-different artifacts cache-collide. The integration value is one
test per axis — if any axis stops discriminating, exactly one test fails.
"""
from __future__ import annotations

import pytest

from agent.skills.freeze import compute_content_digest


def _base_parts() -> dict:
    """The minimal identity dict an EnvBuild emits to compute_content_digest."""
    return {
        "lock":     "abc123lock",
        "longtail": ["curl -sL https://example.org/seqkit.tar.gz | tar xz"],
        "platform": "linux/amd64",
        "engine":   "pixi",
        "base":     "ubuntu:22.04@sha256:deadbeef",
    }


@pytest.mark.integration
def test_same_inputs_produce_same_digest():
    """Stability — the digest is a pure function of the parts dict."""
    a = compute_content_digest(_base_parts())
    b = compute_content_digest(_base_parts())
    assert a == b
    assert a.startswith("sha256:")


@pytest.mark.integration
def test_digest_is_order_independent_for_keys():
    """sort_keys=True in the implementation — dict key order must not
    change the digest. (This is a python-level concern; modern python
    preserves insertion order so a regression to e.g. an unsorted dumps
    would silently break determinism.)"""
    p1 = dict(_base_parts())
    # Force a different insertion order
    p2 = {"engine": p1["engine"], "base": p1["base"],
          "platform": p1["platform"], "longtail": p1["longtail"],
          "lock": p1["lock"]}
    assert compute_content_digest(p1) == compute_content_digest(p2)


@pytest.mark.integration
def test_lock_change_shifts_digest():
    """A different package-version closure (lock_sha256 changes) is a
    different artifact."""
    p = _base_parts()
    a = compute_content_digest(p)
    p2 = dict(p, lock="DIFFERENT_LOCK_xyz789")
    assert a != compute_content_digest(p2)


@pytest.mark.integration
def test_longtail_change_shifts_digest():
    """A different binary-install command (or release URL) is a different
    artifact. The long-tail is the non-conda installs the bakery ran."""
    p = _base_parts()
    p2 = dict(p, longtail=["curl -sL https://example.org/seqkit_v2.tar.gz | tar xz"])
    assert compute_content_digest(p) != compute_content_digest(p2)


@pytest.mark.integration
def test_platform_change_shifts_digest():
    """linux/amd64 vs linux/arm64 are different artifacts (different
    binaries inside)."""
    p = _base_parts()
    p2 = dict(p, platform="linux/arm64")
    assert compute_content_digest(p) != compute_content_digest(p2)


@pytest.mark.integration
def test_base_image_change_shifts_digest():
    """Audit #2 fix: the digest-pinned BASE IMAGE is part of the artifact
    identity. Two builds on different bases must NOT collide. Pre-fix the
    digest was OS-layer-blind — same lock + same longtail on ubuntu:20
    vs ubuntu:22 hashed identically."""
    p = _base_parts()
    p2 = dict(p, base="ubuntu:20.04@sha256:cafef00d")
    assert compute_content_digest(p) != compute_content_digest(p2), \
        "base image change did not shift content_digest (audit#2 regression)"


@pytest.mark.integration
def test_apt_snapshot_when_set_shifts_digest():
    """When apt_snapshot is pinned, the apt layer IS reproducible (snapshot
    URL serves byte-identical bytes), so the snapshot timestamp folds into
    the digest. Without it the digest stays unchanged (back-compat)."""
    p = _base_parts()
    base = compute_content_digest(p)
    p_with_snap = dict(p, apt_snapshot="20250528T120000")
    p_other_snap = dict(p, apt_snapshot="20251015T120000")
    # An apt_snapshot field shifts the digest from the no-snapshot baseline
    assert compute_content_digest(p_with_snap) != base
    # Two different snapshots are different artifacts
    assert compute_content_digest(p_with_snap) != compute_content_digest(p_other_snap)


@pytest.mark.integration
def test_engine_change_shifts_digest():
    """pixi vs micromamba is structurally a different artifact (different
    activation script baked into the runtime image)."""
    p = _base_parts()
    p2 = dict(p, engine="micromamba")
    assert compute_content_digest(p) != compute_content_digest(p2)
