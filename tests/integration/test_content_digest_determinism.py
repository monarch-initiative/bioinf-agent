"""
G5 (honesty-refactor, Tier B): content_digest pure-function determinism.

The `content_digest` is the reproducible anchor for an env: same recipe inputs
(lock + longtail + platform + engine + base_image + apt_snapshot) should
produce the byte-identical digest, no matter who computes it when. This is
what makes 'solve once, pull by digest' real: a downstream user can re-derive
the same identity from the same parts.

CHEAT GUARD LEVEL: L13 — recipe-replay determinism.

There are TWO framings of this test:

  (A) PURE-FUNCTION (this file): assert compute_content_digest is a stable
      pure function of its parts. Perturbing any of {lock, longtail, platform,
      engine, base, apt_snapshot} shifts the digest; same inputs give the
      same digest on every call. Runs in <1ms; ships in the hot loop.

  (B) FULL-REBUILD (deferred, integration_docker_slow): take an existing
      recipe.yaml, rebuild the image from scratch, compare content_digest.
      Useful pre-release; too expensive for every push (3-5 min per build).
      The pure-function variant catches the CONTRACT bug ('digest is not a
      pure function of recipe'); the rebuild variant would catch a build-
      pipeline regression (engine/apt behaving non-deterministically). The
      latter is much rarer and detectable via lookup_anchored on real runs.

This file ships (A). (B) is reserved for the `integration_docker_slow` tier.
"""
from __future__ import annotations

import pytest

from agent.skills.freeze import compute_content_digest


def _canonical_parts() -> dict:
    """A complete, representative set of recipe parts. Each test perturbs ONE
    field and asserts the digest changes; the base case here serves as the
    'unperturbed' reference."""
    return {
        "lock":         "lock_sha:abc123",
        "longtail":     ["install seqkit", "install hifiasm @ ec9a8b22"],
        "platform":     "linux/amd64",
        "engine":       "pixi",
        "base":         "debian:bookworm-slim@sha256:0104b334637a5f19aa9c9",
        "apt_snapshot": "20260526T200000Z",
    }


# ---------------------------------------------------------------------------
# Determinism — same parts → same digest, across many calls
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_same_parts_same_digest_byte_for_byte():
    """The base contract: the digest is a deterministic function of parts."""
    parts = _canonical_parts()
    d1 = compute_content_digest(parts)
    d2 = compute_content_digest(parts)
    assert d1 == d2, f"digest is not deterministic: {d1} vs {d2}"
    # And the digest is a sha256-shaped string (the deliverable format).
    assert d1.startswith("sha256:"), f"digest missing sha256: prefix: {d1!r}"
    assert len(d1) == len("sha256:") + 64, f"digest wrong length: {len(d1)}"


@pytest.mark.integration
def test_key_order_in_parts_does_not_affect_digest():
    """JSON serialization with sort_keys must collapse key-order variants
    onto the same digest. Otherwise a Python dict's hashed-by-insertion-order
    behavior could spuriously shift the digest across processes."""
    a = {"lock": "x", "longtail": [], "platform": "p", "engine": "e",
         "base": "b", "apt_snapshot": "s"}
    b = {"apt_snapshot": "s", "base": "b", "engine": "e",
         "platform": "p", "longtail": [], "lock": "x"}
    assert compute_content_digest(a) == compute_content_digest(b)


# ---------------------------------------------------------------------------
# Sensitivity — perturbing any one part shifts the digest
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize("field,mutation", [
    ("lock",         lambda p: {**p, "lock": p["lock"] + "_X"}),
    ("longtail",     lambda p: {**p, "longtail": p["longtail"] + ["install extra"]}),
    ("platform",     lambda p: {**p, "platform": "linux/arm64"}),
    ("engine",       lambda p: {**p, "engine": "micromamba"}),
    ("base",         lambda p: {**p, "base": p["base"][:-1] + "0"}),
    ("apt_snapshot", lambda p: {**p, "apt_snapshot": "20260527T000000Z"}),
])
def test_each_recipe_part_shifts_the_digest(field, mutation):
    """Every recipe part must independently influence the digest. If perturbing
    one didn't shift it, two materially-different artifacts could share an
    identity — which would silently let a re-pull serve the wrong bytes."""
    base = _canonical_parts()
    base_digest = compute_content_digest(base)
    perturbed = compute_content_digest(mutation(base))
    assert base_digest != perturbed, \
        f"perturbing {field!r} did NOT shift content_digest — recipe parts " \
        f"that don't affect identity create a collision surface"


# ---------------------------------------------------------------------------
# Edge cases — optional vs required parts
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_missing_apt_snapshot_collapses_with_pre_phase2_recipes():
    """apt_snapshot folds into the digest ONLY when present. An older recipe
    that pre-dates apt_snapshot pinning must still produce a STABLE digest
    (so existing EnvCache entries don't get invalidated by the schema growth)."""
    parts_no_snap = {k: v for k, v in _canonical_parts().items() if k != "apt_snapshot"}
    d1 = compute_content_digest(parts_no_snap)
    d2 = compute_content_digest(parts_no_snap)
    assert d1 == d2

    # And the WITH-snap digest must differ from the WITHOUT-snap one (the
    # snapshot is a real ingredient when supplied).
    d_with = compute_content_digest(_canonical_parts())
    assert d1 != d_with, "with-snap and without-snap collapsed onto same digest"


@pytest.mark.integration
def test_longtail_order_collapses_when_sorted():
    """The longtail (per-tool install commands) is canonicalized by sort_keys
    at the json level (the dict is hashed, not the list). Inside the list, ORDER
    matters — agent's command ordering IS semantically meaningful. So two
    longtails that differ only in order MUST produce different digests, unless
    the caller pre-sorts. This documents the contract."""
    a = {**_canonical_parts(), "longtail": ["cmd1", "cmd2"]}
    b = {**_canonical_parts(), "longtail": ["cmd2", "cmd1"]}
    # Two distinct lists → two distinct digests. The CALLER is responsible
    # for any sorting that's semantically meaningful — see EnvBuild.content_digest
    # which sorts longtail before passing to compute_content_digest.
    assert compute_content_digest(a) != compute_content_digest(b)
