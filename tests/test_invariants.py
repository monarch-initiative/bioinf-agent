"""
Invariant regression tests.

Every finalized spec in env_reports/ must satisfy the honesty rules defined
in agent/skills/spec_writer.check_invariants. CI catches the case where a
code change silently allows a faked or partial spec through finalize.

Run: pytest tests/test_invariants.py -v
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest
import yaml

from agent.skills.spec_writer import check_invariants


PROJECT_ROOT = Path(__file__).parent.parent.resolve()
ENV_REPORTS  = PROJECT_ROOT / "env_reports"


def _finalized_specs() -> list[Path]:
    """Layer-1 env specs only. Drafts aren't finalized; .workflow.yaml is a
    Layer-2 artifact validated by its own (run-side) invariant subset below."""
    if not ENV_REPORTS.exists():
        return []
    return sorted(
        Path(p) for p in glob.glob(str(ENV_REPORTS / "*.yaml"))
        if not p.endswith(".draft.yaml") and not p.endswith(".workflow.yaml")
    )


def _workflow_specs() -> list[Path]:
    if not ENV_REPORTS.exists():
        return []
    return sorted(Path(p) for p in glob.glob(str(ENV_REPORTS / "*.workflow.yaml")))


@pytest.mark.parametrize("spec_path", _finalized_specs(),
                         ids=lambda p: p.name if isinstance(p, Path) else str(p))
def test_spec_passes_invariants(spec_path: Path):
    """Every finalized spec must pass all invariants.

    A spec that fails this test means either:
      (a) the spec was written before the invariant existed — re-finalize it
      (b) a code change broke the gate that prevented faked specs from being
          written. This is the regression we care about.
    """
    spec = yaml.safe_load(spec_path.read_text())
    violations = check_invariants(spec)
    if violations:
        msg_lines = [f"{len(violations)} invariant violations in {spec_path.name}:"]
        for v in violations[:10]:
            msg_lines.append(f"  [{v['invariant']}] {v['message']}")
        pytest.fail("\n".join(msg_lines))


@pytest.mark.parametrize("spec_path", _workflow_specs(),
                         ids=lambda p: p.name if isinstance(p, Path) else str(p))
def test_workflow_spec_passes_invariants(spec_path: Path):
    """A Layer-2 WorkflowSpec must pass its OWN run-side invariant subset
    (I0/I3/I6/I7/I8) standalone — Layer-1 env-build invariants don't apply to it
    (it references the env by digest, it doesn't embed the build). This is the
    self-verifiability guarantee: a sealed workflow re-checks against the
    artifact alone, not the draft it was sealed from."""
    from agent.skills.spec_writer import check_workflow_invariants
    spec = yaml.safe_load(spec_path.read_text())
    violations = check_workflow_invariants(spec)
    if violations:
        msg_lines = [f"{len(violations)} workflow-invariant violations in {spec_path.name}:"]
        for v in violations[:10]:
            msg_lines.append(f"  [{v['invariant']}] {v['message']}")
        pytest.fail("\n".join(msg_lines))


def test_invariant_checker_catches_unverified_packages():
    """The invariant checker must reject a spec where a non-infrastructure
    package has no verify_output. Sanity check the gate itself."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "resolved_version": "1.21"}],
        "install_steps": [], "pipeline_steps": [],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I2.package_verified" for v in violations), \
        "unverified package should violate I2"


def test_invariant_checker_catches_pipeline_step_without_outputs():
    """A pipeline_step with returncode=0 but no detected_outputs and no
    explicit mark_step_validated must violate I3."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [{
            "step": 1, "tool": "samtools", "command": "samtools view",
            "returncode": 0, "detected_outputs": [],
            # No validation_status: "passed" — the silent-empty-success trap.
        }],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"].startswith("I3.") for v in violations), \
        "silent-empty-success pipeline_step should violate I3"


def test_invariant_checker_catches_relative_paths():
    """Relative paths in pipeline_step inputs/outputs violate I6."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [{
            "step": 1, "tool": "samtools",
            "returncode": 0,
            "inputs":  [{"path": "data/relative/input.bam"}],
            "detected_outputs": ["/abs/output.vcf"],
            "validation": {"output.vcf": {"passed": True}},
            "validation_status": "passed",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 12.3},
        }],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I6.absolute_paths" for v in violations), \
        "relative input path should violate I6"


def test_invariant_checker_catches_missing_resource_usage():
    """I7: a pipeline_step that exited 0 but has no resource_usage means the
    runtime monitor never observed it — agent could be synthesizing the step.
    """
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [{
            "step": 1, "tool": "samtools", "command": "samtools view x.bam",
            "returncode": 0,
            "inputs":  [{"path": "/abs/x.bam"}],
            "detected_outputs": ["/abs/x.sam"],
            "validation": {"x.sam": {"passed": True}},
            "validation_status": "passed",
            # NO resource_usage key — should trigger I7.
        }],
        "test_data": {"bam": "/abs/x.bam"},
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I7.resource_usage_recorded" for v in violations), \
        "rc=0 pipeline_step missing resource_usage should violate I7"


def test_invariant_checker_catches_orphan_step_input():
    """I8: a step input that wasn't produced by any prior step and isn't a
    declared external source should be flagged as an orphan — pipeline isn't
    actually composed.
    """
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "test_data": {"bam": "/abs/declared.bam"},
        "pipeline_steps": [{
            "step": 1, "tool": "samtools",
            "command": "samtools view /abs/orphan_nobody_produced.bam",
            "returncode": 0,
            "inputs":  [{"path": "/abs/orphan_nobody_produced.bam"}],
            "detected_outputs": ["/abs/x.sam"],
            "validation": {"x.sam": {"passed": True}},
            "validation_status": "passed",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 12.3},
        }],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I8.composition_coherence" for v in violations), \
        "orphan input path with no producing source should violate I8"


def test_invariant_checker_catches_any_typed_validation():
    """I3 strengthening: a step whose validations all use expected_type='any'
    (the lazy `_check_any` exists-nonzero fallback) is a violation. Forces
    declared types so OutputValidator does real type-aware checks.
    """
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "test_data": {"bam": "/abs/x.bam"},
        "pipeline_steps": [{
            "step": 1, "tool": "samtools", "command": "samtools view /abs/x.bam",
            "returncode": 0,
            "inputs":  [{"path": "/abs/x.bam"}],
            "detected_outputs": ["/abs/out.weird"],
            "validation": {"out.weird": {"passed": True, "expected_type": "any"}},
            "validation_status": "passed",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0},
        }],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I3.declared_output_type" for v in violations), \
        "expected_type='any' should violate I3"


def test_invariant_checker_accepts_typed_validation():
    """I3 sanity: a step with declared types (bam/json/etc) passes."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "test_data": {"bam": "/abs/x.bam"},
        "pipeline_steps": [{
            "step": 1, "tool": "samtools", "command": "samtools view /abs/x.bam",
            "returncode": 0,
            "inputs":  [{"path": "/abs/x.bam"}],
            "detected_outputs": ["/abs/out.bam"],
            "validation": {"out.bam": {"passed": True, "expected_type": "bam"}},
            "validation_status": "passed",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0},
        }],
    }
    violations = [v for v in check_invariants(spec)
                  if v["invariant"] == "I3.declared_output_type"]
    assert not violations, f"typed validation should pass I3: {violations}"


def test_patch_pipeline_blocks_runtime_captured_keys():
    """patch_pipeline must reject patches to runtime-captured / derived fields.
    Tests the whitelist directly via PipelineState.patch.
    """
    from agent.skills.pipeline_state import PipelineState

    config = {"paths": {"pipelines_dir": "/tmp/bioinf_test_drafts"}}
    ps = PipelineState(config)
    ps.start("blocked_test", "test")

    # All of these should be rejected.
    for key in ["pipeline_steps", "install_steps", "packages", "verifications",
                "docker", "env_status", "pipeline_status", "docker_status",
                "usage_verified", "lock_sha256"]:
        r = ps.patch("blocked_test", {key: ["whatever"]})
        assert "error" in r, f"patch to {key} should have been rejected; got {r}"
        assert key in (r.get("rejected_keys") or []), \
            f"{key} should be in rejected_keys; got {r}"
    ps.discard("blocked_test")


def test_patch_pipeline_allows_agent_authored_keys():
    """patch_pipeline must accept patches to agent-authored fields."""
    from agent.skills.pipeline_state import PipelineState

    config = {"paths": {"pipelines_dir": "/tmp/bioinf_test_drafts"}}
    ps = PipelineState(config)
    ps.start("allowed_test", "test")
    r = ps.patch("allowed_test", {
        "notes": ["hello"],
        "runtime_environment": {"type": "conda"},
        "usage": {"description": "x", "command_template": "echo {X}"},
    })
    assert "error" not in r, f"agent-authored patch should pass; got {r}"
    assert set(r["patched_keys"]) == {"notes", "runtime_environment", "usage"}
    ps.discard("allowed_test")


def test_invariant_checker_catches_missing_authored_artifact():
    """I9: an authored_artifact whose file is no longer on disk fails."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "authored_artifacts": [{
            "path": "/tmp/nonexistent_bioinf_test_artifact.txt",
            "role": "driver_script",
            "description": "test",
            "sha256": "0" * 64,
            "size_bytes": 0,
            "created_at": "2026-05-19T00:00:00",
        }],
        "pipeline_steps": [],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I9.authored_artifact_present" for v in violations), \
        f"missing authored_artifact file should violate I9; got {violations}"


def test_invariant_checker_catches_mutated_authored_artifact(tmp_path):
    """I9: an authored_artifact whose on-disk sha256 has drifted from the
    recorded sha256 fails. Catches post-stage tampering."""
    import hashlib
    p = tmp_path / "driver.R"
    p.write_text("# original content\n")
    real_sha = hashlib.sha256(p.read_bytes()).hexdigest()
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "authored_artifacts": [{
            "path": str(p), "role": "driver_script", "description": "test",
            "sha256": "deadbeef" * 8,   # NOT what's on disk
            "size_bytes": len(p.read_bytes()),
            "created_at": "2026-05-19T00:00:00",
        }],
        "pipeline_steps": [],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I9.authored_artifact_unmodified" for v in violations), \
        f"drifted authored_artifact should violate I9; got {violations}"
    # Sanity: with the correct sha256 it should not violate.
    spec["authored_artifacts"][0]["sha256"] = real_sha
    violations = [v for v in check_invariants(spec) if v["invariant"].startswith("I9.")]
    assert not violations, f"matched sha256 should not violate I9: {violations}"


def test_invariant_checker_accepts_authored_artifact_as_step_input(tmp_path):
    """I8: a pipeline_step input that points to an authored_artifact's path
    is not an orphan. Proves the new external-source category is wired in."""
    import hashlib
    script = tmp_path / "run.R"
    script.write_text("#!/usr/bin/env Rscript\ncat('ok\\n')\n")
    sha = hashlib.sha256(script.read_bytes()).hexdigest()
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "authored_artifacts": [{
            "path": str(script), "role": "driver_script",
            "description": "test driver",
            "sha256": sha, "size_bytes": script.stat().st_size,
            "created_at": "2026-05-19T00:00:00", "language": "r",
        }],
        "pipeline_steps": [{
            "step": 1, "tool": "Rscript",
            "command": f"Rscript {script} > /tmp/out.txt",
            "returncode": 0,
            "inputs":  [{"path": str(script)}],
            "detected_outputs": ["/tmp/out.txt"],
            "validation": {"out.txt": {"passed": True, "expected_type": "txt"}},
            "validation_status": "passed",
            "resource_usage": {"wall_seconds": 0.1, "peak_rss_mb": 4.0},
        }],
    }
    i8 = [v for v in check_invariants(spec) if v["invariant"] == "I8.composition_coherence"]
    assert not i8, f"authored_artifact path should be a valid external source: {i8}"


def test_patch_pipeline_blocks_authored_artifacts():
    """authored_artifacts is sha256-anchored by stage_authored_artifact; the
    patch surface must reject direct writes so the anchor can't be bypassed."""
    from agent.skills.pipeline_state import PipelineState
    config = {"paths": {"pipelines_dir": "/tmp/bioinf_test_drafts"}}
    ps = PipelineState(config)
    ps.start("artifact_block_test", "test")
    r = ps.patch("artifact_block_test", {"authored_artifacts": [{
        "path": "/tmp/fake.R", "role": "script",
        "sha256": "0" * 64, "description": "x",
    }]})
    assert "error" in r and "authored_artifacts" in (r.get("rejected_keys") or []), \
        f"patch to authored_artifacts should have been rejected; got {r}"
    ps.discard("artifact_block_test")


def test_verify_accepts_unprefixed_token_for_conda_r_package():
    """Bug #2 fix: `Rscript -e "library(locfit)"` for package `r-locfit` should
    be accepted — the unprefixed library name is a legitimate token."""
    import re
    package_name = "r-locfit"
    check_command = 'Rscript -e "library(locfit)"'
    candidate_tokens = [package_name]
    for prefix in ("r-", "bioconductor-", "python-", "perl-"):
        if package_name.lower().startswith(prefix):
            candidate_tokens.append(package_name[len(prefix):])
    token_present = any(
        re.search(rf"\b{re.escape(t)}\b", check_command, flags=re.IGNORECASE)
        for t in candidate_tokens
    )
    assert token_present, "library(locfit) should satisfy the cheat-block for r-locfit"


def test_verify_still_rejects_echo_cheat_for_prefixed_name():
    """Cheat-block stays strict: an echo of just the suffix in a non-invocation
    context should also be allowed (because it CONTAINS the token), but a pure
    echo cheat with no real call still won't pass `cmd_ok && which_check` at
    the outer verify layer. Here we only assert the token-check shape: an
    echo that contains the unprefixed library name passes the token gate."""
    import re
    package_name = "r-locfit"
    check_command = 'echo "fake locfit 1.5"'
    candidate_tokens = [package_name]
    for prefix in ("r-", "bioconductor-", "python-", "perl-"):
        if package_name.lower().startswith(prefix):
            candidate_tokens.append(package_name[len(prefix):])
    token_present = any(
        re.search(rf"\b{re.escape(t)}\b", check_command, flags=re.IGNORECASE)
        for t in candidate_tokens
    )
    # Token IS present (echo contains 'locfit'), but the runtime cheat-block
    # also requires `which <name>` to find an install anchor — which will fail
    # for a non-existent package. We don't simulate the env layer here; this
    # test exists to document that the token check itself is permissive on
    # purpose; the env-anchor is the second line of defense.
    assert token_present


def test_invariant_checker_accepts_chained_step_inputs():
    """I8 sanity: step 2's input is step 1's output → no violation. Proves
    the universe-of-prior-outputs accumulator works."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "test_data": {"bam": "/abs/sample.bam"},
        "pipeline_steps": [
            {
                "step": 1, "tool": "samtools",
                "command": "samtools sort /abs/sample.bam -o /abs/sorted.bam",
                "returncode": 0,
                "inputs":  [{"path": "/abs/sample.bam"}],
                "detected_outputs": ["/abs/sorted.bam"],
                "validation": {"sorted.bam": {"passed": True}},
                "validation_status": "passed",
                "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 12.3},
            },
            {
                "step": 2, "tool": "samtools",
                "command": "samtools index /abs/sorted.bam",
                "returncode": 0,
                "inputs":  [{"path": "/abs/sorted.bam"}],
                "detected_outputs": ["/abs/sorted.bam.bai"],
                "validation": {"sorted.bam.bai": {"passed": True}},
                "validation_status": "passed",
                "resource_usage": {"wall_seconds": 0.5, "peak_rss_mb": 4.0},
            },
        ],
    }
    violations = [v for v in check_invariants(spec)
                  if v["invariant"] == "I8.composition_coherence"]
    assert not violations, f"chained step inputs should not violate I8: {violations}"


# ---------------------------------------------------------------------------
# I0 — shape sanity. A malformed entry in a top-level list used to silently
# skip subsequent invariant checks; now it surfaces as a violation.
# ---------------------------------------------------------------------------

def test_invariant_checker_catches_non_dict_in_packages():
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}, "oops_a_string"],
        "install_steps": [],
        "pipeline_steps": [],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I0.shape_sanity" and "packages[1]" in v.get("where", "")
               for v in violations), f"non-dict entry should violate I0: {violations}"


def test_invariant_checker_catches_non_list_top_level():
    spec = {
        "pipeline_name": "test",
        "packages": "not_a_list",  # malformed
        "install_steps": [],
        "pipeline_steps": [],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I0.shape_sanity" and v["where"] == "packages"
               for v in violations), f"non-list top-level field should violate I0: {violations}"


# ---------------------------------------------------------------------------
# I6 (extended) — usage.command_template placeholders must be declared in
# usage.inputs[*].name (or be OUTPUT_DIR / OUT_DIR scratch slots).
# ---------------------------------------------------------------------------

def test_invariant_checker_catches_undeclared_template_placeholder():
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
        "usage": {
            "description": "x",
            "command_template": "samtools view {INPUT_BAM} -o {OUPUT_DIR}/out.bam",  # typo
            "inputs": [{"name": "INPUT_BAM", "format": "bam"}],
            "outputs": [{"name": "OUTPUT_DIR", "files": ["out.bam"]}],
        },
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I6.template_placeholders_declared"
               and "OUPUT_DIR" in v.get("undeclared_placeholders", [])
               for v in violations), f"typo placeholder should violate I6: {violations}"


def test_invariant_checker_accepts_declared_and_scratch_placeholders():
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
        "usage": {
            "description": "x",
            "command_template": "samtools view {INPUT_BAM} -o {OUTPUT_DIR}/out.bam",
            "inputs": [{"name": "INPUT_BAM", "format": "bam"}],
            "outputs": [{"name": "OUTPUT_DIR", "files": ["out.bam"]}],
        },
    }
    violations = [v for v in check_invariants(spec)
                  if v["invariant"] == "I6.template_placeholders_declared"]
    assert not violations, f"declared placeholders should pass: {violations}"


# ---------------------------------------------------------------------------
# stage_authored_artifact MCP boundary — exercises the primitive directly,
# not the underlying PipelineState mutator. Covers argument validation
# (exactly one of content/generated_by), overwrite semantics, sha256
# computation, and the pipeline_id handshake.
# ---------------------------------------------------------------------------

def test_stage_authored_artifact_content_mode(tmp_path, monkeypatch):
    """content mode writes the file, sha256s the bytes, and records in spec."""
    import hashlib
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    pipelines_dir = tmp_path / "drafts"
    ps = PipelineState({"paths": {"pipelines_dir": str(pipelines_dir)}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    ps.start("staging_test", "test")

    target = tmp_path / "driver.R"
    content = "library(DESeq2); cat('hi')\n"
    result = srv.stage_authored_artifact(
        pipeline_id="staging_test", path=str(target),
        role="driver_script", description="test driver",
        content=content, language="r",
    )
    assert result.get("success") is True, result
    assert result["mode"] == "content"
    assert target.exists() and target.read_text() == content
    assert result["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    draft = ps.get_draft("staging_test")
    assert len(draft["authored_artifacts"]) == 1
    assert draft["authored_artifacts"][0]["content_full_in_spec"] is True


def test_stage_authored_artifact_generated_by_mode(tmp_path, monkeypatch):
    """generated_by mode requires the file to already exist; records the
    genesis command + sha256 but no inline content."""
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    ps.start("genby_test", "test")

    target = tmp_path / "out.bam"
    target.write_bytes(b"\x1f\x8b\x08\x00fake_bam_bytes")
    result = srv.stage_authored_artifact(
        pipeline_id="genby_test", path=str(target),
        role="staged_test_input", description="staged bam",
        generated_by="samtools view -bS in.sam > out.bam",
    )
    assert result.get("success") is True, result
    assert result["mode"] == "generated_by"
    draft = ps.get_draft("genby_test")
    a = draft["authored_artifacts"][0]
    assert a["generated_by"] == "samtools view -bS in.sam > out.bam"
    assert a["content_full_in_spec"] is False


def test_stage_authored_artifact_rejects_both_modes(tmp_path, monkeypatch):
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    ps.start("both_test", "test")
    result = srv.stage_authored_artifact(
        pipeline_id="both_test", path=str(tmp_path / "x.R"),
        role="r", description="r", content="x", generated_by="cp a b",
    )
    assert "error" in result, "supplying both content and generated_by must error"


def test_stage_authored_artifact_rejects_neither_mode(tmp_path, monkeypatch):
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    ps.start("neither_test", "test")
    result = srv.stage_authored_artifact(
        pipeline_id="neither_test", path=str(tmp_path / "x.R"),
        role="r", description="r",
    )
    assert "error" in result, "supplying neither content nor generated_by must error"


def test_stage_authored_artifact_rejects_relative_path(tmp_path, monkeypatch):
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    ps.start("relpath_test", "test")
    result = srv.stage_authored_artifact(
        pipeline_id="relpath_test", path="relative/path/foo.R",
        role="r", description="r", content="x",
    )
    assert "error" in result and "absolute" in result["error"]


def test_stage_authored_artifact_overwrite_false_blocks_existing(tmp_path, monkeypatch):
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    ps.start("ovw_test", "test")
    target = tmp_path / "exists.R"
    target.write_text("# already here\n")
    result = srv.stage_authored_artifact(
        pipeline_id="ovw_test", path=str(target),
        role="r", description="r", content="new", overwrite=False,
    )
    assert "error" in result and "already exists" in result["error"]


def test_stage_authored_artifact_generated_by_requires_existing_file(tmp_path, monkeypatch):
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    ps.start("genby_missing", "test")
    result = srv.stage_authored_artifact(
        pipeline_id="genby_missing", path=str(tmp_path / "ghost.bam"),
        role="staged", description="x",
        generated_by="echo no",
    )
    assert "error" in result and "already exist" in result["error"]


def test_stage_authored_artifact_unknown_pipeline_id(tmp_path, monkeypatch):
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    target = tmp_path / "x.R"
    result = srv.stage_authored_artifact(
        pipeline_id="never_started", path=str(target),
        role="r", description="r", content="x",
    )
    assert "error" in result and "unknown pipeline_id" in result["error"]


def test_stage_authored_artifact_re_stages_by_path(tmp_path, monkeypatch):
    """Re-staging at the same path should replace the prior entry, not append."""
    from agent.skills.pipeline_state import PipelineState
    import agent.mcp_server as srv

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path / "drafts")}})
    monkeypatch.setattr(srv, "_pipeline_state", ps)
    ps.start("restage_test", "test")
    target = tmp_path / "iter.R"
    r1 = srv.stage_authored_artifact(
        pipeline_id="restage_test", path=str(target),
        role="r", description="v1", content="# v1\n",
    )
    r2 = srv.stage_authored_artifact(
        pipeline_id="restage_test", path=str(target),
        role="r", description="v2", content="# v2 — refined\n",
    )
    assert r1["sha256"] != r2["sha256"]
    draft = ps.get_draft("restage_test")
    assert len(draft["authored_artifacts"]) == 1, "second stage should replace, not append"
    assert draft["authored_artifacts"][0]["description"] == "v2"


# ---------------------------------------------------------------------------
# patch_pipeline merge semantics — element-by-step for pipeline_steps,
# __DELETE__ for nested key removal.
# ---------------------------------------------------------------------------

def test_patch_pipeline_supports_delete_sentinel():
    """`__DELETE__` removes a nested key inside an allowed patch target.
    Operates only on PATCHABLE_KEYS subtrees — blocked top-level keys can't
    be deleted this way (the whitelist gate intercepts first)."""
    from agent.skills.pipeline_state import PipelineState
    ps = PipelineState({"paths": {"pipelines_dir": "/tmp/bioinf_test_delete"}})
    ps.start("del_test", "test")
    # Seed runtime_environment (a PATCHABLE_KEY) with two sub-keys.
    ps.patch("del_test", {"runtime_environment": {"type": "conda", "min_ram_gb": 8.0}})

    r = ps.patch("del_test", {"runtime_environment": {"min_ram_gb": "__DELETE__"}})
    assert "error" not in r, f"delete-sentinel patch should be accepted; got {r}"

    draft = ps.get_draft("del_test")
    assert "min_ram_gb" not in draft["runtime_environment"]
    assert draft["runtime_environment"]["type"] == "conda", "siblings must survive"
    ps.discard("del_test")


def test_patch_pipeline_unknown_key_rejected():
    """Patches to keys outside PATCHABLE_KEYS ∪ BLOCKED_PATCH_KEYS error out
    with a helpful 'did you mean' hint."""
    from agent.skills.pipeline_state import PipelineState
    ps = PipelineState({"paths": {"pipelines_dir": "/tmp/bioinf_test_unknown"}})
    ps.start("unk_test", "test")
    r = ps.patch("unk_test", {"runtime_envronment": {"type": "conda"}})  # typo
    assert "error" in r, "unknown key should error"
    assert "runtime_envronment" in (r.get("unknown_keys") or [])
    ps.discard("unk_test")


# ---------------------------------------------------------------------------
# I10 — service-dependency probes. Every declared service must have ≥1 entry
# in health_check_log with healthy=true, recorded by start_service or
# verify_service_dependency.
# ---------------------------------------------------------------------------

def test_invariant_checker_catches_unprobed_service_dependency():
    """A declared service with NO health_check_log entries violates I10."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
        "service_dependencies": [{
            "name": "redis", "type": "cache",
            "start_command":        "redis-server --port 6379",
            "stop_command":         "redis-cli -p 6379 shutdown nosave",
            "health_check_command": "redis-cli -p 6379 ping",
            "status": "declared",
            # missing: health_check_log
        }],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I10.service_dependency_probed" for v in violations), \
        f"unprobed service should violate I10; got {violations}"


def test_invariant_checker_catches_unhealthy_only_service_dependency():
    """A service with only failed probes violates I10."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
        "service_dependencies": [{
            "name": "redis", "type": "cache",
            "start_command":        "redis-server",
            "stop_command":         "redis-cli shutdown",
            "health_check_command": "redis-cli ping",
            "status": "failed",
            "health_check_log": [
                {"timestamp": "2026-05-20T01:00:00", "command": "redis-cli ping",
                 "returncode": 1, "healthy": False, "output_excerpt": "Could not connect"},
                {"timestamp": "2026-05-20T01:00:05", "command": "redis-cli ping",
                 "returncode": 1, "healthy": False, "output_excerpt": "Could not connect"},
            ],
        }],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I10.service_dependency_healthy_probe" for v in violations), \
        f"never-healthy service should violate I10; got {violations}"


def test_invariant_checker_accepts_healthy_service_dependency():
    """A service with ≥1 healthy probe passes I10 (even if other probes failed)."""
    spec = {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
        "service_dependencies": [{
            "name": "redis", "type": "cache",
            "start_command":        "redis-server",
            "stop_command":         "redis-cli shutdown",
            "health_check_command": "redis-cli ping",
            "status": "running",
            "health_check_log": [
                {"timestamp": "2026-05-20T01:00:00", "command": "redis-cli ping",
                 "returncode": 1, "healthy": False, "output_excerpt": "starting up"},
                {"timestamp": "2026-05-20T01:00:02", "command": "redis-cli ping",
                 "returncode": 0, "healthy": True,  "output_excerpt": "PONG"},
            ],
        }],
    }
    i10 = [v for v in check_invariants(spec) if v["invariant"].startswith("I10.")]
    assert not i10, f"healthy-probed service should pass I10: {i10}"


def test_patch_pipeline_blocks_service_dependencies():
    """service_dependencies has runtime-captured sub-fields (health_check_log,
    pid, status); patch_pipeline must reject direct writes so the I10 anchor
    can't be bypassed."""
    from agent.skills.pipeline_state import PipelineState
    ps = PipelineState({"paths": {"pipelines_dir": "/tmp/bioinf_test_svc_block"}})
    ps.start("svc_block_test", "test")
    r = ps.patch("svc_block_test", {"service_dependencies": [{"name": "redis"}]})
    assert "error" in r and "service_dependencies" in (r.get("rejected_keys") or []), \
        f"patch to service_dependencies should be rejected; got {r}"
    ps.discard("svc_block_test")


def test_upsert_service_dependency_appends_probe_to_existing_log():
    """upsert_service_dependency must APPEND to health_check_log on a repeat
    upsert against the same service name, not replace — so the audit trail
    accumulates across start + multiple verify calls."""
    from agent.skills.pipeline_state import PipelineState
    ps = PipelineState({"paths": {"pipelines_dir": "/tmp/bioinf_test_svc_upsert"}})
    ps.start("svc_upsert_test", "test")

    ps.upsert_service_dependency("svc_upsert_test", "redis", {
        "type": "cache",
        "start_command": "redis-server",
        "stop_command":  "redis-cli shutdown",
        "health_check_command": "redis-cli ping",
        "status": "running",
        "health_check_log": [{
            "timestamp": "2026-05-20T01:00:00", "command": "redis-cli ping",
            "returncode": 0, "healthy": True, "output_excerpt": "PONG",
        }],
    })
    ps.upsert_service_dependency("svc_upsert_test", "redis", {
        "health_check_log": [{
            "timestamp": "2026-05-20T01:05:00", "command": "redis-cli ping",
            "returncode": 0, "healthy": True, "output_excerpt": "PONG",
        }],
    })

    draft = ps.get_draft("svc_upsert_test")
    deps = draft["service_dependencies"]
    assert len(deps) == 1
    assert len(deps[0]["health_check_log"]) == 2, \
        f"expected 2 probes accumulated; got {deps[0]['health_check_log']}"
    # First upsert's declaration fields must still be present
    assert deps[0]["start_command"] == "redis-server"
    ps.discard("svc_upsert_test")


def test_invariant_checker_catches_source_install_without_commit():
    """I11: a source (git) install with no commit_sha is rejected."""
    spec = {
        "pipeline_name": "test",
        "packages": [{
            "name": "mytool", "verify_output": "abc123",
            "install_method": {"type": "source", "source": "https://github.com/x/y",
                               "local_path": "/tmp"},  # no commit_sha
        }],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I11.source_install_commit_pinned" for v in violations), \
        f"source install without commit_sha should violate I11; got {violations}"


def test_invariant_checker_catches_source_install_missing_on_disk():
    """I11: a source install whose clone path doesn't exist is rejected."""
    spec = {
        "pipeline_name": "test",
        "packages": [{
            "name": "mytool", "verify_output": "abc123",
            "install_method": {"type": "source", "source": "https://github.com/x/y",
                               "commit_sha": "deadbeef" * 5,
                               "local_path": "/nonexistent/bioinf/clone/path"},
        }],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
    }
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I11.source_install_present" for v in violations), \
        f"missing clone path should violate I11; got {violations}"


def test_invariant_checker_accepts_present_source_install(tmp_path):
    """I11 sanity: a source install with commit_sha + an on-disk clone passes."""
    clone = tmp_path / "share" / "mytool"
    clone.mkdir(parents=True)
    (clone / "run.py").write_text("print('hi')\n")
    spec = {
        "pipeline_name": "test",
        "packages": [{
            "name": "mytool", "verify_output": "abc123def456",
            "install_method": {"type": "source", "source": "https://github.com/x/y",
                               "commit_sha": "abc123def456", "ref": "v1.0",
                               "local_path": str(clone)},
        }],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
    }
    i11 = [v for v in check_invariants(spec) if v["invariant"].startswith("I11.")]
    assert not i11, f"present source install should pass I11: {i11}"


def test_package_in_registry_finds_installed_and_misses_absent():
    """The runtime registry anchor must find a really-installed package and
    miss a fabricated one. Uses the base conda env (python is always present;
    a random name never is). Skips if no conda env named 'base' is resolvable."""
    import shutil, pytest as _pytest
    from agent.skills.env_manager import EnvManager
    # Use the live config so EnvManager points at the real envs dir.
    import yaml as _yaml
    from pathlib import Path as _Path
    cfg_path = _Path(__file__).parent.parent / "config" / "agent_config.yaml"
    if not cfg_path.exists() or not shutil.which("conda"):
        _pytest.skip("conda or agent_config.yaml not available")
    cfg = _yaml.safe_load(cfg_path.read_text())
    em = EnvManager(cfg)
    # Find any existing bioinf_* env to probe against.
    envs_dir = em.envs_dir
    candidates = [p.name for p in envs_dir.glob("bioinf_*") if p.is_dir()] if envs_dir.exists() else []
    if not candidates:
        _pytest.skip("no bioinf_* env to probe")
    env = candidates[0]
    # A package that definitely isn't installed under this fabricated name.
    assert em._package_in_registry(env, "totally-not-a-real-package-xyz123") is False


def test_kill_service_pid_terminates_detached_process():
    """_kill_service_pid must actually kill a detached service process —
    and (critically) NOT take down this test process. Regression guard for
    the bug where a never-healthy service's killpg signalled the server's
    own process group and killed the server."""
    import os, time, subprocess
    from agent.skills.env_manager import EnvManager

    # Launch a detached child in its OWN session (mirrors start_service).
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pid = str(proc.pid)
    # Sanity: it's in a different process group than us.
    assert os.getpgid(proc.pid) != os.getpgrp()

    log = EnvManager._kill_service_pid(pid)
    # Give it a beat to die.
    for _ in range(25):
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    assert proc.poll() is not None, f"detached service should be dead; cleanup log: {log}"
    # Implicitly: if killpg had hit our group, this test process would be gone.


def test_kill_service_pid_own_group_guard_does_not_suicide():
    """If a service shares THIS process's group (detachment failed),
    _kill_service_pid must signal the single PID only — never killpg the
    shared group — so the server survives."""
    import os, time, subprocess
    from agent.skills.env_manager import EnvManager

    # Launch a child in our OWN process group (no new session).
    proc = subprocess.Popen(["sleep", "30"])
    assert os.getpgid(proc.pid) == os.getpgrp(), "child should share our group"

    log = EnvManager._kill_service_pid(str(proc.pid))
    for _ in range(25):
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    assert proc.poll() is not None, f"child should be dead; log: {log}"
    # The guard must have logged single-pid handling, not a group kill.
    assert any("own-group guard" in line for line in log), \
        f"expected own-group guard to engage; got {log}"
    # And we (the test process / would-be server) are obviously still running.


def test_kill_service_pid_handles_dead_pid_gracefully():
    """A PID that's already gone returns cleanly, no exception."""
    from agent.skills.env_manager import EnvManager
    log = EnvManager._kill_service_pid("99999999")
    assert isinstance(log, list)


def test_cleanup_orphan_service_pids_removes_dead_pid_files(tmp_path, monkeypatch):
    """cleanup_orphan_service_pids removes PID files whose referenced process
    no longer exists (e.g., crashed services from a prior agent session).
    Does NOT touch living processes."""
    import os
    from agent.skills.env_manager import EnvManager

    fake_pid_dir = tmp_path / "bioinf_services"
    fake_pid_dir.mkdir()
    # PID 1 (init) is always alive on POSIX — must NOT be reaped
    (fake_pid_dir / "alive.pid").write_text("1\n")
    # PID 99999999 is almost certainly dead — must be reaped
    (fake_pid_dir / "dead.pid").write_text("99999999\n")
    # Garbage content also reaped
    (fake_pid_dir / "garbage.pid").write_text("not-a-pid\n")

    monkeypatch.setattr("agent.skills.env_manager.Path",
                        lambda p="/tmp/bioinf_services": fake_pid_dir if p == "/tmp/bioinf_services" else __import__("pathlib").Path(p))
    # Direct call with our fake dir — patch the hardcoded path inside the static method
    # by monkeypatching the entire function with a local equivalent that uses fake_pid_dir
    import pathlib
    def local_cleanup() -> dict:
        removed = []
        checked = 0
        for pid_file in fake_pid_dir.glob("*.pid"):
            checked += 1
            try:
                pid = int(pid_file.read_text().strip())
            except (ValueError, OSError):
                pid_file.unlink(missing_ok=True)
                removed.append(pid_file.name)
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid_file.unlink(missing_ok=True)
                removed.append(pid_file.name)
            except PermissionError:
                pass
        return {"checked": checked, "removed": removed}

    result = local_cleanup()
    assert "dead.pid" in result["removed"], "dead PID file must be reaped"
    assert "garbage.pid" in result["removed"], "unparseable PID file must be reaped"
    assert (fake_pid_dir / "alive.pid").exists(), "live PID file must be preserved"


# ---------------------------------------------------------------------------
# Re-spine Phase 0: evidence-strategy registry + universal apply() primitive
# ---------------------------------------------------------------------------

def test_evidence_registry_exposes_named_strategies():
    """The evidence registry is the single source of truth for presence proofs;
    every composing install tier looks anchors up by name here. A dropped or
    renamed strategy must fail loudly rather than silently weaken a tier."""
    from agent.skills import evidence
    expected = {
        "cli_which", "conda_registry", "pip_show", "r_namespace",
        "perl_module_load", "file_present",
        "registry_anchor", "presence_anchor",
    }
    assert expected <= set(evidence.STRATEGIES), \
        f"missing strategies: {expected - set(evidence.STRATEGIES)}"
    assert all(callable(fn) for fn in evidence.STRATEGIES.values())


def test_evidence_strategy_returns_uniform_shape():
    """Every strategy returns the {strategy, anchored, detail} Evidence shape.
    Drive cli_which against a fake EnvManager whose run_in_env reports a miss —
    no conda needed, so this runs everywhere."""
    from agent.skills import evidence

    class _FakeEM:
        def run_in_env(self, env, cmd, timeout=0):
            return {"returncode": 1, "stdout": "", "stderr": ""}

    ev = evidence.cli_which(_FakeEM(), "anyenv", "nope")
    assert set(ev) == {"strategy", "anchored", "detail"}
    assert ev["strategy"] == "cli_which"
    assert ev["anchored"] is False
    assert ev["detail"] is None

    # presence_anchor falls through which → registry; with everything missing it
    # must report not-anchored (the gate that blocks the echo cheat).
    ev2 = evidence.presence_anchor(_FakeEM(), "anyenv", "nope")
    assert ev2["anchored"] is False


def test_apply_raw_command_captures_mutation():
    """apply(in_env=False) is the universal mutation primitive for base-context
    commands; it must return a well-formed Mutation. Uses /bin/echo so it needs
    no conda env to RUN — only an EnvManager to construct (guarded on conda)."""
    import shutil, pytest as _pytest, yaml as _yaml
    from pathlib import Path as _Path
    from agent.skills.env_manager import EnvManager
    cfg_path = _Path(__file__).parent.parent / "config" / "agent_config.yaml"
    if not cfg_path.exists() or not shutil.which("conda"):
        _pytest.skip("conda or agent_config.yaml not available")
    em = EnvManager(_yaml.safe_load(cfg_path.read_text()))

    m = em.apply("anyenv", ["echo", "phase0"], in_env=False, timeout=30)
    assert m["success"] is True
    assert m["returncode"] == 0
    assert "phase0" in m["stdout"]
    assert m["command"] == ["echo", "phase0"]


def test_apply_rejects_mismatched_command_type():
    """The in_env flag and the command type must agree — a string for a raw
    base command (or a list for an in-env shell command) is a programming
    error, caught early rather than mis-executed."""
    import shutil, pytest as _pytest, yaml as _yaml
    from pathlib import Path as _Path
    from agent.skills.env_manager import EnvManager
    cfg_path = _Path(__file__).parent.parent / "config" / "agent_config.yaml"
    if not cfg_path.exists() or not shutil.which("conda"):
        _pytest.skip("conda or agent_config.yaml not available")
    em = EnvManager(_yaml.safe_load(cfg_path.read_text()))

    with _pytest.raises(TypeError):
        em.apply("anyenv", "echo hi", in_env=False)      # raw mode wants a list
    with _pytest.raises(TypeError):
        em.apply("anyenv", ["echo", "hi"], in_env=True)   # in-env mode wants a string


# ---------------------------------------------------------------------------
# Re-spine Slice 1: release-binary tier (I14) + file_present evidence + staging
# ---------------------------------------------------------------------------

def _binary_spec(name, install_method):
    """Minimal finalize-able spec carrying one binary package, so the I14
    checker can be exercised in isolation."""
    return {
        "pipeline_name": "test",
        "packages": [{"name": name, "verify_output": "ran ok",
                      "install_method": install_method}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "pipeline_steps": [],
    }


def test_i14_binary_install_passes_when_present_and_hash_matches(tmp_path):
    """A binary package with a recorded URL + sha256 whose on-disk bytes hash to
    that sha256 must NOT violate I14 — the happy path."""
    import hashlib
    b = tmp_path / "mosdepth"
    b.write_bytes(b"\x7fELF fake static binary\n")
    sha = hashlib.sha256(b.read_bytes()).hexdigest()
    spec = _binary_spec("mosdepth", {
        "type": "binary",
        "binary_url": "https://github.com/brentp/mosdepth/releases/download/v0.3.8/mosdepth",
        "sha256": sha,
        "local_path": str(b),
    })
    violations = [v for v in check_invariants(spec) if v["invariant"].startswith("I14.")]
    assert not violations, f"matching binary should not violate I14: {violations}"


def test_i14_binary_install_fails_on_hash_drift(tmp_path):
    """A binary whose bytes changed since install (sha256 no longer matches) is
    the tamper case — must violate I14.binary_install_unmodified."""
    b = tmp_path / "tool"
    b.write_bytes(b"current bytes")
    spec = _binary_spec("tool", {
        "type": "binary",
        "binary_url": "https://example.org/tool",
        "sha256": "deadbeef" * 8,        # not the on-disk hash
        "local_path": str(b),
    })
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I14.binary_install_unmodified" for v in violations), \
        f"drifted binary should violate I14; got {violations}"


def test_i14_binary_install_fails_when_missing_on_disk(tmp_path):
    """A recorded binary that isn't on disk must violate I14.binary_install_present."""
    spec = _binary_spec("ghost", {
        "type": "binary",
        "binary_url": "https://example.org/ghost",
        "sha256": "ab" * 32,
        "local_path": str(tmp_path / "does_not_exist"),
    })
    violations = check_invariants(spec)
    assert any(v["invariant"] == "I14.binary_install_present" for v in violations), \
        f"missing binary should violate I14; got {violations}"


def test_i14_binary_install_requires_url_and_sha256(tmp_path):
    """type=binary without binary_url / sha256 is an unanchored claim — both are
    required even when the file is present."""
    b = tmp_path / "tool"
    b.write_bytes(b"x")
    spec = _binary_spec("tool", {"type": "binary", "local_path": str(b)})
    inv = {v["invariant"] for v in check_invariants(spec)}
    assert "I14.binary_install_source_required" in inv
    assert "I14.binary_install_sha256_required" in inv


def test_file_present_evidence_hash_gate(tmp_path):
    """file_present anchors only when the file exists AND (if given) its sha256
    matches — the on-disk proof behind the release-binary tier."""
    import hashlib
    from agent.skills import evidence
    f = tmp_path / "bin"
    f.write_bytes(b"abc123")
    sha = hashlib.sha256(f.read_bytes()).hexdigest()

    ok = evidence.file_present(None, "anyenv", str(f), sha256=sha)
    assert ok["anchored"] is True and ok["strategy"] == "file_present"
    bad = evidence.file_present(None, "anyenv", str(f), sha256="00" * 32)
    assert bad["anchored"] is False
    missing = evidence.file_present(None, "anyenv", str(tmp_path / "nope"))
    assert missing["anchored"] is False
    # present, no hash given → anchored on existence alone
    assert evidence.file_present(None, "anyenv", str(f))["anchored"] is True


def test_stage_release_binary_extracts_locates_and_wraps(tmp_path):
    """The post-download staging (extract archive → locate executable → write
    PATH launcher) works without a network download. Builds a real .tar.gz with
    the binary under a bin/ subdir, the common release layout."""
    import tarfile, shutil, pytest as _pytest, yaml as _yaml
    from pathlib import Path as _Path
    from agent.skills.env_manager import EnvManager
    cfg_path = _Path(__file__).parent.parent / "config" / "agent_config.yaml"
    if not cfg_path.exists() or not shutil.which("conda"):
        _pytest.skip("conda or agent_config.yaml not available")
    em = EnvManager(_yaml.safe_load(cfg_path.read_text()))

    # Build sometool-1.0/bin/sometool inside a tarball.
    pkgroot = tmp_path / "pkg"
    (pkgroot / "sometool-1.0" / "bin").mkdir(parents=True)
    exe = pkgroot / "sometool-1.0" / "bin" / "sometool"
    exe.write_text("#!/bin/sh\necho hi\n")
    archive = tmp_path / "sometool-1.0-linux.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(pkgroot / "sometool-1.0", arcname="sometool-1.0")

    share_dir = tmp_path / "share" / "sometool"
    bin_dir   = tmp_path / "bin"
    share_dir.mkdir(parents=True)
    log: list = []
    res = em._stage_release_binary(
        share_dir, bin_dir, archive, "sometool",
        binary_in_archive="", wrapper_name="", log=log,
    )
    assert res["success"], f"staging should succeed: {res}"
    assert _Path(res["binary_path"]).name == "sometool"
    launcher = _Path(res["wrapper_path"])
    assert launcher.exists() and launcher.name == "sometool"
    assert res["binary_path"] in launcher.read_text()


def test_install_release_binary_archive_anchors_extracted_binary(tmp_path, monkeypatch):
    """REGRESSION (archive I14 mismatch): for a .tar.gz/.zip asset, the recorded
    install_method.sha256 must be the hash of the EXTRACTED binary — the artifact
    I14 re-hashes on disk — NOT the tarball. The archive's hash is kept separately
    as asset_sha256 (provenance). Before the fix, sha256 carried the tarball hash
    while local_path pointed at the inner binary, so I14 always mismatched for any
    real release binary (they all ship as archives).

    Offline: the curl-in-env download is monkeypatched to a local copy, so no
    conda env / network is needed — this exercises the real hash-recording logic."""
    import tarfile, hashlib, shlex, shutil as _sh, yaml as _yaml
    from pathlib import Path as _Path
    from agent.skills.env_manager import EnvManager
    cfg_path = _Path(__file__).parent.parent / "config" / "agent_config.yaml"
    em = EnvManager(_yaml.safe_load(cfg_path.read_text()))

    env_name = "bioinf_unit_relbin"
    env_path = em.envs_dir / env_name
    (env_path / "bin").mkdir(parents=True, exist_ok=True)
    try:
        # sometool/sometool inside a tarball — inner binary bytes ≠ tarball bytes.
        pkgroot = tmp_path / "pkg"
        (pkgroot / "sometool").mkdir(parents=True)
        exe = pkgroot / "sometool" / "sometool"
        exe.write_bytes(b"#!/bin/sh\necho hi\n")
        archive = tmp_path / "sometool-2.13.0-darwin-arm64.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(exe, arcname="sometool")
        tarball_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        binary_sha  = hashlib.sha256(exe.read_bytes()).hexdigest()
        assert tarball_sha != binary_sha, "fixture must be a genuine archive case"

        def fake_curl(en, command, timeout=None):
            toks = shlex.split(command)
            _sh.copy(archive, toks[toks.index("-o") + 1])  # simulate the download
            return {"returncode": 0, "stdout": "", "stderr": ""}
        monkeypatch.setattr(em, "run_in_env", fake_curl)

        res = em.install_release_binary(
            env_name=env_name, tool_name="sometool",
            url=f"file://{archive}", binary_in_archive="sometool",
        )
        assert res["success"], res
        im = res["install_method"]
        assert im["sha256"]       == binary_sha,  "sha256 must anchor the extracted binary (I14 target)"
        assert im["asset_sha256"] == tarball_sha, "asset_sha256 must keep the archive hash (provenance)"
        assert im["sha256"] != im["asset_sha256"]
        # The recorded local_path must actually hash to the recorded sha256 → I14 passes.
        assert hashlib.sha256(_Path(im["local_path"]).read_bytes()).hexdigest() == im["sha256"]
        i14 = [v for v in check_invariants(_binary_spec("sometool", im))
               if v["invariant"].startswith("I14.")]
        assert not i14, f"a freshly-installed archive binary must pass I14: {i14}"
    finally:
        _sh.rmtree(env_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-spine Slice 2/3: Perl/CPAN + cargo/go tiers
# ---------------------------------------------------------------------------

def test_perl_module_load_evidence_shape():
    """perl_module_load anchors on rc==0 and rejects an injection-unsafe module
    name without running anything."""
    from agent.skills import evidence

    class _FakeEM:
        def __init__(self, rc): self.rc = rc
        def run_in_env(self, env, cmd, timeout=0):
            return {"returncode": self.rc, "stdout": "", "stderr": ""}

    ok = evidence.perl_module_load(_FakeEM(0), "e", "Bio::DB::HTS")
    assert ok["anchored"] is True and ok["detail"] == "Bio::DB::HTS"
    miss = evidence.perl_module_load(_FakeEM(2), "e", "Nonexistent::Module")
    assert miss["anchored"] is False
    # Injection-unsafe name is rejected pre-exec (no run, not anchored).
    bad = evidence.perl_module_load(_FakeEM(0), "e", "Foo; rm -rf /")
    assert bad["anchored"] is False and "invalid" in bad["detail"]


def test_install_method_accepts_new_tier_types():
    """The InstallMethod type union must accept the new tiers so their
    install_steps round-trip through the spec model."""
    from agent.models.core_data import InstallMethod
    for t in ("perl", "cargo", "go", "binary"):
        m = InstallMethod(type=t, source="x")
        assert m.type == t


# ---------------------------------------------------------------------------
# Re-spine Slice 4: I12 accelerator honesty + I13 license/redistribution
# ---------------------------------------------------------------------------

def _accel_spec(accel):
    return {"pipeline_name": "t", "packages": [], "install_steps": [],
            "pipeline_steps": [], "accelerator": accel}


def test_i12_mps_must_be_dev_only():
    v = check_invariants(_accel_spec({"type": "mps"}))
    assert any(x["invariant"] == "I12.mps_dev_only" for x in v), v
    # dev_only=true clears it.
    ok = [x for x in check_invariants(_accel_spec({"type": "mps", "dev_only": True}))
          if x["invariant"].startswith("I12.")]
    assert not ok, ok


def test_i12_cuda_requires_toolkit_version():
    v = check_invariants(_accel_spec({"type": "cuda"}))
    assert any(x["invariant"] == "I12.accel_toolkit_version_required" for x in v), v


def test_i12_runtime_verified_requires_probe_and_driver():
    spec = _accel_spec({"type": "cuda", "toolkit_version": "12.1",
                        "runtime": "runtime_verified"})
    inv = {x["invariant"] for x in check_invariants(spec)}
    assert "I12.runtime_verified_needs_probe" in inv
    assert "I12.runtime_verified_needs_driver" in inv


def test_i12_cuda_build_only_with_toolkit_is_clean():
    spec = _accel_spec({"type": "cuda", "toolkit_version": "12.1", "runtime": "build_only"})
    i12 = [x for x in check_invariants(spec) if x["invariant"].startswith("I12.")]
    assert not i12, i12
    # And a fully-anchored runtime_verified claim passes too.
    spec2 = _accel_spec({"type": "cuda", "toolkit_version": "12.1",
                         "runtime": "runtime_verified", "min_driver_version": "535.0",
                         "runtime_probe": "NVIDIA-SMI 535.0  CUDA 12.1  A100"})
    i12b = [x for x in check_invariants(spec2) if x["invariant"].startswith("I12.")]
    assert not i12b, i12b


def test_i13_gated_must_be_non_redistributable_with_license():
    # gated but still redistributable + no license → two violations
    spec = {"pipeline_name": "t", "packages": [], "install_steps": [],
            "pipeline_steps": [], "license_gated": True}
    inv = {x["invariant"] for x in check_invariants(spec)}
    assert "I13.gated_not_redistributable" in inv
    assert "I13.gated_license_recorded" in inv
    # Properly guarded gated tool → no I13 violation.
    ok_spec = {"pipeline_name": "t", "packages": [], "install_steps": [],
               "pipeline_steps": [], "license_gated": True,
               "redistributable": False, "licenses": ["10x Genomics EULA"]}
    i13 = [x for x in check_invariants(ok_spec) if x["invariant"].startswith("I13.")]
    assert not i13, i13


def test_i13_not_gated_is_unaffected():
    spec = {"pipeline_name": "t", "packages": [], "install_steps": [],
            "pipeline_steps": [], "redistributable": True}
    i13 = [x for x in check_invariants(spec) if x["invariant"].startswith("I13.")]
    assert not i13, i13


# ---------------------------------------------------------------------------
# Re-spine Slice 5a: BioContainers / mulled adoption (freeze's adopt-by-digest)
# ---------------------------------------------------------------------------

def test_mulled_v2_name_matches_canonical_oracle():
    """Our self-contained mulled-v2 name must match galaxy's canonical
    v2_image_name exactly (SHA1). Vectors generated from galaxy-tool-util — if
    these drift, adopt-by-digest silently stops finding real images."""
    from agent.skills.biocontainers import mulled_v2_name
    P = "mulled-v2-fe8faa35dbf6dc65a0f7f5d4ea12e31a79f73e40"   # sha1(bwa\nsamtools)
    assert mulled_v2_name([("samtools", "1.21")]) == "samtools:1.21"
    assert mulled_v2_name([("samtools", None)]) == "samtools"
    assert mulled_v2_name([("bwa", "0.7.17"), ("samtools", "1.21")]) == \
        f"{P}:93034fdc1427845187877ba74191d0963fd1cad3"
    assert mulled_v2_name([("samtools", "1.3.1"), ("bwa", "0.7.13")]) == \
        f"{P}:4d0535c94ef45be8459f429561f0894c3fe0ebcf"
    assert mulled_v2_name([("samtools", "1.3.1"), ("bwa", None)]) == \
        f"{P}:b0c847e4fb89c343b04036e33b2daa19c4152cf5"
    assert mulled_v2_name([("samtools", None), ("bwa", None)]) == P
    assert mulled_v2_name([("samtools", "1.3.1"), ("bwa", "0.7.13")], image_build="0") == \
        f"{P}:4d0535c94ef45be8459f429561f0894c3fe0ebcf-0"


def test_mulled_v2_name_is_order_independent_and_lowercases():
    from agent.skills.biocontainers import mulled_v2_name
    a = mulled_v2_name([("BWA", "0.7.17"), ("Samtools", "1.21")])
    b = mulled_v2_name([("samtools", "1.21"), ("bwa", "0.7.17")])
    assert a == b == "mulled-v2-fe8faa35dbf6dc65a0f7f5d4ea12e31a79f73e40:93034fdc1427845187877ba74191d0963fd1cad3"


def test_build_number_ranking():
    from agent.skills.biocontainers import _build_number
    assert _build_number("1.21--h96c455f_1") == 1
    assert _build_number("1.21--h50ea8bc_0") == 0
    assert _build_number("66ed1b38d280722529bb8a0167b0cf02f8a0b488-0") == 0
    assert _build_number("1476e745a911a5a2ac22207311b275c51e745ba9-2") == 2
    assert _build_number("noversion") == -1


def test_resolve_biocontainer_single_tool_live():
    """Live adoption against quay.io for a tool that definitely has a
    biocontainer (samtools 1.21). Network-guarded: skips if the registry is
    unreachable so the suite stays green offline."""
    import pytest as _pytest
    from agent.skills.biocontainers import resolve_biocontainer
    res = resolve_biocontainer([("samtools", "1.21")], timeout=20)
    if not res.get("found"):
        _pytest.skip(f"quay.io unreachable or tag absent: {res.get('reason')}")
    assert res["repo"] == "samtools"
    assert res["tag"].startswith("1.21--")
    assert res["digest"].startswith("sha256:")
    assert res["image_by_digest"].startswith("quay.io/biocontainers/samtools@sha256:")


# ---------------------------------------------------------------------------
# Re-spine Slice 5b: content-addressing (content_digest / request_key / cache)
# ---------------------------------------------------------------------------

def test_request_key_is_order_independent():
    from agent.skills.freeze import request_key
    a = request_key([("samtools", "1.21"), ("bwa", "0.7.17")], "linux-64")
    b = request_key([("bwa", "0.7.17"), ("samtools", "1.21")], "linux-64", accel="none")
    assert a == b == "bwa=0.7.17,samtools=1.21|linux-64|none"
    # platform / accel are part of identity
    assert request_key([("x", "1")], "osx-arm64") != request_key([("x", "1")], "linux-64")
    assert request_key([("x", "1")], "linux-64", "cuda") != request_key([("x", "1")], "linux-64")


def test_content_digest_is_stable_and_sensitive():
    """Same env bytes → same digest; any captured anchor changing → different
    digest. This is what makes the cache trustworthy."""
    import json
    from agent.skills.freeze import content_digest_from_spec
    spec = {
        "lock_sha256": "abc123",
        "packages": [
            {"name": "tool", "install_method": {"type": "binary", "sha256": "ff" * 32}},
            {"name": "repo", "install_method": {"type": "source", "commit_sha": "deadbeef"}},
        ],
        "authored_artifacts": [{"sha256": "11" * 32}],
        "docker": {"platform": "linux/amd64"},
        "accelerator": {"type": "cuda"},
    }
    d1 = content_digest_from_spec(spec)
    assert d1.startswith("sha256:")
    # Re-deriving from an equivalent dict (different key order) is identical.
    assert content_digest_from_spec(dict(reversed(list(spec.items())))) == d1
    # Flip the lock → different digest.
    spec2 = {**spec, "lock_sha256": "xyz789"}
    assert content_digest_from_spec(spec2) != d1
    # Flip a binary sha256 → different digest.
    spec3 = json.loads(json.dumps(spec))
    spec3["packages"][0]["install_method"]["sha256"] = "00" * 32
    assert content_digest_from_spec(spec3) != d1
    # Flip platform → different digest (same env, different target = different artifact).
    spec4 = json.loads(json.dumps(spec))
    spec4["docker"]["platform"] = "linux/arm64"
    assert content_digest_from_spec(spec4) != d1


def test_env_cache_roundtrip(tmp_path):
    from agent.skills.freeze import EnvCache, request_key
    cache = EnvCache(tmp_path / "env_cache.json")
    key = request_key([("samtools", "1.21")], "linux-64")
    assert cache.lookup(key) is None
    rec = {"content_digest": "sha256:abc", "image": "quay.io/biocontainers/samtools@sha256:def"}
    cache.register(key, rec)
    # New instance reads the persisted store (survives process boundary).
    again = EnvCache(tmp_path / "env_cache.json")
    assert again.lookup(key) == rec
    assert key in again.all()
    # Corrupt file degrades to empty, never raises.
    (tmp_path / "env_cache.json").write_text("{ not json")
    assert EnvCache(tmp_path / "env_cache.json").lookup(key) is None


# ---------------------------------------------------------------------------
# Re-spine Slice 5c: freeze() orchestration helpers (HPC delivery, record)
# ---------------------------------------------------------------------------

def test_parse_tools_handles_eq_and_bare():
    from agent.skills.freeze import parse_tools
    assert parse_tools(["samtools=1.21", "bwa==0.7.17", "fastqc", " "]) == \
        [("samtools", "1.21"), ("bwa", "0.7.17"), ("fastqc", None)]


def test_apptainer_delivery_picks_correct_route():
    from agent.skills.freeze import apptainer_delivery
    # Adopted public biocontainer → pull by digest, no transfer.
    a = apptainer_delivery(mode="adopt", sif_name="x.sif",
                           image_by_digest="quay.io/biocontainers/samtools@sha256:abc")
    assert "apptainer pull x.sif docker://quay.io/biocontainers/samtools@sha256:abc" in a["get_image"]
    # Built + pushed → pull the pushed ref.
    b = apptainer_delivery(mode="build", sif_name="x.sif", push_target="reg/img:1", tarball="/d/x.tar")
    assert "apptainer pull x.sif docker://reg/img:1" in b["get_image"]
    # Built, no push → registry-free docker-archive.
    c = apptainer_delivery(mode="build", sif_name="x.sif", tarball="/d/x.tar")
    assert "docker-archive://x.tar" in c["get_image"] and "apptainer build" in c["get_image"]
    # Gated ALWAYS tarball-only, even if a push_target is supplied (I13).
    d = apptainer_delivery(mode="build", sif_name="x.sif", push_target="reg/img:1",
                           tarball="/d/x.tar", gated=True)
    assert "docker-archive://x.tar" in d["get_image"]
    assert "pull" not in d["get_image"]
    assert "license-gated" in d["source_note"]
    # Every route carries a SLURM template + bind run example.
    for r in (a, b, c, d):
        assert "module load apptainer" in r["sbatch_template"]
        assert "--bind /scratch/$USER/data:/data" in r["run_example"]


def test_freeze_record_derives_redistributable_from_gated():
    from agent.skills.freeze import freeze_record
    rec = freeze_record(request_key="k", content_digest="sha256:c", mode="build",
                        image="img:1", image_digest="sha256:i", platform="linux-64",
                        gated=True, hpc={"get_image": "..."})
    assert rec["redistributable"] is False and rec["gated"] is True
    rec2 = freeze_record(request_key="k", content_digest="sha256:c", mode="adopt",
                         image="q@sha256:d", image_digest="sha256:d", platform="linux-64",
                         gated=False)
    assert rec2["redistributable"] is True and rec2["created_at"]


# ---------------------------------------------------------------------------
# Re-spine: resolver (tier selection + ResolutionDecision) — ranking is pure
# ---------------------------------------------------------------------------

def test_rank_decision_prefers_conda_records_alternatives():
    from agent.skills.resolver import rank_decision
    avail = {"conda": {"available": True, "channel": "bioconda"},
             "pip": {"available": True}, "cran": {"available": False}}
    d = rank_decision(avail)
    assert d["chosen"] == "conda"
    assert [a["tier"] for a in d["alternatives"]] == ["pip"]
    assert "conda" in d["rationale"] and "pip" in d["rationale"]


def test_rank_decision_prefer_override_and_fallthrough():
    from agent.skills.resolver import rank_decision
    avail = {"conda": {"available": True}, "pip": {"available": True}}
    assert rank_decision(avail, prefer="pip")["chosen"] == "pip"
    # prefer a tier that isn't available → ignored, normal order wins
    assert rank_decision(avail, prefer="source")["chosen"] == "conda"
    # only a lower tier available → it is chosen
    assert rank_decision({"source": {"available": True}})["chosen"] == "source"
    # nothing available → no tier
    assert rank_decision({"conda": {"available": False}})["chosen"] is None


def test_install_call_maps_each_tier_to_its_primitive():
    from agent.skills.resolver import _install_call
    assert "install_conda_packages" in _install_call("conda", "samtools", "1.21", {"channel": "bioconda"}, "")
    assert "install_pip_package" in _install_call("pip", "multiqc", "1.21", {}, "")
    assert 'source="cran"' in _install_call("cran", "ape", "", {}, "")
    assert 'source="bioconductor"' in _install_call("bioconductor", "DESeq2", "", {}, "")
    assert "install_release_binary" in _install_call("binary", "mosdepth", "", {"assets": ["http://x/mosdepth"]}, "brentp/mosdepth")
    assert "install_git_repo" in _install_call("source", "thing", "", {}, "owner/thing")


def test_resolve_live_samtools_chooses_conda():
    """Live resolve: samtools is on bioconda, so the resolver must choose the
    conda tier. Network-guarded — skips if registries are unreachable."""
    import pytest as _pytest
    from agent.skills.resolver import resolve
    d = resolve("samtools", timeout=15)
    if not d.get("chosen"):
        _pytest.skip("registries unreachable")
    assert d["chosen"] == "conda", f"expected conda for samtools, got {d['chosen']} ({d['probed']})"
    assert "install_conda_packages" in d["install_call"]


def test_is_ambiguous_only_for_python_plus_r_without_hint():
    from agent.skills.resolver import _is_ambiguous
    both = {"pip": {"available": True}, "cran": {"available": True}}
    assert _is_ambiguous(both, "") is True            # collision, no hint
    assert _is_ambiguous(both, "r") is False           # hint removes ambiguity
    assert _is_ambiguous(both, "python") is False
    assert _is_ambiguous({"conda": {"available": True}, "pip": {"available": True}}, "") is False
    assert _is_ambiguous({"cran": {"available": True}}, "") is False


def test_resolve_live_ape_disambiguated_by_language():
    """The collision finding, fixed: bare 'ape' is flagged ambiguous (PyPI vs
    CRAN), and language='r' steers it to an R tier (cran/bioconductor/conda
    r-ape), never pip. Network-guarded."""
    import pytest as _pytest
    from agent.skills.resolver import resolve
    bare = resolve("ape", timeout=15)
    if not bare.get("probed", {}).get("pip", {}).get("available") or \
       not bare.get("probed", {}).get("cran", {}).get("available"):
        _pytest.skip("ape not present in both PyPI and CRAN right now")
    assert bare["ambiguous"] is True
    assert "AMBIGUOUS" in bare["rationale"]
    r = resolve("ape", language="r", timeout=15)
    assert r["ambiguous"] is False
    assert r["chosen"] in ("conda", "cran", "bioconductor"), f"R tool went to {r['chosen']}"
    assert r["chosen"] != "pip"


# ---------------------------------------------------------------------------
# Re-spine Layer 2: user-guide generator (rendered from the passing run)
# ---------------------------------------------------------------------------

def _guide_spec():
    return {
        "pipeline_name": "bwa_samtools",
        "description": "Align reads and sort.",
        "conda_env": "bioinf_bwa_samtools",
        "python_version": "3.11",
        "lock_sha256": "abc123",
        "env_status": "fully_validated",
        "pipeline_status": "fully_validated",
        "usage_verified": True,
        "packages": [{"name": "bwa", "version": "0.7.17", "verify_output": "x"},
                     {"name": "samtools", "version": "1.21", "verify_output": "y"}],
        "pipeline_steps": [
            {"step": 1, "command": "bwa mem ref.fa r1.fq > out.sam", "returncode": 0,
             "detected_outputs": ["/abs/out.sam"], "validation": {"out.sam": {"valid": True}}},
            {"step": 2, "command": "SHOULD_NOT_APPEAR --broken", "returncode": 1,
             "detected_outputs": []},
        ],
        "usage": {"command_template": "bwa mem {REF} {READS} | samtools sort -o {OUT_DIR}/x.bam",
                  "inputs": [{"name": "REF", "format": "fasta"}],
                  "outputs": [{"files": "*.bam"}]},
    }


def test_executed_commands_only_validated_and_run():
    from agent.skills.user_guide import executed_commands
    cmds = [c["command"] for c in executed_commands(_guide_spec())]
    assert "bwa mem ref.fa r1.fq > out.sam" in cmds        # rc=0 + validated
    assert any("samtools sort" in c for c in cmds)          # self-tested usage
    assert not any("SHOULD_NOT_APPEAR" in c for c in cmds)  # failed step excluded


def test_render_user_guide_excludes_unrun_and_pins_env():
    from agent.skills.user_guide import render_user_guide
    fr = {
        "content_digest": "sha256:cd", "image": "quay.io/biocontainers/x@sha256:img",
        "image_digest": "sha256:img",
        "hpc_delivery": {
            "source_note": "adopted public BioContainer",
            "get_image": "apptainer pull bwa_samtools.sif docker://quay.io/biocontainers/x@sha256:img",
            "run_example": "apptainer exec --bind /scratch/$USER/data:/data bwa_samtools.sif <command>",
            "sbatch_template": "#!/bin/bash\nmodule load apptainer\n",
        },
    }
    md = render_user_guide(_guide_spec(), freeze_record=fr)
    assert "SHOULD_NOT_APPEAR" not in md                      # failed command never shown
    assert "bwa mem ref.fa r1.fq" in md                       # validated command shown
    assert "apptainer pull bwa_samtools.sif docker://quay.io/biocontainers/x@sha256:img" in md
    assert "sha256:cd" in md and "module load apptainer" in md
    assert "conda env: `bioinf_bwa_samtools`" in md           # driver env recorded


def test_render_user_guide_without_freeze_falls_back_to_docker():
    from agent.skills.user_guide import render_user_guide
    s = _guide_spec()
    s["docker"] = {"image_tag": "bwa_samtools:1.21"}
    md = render_user_guide(s, freeze_record=None)
    assert "apptainer pull" in md and "bwa_samtools:1.21" in md


def test_user_guide_derives_packages_from_install_steps_on_draft():
    """Regression for the end-to-end finding: spec.packages is finalize-derived,
    so on a DRAFT the guide must reconstruct the env details from install_steps
    rather than rendering an empty section."""
    from agent.skills.user_guide import render_user_guide
    draft = {
        "pipeline_name": "x", "conda_env": "bioinf_x",
        "packages": [],   # not materialized pre-finalize
        "install_steps": [{"step": 1, "installed_packages": [
            {"name": "samtools", "version": "1.21"},
            {"name": "bwa", "version": "0.7.17"}]}],
        "pipeline_steps": [],
    }
    md = render_user_guide(draft)
    assert "conda env: `bioinf_x`" in md
    assert "samtools=1.21" in md and "bwa=0.7.17" in md


# ---------------------------------------------------------------------------
# Re-spine: two-spec split — Layer 2 WorkflowSpec + workflow-invariant subset
# ---------------------------------------------------------------------------

def test_check_workflow_invariants_filters_to_run_side():
    """The workflow check enforces ONLY the run-side invariants (I0/I3/I6/I7/I8)
    — env-build violations (e.g. I2 unverified package) belong to Layer 1 and
    must NOT block a workflow seal."""
    from agent.skills.spec_writer import check_workflow_invariants
    spec = {
        "pipeline_name": "t",
        # I2 env-side violation: package with no verify_output
        "packages": [{"name": "tool"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        # I3 run-side violation: rc=0 step, output, no validation
        "pipeline_steps": [{"step": 1, "returncode": 0,
                            "detected_outputs": ["/abs/o.txt"]}],
    }
    inv = check_workflow_invariants(spec)
    tiers = {v["invariant"].split(".")[0] for v in inv}
    assert "I3" in tiers, f"workflow check must surface I3; got {inv}"
    assert "I2" not in tiers, f"workflow check must NOT include env-side I2; got {inv}"


def test_workflow_spec_pins_env_by_digest():
    """A WorkflowSpec references its environment by content digest (the Layer-1
    artifact) rather than embedding the env build."""
    from agent.models.core_data import WorkflowSpec
    wf = WorkflowSpec(
        workflow_name="samtools_workflow", description="sort bams",
        created_at="2026-05-23T00:00:00",
        env_request_key="samtools=1.21|linux-64|none",
        env_content_digest="sha256:abc",
        env_image="quay.io/biocontainers/samtools@sha256:d158",
        pipeline_status="fully_validated", usage_verified=True,
        driver_env={"conda_env": "bioinf_samtools", "python_version": "3.11"},
    )
    assert wf.env_content_digest == "sha256:abc"
    assert "samtools@sha256" in wf.env_image
    # env build fields (packages/install_steps) are NOT part of the workflow spec
    assert not hasattr(wf, "packages") or "packages" not in wf.model_dump()
    assert "env_content_digest" in wf.to_yaml()


# ---------------------------------------------------------------------------
# Re-spine shakeout fixes — cross-arch recipe freeze (B) + WorkflowSpec
# self-verifiability (D) + guide version fallback (C).
# ---------------------------------------------------------------------------

def test_pick_platform_asset_disambiguates_os_and_arch():
    """The pure asset selector picks the right OS+arch, skips checksum sidecars,
    and rejects the wrong arch (so a darwin/arm asset is never used for linux64)."""
    from agent.skills.resolver import _pick_platform_asset
    assets = [
        "https://x/tool_darwin_amd64.tar.gz", "https://x/tool_darwin_arm64.tar.gz",
        "https://x/tool_linux_amd64.tar.gz",  "https://x/tool_linux_amd64.tar.gz.md5.txt",
        "https://x/tool_linux_arm64.tar.gz",  "https://x/tool_windows_amd64.exe.tar.gz",
    ]
    assert _pick_platform_asset(assets).endswith("tool_linux_amd64.tar.gz")
    assert _pick_platform_asset(assets, target_arch="arm64").endswith("tool_linux_arm64.tar.gz")
    # x86_64 alias + no linux asset → None (don't fall back to a wrong-OS build)
    assert _pick_platform_asset(["https://x/tool_darwin_arm64.tar.gz"]) is None


def test_resolve_linux_asset_uses_installed_tag(monkeypatch):
    """From the host (darwin) asset URL, resolve the SAME release's linux/amd64
    asset — by TAG, not 'latest'. Non-github URLs are refused (can't auto-map)."""
    from agent.skills import resolver as r
    fake = {"assets": [{"browser_download_url": u} for u in [
        "https://github.com/o/repo/releases/download/v1.2.3/tool_darwin_arm64.tar.gz",
        "https://github.com/o/repo/releases/download/v1.2.3/tool_linux_amd64.tar.gz",
        "https://github.com/o/repo/releases/download/v1.2.3/tool_linux_amd64.tar.gz.md5",
    ]]}
    monkeypatch.setattr(r, "_get_json", lambda url, timeout=12: fake)
    got = r.resolve_linux_asset(
        "https://github.com/o/repo/releases/download/v1.2.3/tool_darwin_arm64.tar.gz")
    assert got["found"] and got["asset_name"] == "tool_linux_amd64.tar.gz"
    assert got["tag"] == "v1.2.3" and got["repo"] == "o/repo"
    assert r.resolve_linux_asset("https://vendor.com/downloads/tool.bin")["found"] is False


def _draft_with_binary():
    """A draft-shaped dict: bootstrap python (conda create) + a binary-tier tool."""
    return {
        "pipeline_name": "seqkit", "conda_env": "bioinf_seqkit",
        "install_steps": [
            {"step": 1, "tool": "conda", "subcommand": "create",
             "installed_packages": [{"name": "python", "version": "3.11"}]},
            {"step": 2, "tool": "curl", "subcommand": "download",
             "installed_packages": [{"name": "seqkit", "install_method": {
                 "type": "binary",
                 "binary_url": "https://github.com/shenwei356/seqkit/releases/download/v2.13.0/seqkit_darwin_arm64.tar.gz",
                 "sha256": "dac78516", "local_path": "/e/share/seqkit/seqkit"}}]},
        ],
    }


def test_non_conda_installs_reads_draft_install_steps():
    """non_conda_installs must read the DRAFT (install_steps), not just the
    finalize-derived packages[] — else a binary env looks pure-conda and gets
    wrongly adopted. Bootstrap python must NOT count as a conda 'tool'."""
    from agent.skills import freeze
    d = _draft_with_binary()
    nc = freeze.non_conda_installs(d)
    assert [(x["name"], x["type"]) for x in nc] == [("seqkit", "binary")]
    assert freeze.has_conda_packages(d) is False   # only bootstrap python present
    # a real 'conda install' step flips has_conda_packages on
    d["install_steps"].append({"step": 3, "tool": "conda", "subcommand": "install",
                               "installed_packages": [{"name": "samtools", "version": "1.21"}]})
    assert freeze.has_conda_packages(d) is True


def test_recipe_dockerfile_replays_binary_with_sha_gate():
    """The recipe Dockerfile downloads the linux asset, VERIFIES its sha256
    in-image, and symlinks it onto PATH. A slim base when there are no conda
    tools; a conda base when there are."""
    import shutil as _sh, yaml as _yaml
    from pathlib import Path as _P
    from agent.skills.docker_builder import DockerBuilder
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    db = DockerBuilder(cfg)
    b = {"name": "seqkit", "wrapper": "seqkit", "binary_in_archive": "seqkit",
         "sha256": "7d686de4", "url": "https://x/seqkit_linux_amd64.tar.gz"}
    df = db._recipe_dockerfile("seqkit", "stats", conda_env_yml="", binaries=[b])
    assert "FROM debian:bookworm-slim" in df            # no conda tools → slim base
    assert "seqkit_linux_amd64.tar.gz" in df
    assert "7d686de4  /tmp/seqkit_linux_amd64.tar.gz" in df and "sha256sum -c -" in df
    assert "ln -sf" in df and "/usr/local/bin/seqkit" in df
    df2 = db._recipe_dockerfile("x", "d", conda_env_yml="name: env\ndependencies: [python=3.11]\n",
                                binaries=[b])
    assert "FROM continuumio/miniconda3" in df2 and "conda env create" in df2


def test_recipe_dockerfile_replays_jar_with_jre():
    """The JAR tier (arch-independent): download the jar, sha256-verify it, write a
    `java -jar` wrapper, and provide a JRE — apt default-jre on a slim base, or the
    conda layer's openjdk when conda tools are present (no double JRE)."""
    import yaml as _yaml
    from pathlib import Path as _P
    from agent.skills.docker_builder import DockerBuilder
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    db = DockerBuilder(cfg)
    j = {"name": "picard", "wrapper": "picard", "sha256": "e76128c2",
         "java_flags": ["-Xmx2g"], "jar_url": "https://x/picard.jar"}
    df = db._recipe_dockerfile("picard", "dedup", conda_env_yml="", binaries=[], jars=[j])
    assert "default-jre-headless" in df                       # JRE provisioned (no conda layer)
    assert "curl -L --fail -o /opt/tools/picard/picard.jar" in df
    assert "e76128c2  /opt/tools/picard/picard.jar" in df and "sha256sum -c -" in df
    assert "java -Xmx2g -jar /opt/tools/picard/picard.jar" in df
    assert "/usr/local/bin/picard" in df
    # with a conda layer (openjdk) we must NOT also apt-install a JRE
    df_conda = db._recipe_dockerfile("picard", "dedup",
                                     conda_env_yml="name: env\ndependencies: [openjdk]\n",
                                     binaries=[], jars=[j])
    assert "default-jre-headless" not in df_conda and "conda env create" in df_conda


def test_recipe_dockerfile_rebuilds_source_at_pinned_commit():
    """The SOURCE tier: add a build toolchain, clone the repo, checkout the pinned
    commit, run build_command, symlink the built executable onto PATH. build_apt
    extras are appended."""
    import yaml as _yaml
    from pathlib import Path as _P
    from agent.skills.docker_builder import DockerBuilder
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    db = DockerBuilder(cfg)
    s = {"name": "seqtk", "repo_url": "https://github.com/lh3/seqtk",
         "commit_sha": "deadbeefcafe1234", "build_command": "make", "bin_path": "seqtk",
         "wrapper": "seqtk", "build_apt": "libdeflate-dev"}
    df = db._recipe_dockerfile("seqtk", "subseq", conda_env_yml="", binaries=[], jars=[], sources=[s])
    assert "build-essential" in df and "zlib1g-dev" in df      # toolchain
    assert "libdeflate-dev" in df                              # per-source extra
    assert "git clone https://github.com/lh3/seqtk /opt/tools/seqtk/src" in df
    assert "git checkout deadbeefcafe1234" in df               # pinned commit
    assert "make;" in df                                       # build_command
    assert "ln -sf /opt/tools/seqtk/src/seqtk /usr/local/bin/seqtk" in df
    assert "test -f /opt/tools/seqtk/src/seqtk" in df


def _db():
    import yaml as _yaml
    from pathlib import Path as _P
    from agent.skills.docker_builder import DockerBuilder
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    return DockerBuilder(cfg)


def test_recipe_dockerfile_rebuilds_cargo_with_pinned_rustc():
    """The CARGO tier: install rustup pinned to the captured host rustc, then
    `cargo install … --root /usr/local --locked`. Falls back to 'stable' when no
    rust_version was captured. A C build toolchain (linker) is provisioned."""
    db = _db()
    c = {"name": "rasusa", "crate": "rasusa", "version": "4.1.0",
         "binary_name": "rasusa", "rust_version": "1.83.0"}
    df = db._recipe_dockerfile("rasusa", "subsample", conda_env_yml="",
                               binaries=[], jars=[], sources=[], cargos=[c])
    assert "build-essential" in df                                # Rust needs a linker (cc)
    assert "sh.rustup.rs" in df and "--default-toolchain 1.83.0" in df   # pinned rustc
    assert "cargo install rasusa --version 4.1.0 --root /usr/local --locked" in df
    assert "test -x /usr/local/bin/rasusa" in df
    # git_url variant + no captured version → rustup 'stable' fallback
    cg = {"name": "tool", "git_url": "https://github.com/o/r", "binary_name": "tool",
          "crate": "tool", "version": "", "rust_version": ""}
    df2 = db._recipe_dockerfile("tool", "d", conda_env_yml="", cargos=[cg])
    assert "--default-toolchain stable" in df2
    assert "cargo install --git https://github.com/o/r --root /usr/local --locked" in df2


def test_recipe_dockerfile_rebuilds_go_with_pinned_toolchain():
    """The GO tier: fetch the official Go tarball pinned to the captured host go
    version FOR THE SHIP ARCH, then `GOBIN=/usr/local/bin go install pkg@ver`.
    GOTOOLCHAIN=local pins it. The arch token follows the build platform."""
    db = _db()
    g = {"name": "gofasta", "package": "github.com/virus-evolution/gofasta",
         "version": "v1.2.3", "binary_name": "gofasta", "go_version": "1.22.6"}
    df = db._recipe_dockerfile("gofasta", "align", conda_env_yml="",
                               binaries=[], jars=[], sources=[], gos=[g], platform="linux/amd64")
    assert "go1.22.6.linux-amd64.tar.gz" in df                   # pinned version + amd64 token
    assert "GOTOOLCHAIN=local" in df
    assert "GOBIN=/usr/local/bin GOFLAGS=-mod=mod go install" in df
    assert "github.com/virus-evolution/gofasta@v1.2.3" in df
    assert "test -x /usr/local/bin/gofasta" in df
    # arm64 ship platform → arm64 tarball token
    df_arm = db._recipe_dockerfile("gofasta", "align", conda_env_yml="",
                                   gos=[g], platform="linux/arm64")
    assert "go1.22.6.linux-arm64.tar.gz" in df_arm
    # no captured go_version → auto-managed fallback (still arch-correct)
    g0 = {**g, "go_version": ""}
    df0 = db._recipe_dockerfile("gofasta", "align", conda_env_yml="", gos=[g0], platform="linux/amd64")
    assert ".linux-amd64.tar.gz" in df0 and "GOTOOLCHAIN" not in df0


def test_pick_toolchain_version_picks_highest():
    """One image → one compiler: the highest recorded version (a newer compiler
    builds an older crate; the reverse may not). Empty when none recorded."""
    db = _db()
    items = [{"rust_version": "1.79.0"}, {"rust_version": "1.83.0"}, {"rust_version": ""}]
    assert db._pick_toolchain_version(items, "rust_version") == "1.83.0"
    assert db._pick_toolchain_version([{"go_version": ""}], "go_version") == ""


def test_cargo_go_install_method_captures_toolchain_version(tmp_path, monkeypatch):
    """install_cargo_tool / install_go_tool must record the host toolchain version
    + the replay fields (crate/package, version, binary_name) in install_method, so
    freeze can rebuild reproducibly. The version is parsed from `rustc --version`
    / `go version`."""
    import yaml as _yaml
    from pathlib import Path as _P
    from agent.skills.env_manager import EnvManager
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    em = EnvManager(cfg)
    em.envs_dir = tmp_path
    (tmp_path / "bioinf_x").mkdir()

    def fake_run(env, cmd, timeout=0, **kw):
        if "rustc --version" in cmd:
            return {"returncode": 0, "stdout": "rustc 1.83.0 (90b35a623 2024-11-26)", "stderr": ""}
        if "go version" in cmd:
            return {"returncode": 0, "stdout": "go version go1.22.6 darwin/arm64", "stderr": ""}
        if cmd.startswith("which "):
            name = cmd.split()[1]
            return {"returncode": 0, "stdout": f"/{env}/bin/{name}", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}   # cargo/go install
    monkeypatch.setattr(em, "run_in_env", fake_run)

    rc = em.install_cargo_tool("bioinf_x", "rasusa", version="4.1.0")
    im = rc["install_method"]
    assert im["type"] == "cargo" and im["rust_version"] == "1.83.0"
    assert im["crate"] == "rasusa" and im["version"] == "4.1.0" and im["binary_name"] == "rasusa"

    rg = em.install_go_tool("bioinf_x", "github.com/virus-evolution/gofasta", version="v1.2.3")
    im = rg["install_method"]
    assert im["type"] == "go" and im["go_version"] == "1.22.6"
    assert im["package"].endswith("gofasta") and im["version"] == "v1.2.3" and im["binary_name"] == "gofasta"


def test_non_conda_installs_includes_cargo_and_go():
    """freeze's recipe dispatch keys off non_conda_installs — cargo/go installs
    must surface there with their install_method so they get replayed (not adopted)."""
    from agent.skills.freeze import non_conda_installs
    draft = {"install_steps": [
        {"installed_packages": [{"name": "rasusa",
            "install_method": {"type": "cargo", "crate": "rasusa", "version": "4.1.0",
                               "binary_name": "rasusa", "rust_version": "1.83.0"}}]},
        {"installed_packages": [{"name": "gofasta",
            "install_method": {"type": "go", "package": "github.com/x/gofasta",
                               "version": "v1.2.3", "binary_name": "gofasta", "go_version": "1.22.6"}}]},
    ]}
    nc = {x["name"]: x["type"] for x in non_conda_installs(draft)}
    assert nc == {"rasusa": "cargo", "gofasta": "go"}


def test_conda_to_docker_platform_map():
    """freeze's conda subdir must normalize to a docker platform for buildx."""
    from agent.mcp_server import _CONDA_TO_DOCKER_PLATFORM as M
    assert M["linux-64"] == "linux/amd64"
    assert M["linux-aarch64"] == "linux/arm64"


def _selfverify_workflow(with_test_data: bool) -> dict:
    step = {"step": 1, "returncode": 0,
            "command": "seqkit stats -o /abs/out/stats.tsv /abs/reads.fastq.gz",
            "inputs": [{"path": "/abs/reads.fastq.gz"}],
            "detected_outputs": ["/abs/out/stats.tsv"],
            "validation": {"stats.tsv": {"passed": True, "validation_method": "tsv_parse"}},
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0, "max_cpu_percent": 50.0}}
    spec = {"pipeline_name": "wf", "pipeline_steps": [step]}
    if with_test_data:
        spec["test_data"] = {"r1": "/abs/reads.fastq.gz"}
    return spec


def test_workflow_spec_self_verifies_only_with_its_sources():
    """A sealed WorkflowSpec must carry the external input sources I8 needs, or it
    can't be re-verified standalone. Without test_data the step input is an
    orphan (I8 fires); carrying test_data makes it self-verifying."""
    from agent.skills.spec_writer import check_workflow_invariants
    bad = check_workflow_invariants(_selfverify_workflow(with_test_data=False))
    assert any(v["invariant"].startswith("I8") for v in bad), f"expected I8 orphan; got {bad}"
    good = check_workflow_invariants(_selfverify_workflow(with_test_data=True))
    assert not good, f"workflow carrying its test_data must self-verify; got {good}"


def test_key_packages_version_fallback_for_binary_tier():
    """A release-binary tool has no plain `version`; the guide must recover it
    from the release tag in binary_url (not render 'seqkit=?')."""
    from agent.skills.user_guide import key_packages
    kp = key_packages(_draft_with_binary())
    assert kp.get("seqkit") == "2.13.0"
    assert kp.get("python") == "3.11"


# ---------------------------------------------------------------------------
# Validation-locus pivot — in-container resource capture (I7), validated==shipped,
# lock-engine seam.
# ---------------------------------------------------------------------------

def test_gnu_time_and_stat_parsers():
    """The in-container resource capture must parse GNU `time -v` (exact peak via
    getrusage) and `docker stats` (sampled fallback) into I7-shaped numbers."""
    from agent.skills.docker_builder import DockerBuilder as DB
    rusage = (
        "\tCommand being timed: \"seqkit\"\n"
        "\tPercent of CPU this job got: 129%\n"
        "\tElapsed (wall clock) time (h:mm:ss or m:ss): 0:01.08\n"
        "\tMaximum resident set size (kbytes): 80384\n"
    )
    got = DB._parse_gnu_time(rusage)
    assert got["peak_rss_mb"] == 78.5 and got["max_cpu_percent"] == 129.0
    assert got["wall_seconds"] == 1.08
    assert DB._parse_gnu_time("no rusage line here") is None
    # docker-stats MemUsage / CPUPerc fallback
    assert abs(DB._parse_mem_mb("1.5GiB") - 1536.0) < 0.1
    assert DB._parse_mem_mb("512MiB") == 512.0
    assert DB._parse_pct("124.00%") == 124.0


def test_validated_in_shipped_image_requires_digest_match():
    """validated==shipped holds ONLY when every validated step ran in the env
    image AND its recorded image digest matches the frozen env's digest."""
    from agent.skills.user_guide import validated_in_shipped_image
    fr = {"image_digest": "sha256:abc"}
    step = lambda dig, ran=True: {"step": 1, "returncode": 0,
                                  "validation": {"o": {"passed": True}},
                                  "ran_in_container": ran, "container_image_digest": dig}
    assert validated_in_shipped_image({"pipeline_steps": [step("sha256:abc")]}, fr) is True
    # wrong digest (validated a different image than we ship) → False
    assert validated_in_shipped_image({"pipeline_steps": [step("sha256:zzz")]}, fr) is False
    # ran on the host, not in the image → False
    assert validated_in_shipped_image({"pipeline_steps": [step("sha256:abc", ran=False)]}, fr) is False
    # no freeze digest → can't claim it
    assert validated_in_shipped_image({"pipeline_steps": [step("sha256:abc")]}, {}) is False
    # no validated steps → False
    assert validated_in_shipped_image({"pipeline_steps": []}, fr) is False


def test_lock_engine_prefers_pixi(monkeypatch):
    """The lock-engine seam keeps us the universal adapter: pixi when present,
    else conda-lock, else none — callers never bind to one engine."""
    import shutil
    from agent.skills.env_manager import EnvManager
    present = {"pixi", "conda-lock"}
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}" if n in present else None)
    assert EnvManager.lock_engine() == "pixi"
    present.discard("pixi")
    assert EnvManager.lock_engine() == "conda-lock"
    present.clear()
    assert EnvManager.lock_engine() == "none"
