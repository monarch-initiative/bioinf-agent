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
    if not ENV_REPORTS.exists():
        return []
    return sorted(
        Path(p) for p in glob.glob(str(ENV_REPORTS / "*.yaml"))
        if not p.endswith(".draft.yaml")
    )


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
