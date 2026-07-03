"""seal-time derivation of each pipeline step's `depends_on`.

The PipelineStep model documents depends_on as 'derived at finalize from
input/output overlap if absent' — but finalize_pipeline was retired in the
respine and seal never picked the derivation up, so depends_on was ALWAYS empty.
The multi-step chaining probe (row 8) surfaced it: a 2-step pipeline sealed with
step2.depends_on=[] even though its BAM input IS step1's output. `seal_workflow`
now stamps the edge (the same input↔output overlap it already computes to CHECK
I8) via `_derive_step_dependencies`, so the sealed WorkflowSpec is self-DOCUMENTING
about its lineage, not merely self-verifying.
"""
from __future__ import annotations

from agent.mcp_tools.workflow_tools import _derive_step_dependencies


def _chain():
    """The row-8 vehicle: bwa -> (BAM) -> bcftools. step2 consumes step1's output."""
    return [
        {"step": 1, "tool": "bwa",
         "inputs": [{"path": "/g/chr22.fa"}, {"path": "/r/R1.fq"}, {"path": "/r/R2.fq"}],
         "detected_outputs": ["/d/aln.sorted.bam", "/d/aln.sorted.bam.bai"]},
        {"step": 2, "tool": "bcftools",
         "inputs": [{"path": "/d/aln.sorted.bam"}, {"path": "/g/chr22.fa"}],
         "detected_outputs": ["/d/calls.vcf.gz"]},
    ]


def test_chained_step_gets_its_producer():
    out = _derive_step_dependencies(_chain())
    by = {s["step"]: s for s in out}
    assert by[1]["depends_on"] == []          # step1 consumes only external inputs
    assert by[2]["depends_on"] == [1]         # step2's BAM traces to step1


def test_external_only_step_has_no_deps():
    """A step whose every input is external (test_data / reference) gets []."""
    steps = [{"step": 1, "tool": "x",
              "inputs": [{"path": "/ext/in.fq"}], "detected_outputs": ["/d/out.bam"]}]
    assert _derive_step_dependencies(steps)[0]["depends_on"] == []


def test_explicit_depends_on_is_preserved():
    """An already-set depends_on is honored ('if absent' contract) — never overwritten."""
    steps = _chain()
    steps[1]["depends_on"] = [99]
    out = _derive_step_dependencies(steps)
    assert {s["step"]: s for s in out}[2]["depends_on"] == [99]


def test_out_of_order_list_still_resolves_and_preserves_order():
    """Lineage is walked by step NUMBER, but the emitted list keeps caller order."""
    steps = [_chain()[1], _chain()[0]]        # step 2 listed before step 1
    out = _derive_step_dependencies(steps)
    assert [s["step"] for s in out] == [2, 1]          # order preserved
    assert {s["step"]: s for s in out}[2]["depends_on"] == [1]   # still resolves


def test_no_self_dependency():
    """A step whose input path equals its own output must not depend on itself."""
    steps = [{"step": 1, "inputs": [{"path": "/d/x"}], "detected_outputs": ["/d/x"]}]
    assert _derive_step_dependencies(steps)[0]["depends_on"] == []


def test_last_writer_wins_on_overwrite():
    """When two prior steps wrote the same path, the consumer depends on the LATER
    producer (matches _check_lineage_integrity's overwrite semantics)."""
    steps = [
        {"step": 1, "inputs": [{"path": "/ext/a"}], "detected_outputs": ["/d/x.bam"]},
        {"step": 2, "inputs": [{"path": "/ext/b"}], "detected_outputs": ["/d/x.bam"]},  # overwrite
        {"step": 3, "inputs": [{"path": "/d/x.bam"}], "detected_outputs": ["/d/y"]},
    ]
    assert {s["step"]: s for s in _derive_step_dependencies(steps)}[3]["depends_on"] == [2]


def test_non_dict_entries_pass_through():
    out = _derive_step_dependencies([{"step": 1, "inputs": [], "detected_outputs": []}, "junk"])
    assert out[1] == "junk"
    assert out[0]["depends_on"] == []


def test_multiple_producers_in_one_consumer():
    """A step consuming outputs of two different prior steps depends on both."""
    steps = [
        {"step": 1, "inputs": [{"path": "/ext/a"}], "detected_outputs": ["/d/1.bam"]},
        {"step": 2, "inputs": [{"path": "/ext/b"}], "detected_outputs": ["/d/2.vcf"]},
        {"step": 3, "inputs": [{"path": "/d/1.bam"}, {"path": "/d/2.vcf"}],
         "detected_outputs": ["/d/3.txt"]},
    ]
    assert {s["step"]: s for s in _derive_step_dependencies(steps)}[3]["depends_on"] == [1, 2]
