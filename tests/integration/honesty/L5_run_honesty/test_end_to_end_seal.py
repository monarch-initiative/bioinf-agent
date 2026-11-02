"""
G2 (honesty-refactor, Tier A): end-to-end seal-workflow wiring test.

Every other integration test mocks at a narrow boundary. We had no test
that exercises the full draft → seal_workflow → WorkflowSpec write loop in
one linear flow, asserting that an HONEST run-through actually lands a
complete, honest WorkflowSpec on disk AND that the SAME setup with one
field cheated gets refused.

This is the "positive path through the full lifecycle" test the suite was
missing. It uses a programmatically-assembled validated-run draft (real
on-disk authored artifacts so G1's integrity check passes, real EnvCache
entry so the freeze pin resolves, the step recorded with its template so the seal
derives the how-to; no real conda env is needed), and calls seal_workflow.

CHEAT GUARD LEVELS:
  L5a (run-locus: shipped-image digest match)
  L6b (authored-artifact integrity)
  L8  (attestation predicate: env_image_digest, env_content_digest pinned)
  L9  (HOWTO/guide: rendered from the validated run)

The mutation half asserts that downgrading any one of these breaks the seal.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent import mcp_server as m
from env_records import env_evidence


# ---------------------------------------------------------------------------
# Fixture: build a complete VALID draft + EnvCache entry under tmp_path.
# ---------------------------------------------------------------------------

@pytest.fixture
def _staged_pipeline(tmp_path, monkeypatch, request):
    """A pipeline_id with: real authored artifact on disk, complete install
    step + pipeline_step with detected_outputs + validation + resource_usage
    + ran_in_container=True, and a matching EnvCache entry whose image_digest
    matches the step's container_image_digest. Returns (pipeline_id,
    freeze_request_key, authored_artifact_path).

    Uses a node-unique pipeline_id so the module-level _pipeline_state
    singleton can't carry state across tests."""
    pipeline_id = "p_e2e_seal_" + request.node.name
    image_digest = "sha256:" + ("ab" * 32)
    image_name   = "fake_pkg_env:latest"
    request_key  = "fake_pkg=1.0|linux/amd64|none"

    # ---- authored artifact: real on-disk file + real sha256 -----------------
    art_path  = tmp_path / "fixtures" / "driver.sh"
    art_path.parent.mkdir(parents=True, exist_ok=True)
    art_bytes = b"#!/usr/bin/env bash\necho 'driver wrote this'\n"
    art_path.write_bytes(art_bytes)
    art_sha   = hashlib.sha256(art_bytes).hexdigest()

    # ---- output the step "produced" — real file so I3 outputs pass ---------
    out_path  = tmp_path / "out" / "result.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("output content\n")

    # ---- draft (passing-shape; assembled to satisfy I0/I3/I6/I7/I8) ---------
    # Discard any stale draft from a prior test run — PipelineState.start()
    # silently resumes if the pipeline_id is already in-memory or on disk,
    # which would otherwise blend prior fields into this run's draft.
    m._pipeline_state.discard(pipeline_id)
    m._pipeline_state.start(pipeline_id, "end-to-end seal lifecycle test")
    m._pipeline_state.patch(pipeline_id, {
        "description":     "Hermetic full-lifecycle seal test.",
        "conda_env":       "bioinf_fake_pkg",
        "python_version":  "3.11",
    })
    m._pipeline_state.add_install_step(pipeline_id, {
        "tool": "conda", "subcommand": "install",
        "purpose": "Install fake_pkg",
        "command": "conda install -y fake_pkg=1.0",
        "returncode": 0,
        "installed_packages": [{"name": "fake_pkg", "version": "1.0",
                                "channel": "conda-forge",
                                "install_method": {"type": "conda",
                                                   "source": "conda-forge"}}],
    })
    from datetime import datetime, timezone
    m._pipeline_state.add_authored_artifact(pipeline_id, {
        "path": str(art_path), "role": "driver_script",
        "description": "Fake-package driver",
        "sha256": art_sha, "size_bytes": len(art_bytes),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    m._pipeline_state.add_step(pipeline_id, {
        "step": 1, "tool": "fake_pkg", "subcommand": "run",
        "purpose": "Produce output.txt",
        "command": f"fake_pkg --in {art_path} --out {out_path}",
        # the how-to the step ran, as the run tools record it: the seal derives usage from it
        "template": "fake_pkg --in {INPUT} --out {OUTPUT_DIR}/result.txt",
        "bindings": {"INPUT": str(art_path)},
        "work_dir": str(out_path.parent),
        "returncode": 0,
        "inputs": [{"path": str(art_path)}],
        "outputs": [str(out_path)],
        "detected_outputs": [str(out_path)],
        "validation": {out_path.name: {"passed": True, "expected_type": "txt"}},
        "validation_status": "passed",
        "resource_usage": {"wall_seconds": 0.1, "peak_rss_mb": 12.0,
                            "max_cpu_percent": 5.0},
        "ran_in_container": True,
        "container_image":  image_name,
        "container_image_digest": image_digest,
    })

    # ---- EnvCache entry: makes freeze_request_key resolvable -----------------
    # Carries `verifications` because seal now asks the SERVING question — a record
    # that can't satisfy Layer-1 can't be the foundation of a Layer-2 spec. A fixture
    # without it was a shape no producer can emit (see tests/env_records.py).
    cache_path = tmp_path / "_env_cache.json"
    cache_path.write_text(json.dumps({request_key: {
        "request_key":     request_key,
        "image":           image_name,
        "image_digest":    image_digest,
        "content_digest":  "ec_fake_pkg_1.0",
        "verifications":   env_evidence("fake_pkg"),
        "hpc_delivery":    {"format": "tarball", "path": "/tmp/fake_pkg.tar"},
    }}))
    monkeypatch.setattr(m._env_cache, "path", cache_path)

    # ---- where the deliverables land ---------------------------------------
    # write_workflow_spec and the Layer-2 {workflow}.RUN.html renderer both write
    # into the env's directory under the workspace, which the root conftest has
    # pointed at this test's tmp_path. Asking the resolver is what keeps the shim
    # below writing where the real writer would.
    from agent.skills import workspace
    out_dir = workspace.env_dir("bioinf_fake_pkg")       # the env the fixture pins

    from agent.skills import spec_writer
    real_write = spec_writer.write_workflow_spec

    def _shim_write(workflow: dict, _config: dict, env_name: str | None = None) -> dict:
        # Mirror the real writer: YAML only, no markdown guide (retired).
        from agent.models.core_data import WorkflowSpec
        import yaml as _yaml
        try:
            wf = WorkflowSpec.model_validate(workflow)
        except Exception as e:
            return {"error": f"WorkflowSpec validation failed: {e}"}
        name = wf.workflow_name
        yaml_path = out_dir / f"{name}.workflow.yaml"
        data = wf.model_dump(exclude_none=True)
        yaml_path.write_text(_yaml.dump(data, default_flow_style=False, sort_keys=False))
        return {"workflow_spec_path": str(yaml_path)}

    # mcp_server imports write_workflow_spec inside seal_workflow's body,
    # so patching the module attr propagates to the next call.
    monkeypatch.setattr(spec_writer, "write_workflow_spec", _shim_write)

    return pipeline_id, request_key, art_path, out_path, image_digest, out_dir


# ---------------------------------------------------------------------------
# The headline test: a valid run-through seals cleanly and the artifact is
# honest at every layer.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_e2e_seal_valid_run_seals_honestly(_staged_pipeline, tmp_path):
    pipeline_id, request_key, art_path, out_path, image_digest, out_dir = _staged_pipeline

    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                              workflow_name="e2e_seal",
                              description="end-to-end seal")

    assert result.get("success"), f"valid run-through was refused: {result}"
    # live outcome tag on the success response (the runtime affordance — the
    # caller branches on `outcome`/`code`, not on prose)
    assert result["outcome"] == "proven" and result["code"] == "seal.sealed"

    # The WorkflowSpec file on disk MUST exist + carry the honesty fields.
    spec_path = out_dir / "e2e_seal.workflow.yaml"
    assert spec_path.is_file(), f"WorkflowSpec not written at {spec_path}"

    import yaml
    spec = yaml.safe_load(spec_path.read_text())

    # L5a — validated-in-shipped-image: the step's container_image_digest
    # matched the EnvCache entry, so the workflow's truth-claim is honest.
    assert spec.get("validated_in_shipped_image") is True, \
        f"validated_in_shipped_image should be True (digests matched): {spec.get('validated_in_shipped_image')}"

    # L8 — env pin: the spec carries env_request_key + env_content_digest +
    # env_image_digest from the EnvCache; agent can't have written these.
    assert spec.get("env_request_key") == request_key
    assert spec.get("env_content_digest") == "ec_fake_pkg_1.0"

    # L6b — authored artifact survived the integrity re-anchor at seal time.
    arts = spec.get("authored_artifacts") or []
    assert len(arts) == 1 and arts[0]["sha256"] == hashlib.sha256(art_path.read_bytes()).hexdigest()

    # Multi-env chaining surface: envs[] reflects the single env used.
    envs = spec.get("envs") or []
    assert any(e.get("image_digest") == image_digest for e in envs), \
        f"envs[] should record the step's container_image_digest: {envs}"

    # The Layer-2 run dashboard is rendered alongside the spec (the how-to lives
    # inside it as a distinct panel — no markdown guide is written).
    run_report = out_dir / "e2e_seal.RUN.html"
    assert run_report.is_file(), f"run dashboard not written at {run_report}"
    assert not (out_dir / "e2e_seal.GUIDE.md").exists(), "markdown guide should be retired"
    assert result.get("run_report_path") == str(run_report)
    html = run_report.read_text()
    assert "Workflow run report — e2e_seal" in html
    assert "How to run it" in html


@pytest.mark.integration
def test_e2e_seal_unknown_pipeline_id_tagged():
    """The cheapest terminal, exercised directly: an unknown pipeline_id returns
    the machine-readable outcome class + code on the LIVE response (no fixtures —
    get_draft returns None before any env lookup). This is the runtime affordance
    that lets the agent branch on `code` instead of parsing the error string."""
    result = m.seal_workflow("no_such_draft_xyz", freeze_request_key="whatever")
    assert result["outcome"] == "refused"
    assert result["code"] == "seal.unknown_pipeline_id"
    assert result["success"] is False


# ---------------------------------------------------------------------------
# Mutation half: drop one honest field, re-seal, assert refusal at the gate.
# Each sub-test proves the seal genuinely re-checks the cheat-surface, not just
# trusts the draft.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_e2e_seal_refuses_mutated_authored_artifact(_staged_pipeline):
    """Stage-time sha256=X, mutate bytes after stage, re-seal — REFUSED at
    I8.authored_artifact_mutated. (G1 sub-check exercising the seal gate.)"""
    pipeline_id, request_key, art_path, *_ = _staged_pipeline
    # The cheat: overwrite the artifact bytes after the spec was assembled.
    art_path.write_bytes(b"#!/bin/sh\necho 'I am not the original driver'\n")

    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                              workflow_name="e2e_seal_mutated")
    assert result.get("success") is False, \
        f"mutated authored_artifact was not refused: {result}"
    # the live refusal carries its class: a failed invariant surfaces as the
    # seal.workflow_invariants terminal (the per-clause code is in violations[])
    assert result["outcome"] == "refused" and result["code"] == "seal.workflow_invariants"
    invs = [v.get("invariant", "") for v in result.get("violations") or []]
    assert any("authored_artifact_mutated" in i for i in invs), \
        f"expected I8.authored_artifact_mutated in violations: {invs}"


@pytest.mark.integration
def test_e2e_seal_refuses_step_without_resource_usage(_staged_pipeline):
    """I7 presence is TYPED since Seam A, so this pins both ends. Write end:
    a mutation stripping resource_usage from an rc=0 step is refused at the
    funnel (the strip never lands on disk). Serve end: a draft hand-edited
    AROUND the funnel is still refused when seal builds the WorkflowSpec —
    the assert-at-both-ends rule, so a record grandfathered onto disk cannot
    seal."""
    import yaml as _yaml
    from agent.skills.typed_nouns import TypedNounViolation
    pipeline_id, request_key, *_ = _staged_pipeline

    with pytest.raises(TypedNounViolation, match="resource_usage"):
        m._pipeline_state.mutate_on_disk(
            pipeline_id, lambda d: d["pipeline_steps"][0].pop("resource_usage", None))
    on_disk = m._pipeline_state.get_draft(pipeline_id)
    assert "resource_usage" in on_disk["pipeline_steps"][0], \
        "the refused strip reached the draft anyway — the gate ran after the write"

    # Bypass the funnel the only way left: edit the draft file by hand.
    draft_path = m._pipeline_state._draft_path(pipeline_id)
    d = _yaml.safe_load(draft_path.read_text())
    d["pipeline_steps"][0].pop("resource_usage", None)
    draft_path.write_text(_yaml.safe_dump(d))

    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                              workflow_name="e2e_seal_no_rss")
    assert result.get("success") is not True
    assert "resource_usage" in str(result), \
        f"expected the serve-side model refusal to name resource_usage: {result}"


@pytest.mark.integration
def test_e2e_seal_derives_the_howto_from_the_recorded_step(_staged_pipeline):
    """I4: the how-to IS the run. The spec's usage is the step's template with its bindings
    as the one trial, its outputs the step's observed outputs, and the verification
    record says so (source `steps`) and carries the literal command the step ran."""
    pipeline_id, request_key, art_path, out_path, *_ = _staged_pipeline
    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                             workflow_name="e2e_seal_derived")
    assert result.get("success") is True, result
    assert result["howto_commands"] == 1 and result["samples"] == ["trial_1"]
    import yaml
    spec = yaml.safe_load(open(result["workflow_spec_path"]))
    assert spec["usage"]["command_template"] == ["fake_pkg --in {INPUT} --out {OUTPUT_DIR}/result.txt"]
    assert [i["name"] for i in spec["usage"]["inputs"]] == ["INPUT"]
    assert spec["usage"]["outputs"] == [{"name": "OUTPUT_DIR", "files": ["result.txt"]}]
    assert spec["usage"]["trials"][0]["substitutions"] == {"INPUT": str(art_path)}
    uv = spec["usage_verification"]
    assert uv["status"] == "verified" and uv["source"] == "steps" and spec["usage_verified"] is True
    assert uv["trials"][0]["commands_run"] == [f"fake_pkg --in {art_path} --out {out_path}"]
    assert uv["trials"][0]["produced_files"] == ["result.txt"]
    assert "patch" not in (uv.get("reason") or "").lower()


@pytest.mark.integration
def test_e2e_seal_refuses_a_step_recorded_without_its_template(_staged_pipeline):
    """A step recorded by its literal command alone (run_in_env, a record predating
    bindings) cannot be the how-to: the seal refuses and names the step to re-run,
    rather than writing a spec with no how-to or a how-to nobody ran."""
    pipeline_id, request_key, *_ = _staged_pipeline
    m._pipeline_state.mutate_on_disk(pipeline_id, lambda d: [s.pop("template", None) for s in d["pipeline_steps"]])
    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                             workflow_name="e2e_seal_untemplated")
    assert result.get("success") is False
    assert result["outcome"] == "refused" and result["code"] == "seal.howto_underivable"
    assert "step 1 (fake_pkg)" in result["error"] and "bindings" in result["error"]
    assert "workflow_spec_path" not in result, "a refused seal must write no artifact"


@pytest.mark.integration
def test_e2e_seal_tags_a_fully_proven_workflow_proven(_staged_pipeline):
    """The other side of the split: the happy path keeps the published `seal.sealed`
    code. Re-tagging that code rather than adding a second one would silently change the
    meaning of a value other readers join on."""
    pipeline_id, request_key, *_ = _staged_pipeline
    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                             workflow_name="e2e_seal_proven_tag")
    assert result["outcome"] == "proven" and result["code"] == "seal.sealed"
    assert result["usage_verification"]["status"] == "verified"
    assert result["usage_verification"]["source"] == "steps"


@pytest.mark.integration
@pytest.mark.parametrize("steps_shape", ["none", "all_failed"])
def test_e2e_seal_refuses_a_spec_with_no_validated_run(_staged_pipeline, steps_shape):
    """A WorkflowSpec's premise is THE VALIDATED RUN; with no successful step there is
    nothing for it to be a record of.

    Every step-side clause iterates pipeline_steps, so they all go silent at once and the
    walk returns []; `derive_pipeline_status([])` then returns "in_progress", a valid
    status, so the model validates and the artifact is written green. Layer 1 makes the
    analogous evidence-free record a hard violation. REFUSED rather than degraded: a
    degrade says the artifact proves less than it looks like it does, but here it has no
    subject at all and should not exist."""
    pipeline_id, request_key, *_ = _staged_pipeline
    def _break_the_run(draft):
        if steps_shape == "none":
            draft["pipeline_steps"] = []
        else:
            for st in draft["pipeline_steps"]:
                st["returncode"] = 1
    m._pipeline_state.mutate_on_disk(pipeline_id, _break_the_run)

    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                             workflow_name=f"e2e_seal_norun_{steps_shape}")
    assert result.get("success") is False
    assert result["outcome"] == "refused" and result["code"] == "seal.no_validated_run"
    assert result["pipeline_steps_succeeded"] == 0
    assert "workflow_spec_path" not in result, "a refused seal must write no artifact"


@pytest.mark.integration
def test_e2e_seal_marks_unshipped_when_digest_mismatch(_staged_pipeline, tmp_path):
    """L5a — if the step's container_image_digest doesn't match any EnvCache
    entry, `validated_in_shipped_image` MUST be False. The seal is still
    written (it's a derived honesty field, not a refusal gate), but the
    deliverable honestly reports the gap.

    This is the inverse of the positive headline: we PROVE the digest-match
    check is real by giving it a non-matching digest."""
    pipeline_id, request_key, *_, out_dir = _staged_pipeline
    # ON DISK — `get_draft` hands back a copy, so mutating it would leave the real digest
    # in place and this test would "prove" the check by feeding it a MATCHING digest.
    m._pipeline_state.mutate_on_disk(
        pipeline_id,
        lambda d: d["pipeline_steps"][0].__setitem__(
            "container_image_digest", "sha256:" + ("ff" * 32)))

    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                              workflow_name="e2e_seal_unshipped")
    assert result.get("success"), f"seal should not refuse for unshipped: {result}"

    import yaml
    spec = yaml.safe_load((out_dir / "e2e_seal_unshipped.workflow.yaml").read_text())
    assert spec.get("validated_in_shipped_image") is False, \
        f"validated_in_shipped_image should be False (digest mismatch): " \
        f"{spec.get('validated_in_shipped_image')}"
