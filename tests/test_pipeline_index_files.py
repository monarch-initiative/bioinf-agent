"""A tool finds an input's index beside it by name (`x.vcf.gz.tbi`, `x.bam.bai`,
`ref.fa.fai`, `ref.dict`). The sealed run read absolute paths with their indexes
next to them; a Nextflow task sees only what is staged into its work directory.

Pinned here: the index files found beside EVERY file a path input read in the sealed
run are recorded on the param or column and staged with it — a row's index rides in
the row's tuple, a shared input's as an extra staged input — while a render with none
stays exactly as it was; index files named differently across rows are not staged and
the notes say so, as they do when the sealed run's files are not on this machine to
look beside; an index the how-to binds as an input of its own is staged once, through
that input; a launch checks every index file at the locus before it submits; and the
rendered main.nf builds its channels under the real binary.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from pipeline_fixtures import probe_spec

from agent.skills import pipeline_record as pr
from agent.skills.pipeline_render import render_pipeline_files

_T = ["bcftools view -r chr22 {VCF} > {OUTPUT_DIR}/{SAMPLE}.vcf",
      "gatk HaplotypeCaller -R {REF} -I {BAM} -O {OUTPUT_DIR}/{SAMPLE}.g.vcf"]


def _spec(data: str, samples=("p1", "p2")):
    def outs(ti, run, s):
        return [f"{run}/{s['SAMPLE']}.vcf"] if ti == 0 else [f"{run}/{s['SAMPLE']}.g.vcf"]
    trials = [(x, {"VCF": f"{data}/{x}.vcf.gz", "BAM": f"{data}/{x}.bam", "REF": f"{data}/chr22.fa",
                   "SAMPLE": x}) for x in samples]
    return probe_spec(_T, trials, [{"name": "OUTPUT_DIR", "files": ["*.vcf"]}],
                      inputs=[{"name": "VCF", "format": "vcf"}, {"name": "BAM", "format": "bam"},
                              {"name": "REF"}, {"name": "SAMPLE"}], step_outputs=outs)


def _observed(spec) -> dict[str, list[str]]:
    """What render_pipeline reads off the disk."""
    from agent.mcp_tools.pipeline_tools import _index_files
    return _index_files(spec)


def _data(root: Path, *, bai=("p1.bam.bai", "p2.bam.bai")) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("p1.vcf.gz", "p1.vcf.gz.tbi", "p2.vcf.gz", "p2.vcf.gz.tbi", "p1.bam", "p2.bam",
                 "chr22.fa", "chr22.fa.fai", "chr22.dict", *bai):
        (root / name).write_text("x")
    return root


def test_indexes_beside_every_file_are_staged_with_it(tmp_path):
    data = _data(tmp_path / "d")
    rec = pr.derive_pipeline_record(_spec(str(data)), name="vc", index_files=_observed(_spec(str(data))))
    assert [(i.strip, i.add) for i in rec.param("REF").index_files] == [("", ".fai"), (".fa", ".dict")]
    cols = {c.name: c for c in rec.samplesheet.columns}
    assert [i.add for i in cols["vcf"].index_files] == [".tbi"] and [i.add for i in cols["bam"].index_files] == [".bai"]
    main = render_pipeline_files(rec)["main.nf"]
    assert "[file(r.vcf + '.tbi', checkIfExists: true)]" in main
    assert "tuple val(meta), path(vcf), path(vcf_index)" in main
    assert "file(params.ref.replaceFirst(/\\.fa$/, '.dict'), checkIfExists: true)" in main
    assert "path ref_index" in main
    assert "gatk HaplotypeCaller -R ${ref} -I ${bam}" in main, "the commands still name the file itself"


def test_a_render_with_no_index_files_is_unchanged(tmp_path):
    data = _data(tmp_path / "d", bai=())
    for ix in ("p1.vcf.gz.tbi", "p2.vcf.gz.tbi", "chr22.fa.fai", "chr22.dict"):
        (data / ix).unlink()
    spec = _spec(str(data))
    found = _observed(spec)
    assert found and not any(found.values()), "the disk was read and held no index"
    none_there = pr.derive_pipeline_record(spec, name="vc", index_files=found)
    unobserved = pr.derive_pipeline_record(spec, name="vc")
    assert render_pipeline_files(none_there)["main.nf"] == render_pipeline_files(unobserved)["main.nf"]
    assert not any("not on this machine" in n for n in none_there.notes)


def test_files_not_on_this_machine_say_their_indexes_were_not_looked_for(tmp_path):
    spec = _spec("/cluster/only")
    rec = pr.derive_pipeline_record(spec, name="vc", index_files=_observed(spec))
    assert any(n.startswith("REF: the sealed run's files are not on this machine") for n in rec.notes)


def test_an_index_the_how_to_binds_itself_is_staged_once(tmp_path):
    """nf-core style: the BAM's index is an input of its own, {BAI}. It is staged
    through that input, never a second time beside the BAM (Nextflow refuses two
    inputs of one name)."""
    data = _data(tmp_path / "d")
    trials = [(x, {"BAM": f"{data}/{x}.bam", "BAI": f"{data}/{x}.bam.bai", "SAMPLE": x}) for x in ("p1", "p2")]
    spec = probe_spec(["samtools view {BAM} -X {BAI} > {OUTPUT_DIR}/{SAMPLE}.sam"], trials,
                      [{"name": "OUTPUT_DIR", "files": ["*.sam"]}],
                      inputs=[{"name": "BAM", "format": "bam"}, {"name": "BAI"}, {"name": "SAMPLE"}],
                      step_outputs=lambda ti, run, s: [f"{run}/{s['SAMPLE']}.sam"])
    rec = pr.derive_pipeline_record(spec, name="sv", index_files=_observed(spec))
    assert {c.name: c for c in rec.samplesheet.columns}["bam"].index_files == []
    assert "tuple val(meta), path(bam), path(bai)" in render_pipeline_files(rec)["main.nf"]


def test_a_launch_checks_the_index_files_before_it_submits(tmp_path):
    from agent.skills import run_production
    data = _data(tmp_path / "d")
    rec = pr.derive_pipeline_record(_spec(str(data)), name="vc", index_files=_observed(_spec(str(data))))
    rows = [{"sample": "p9", "vcf": "/n/p9.vcf.gz", "bam": "/n/p9.bam"}]
    _, _, files = run_production._bound_paths(rec, {"ref": "/n/ref.fa"}, rows)
    assert {"/n/p9.vcf.gz.tbi", "/n/p9.bam.bai", "/n/ref.fa.fai", "/n/ref.dict"} <= set(files)
    with pytest.raises(run_production._Refusal) as e:
        run_production._bound_paths(rec, {"ref": "/n/ref.fasta"}, rows)
    assert e.value.result["code"] == "run_production.index_not_beside" and ".fa" in e.value.result["error"]


def test_indexes_named_differently_across_rows_are_not_staged_and_the_notes_say_so(tmp_path):
    data = _data(tmp_path / "d", bai=("p1.bam.bai", "p2.bai"))
    rec = pr.derive_pipeline_record(_spec(str(data)), name="vc", index_files=_observed(_spec(str(data))))
    assert {c.name: c for c in rec.samplesheet.columns}["bam"].index_files == []
    assert any(n.startswith("BAM: index files sit beside its files, but not under one name") for n in rec.notes)


_RUNTIME = Path(__file__).resolve().parents[1] / ".conda_runtime"
_NEXTFLOW, _JVM = _RUNTIME / "bin" / "nextflow", _RUNTIME / "lib" / "jvm"


@pytest.mark.skipif(not (_NEXTFLOW.is_file() and (_JVM / "bin" / "java").is_file()),
                    reason="needs the runtime env's nextflow")
def test_the_rendered_channels_build_under_the_real_binary(tmp_path):
    data = _data(tmp_path / "d")
    rec = pr.derive_pipeline_record(_spec(str(data)), name="vc", index_files=_observed(_spec(str(data))))
    pipe = tmp_path / "pipe"
    for name, text in render_pipeline_files(rec).items():
        (pipe / name).parent.mkdir(parents=True, exist_ok=True)
        (pipe / name).write_text(text)
    env = dict(os.environ, JAVA_HOME=str(_JVM), JAVA_CMD=str(_JVM / "bin" / "java"),
               NXF_HOME=str(pipe / ".nextflow_home"), NXF_OFFLINE="true", NXF_ANSI_LOG="false")
    run = subprocess.run([str(_NEXTFLOW), "run", "main.nf", "-profile", "local", "-params-file", "params.yaml",
                          "-preview"], cwd=pipe, env=env, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
