"""A step input beneath a registered reference DIRECTORY traces to it (I8).

An aligner index, an annotation folder, the Exomiser data bundle: a reference database
whose local_path is a directory is consumed file by file, and the file a step reads is
an input of that database. Component-wise, so `/ref/db10` is not under `/ref/db1`; scoped
to declared reference directories, never to a prior output's directory (that hole stays
closed), and a file under an undeclared directory is still an orphan.
"""
from __future__ import annotations

from pathlib import Path

from agent.models.core_data import PipelineStep
from agent.skills import spec_writer as sw


def _i8(spec) -> list:
    return [v for v in sw.check_workflow_invariants(spec) if v["invariant"] == "I8.composition_coherence"]


def _step(n: int, inputs: list[str], outputs: list[str]) -> dict:
    return PipelineStep.produce(
        tool="t", command="t", returncode=0, inputs=inputs, detected_outputs=outputs,
        validation={o: {"passed": True} for o in outputs},
        resource_usage={"wall_seconds": 1.0, "peak_rss_mb": 1.0, "max_cpu_percent": 1.0}) | {"step": n}


def _rdb(path: str, **extra) -> dict:
    return {"name": "idx", "version": "1", "local_path": path, **extra}


def test_a_file_beneath_a_reference_directory_traces(tmp_path):
    idx = tmp_path / "hisat2_index_chr22"
    idx.mkdir()
    (idx / "chr22.1.ht2").write_text("x")
    spec = {"reference_databases": [_rdb(str(idx))],
            "pipeline_steps": [_step(1, [str(idx / "chr22.1.ht2")], ["/out/a.bam"]),
                               _step(2, [str(idx / "deeper" / "shard.ht2")], ["/out/b.bam"])]}
    assert _i8(spec) == []


def test_a_sibling_directory_with_the_same_prefix_is_still_an_orphan(tmp_path):
    (tmp_path / "db1").mkdir()
    (tmp_path / "db10").mkdir()
    spec = {"reference_databases": [_rdb(str(tmp_path / "db1"))],
            "pipeline_steps": [_step(1, [str(tmp_path / "db10" / "f.tsv")], ["/out/a"])]}
    assert [v["orphan_path"] for v in _i8(spec)] == [str(tmp_path / "db10" / "f.tsv")]


def test_a_file_under_an_undeclared_directory_is_an_orphan(tmp_path):
    (tmp_path / "ref").mkdir()
    spec = {"pipeline_steps": [_step(1, [str(tmp_path / "ref" / "f.tsv")], ["/out/a"])]}
    assert len(_i8(spec)) == 1


def test_a_reference_file_does_not_make_its_directory_traceable(tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "genes.gtf").write_text("x")
    (ref / "other.gtf").write_text("x")
    spec = {"reference_databases": [_rdb(str(ref / "genes.gtf"))],
            "pipeline_steps": [_step(1, [str(ref / "other.gtf")], ["/out/a"])]}
    assert len(_i8(spec)) == 1, "a declared FILE traces only itself"


def test_a_cluster_locus_directory_traces_by_prefix_without_a_stat():
    spec = {"reference_databases": [_rdb("/cluster/common/exomiser_data", locus="cluster")],
            "pipeline_steps": [_step(1, ["/cluster/common/exomiser_data/2402_hg38/x.mv.db"], ["/out/a"])]}
    assert _i8(spec) == []


def test_the_run_time_disclosure_agrees(tmp_path):
    idx = tmp_path / "idx"
    idx.mkdir()
    spec = {"reference_databases": [_rdb(str(idx))],
            "pipeline_steps": [_step(1, [str(idx / "a.1.ht2")], ["/out/a"])]}
    assert sw.untraced_step_inputs(spec, 1) == []
