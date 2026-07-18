"""Phase-3 Piece B — the ONE lifecycle answer, re-earned from artifacts.

Before this, "where is this pipeline?" was scattered across four unlinked places
and the two fields that LOOKED like a state machine (draft env_status /
pipeline_status) were stamped 'in_progress' at birth and never transitioned.
`current_state()` collapses that into one deriver, RE-EARNED every read:

  ABSENT < DRAFT < ENV_BUILT < ENV_FROZEN < SEALED

with the higher states confirmed by injected checks (never trusted-because-stored),
and the injected checks REQUIRED (an unwired call raises, never silently assumes).
Separately, seal now DERIVES WorkflowSpec.pipeline_status from the run instead of
stamping the 'in_progress' default (the "fabricated default authors drift" lesson).
"""
from __future__ import annotations

import pytest

from agent.skills.pipeline_state import (
    ABSENT, DRAFT, ENV_BUILT, ENV_FROZEN, SEALED,
    PipelineState, current_state, state_checks,
)
from agent.skills.spec_writer import derive_pipeline_status


# ── the run-status derivation (shared by seal + renderer) ───────────────────────

@pytest.mark.parametrize("steps,expected", [
    ([], "in_progress"),
    ([{"returncode": 1}], "failed"),
    ([{"returncode": 0, "validation": {"passed": True}}], "fully_validated"),
    ([{"returncode": 0, "validation_status": "passed"},
      {"returncode": 0, "validation": {"passed": True}}], "fully_validated"),
    ([{"returncode": 0, "validation": {"passed": True}}, {"returncode": 0}], "partially_validated"),
    ([{"returncode": 0}], "complete"),
    # a failure anywhere dominates a validated sibling
    ([{"returncode": 0, "validation": {"passed": True}}, {"returncode": 2}], "failed"),
])
def test_derive_pipeline_status(steps, expected):
    assert derive_pipeline_status(steps) == expected


# ── current_state: the decision, with injected checks ──────────────────────────

def _checks(frozen=False, sealed=False):
    """Fake the two re-earned checks so the deriver's decision logic is tested in
    isolation (state_checks itself is tested separately, against real files)."""
    return {"verify_frozen": lambda rk: frozen, "spec_sealed": lambda d: sealed}


def test_absent_when_no_draft():
    assert current_state(None, **_checks()) == ABSENT
    assert current_state({}, **_checks()) == ABSENT


def test_draft_when_bare():
    assert current_state({"pipeline_name": "x"}, **_checks()) == DRAFT


def test_env_built_needs_conda_env_and_an_install_step():
    assert current_state({"conda_env": "e"}, **_checks()) == DRAFT           # no install step yet
    assert current_state({"conda_env": "e", "install_steps": [{"step": 1}]}, **_checks()) == ENV_BUILT


def test_env_frozen_requires_a_verified_pointer():
    d = {"conda_env": "e", "install_steps": [{"step": 1}], "frozen_as": "rk-1"}
    assert current_state(d, **_checks(frozen=True)) == ENV_FROZEN
    # eviction / a forged pointer self-heals DOWN — the check re-earns, so a
    # frozen_as the honesty contract no longer backs does NOT read as frozen
    assert current_state(d, **_checks(frozen=False)) == ENV_BUILT


def test_sealed_is_highest_and_re_earns():
    d = {"conda_env": "e", "install_steps": [{"step": 1}],
         "frozen_as": "rk-1", "sealed_as": ["wf"]}
    assert current_state(d, **_checks(frozen=True, sealed=True)) == SEALED
    # a sealed_as whose spec no longer matches/pins-a-live-env drops BELOW sealed
    assert current_state(d, **_checks(frozen=True, sealed=False)) == ENV_FROZEN


def test_unwired_checks_raise_never_silently_assume():
    """The injected checks are REQUIRED — an unwired call must RAISE, not default
    to a permissive verdict ('gate present in code, absent in effect')."""
    with pytest.raises(TypeError):
        current_state({"pipeline_name": "x"})


# ── state_checks: the real re-earned checks against real deps ──────────────────

class _FakeEnvCache:
    def __init__(self, verified=None, records=None):
        # verified: {request_key: (record_or_None, violations_list)}
        self._verified = verified or {}
        self._records = records or {}
    def lookup_verified(self, key):
        return self._verified.get(key, (None, []))
    def all(self):
        return self._records


def test_verify_frozen_true_only_when_green():
    ec = _FakeEnvCache(verified={
        "green":     ({"image_digest": "sha256:A"}, []),
        "violated":  ({"image_digest": "sha256:A"}, [{"clause": "VALIDATED_IN_IMAGE"}]),
        "missing":   (None, []),
    })
    checks = state_checks(ec, "/nonexistent")
    assert checks["verify_frozen"]("green") is True
    assert checks["verify_frozen"]("violated") is False
    assert checks["verify_frozen"]("missing") is False


def _write_spec(reports_dir, name, *, env_request_key, image_digest):
    import yaml
    p = reports_dir / f"{name}.workflow.yaml"
    p.write_text(yaml.dump({
        "workflow_name": name,
        "env_request_key": env_request_key,
        "envs": [{"request_key": env_request_key, "image_digest": image_digest}],
    }))
    return p


def test_spec_sealed_requires_existence_identity_and_a_live_digest(tmp_path):
    # the sealed spec pins env digest sha256:A via request_key rk-1
    _write_spec(tmp_path, "mywf", env_request_key="rk-1", image_digest="sha256:A")
    ec = _FakeEnvCache(records={"rk-1": {"image_digest": "sha256:A"}})   # digest live in cache
    checks = state_checks(ec, tmp_path)

    # matches: sealed_as names it, frozen_as identity-matches, digest is live
    assert checks["spec_sealed"]({"sealed_as": ["mywf"], "frozen_as": "rk-1"}) is True
    # no sealed_as pointer at all
    assert checks["spec_sealed"]({}) is False
    # names a spec that isn't on disk
    assert checks["spec_sealed"]({"sealed_as": ["ghost"], "frozen_as": "rk-1"}) is False


def test_spec_sealed_rejects_a_colliding_filename_from_another_pipeline(tmp_path):
    """workflow_name is caller-overridable, so two pipelines can write the same
    {name}.workflow.yaml. The spec must IDENTITY-match this pipeline (its
    env_request_key), or a stranger's spec would read as OUR seal."""
    _write_spec(tmp_path, "shared", env_request_key="rk-OTHER", image_digest="sha256:A")
    ec = _FakeEnvCache(records={"rk-OTHER": {"image_digest": "sha256:A"}})
    checks = state_checks(ec, tmp_path)
    # our pipeline froze rk-MINE, but the on-disk spec belongs to rk-OTHER
    assert checks["spec_sealed"]({"sealed_as": ["shared"], "frozen_as": "rk-MINE"}) is False


def test_spec_sealed_false_when_the_pinned_env_is_evicted(tmp_path):
    """SEALED re-earns: a spec whose pinned env digest is no longer in the
    EnvCache is not 'sealed' any more (self-heals down), never a bare os.stat."""
    _write_spec(tmp_path, "mywf", env_request_key="rk-1", image_digest="sha256:GONE")
    ec = _FakeEnvCache(records={"rk-1": {"image_digest": "sha256:STILL_HERE"}})
    checks = state_checks(ec, tmp_path)
    assert checks["spec_sealed"]({"sealed_as": ["mywf"], "frozen_as": "rk-1"}) is False


# ── the pointer writers on a real PipelineState ────────────────────────────────

def _ps(tmp_path):
    return PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})


def test_set_frozen_pointer_writes_and_persists(tmp_path):
    ps = _ps(tmp_path)
    ps.start("p1", "desc")
    ps.set_frozen_pointer("p1", "rk-123")
    assert ps.get_draft("p1")["frozen_as"] == "rk-123"
    # persisted: a fresh PipelineState reloads it
    assert _ps(tmp_path).get_draft("p1")["frozen_as"] == "rk-123"


def test_append_sealed_pointer_is_a_deduped_list(tmp_path):
    ps = _ps(tmp_path)
    ps.start("p1", "desc")
    ps.append_sealed_pointer("p1", "wfA")
    ps.append_sealed_pointer("p1", "wfB")
    ps.append_sealed_pointer("p1", "wfA")            # dup — ignored
    assert ps.get_draft("p1")["sealed_as"] == ["wfA", "wfB"]


def test_pointer_writers_noop_on_unknown_pipeline(tmp_path):
    ps = _ps(tmp_path)
    ps.set_frozen_pointer("ghost", "rk")             # must not raise
    ps.append_sealed_pointer("ghost", "wf")
    assert ps.get_draft("ghost") is None


def test_start_no_longer_stamps_dead_status_fields(tmp_path):
    ps = _ps(tmp_path)
    ps.start("p1", "desc")
    d = ps.get_draft("p1")
    assert "env_status" not in d and "pipeline_status" not in d


def test_lifecycle_pointers_are_blocked_from_patch(tmp_path):
    ps = _ps(tmp_path)
    ps.start("p1", "desc")
    out = ps.patch("p1", {"frozen_as": "rk-forged"})
    assert out.get("code") == "pipeline_state.blocked_keys"
    assert "frozen_as" not in (ps.get_draft("p1") or {})
