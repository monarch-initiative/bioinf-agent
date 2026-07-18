"""Phase-3 Piece A — the seal write-guard.

A sealed `{workflow_name}.workflow.yaml` is a digest-pinned provenance artifact.
`write_workflow_spec` overwrites it with no exists-check, so re-sealing over an
existing sealed spec could silently DESTROY a prior artifact's provenance. The
guard (`workflow_tools._guard_spec_overwrite`) refuses that at the terminal WRITE:

  - no existing spec            -> write (first seal)
  - same env identity, no
    evidence dropped            -> write (locus ACCRETION — the documented flow)
  - different env identity, or
    evidence dropped, no
    supersede                   -> REFUSE seal.would_clobber_sealed_spec
  - ...with supersede=True      -> PRESERVE the prior spec under a .superseded.N
                                   name (never destroyed), stamp a note, write

These are UNIT tests over the pure decision helpers against a REAL tmp dir (real
files on disk) — the guard's logic lives entirely in the (wf, out_dir, supersede)
inputs, so this exercises every branch without the full seal machinery. The
codebase's rule that a reframed hole gets a proportionate fix (a WRITE gate, not
a 15-site draft lock) is what these tests lock in.
"""
from __future__ import annotations

import yaml

from agent.mcp_tools.workflow_tools import (
    _guard_spec_overwrite,
    _next_superseded_path,
    _spec_env_identity,
    _validated_step_count,
)


# ── fixtures: minimal spec dicts, only the fields the guard reads ───────────────

def _spec(*, name="wf1", image_digest="sha256:AAA", content_digest="cd-AAA",
          request_key="rk-1", validated_steps=1):
    """A minimal WorkflowSpec-shaped dict carrying exactly what the guard reads:
    the env identity (envs[].image_digest + env_content_digest + env_request_key)
    and enough validated pipeline_steps to count."""
    return {
        "workflow_name": name,
        "env_request_key": request_key,
        "env_content_digest": content_digest,
        "envs": [{"request_key": request_key, "image": "img", "image_digest": image_digest}],
        "pipeline_steps": [{"command": f"step{i}", "validation": {"passed": True}}
                           for i in range(validated_steps)],
    }


def _write(out_dir, spec):
    p = out_dir / f"{spec['workflow_name']}.workflow.yaml"
    p.write_text(yaml.dump(spec, sort_keys=False))
    return p


# ── the env-identity fingerprint ───────────────────────────────────────────────

def test_env_identity_is_stable_for_the_same_env():
    a, b = _spec(), _spec()
    assert _spec_env_identity(a) == _spec_env_identity(b)


def test_env_identity_differs_for_a_rebuilt_env_same_request_new_content():
    """A rebuilt env keeps its request_key (derived from the REQUEST) but gets a
    new content/image digest — the exact case the user said must require an
    explicit supersede, not read as a silent additive update."""
    original = _spec(image_digest="sha256:AAA", content_digest="cd-AAA", request_key="rk-1")
    rebuilt  = _spec(image_digest="sha256:BBB", content_digest="cd-BBB", request_key="rk-1")
    assert _spec_env_identity(original) != _spec_env_identity(rebuilt)


def test_env_identity_differs_for_a_wholly_different_env():
    assert _spec_env_identity(_spec(request_key="rk-1")) != _spec_env_identity(_spec(request_key="rk-2"))


def test_validated_step_count_counts_only_evidence_bearing_steps():
    spec = {"pipeline_steps": [
        {"command": "a", "validation": {"passed": True}},
        {"command": "b", "validation_status": "passed"},
        {"command": "c"},                                   # no evidence
        "not-a-dict",                                       # shape junk
    ]}
    assert _validated_step_count(spec) == 2


# ── the guard's decisions ──────────────────────────────────────────────────────

def test_first_seal_writes_through(tmp_path):
    proceed, refusal = _guard_spec_overwrite(_spec(), tmp_path, supersede=False)
    assert proceed is True and refusal is None


def test_accretion_same_env_same_evidence_writes_through(tmp_path):
    _write(tmp_path, _spec(validated_steps=1))
    proceed, refusal = _guard_spec_overwrite(_spec(validated_steps=1), tmp_path, supersede=False)
    assert proceed is True and refusal is None


def test_accretion_same_env_MORE_evidence_writes_through(tmp_path):
    """The documented locus-accretion flow: seal locally, later run on cluster,
    re-seal so the dashboard accretes the new locus. Same env, more validated
    steps — must write without a supersede."""
    _write(tmp_path, _spec(validated_steps=1))
    proceed, refusal = _guard_spec_overwrite(_spec(validated_steps=3), tmp_path, supersede=False)
    assert proceed is True and refusal is None


def test_dropping_evidence_over_the_same_env_refuses_without_supersede(tmp_path):
    """Same env but FEWER validated steps is a thinner spec replacing a richer
    one — that is evidence loss, not accretion, so it must not silently write."""
    _write(tmp_path, _spec(validated_steps=3))
    proceed, refusal = _guard_spec_overwrite(_spec(validated_steps=1), tmp_path, supersede=False)
    assert proceed is False
    assert refusal["code"] == "seal.would_clobber_sealed_spec"


def test_different_env_refuses_and_names_supersede(tmp_path):
    _write(tmp_path, _spec(image_digest="sha256:AAA", content_digest="cd-AAA"))
    newer = _spec(image_digest="sha256:BBB", content_digest="cd-BBB")   # rebuilt env
    proceed, refusal = _guard_spec_overwrite(newer, tmp_path, supersede=False)
    assert proceed is False
    assert refusal["code"] == "seal.would_clobber_sealed_spec"
    assert refusal["success"] is False
    # the refusal must TELL the agent how to proceed, and expose both identities
    assert "supersede=True" in refusal["error"]
    assert refusal["existing_env_identity"] != refusal["new_env_identity"]
    # and it must not have written anything yet
    assert not (tmp_path / "wf1.superseded.1.workflow.yaml").exists()


def test_supersede_preserves_the_prior_spec_and_stamps_a_note(tmp_path):
    prior = _write(tmp_path, _spec(image_digest="sha256:AAA", content_digest="cd-AAA"))
    prior_bytes = prior.read_text()
    newer = _spec(image_digest="sha256:BBB", content_digest="cd-BBB")
    proceed, refusal = _guard_spec_overwrite(newer, tmp_path, supersede=True)
    assert proceed is True and refusal is None
    # the prior spec is PRESERVED, not destroyed
    preserved = tmp_path / "wf1.superseded.1.workflow.yaml"
    assert preserved.exists()
    assert preserved.read_text() == prior_bytes
    # the original path is now free for the caller (seal) to write the new spec
    assert not (tmp_path / "wf1.workflow.yaml").exists()
    # the new spec carries a supersession note (persisted — WorkflowSpec is extra=allow)
    assert newer["superseded"]["replaced_spec"] == "wf1.superseded.1.workflow.yaml"
    assert newer["superseded"]["prior_env_identity"] == sorted(_spec_env_identity(_spec()))


def test_repeated_supersede_never_collides(tmp_path):
    _write(tmp_path, _spec(content_digest="cd-A", image_digest="sha256:A"))
    _guard_spec_overwrite(_spec(content_digest="cd-B", image_digest="sha256:B"), tmp_path, supersede=True)
    # simulate the caller having written the new current spec, then supersede AGAIN
    _write(tmp_path, _spec(content_digest="cd-B", image_digest="sha256:B"))
    _guard_spec_overwrite(_spec(content_digest="cd-C", image_digest="sha256:C"), tmp_path, supersede=True)
    assert (tmp_path / "wf1.superseded.1.workflow.yaml").exists()
    assert (tmp_path / "wf1.superseded.2.workflow.yaml").exists()


def test_next_superseded_path_picks_the_first_free_integer(tmp_path):
    assert _next_superseded_path(tmp_path, "wf1").name == "wf1.superseded.1.workflow.yaml"
    (tmp_path / "wf1.superseded.1.workflow.yaml").write_text("x")
    assert _next_superseded_path(tmp_path, "wf1").name == "wf1.superseded.2.workflow.yaml"


def test_one_env_many_workflows_do_not_collide(tmp_path):
    """Sealing workflow B (different name) from the same draft/env is legal today
    (seal doesn't pop the draft). Different filenames => the guard never fires."""
    _write(tmp_path, _spec(name="wfA"))
    proceed, refusal = _guard_spec_overwrite(_spec(name="wfB"), tmp_path, supersede=False)
    assert proceed is True and refusal is None


def test_a_corrupt_prior_spec_is_treated_as_a_clobber_not_a_crash(tmp_path):
    """If the on-disk prior spec can't be parsed, its identity is empty and the
    new (non-empty) identity differs — so the guard refuses rather than raising,
    and a supersede still preserves the unreadable bytes."""
    (tmp_path / "wf1.workflow.yaml").write_text(":\n  not: [valid: yaml")
    proceed, refusal = _guard_spec_overwrite(_spec(), tmp_path, supersede=False)
    assert proceed is False
    assert refusal["code"] == "seal.would_clobber_sealed_spec"
