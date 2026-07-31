"""
A multi-step CLUSTER chain could not be sealed, and the reason was narrower than
it looked.

Measured on the real draft at data/pipeline_drafts/rnaseq_deseq2_chr22_cluster —
htseq-count then DESeq2, both on Longleaf — `check_workflow_invariants` returned
9 I8 orphans. They have TWO different causes and only one is a system defect:

  8 of 9  the draft declared no external sources at all, so its cluster-resident
          BAMs, GTF and coldata.tsv were undeclared. A supported declaration
          path already exists (`reference_databases` entries with
          `locus: cluster`), and using it clears all eight. Not a defect.

  1 of 9  step 2's input is the DIRECTORY step 1 wrote into. Step 1's
          `detected_outputs` are the DOWNLOADED LOCAL copies — right for
          validation, since those are the bytes we hashed — so there was no
          record anywhere of where its outputs live ON THE CLUSTER, and no local
          path can ever match a remote one. Step 2's input traced to nothing.
          THAT is the defect, and it made the chain unsealable.

THE FIX RECORDS THE TRUTH RATHER THAN WIDENING THE GATE. Two halves:

  * `remote_outputs` — where an off-host step's outputs live at the locus that
    produced them. A DEDICATED field, not a reuse of `outputs`: `outputs` is
    read first-wins as `detected_outputs or outputs`, so remote paths there
    would make a step whose downloads ALL failed look like it produced
    something, defeating the silent-empty-success trap I3 exists to catch. It
    also already has a second producer (`run_in_env`) writing a different
    dialect. That objection is pinned below as a regression test.

  * directory containment — an input that is the EXACT parent of a recorded
    prior output traces to it. Exact, never prefix or ancestor, so `/work/run1`
    cannot capture `/work/run10` and an input of `/work` cannot capture the
    filesystem.

With both, the real two-step chain goes to zero violations.
"""
from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path

import pytest
import yaml

from agent.skills import spec_writer as sw


@pytest.fixture(autouse=True)
def _never_dial_out(monkeypatch):
    """`check_workflow_invariants` really does ssh on a cluster-locus reference
    (I5 → acquire_data.check_cluster_reference_db → _probe_cluster_path). A test
    that reaches a real cluster is both slow and against the rules here, so the
    probe is stubbed for every test in this file."""
    from agent.skills import acquire_data
    monkeypatch.setattr(acquire_data, "check_cluster_reference_db", lambda rdb: [])


def _i8(spec) -> list:
    return [v for v in sw.check_workflow_invariants(spec)
            if v["invariant"].startswith("I8")]


def _step(n, ins, outs, *, remote=None, locus="cluster"):
    s = {"step": n, "tool": f"t{n}", "command": f"c{n}", "returncode": 0,
         "inputs": [{"path": p} for p in ins],
         "detected_outputs": outs,
         "validation": {o: {"passed": True} for o in outs},
         "validation_locus": locus,
         "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 1.0,
                            "peak_cpu_percent": 1.0}}
    if remote is not None:
        s["remote_outputs"] = remote
    return s


# ---------------------------------------------------------------------------
# Directory containment — exact parent, and nothing else.
# ---------------------------------------------------------------------------

def test_a_step_may_consume_the_directory_a_prior_step_wrote_into():
    """The normal shape of a cluster chain: N files out, one directory in."""
    spec = {"pipeline_steps": [
        _step(1, ["/ext/in.bam"], ["/local/a.tsv"],
              remote=["/remote/wf/a.tsv", "/remote/wf/b.tsv"]),
        _step(2, ["/remote/wf"], ["/local/res.tsv"])],
        "reference_databases": [{"name": "in", "version": "1",
                                 "local_path": "/ext/in.bam"}]}
    assert _i8(spec) == []


def test_containment_is_the_exact_parent_never_a_prefix():
    """`/work/run1` must not capture `/work/run10` — the classic string-prefix
    bug, which here would forge a lineage edge between unrelated runs."""
    spec = {"pipeline_steps": [
        _step(1, [], [], remote=["/work/run10/out.tsv"]),
        _step(2, ["/work/run1"], ["/local/x.tsv"])]}
    orphans = [v["orphan_path"] for v in _i8(spec)]
    assert orphans == ["/work/run1"]


def test_containment_does_not_reach_into_a_subtree():
    """An input of `/work` must not trace to `/work/a/b/out.tsv`. Ancestor
    matching is not provenance, it is a wildcard."""
    spec = {"pipeline_steps": [
        _step(1, [], [], remote=["/work/a/b/out.tsv"]),
        _step(2, ["/work"], ["/local/x.tsv"])]}
    assert [v["orphan_path"] for v in _i8(spec)] == ["/work"]


def test_a_trailing_slash_does_not_change_the_answer():
    spec = {"pipeline_steps": [
        _step(1, [], [], remote=["/remote/wf/a.tsv"]),
        _step(2, ["/remote/wf/"], ["/local/x.tsv"])]}
    assert _i8(spec) == []


def test_containment_also_works_for_local_chains():
    """Not cluster-specific — a local step that consumes a directory a prior
    local step filled is the same true relation."""
    spec = {"pipeline_steps": [
        _step(1, [], ["/out/dir/a.tsv"], locus="host"),
        _step(2, ["/out/dir"], ["/out/res.tsv"], locus="host")]}
    assert _i8(spec) == []


# ---------------------------------------------------------------------------
# remote_outputs — a dedicated channel, with the trap it was kept away from.
# ---------------------------------------------------------------------------

def test_remote_outputs_lets_a_second_cluster_step_trace_a_specific_file():
    spec = {"pipeline_steps": [
        _step(1, [], ["/local/a.tsv"], remote=["/remote/wf/a.tsv"]),
        _step(2, ["/remote/wf/a.tsv"], ["/local/res.tsv"])]}
    assert _i8(spec) == []


def test_without_remote_outputs_the_chain_is_exactly_as_broken_as_before():
    """The counterfactual: this is the state every cluster record on disk is in,
    and it is why a multi-step cluster chain could not seal."""
    spec = {"pipeline_steps": [
        _step(1, [], ["/local/a.tsv"]),
        _step(2, ["/remote/wf/a.tsv"], ["/local/res.tsv"])]}
    assert [v["orphan_path"] for v in _i8(spec)] == ["/remote/wf/a.tsv"]


def test_remote_outputs_does_not_rescue_a_step_that_produced_nothing():
    """THE REGRESSION GUARD, and the reason this is not just written into
    `outputs`. `outputs` is read first-wins (`detected_outputs or outputs`), so
    putting remote paths there would make a step whose downloads ALL failed —
    detected_outputs empty — look like it produced something. I3's
    silent-empty-success refusal must still fire."""
    step = _step(1, [], [], remote=["/remote/wf/a.tsv"])
    step["validation"] = {}
    codes = {v["invariant"] for v in sw.check_workflow_invariants(
        {"pipeline_steps": [step]})}
    assert any(c.startswith("I3") for c in codes), \
        f"a step with no detected outputs must still be refused; got {codes}"


def test_remote_outputs_is_held_to_the_same_absoluteness_rule():
    """It feeds the I8 universe, so it is a path channel into the lineage graph.
    A field other invariants trust and I6 does not examine is an unchecked
    channel."""
    spec = {"pipeline_steps": [_step(1, [], ["/local/a.tsv"],
                                     remote=["relative/a.tsv"])]}
    msgs = [v["message"] for v in sw.check_workflow_invariants(spec)
            if v["invariant"] == "I6.absolute_paths"]
    assert any("remote output" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# The real chain — from a COMMITTED fixture, not from the working tree.
#
# These three tests used to read data/pipeline_drafts/rnaseq_deseq2_chr22_cluster
# .draft.yaml directly. That is agent-mutable working state under a gitignored
# `data/` (0 files tracked), so it was never a regression test: it pinned whatever
# the agent had most recently done, and it errored outright on a fresh clone where
# the path does not exist. The next Longleaf drive re-ran this very chain and all
# three failed — not because the fix regressed, but because the draft they were
# reading had been replaced by a better one.
#
# The fixture below is that shape, committed: the same two real cluster steps, with
# the two things the pre-fix draft lacked removed (the runner did not yet record
# `remote_outputs`, and no external sources were declared). Derived from a genuine
# second run of the chain on Longleaf, it reproduces the original measurement
# exactly — 9 orphans, 8 undeclared external inputs plus 1 untraceable directory —
# which is the independent check that it is faithful rather than merely convenient.
# ---------------------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "cluster_chain_prefix_draft.yaml"

#: The workflow_dir step 1 wrote into, and the directory step 2 consumes.
CHAIN_WFDIR = "/work/users/a/o/ao33/CLAUDE_SCRATCH/longleaf_test/htseq_count_chain"
CHAIN_INPUTS = "/work/users/a/o/ao33/CLAUDE_SCRATCH/longleaf_test/rnaseq_inputs"
COUNTS = ("ctrl_rep1", "ctrl_rep2", "ctrl_rep3", "treat_rep1", "treat_rep2", "treat_rep3")


def _real():
    return yaml.safe_load(FIXTURE.read_text())


def _with_remote_outputs(d):
    d["pipeline_steps"][0]["remote_outputs"] = [
        f"{CHAIN_WFDIR}/{n}.counts.tsv" for n in COUNTS]
    return d


def test_the_real_cluster_chain_reproduces_the_gap_as_measured():
    """9 orphans, on a real two-step cluster chain with nothing declared."""
    v = _i8(_real())
    assert len(v) == 9, Counter(x["orphan_path"] for x in v)


def test_recording_the_remote_outputs_closes_the_lineage_orphan():
    """The one system defect of the nine. The other eight are undeclared external
    inputs, which is a different problem with a supported answer."""
    orphans = [v["orphan_path"] for v in _i8(_with_remote_outputs(_real()))]
    assert CHAIN_WFDIR not in orphans, "the directory input still does not trace"
    assert len(orphans) == 8
    assert all("/rnaseq_inputs/" in p for p in orphans), \
        "what remains should be exactly the undeclared external inputs"


def test_the_real_two_step_cluster_chain_now_seals():
    """END TO END. Outputs recorded by the runner + inputs declared by the user
    ⇒ zero violations. This is the claim 'a multi-step cluster chain cannot be
    honestly sealed' being retired, on the real artifact that established it."""
    d = _with_remote_outputs(_real())
    d["reference_databases"] = [
        {"name": n, "version": "chr22-demo", "locus": "cluster",
         "local_path": f"{CHAIN_INPUTS}/{n}"}
        for n in ("ctrl_rep1.sorted.bam", "ctrl_rep2.sorted.bam", "ctrl_rep3.sorted.bam",
                  "treat_rep1.sorted.bam", "treat_rep2.sorted.bam", "treat_rep3.sorted.bam",
                  "gencode.v44.chr22.gtf", "coldata.tsv")]
    assert sw.check_workflow_invariants(d) == []


@pytest.mark.parametrize("spec_path", sorted(__import__("glob").glob(
    "env_reports/*.workflow.yaml")) or [None])
def test_no_already_sealed_workflow_starts_failing(spec_path, monkeypatch):
    """The rules above only ever ACCEPT more, but 'only accepts more' has been
    wrong here before — the last I8 change refused a real sealed cluster
    workflow. Check the artifacts, not the reasoning.

    `env_reports/` is gitignored, so on a fresh clone there is nothing to check and
    a green tick here would mean nothing. SKIP, loudly: a skip is visible in the
    run, a vacuous pass is indistinguishable from coverage."""
    if spec_path is None:
        pytest.skip("no sealed workflow artifacts on disk (env_reports/ is gitignored) "
                    "— this ratchet only has force in a tree that has sealed something")
    spec = yaml.safe_load(open(spec_path))
    assert sw.check_workflow_invariants(spec) == [], spec_path


# ---------------------------------------------------------------------------
# The field has to reach the ARTIFACT, not just the draft.
# ---------------------------------------------------------------------------

def test_remote_outputs_survives_into_the_sealed_spec():
    """The `service_dependencies` bug, one layer down, and the reason to check:
    a field the runtime records but the artifact drops leaves the sealed spec's
    I8 re-checking against nothing — it would pass standalone for the wrong
    reason, which is worse than failing."""
    import yaml as _yaml
    from agent.models.core_data import WorkflowSpec
    step = {"step": 1, "tool": "t", "command": "c", "returncode": 0,
            "inputs": [{"path": "/a"}], "detected_outputs": ["/local/x.tsv"],
            "remote_outputs": ["/remote/wf/x.tsv"],
            "cluster_job_verdict": "succeeded", "validation_locus": "cluster",
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 1.0,
                               "peak_cpu_percent": 1.0}}
    spec = WorkflowSpec.model_validate({
        "workflow_name": "w", "description": "d", "created_at": "t",
        "env_request_key": "k", "env_content_digest": "sha256:c",
        "env_image": "i", "pipeline_status": "fully_validated",
        "pipeline_steps": [step]})
    on_disk = _yaml.safe_load(spec.to_yaml())["pipeline_steps"][0]
    assert on_disk["remote_outputs"] == ["/remote/wf/x.tsv"]
    assert on_disk["cluster_job_verdict"] == "succeeded"


def test_both_are_declared_fields_not_extras():
    """Declared, because two invariants read remote_outputs. An undeclared key
    riding on extra='allow' lets a typo (`remote_output`) vanish silently while
    the suite stays green — the shipped_binaries scar."""
    from agent.models.core_data import PipelineStep
    assert "remote_outputs" in PipelineStep.model_fields
    assert "cluster_job_verdict" in PipelineStep.model_fields


def test_a_local_step_carries_no_empty_remote_keys():
    """`outputs: list[str] = []` is the anti-pattern in this very model: a
    default that stamps a meaningless value into every sealed spec. These use
    None so `to_yaml` omits them where they do not apply."""
    import yaml as _yaml
    from agent.models.core_data import WorkflowSpec
    spec = WorkflowSpec.model_validate({
        "workflow_name": "w", "description": "d", "created_at": "t",
        "env_request_key": "k", "env_content_digest": "sha256:c",
        "env_image": "i", "pipeline_status": "fully_validated",
        "pipeline_steps": [{"step": 1, "tool": "t", "command": "c",
                            "returncode": 0, "detected_outputs": ["/x"]}]})
    on_disk = _yaml.safe_load(spec.to_yaml())["pipeline_steps"][0]
    assert "remote_outputs" not in on_disk
    assert "cluster_job_verdict" not in on_disk
