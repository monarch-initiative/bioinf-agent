"""A phenopacket is a per-sample input a step can take and the seal can trace.

`select_test_data(assay_type="phenopacket", accession=<its id>)` records the patient's
JSON under `test_data.phenopacket`, sha256-anchored at selection like reads, so a step
whose input is the phenopacket itself traces at seal (I8) instead of landing as an orphan
with no primitive that would have accepted it. A reads request never lands on one; a
second patient is a further sample of the block.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
import yaml

from agent.models import core_data as cd
from agent.skills.spec_writer import check_workflow_invariants

_PK = {"PMID_1_Patient_A": "Patient A", "PMID_2_Patient_B": "Patient B"}


def _manifest() -> dict:
    return {
        "genome_build": "hg38", "species": "homo_sapiens", "chromosome_subset": "chr22",
        "genome": {"fasta": "genome/chr22.fa", "fai": "genome/chr22.fa.fai", "chromosome": "chr22"},
        "sequencing_data": {"short_read": {"paired_end": {"wgs": [{
            "sample": "NA1", "accession": "SRR1", "read_type": "short_read", "end_type": "paired_end",
            "assay_type": "wgs", "platform": "illumina",
            "subsets": {"10K": {"r1": "short_read/paired_end/wgs/NA1_R1.fastq.gz",
                                "r2": "short_read/paired_end/wgs/NA1_R2.fastq.gz",
                                "num_reads": 10000, "available": True}}}]}}},
        "phenopackets": [{"phenopacket_id": pid, "subject_id": subj, "sex": "FEMALE",
                          "genes": ["ACTB"], "hpo_terms": ["HP:0001873"], "diseases": [],
                          "variants": [], "genome_assembly": "hg38", "available": True,
                          "file": f"phenopackets/{pid}.json"} for pid, subj in _PK.items()],
    }


def _core_tree() -> Path:
    from agent.skills import workspace
    core = workspace.resources_root() / "core_test_data_hg38"
    for end in ("R1", "R2"):
        p = core / "short_read" / "paired_end" / "wgs" / f"NA1_{end}.fastq.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"@NA1_{end}\nACGT\n+\nIIII\n")
    (core / "genome").mkdir(exist_ok=True)
    (core / "genome" / "chr22.fa").write_text(">chr22\nACGTACGTACGT\n")
    (core / "genome" / "chr22.fa.fai").write_text("chr22\t12\t7\t12\t13\n")
    (core / "phenopackets").mkdir(exist_ok=True)
    for pid, subj in _PK.items():
        (core / "phenopackets" / f"{pid}.json").write_text(json.dumps({"id": pid, "subject": {"id": subj}}))
    (core / "manifest.yaml").write_text(yaml.safe_dump(_manifest()))
    return core


@pytest.fixture()
def server(monkeypatch):
    from agent import mcp_server as m
    _core_tree()
    monkeypatch.setattr(m, "config", {})
    return m


def _pid(server) -> str:
    return server.start_pipeline(f"pk_{uuid.uuid4().hex[:8]}", description="x")["pipeline_id"]


def test_a_phenopacket_is_selected_by_its_assay_and_anchored(server):
    pid = _pid(server)
    r = server.select_test_data(genome_build="hg38", assay_type="phenopacket",
                                accession="PMID_1_Patient_A", pipeline_id=pid)
    assert r.get("pipeline_merge", {}).get("status") == "merged", r
    td = server._pipeline_state.get_draft(pid)["test_data"]
    assert td["assay_type"] == "phenopacket"
    assert (td["accession"], td["sample"]) == ("PMID_1_Patient_A", "Patient A")
    assert td["phenopacket"].endswith("phenopackets/PMID_1_Patient_A.json")
    assert td["samples"][0]["phenopacket"] == td["phenopacket"]
    paths = cd.test_data_paths(td)
    assert paths["phenopacket"] == td["phenopacket"]
    anchor = cd.test_data_anchors(td)["phenopacket"]
    assert anchor["sha256"] == hashlib.sha256(Path(td["phenopacket"]).read_bytes()).hexdigest()
    cd.TestDataRef.model_validate(td)


def test_a_reads_request_never_lands_on_a_phenopacket(server):
    r = server.select_test_data(genome_build="hg38")
    assert r["test_data"]["accession"] == "SRR1"
    r = server.select_test_data(genome_build="hg38", accession="PMID_1_Patient_A")
    assert r.get("code") == "data.no_test_data_match", r


def test_a_second_patient_is_a_further_sample(server):
    pid = _pid(server)
    server.select_test_data(genome_build="hg38", assay_type="phenopacket",
                            accession="PMID_1_Patient_A", pipeline_id=pid)
    r = server.select_test_data(genome_build="hg38", assay_type="phenopacket",
                                accession="PMID_2_Patient_B", pipeline_id=pid)
    assert r["pipeline_merge"]["status"] == "added_sample"
    td = server._pipeline_state.get_draft(pid)["test_data"]
    paths = cd.test_data_paths(td)
    assert paths["samples.1.phenopacket"].endswith("PMID_2_Patient_B.json")
    assert "samples.1.phenopacket" in cd.test_data_anchors(td)


def test_a_step_that_takes_the_phenopacket_traces_at_seal(server):
    pid = _pid(server)
    server.select_test_data(genome_build="hg38", assay_type="phenopacket",
                            accession="PMID_1_Patient_A", pipeline_id=pid)
    td = server._pipeline_state.get_draft(pid)["test_data"]
    spec = {"test_data": td, "pipeline_steps": [cd.PipelineStep.produce(
        tool="exomiser", command=f"exomiser --phenopacket {td['phenopacket']}", returncode=0,
        inputs=[td["phenopacket"]], detected_outputs=["/out/results.tsv"],
        validation={"/out/results.tsv": {"passed": True}},
        resource_usage={"wall_seconds": 1.0, "peak_rss_mb": 1.0, "max_cpu_percent": 1.0}) | {"step": 1}]}
    assert [v for v in check_workflow_invariants(spec) if v["invariant"].startswith("I8")] == []
