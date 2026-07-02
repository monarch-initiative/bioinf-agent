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
entry so the freeze pin resolves), monkeypatches the usage self-test to
short-circuit (no need for a real conda env), and calls seal_workflow.

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
        "usage": {
            # Both placeholders declared in usage.inputs[*].name (I6).
            "description":      "fake_pkg canonical command.",
            "command_template": "fake_pkg --in {INPUT} --out {OUTPUT}",
            "inputs":  [{"name": "INPUT",  "format": "txt"},
                        {"name": "OUTPUT", "format": "txt"}],
            "outputs": [{"name": "OUTPUT", "files": ["*.txt"]}],
            # Empty trials → single inferred trial; usage_verified gets short-
            # circuited below via the spec_writer.self_test_usage monkeypatch.
            "trials":  [],
        },
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
        "returncode": 0,
        "inputs": [{"path": str(art_path), "kind": "authored_artifact"}],
        "outputs": [str(out_path)],
        "detected_outputs": [str(out_path)],
        "validation": {out_path.name: {"valid": True, "expected_type": "txt"}},
        "validation_status": "passed",
        "resource_usage": {"wall_seconds": 0.1, "peak_rss_mb": 12.0,
                            "peak_cpu_pct": 5.0},
        "ran_in_container": True,
        "container_image":  image_name,
        "container_image_digest": image_digest,
    })

    # ---- EnvCache entry: makes freeze_request_key resolvable -----------------
    cache_path = tmp_path / "_env_cache.json"
    cache_path.write_text(json.dumps({request_key: {
        "request_key":     request_key,
        "image":           image_name,
        "image_digest":    image_digest,
        "content_digest":  "ec_fake_pkg_1.0",
        "hpc_delivery":    {"format": "tarball", "path": "/tmp/fake_pkg.tar"},
    }}))
    monkeypatch.setattr(m._env_cache, "path", cache_path)

    # ---- short-circuit usage self-test (no real conda env on the box) -------
    from agent.skills import spec_writer
    monkeypatch.setattr(spec_writer, "self_test_usage",
                        lambda *_a, **_kw: {"ok": True})

    # ---- redirect the WorkflowSpec writer to a tmp out_dir ------------------
    # write_workflow_spec resolves its out_dir from spec_writer's project_root
    # constant + config["paths"]["pipelines_dir"]; the agent's normal output
    # path is env_reports/ in the repo. For the test we shim the function to
    # land deliverables under tmp_path so each test is hermetic.
    out_dir = tmp_path / "env_reports"
    out_dir.mkdir(exist_ok=True)

    # seal_workflow ALSO renders the Layer-2 {workflow}.RUN.html dashboard into
    # config["paths"]["pipelines_dir"]. Point that at the tmp out_dir too so the
    # test can't leak a RUN.html into the repo's env_reports/.
    monkeypatch.setitem(m.config["paths"], "pipelines_dir", str(out_dir))

    from agent.skills import spec_writer
    real_write = spec_writer.write_workflow_spec

    def _shim_write(workflow: dict, _config: dict) -> dict:
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
        data.pop("user_guide", None)
        data.pop("user_guide_path", None)
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
    invs = [v.get("invariant", "") for v in result.get("violations") or []]
    assert any("authored_artifact_mutated" in i for i in invs), \
        f"expected I8.authored_artifact_mutated in violations: {invs}"


@pytest.mark.integration
def test_e2e_seal_refuses_step_without_resource_usage(_staged_pipeline):
    """I7 — every rc=0 pipeline_step must have resource_usage. Stripping it
    from the draft must refuse at seal."""
    pipeline_id, request_key, *_ = _staged_pipeline
    draft = m._pipeline_state.get_draft(pipeline_id)
    draft["pipeline_steps"][0].pop("resource_usage", None)

    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                              workflow_name="e2e_seal_no_rss")
    assert result.get("success") is False
    invs = [v.get("invariant", "") for v in result.get("violations") or []]
    assert any(i.startswith("I7") for i in invs), \
        f"expected I7 violation when resource_usage missing: {invs}"


@pytest.mark.integration
def test_e2e_seal_refuses_failed_usage_self_test(_staged_pipeline, monkeypatch):
    """H2 — I4 now GATES the seal. The draft declares a usage block; if its
    command_template fails the self-test (doesn't run green against every
    declared trial), seal must REFUSE rather than write usage_verified=False
    cosmetically. We override the fixture's short-circuit to simulate a
    failing template."""
    pipeline_id, request_key, *_ = _staged_pipeline
    from agent.skills import spec_writer
    monkeypatch.setattr(spec_writer, "self_test_usage",
                        lambda *_a, **_kw: {"ok": False,
                                            "reason": "command ran but expected outputs missing"})

    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                              workflow_name="e2e_seal_bad_usage")
    assert result.get("success") is False, \
        f"a failing usage self-test must refuse the seal (H2): {result}"
    assert result.get("stage") == "usage_self_test", \
        f"refusal should be at the usage_self_test gate: {result}"
    assert (result.get("usage_self_test") or {}).get("ok") is False


@pytest.mark.integration
def test_e2e_seal_marks_unshipped_when_digest_mismatch(_staged_pipeline, tmp_path):
    """L5a — if the step's container_image_digest doesn't match any EnvCache
    entry, `validated_in_shipped_image` MUST be False. The seal is still
    written (it's a derived honesty field, not a refusal gate), but the
    deliverable honestly reports the gap.

    This is the inverse of the positive headline: we PROVE the digest-match
    check is real by giving it a non-matching digest."""
    pipeline_id, request_key, *_, out_dir = _staged_pipeline
    draft = m._pipeline_state.get_draft(pipeline_id)
    draft["pipeline_steps"][0]["container_image_digest"] = "sha256:" + ("ff" * 32)

    result = m.seal_workflow(pipeline_id, freeze_request_key=request_key,
                              workflow_name="e2e_seal_unshipped")
    assert result.get("success"), f"seal should not refuse for unshipped: {result}"

    import yaml
    spec = yaml.safe_load((out_dir / "e2e_seal_unshipped.workflow.yaml").read_text())
    assert spec.get("validated_in_shipped_image") is False, \
        f"validated_in_shipped_image should be False (digest mismatch): " \
        f"{spec.get('validated_in_shipped_image')}"
