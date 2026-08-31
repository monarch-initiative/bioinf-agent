"""The core genome reference had a schema slot, a producer, and no way between them.

WHAT WENT WRONG (measured at seal, sea trial S2). A two-step alignment workflow against
the reference this system bootstraps onto the disk itself was REFUSED, twice:

    I8.composition_coherence — pipeline_step 1 input '…/genome/chr22.fa' has no
    producing source … recognized external kinds are authored_artifacts,
    reference_databases, runtime_configs, test_data

The invariant was right — nothing declared the reference, so nothing should have traced.
The hole was upstream, and it was a closed loop with no door:

  * `TEST_DATA_PATH_KEYS` has carried `reference_fasta` / `fasta` / `fai` since I8
    learned about external sources, so a genome declared in `test_data` WOULD trace;
  * `select_test_data` is the SOLE producer of `test_data` (the field is deliberately
    not patchable, so anchors cannot be authored) and it wrote r1, r2 and core_data_dir
    — never a genome key — while the manifest beside it shipped a full `genome:` block;
  * the one patchable alternative disclaimed itself in its own model docstring:
    `ReferenceDatabase` is for data "beyond the genome FASTA", tracked "separately from
    the genome reference".

So alignment-to-a-reference — the most common shape in the field — had no correct field
to declare its reference in, and every such workflow cost a refused seal plus a
hand-authored entry into a class whose model says it is the wrong class. Closed by
giving the slot its producer: `select_test_data` now records the matched dataset's core
genome, anchored like every other path.

The refusal was never wrong and these tests do not weaken it — the counterfactual below
asserts an UNDECLARED genome still fails exactly as it did.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from agent.models import core_data as cd
from agent.skills import resources as res
from agent.skills.spec_writer import check_workflow_invariants


# ---------------------------------------------------------------------------
# a miniature core-test-data tree with REAL files, so the production path runs
# (list_resources -> score -> genome_reference_for -> anchor) with nothing stubbed
# ---------------------------------------------------------------------------

_MANIFEST = {
    "genome_build": "hg38", "species": "homo_sapiens", "chromosome_subset": "chr22",
    "genome": {
        "fasta": "genome/chr22.fa",
        "fai": "genome/chr22.fa.fai",
        "chromosome": "chr22",
        # The manifest also ships aligner index families. They must NOT be pinned — see
        # test_aligner_index_files_are_not_pinned_into_test_data.
        "indexes": {"bwa": ["genome/chr22.fa.amb", "genome/chr22.fa.bwt"]},
    },
    "sequencing_data": {"short_read": {"paired_end": {"wgs": [{
        "sample": "S", "accession": "SRR1", "read_type": "short_read",
        "end_type": "paired_end", "assay_type": "wgs", "platform": "illumina",
        "subsets": {"10K": {"r1": "short_read/paired_end/wgs/S_R1.fastq.gz",
                            "r2": "short_read/paired_end/wgs/S_R2.fastq.gz",
                            "num_reads": 10000, "available": True}}}]}}},
}


def _core_tree(tmp_path: Path, manifest: dict = None) -> tuple[dict, Path]:
    core = tmp_path / "data" / "core_test_data_hg38"
    reads = core / "short_read" / "paired_end" / "wgs"
    reads.mkdir(parents=True)
    (reads / "S_R1.fastq.gz").write_text("@a\nACGT\n+\nIIII\n")
    (reads / "S_R2.fastq.gz").write_text("@a\nTGCA\n+\nIIII\n")
    genome = core / "genome"
    genome.mkdir()
    (genome / "chr22.fa").write_text(">chr22\nACGTACGTACGT\n")
    (genome / "chr22.fa.fai").write_text("chr22\t12\t7\t12\t13\n")
    for idx in ("chr22.fa.amb", "chr22.fa.bwt"):
        (genome / idx).write_text("index-bytes\n")
    (core / "manifest.yaml").write_text(
        yaml.safe_dump(_MANIFEST if manifest is None else manifest))
    return {"paths": {"data_dir": str(tmp_path / "data")}}, core


@pytest.fixture()
def selected(tmp_path, monkeypatch):
    """`(result, core_dir)` from a real `select_test_data` call over that tree."""
    from agent import mcp_server as m
    config, core = _core_tree(tmp_path)
    monkeypatch.setattr(m, "config", config)
    return m.select_test_data(genome_build="hg38", assay_type="wgs"), core


def _align_spec(tmp_path: Path, test_data: dict, fasta: str) -> dict:
    """The shape S2 could not seal: a step whose input is the core genome FASTA.
    Everything else passes, so an `I8.composition_coherence` here is about the
    reference and nothing else."""
    out = tmp_path / "aln.bam"
    out.write_text("BAM\n")
    return {"workflow_name": "wf", "pipeline_steps": [{
        "step": 1, "tool": "bwa", "command": "bwa mem ref reads", "returncode": 0,
        "inputs": [{"path": fasta}, {"path": test_data["r1"]}],
        "detected_outputs": [str(out)],
        "validation": {"aln.bam": {"passed": True, "file": str(out)}},
        "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0,
                           "max_cpu_percent": 5.0},
    }], "test_data": test_data}


def _ids(spec) -> list[str]:
    return [v["invariant"] for v in check_workflow_invariants(spec)]


# ---------------------------------------------------------------------------
# the door
# ---------------------------------------------------------------------------

def test_a_selected_dataset_declares_the_core_genome(selected):
    """The producer half. `reference_fasta` and `fai` were readable by I8 and written by
    nobody; now the sole producer of `test_data` writes them."""
    td = selected[0]["test_data"]
    assert td.get("reference_fasta"), td
    assert td.get("fai"), td
    assert td["reference_fasta"].endswith("genome/chr22.fa")


def test_the_genome_is_anchored_like_every_other_path(selected):
    """A path in `test_data` that nothing pins is a widened I8 universe and no integrity
    claim at all — the exact asymmetry I8.test_data_* was written to end. The genome
    must arrive with the same anchor the reads get."""
    td = selected[0]["test_data"]
    anchors = td.get("content_anchors") or {}
    for key in ("reference_fasta", "fai"):
        assert key in anchors, (f"select_test_data emitted {key} with no content anchor "
                                f"— an input nothing pins")
        assert anchors[key]["kind"] == "file"
        assert anchors[key]["sha256"] == hashlib.sha256(
            Path(td[key]).read_bytes()).hexdigest()


def test_an_aligner_step_consuming_the_core_genome_now_seals(selected, tmp_path):
    """S2's refusal, replayed. This is the assertion the whole fix exists to make true."""
    td = selected[0]["test_data"]
    assert _ids(_align_spec(tmp_path, td, td["reference_fasta"])) == []


def test_an_undeclared_genome_is_still_refused(selected, tmp_path):
    """The counterfactual, so this suite proves a door was opened and not a wall
    knocked down. Strip the genome back out of the block and the same step is the same
    orphan it was before — I8 did not get weaker, it got a producer."""
    td = dict(selected[0]["test_data"])
    fasta = td["reference_fasta"]
    for key in ("reference_fasta", "fai"):
        td.pop(key)
    td["content_anchors"] = {k: v for k, v in (td.get("content_anchors") or {}).items()
                             if k not in ("reference_fasta", "fai")}
    assert "I8.composition_coherence" in _ids(_align_spec(tmp_path, td, fasta))


def test_a_swapped_genome_refuses_the_seal(selected, tmp_path):
    """Anchoring it means something: a reference rewritten between the run and the seal
    is a run validated against different bytes than the spec names."""
    td = selected[0]["test_data"]
    Path(td["reference_fasta"]).write_text(">chr22\nTTTTTTTTTTTT\n")   # same length
    assert "I8.test_data_mutated" in _ids(_align_spec(tmp_path, td, td["reference_fasta"]))


# ---------------------------------------------------------------------------
# THREE states — absence is stated, never recorded and never rounded up
# ---------------------------------------------------------------------------

def test_a_genome_declared_but_missing_is_stated_not_recorded(tmp_path, monkeypatch):
    """The manifest names a FASTA and the bytes are gone. Recording the path anyway
    would refuse the seal of every workflow that never touched a genome, and anchoring
    it is impossible — so nothing is recorded and the caller is TOLD, with the remedy.
    `none_declared` would be a lie here: this core dir does declare a reference."""
    from agent import mcp_server as m
    config, core = _core_tree(tmp_path)
    monkeypatch.setattr(m, "config", config)
    (core / "genome" / "chr22.fa").unlink()

    res_ = m.select_test_data(genome_build="hg38", assay_type="wgs")
    assert res_["genome_reference"]["state"] == res.GENOME_DECLARED_ABSENT
    assert "reference_fasta" not in res_["test_data"]
    assert "reference_fasta" not in (res_["test_data"].get("content_anchors") or {})
    assert "setup_core_test_data" in res_["genome_reference"]["detail"]
    # and the declared-but-absent path is still REPORTED, so the caller can see which
    # file to go and fetch.
    assert res_["genome_reference"]["declared"]["reference_fasta"].endswith("chr22.fa")


def test_a_core_dir_with_no_genome_says_so(tmp_path, monkeypatch):
    """The third state, and a different remedy: nothing to re-fetch, bring your own
    reference. A caller reading only "there is no reference_fasta key" cannot tell this
    from the case above."""
    from agent import mcp_server as m
    no_genome = {k: v for k, v in _MANIFEST.items() if k != "genome"}
    config, _ = _core_tree(tmp_path, manifest=no_genome)
    monkeypatch.setattr(m, "config", config)

    res_ = m.select_test_data(genome_build="hg38", assay_type="wgs")
    assert res_["genome_reference"]["state"] == res.GENOME_NONE_DECLARED
    assert "reference_fasta" not in res_["test_data"]
    assert res_["genome_reference"]["paths"] == {}


def test_the_recorded_state_is_the_one_that_carries_paths(selected):
    """The happy state names itself too — `state` is read, not inferred from whether
    `paths` came back non-empty."""
    g = selected[0]["genome_reference"]
    assert g["state"] == res.GENOME_RECORDED
    assert set(g["paths"]) == {"reference_fasta", "fai"}


# ---------------------------------------------------------------------------
# scope, asserted rather than described
# ---------------------------------------------------------------------------

def test_aligner_index_files_are_not_pinned_into_test_data(selected):
    """The manifest's `indexes:` families are DERIVED artifacts, and pinning them would
    turn a workflow that legitimately re-runs `bwa index` on the core genome into an
    `I8.test_data_mutated` refusal — a false accusation against a step that did the
    right thing. Their prefix is the FASTA path already recorded, so nothing is lost."""
    td = selected[0]["test_data"]
    recorded = set(cd.test_data_paths(td).values())
    assert not [p for p in recorded if p.endswith((".amb", ".bwt", ".ann", ".pac", ".sa"))], \
        f"an aligner index file was pinned as test data: {sorted(recorded)}"


def test_a_genome_block_with_no_fasta_is_not_an_available_genome(tmp_path):
    """`core_dir / ""` is the core dir, which exists — so a `genome:` block with no
    `fasta:` used to yield a genome entry reporting `available: True` for a reference
    that is not there. list_resources is where a reader learns what is on disk."""
    manifest = dict(_MANIFEST)
    manifest["genome"] = {"chromosome": "chr22"}
    config, _ = _core_tree(tmp_path, manifest=manifest)
    assert res.list_resources({"resource_type": "genomes"}, config)["genomes"] == []


def test_the_genome_entry_carries_the_fai_the_producer_needs(tmp_path):
    """`list_resources` is the only reader of the manifest's genome block. It reported
    the FASTA and the index FAMILY NAMES and dropped the `fai` path entirely — so the
    producer downstream could only ever have recorded half the reference."""
    config, _ = _core_tree(tmp_path)
    got = res.list_resources({"resource_type": "genomes"}, config)["genomes"]
    assert len(got) == 1
    assert got[0]["fai"].endswith("genome/chr22.fa.fai")
    assert got[0]["available"] is True


def test_the_reader_is_told_when_the_manifest_declares_no_fai(tmp_path):
    """Absent from the manifest ⇒ None, never a path derived by appending '.fai' to the
    FASTA. A fabricated path that happens to exist would get anchored as if the manifest
    had named it."""
    manifest = dict(_MANIFEST)
    manifest["genome"] = {"fasta": "genome/chr22.fa", "chromosome": "chr22"}
    config, _ = _core_tree(tmp_path, manifest=manifest)
    got = res.list_resources({"resource_type": "genomes"}, config)["genomes"]
    assert got[0]["fai"] is None
    ref = res.genome_reference_for(got[0]["core_dir"], got)
    assert ref["state"] == res.GENOME_RECORDED
    assert set(ref["paths"]) == {"reference_fasta"}
