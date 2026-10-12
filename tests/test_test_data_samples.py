"""A workflow's test data holds every sample it was validated on.

`select_test_data` records ONE block per workflow with a plural `samples` list. A
second selection of the same kind of data (same genome build and assay) with a new
accession is added to it, its paths anchored under `samples.<i>.r1/r2`; a different
kind of data, the same accession again, or `replace=True` replaces the block. The flat
per-sample fields mirror the first sample, so every reader of the single-sample shape
keeps working, and the seal traces and re-anchors the further samples through the one
path reader, so a multi-sample run no longer has to smuggle its other FASTQs in as
reference databases.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from agent.models import core_data as cd
from agent.skills.pipeline_state import PipelineState
from agent.skills.spec_writer import (TEST_DATA_VERIFIED, check_workflow_invariants,
                                      verify_test_data)


def _entry(sample: str, acc: str, assay: str) -> dict:
    return {"sample": sample, "accession": acc, "read_type": "short_read",
            "end_type": "paired_end", "assay_type": assay, "platform": "illumina",
            "subsets": {"10K": {"r1": f"short_read/paired_end/{assay}/{sample}_R1.fastq.gz",
                                "r2": f"short_read/paired_end/{assay}/{sample}_R2.fastq.gz",
                                "num_reads": 10000, "available": True}}}


_MANIFEST = {
    "genome_build": "hg38", "species": "homo_sapiens", "chromosome_subset": "chr22",
    "genome": {"fasta": "genome/chr22.fa", "fai": "genome/chr22.fa.fai", "chromosome": "chr22"},
    "sequencing_data": {"short_read": {"paired_end": {
        "wgs": [_entry("A", "SRR1", "wgs"), _entry("B", "SRR2", "wgs")],
        "rnaseq": [_entry("C", "SRR3", "rnaseq")],
    }}},
}


def _core_tree() -> Path:
    from agent.skills import workspace
    core = workspace.resources_root() / "core_test_data_hg38"
    for assay, samples in (("wgs", "AB"), ("rnaseq", "C")):
        for smp in samples:
            for end in ("R1", "R2"):
                p = core / "short_read" / "paired_end" / assay / f"{smp}_{end}.fastq.gz"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"@{smp}_{end}\nACGT\n+\nIIII\n")
    (core / "genome").mkdir()
    (core / "genome" / "chr22.fa").write_text(">chr22\nACGTACGTACGT\n")
    (core / "genome" / "chr22.fa.fai").write_text("chr22\t12\t7\t12\t13\n")
    (core / "manifest.yaml").write_text(yaml.safe_dump(_MANIFEST))
    return core


@pytest.fixture()
def server(tmp_path, monkeypatch):
    from agent import mcp_server as m
    _core_tree()
    monkeypatch.setattr(m, "config", {})
    return m


def _pid(server) -> str:
    """A fresh draft per test: the state singleton caches drafts by name."""
    import uuid
    return server.start_pipeline(f"multi_sample_{uuid.uuid4().hex[:8]}", description="x")["pipeline_id"]


def _select(server, pid, acc, assay="wgs", **kw):
    return server.select_test_data(genome_build="hg38", assay_type=assay, accession=acc,
                                   pipeline_id=pid, **kw)


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# the producer
# ---------------------------------------------------------------------------

def test_the_first_selection_records_one_sample_as_the_flat_block_mirrors(server):
    pid = _pid(server)
    r = _select(server, pid, "SRR1")
    assert r["pipeline_merge"]["status"] == "merged"
    td = server._pipeline_state.get_draft(pid)["test_data"]
    assert [s["accession"] for s in td["samples"]] == ["SRR1"]
    assert (td["accession"], td["sample"]) == ("SRR1", "A")
    assert td["samples"][0]["r1"] == td["r1"]
    paths = cd.test_data_paths(td)
    assert "r1" in paths and not any(k.startswith("samples.") for k in paths)


def test_a_second_accession_of_the_same_kind_is_added_and_anchored(server):
    pid = _pid(server)
    _select(server, pid, "SRR1")
    r = _select(server, pid, "SRR2")
    assert r["pipeline_merge"]["status"] == "added_sample"
    td = server._pipeline_state.get_draft(pid)["test_data"]
    assert r["test_data"] == td, "the response is the block as recorded, every sample included"
    assert [s["accession"] for s in td["samples"]] == ["SRR1", "SRR2"]
    assert td["accession"] == "SRR1", "the flat fields still mirror the first sample"
    paths = cd.test_data_paths(td)
    assert paths["samples.1.r1"].endswith("B_R1.fastq.gz")
    assert paths["samples.1.r2"].endswith("B_R2.fastq.gz")
    anchors = cd.test_data_anchors(td)
    for key in ("samples.1.r1", "samples.1.r2"):
        assert anchors[key]["sha256"] == _sha(paths[key]), key
    # the first sample's anchors are untouched
    assert anchors["r1"]["sha256"] == _sha(paths["r1"])


def test_the_same_accession_again_replaces(server):
    pid = _pid(server)
    _select(server, pid, "SRR1")
    r = _select(server, pid, "SRR1")
    assert r["pipeline_merge"]["status"] == "replaced"
    td = server._pipeline_state.get_draft(pid)["test_data"]
    assert [s["accession"] for s in td["samples"]] == ["SRR1"]


def test_a_different_kind_of_data_replaces(server):
    pid = _pid(server)
    _select(server, pid, "SRR1")
    r = _select(server, pid, "SRR3", assay="rnaseq")
    assert r["pipeline_merge"]["status"] == "replaced"
    td = server._pipeline_state.get_draft(pid)["test_data"]
    assert [s["accession"] for s in td["samples"]] == ["SRR3"]
    assert td["assay_type"] == "rnaseq"
    assert not any(k.startswith("samples.") for k in cd.test_data_anchors(td))


def test_replace_true_replaces_a_sample_of_the_same_kind(server):
    pid = _pid(server)
    _select(server, pid, "SRR1")
    r = _select(server, pid, "SRR2", replace=True)
    assert r["pipeline_merge"]["status"] == "replaced"
    td = server._pipeline_state.get_draft(pid)["test_data"]
    assert [s["accession"] for s in td["samples"]] == ["SRR2"]


def test_a_selection_without_a_pipeline_still_returns_the_single_sample_block(server):
    r = server.select_test_data(genome_build="hg38", assay_type="wgs", accession="SRR2")
    assert "pipeline_merge" not in r
    assert [s["accession"] for s in r["test_data"]["samples"]] == ["SRR2"]
    assert r["test_data"]["accession"] == "SRR2"


# ---------------------------------------------------------------------------
# the seal reads every sample
# ---------------------------------------------------------------------------

def _spec(tmp_path: Path, td: dict, inputs: list[str]) -> dict:
    out = tmp_path / "merged.counts"
    out.write_text("counts\n")
    return {"workflow_name": "wf", "pipeline_steps": [{
        "step": 1, "tool": "t", "command": "c", "returncode": 0,
        "inputs": [{"path": p} for p in inputs], "detected_outputs": [str(out)],
        "validation": {"merged.counts": {"passed": True, "file": str(out)}},
        "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0, "max_cpu_percent": 5.0},
    }], "test_data": td}


def test_the_seal_traces_and_re_anchors_the_second_sample(server, tmp_path):
    pid = _pid(server)
    _select(server, pid, "SRR1")
    _select(server, pid, "SRR2")
    td = server._pipeline_state.get_draft(pid)["test_data"]
    paths = cd.test_data_paths(td)
    both = [str(cd.resolve_data_path(paths["r1"])), str(cd.resolve_data_path(paths["samples.1.r1"]))]
    spec = _spec(tmp_path, td, both)
    assert [v["invariant"] for v in check_workflow_invariants(spec)] == []
    r = verify_test_data(spec)
    assert r["status"] == TEST_DATA_VERIFIED
    assert r["checked"] == len(paths) and "samples.1.r1" in paths

    Path(both[1]).write_text("@B_R1\nTTTT\n+\nIIII\n")            # same length, other bases
    ids = [v["invariant"] for v in check_workflow_invariants(spec)]
    assert "I8.test_data_mutated" in ids
    msgs = " ".join(v["message"] for v in check_workflow_invariants(spec))
    assert "samples.1.r1" in msgs


def test_a_step_consuming_an_unselected_fastq_is_still_an_orphan(server, tmp_path):
    """The door opened for the selected samples and no wider."""
    pid = _pid(server)
    _select(server, pid, "SRR1")
    td = server._pipeline_state.get_draft(pid)["test_data"]
    stray = str(cd.resolve_data_path("core_test_data_hg38/short_read/paired_end/wgs/B_R1.fastq.gz"))
    ids = [v["invariant"] for v in check_workflow_invariants(_spec(tmp_path, td, [stray]))]
    assert "I8.composition_coherence" in ids


# ---------------------------------------------------------------------------
# the RUN page names each sample's reads
# ---------------------------------------------------------------------------

def test_the_run_page_labels_each_samples_reads_by_accession(server, tmp_path):
    from agent.skills.run_dashboard_html import render_run_dashboard_html
    pid = _pid(server)
    _select(server, pid, "SRR1")
    _select(server, pid, "SRR2")
    td = server._pipeline_state.get_draft(pid)["test_data"]
    spec = {**_spec(tmp_path, td, []), "created_at": "2026-01-01T00:00:00Z"}
    html = render_run_dashboard_html(spec, env_record=None)
    text = re.sub(r"<[^>]+>", " ", html)
    assert "r1 · sample SRR1" in text and "r2 · sample SRR1" in text
    assert "r1 · sample SRR2" in text and "r2 · sample SRR2" in text
    assert "samples.1.r1" not in text


# ---------------------------------------------------------------------------
# the noun
# ---------------------------------------------------------------------------

def test_the_model_fills_the_flat_fields_from_the_first_sample():
    ref = cd.TestDataRef(genome_build="hg38", samples=[{"accession": "SRR1", "r1": "/a_R1.fq"}])
    assert (ref.accession, ref.r1) == ("SRR1", "/a_R1.fq")


def test_the_model_refuses_a_flat_field_that_contradicts_the_first_sample():
    with pytest.raises(Exception, match="contradicts"):
        cd.TestDataRef(genome_build="hg38", accession="SRR9", samples=[{"accession": "SRR1"}])


def test_a_sample_record_forbids_an_unknown_key():
    with pytest.raises(Exception):
        cd.TestSample(accession="SRR1", reads="/x")


def test_test_data_paths_keys_further_samples_and_not_the_first():
    td = {"genome_build": "hg38", "r1": "/a_R1.fq",
          "samples": [{"accession": "A", "r1": "/a_R1.fq"},
                      {"accession": "B", "r1": "/b_R1.fq", "r2": "/b_R2.fq", "num_reads": 5},
                      {"accession": "C", "r1": "https://x/c.fq"}]}
    assert cd.test_data_paths(td) == {"r1": "/a_R1.fq", "samples.1.r1": "/b_R1.fq",
                                      "samples.1.r2": "/b_R2.fq"}


def test_a_legacy_block_without_samples_reads_as_before():
    td = {"genome_build": "hg38", "r1": "core_test_data_hg38/x_R1.fastq.gz", "fai": "/g.fa.fai"}
    assert cd.test_data_paths(td) == {"r1": "core_test_data_hg38/x_R1.fastq.gz", "fai": "/g.fa.fai"}
    ref = cd.TestDataRef.model_validate(td)
    assert ref.samples == [] and ref.fai == "/g.fa.fai"


# ---------------------------------------------------------------------------
# the write funnel
# ---------------------------------------------------------------------------

def test_the_stored_block_is_whole_and_free_of_unset_fields(tmp_path):
    ps = PipelineState({})
    ps.start("p", "d")
    ps.set_test_data("p", {"genome_build": "hg38", "core_data_dir": "core_test_data_hg38",
                           "samples": [{"accession": "SRR1", "r1": "/a_R1.fq"}],
                           "content_anchors": {"core_data_dir": {"kind": "directory", "sha256": None,
                                                                 "size_bytes": None}}})
    td = ps.get_draft("p")["test_data"]
    # an anchor's `sha256: None` is a statement and survives the write whole
    assert td["content_anchors"]["core_data_dir"] == {"kind": "directory", "sha256": None, "size_bytes": None}
    assert td["samples"] == [{"accession": "SRR1", "r1": "/a_R1.fq"}]
    assert (td["accession"], td["r1"]) == ("SRR1", "/a_R1.fq")
    for absent in ("r2", "subset", "upstream_pipelines", "chromosome_subset"):
        assert absent not in td, absent


def test_a_contradicting_block_never_reaches_the_draft(tmp_path):
    ps = PipelineState({})
    ps.start("p", "d")
    with pytest.raises(Exception, match="contradicts"):
        ps.set_test_data("p", {"genome_build": "hg38", "accession": "SRR9",
                               "samples": [{"accession": "SRR1"}]})
    assert "test_data" not in ps.get_draft("p")
