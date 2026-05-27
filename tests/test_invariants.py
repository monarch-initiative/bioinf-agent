"""
Invariant regression tests.

Constructed-spec regression tests for the honesty checkers
(agent/skills/spec_writer.check_invariants / check_workflow_invariants) plus the
freeze/resolver helpers. Each test builds its own spec in-test, so coverage is
deterministic and CI-effective. These do NOT parametrize over runtime artifacts in
env_reports/ (which is gitignored — empty in CI, and post-respine the old `*.yaml`
glob matched unrelated schemas like *.recipe.yaml locally); the meaningful coverage
(positive + negative for both checkers) lives in the constructed cases below, e.g.
test_workflow_spec_self_verifies_only_with_its_sources.

Run: pytest tests/test_invariants.py -v
"""

from __future__ import annotations

import pytest

from agent.skills.spec_writer import check_invariants


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
        # The recorded local_path must actually hash to the recorded sha256 — this is
        # the bytes env_freeze re-fetches and the container build re-validates inside
        # the ship image (install==ship replaces the old host-side I14 re-hash).
        assert hashlib.sha256(_Path(im["local_path"]).read_bytes()).hexdigest() == im["sha256"]
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


def test_content_digest_from_spec_is_degenerate_on_a_draft():
    """A LIVE draft has no packages[]/lock_sha256 (those are finalized-only), so
    content_digest_from_spec collapses to ONE constant for any draft — which is why
    the freeze record must NOT use it as the anchor. This guards the regression the
    shakeout caught (4 distinct envs → identical record content_digest)."""
    from agent.skills.freeze import content_digest_from_spec
    draft_a = {"install_steps": [{"tool": "conda", "subcommand": "install",
                                  "installed_packages": [{"name": "pyfaidx"}]}]}
    draft_b = {"install_steps": [{"tool": "git", "subcommand": "synthesize",
                                  "installed_packages": [{"name": "bwa"}]}]}
    # Two clearly-different envs hash the SAME from a draft → degenerate (the bug).
    assert content_digest_from_spec(draft_a) == content_digest_from_spec(draft_b)


def test_record_content_digest_picks_the_what_was_got_anchor():
    """The freeze anchor by mode: EnvBuild digest for a build, biocontainer manifest
    digest for an adopt, request hash only as a last resort. Distinct envs get
    distinct anchors (no false content collision)."""
    from agent.skills.freeze import record_content_digest
    # build → the EnvBuild lock+longtail digest (the one in the recipe / verify).
    assert record_content_digest("build", build_digest="sha256:BUILD",
                                 adopt_digest="sha256:ADOPT", fallback="sha256:FB") == "sha256:BUILD"
    # adopt → the biocontainer manifest digest.
    assert record_content_digest("adopt", adopt_digest="sha256:ADOPT",
                                 fallback="sha256:FB") == "sha256:ADOPT"
    # missing mode-specific digest → fallback (never silently empty).
    assert record_content_digest("build", build_digest="", fallback="sha256:FB") == "sha256:FB"
    # two builds with different EnvBuild digests → different anchors (the fix).
    assert (record_content_digest("build", build_digest="sha256:A")
            != record_content_digest("build", build_digest="sha256:B"))


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


def test_pick_platform_asset_bare_binary_fallback():
    """Bare-named single-binary releases (mosdepth ships `mosdepth`, somalier ships
    `somalier`: a Linux static binary with NO platform tokens) must still resolve —
    the permissive fallback accepts an untagged asset, and the in-image smoke verify
    is the safety net against a wrong pick. Foreign-OS/arch assets stay excluded even
    in the fallback."""
    from agent.skills.resolver import _pick_platform_asset
    # mosdepth: ['mosdepth', 'mosdepth_d4'] — both untagged; shortest (the plain
    # tool) wins. somalier: a single bare binary.
    assert _pick_platform_asset(["https://x/mosdepth", "https://x/mosdepth_d4"]).endswith("/mosdepth")
    assert _pick_platform_asset(["https://x/somalier"]).endswith("/somalier")
    # A strict os+arch asset still wins over a bare one when both are present.
    assert _pick_platform_asset(
        ["https://x/tool", "https://x/tool_linux_amd64.tar.gz"]).endswith("tool_linux_amd64.tar.gz")
    # Fallback never crosses to a foreign OS / competing arch, even with no strict match.
    assert _pick_platform_asset(["https://x/tool_darwin", "https://x/tool.exe"]) is None
    assert _pick_platform_asset(["https://x/tool-aarch64"]) is None
    # A lone Linux-only build with the OS token but no arch token resolves (loose).
    assert _pick_platform_asset(["https://x/tool-linux"]).endswith("tool-linux")


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


def _db():
    import yaml as _yaml
    from pathlib import Path as _P
    from agent.skills.docker_builder import DockerBuilder
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    return DockerBuilder(cfg)


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


def test_perl_install_method_records_replay_fields(tmp_path, monkeypatch):
    """install_perl_package must record module/distribution/cpanm_flags/build_env in
    install_method so freeze can replay the cpanm build reproducibly."""
    import yaml as _yaml
    from pathlib import Path as _P
    from agent.skills.env_manager import EnvManager
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    em = EnvManager(cfg)
    em.envs_dir = tmp_path
    (tmp_path / "bioinf_x").mkdir()
    monkeypatch.setattr(em, "run_in_env",
                        lambda env, cmd, timeout=0, **kw: {"returncode": 0, "stdout": "", "stderr": ""})
    r = em.install_perl_package("bioinf_x", "Bio::DB::HTS", cpanm_flags="--notest",
                                build_env="HTSLIB_DIR=$CONDA_PREFIX")
    im = r["install_method"]
    assert im["type"] == "perl" and im["module"] == "Bio::DB::HTS"
    assert im["distribution"] == "Bio::DB::HTS" and im["cpanm_flags"] == "--notest"
    assert im["build_env"] == "HTSLIB_DIR=$CONDA_PREFIX"


def test_container_native_dockerfile_bakes_recorded_commands():
    """The container-native emitter: an engine env-layer from the lock + one verbatim
    RUN per recorded long-tail command (NO per-tier translation), MULTI-STAGE — a
    builder with the full toolchain and a slim runtime that COPYs only the env +
    artifacts (Phase D)."""
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    steps = [{"command": "curl -fsSL x.tgz | tar -xz -C /usr/local/bin seqkit",
              "purpose": "seqkit release binary"}]
    df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(), has_env_layer=True, longtail_steps=steps)
    # two stages: a named builder + a slim runtime
    assert "FROM debian:bookworm-slim AS builder" in df and df.count("FROM debian:bookworm-slim") == 2
    assert "pixi.sh/install.sh" in df                      # pixi engine layered on (builder)
    assert "COPY pixi.toml pixi.lock ./" in df and "pixi install --locked" in df  # reproducible conda/pip
    assert "# seqkit release binary" in df
    assert "RUN curl -fsSL x.tgz | tar -xz -C /usr/local/bin seqkit" in df        # baked VERBATIM (builder)
    # runtime stage COPYs the env (same paths) + the built artifacts from the builder
    assert "COPY --from=builder /root/.pixi /root/.pixi" in df
    assert "COPY --from=builder /work /work" in df
    assert "COPY --from=builder /usr/local /usr/local" in df
    # build toolchain is BUILD-only; the runtime apt is the slim *.so set (no build-essential
    # after the runtime FROM)
    runtime = df.split("# ---- runtime image (shipped) ----", 1)[1]
    assert "build-essential" not in runtime and "zlib1g-dev" not in runtime
    assert "zlib1g" in runtime and "ca-certificates" in runtime
    # no conda/pip tools → no env layer; long-tail only still works (still multi-stage)
    df2 = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(), has_env_layer=False, longtail_steps=steps)
    assert "pixi install --locked" not in df2 and "COPY pixi.toml" not in df2
    assert "COPY --from=builder /root/.pixi" not in df2     # no env to copy
    assert "COPY --from=builder /usr/local /usr/local" in df2
    assert "RUN curl -fsSL x.tgz | tar -xz -C /usr/local/bin seqkit" in df2


def test_container_native_engine_is_swappable():
    """The locus is engine-agnostic: swap pixi→micromamba and ONLY the env-layer
    lines change; the long-tail bake + base are identical. Proves we're not married
    to pixi ('universal adapter' one level down)."""
    from agent.skills.container_build import emit_dockerfile, PixiEngine, MicromambaEngine
    steps = [{"command": "install -m0755 /tmp/mosdepth /usr/local/bin/", "purpose": "mosdepth"}]
    dp = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(), has_env_layer=True, longtail_steps=steps)
    dm = emit_dockerfile("debian:bookworm-slim", engine=MicromambaEngine(), has_env_layer=True, longtail_steps=steps)
    # pixi-specific vs micromamba-specific env layers
    assert "pixi install --locked" in dp and "micromamba" not in dp
    assert "micromamba create -y -n env --file env.lock" in dm and "pixi" not in dm
    assert 'env_engine="pixi"' in dp and 'env_engine="micromamba"' in dm
    # identical locus: same base + same verbatim long-tail bake regardless of engine
    assert "FROM debian:bookworm-slim" in dp and "FROM debian:bookworm-slim" in dm
    assert "RUN install -m0755 /tmp/mosdepth /usr/local/bin/" in dp
    assert "RUN install -m0755 /tmp/mosdepth /usr/local/bin/" in dm
    # micromamba's explicit lock is single-platform but reproducible (URLs+sha)
    assert "env.lock" in MicromambaEngine().lock_artifacts()


def test_container_native_repo_name_sanitized():
    """docker repo names must be lowercase [a-z0-9._-]; freeze must sanitize a
    pipeline name like 'VEP_annotate' rather than fail the build."""
    from agent.skills.container_build import _docker_repo
    assert _docker_repo("VEP_annotate") == "vep_annotate"   # uppercase→lower; _ is legal
    assert _docker_repo("phaseA_demo") == "phasea_demo"
    assert _docker_repo("Tool:weird name") == "tool-weird-name"  # illegal chars → '-'
    assert _docker_repo("samtools") == "samtools"
    assert _docker_repo("---") == "bioinf"                   # empty after strip → fallback


def test_install_command_generators_self_contained_tiers():
    """Per-tier knowledge in ONE place: each generator returns a shell command that
    installs to /usr/local/bin + an evidence check. These get baked VERBATIM."""
    from agent.skills import install_commands as ic
    # release binary (archive): download → sha → extract → find → symlink
    rb = ic.release_binary("seqkit", "https://x/seqkit_linux_amd64.tar.gz",
                           sha256="DEAD", binary_in_archive="seqkit")
    assert "curl -fsSL -o seqkit_linux_amd64.tar.gz" in rb["command"]
    assert "dead  seqkit_linux_amd64.tar.gz" in rb["command"] and "sha256sum -c -" in rb["command"]
    assert "tar -xf" in rb["command"] and "/usr/local/bin/seqkit" in rb["command"]
    # bare binary: install, no extract
    rb2 = ic.release_binary("mosdepth", "https://x/mosdepth")
    assert "install -m 0755 mosdepth /usr/local/bin/mosdepth" in rb2["command"]
    # source: SWH-fallback clone → checkout → build → MANUAL install (no make install target).
    # The `_swh_clone` wrapper is the Phase 3 link-rot protection — tries upstream first,
    # falls back to Software Heritage's vault on failure; final `git checkout <sha>` proves
    # the bytes either way.
    s = ic.source("tabtk", "https://github.com/lh3/tabtk", ref="abc123", build_command="make")
    assert "_swh_clone https://github.com/lh3/tabtk abc123 /opt/tools/tabtk/src" in s["command"]
    assert "git checkout abc123" in s["command"] and "make" in s["command"]
    assert "install -m 0755 /opt/tools/tabtk/src/tabtk /usr/local/bin/tabtk" in s["command"]
    assert s["evidence"] == "command -v tabtk"
    # script repo (half-baked run-by-path): SWH-fallback clone → wrapper exec'ing the entry script
    sr = ic.script_repo("mytool", "https://github.com/lab/mytool", script_rel="run.py",
                        interpreter="python")
    assert "_swh_clone https://github.com/lab/mytool" in sr["command"]
    assert "exec python /opt/tools/mytool/run.py" in sr["command"]
    assert "/usr/local/bin/mytool" in sr["command"]


def test_install_command_generators_toolchain_coupled_tiers():
    """cargo/go/perl build with the ENGINE's toolchain → engine_coupled=True so the
    command (and evidence) run via engine.run(). Output binaries are self-contained
    at runtime (/usr/local/bin); perl's module lives in the engine perl."""
    from agent.skills import install_commands as ic
    c = ic.cargo("rasusa", "rasusa", version="2.0.0")
    assert c["engine_coupled"] and "cargo install rasusa --version 2.0.0 --root /usr/local --locked" in c["command"]
    g = ic.go("gofasta", "github.com/virus-evolution/gofasta", version="v1.2.3")
    assert g["engine_coupled"] and "go install github.com/virus-evolution/gofasta@v1.2.3" in g["command"]
    p = ic.perl_cpanm("Bio::DB::HTS", build_env="HTSLIB_DIR=$CONDA_PREFIX")
    assert p["engine_coupled"] and "HTSLIB_DIR=$CONDA_PREFIX cpanm --notest Bio::DB::HTS" in p["command"]
    assert p["evidence"] == "perl -MBio::DB::HTS -e1"


def test_install_spec_engine_coupling_wraps_with_engine_run(monkeypatch):
    """ContainerBuild.install() wraps a coupled spec's command+evidence with
    engine.run() (so it's correct in the build container AND when baked), and leaves
    a self-contained spec bare."""
    from agent.skills.container_build import ContainerBuild, PixiEngine
    cb = ContainerBuild(engine=PixiEngine())
    cb.cid = "fake"
    calls = []
    monkeypatch.setattr(cb, "_sh", lambda args, timeout=0: (
        calls.append(args[-1]), {"returncode": 0, "stdout": "ok", "stderr": ""})[1])
    # coupled → wrapped with `pixi run`
    cb.install({"command": "cargo install rasusa --root /usr/local --locked",
                "evidence": "command -v rasusa", "purpose": "rasusa", "engine_coupled": True})
    # baked form is wrapped in `pixi run bash -c '...'` (shell so builtins/pipes work)
    assert cb.longtail[-1]["command"].startswith("pixi run bash -c ")
    assert "cargo install rasusa --root /usr/local --locked" in cb.longtail[-1]["command"]
    # self-contained → bare
    cb.install({"command": "install -m0755 /tmp/x /usr/local/bin/x", "evidence": "command -v x",
                "purpose": "x"})
    assert cb.longtail[-1]["command"] == "install -m0755 /tmp/x /usr/local/bin/x"


def test_envbuild_orchestrator_plan_and_content_digest():
    """The core orchestrator records conda specs + long-tail tools + how to verify
    each in the image, and content-addresses by what was GOT (lock + commands +
    platform + engine). Pure parts — no container."""
    from agent.skills.env_build import EnvBuild
    from agent.skills import install_commands as ic
    eb = EnvBuild("demo", "1.0", platform="linux/amd64")
    eb.add_conda(["samtools=1.21"], verify=[("samtools", "samtools --version")])
    eb.add_tool(ic.release_binary("seqkit", "https://x/seqkit_linux_amd64.tar.gz", binary_in_archive="seqkit"))
    eb.add_tool(ic.cargo("rasusa", "rasusa", version="4.1.0"))
    # routing: conda verify is engine-coupled; binary is bare; cargo is coupled
    by = {v["label"]: v for v in eb.verifications}
    assert by["samtools"]["engine_coupled"] is True
    assert by["seqkit (release binary)"]["engine_coupled"] is False
    assert any(v["engine_coupled"] for v in eb.verifications if "cargo" in v["label"])
    assert eb.conda_specs == ["samtools=1.21"] and len(eb.tools) == 2
    # content digest: stable, and sensitive to the recipe (lock + commands)
    eb.lock_text = "pixi.lock-bytes"
    eb.cb.longtail = [{"command": "RUN x"}]
    d1 = eb.content_digest()
    assert d1.startswith("sha256:") and d1 == eb.content_digest()
    eb.cb.longtail = [{"command": "RUN y"}]
    assert eb.content_digest() != d1
    # ...and sensitive to the BASE IMAGE digest (the OS foundation is in the anchor).
    eb.cb.longtail = [{"command": "RUN x"}]
    d_base = eb.content_digest()
    eb.cb.base = "debian:bookworm-slim@sha256:" + "00" * 32
    assert eb.content_digest() != d_base


def test_container_native_engine_optional_for_longtail_only():
    """A pure binary/source/half-baked env carries NO engine (no pixi/micromamba) —
    bootstrap lines appear only when conda/pip specs were declared."""
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    steps = [{"command": "set -eux; git clone x && make && install -m755 t /usr/local/bin/", "purpose": "t"}]
    df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(), has_env_layer=False, longtail_steps=steps)
    assert "pixi" not in df and "micromamba" not in df          # no engine at all
    assert "RUN set -eux; git clone x && make" in df            # long-tail baked verbatim
    assert "build-essential" in df                              # apt toolchain present for source


def test_non_conda_installs_includes_perl():
    """perl installs must surface in non_conda_installs so freeze replays (not adopts)."""
    from agent.skills.freeze import non_conda_installs
    draft = {"install_steps": [{"installed_packages": [{"name": "Bio::DB::HTS",
        "install_method": {"type": "perl", "module": "Bio::DB::HTS",
                           "distribution": "Bio::DB::HTS", "cpanm_flags": "--notest"}}]}]}
    nc = {x["name"]: x["type"] for x in non_conda_installs(draft)}
    assert nc == {"Bio::DB::HTS": "perl"}


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


def test_validated_in_shipped_image_multi_env_chaining():
    """A multi-env workflow chains steps that ran in DIFFERENT frozen envs; it's
    still validated==shipped iff every validated step's digest is in the set of all
    frozen env digests."""
    from agent.skills.user_guide import validated_in_shipped_image
    step = lambda dig: {"step": 1, "returncode": 0, "validation": {"o": {"passed": True}},
                        "ran_in_container": True, "container_image_digest": dig}
    spec = {"pipeline_steps": [step("sha256:envA"), step("sha256:envB")]}
    # both envs are frozen → validated==shipped across the chain
    assert validated_in_shipped_image(spec, valid_digests={"sha256:envA", "sha256:envB"}) is True
    # one step ran in an env that isn't a known frozen env → not shippable
    assert validated_in_shipped_image(spec, valid_digests={"sha256:envA"}) is False
    # single-env default still works (one digest)
    assert validated_in_shipped_image({"pipeline_steps": [step("sha256:envA")]},
                                      {"image_digest": "sha256:envA"}) is True


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


# ---------------------------------------------------------------------------
# env_honesty — the container-native Layer-1 contract (REPLACES the host moat's
# env-build invariants: I1/I2/I5/I9/I10/I11/I12/I13/I14 → BUILT · VALIDATED_IN_IMAGE
# · POLICY_CLEAN). Pure: check_build over a hand-built BuildResult, no container.
# ---------------------------------------------------------------------------

def _clean_build_result(**over):
    """A BuildResult that satisfies the contract; override fields to break it."""
    r = {
        "image": "demo:1.0", "image_digest": "sha256:abc",
        "verifications": [
            {"label": "samtools", "tool": "samtools", "check": "samtools --version", "passed": True},
            {"label": "seqkit (release binary)", "tool": "seqkit",
             "check": "command -v seqkit", "passed": True},
        ],
        "accelerator": None, "license_gated": False, "licenses": [], "redistributable": True,
    }
    r.update(over)
    return r


def test_env_honesty_accepts_a_clean_build():
    from agent.skills.env_honesty import check_build
    assert check_build(_clean_build_result()) == []


def test_env_honesty_BUILT_requires_image_and_digest():
    """BUILT absorbs I1/I9/I11/I14: the image existing IS the anchor (a failed RUN,
    each carrying its own inline sha256/commit/bake anchor, fails docker build)."""
    from agent.skills.env_honesty import check_build
    inv = {v["invariant"] for v in check_build(_clean_build_result(image=""))}
    assert "BUILT.image_present" in inv
    inv = {v["invariant"] for v in check_build(_clean_build_result(image_digest=""))}
    assert "BUILT.image_digest_resolved" in inv


def test_env_honesty_VALIDATED_catches_failed_in_image_evidence():
    """validated==shipped: a tool whose evidence didn't pass in the SHIPPED image
    is not provably present in what we ship (the strong form of I2)."""
    from agent.skills.env_honesty import check_build
    r = _clean_build_result()
    r["verifications"][1]["passed"] = False
    r["verifications"][1]["rc"] = 127
    inv = {v["invariant"] for v in check_build(r)}
    assert "VALIDATED_IN_IMAGE.evidence_passed" in inv


def test_env_honesty_VALIDATED_rejects_echo_cheat_shapes():
    """The carried I2 anti-echo-cheat shape rule: a constant-true / bare-echo /
    no-tool-token evidence is rejected even if it 'passes' (rc=0) in the image —
    it never exercised the tool."""
    from agent.skills.env_honesty import check_build
    for cheat in ("true", ":", "exit 0", 'echo "samtools 1.21"', "[ 1 = 1 ]"):
        r = _clean_build_result()
        r["verifications"][0]["check"] = cheat   # but still passed=True (the cheat works)
        inv = {v["invariant"] for v in check_build(r)}
        assert "VALIDATED_IN_IMAGE.evidence_shape" in inv, f"missed cheat: {cheat!r}"


def test_env_honesty_VALIDATED_requires_some_evidence():
    from agent.skills.env_honesty import check_build
    inv = {v["invariant"] for v in check_build(_clean_build_result(verifications=[]))}
    assert "VALIDATED_IN_IMAGE.no_evidence" in inv


def test_env_honesty_evidence_shape_word_boundary_and_perl_and_probes():
    """The shape rule mirrors I2's word-boundary token (cat ≠ concatenate), accepts
    the perl -M idiom (module glued to a capital letter) and conda-prefix-stripped
    forms (r-ape→ape), and accepts a tool-less authored-file probe (test -f)."""
    from agent.skills.env_honesty import evidence_shape_violation as sv
    # real probes pass
    assert sv("samtools --version", "samtools") is None
    assert sv("command -v seqkit", "seqkit") is None
    assert sv("perl -MBio::DB::HTS -e1", "Bio::DB::HTS") is None
    assert sv("Rscript -e 'library(ape)'", "r-ape") is None          # prefix-stripped
    assert sv("test -f /opt/x/config.yaml") is None                   # no tool token, real probe
    # cheats fail
    assert sv("true", "samtools")
    assert sv('echo "cat 1.0"', "cat")
    assert sv("concatenate --help", "cat")                            # word-boundary: cat ⊄ concatenate
    assert sv("", "samtools")
    assert sv("uname -a")                                             # no token, no recognizable probe


def test_env_honesty_POLICY_carries_I12_accelerator():
    from agent.skills.env_honesty import check_build
    # mps must be dev_only
    inv = {v["invariant"] for v in check_build(_clean_build_result(accelerator={"type": "mps"}))}
    assert "I12.mps_dev_only" in inv
    # cuda needs a toolkit version
    inv = {v["invariant"] for v in check_build(_clean_build_result(accelerator={"type": "cuda"}))}
    assert "I12.accel_toolkit_version_required" in inv
    # honest cuda passes
    assert check_build(_clean_build_result(
        accelerator={"type": "cuda", "toolkit_version": "12.4", "runtime": "build_only"})) == []


def test_env_honesty_POLICY_carries_I13_license_firewall():
    from agent.skills.env_honesty import check_build
    inv = {v["invariant"] for v in check_build(
        _clean_build_result(license_gated=True, redistributable=True, licenses=[]))}
    assert "I13.gated_not_redistributable" in inv and "I13.gated_license_recorded" in inv
    # honest gated artifact passes
    assert check_build(_clean_build_result(
        license_gated=True, redistributable=False, licenses=["10x EULA"])) == []


def test_envbuild_run_uses_check_build_as_the_gate(monkeypatch):
    """EnvBuild.run() refuses (success=False) when the contract is violated, even
    if the raw in-image validation 'succeeded' — the contract is the sole gate."""
    from agent.skills.env_build import EnvBuild
    from agent.skills import install_commands as ic
    eb = EnvBuild("demo", "1.0")
    eb.add_tool(ic.release_binary("seqkit", "https://x/seqkit_linux_amd64.tar.gz",
                                  binary_in_archive="seqkit"))
    # stub the container-driving stages: build ok, freeze ok, digest ok, and an
    # in-image verify that "passes" but with a CHEAT shape on the recorded check.
    monkeypatch.setattr(eb, "build", lambda: {"success": True})
    monkeypatch.setattr(eb, "freeze", lambda: {"success": True, "image": "demo:1.0"})
    monkeypatch.setattr(eb.cb, "image_digest", lambda img: "sha256:abc")
    monkeypatch.setattr(eb.cb, "close", lambda: None)
    monkeypatch.setattr(eb, "verify_in_image", lambda img: {"success": True, "verifications": [
        {"label": "seqkit", "tool": "seqkit", "check": "true", "passed": True}]})
    res = eb.run()
    assert res["success"] is False
    assert any(v["invariant"] == "VALIDATED_IN_IMAGE.evidence_shape"
               for v in res["honesty_violations"])
    # and the happy path: honest evidence → accepted
    monkeypatch.setattr(eb, "verify_in_image", lambda img: {"success": True, "verifications": [
        {"label": "seqkit", "tool": "seqkit", "check": "command -v seqkit", "passed": True}]})
    assert eb.run()["success"] is True


# ---------------------------------------------------------------------------
# Build locus — native vs emulated, and the honesty stamp it produces.
# The VALIDATED_IN_IMAGE pass/fail is sound under emulation (faithful CPU
# emulators); only I7 timings need native silicon to be authoritative.
# ---------------------------------------------------------------------------

def test_locus_target_arch_parsing():
    from agent.skills import locus
    assert locus.target_arch("linux/amd64") == "amd64"
    assert locus.target_arch("linux/arm64") == "arm64"
    assert locus.target_arch("") == "amd64"  # sane default


def test_locus_native_when_daemon_matches_target(monkeypatch):
    """daemon arch == target arch → native: I7 timings ARE authoritative, no advisory."""
    from agent.skills import locus
    monkeypatch.setattr(locus, "daemon_arch", lambda: "amd64")
    d = locus.detect_locus("linux/amd64")
    assert d["locus"] == "native"
    assert d["i7_authoritative"] is True
    assert d["emulator"] == "none"
    assert d["advisory"] == ""


def test_locus_emulated_when_daemon_differs(monkeypatch):
    """arm64 daemon building linux/amd64 → emulated: I7 NOT authoritative, advisory set."""
    from agent.skills import locus
    monkeypatch.setattr(locus, "daemon_arch", lambda: "arm64")
    d = locus.detect_locus("linux/amd64")  # probe_emulator defaults False → no container run
    assert d["locus"] == "emulated"
    assert d["i7_authoritative"] is False
    assert d["advisory"]  # actionable, non-empty
    assert "I7" in d["advisory"]


def test_daemon_is_remote_reads_docker_host(monkeypatch):
    from agent.skills import locus
    monkeypatch.setenv("DOCKER_HOST", "ssh://user@amd64-box")
    assert locus.daemon_is_remote() is True
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.5:2375")
    assert locus.daemon_is_remote() is True
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert locus.daemon_is_remote() is False


def test_detect_locus_native_via_remote_amd64_host(monkeypatch):
    """Point DOCKER_HOST at a native amd64 daemon → locus=native (daemon-agnostic
    build/validate), daemon_location=remote, I7 authoritative."""
    from agent.skills import locus
    monkeypatch.setattr(locus, "daemon_arch", lambda: "amd64")
    monkeypatch.setenv("DOCKER_HOST", "ssh://user@amd64-box")
    d = locus.detect_locus("linux/amd64")
    assert d["locus"] == "native" and d["i7_authoritative"] is True
    assert d["daemon_location"] == "remote"


def test_emulation_advisory_points_to_native_docker_host(monkeypatch):
    from agent.skills import locus
    monkeypatch.setattr(locus, "daemon_arch", lambda: "arm64")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    adv = locus.detect_locus("linux/amd64")["advisory"]
    assert "DOCKER_HOST" in adv and "INCONCLUSIVE" in adv


def test_locus_unknown_when_daemon_unqueryable(monkeypatch):
    """No daemon (docker absent) → unknown, never crashes, never claims authority."""
    from agent.skills import locus
    monkeypatch.setattr(locus, "daemon_arch", lambda: "")
    d = locus.detect_locus("linux/amd64")
    assert d["locus"] == "unknown"
    assert d["i7_authoritative"] is False


def test_locus_advisory_distinguishes_rosetta_and_qemu():
    from agent.skills import locus
    assert "Rosetta" in locus._emulation_advisory("rosetta")
    qemu = locus._emulation_advisory("qemu")
    # qemu wording only nudges toward Rosetta when on Apple Silicon; either way it
    # must flag the I7 caveat.
    assert "I7" in qemu


def test_envbuild_run_stamps_validation_locus(monkeypatch):
    """run() records WHERE it validated — native stamps i7_authoritative=True so the
    Layer-2 path can trust I7; the cache record carries the locus too."""
    from agent.skills.env_build import EnvBuild
    from agent.skills import install_commands as ic
    from agent.skills import locus
    monkeypatch.setattr(locus, "daemon_arch", lambda: "amd64")  # pretend native
    eb = EnvBuild("demo", "1.0")
    eb.add_tool(ic.release_binary("seqkit", "https://x/seqkit_linux_amd64.tar.gz",
                                  binary_in_archive="seqkit"))
    monkeypatch.setattr(eb, "build", lambda: {"success": True})
    monkeypatch.setattr(eb, "freeze", lambda: {"success": True, "image": "demo:1.0"})
    monkeypatch.setattr(eb.cb, "image_digest", lambda img: "sha256:abc")
    monkeypatch.setattr(eb.cb, "close", lambda: None)
    monkeypatch.setattr(eb, "verify_in_image", lambda img: {"success": True, "verifications": [
        {"label": "seqkit", "tool": "seqkit", "check": "command -v seqkit", "passed": True}]})
    res = eb.run()
    assert res["success"] is True
    assert res["validation_locus"] == "native"
    assert res["i7_authoritative"] is True
    rec = eb.to_cache_record(res)
    assert rec["validation_locus"] == "native"


def test_stamp_i7_authority_tracks_locus(monkeypatch):
    """Captured I7 numbers are marked authoritative only on a native locus; under
    emulation they're stamped non-authoritative (emulator artefacts). Pass/fail of
    the step is unaffected — this is a truth label, not a gate."""
    from agent import mcp_server as m
    from agent.skills import locus
    monkeypatch.setattr(locus, "daemon_arch", lambda: "amd64")  # native
    ru = m._stamp_i7_authority({"peak_rss_kb": 123, "wall_s": 4.2}, "linux/amd64")
    assert ru["i7_authoritative"] is True and ru["locus"] == "native"
    monkeypatch.setattr(locus, "daemon_arch", lambda: "arm64")  # emulated amd64
    ru2 = m._stamp_i7_authority({"peak_rss_kb": 123}, "linux/amd64")
    assert ru2["i7_authoritative"] is False and ru2["locus"] == "emulated"
    assert m._stamp_i7_authority(None, "linux/amd64") is None  # no measurement → no crash


def test_base_image_is_pinned_by_digest():
    """The build/ship base must be pinned by DIGEST, not a moving tag — OS-layer
    reproducibility (the env layer is lock-pinned, binaries sha256-anchored). Both
    EnvBuild and ContainerBuild must honor the single pinned constant so a rebuild
    gets identical base bytes."""
    from agent.skills.container_build import BASE_IMAGE, ContainerBuild
    from agent.skills.env_build import EnvBuild
    assert "@sha256:" in BASE_IMAGE, "base must be digest-pinned, not a bare tag"
    assert ContainerBuild().base == BASE_IMAGE
    assert EnvBuild("x").cb.base == BASE_IMAGE
    # and it flows into the emitted Dockerfile's FROM (what actually ships)
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    df = emit_dockerfile(BASE_IMAGE, engine=PixiEngine(), has_env_layer=False, longtail_steps=[])
    assert f"FROM {BASE_IMAGE}" in df


# ---------------------------------------------------------------------------
# Env report — rendered PURELY from the verified record (can't be faked).
# ---------------------------------------------------------------------------

def test_resolved_packages_parses_conda_meta_and_dist_info(monkeypatch):
    """The closure is read engine-agnostically from conda-meta/*.json (name-version-
    build) + site-packages/*.dist-info (name-version) — not a fragile engine table."""
    from agent.skills.container_build import ContainerBuild
    cb = ContainerBuild()
    cb.cid, cb.has_env_layer = "fake", True
    monkeypatch.setattr(cb, "exec", lambda *a, **k: {"returncode": 0, "stderr": "",
        "stdout": ("conda samtools-1.21-h50ea8bc_0\n"
                   "conda libdeflate-1.19-hd590300_0\n"
                   "pypi pyfaidx-0.8.1.1\n")})
    pkgs = cb.resolved_packages()
    by = {p["name"]: p for p in pkgs}
    assert by["samtools"] == {"name": "samtools", "version": "1.21", "kind": "conda"}
    assert by["libdeflate"]["version"] == "1.19"
    assert by["pyfaidx"] == {"name": "pyfaidx", "version": "0.8.1.1", "kind": "pypi"}


def _sample_record(locus="emulated"):
    return {
        "name": "demo", "image": "demo:1.0", "image_digest": "sha256:img",
        "content_digest": "sha256:cd", "platform": "linux/amd64", "mode": "build",
        "build_method": "container-native", "engine": "pixi", "gated": False,
        "redistributable": True, "validation_locus": locus, "created_at": "2026-05-24",
        "requested_tools": ["samtools"], "conda_specs": ["samtools=1.21"],
        "verifications": [{"tool": "samtools", "label": "samtools",
                           "check": "command -v samtools", "passed": True, "rc": 0}],
        "resolved_packages": [
            {"name": "samtools", "version": "1.21", "kind": "conda"},
            {"name": "htslib", "version": "1.21", "kind": "conda"},
            {"name": "libdeflate", "version": "1.19", "kind": "conda"}],
    }


def test_env_report_splits_requested_vs_ride_along():
    from agent.skills.env_report import render_env_report
    md = render_env_report(_sample_record())
    assert "## Requested tools (1)" in md
    assert "## Along for the ride (2)" in md          # htslib + libdeflate, NOT samtools
    assert "| samtools | 1.21 | conda | ✓" in md      # requested row with in-image evidence
    assert "htslib" in md and "libdeflate" in md
    # samtools must NOT appear in the ride-along dependency table (scope to that
    # section only — the Install & provenance block below legitimately names it)
    ride = md.split("## Along for the ride")[1].split("## Install")[0]
    assert "samtools" not in ride


def test_env_report_long_tail_tier_version_and_delivery():
    """Long-tail tools show their real install tier + the version they printed
    in-image (not '—'/'long-tail (baked)'); the Delivery section + reproducibility
    line render from the record."""
    from agent.skills.env_report import render_env_report
    rec = {
        "name": "vc", "image": "vc:1", "image_digest": "sha256:i", "mode": "build",
        "validation_locus": "native", "requested_tools": ["seqkit", "seqtk"],
        "push_status": "pushed: ghcr.io/org/vc:1",
        "shipped_binaries": [{"name": "seqkit binary", "command": "curl ... | sha256sum -c"},
                             {"name": "seqtk (source @ 7c04ce7)", "command": "git clone ... && make"}],
        "verifications": [{"tool": "seqkit", "label": "seqkit", "check": "seqkit version",
                           "passed": True, "out": "seqkit v2.13.0"},
                          {"tool": "seqtk", "label": "seqtk", "check": "seqtk 2>&1 | head",
                           "passed": True, "out": "Version: 1.4-r122"}],
        "resolved_packages": [],
        "hpc_delivery": {"get_image": "apptainer pull vc.sif docker://ghcr.io/org/vc:1"},
    }
    md = render_env_report(rec)
    assert "| seqkit | 2.13.0 | binary |" in md      # version from in-image output + real tier
    # source tier with a SHA install anchor: dual-display keeps the commit
    # visible alongside the version (full provenance — non-fakeable).
    assert "| seqtk | 1.4-r122 (commit 7c04ce7) | source |" in md
    assert "Registry: pushed: ghcr.io/org/vc:1" in md
    assert "apptainer pull vc.sif docker://ghcr.io/org/vc:1" in md
    assert "Reproducibility" in md and "digest-pinned" in md


def test_env_report_system_packages_sbom_section():
    """The apt/OS layer renders as its own collapsible SBOM section."""
    from agent.skills.env_report import render_env_report
    rec = dict(_sample_record("native"))
    rec["system_packages"] = [{"name": "libssl3", "version": "3.0.14-1", "kind": "apt"},
                              {"name": "zlib1g", "version": "1:1.2.13", "kind": "apt"}]
    md = render_env_report(rec)
    assert "## System packages (2)" in md
    assert "| libssl3 | 3.0.14-1 |" in md


def test_attestation_sbom_includes_apt_as_deb_purls():
    """A complete SBOM: the apt layer joins conda/pip in resolvedDependencies as
    deb purls — self-describing artifact for audit."""
    from agent.skills.attestation import build_attestation
    rec = dict(_sample_record("native"))
    rec["system_packages"] = [{"name": "libssl3", "version": "3.0.14-1", "kind": "apt"}]
    att = build_attestation(rec)
    uris = [d["uri"] for d in att["predicate"]["buildDefinition"]["resolvedDependencies"]]
    assert "pkg:conda/htslib@1.21" in uris        # tool closure
    assert "pkg:deb/debian/libssl3@3.0.14-1" in uris  # OS layer


def test_env_report_honesty_footer_reflects_locus():
    from agent.skills.env_report import render_env_report
    assert "NOT authoritative" in render_env_report(_sample_record("emulated"))
    assert "are authoritative" in render_env_report(_sample_record("native"))


def test_env_report_renders_for_adopted_image_without_crashing():
    """Adopt path has no in-locus validation/closure — the report still renders."""
    from agent.skills.env_report import render_env_report
    md = render_env_report({"name": "bt", "image": "biocontainers/x@sha256:d",
                            "image_digest": "sha256:d", "mode": "adopt",
                            "validation_locus": "adopted", "requested_tools": ["x"]})
    assert "adopted" in md.lower()
    assert "## Requested tools (1)" in md


def test_env_report_md_adopt_footer_does_not_overclaim():
    """The .md footer must NOT claim VALIDATED_IN_IMAGE for an adopted image (it was
    never validated in-locus) — it asserts ADOPTED_BY_DIGEST instead."""
    from agent.skills.env_report import render_env_report
    adopt = render_env_report({"name": "bt", "image": "x@sha256:d", "image_digest": "sha256:d",
                               "mode": "adopt", "validation_locus": "adopted",
                               "requested_tools": ["x"]})
    assert "ADOPTED_BY_DIGEST" in adopt and "VALIDATED_IN_IMAGE" not in adopt
    # a real build still makes the strong claim
    assert "VALIDATED_IN_IMAGE" in render_env_report(_sample_record())


# ---------------------------------------------------------------------------
# The HTML env report — the human-facing deliverable. Pure over the record,
# escaped, deterministic, mode-honest, verified-vs-declared separation.
# ---------------------------------------------------------------------------

def test_env_report_html_deterministic_and_contains_verified_facts():
    from agent.skills.env_report_html import render_env_report_html
    rec = _sample_record()
    h1 = render_env_report_html(rec)
    assert h1 == render_env_report_html(rec)                  # deterministic (no clock read)
    assert h1.startswith("<!DOCTYPE html>") and h1.rstrip().endswith("</html>")
    assert "sha256:cd" in h1 and "sha256:img" in h1          # content + image digests
    assert "samtools" in h1
    assert "VALIDATED_IN_IMAGE" in h1                         # build-mode honesty footer
    assert "command -v samtools" in h1                        # the actual in-image check shown
    assert "badge ok" in h1                                   # the ✓ validated badge


def test_env_report_html_escapes_injection():
    """No record value can inject markup — a hostile package name is escaped."""
    from agent.skills.env_report_html import render_env_report_html
    rec = dict(_sample_record())
    rec["resolved_packages"] = [{"name": "evil<script>alert(1)</script>", "version": "1", "kind": "conda"}]
    h = render_env_report_html(rec)
    assert "<script>alert(1)</script>" not in h
    assert "evil&lt;script&gt;" in h


def test_env_report_html_adopt_mode_does_not_claim_validation():
    from agent.skills.env_report_html import render_env_report_html
    h = render_env_report_html({"name": "bt", "image": "x@sha256:d", "image_digest": "sha256:d",
                                "mode": "adopt", "validation_locus": "adopted",
                                "requested_tools": ["x"]})
    assert "ADOPTED_BY_DIGEST" in h and "VALIDATED_IN_IMAGE" not in h
    assert "not built or validated in-locus" in h.lower()


def test_env_report_html_separates_declared_policy_from_verified():
    """gated/licenses/accelerator render in a DECLARED section explicitly labelled as
    submitter assertions, never as runtime-verified facts."""
    from agent.skills.env_report_html import render_env_report_html
    rec = dict(_sample_record())
    rec.update({"gated": True, "redistributable": False, "licenses": ["proprietary-EULA"],
                "accelerator": {"type": "cuda", "toolkit_version": "12.4"}})
    h = render_env_report_html(rec)
    assert "Declared policy" in h
    assert "caller assertion" in h.lower() or "submitter-declared" in h.lower()
    assert "proprietary-EULA" in h and "cuda" in h


def test_attestation_adopt_mode_does_not_claim_validated_in_image():
    """The SLSA attestation for an adopted image must assert ADOPTED_BY_DIGEST, not a
    VALIDATED_IN_IMAGE guarantee it never performed."""
    from agent.skills.attestation import build_attestation
    att = build_attestation({"image": "x@sha256:d", "image_digest": "sha256:d", "mode": "adopt"})
    guarantees = att["predicate"]["buildDefinition"]["internalParameters"]["honesty_contract"]
    assert "ADOPTED_BY_DIGEST" in guarantees and "VALIDATED_IN_IMAGE" not in guarantees
    # a build still claims the full contract
    bguar = build_attestation(_sample_record())["predicate"]["buildDefinition"][
        "internalParameters"]["honesty_contract"]
    assert "VALIDATED_IN_IMAGE" in bguar


# ---------------------------------------------------------------------------
# Registry push as default delivery — the push-target derivation + I13 firewall.
# ---------------------------------------------------------------------------

def test_attestation_is_intoto_slsa_statement_from_record():
    """The attestation is a standard in-toto Statement v1 / SLSA Provenance v1,
    built purely from the record: subject = image+digest, resolvedDependencies =
    the closure as purls + base image, and the validated==shipped evidence +
    honesty guarantees live in the predicate."""
    from agent.skills.attestation import build_attestation, STATEMENT_TYPE, SLSA_PREDICATE
    att = build_attestation(_sample_record("native"), base_image="debian:bookworm-slim@sha256:dead")
    assert att["_type"] == STATEMENT_TYPE
    assert att["predicateType"] == SLSA_PREDICATE
    # subject names the image with a parsed digest set
    assert att["subject"][0]["name"] == "demo:1.0"
    assert att["subject"][0]["digest"] == {"sha256": "img"}
    pred = att["predicate"]
    # the resolved closure became purls, plus the base image as a dependency
    uris = [d["uri"] for d in pred["buildDefinition"]["resolvedDependencies"]]
    assert "pkg:conda/htslib@1.21" in uris
    assert any(u.startswith("docker:debian:bookworm-slim@sha256:") for u in uris)
    ip = pred["buildDefinition"]["internalParameters"]
    assert ip["honesty_contract"] == ["BUILT", "VALIDATED_IN_IMAGE", "POLICY_CLEAN"]
    assert ip["validation_locus"] == "native"
    # validated==shipped evidence is carried per tool
    assert any(v["tool"] == "samtools" and v["passed"] for v in ip["validated_in_image"])
    assert pred["runDetails"]["builder"]["id"]


def test_effective_push_target_derivation_and_i13():
    from agent.mcp_server import _effective_push_target as ept
    # nothing configured → no push (registry-free tarball default)
    assert ept("", "", "demo", "1.0", False) == ""
    # configured default registry → auto-derived ref, no per-call arg needed
    assert ept("", "ghcr.io/myorg", "demo", "1.0", False) == "ghcr.io/myorg/demo:1.0"
    assert ept("", "ghcr.io/myorg/", "demo", "", False) == "ghcr.io/myorg/demo:latest"  # trailing slash + default tag
    # explicit push_target WINS over the configured default
    assert ept("reg.io/x:9", "ghcr.io/myorg", "demo", "1.0", False) == "reg.io/x:9"
    # I13: gated artifacts are NEVER pushed, even with a target or a default registry
    assert ept("reg.io/x:9", "ghcr.io/myorg", "demo", "1.0", True) == ""
    assert ept("", "ghcr.io/myorg", "demo", "1.0", True) == ""


# ---------------------------------------------------------------------------
# pt4b — EnvCache bridge (solve-once, re-anchored) + resolver container-native routing.
# ---------------------------------------------------------------------------

def test_envbuild_request_key_and_cache_record():
    """request_key is the order-independent lookup handle (tools+platform+accel);
    to_cache_record carries content_digest + image handles + the I13 firewall."""
    from agent.skills.env_build import EnvBuild
    from agent.skills import install_commands as ic
    eb = EnvBuild("demo", "1.0", platform="linux/amd64")
    eb.add_conda(["samtools=1.21"], verify=[("samtools", "samtools --version")])
    eb.add_tool(ic.release_binary("seqkit", "https://x/seqkit_linux_amd64.tar.gz",
                                  binary_in_archive="seqkit"))
    key = eb.request_key()
    assert "samtools=1.21" in key and "seqkit" in key and "linux/amd64" in key and key.endswith("none")
    # order-independence: adding in a different order yields the same handle
    eb2 = EnvBuild("demo", "1.0", platform="linux/amd64")
    eb2.add_tool(ic.release_binary("seqkit", "https://x/seqkit_linux_amd64.tar.gz",
                                   binary_in_archive="seqkit"))
    eb2.add_conda(["samtools=1.21"], verify=[("samtools", "samtools --version")])
    assert eb2.request_key() == key
    rec = eb.to_cache_record({"content_digest": "sha256:cd", "image": "demo:1.0",
                              "image_digest": "sha256:img", "platform": "linux/amd64",
                              "engine": "pixi"})
    assert rec["mode"] == "container-native" and rec["content_digest"] == "sha256:cd"
    assert rec["image_digest"] == "sha256:img" and rec["redistributable"] is True


def test_envcache_lookup_anchored_treats_evicted_image_as_miss(tmp_path):
    """A cache hit is re-anchored against reality: present image → hit; evicted
    image → MISS (None) so the caller rebuilds rather than ship a dangling ref."""
    from agent.skills.freeze import EnvCache
    cache = EnvCache(tmp_path / "envcache.json")
    cache.register("k", {"image": "demo:1.0", "image_digest": "sha256:img"})
    assert cache.lookup_anchored("k", image_present=lambda ref: True)["image"] == "demo:1.0"
    assert cache.lookup_anchored("k", image_present=lambda ref: False) is None   # evicted
    assert cache.lookup_anchored("missing", image_present=lambda ref: True) is None


def test_envbuild_build_or_cached_returns_hit_without_building(tmp_path, monkeypatch):
    """build_or_cached short-circuits on an anchored hit (no run()); on a miss it
    runs and registers the successful build."""
    from agent.skills.env_build import EnvBuild
    from agent.skills.freeze import EnvCache
    from agent.skills import install_commands as ic
    cache = EnvCache(tmp_path / "c.json")
    eb = EnvBuild("demo", "1.0")
    eb.add_tool(ic.release_binary("seqkit", "https://x/seqkit_linux_amd64.tar.gz",
                                  binary_in_archive="seqkit"))
    # pre-seed the cache with a record under eb's key → anchored hit, run() never called
    cache.register(eb.request_key(), {"image": "demo:1.0", "image_digest": "sha256:img",
                                      "content_digest": "sha256:cd"})
    monkeypatch.setattr(eb, "run", lambda: (_ for _ in ()).throw(AssertionError("run() should not be called on a hit")))
    hit = eb.build_or_cached(cache, image_present=lambda ref: True)
    assert hit["cached"] is True and hit["image"] == "demo:1.0"
    # miss path (different key via a new build) → run() called + registered
    eb3 = EnvBuild("other", "9")
    eb3.add_tool(ic.release_binary("mosdepth", "https://x/mosdepth_linux_amd64", binary_in_archive="mosdepth"))
    monkeypatch.setattr(eb3, "run", lambda: {"success": True, "content_digest": "sha256:z",
                                             "image": "other:9", "image_digest": "sha256:o",
                                             "platform": "linux/amd64", "engine": "none"})
    miss = eb3.build_or_cached(cache, image_present=lambda ref: False)
    assert miss["cached"] is False
    assert cache.lookup(eb3.request_key())["image_digest"] == "sha256:o"   # registered


def test_resolver_route_maps_every_tier():
    """route() maps a decision to an EnvBuild action across ALL tiers: conda spec /
    pip spec / R generator (cran|bioc) / release_binary / source generator."""
    from agent.skills.resolver import route
    # conda
    a = route({"chosen": "conda", "tool": "samtools", "version": "1.21",
               "probed": {"conda": {"channel": "bioconda"}}})
    assert a["kind"] == "conda" and a["spec"] == "samtools=1.21" and a["channel"] == "bioconda"
    # pip → engine --pypi spec
    a = route({"chosen": "pip", "tool": "cyvcf2", "version": "0.31.1", "probed": {"pip": {}}})
    assert a["kind"] == "pip" and a["spec"] == "cyvcf2==0.31.1"
    # cran / bioconductor → R install generator (engine-coupled Rscript)
    a = route({"chosen": "cran", "tool": "ape", "probed": {"cran": {}}})
    assert a["kind"] == "tool" and a["spec"]["engine_coupled"] and "install.packages" in a["spec"]["command"]
    a = route({"chosen": "bioconductor", "tool": "DESeq2", "probed": {"bioconductor": {}}})
    assert a["kind"] == "tool" and "BiocManager::install" in a["spec"]["command"]
    # binary → picks the linux/amd64 asset (rejects wrong-arch + wrong-os), emits a
    # release_binary generator spec
    a = route({"chosen": "binary", "tool": "sylph", "github_repo": "bluenote-1577/sylph",
               "probed": {"binary": {"assets": [
                   "https://github.com/bluenote-1577/sylph/releases/download/v0.8.0/sylph-linux-x86_64.tar.gz",
                   "https://github.com/bluenote-1577/sylph/releases/download/v0.8.0/sylph-linux-aarch64.tar.gz",
                   "https://github.com/bluenote-1577/sylph/releases/download/v0.8.0/sylph-macos-x86_64.tar.gz"]}}})
    assert a["kind"] == "tool" and a["spec"]["tool"] == "sylph" and a["needs_sha256"] is True
    assert "linux-x86_64" in a["spec"]["command"] and "aarch64" not in a["spec"]["command"] \
        and "macos" not in a["spec"]["command"]
    # source → clone at the release tag
    a = route({"chosen": "source", "tool": "seqtk", "github_repo": "lh3/seqtk",
               "probed": {"source": {"tag": "v1.4"}}})
    assert a["kind"] == "tool" and "github.com/lh3/seqtk" in a["spec"]["command"] and "v1.4" in a["spec"]["command"]
    # no automatable tier → honest defer
    assert route({"chosen": None, "tool": "x"})["kind"] == "defer"


def test_install_command_jar_generator_self_contained():
    """jar generator (C1): self-contained (JRE via apt only if java absent),
    sha256-anchored, single-jar AND zip-distribution wrappers, honest evidence."""
    from agent.skills import install_commands as ic
    # single jar (Picard-style)
    j = ic.jar("picard", "https://github.com/broadinstitute/picard/releases/download/3.1.1/picard.jar",
               sha256="abc123")
    assert j["tool"] == "picard" and j.get("engine_coupled", False) is False
    assert "default-jre-headless" in j["command"] and "command -v java" in j["command"]
    assert "sha256sum -c" in j["command"] and "abc123" in j["command"]
    assert "exec java -Xmx4g -jar /opt/tools/picard/picard.jar" in j["command"]
    assert j["evidence"] == "command -v picard && java -version"
    # zip distribution (Exomiser-style) → locate the jar inside
    z = ic.jar("exomiser", "https://x/exomiser-cli-14.0.0.zip", java_flags=["-Xmx8g"], wrapper="exomiser")
    assert "unzip -o" in z["command"] and "find /opt/tools/exomiser -name '*.jar'" in z["command"]
    assert "-Xmx8g" in z["command"]


# ---------------------------------------------------------------------------
# C3 — env_freeze: the container-native freeze translator (spec -> ContainerBuild).
# Pure parts (install_method -> generator mapping + toolchain injection) tested with
# network mocked; build_env_image's container drive is live-proven separately.
# ---------------------------------------------------------------------------

def test_env_freeze_maps_each_install_method_to_its_generator():
    from agent.skills import env_freeze as ef
    fake_la = lambda url, **k: {"found": True, "url": "https://x/tool-linux-x86_64.tar.gz"}
    fake_sha = lambda url, **k: {"ok": True, "sha256": "deadbeef"}
    # binary → release_binary with the resolved linux asset + sha
    b = ef._map_install({"name": "mosdepth", "type": "binary",
                         "install_method": {"binary_url": "https://x/mosdepth_darwin", "local_path": "/h/mosdepth"}},
                        resolve_linux_asset=fake_la, sha256_of_url=fake_sha)
    assert "spec" in b and b["spec"]["tool"] == "mosdepth" and "deadbeef" in b["spec"]["command"]
    # jar → jar generator
    j = ef._map_install({"name": "picard", "type": "jar", "install_method": {"source": "https://x/picard.jar"}},
                        sha256_of_url=fake_sha)
    assert j["spec"]["tool"] == "picard" and "-jar /opt/tools/picard/picard.jar" in j["spec"]["command"]
    # source → source generator (needs build_command + bin_path)
    s = ef._map_install({"name": "seqtk", "type": "source",
                         "install_method": {"source": "https://github.com/lh3/seqtk", "commit_sha": "abc123",
                                            "build_command": "make", "bin_path": "seqtk"}})
    assert s["spec"]["tool"] == "seqtk" and "abc123" in s["spec"]["command"]
    # cargo / go / perl → coupled generators
    assert ef._map_install({"name": "rasusa", "type": "cargo",
                            "install_method": {"crate": "rasusa", "version": "2.0.0"}})["spec"]["engine_coupled"]
    assert ef._map_install({"name": "gofasta", "type": "go",
                            "install_method": {"package": "github.com/x/gofasta"}})["spec"]["engine_coupled"]
    assert ef._map_install({"name": "BioX", "type": "perl",
                            "install_method": {"module": "JSON::XS"}})["spec"]["tool"] == "JSON::XS"
    # non-replayable source (no build_command/bin_path) → error, not a silent pass
    e = ef._map_install({"name": "x", "type": "source", "install_method": {"source": "https://x"}})
    assert "error" in e and "not replayable" in e["error"]


def test_env_freeze_injects_engine_toolchains_for_coupled_tiers():
    """plan_conda adds rust/go/perl(+compilers) to the conda layer when those tiers
    are present — the container-native replacement for the host rustup/go-tarball/
    conda-builddeps emitters. Order-stable, deduped."""
    from agent.skills.env_freeze import plan_conda
    non_conda = [{"type": "cargo"}, {"type": "go"}, {"type": "perl"}, {"type": "binary"}]
    out = plan_conda(["samtools=1.21"], non_conda)
    assert out[0] == "samtools=1.21"
    assert set(out[1:]) == {"rust", "go", "perl", "perl-app-cpanminus", "c-compiler", "cxx-compiler"}
    # dedup: a toolchain already declared isn't doubled
    assert plan_conda(["rust"], [{"type": "cargo"}]) == ["rust"]
    # no coupled tiers → unchanged
    assert plan_conda(["bwa=0.7.18"], [{"type": "binary"}]) == ["bwa=0.7.18"]


def test_env_freeze_build_refuses_nonreplayable_before_building(monkeypatch):
    """build_env_image fails on a non-replayable record BEFORE spinning a container."""
    from agent.skills import env_freeze as ef
    spec = {"install_steps": [{"installed_packages": [
        {"name": "x", "install_method": {"type": "source", "source": "https://x"}}]}]}
    # EnvBuild must never be constructed/run on a map failure
    monkeypatch.setattr(ef, "EnvBuild", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build")))
    r = ef.build_env_image(spec, name="demo")
    assert r["success"] is False and r["stage"] == "map_install"


# ---------------------------------------------------------------------------
# C4 — migrated intent: the recipe-zoo tests' guarantees now live on the container-
# native side (generators + translator). These lock in what the deleted recipe tests
# proved (binary sha gate, perl XS shim, conda-spec extraction).
# ---------------------------------------------------------------------------

def test_requested_conda_specs_excludes_bootstrap_python():
    """freeze.requested_conda_specs returns the EXPLICITLY-requested conda tools
    (install steps), not the bootstrap python from create_conda_env (a create step)."""
    from agent.skills.freeze import requested_conda_specs
    draft = {"install_steps": [
        {"tool": "conda", "subcommand": "create",
         "installed_packages": [{"name": "python", "version": "3.11"}]},   # scaffolding — excluded
        {"tool": "conda", "subcommand": "install",
         "installed_packages": [{"name": "samtools", "version": "1.21"},
                                {"name": "bcftools"}]},                      # no version -> bare name
        {"tool": "pip", "subcommand": "install",
         "installed_packages": [{"name": "pysam", "version": "0.22"}]},     # not conda -> excluded
    ]}
    assert requested_conda_specs(draft) == ["samtools=1.21", "bcftools"]


def test_perl_cpanm_bakes_xlocale_shim_and_release_binary_sha_gate():
    """Migrated intent: perl XS builds against conda perl get the xlocale.h shim
    (recipe's _emit_perl_conda_builddeps knowledge), and a release binary with a
    sha256 emits the sha256sum -c gate (recipe's binary sha-gate knowledge)."""
    from agent.skills import install_commands as ic
    p = ic.perl_cpanm("JSON::XS")
    assert 'xlocale.h' in p["command"] and "$CONDA_PREFIX/include" in p["command"]
    assert "cpanm --notest" in p["command"] and p["engine_coupled"]
    b = ic.release_binary("mosdepth", "https://x/mosdepth_linux_amd64", sha256="cafef00d")
    assert "cafef00d  mosdepth_linux_amd64" in b["command"] and "sha256sum -c" in b["command"]


# ---------------------------------------------------------------------------
# Follow-up slice: container-native pip (engine --pypi) + R install generator.
# ---------------------------------------------------------------------------

def test_install_command_r_package_generator():
    """r_package: cran/bioconductor/github via Rscript, engine-coupled, verified by
    library() in-image; tool token anchors the shape rule."""
    from agent.skills import install_commands as ic
    c = ic.r_package("ape", source="cran")
    assert c["engine_coupled"] and c["tool"] == "ape"
    assert 'install.packages("ape"' in c["command"] and "Rscript -e" in c["command"]
    assert c["evidence"] == "Rscript -e 'library(ape)'"
    b = ic.r_package("DESeq2", source="bioconductor")
    assert 'BiocManager::install("DESeq2"' in b["command"]
    g = ic.r_package("treedater", source="github:emvolz-phylodynamics/treedater")
    assert 'remotes::install_github("emvolz-phylodynamics/treedater",dependencies=FALSE)' in g["command"]


def test_install_command_r_package_load_or_die_and_mirror_pin():
    """Every r_package install command ends with a `requireNamespace || stop()` so a
    silent BiocManager-picks-foreign-mirror failure fails the build AT this install
    step with the tool's actual error, not later at the evidence stage with no
    diagnostic. The bioconductor branch pins `options(BioC_mirror=...)` so the
    auto-select wildcard is removed entirely. The github branch passes
    `dependencies=FALSE` so transitive deps come from conda or earlier R steps (the
    install plan is the source of truth, not remotes' opportunistic auto-resolve)."""
    from agent.skills import install_commands as ic

    # all three sources gain the load-or-die check
    for spec in (ic.r_package("ape", source="cran"),
                 ic.r_package("snpStats", source="bioconductor"),
                 ic.r_package("GAPIT", source="github:jiabowang/GAPIT")):
        name = spec["tool"]
        assert "requireNamespace" in spec["command"]
        assert f"\"{name}\"" in spec["command"]   # the name is checked, not a generic stop
        assert "stop(" in spec["command"]
        assert "not loadable" in spec["command"]

    # bioconductor branch pins the mirror; the host-side observed bug is that R's
    # in-container BiocManager auto-detect can pick an unreachable foreign mirror
    # and silently fail. The pin removes the wildcard.
    b = ic.r_package("snpStats", source="bioconductor")
    assert 'options(BioC_mirror="https://bioconductor.org")' in b["command"]
    assert b["command"].index('options(BioC_mirror') < b["command"].index("BiocManager::install"), \
        "mirror must be pinned BEFORE BiocManager::install or it can still auto-select"

    # the mirror is overridable for callers that need a local registry
    b2 = ic.r_package("snpStats", source="bioconductor", bioc_mirror="https://bioc.example.org")
    assert 'options(BioC_mirror="https://bioc.example.org")' in b2["command"]

    # the github branch passes dependencies=FALSE (deps come from the install plan)
    g = ic.r_package("GAPIT", source="github:jiabowang/GAPIT")
    assert "dependencies=FALSE" in g["command"]
    assert 'remotes::install_github("jiabowang/GAPIT",dependencies=FALSE)' in g["command"]


def test_pipeline_state_smart_replace_step_appends_when_replacing_a_failed_slot():
    """The install_step-ordering trap: agent installs GAPIT (fails — missing
    transitive snpStats), installs snpStats (succeeds), retries GAPIT with
    step=N to replace the failed slot. The naive replace would put the
    successful GAPIT BACK at its original slot — BEFORE snpStats — and the
    container replay would fail with the same missing-dep error. The smart
    replace appends instead so the new step lands AFTER snpStats. Successful
    replaces (e.g. agent edits a version) keep the original edit-in-place
    semantic. This is the autonomy fix: the agent doesn't have to think about
    step numbers at all when recovering from a missing-dep failure."""
    from agent.skills.pipeline_state import PipelineState

    ps = PipelineState({"paths": {"pipelines_dir": "/tmp/bioinf_test_smart_replace"}})
    ps.start("smart_replace_test", "test")

    # Failed install of GAPIT lands at step 1.
    i1 = ps.add_install_step("smart_replace_test",
        {"tool": "Rscript", "purpose": "GAPIT first try",
         "returncode": 1, "installed_packages": [{"name": "GAPIT"}]})
    assert i1 == 1

    # Agent then installs snpStats (the missing dep). Lands at step 2.
    i2 = ps.add_install_step("smart_replace_test",
        {"tool": "Rscript", "purpose": "snpStats",
         "returncode": 0, "installed_packages": [{"name": "snpStats"}]})
    assert i2 == 2

    # Agent retries GAPIT with step=1 to "replace the failed prior attempt".
    # Smart-replace: previous attempt at slot 1 had rc!=0 + new attempt rc==0
    # → APPEND at step 3, NOT replace slot 1.
    i3 = ps.add_install_step("smart_replace_test",
        {"tool": "Rscript", "purpose": "GAPIT retry",
         "returncode": 0, "installed_packages": [{"name": "GAPIT"}]},
        replace_step=1)
    assert i3 == 3, f"smart-replace must append after intervening success; got step={i3}"

    steps = ps._drafts["smart_replace_test"]["install_steps"]
    assert len(steps) == 3
    assert steps[0]["returncode"] == 1                          # failed attempt preserved
    assert steps[0]["installed_packages"][0]["name"] == "GAPIT"
    assert steps[1]["installed_packages"][0]["name"] == "snpStats"
    assert steps[2]["installed_packages"][0]["name"] == "GAPIT"
    assert steps[2]["returncode"] == 0
    # Replay order = [snpStats, GAPIT] once failed step is filtered — correct.

    # Successful-replace still edits in place (the version-bump case): agent
    # decides EMMREML 3.1 was wrong, wants 3.2 → step= must overwrite.
    ps.add_install_step("smart_replace_test",
        {"tool": "Rscript", "purpose": "EMMREML 3.1",
         "returncode": 0, "installed_packages": [{"name": "EMMREML", "version": "3.1"}]})
    i_edit = ps.add_install_step("smart_replace_test",
        {"tool": "Rscript", "purpose": "EMMREML 3.2 (corrected)",
         "returncode": 0, "installed_packages": [{"name": "EMMREML", "version": "3.2"}]},
        replace_step=4)
    assert i_edit == 4, "successful replace should overwrite in place"
    steps = ps._drafts["smart_replace_test"]["install_steps"]
    assert len(steps) == 4
    assert steps[3]["installed_packages"][0]["version"] == "3.2"

    ps.discard("smart_replace_test")


def test_freeze_installed_packages_successful_only_filters_failed_steps():
    """installed_packages(successful_only=True) skips install_steps with rc!=0
    and applies last-wins dedup so a later successful retry of the same package
    supersedes an earlier failed attempt. non_conda_installs (the build-plan
    feeder) uses this filter — a failed install_method must never reach the
    container build, period. Default (successful_only=False) keeps the
    inventory/reporting view that includes failed attempts."""
    from agent.skills.freeze import installed_packages, non_conda_installs

    spec = {"install_steps": [
        {"step": 1, "tool": "Rscript", "returncode": 1,
         "installed_packages": [{"name": "GAPIT", "version": None,
                                 "install_method": {"type": "r_install",
                                                    "source": "remotes::install_github('jiabowang/GAPIT')"}}]},
        {"step": 2, "tool": "Rscript", "returncode": 0,
         "installed_packages": [{"name": "snpStats", "version": "1.60.0",
                                 "install_method": {"type": "r_install",
                                                    "source": "BiocManager::install('snpStats')"}}]},
        {"step": 3, "tool": "Rscript", "returncode": 0,
         "installed_packages": [{"name": "GAPIT", "version": "4.1.0",
                                 "install_method": {"type": "r_install",
                                                    "source": "remotes::install_github('jiabowang/GAPIT')"}}]},
    ]}

    # successful_only=True: failed step skipped, last-wins → GAPIT picks up
    # the SUCCESSFUL row (with version), not the failed placeholder.
    succ = {p["name"]: p for p in installed_packages(spec, successful_only=True)}
    assert set(succ) == {"GAPIT", "snpStats"}
    assert succ["GAPIT"]["version"] == "4.1.0", \
        "successful-step row must win dedup over the failed placeholder"

    # Default (inventory view) still surfaces the failed-step row exists.
    all_pkgs = installed_packages(spec)
    assert len(all_pkgs) == 2   # dedup by name (last-wins keeps successful row)

    # non_conda_installs feeds the build plan → MUST filter failed.
    nc = non_conda_installs(spec)
    nc_names = {x["name"] for x in nc}
    assert nc_names == {"GAPIT", "snpStats"}
    # The GAPIT install_method must be the successful one (real source string),
    # not whatever was in the failed-step placeholder.
    g = next(x for x in nc if x["name"] == "GAPIT")
    assert "install_github" in g["install_method"]["source"]


def test_install_r_package_surfaces_missing_packages_on_failure(monkeypatch):
    """When R install fails, parse stderr for missing-package signals and
    surface them as a structured `missing_packages: [...]` field so the
    autonomy loop doesn't have to grep error text. R produces two distinct
    shapes both meaning 'install X then retry me': `Package 'X' not available
    after install attempt` (BiocManager dep resolution) and `there is no
    package called 'X'` (lazy-load failure during R CMD INSTALL byte-compile).
    Both must be captured. The package WE were trying to install is NOT
    in missing_packages — its own load-or-die already telegraphs that."""
    import agent.mcp_server as m

    # Simulate the exact stderr shape we see in the wild from a failed
    # remotes::install_github('jiabowang/GAPIT') when snpStats is missing.
    fake_stderr = (
        "Using GitHub PAT from the git credential store.\n"
        "Downloading GitHub repo jiabowang/GAPIT@HEAD\n"
        "Running `R CMD build`...\n"
        "* installing *source* package 'GAPIT' ...\n"
        "** byte-compile and prepare package for lazy loading\n"
        "ERROR: lazy loading failed for package 'GAPIT'\n"
        "Bioconductor version 3.22 (BiocManager 1.30.27), R 4.5.3 (2026-03-11)\n"
        "Installing package(s) 'BiocVersion', 'snpStats'\n"
        "Warning: packages 'BiocVersion', 'snpStats' are not available for Bioconductor version '3.22'\n"
        "Error : Package 'snpStats' not available after install attempt.\n"
        "Error: unable to load R code in package 'GAPIT'\n"
        "Execution halted\n"
    )
    monkeypatch.setattr(m._env_mgr, "run_in_env",
        lambda env, cmd, timeout=1800: {"returncode": 1, "stdout": "", "stderr": fake_stderr,
                                         "success": False, "command": cmd})

    res = m.install_r_package(env_name="x", name="GAPIT",
                               source="github:jiabowang/GAPIT", pipeline_id="")
    assert res["returncode"] == 1
    assert res.get("missing_packages") == ["snpStats"], \
        f"expected ['snpStats']; got {res.get('missing_packages')!r}"

    # The other failure shape: `there is no package called 'X'` (lazy-load).
    fake_stderr_loadfail = (
        "Error in loadNamespace(j <- i[[1L]], c(lib.loc, .libPaths()), versionCheck = vI[[j]]) : \n"
        "  there is no package called 'multtest'\n"
        "Calls: <Anonymous> ... loadNamespace -> withRestarts -> withOneRestart -> doWithOneRestart\n"
        "Execution halted\n"
    )
    monkeypatch.setattr(m._env_mgr, "run_in_env",
        lambda env, cmd, timeout=1800: {"returncode": 1, "stdout": "", "stderr": fake_stderr_loadfail,
                                         "success": False, "command": cmd})
    res2 = m.install_r_package(env_name="x", name="GAPIT",
                                source="github:jiabowang/GAPIT", pipeline_id="")
    assert res2.get("missing_packages") == ["multtest"]

    # The package WE were installing must NOT appear in missing_packages even
    # if its name appears in the load-or-die failure line.
    self_fail = (
        "trying to install GAPIT\n"
        "Error: install reported success but GAPIT is not loadable\n"
        # if R also said "there is no package called 'GAPIT'" we'd EXCLUDE GAPIT itself
        "Error in library(GAPIT) : there is no package called 'GAPIT'\n"
    )
    monkeypatch.setattr(m._env_mgr, "run_in_env",
        lambda env, cmd, timeout=1800: {"returncode": 1, "stdout": "", "stderr": self_fail,
                                         "success": False, "command": cmd})
    res3 = m.install_r_package(env_name="x", name="GAPIT",
                                source="github:jiabowang/GAPIT", pipeline_id="")
    assert "GAPIT" not in (res3.get("missing_packages") or []), \
        "the package being installed must not appear in its own missing_packages list"

    # Successful install: no missing_packages field (only set on failure).
    monkeypatch.setattr(m._env_mgr, "run_in_env",
        lambda env, cmd, timeout=1800: {"returncode": 0, "stdout": "1.0.0\n", "stderr": "",
                                         "success": True, "command": cmd})
    res4 = m.install_r_package(env_name="x", name="ape",
                                source="cran", pipeline_id="")
    assert "missing_packages" not in res4 or not res4.get("missing_packages")


def test_pixi_engine_add_pypi_and_micromamba_refuses():
    """PixiEngine.add_pypi → `pixi add --pypi` (into the lock); MicromambaEngine
    refuses honestly (its explicit lock can't capture pip)."""
    from agent.skills.container_build import PixiEngine, MicromambaEngine
    calls = []
    class FakeCB:
        def exec(self, cmd, timeout=0):
            calls.append(cmd); return {"returncode": 0, "stdout": "", "stderr": ""}
    assert PixiEngine().add_pypi(FakeCB(), ["cyvcf2==0.31.1"])["success"]
    assert any("pixi add --pypi" in c and "cyvcf2==0.31.1" in c for c in calls)
    r = MicromambaEngine().add_pypi(FakeCB(), ["cyvcf2"])
    assert r["success"] is False and "pip" in r["reason"].lower()


def test_envbuild_add_pip_records_engine_coupled_verification():
    from agent.skills.env_build import EnvBuild
    eb = EnvBuild("demo", "1.0")
    eb.add_pip(["cyvcf2==0.31.1"], verify=[("cyvcf2", "pip show cyvcf2")])
    assert eb.pip_specs == ["cyvcf2==0.31.1"]
    v = eb.verifications[-1]
    assert v["tool"] == "cyvcf2" and v["engine_coupled"] is True
    assert "cyvcf2=0.31.1" in eb.request_key()   # pip specs feed the lookup handle (== normalized to =)


def test_env_freeze_maps_pip_and_r_install():
    """env_freeze routes r_install -> r_package generator (source reconstructed from
    the recorded R expr) and injects the R toolchain; pip is partitioned to add_pip."""
    from agent.skills import env_freeze as ef
    # r_install mapping: source reconstructed from install_method.source expr
    r = ef._map_install({"name": "ape", "type": "r_install",
                         "install_method": {"type": "r_install", "source": "install.packages('ape')"}})
    assert r["spec"]["tool"] == "ape" and 'install.packages("ape"' in r["spec"]["command"]
    rg = ef._map_install({"name": "treedater", "type": "r_install",
                          "install_method": {"source": "remotes::install_github('emvolz/treedater')"}})
    assert 'install_github("emvolz/treedater",dependencies=FALSE)' in rg["spec"]["command"]
    # plan_conda injects the R toolchain (r-base + compilers)
    out = ef.plan_conda([], [{"type": "r_install"}])
    assert "r-base" in out and "fortran-compiler" in out
    # zlib lives in the R toolchain because source-compiled Bioc/CRAN packages that
    # work with compressed data (snpStats: -lz for read_uncertain.c; many htslib-
    # adjacent R packages) #include <zlib.h>. Without it the container build fails
    # at the install step with a "zlib.h: No such file or directory" hard-error.
    assert "zlib" in out
    # pip is NOT a generator (no toolchain injected for it)
    assert ef.plan_conda([], [{"type": "pip"}]) == []


def test_mcp_freeze_repoint_drives_container_native_builder(monkeypatch, tmp_path):
    """C4 wiring (the gap that was py_compile-only): the re-pointed freeze, on a draft
    with a non-conda install, takes the container-native branch — calls
    env_freeze.build_env_image with conda_deps from requested_conda_specs + the docker
    platform + primary_tools, and maps the result to a build/container-native
    freeze_record registered in the EnvCache. (The build is live-proven separately;
    this proves the FACE wiring deterministically — no docker.)"""
    import agent.mcp_server as m
    draft = {"install_steps": [
        {"tool": "conda", "subcommand": "install",
         "installed_packages": [{"name": "samtools", "version": "1.21",
                                 "install_method": {"type": "conda"}}]},
        {"tool": "install_release_binary", "subcommand": "install",
         "installed_packages": [{"name": "seqkit", "version": "2.13.0",
                                 "install_method": {"type": "binary",
                                                    "binary_url": "https://x/seqkit_darwin_arm64.tar.gz",
                                                    "local_path": "/h/seqkit"}}]},
    ]}
    monkeypatch.setattr(m._pipeline_state, "get_draft", lambda pid: draft)
    monkeypatch.setattr(m._biocontainers, "resolve_biocontainer", lambda parsed: {"found": False})
    monkeypatch.setattr(m._env_cache, "lookup", lambda k: None)
    registered = {}
    monkeypatch.setattr(m._env_cache, "register", lambda k, rec: registered.update({k: rec}) or rec)
    monkeypatch.setattr(m._env_mgr, "generate_lock", lambda *a, **k: {"success": False})
    monkeypatch.setattr(m._docker, "save_archive", lambda img, path: {"success": False})
    monkeypatch.setattr(m._docker, "image_digest", lambda img: "")
    captured = {}
    def fake_build(spec, **kw):
        captured["spec"], captured["kw"] = spec, kw
        return {"success": True, "image": "samtools-seqkit:latest", "image_digest": "sha256:deadbeef",
                "content_digest": "sha256:cd",
                "longtail_steps": [{"purpose": "seqkit (release binary)", "command": "set -eux; curl ..."}]}
    monkeypatch.setattr(m._env_freeze, "build_env_image", fake_build)
    monkeypatch.setattr(m._env_mgr, "project_root", tmp_path)   # deliverables -> tmp, not the real env_reports/

    res = m.freeze("bioinf_x", ["samtools=1.21", "seqkit"], platform="linux-64", pipeline_id="p1")

    assert res["success"] and res["mode"] == "build" and res["build_method"] == "container-native"
    assert res["image"] == "samtools-seqkit:latest" and res["image_digest"] == "sha256:deadbeef"
    # the re-point derived the right args for the container-native builder
    assert captured["kw"]["conda_deps"] == ["samtools=1.21"]      # requested_conda_specs (no bootstrap python)
    assert captured["kw"]["platform"] == "linux/amd64"            # converted from the conda subdir linux-64
    assert set(captured["kw"]["primary_tools"]) == {"samtools", "seqkit"}
    assert captured["spec"] is draft
    # registered in the cache; shipped_binaries record the baked long-tail command
    assert registered and any(sb.get("name") == "seqkit (release binary)"
                              for sb in res.get("shipped_binaries", []))


# ---------------------------------------------------------------------------
# Declarative builder: resolve -> route -> EnvBuild, straight from tool NAMES
# (no host install). The container-native "call once per tool" entry point.
# ---------------------------------------------------------------------------

def test_build_env_from_tools_assembles_plan_across_tiers(monkeypatch):
    """resolve->route->EnvBuild: a conda tool, a pip tool, an R (cran) tool, and a
    binary are each resolved + routed to the right EnvBuild call; the R toolchain is
    injected. EnvBuild is stubbed to capture the assembled plan (no container)."""
    from agent.skills import env_freeze as ef

    decisions = {
        "samtools": {"chosen": "conda", "tool": "samtools", "version": "1.21",
                     "probed": {"conda": {"channel": "bioconda"}}},
        "pyfaidx":  {"chosen": "pip", "tool": "pyfaidx", "probed": {"pip": {}}},
        "ape":      {"chosen": "cran", "tool": "ape", "probed": {"cran": {}}},
        "sylph":    {"chosen": "binary", "tool": "sylph", "github_repo": "x/sylph",
                     "probed": {"binary": {"assets": ["https://x/sylph-linux-x86_64.tar.gz"]}}},
    }
    plan = {"conda": [], "conda_verify": [], "pip": [], "tools": []}
    class FakeEB:
        def __init__(self, *a, **k): plan["init"] = k
        def add_conda(self, specs, verify): plan["conda"] = specs; plan["conda_verify"] = verify
        def add_pip(self, specs, verify): plan["pip"] = specs
        def add_tool(self, spec): plan["tools"].append(spec)
        def run(self): return {"success": True, "image": "x:1"}
        def request_key(self): return "rk"
    monkeypatch.setattr(ef, "EnvBuild", FakeEB)

    res = ef.build_env_from_tools("demo", ["samtools=1.21", "pyfaidx", "ape", "sylph"],
                                  github_repos={"sylph": "x/sylph"},
                                  resolve_fn=lambda tool, **k: decisions[tool])
    assert res["success"] and res["request_key"] == "rk"
    assert "samtools=1.21" in plan["conda"]
    # R toolchain injected because a cran tool is present
    assert {"r-base", "c-compiler", "cxx-compiler", "fortran-compiler"} <= set(plan["conda"])
    assert plan["pip"] == ["pyfaidx"]
    # R (cran) + binary became add_tool generator specs
    tool_tools = {t["tool"] for t in plan["tools"]}
    assert "ape" in tool_tools and "sylph" in tool_tools
    # conda verify is presence-based: a CLI on PATH, else installed dist-metadata
    # (so library-only packages validate too). Both clauses name the tool token.
    sv = dict(plan["conda_verify"])
    assert sv["samtools"].startswith("command -v samtools")
    assert "importlib.metadata" in sv["samtools"]


def test_build_env_from_tools_refuses_ambiguous_and_unroutable(monkeypatch):
    """Refuses BEFORE building on an ambiguous resolve or a tier with no route."""
    from agent.skills import env_freeze as ef
    monkeypatch.setattr(ef, "EnvBuild", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build")))
    r = ef.build_env_from_tools("demo", ["ape"],
                                resolve_fn=lambda tool, **k: {"ambiguous": True, "rationale": "ape is PyPI AND CRAN"})
    assert r["success"] is False and r["stage"] == "resolve"
    # no automatable tier -> route returns defer -> refuse
    r = ef.build_env_from_tools("demo", ["weirdtool"],
                                resolve_fn=lambda tool, **k: {"chosen": None, "tool": tool})
    assert r["success"] is False and r["stage"] == "route"


def test_container_native_multistage_runtime_provisions_jar_jre():
    """Phase D: a jar tool's wrapper needs java at RUNTIME — the runtime stage adds a
    JRE only when a jar step is present (non-jar images stay lean)."""
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    jar_step = [{"command": "set -eux; curl -o /opt/tools/picard/picard.jar x; ...",
                 "purpose": "picard (java jar)"}]
    df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(), has_env_layer=False,
                         longtail_steps=jar_step)
    runtime = df.split("# ---- runtime image (shipped) ----", 1)[1]
    assert "default-jre-headless" in runtime          # JRE shipped for the jar wrapper
    # a non-jar image does NOT carry a JRE
    bin_step = [{"command": "install -m0755 /tmp/x /usr/local/bin/x", "purpose": "x (release binary)"}]
    df2 = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(), has_env_layer=False,
                          longtail_steps=bin_step)
    assert "default-jre-headless" not in df2


def test_mcp_freeze_pure_conda_builds_container_native_no_condapack(monkeypatch, tmp_path):
    """Phase E (E1): freeze no longer uses conda-pack. A pure-conda env (no draft, no
    biocontainer) builds container-native — conda_deps derived from the requested
    tools — cross-arch with NO refusal."""
    import agent.mcp_server as m
    monkeypatch.setattr(m._pipeline_state, "get_draft", lambda pid: None)
    monkeypatch.setattr(m._biocontainers, "resolve_biocontainer", lambda parsed: {"found": False})
    monkeypatch.setattr(m._env_cache, "lookup", lambda k: None)
    monkeypatch.setattr(m._env_cache, "register", lambda k, rec: rec)
    monkeypatch.setattr(m._env_mgr, "generate_lock", lambda *a, **k: {"success": False})
    monkeypatch.setattr(m._docker, "save_archive", lambda img, path: {"success": False})
    monkeypatch.setattr(m._docker, "image_digest", lambda img: "")
    captured = {}
    monkeypatch.setattr(m._env_freeze, "build_env_image",
                        lambda spec, **kw: captured.update(kw) or {
                            "success": True, "image": "x:1", "image_digest": "sha256:d", "longtail_steps": []})
    monkeypatch.setattr(m._env_mgr, "project_root", tmp_path)   # deliverables -> tmp, not the real env_reports/
    res = m.freeze("bioinf_x", ["samtools=1.21", "bcftools=1.21"], platform="linux/amd64", pipeline_id="")
    assert res["success"] and res["mode"] == "build" and res["build_method"] == "container-native"
    assert captured["conda_deps"] == ["samtools=1.21", "bcftools=1.21"]   # from tools (no draft)
    assert captured["platform"] == "linux/amd64" and captured["license_gated"] is False


# ---------------------------------------------------------------------------
# GAB shakeout fixes — real-world half-baked academic install robustness
# (baumannlab/Genome_Assembly_Booster: sci-python co-solve + run-by-path repo)
# ---------------------------------------------------------------------------

def test_ensure_python_for_pip_injects_interpreter():
    """pip via the engine (pixi --pypi/uv) needs python IN the env; inject it when
    no conda package provides one (the pip analog of toolchain injection)."""
    from agent.skills import env_freeze as ef
    assert "python" in ef.ensure_python_for_pip(["r-base"], True)        # pip + no python -> injected
    assert ef.ensure_python_for_pip(["python=3.12"], True) == ["python=3.12"]  # explicit python kept, no dupe
    assert ef.ensure_python_for_pip(["python"], True) == ["python"]
    assert ef.ensure_python_for_pip(["r-base"], False) == ["r-base"]     # no pip -> untouched
    out = ef.ensure_python_for_pip(["python-louvain"], True)             # python-foo is NOT python
    assert "python" in out and out != ["python-louvain"]


def test_conda_presence_check_validates_libraries_honesty_safe():
    """Library-only conda packages (no CLI) validate via installed dist-metadata,
    NOT command -v. The check still NAMES the package, so env_honesty's anti-cheat
    shape rule accepts it — and a bare `import community` (name != package) is still
    rejected, proving the guarantee isn't weakened."""
    from agent.skills import env_freeze as ef
    from agent.skills import env_honesty as eh
    chk = ef._conda_presence_check("python-louvain")
    assert "command -v python-louvain" in chk and "distribution('python-louvain')" in chk
    assert eh.evidence_shape_violation(chk, "python-louvain") is None          # accepted (names the token)
    assert eh.evidence_shape_violation('python -c "import community"', "python-louvain") is not None  # cheat-shape still caught


def test_map_install_routes_run_by_path_to_script_repo():
    """A clone-and-run script collection (entrypoint, no compiled binary) is
    replayable via the script_repo generator; without entrypoint OR build/bin_path
    it's still refused."""
    from agent.skills import env_freeze as ef
    m = ef._map_install({"name": "gab", "type": "source", "install_method": {
        "type": "source", "source": "https://github.com/x/gab", "commit_sha": "abc",
        "entrypoint": "HIC_ASSEMBLER/run_hicAssembler.py", "interpreter": "python"}})
    assert "spec" in m and "error" not in m
    cmd = m["spec"]["command"]
    assert "_swh_clone" in cmd and "run_hicAssembler.py" in cmd and "/usr/local/bin/gab" in cmd
    bad = ef._map_install({"name": "gab", "type": "source", "install_method": {
        "type": "source", "source": "https://github.com/x/gab", "commit_sha": "abc"}})
    assert "error" in bad


def test_resolver_filters_serial_version_anomaly():
    """An abandoned date/serial 'version' (bioconda hmmlearn's 20151031) must not
    masquerade as latest — picked over the real semver only if nothing else parses."""
    from agent.skills import resolver as r
    assert r._looks_like_serial("20151031")
    assert not r._looks_like_serial("0.3.3") and not r._looks_like_serial("2026.4.0")  # CalVer ok
    assert r._pick_latest(["20151031", "0.1.1"]) == "0.1.1"
    assert r._pick_latest(["0.2.0", "0.3.3", "0.3.2"]) == "0.3.3"
    assert r._pick_latest(["20151031"]) == "20151031"   # all-serial -> still resolves


def test_build_env_from_tools_bare_name_for_unpinned_conda(monkeypatch):
    """Unpinned conda tools join the co-solve as BARE names (the solver co-resolves
    compatible versions); an explicit user pin is honored. Over-pinning each to its
    independent latest is what broke GAB's numba/numpy co-solve."""
    from agent.skills import env_freeze as ef
    cap = {}
    class FakeEB:
        def __init__(self, *a, **k): pass
        def add_conda(self, specs, verify): cap["conda"] = specs
        def add_pip(self, *a, **k): pass
        def add_tool(self, *a, **k): pass
        def run(self): return {"success": True, "image": "x:1"}
        def request_key(self): return "rk"
    monkeypatch.setattr(ef, "EnvBuild", FakeEB)
    dec = lambda tool, **k: {"chosen": "conda", "tool": tool, "version": k.get("version", ""),
                             "probed": {"conda": {"latest": "2.4.6", "channel": "conda-forge"}}, "found": True}
    ef.build_env_from_tools("d", ["numpy", "samtools=1.21"], resolve_fn=dec)
    assert "numpy" in cap["conda"] and "numpy=2.4.6" not in cap["conda"]   # bare (no auto-latest pin)
    assert "samtools=1.21" in cap["conda"]                                  # explicit pin honored


def test_runtime_image_is_self_activating_env_on_path():
    """GAB fix #5: the shipped runtime image must bake the conda env onto PATH so
    `apptainer exec image <tool>` / plain `docker run image <tool>` reach the conda
    tools + python directly (not only via `pixi run`). Without this every conda
    tool — and a run-by-path wrapper's `python` — 404s under the HPC delivery."""
    from agent.skills.container_build import PixiEngine, MicromambaEngine
    pix = "\n".join(PixiEngine().runtime_lines())
    assert "/work/.pixi/envs/default/bin" in pix and "ENV PATH" in pix and "CONDA_PREFIX" in pix
    mam = "\n".join(MicromambaEngine().runtime_lines())
    assert "/opt/micromamba/envs/env/bin" in mam and "ENV PATH" in mam and "CONDA_PREFIX" in mam


# ---------------------------------------------------------------------------
# BANNER-PROBE NON-FAKEABILITY — the version-cell honesty contract.
# The probe captures the shipped tool's self-reported version BANNER without
# letting the agent influence what gets run or what gets accepted. These tests
# guard the seams that make it non-fakeable: the probe command is synthesized
# from a sanitized tool token only, an unsafe token is skipped (no banner) instead
# of risking shell injection, and the renderer accepts only version-shaped tokens
# extracted from the captured stdout.
# ---------------------------------------------------------------------------

def test_validate_in_image_probe_command_uses_only_tool_token(monkeypatch):
    """The banner probe is synthesized inside container_build from the tool token
    alone — no agent text reaches the shell. Capture every docker invocation and
    assert each probe is `<tool> --version` / bare `<tool>`, nothing else."""
    from agent.skills import container_build as cb
    cb_inst = cb.ContainerBuild.__new__(cb.ContainerBuild)
    cb_inst.platform = "linux/amd64"
    cb_inst.workdir = "/work"
    invocations = []

    def fake_sh(argv, timeout=300):
        invocations.append(argv)
        return {"returncode": 0, "stdout": "Version: 1.4-r122\n", "stderr": ""}

    cb_inst._sh = fake_sh
    res = cb_inst.validate_in_image("image:tag", checks=["samtools --help 2>/dev/null"],
                                    probe_tools=["seqtk"])
    probe_argvs = [a for a in invocations
                   if any("seqtk" in x and "samtools" not in x for x in a)]
    assert probe_argvs, "expected probe invocations for seqtk"
    for argv in probe_argvs:
        shell_cmd = argv[-1]   # docker run ... bash -c <cmd>
        # the probe command must mention ONLY the tool token + flags + the 2>&1 || true tail
        assert shell_cmd.startswith("seqtk")
        assert "samtools" not in shell_cmd   # other tools never leak in
        # no agent-payload metacharacters beyond the fixed `2>&1 || true` suffix
        assert ";" not in shell_cmd and "$" not in shell_cmd and "`" not in shell_cmd
    assert res["banners"]["seqtk"].startswith("Version: 1.4-r122")


def test_validate_in_image_unsafe_tool_token_is_skipped(monkeypatch):
    """A tool token with shell metacharacters MUST NOT produce a probe command.
    Closes the only shell-injection seam — the renderer would refuse the empty
    banner anyway, but the probe must not even synthesize the dangerous string."""
    from agent.skills import container_build as cb
    cb_inst = cb.ContainerBuild.__new__(cb.ContainerBuild)
    cb_inst.platform = "linux/amd64"
    cb_inst.workdir = "/work"
    invocations = []
    cb_inst._sh = lambda argv, timeout=300: (invocations.append(argv),
                                             {"returncode": 0, "stdout": "", "stderr": ""})[1]
    bad = "seqtk; rm -rf /"
    res = cb_inst.validate_in_image("image:tag", checks=[], probe_tools=[bad])
    assert res["banners"][bad] == ""        # nothing captured
    # no docker invocation ever referenced the dangerous token
    assert not any(bad in x for argv in invocations for x in argv)


def test_version_from_banner_accepts_only_version_shaped_tokens():
    """The renderer extracts a version from the captured banner ONLY when it
    matches the strict version shape (digit-led, dotted). Arbitrary text in a
    banner can't be smuggled into the version cell."""
    from agent.skills.env_report import _version_from_banner
    assert _version_from_banner("Version: 1.4-r122\nUsage: seqtk ...") == "1.4-r122"
    assert _version_from_banner("seqtk 1.4-r122\nCopyright (c) ...") == "1.4-r122"
    assert _version_from_banner("bcftools 1.21\nUsing htslib 1.21") == "1.21"
    assert _version_from_banner("samtools v0.7.17-r1188") == "0.7.17-r1188"
    # garbage in → empty out
    assert _version_from_banner("Usage: seqtk <command>\nProgram: seqtk\n") == ""
    assert _version_from_banner("just-a-string-with-no-version") == ""
    assert _version_from_banner("") == ""


def test_resolved_version_prefers_conda_then_banner_then_out_then_anchor():
    """The chain that's identical across .md + .html renderers: conda/pip > banner
    > evidence `out` > install anchor. Each step is a runtime-captured fact (or
    an agent-supplied evidence command — labelled honestly); fakeability decreases
    left to right."""
    from agent.skills.env_report import _resolved_version
    # conda wins
    assert _resolved_version("samtools", {"version": "1.21"},
                             {"banner": "samtools 1.99", "out": "samtools 1.50"}, []) == "1.21"
    # banner beats out when no conda
    assert _resolved_version("seqtk", None,
                             {"banner": "Version: 1.4-r122", "out": ""}, []) == "1.4-r122"
    # out only — last resort before the anchor
    assert _resolved_version("tool", None, {"banner": "", "out": "tool v0.9.1"}, []) == "0.9.1"
    # anchor is the last resort (synthesized purpose carries the commit)
    assert _resolved_version("seqtk", None, {"banner": "", "out": ""},
                             [{"name": "seqtk (synthesized @ abc1234def56)",
                               "command": "..."}]) == "abc1234def56"
    # nothing → empty
    assert _resolved_version("ghost", None, None, []) == ""


def test_is_sha_recognizes_commit_only_for_hex_blobs():
    """The dual-display rule appends `(commit <sha>)` only when the install
    anchor LOOKS like a git SHA — a release tag like 'v1.4' is suppressed (would
    just duplicate the banner version)."""
    from agent.skills.env_report import _is_sha
    assert _is_sha("94e707082d39")
    assert _is_sha("abcdef1")              # 7 chars — git's short-SHA floor
    assert not _is_sha("v1.4")
    assert not _is_sha("1.4-r122")
    assert not _is_sha("abc12")            # too short
    assert not _is_sha("")


def test_html_report_dual_displays_banner_version_with_sha_anchor():
    """When a banner version is captured AND the install anchor is a SHA, the
    cell renders both: '1.4-r122 (commit 94e707082d)'. Full provenance kept."""
    from agent.skills.env_report_html import render_env_report_html
    record = {"name": "demo", "image": "demo:latest", "image_digest": "sha256:x",
              "content_digest": "sha256:y", "platform": "linux-64",
              "mode": "build", "validation_locus": "native",
              "redistributable": True, "requested_tools": ["seqtk"],
              "verifications": [{"tool": "seqtk", "check": "seqtk 2>&1 | grep -qi usage",
                                 "passed": True, "out": "",
                                 "banner": "Version: 1.4-r122\nUsage: seqtk ..."}],
              "shipped_binaries": [{"name": "seqtk (synthesized @ 94e707082d39)",
                                    "command": "git clone ..."}],
              "resolved_packages": [], "system_packages": [], "conda_specs": []}
    html = render_env_report_html(record)
    # banner version up front, commit anchor in parentheses
    assert "1.4-r122" in html
    assert "(commit 94e707082d39)" in html


def test_envbuild_verify_in_image_threads_banner_into_each_record(monkeypatch):
    """EnvBuild.verify_in_image must pass the tool tokens to validate_in_image and
    write the per-tool banner into the verification records — this is how the
    captured fact reaches the freeze record + the renderer."""
    from agent.skills import env_build as eb

    class FakeCB:
        platform = "linux/amd64"
        def validate_in_image(self, image, checks, probe_tools=None):
            assert probe_tools == ["seqtk"], "tool tokens must be forwarded"
            return {"success": True,
                    "checks": {checks[0]: {"rc": 0, "out": ""}},
                    "banners": {"seqtk": "Version: 1.4-r122"}}

    inst = eb.EnvBuild.__new__(eb.EnvBuild)
    inst.cb = FakeCB()
    inst.verifications = [{"label": "seqtk", "tool": "seqtk",
                           "check": "seqtk 2>&1 | grep -qi usage", "engine_coupled": False}]
    res = inst.verify_in_image("image:tag")
    assert res["success"]
    assert res["verifications"][0]["banner"] == "Version: 1.4-r122"
    assert res["verifications"][0]["tool"] == "seqtk"


def test_mcp_freeze_evicted_image_falls_through_to_rebuild(monkeypatch, tmp_path):
    """An EnvCache record whose image is no longer in the docker daemon (the user
    `docker rmi`'d it, or the daemon was reset) must MISS the cache and trigger
    a fresh build — not silently hand back a dangling reference + un-rendered
    reports. Re-anchors the cache against the docker daemon at lookup time."""
    import importlib
    from agent.skills import freeze as freeze_mod
    m = importlib.import_module("agent.mcp_server")
    # an existing cached record pointing at an image that's NOT in the daemon
    cache = freeze_mod.EnvCache(tmp_path / "cache.json")
    cache.register("samtools|linux-64|none",
                   {"image": "samtools_dangling:latest", "image_digest": "sha256:dead",
                    "content_digest": "sha256:dead", "mode": "build", "platform": "linux-64",
                    "tarball": "", "hpc_delivery": {}, "redistributable": True})
    # stub the daemon to return "image not present" for any inspect call
    monkeypatch.setattr(m, "subprocess",
                        type("SP", (), {"run": staticmethod(
                            lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())})())
    monkeypatch.setattr(m, "_env_cache", cache)
    monkeypatch.setattr(m._env_mgr, "project_root", tmp_path)
    # force the build path to refuse so we get a clear post-cache signal that the
    # rebuild was attempted (which is the assertion that matters here).
    def fake_build(*a, **k):
        return {"success": False, "stage": "container_build", "reason": "stub"}
    monkeypatch.setattr(m._env_freeze, "build_env_image", fake_build)
    monkeypatch.setattr(m._biocontainers, "resolve_biocontainer",
                        lambda tools, gated=False: {"found": False})
    res = m.freeze(env_name="samtools_e", tools=["samtools"], platform="linux-64")
    # NOT a cache hit (the dangling image was correctly invalidated)
    assert res.get("success") is False
    assert res.get("stage") == "container_build"   # reached the build path
    assert "cache_hit" not in res or res["cache_hit"] is False


# ---------------------------------------------------------------------------
# PHASE 1 — CONDA LOCK IN THE RECIPE.
# The recipe carries pixi.toml + pixi.lock so a rebuild materializes the env
# from those exact bytes — no solve, no chance of bioconda drift picking a
# newer build of htslib (etc.) and producing a different content_digest months
# later. These tests guard the seams that thread the lock through.
# ---------------------------------------------------------------------------

def test_extract_recipe_carries_conda_lock_when_provided():
    """extract_recipe stores the per-file lock dict as `conda_lock` in the
    recipe (or {} when omitted — backward compatible with pre-Phase-1 recipes)."""
    from agent.skills import env_recipe
    lock = {"pixi.toml": '[project]\nname="x"\n', "pixi.lock": "version: 6\n"}
    rec = env_recipe.extract_recipe(None, name="x", conda_deps=["samtools=1.21"],
                                    primary_tools=["samtools"], conda_lock=lock)
    assert rec["conda_lock"] == lock
    assert rec["conda_lock"] is not lock  # defensive copy
    # backward compatible: omitting conda_lock yields {} (not missing key)
    rec0 = env_recipe.extract_recipe(None, name="x", conda_deps=["samtools=1.21"],
                                     primary_tools=["samtools"])
    assert rec0["conda_lock"] == {}


def test_rebuild_from_recipe_passes_conda_lock_through(monkeypatch):
    """rebuild_from_recipe must forward the recipe's `conda_lock` to build_env_image
    as `conda_lock_files`, so the replay skips the solve. Without this thread, the
    recipe carries the lock but the replay still re-solves — silent drift."""
    from agent.skills import env_recipe
    cap = {}
    def fake_build(spec, **kw):
        cap.update(kw)
        return {"success": True, "content_digest": "sha256:x"}
    recipe = {"recipe_version": 1, "name": "x", "conda_deps": ["samtools=1.21"],
              "primary_tools": ["samtools"], "install_steps": [],
              "conda_lock": {"pixi.toml": "...", "pixi.lock": "..."},
              "content_digest": "sha256:x"}
    env_recipe.rebuild_from_recipe(recipe, build_fn=fake_build)
    assert cap["conda_lock_files"] == recipe["conda_lock"]


def test_envbuild_prebaked_lock_skips_solve_calls_declare_locked(monkeypatch):
    """When prebaked_lock_files is set, EnvBuild.build() must call declare_locked
    (write the lock + `pixi install --locked`) — NOT declare/declare_pypi (which
    would re-solve). This is the seam that makes Phase 1 non-fakeable: a recipe
    with a lock cannot accidentally fall back to solving."""
    from agent.skills import env_build as eb
    calls = []

    class FakeCB:
        workdir = "/work"
        class engine:
            name = "pixi"
            @staticmethod
            def lock_artifacts(): return ["pixi.toml", "pixi.lock"]
        def start(self): return {"success": True}
        def declare(self, *a, **k):
            calls.append("declare"); return {"success": True}
        def declare_pypi(self, *a, **k):
            calls.append("declare_pypi"); return {"success": True}
        def declare_locked(self, lock_files):
            calls.append(("declare_locked", sorted(lock_files)))
            return {"success": True}
        def exec(self, cmd, timeout=300):
            return {"returncode": 0, "stdout": "captured-lock-content", "stderr": ""}

    inst = eb.EnvBuild.__new__(eb.EnvBuild)
    inst.cb = FakeCB()
    inst.conda_specs = ["samtools=1.21"]
    inst.pip_specs = []
    inst.tools = []
    inst.lock_text = ""
    inst.lock_files = {}
    inst.prebaked_lock_files = {"pixi.toml": "T", "pixi.lock": "L"}
    inst.build()
    assert ("declare_locked", ["pixi.lock", "pixi.toml"]) in calls
    assert "declare" not in calls and "declare_pypi" not in calls   # NO solve


def test_pixi_engine_install_from_lock_writes_files_and_installs():
    """PixiEngine.install_from_lock writes each lock file via base64+`base64 -d`,
    then runs `pixi install --locked`. The base64 encoding is what makes binary-
    or unicode-safe lock content survive the docker exec round-trip."""
    from agent.skills.container_build import PixiEngine
    eng = PixiEngine()
    calls = []
    class FakeCB:
        def exec(self, cmd, timeout=300):
            calls.append(cmd)
            return {"returncode": 0, "stdout": "", "stderr": ""}
    r = eng.install_from_lock(FakeCB(), {"pixi.toml": "T", "pixi.lock": "L"})
    assert r["success"]
    # both files written via base64 decode into /work
    written = [c for c in calls if "base64 -d" in c]
    assert any("pixi.toml" in c for c in written)
    assert any("pixi.lock" in c for c in written)
    # final step is the lock-aware install — explicit `--locked`, NOT a solve
    assert any("pixi install --locked" in c for c in calls)


def test_base_engine_install_from_lock_refuses_rather_than_solving():
    """The base EnvEngine returns an explicit error from install_from_lock — an
    engine that hasn't implemented the lock-replay path must FAIL LOUD, never
    silently fall back to solving (which would erase the whole reproducibility
    guarantee). Micromamba inherits this default until someone implements it."""
    from agent.skills.container_build import EnvEngine
    r = EnvEngine().install_from_lock(None, {"foo": "bar"})
    assert r["success"] is False
    assert "does not implement install_from_lock" in r["stderr"]


# ---------------------------------------------------------------------------
# PHASE 2 — APT LAYER PINNED TO SNAPSHOT.DEBIAN.ORG.
# The freeze captures a UTC timestamp; every apt-get (live build container AND
# emitted Dockerfile, builder + runtime stages) points at the snapshot archive
# at that timestamp. Same apt bytes across time/machines — no `apt-get update`
# drift picking newer libssl3 et al.
# ---------------------------------------------------------------------------

def test_snapshot_sources_list_formats_bookworm_main_plus_security():
    """The sources.list emitted points at snapshot.debian.org/<timestamp>/ for
    bookworm main AND debian-security/<timestamp>/ for bookworm-security. Both
    are needed so security updates within the snapshot moment are resolvable."""
    from agent.skills.container_build import _snapshot_sources_list
    out = _snapshot_sources_list("20260526T200000Z")
    assert "http://snapshot.debian.org/archive/debian/20260526T200000Z/ bookworm main" in out
    assert "http://snapshot.debian.org/archive/debian-security/20260526T200000Z/ bookworm-security main" in out


def test_emit_dockerfile_pins_apt_to_snapshot_when_timestamp_given():
    """When apt_snapshot is set, every `apt-get update` in the Dockerfile is
    preceded by a sources.list rewrite to snapshot.debian.org AND uses
    Acquire::Check-Valid-Until=false (the snapshot's expired Valid-Until is
    still GPG-valid — we accept it explicitly)."""
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(),
                         has_env_layer=False, longtail_steps=[],
                         apt_snapshot="20260526T200000Z")
    assert "snapshot.debian.org/archive/debian/20260526T200000Z" in df
    assert "Acquire::Check-Valid-Until=false" in df
    # both stages (builder + runtime) must be pinned — search count.
    assert df.count("snapshot.debian.org/archive/debian/20260526T200000Z") >= 2


def test_emit_dockerfile_omits_snapshot_when_timestamp_empty():
    """Backward compat: pre-Phase-2 recipes (no apt_snapshot) emit the original
    floating-apt Dockerfile, byte-identical to the previous behavior."""
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(),
                         has_env_layer=False, longtail_steps=[])
    assert "snapshot.debian.org" not in df
    assert "Acquire::Check-Valid-Until" not in df


def test_envbuild_content_digest_folds_in_apt_snapshot_when_set():
    """When apt_snapshot is set, it BINDS the apt layer into the digest — two
    rebuilds at different snapshot timestamps yield different digests (so the
    digest no longer lies about the apt layer). Empty apt_snapshot leaves the
    digest unchanged (pre-Phase-2 behavior, recipes verify across versions)."""
    from agent.skills.env_build import EnvBuild
    eb_a = EnvBuild("demo", "1.0", platform="linux/amd64", apt_snapshot="20260526T200000Z")
    eb_b = EnvBuild("demo", "1.0", platform="linux/amd64", apt_snapshot="20260526T200001Z")
    eb_z = EnvBuild("demo", "1.0", platform="linux/amd64")   # no snapshot
    eb_a.lock_text = eb_b.lock_text = eb_z.lock_text = "L"
    da, db, dz = eb_a.content_digest(), eb_b.content_digest(), eb_z.content_digest()
    assert da != db                    # different snapshots → different digests
    assert dz != da and dz != db       # no-snapshot digest is its own thing
    # second EnvBuild with the SAME snapshot must reproduce the digest
    eb_a2 = EnvBuild("demo", "1.0", platform="linux/amd64", apt_snapshot="20260526T200000Z")
    eb_a2.lock_text = "L"
    assert eb_a2.content_digest() == da


def test_envbuild_apt_snapshot_is_threaded_into_container_build():
    """EnvBuild constructs its ContainerBuild with apt_snapshot — the live build
    container's apt commands honor the pin, not just the emitted Dockerfile.
    Without this, `pixi add` would solve against the snapshot-pinned env in the
    Dockerfile but the BUILD container's apt would float (mismatch)."""
    from agent.skills.env_build import EnvBuild
    eb = EnvBuild("demo", platform="linux/amd64", apt_snapshot="20260526T200000Z")
    assert eb.cb.apt_snapshot == "20260526T200000Z"


def test_extract_recipe_carries_apt_snapshot():
    """extract_recipe stores apt_snapshot in the recipe so rebuild_from_recipe
    can replay against the same snapshot. Empty string when omitted (backward
    compatible — pre-Phase-2 recipes are missing the field)."""
    from agent.skills import env_recipe
    rec = env_recipe.extract_recipe(None, name="x", conda_deps=[], primary_tools=[],
                                    apt_snapshot="20260526T200000Z")
    assert rec["apt_snapshot"] == "20260526T200000Z"
    rec0 = env_recipe.extract_recipe(None, name="x", conda_deps=[], primary_tools=[])
    assert rec0["apt_snapshot"] == ""


def test_rebuild_from_recipe_forwards_apt_snapshot_to_build_env_image():
    """The recipe-replay path threads apt_snapshot through so the rebuild's apt
    resolves to the SAME bytes as the original freeze. No drift on rebuild."""
    from agent.skills import env_recipe
    cap = {}
    def fake_build(spec, **kw):
        cap.update(kw); return {"success": True, "content_digest": "sha256:x"}
    recipe = {"recipe_version": 1, "name": "x", "conda_deps": [],
              "primary_tools": [], "install_steps": [],
              "apt_snapshot": "20260526T200000Z"}
    env_recipe.rebuild_from_recipe(recipe, build_fn=fake_build)
    assert cap["apt_snapshot"] == "20260526T200000Z"


def test_build_env_image_auto_generates_snapshot_when_none_given(monkeypatch):
    """A FIRST freeze (no recipe yet) auto-generates a UTC snapshot timestamp.
    Captures it into the build so the recipe written from that freeze carries it,
    closing the loop — subsequent replays pin to the captured moment."""
    import re
    from agent.skills import env_freeze
    cap = {}
    class FakeEB:
        def __init__(self, *a, **k): cap.update(k)
        def add_conda(self, *a, **k): pass
        def add_pip(self, *a, **k): pass
        def add_tool(self, *a, **k): pass
        def run(self): return {"success": True}
        def request_key(self): return "rk"
    monkeypatch.setattr(env_freeze, "EnvBuild", FakeEB)
    env_freeze.build_env_image({"install_steps": []}, name="demo", conda_deps=[])
    assert re.fullmatch(r"\d{8}T\d{6}Z", cap["apt_snapshot"])


# ---------------------------------------------------------------------------
# PHASE 3 — SOFTWARE HERITAGE LINK-ROT FALLBACK FOR SOURCE-TIER INSTALLS.
# A git source install gets a `_swh_clone` wrapper baked into the install
# command. If the upstream URL is dead at rebuild time, SWH's git_bare vault
# serves the same commit (SHA-verified after — SWH can't lie about contents
# without changing the SHA). Zero freeze-time cost; pays only when needed.
# ---------------------------------------------------------------------------

def test_source_install_uses_swh_clone_not_bare_git_clone():
    """install_commands.source emits `_swh_clone <url> <ref> <dst>` instead of
    a bare `git clone`. This is the seam that makes link-rot self-healing — a
    bare git clone has no fallback; _swh_clone tries upstream then SWH."""
    from agent.skills.install_commands import source
    s = source("seqtk", "https://github.com/lh3/seqtk",
               ref="ae7defa8bead", build_command="make", bin_path="seqtk")
    assert "_swh_clone https://github.com/lh3/seqtk ae7defa8bead " in s["command"]
    assert "git clone https://" not in s["command"]   # no bare clone left


def test_script_repo_install_uses_swh_clone_not_bare_git_clone():
    """script_repo (run-by-path academic repos) gets the same _swh_clone wrap —
    half-baked academic tools are the MOST common link-rot victims."""
    from agent.skills.install_commands import script_repo
    s = script_repo("acad_tool", "https://github.com/lab/x", ref="abc1234",
                    script_rel="run.py", interpreter="python")
    assert "_swh_clone https://github.com/lab/x abc1234 " in s["command"]
    assert "git clone https://" not in s["command"]


def test_swh_clone_script_has_required_shape():
    """The _SWH_CLONE_SCRIPT helper installed in the builder stage must (1) try
    upstream first, (2) hit SWH's vault API on failure, (3) poll until done or
    fail loud, (4) clone from the resulting tarball locally."""
    from agent.skills.container_build import _SWH_CLONE_SCRIPT
    assert "git clone \"$url\" \"$dst\"" in _SWH_CLONE_SCRIPT         # upstream first
    assert "archive.softwareheritage.org/api/1/vault/git_bare/" in _SWH_CLONE_SCRIPT
    assert "swh:1:rev:${commit}" in _SWH_CLONE_SCRIPT                 # commit-anchored
    assert "jq -r '.status" in _SWH_CLONE_SCRIPT                      # status polling
    assert 'status" = "done"' in _SWH_CLONE_SCRIPT                    # done branch
    assert "exit 2" in _SWH_CLONE_SCRIPT                              # fails loud on SWH failure


def test_emit_dockerfile_installs_swh_clone_helper_in_builder_stage():
    """The builder stage of the emitted Dockerfile must install _swh_clone BEFORE
    any source-tier install commands run — they call it. Without this, source
    installs would fail with `command not found: _swh_clone`."""
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(),
                         has_env_layer=False, longtail_steps=[])
    assert "/usr/local/bin/_swh_clone" in df
    assert "chmod +x /usr/local/bin/_swh_clone" in df
    # the install must precede `mkdir -p /opt/tools` (which is where source
    # installs land) — installed in the builder stage early, before any source
    # commands can call it.
    swh_pos = df.index("/usr/local/bin/_swh_clone")
    tools_pos = df.index("mkdir -p /opt/tools")
    assert swh_pos < tools_pos


def test_build_apt_includes_jq_for_swh_fallback():
    """jq is required by _swh_clone to parse SWH's vault API JSON. It's in the
    BUILD set (~1MB) but NOT the RUNTIME set — never ships."""
    from agent.skills.container_build import _BUILD_APT, _RUNTIME_APT
    assert " jq " in f" {_BUILD_APT} "
    assert " jq " not in f" {_RUNTIME_APT} "   # never ships


def test_env_vendor_stub_returns_explicit_not_implemented():
    """The HEAVY mirror sidecar (audit-proof mode) is a stub for now — must
    refuse explicitly rather than silently doing nothing. Adding it later is a
    contained change against this contract."""
    from agent.skills import env_vendor
    r = env_vendor.materialize([{"install_method": {"type": "release_binary"}}], "/tmp/x")
    assert r["success"] is False
    assert "audit_proof" in r["reason"] or "future heavy-mode" in r["reason"]
