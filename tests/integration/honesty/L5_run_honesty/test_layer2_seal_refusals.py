"""
Layer-2 (workflow) seal contract: seal_workflow refuses to write a
WorkflowSpec on any of:

  I0  shape sanity — top-level lists hold only dicts
  I3  validated outputs — every rc=0 step has detected_outputs AND no
                          validation uses expected_type='any'
  I6  paths/placeholders — every input/output path is absolute AND every
                           {PLACEHOLDER} in usage.command_template is
                           declared in usage.inputs (or OUTPUT_DIR/OUT_DIR)
  I7  resource_usage — every rc=0 step has wall_seconds + peak_rss_mb
  I8  composition coherence — every step input traces to a prior step's
                              outputs OR an external source (test_data,
                              reference_databases, runtime_configs,
                              authored_artifacts)

Every clause was either added because of a real failure mode the contract
needed to catch, or carried verbatim from the host-writer days. They are
the gates that let `validated == shipped` stay an honest claim.

Why integration, not unit: each invariant clause is independently
testable as a function (and is, in test_invariants.py), but the
INTEGRATION value is that one constructed spec exercises the whole
check_workflow_invariants surface. If a regression silently drops a
clause from the workflow tier, the unit tests still pass but
seal_workflow lets a violating spec through. These tests pin which
invariants fire on which violations from a single chained call.
"""
from __future__ import annotations

import pytest

from agent.skills.spec_writer import check_workflow_invariants


def _minimal_passing_spec() -> dict:
    """A spec that passes all five workflow invariants — the baseline
    each test mutates one field of."""
    return {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "test_data": {"bam": "/abs/path/in.bam"},
        "pipeline_steps": [{
            "step": 1, "tool": "samtools",
            "command": "samtools view /abs/path/in.bam > /abs/path/out.txt",
            "returncode": 0,
            "inputs": [{"path": "/abs/path/in.bam"}],
            "detected_outputs": ["/abs/path/out.txt"],
            "validation": {"out.txt": {"valid": True, "expected_type": "txt"}},
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0,
                               "max_cpu_percent": 20.0},
        }],
    }


def _violations(spec: dict, invariant_prefix: str) -> list[dict]:
    return [v for v in check_workflow_invariants(spec)
            if v.get("invariant", "").startswith(invariant_prefix)]


@pytest.mark.integration
def test_baseline_spec_passes_every_workflow_invariant():
    """The fixture itself must be clean — otherwise the negative tests
    below are testing something else by accident."""
    assert check_workflow_invariants(_minimal_passing_spec()) == []


@pytest.mark.integration
def test_i3_step_with_no_outputs_refused():
    """A rc=0 step with empty detected_outputs (and no mark_step_validated)
    is the silent-empty-success trap. I3 catches it."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["detected_outputs"] = []
    spec["pipeline_steps"][0].pop("validation", None)
    v = _violations(spec, "I3.")
    assert any(x["invariant"] == "I3.pipeline_step_has_outputs" for x in v), \
        f"silent-empty-success not refused: {v}"


@pytest.mark.integration
def test_i3_expected_type_any_refused():
    """The amendment: every validation must declare a real type so the
    validator dispatches to a type-aware checker. `touch foo.bar`
    creating a non-empty file passes type='any' but fails the contract."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["validation"] = {
        "out.txt": {"valid": True, "expected_type": "any"},
    }
    v = _violations(spec, "I3.")
    assert any(x["invariant"] == "I3.declared_output_type" for x in v), \
        f"expected_type=any was not refused: {v}"


@pytest.mark.integration
def test_i3_failed_validation_refused():
    """C1: a validate_output record EXISTING is not the same as it PASSING.
    A step whose output validation recorded passed=False (malformed BAM,
    empty VCF, bad JSON) must NOT seal — that would let the guide claim
    'outputs checked' over a demonstrably failed check."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["validation"] = {
        "out.txt": {"passed": False, "expected_type": "txt",
                    "error": "type-aware validator rejected the file"},
    }
    v = _violations(spec, "I3.")
    assert any(x["invariant"] == "I3.validation_passed" for x in v), \
        f"a passed=False validation record was not refused: {v}"


@pytest.mark.integration
def test_i3_failed_validation_cannot_be_overridden_by_mark_validated():
    """mark_step_validated CANNOT bury a validation record that says FAILED.

    This test used to assert the opposite — that `validation_status='passed'` let a
    step seal over a `passed: False` record because "the agent asserts it verified the
    output by other means". That is verbatim the one thing CLAUDE.md's opening promise
    rules out ("nothing is taken on faith from the agent"), and it was a switch, exposed
    on the MCP surface, that turned off the C1 amendment built specifically to stop this.

    The distinction that makes the override legitimate elsewhere: the other I3 clauses
    concern ABSENT evidence, where "I checked it another way" adds information. Here the
    evidence exists and says FAILED. An assertion cannot un-fail a measurement — it can
    only hide it (audit 2026-07-16, re-audit)."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["validation"] = {
        "out.txt": {"passed": False, "expected_type": "txt"},
    }
    spec["pipeline_steps"][0]["validation_status"] = "passed"
    v = _violations(spec, "I3.")
    assert any(x["invariant"] == "I3.validation_passed" for x in v), \
        f"mark_step_validated must NOT override a failed validation record: {v}"


@pytest.mark.integration
def test_i3_mark_validated_still_substitutes_for_absent_validation():
    """The override that IS legitimate survives: outputs with NO validate_output record
    (not validate_output-able, verified another way) still seal when explicitly marked.
    Narrowing the failed-record case must not break the absent-record case."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["validation"] = {}          # no per-file records at all
    spec["pipeline_steps"][0]["validation_status"] = "passed"
    v = _violations(spec, "I3.")
    assert not any(x["invariant"] == "I3.outputs_validated" for x in v), \
        f"mark_step_validated should still substitute for ABSENT validation: {v}"


@pytest.mark.integration
def test_i3_outputs_present_but_unvalidated_refused():
    """Gap surfaced by scripts/extract_outcomes.py (I3.outputs_validated had no
    test reference): a rc=0 step with detected_outputs but NO validate_output
    record (and no mark_step_validated) claims outputs it never proved. This is
    the sibling of the no-outputs trap (I3.pipeline_step_has_outputs) and the
    failed-validation trap (I3.validation_passed) — outputs exist, proof doesn't."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0].pop("validation", None)   # keep the outputs, drop the proof
    v = _violations(spec, "I3.")
    assert any(x["invariant"] == "I3.outputs_validated" for x in v), \
        f"outputs-present-but-unvalidated was not refused: {v}"


@pytest.mark.integration
def test_i7_zero_resources_refused():
    """C3: keys existing is not enough — an all-zeros resource_usage means
    the monitor captured NOTHING (a process that ran has nonzero peak RSS
    and wall). Sealing zeros would fabricate the HPC job-sizing numbers the
    guide publishes."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["resource_usage"] = {
        "wall_seconds": 0.0, "peak_rss_mb": 0.0, "max_cpu_percent": 0.0,
    }
    v = _violations(spec, "I7.")
    assert any(x["invariant"] == "I7.resource_usage_captured" for x in v), \
        f"all-zeros resource_usage was not refused: {v}"


@pytest.mark.integration
def test_i7_sacct_error_refused():
    """C3: a cluster step whose sacct query hiccuped records a sacct_error
    marker with placeholder zeros. That's no honest observation — refuse."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["resource_usage"] = {
        "wall_seconds": 0.0, "peak_rss_mb": 0.0, "max_cpu_percent": 0.0,
        "locus": "cluster", "sacct_job_id": "123",
        "sacct_error": "sacct returned no rows for job 123",
    }
    v = _violations(spec, "I7.")
    assert any(x["invariant"] == "I7.resource_usage_captured" for x in v), \
        f"sacct_error resource_usage was not refused: {v}"


@pytest.mark.integration
def test_i6_relative_input_path_refused():
    """Relative paths are reproducibility landmines (they depend on the
    agent's CWD at finalize time)."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["inputs"] = [{"path": "data/relative/in.bam"}]
    v = _violations(spec, "I6.")
    assert any(x["invariant"] == "I6.absolute_paths" for x in v), \
        f"relative input path was not refused: {v}"


@pytest.mark.integration
def test_i6_undeclared_template_placeholder_refused():
    """`{OUPUT_DIR}` (typo for OUTPUT_DIR) silently passes the static
    path check (starts with `{`). I6 placeholder-declared check catches
    it before a trial run does."""
    spec = _minimal_passing_spec()
    spec["usage"] = {
        "command_template": "samtools view {INPUT_BAM} > {OUPUT_DIR}/out.txt",
        "inputs": [{"name": "INPUT_BAM", "format": "bam"}],
    }
    v = _violations(spec, "I6.")
    assert any(x["invariant"] == "I6.template_placeholders_declared" for x in v), \
        f"undeclared placeholder {{OUPUT_DIR}} was not refused: {v}"


@pytest.mark.integration
def test_i7_missing_resource_usage_refused():
    """psutil's wall/peak_rss observations are the proof that the runtime
    actually saw the step run. Without them an agent could synthesize a
    pipeline_step record without ever running it."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0].pop("resource_usage")
    v = _violations(spec, "I7.")
    assert any(x["invariant"] == "I7.resource_usage_recorded" for x in v), \
        f"missing resource_usage was not refused: {v}"


@pytest.mark.integration
def test_i8_orphan_input_refused():
    """An input that traces to neither external sources NOR a prior step's
    outputs is an orphan — the pipeline doesn't actually compose."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["inputs"] = [{"path": "/abs/path/orphan_input.bam"}]
    # No matching test_data, no prior step output. Should fire I8.
    v = _violations(spec, "I8.")
    assert any(x["invariant"] == "I8.composition_coherence" for x in v), \
        f"orphan input was not refused: {v}"


@pytest.mark.integration
def test_i8_authored_artifact_satisfies_provenance(tmp_path):
    """authored_artifacts is a legitimate provenance source — a step that
    consumes a hand-staged BAM (recorded via stage_authored_artifact)
    must NOT trip I8 just because the path isn't in test_data.

    Uses a REAL on-disk file with a REAL sha256 because seal-time integrity
    (I8.authored_artifact_mutated/missing — Tier A G1) re-anchors the recorded
    sha256 against the bytes on disk. A fake path + fake sha would fail the
    integrity check; this test is about the COMPOSITION-coherence check, so
    we have to satisfy integrity to isolate composition behavior."""
    import hashlib
    art_path = tmp_path / "authored.bam"
    art_bytes = b"BAM\x01"
    art_path.write_bytes(art_bytes)
    art_sha = hashlib.sha256(art_bytes).hexdigest()

    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["inputs"] = [{"path": str(art_path)}]
    spec["authored_artifacts"] = [
        {"path": str(art_path), "role": "fixture",
         "description": "hand-staged test BAM", "sha256": art_sha},
    ]
    v = _violations(spec, "I8.")
    assert v == [], f"authored_artifact path should satisfy I8: {v}"


# ---------------------------------------------------------------------------
# usage.command_template is a str OR a list[str] — the multi-phase how-to.
# `pipeline_steps` was always a list and I8 lineage always held across a chain,
# but the HOW-TO contract — the thing a user reads and runs, and the thing the
# guides render — could only ever say ONE command. No amount of later guide
# design fixes data that cannot say what you mean (audit 2026-07-16).
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_i6_scans_placeholders_in_every_command_not_just_the_first():
    """An undeclared placeholder in command 3 is exactly as broken as one in command 1.

    Reading the raw field here would have seen only the first command — which is the
    tier-2 bug in miniature: a check that inspects something other than what the runner
    actually uses."""
    spec = _minimal_passing_spec()
    spec["usage"] = {
        "description": "three phases",
        "command_template": [
            "sort {INPUT_BAM} > {OUTPUT_DIR}/a.txt",
            "uniq {OUTPUT_DIR}/a.txt > {OUTPUT_DIR}/b.txt",
            "wc -l < {OUTPUT_DIR}/b.txt > {TYPOED_DIR}/c.txt",   # only reachable if all are scanned
        ],
        "inputs": [{"name": "INPUT_BAM", "format": "bam"}],
    }
    v = _violations(spec, "I6.")
    assert any(x["invariant"] == "I6.template_placeholders_declared" for x in v), \
        f"an undeclared placeholder in the LAST command was not refused: {v}"
    bad = next(x for x in v if x["invariant"] == "I6.template_placeholders_declared")
    assert bad["undeclared_placeholders"] == ["TYPOED_DIR"], bad


@pytest.mark.integration
def test_i6_accepts_a_valid_multi_command_how_to():
    """The pair: a well-formed multi-phase how-to must NOT be refused."""
    spec = _minimal_passing_spec()
    spec["usage"] = {
        "description": "three phases",
        "command_template": [
            "sort {INPUT_BAM} > {OUTPUT_DIR}/a.txt",
            "uniq {OUTPUT_DIR}/a.txt > {OUTPUT_DIR}/b.txt",
        ],
        "inputs": [{"name": "INPUT_BAM", "format": "bam"}],
    }
    v = _violations(spec, "I6.")
    assert not any(x["invariant"] == "I6.template_placeholders_declared" for x in v), v


@pytest.mark.integration
def test_self_test_runs_every_command_in_order_sharing_one_scratch_dir():
    """A → B → C: each command consumes the previous one's output. Drives the REAL
    self_test_usage with a real shell runner — the whole point is that the sequence,
    not just the first command, is what gets verified."""
    import subprocess as _sp
    import tempfile
    from pathlib import Path as _P
    from agent.skills import spec_writer

    src = _P(tempfile.mkdtemp()) / "reads.txt"
    src.write_text("beta\nalpha\nalpha\n")

    class _Host:
        is_image_runner = False
        def run_in_env(self, env, command, timeout=600, watch_dir=None):
            p = _sp.run(command, shell=True, capture_output=True, text=True, cwd=watch_dir)
            return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}

    spec = {
        "pipeline_name": "multi", "conda_env": "host",
        "usage": {
            "description": "A->B->C",
            "command_template": [
                "sort {INPUT} > {OUTPUT_DIR}/a.sorted.txt",
                "uniq {OUTPUT_DIR}/a.sorted.txt > {OUTPUT_DIR}/b.uniq.txt",
                "wc -l < {OUTPUT_DIR}/b.uniq.txt > {OUTPUT_DIR}/c.count.txt",
            ],
            "inputs":  [{"name": "INPUT", "format": "txt"}],
            "outputs": [{"name": "OUTPUT_DIR",
                         "files": ["a.sorted.txt", "b.uniq.txt", "c.count.txt"]}],
            "trials":  [{"name": "abc", "substitutions": {"INPUT": str(src)}}],
        },
    }
    r = spec_writer.self_test_usage(spec, _Host())
    assert r["status"] == "verified", r
    t = r["trials"][0]
    assert t["ok"] is True, t
    assert len(t["commands_run"]) == 3, t["commands_run"]
    # step 2 really consumed step 1's output, so all three landed in ONE scratch dir
    assert sorted(t["produced_files"]) == ["a.sorted.txt", "b.uniq.txt", "c.count.txt"], t


@pytest.mark.integration
def test_self_test_stops_at_the_first_failing_command():
    """A broken middle step fails the trial and names WHICH step — and must stop, so a
    later command can never `touch` the declared outputs over a broken earlier one."""
    import subprocess as _sp
    import tempfile
    from pathlib import Path as _P
    from agent.skills import spec_writer

    src = _P(tempfile.mkdtemp()) / "reads.txt"
    src.write_text("x\n")

    class _Host:
        is_image_runner = False
        def run_in_env(self, env, command, timeout=600, watch_dir=None):
            p = _sp.run(command, shell=True, capture_output=True, text=True, cwd=watch_dir)
            return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}

    spec = {
        "pipeline_name": "multi", "conda_env": "host",
        "usage": {
            "description": "B is broken; C would fake the outputs",
            "command_template": [
                "sort {INPUT} > {OUTPUT_DIR}/a.sorted.txt",
                "zzz_no_such_command_xyz {OUTPUT_DIR}/a.sorted.txt",
                "touch {OUTPUT_DIR}/b.uniq.txt {OUTPUT_DIR}/c.count.txt",
            ],
            "inputs":  [{"name": "INPUT", "format": "txt"}],
            "outputs": [{"name": "OUTPUT_DIR", "files": ["b.uniq.txt", "c.count.txt"]}],
            "trials":  [{"name": "broken", "substitutions": {"INPUT": str(src)}}],
        },
    }
    r = spec_writer.self_test_usage(spec, _Host())
    assert r["status"] == "failed", r
    t = r["trials"][0]
    assert t["failed_index"] == 2, t
    assert "command 2 of 3" in t["reason"], t["reason"]
    assert len(t["commands_run"]) == 2, "must not have run command 3"
