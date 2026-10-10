"""The Nextflow form is rendered from the pipeline record and nothing else: every
file is pinned here as the literal text a human reads, copies and re-runs.

The directory offers ONE way to run — Nextflow over samples.csv — so there is no
second, by-hand form to keep in step with it. A one-trial seal is a one-row
samplesheet rendered exactly like a three-row one; per-sample inputs are columns,
never params. params.yaml holds the pipeline's parameters and nothing about WHERE it
runs: each profile in nextflow.config names its own image — the docker tag locally,
the .sif on the cluster — and main.nf refuses to start a cluster run whose container
is still empty. The last class runs the rendered directory through the runtime env's
real nextflow binary, so the files are known to parse and the guard is known to
fire, not merely to read well."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from pipeline_fixtures import DIGEST, GTF, INDEX, REQUEST_KEY, TEMPLATES, sealed_rnaseq_spec

from agent.skills import pipeline_record as pr
from agent.skills.pipeline_render_nextflow import (RUN_HPC, RUN_HPC_NEXTFLOW, RUN_LOCAL, RUN_RECORD_BLOCK,
                                                   RUN_RECORD_CONFIG, RUN_STAMP_LINE,
                                                   RUN_RECORD_FILES, RUN_RECORDS, STRICT_MODE_LINE, TRACE_FIELDS,
                                                   bound_commands, render_nextflow, run_lines)

#: A compute env block the way projects_access.yaml declares one: SLURM policy,
#: notification email, the Lmod names the launcher loads. Names nothing real.
ENV = {
    "name": "cluster", "type": "ssh", "host": "login.example.org", "user": "someone",
    "email": "someone@example.org",
    "slurm": {"account": "acct", "partition": "cpu",
              "gpu": {"partition": "gpu", "qos": "gpu_access"}},
    "apptainer_module": "apptainer/1.5.0", "nextflow_module": "nextflow/25.04.7",
}
SIZED = {"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}}
GPU = {"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00", "gpus": 1}}
ALL_SIZED = {"HISAT2": {"cpus": 8, "mem": "32G", "time": "1-12:30:00"},
             "SAMTOOLS": {"cpus": 1, "mem": "12000M", "time": "0:20:00"},
             "HTSEQ_COUNT": {"cpus": 2, "mem": "4G", "time": "0:00:45"}}

#: The one file set, whatever the record: no shape has a fourth or a sixth file.
FILES = {"main.nf", "nextflow.config", "params.yaml", "samples.csv", "launcher.sh"}
#: Where stage_apptainer_image put the fixture's image on a cluster.
SIF = "/cluster/containers/rnaseq_cli_48ac8c5b25d2.sif"
#: A second image, for the multi-env chain: the fixture's last stage moved into it.
OTHER = "sha256:bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"
OTHER_IMAGE = "bioinf_counts:latest"

ALIGN_LINE = ("hisat2 -p 4 -x ${file(params.hisat2_index).name} -U ${reads} "
              "| samtools sort -o aligned.bam")
INDEX_LINE = "samtools index aligned.bam"
COUNT_LINE = ("htseq-count -s ${params.stranded} -f bam aligned.bam ${gtf} "
              "> ${meta.sample}.counts.tsv")

#: The fail-fast guard that opens every workflow block: one image reads the resolved
#: container as a string; several read it as a map that holds ONLY the processes whose
#: container is set, so every stage is looked up by name.
GUARD_ONE = "    if (workflow.profile.tokenize(',').contains('slurm') && !workflow.container)\n"
GUARD_MANY = ("    if (workflow.profile.tokenize(',').contains('slurm') && "
              "!(workflow.container instanceof Map ? ['HISAT2', 'SAMTOOLS', 'HTSEQ_COUNT'].every "
              "{ workflow.container[it] } : workflow.container))\n")


def _guard_error(*digests: str) -> str:
    return ('        error "nextflow.config, profile slurm: process.container is empty; set it to '
            f'the .sif built from image {", ".join(digests)}"\n')


def _set_me(digest: str, indent: int) -> str:
    """The slurm profile's container lines for an image whose .sif is not known."""
    pad = " " * indent
    return (f"{pad}// SET ME: the .sif built from image {digest}. Render with env= naming the cluster,\n"
            f"{pad}// or paste the path stage_apptainer_image reports.\n"
            f"{pad}container = ''\n")


def _sif_set(digest: str, sif: str, indent: int, where: str = "") -> str:
    """The slurm profile's container lines for an image whose .sif PATH the record
    carries but which nothing has staged yet: the path is a prediction, said so."""
    pad = " " * indent
    return (f"{pad}// the .sif built from image {digest}: the path stage_apptainer_image writes{where}.\n"
            f"{pad}// Not staged yet — run stage_apptainer_image before sbatch launcher.sh.\n"
            f"{pad}container = '{sif}'\n")


def _sif_staged(digest: str, sif: str, sha: str, indent: int, where: str = "") -> str:
    """The slurm profile's container lines for an image whose .sif WAS staged: the
    record carries the staged file's sha256, so the comment states it as a fact."""
    pad = " " * indent
    return (f"{pad}// the .sif built from image {digest}, staged{where} by stage_apptainer_image (sha256 {sha})\n"
            f"{pad}container = '{sif}'\n")


def _record(**kw):
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", **kw)


def _one_row(**kw):
    """A seal that proved ONE trial: a one-row samplesheet, not a different pipeline."""
    return pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one", **kw)


def _threads(**kw):
    """The how-to with its aligner's thread count declared as a slot (`format: threads`)."""
    return pr.derive_pipeline_record(sealed_rnaseq_spec(threads=True), name="rnaseq_counts", **kw)


def _two_images(rec):
    """The fixture's chain with its last stage in a second image — the shape a
    how-to that spans two frozen envs renders as."""
    rec.stages[2].image, rec.stages[2].image_digest = OTHER_IMAGE, OTHER
    return rec


def _process_block(main_nf: str, name: str) -> str:
    """The text of ONE process, `process NAME {` through its closing brace."""
    m = re.search(rf"^process {name} \{{\n(.*?)^\}}", main_nf, re.S | re.M)
    assert m, f"no process {name} in main.nf"
    return m.group(1)


def _script_lines(main_nf: str, name: str) -> list[str]:
    """The lines inside a process's script block, their indent stripped: what the
    compute node runs."""
    m = re.search(r'    script:\n    """\n(.*?)    """', _process_block(main_nf, name), re.S)
    assert m, f"no script block in process {name}"
    return [ln[4:] for ln in m.group(1).splitlines()]


def _workflow_block(main_nf: str) -> str:
    m = re.search(r"^workflow \{\n(.*?)^\}", main_nf, re.S | re.M)
    assert m, "no workflow block in main.nf"
    return m.group(1)


def _profile(cfg: str, name: str) -> str:
    """The text of ONE profile of nextflow.config, `    local {` through the closing
    brace at its own indent."""
    m = re.search(rf"^    {name} \{{\n(.*?)^    \}}", cfg, re.S | re.M)
    assert m, f"no profile {name} in nextflow.config"
    return m.group(1)


def _without_stage_comments(main_nf: str) -> str:
    """main.nf minus the two comment lines above each process that quote what the
    seal observed — which steps back the stage and what they measured."""
    return "\n".join(ln for ln in main_nf.splitlines()
                     if not ln.startswith("// stage ") and not ln.startswith("// measured"))


# ===========================================================================
# The file set
# ===========================================================================


class TestFileSet:
    @pytest.mark.parametrize("rows", ["three", "one"])
    @pytest.mark.parametrize("with_env", [True, False])
    def test_the_render_is_always_the_same_five_files(self, rows, with_env):
        """One way to run, one file set. A one-trial seal renders exactly the files a
        three-trial one does — there is no by-hand form and no four-file shape."""
        rec = _record() if rows == "three" else _one_row()
        assert set(render_nextflow(rec, env=ENV if with_env else None)) == FILES

    def test_every_file_opens_by_naming_the_sealed_workflow_and_what_to_edit_instead(self):
        files = render_nextflow(_record(), env=ENV)
        for name, text in files.items():
            if name == "samples.csv":
                continue                      # a comment line would be read as the CSV header
            first = text.splitlines()[1] if text.startswith("#!") else text.splitlines()[0]
            assert "rendered from sealed workflow rnaseq_counts_workflow" in first, name
            if name == "params.yaml":         # the file the reader IS meant to edit
                assert "edit this file and samples.csv, not main.nf" in first
            else:
                assert "edit params.yaml / samples.csv, not this file" in first, name

    def test_samples_csv_is_the_records_one_samplesheet_rendering_keyed_by_sample(self):
        """`sample` is the row key: the how-to's own {SAMPLE} placeholder is bound to
        that column, so the row's name tags its tasks and names its outputs."""
        rec = _record()
        files = render_nextflow(rec)
        assert files["samples.csv"] == pr.render_samplesheet(rec)
        assert files["samples.csv"].splitlines() == [
            "sample,reads",
            "SRR1039508,/data/reads/SRR1039508_10K_R1.fastq.gz",
            "SRR1039509,/data/reads/SRR1039509_10K_R1.fastq.gz",
            "SRR1039512,/data/reads/SRR1039512_10K_R1.fastq.gz"]
        key = rec.samplesheet.columns[0]
        assert (key.name, key.placeholder) == ("sample", "SAMPLE")
        assert "row key" in key.description
        assert "${meta.sample}.counts.tsv" in files["main.nf"]

    @pytest.mark.parametrize("rows", ["three", "one"])
    @pytest.mark.parametrize("with_env", [True, False])
    def test_no_placeholder_survives_in_any_rendered_file(self, rows, with_env):
        rec = _record(resources=GPU) if rows == "three" else _one_row(resources=GPU)
        files = render_nextflow(rec, env=ENV if with_env else None)
        for name, text in files.items():
            assert pr.placeholders(text) == [], f"{name} still carries a placeholder"

    @pytest.mark.parametrize("rows", ["three", "one"])
    def test_every_stage_name_appears_exactly_once_as_a_process(self, rows):
        rec = _record() if rows == "three" else _one_row()
        main = render_nextflow(rec)["main.nf"]
        assert re.findall(r"^process (\w+) \{$", main, re.M) == ["HISAT2", "SAMTOOLS", "HTSEQ_COUNT"]
        assert [s.name for s in rec.stages] == ["HISAT2", "SAMTOOLS", "HTSEQ_COUNT"]

    def test_the_render_is_pure_deterministic_and_leaves_the_record_untouched(self):
        rec = _record(resources=GPU)
        before = rec.model_dump()
        first = render_nextflow(rec, env=ENV)
        second = render_nextflow(rec, env=ENV)
        assert first == second
        assert rec.model_dump() == before


# ===========================================================================
# A one-trial seal is a one-row samplesheet
# ===========================================================================


class TestOneRow:
    """`sealed_rnaseq_spec(["SRR1039508"])` proved ONE trial. That is a one-row
    samplesheet, not a different pipeline: the same processes over the same meta
    tuple, the same config, params and launcher, and per-sample inputs that stay
    columns with no default — the seal's one row IS the worked example."""

    def test_a_one_trial_seal_renders_like_a_three_trial_one_but_for_the_rows(self):
        three = render_nextflow(_record())
        one = render_nextflow(pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]),
                                                        name="rnaseq_counts"))
        assert one["samples.csv"].splitlines() == three["samples.csv"].splitlines()[:2]
        for name in ("nextflow.config", "params.yaml", "launcher.sh"):
            assert one[name] == three[name], name
        # main.nf differs only in what the seal observed: which steps back each stage
        # and what they measured
        assert _without_stage_comments(one["main.nf"]) == _without_stage_comments(three["main.nf"])
        assert "derived from sealed step(s) [2]\n" in one["main.nf"]
        assert "measured on 1 sealed step on the workflow's test data" in one["main.nf"]

    def test_per_sample_inputs_are_columns_with_no_default_never_params(self):
        rec = _one_row()
        assert {p.name: p.default for p in rec.params if p.kind == "per_sample"} == {
            "READS": None, "SAMPLE": None}
        main = render_nextflow(rec)["main.nf"]
        assert "params.reads" not in main and not re.search(r"params\.sample\b", main)
        assert "REQUIRED" not in main and "= null" not in main
        assert "tuple val(meta), path(reads)" in _process_block(main, "HISAT2")
        assert "tag { meta.sample }" in main and ".splitCsv(header: true)" in main
        assert "params.samplesheet = 'samples.csv'" in main


# ===========================================================================
# main.nf
# ===========================================================================


class TestMainNf:
    def test_the_header_counts_stages_and_commands_run_once_per_row(self):
        main = render_nextflow(_record())["main.nf"]
        assert main.splitlines()[1] == (
            "// 3 stage(s) over 3 how-to command(s), each stage run once per samples.csv row; every "
            "process runs its sealed command(s) with the placeholders bound.")
        merged = render_nextflow(_record(stages=[[0, 1], [2]], stage_names=["ALIGN", "COUNT"]))["main.nf"]
        assert merged.splitlines()[1].startswith("// 2 stage(s) over 3 how-to command(s), ")

    def test_dsl2_and_the_shared_params_with_their_sealed_defaults(self):
        main = render_nextflow(_record())["main.nf"]
        assert "nextflow.enable.dsl = 2" in main
        assert f"params.hisat2_index = '{INDEX}'" in main
        assert "params.stranded = 'reverse'" in main
        assert f"params.gtf = '{GTF}'" in main
        assert "params.samplesheet = 'samples.csv'" in main
        assert "params.outdir = 'results'" in main
        assert "run_id" not in main and "params.sif" not in main
        # per-sample inputs are samplesheet columns, never params
        assert "params.reads" not in main and "params.sample " not in main
        assert "${meta.sample}" in main

    def test_every_param_says_where_it_came_from_and_that_the_default_was_validated(self):
        main = render_nextflow(_record())["main.nf"]
        idx = main.index("params.hisat2_index =")
        preamble = main[:idx].splitlines()[-2:]
        assert "HISAT2_INDEX — a prefix naming a family of files; produced by sealed step 1 (hisat2-build)" \
            in preamble[0]
        assert preamble[1] == "// The default is what the sealed run was validated with."
        assert "// GTF — a path; a how-to input; format gtf; gene annotation; used by HTSEQ_COUNT." in main

    def test_an_integer_default_is_a_number_and_a_yaml_looking_word_stays_a_string(self):
        two = pr.derive_pipeline_record(sealed_rnaseq_spec(stranded="2"), name="n")
        files = render_nextflow(two)
        assert "params.stranded = 2" in files["main.nf"]
        assert "\nstranded: 2\n" in files["params.yaml"]
        yes = pr.derive_pipeline_record(sealed_rnaseq_spec(stranded="yes"), name="y")
        files = render_nextflow(yes)
        assert "params.stranded = 'yes'" in files["main.nf"]
        assert "\nstranded: 'yes'\n" in files["params.yaml"]   # unquoted, YAML reads a boolean

    def test_the_script_blocks_are_the_sealed_commands_with_placeholders_bound(self):
        main = render_nextflow(_record())["main.nf"]
        assert f"    {ALIGN_LINE}\n" in _process_block(main, "HISAT2")
        assert f"    {INDEX_LINE}\n" in _process_block(main, "SAMTOOLS")
        assert f"    {COUNT_LINE}\n" in _process_block(main, "HTSEQ_COUNT")
        for name in ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT"):
            block = _process_block(main, name)
            assert '    script:\n    """\n' in block and block.rstrip().endswith('"""')

    def test_processes_carry_no_container_line_the_config_owns_containers(self):
        main = render_nextflow(_record(), env=ENV)["main.nf"]
        for name in ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT"):
            assert "container" not in _process_block(main, name)

    def test_stage_comments_name_index_tool_image_sealed_steps_and_the_measurement(self):
        main = render_nextflow(_record())["main.nf"]
        assert (f"// stage 1 of 3 · hisat2 · image {DIGEST} · derived from sealed step(s) [2, 5, 8]\n"
                "// measured on 3 sealed steps on the workflow's test data: wall 68 s · peak RSS "
                "1570 MB · max CPU 100% (authoritative) — a measurement, never the request\n"
                "process HISAT2 {") in main
        assert f"// stage 3 of 3 · htseq-count · image {DIGEST} · derived from sealed step(s) [4, 7, 10]" in main
        # a stage in a second image names ITS image, not the chain's first
        main = render_nextflow(_two_images(_record()))["main.nf"]
        assert f"// stage 3 of 3 · htseq-count · image {OTHER} · derived from sealed step(s) [4, 7, 10]" in main

    def test_tag_and_every_stage_publishes_what_it_writes_into_the_rows_directory(self):
        main = render_nextflow(_record())["main.nf"]
        for name in ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT"):
            block = _process_block(main, name)
            assert "tag { meta.sample }" in block
            assert 'publishDir { "${params.outdir}/${meta.sample}" }, mode: \'copy\', overwrite: true' in block
        assert "pattern:" not in main                     # everything a stage writes is published
        assert "stageInMode" not in main                  # no stage rewrites what it consumed

    def test_inputs_in_the_fixed_order_meta_tuple_then_shared_paths_then_prefix_families(self):
        main = render_nextflow(_record())["main.nf"]
        assert ("    input:\n"
                "    tuple val(meta), path(reads)\n"
                "    path hisat2_index_files\n") in _process_block(main, "HISAT2")
        assert ("    input:\n"
                "    tuple val(meta), path('aligned.bam')\n") in _process_block(main, "SAMTOOLS")
        assert ("    input:\n"
                "    tuple val(meta), path('aligned.bam'), path('aligned.bam.bai')\n"
                "    path gtf\n") in _process_block(main, "HTSEQ_COUNT")

    def test_outputs_emit_one_channel_per_artifact_named_by_its_literal_parts(self):
        main = render_nextflow(_record())["main.nf"]
        assert ("    output:\n"
                "    tuple val(meta), path('aligned.bam'), emit: aligned_bam\n") in _process_block(main, "HISAT2")
        assert ("    output:\n"
                "    tuple val(meta), path('aligned.bam.bai'), emit: aligned_bam_bai\n") \
            in _process_block(main, "SAMTOOLS")
        assert ("    output:\n"
                '    tuple val(meta), path("${meta.sample}.counts.tsv"), emit: counts_tsv\n') \
            in _process_block(main, "HTSEQ_COUNT")

    def test_the_rows_channel_and_the_calls_in_stage_order(self):
        wf = _workflow_block(render_nextflow(_record())["main.nf"])
        assert ("    rows = channel.fromPath(params.samplesheet, checkIfExists: true)\n"
                "        .splitCsv(header: true)\n"
                "        .map { r -> tuple([sample: r.sample], file(r.reads, checkIfExists: true)) }"
                "   // one (meta, reads) per samples.csv row\n") in wf
        # files(), not file(): a glob that matches a family of files is a collection, and
        # Nextflow warns on file() for one
        assert '    HISAT2(rows, files("${params.hisat2_index}*"))\n' in wf
        assert "    SAMTOOLS(HISAT2.out.aligned_bam)\n" in wf
        assert ("    HTSEQ_COUNT(HISAT2.out.aligned_bam.join(SAMTOOLS.out.aligned_bam_bai), file(params.gtf))"
                "   // .join() on meta: (meta, aligned.bam) + (meta, aligned.bam.bai) "
                "-> (meta, aligned.bam, aligned.bam.bai)\n") in wf
        order = [wf.index(f"    {n}(") for n in ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT")]
        assert order == sorted(order)

    def test_the_workflow_refuses_to_start_on_the_cluster_without_a_container(self):
        """The guard reads the container Nextflow resolved from nextflow.config — never
        a params entry — so the one place a .sif is named is the one place it is
        checked. It opens the workflow block, before any channel exists."""
        main = render_nextflow(_record())["main.nf"]
        wf = _workflow_block(main)
        assert wf.startswith(GUARD_ONE + _guard_error(DIGEST))
        assert "params.sif" not in main

    def test_with_several_images_the_guard_checks_every_stages_container_and_names_every_digest(self):
        main = render_nextflow(_two_images(_record()))["main.nf"]
        wf = _workflow_block(main)
        assert wf.startswith(GUARD_MANY + _guard_error(DIGEST, OTHER))
        assert "params.sif" not in main

    def test_a_stage_that_rewrites_what_it_consumed_stages_a_copy(self):
        rec = _record()
        rec.stages[1].stage_in_copy = True
        assert "stageInMode 'copy'" in _process_block(render_nextflow(rec)["main.nf"], "SAMTOOLS")

    def test_a_merged_stage_publishes_both_its_artifacts(self):
        rec = _record(stages=[[0, 1], [2]], stage_names=["ALIGN", "COUNT"])
        main = render_nextflow(rec)["main.nf"]
        align = _process_block(main, "ALIGN")
        assert 'publishDir { "${params.outdir}/${meta.sample}" }, mode: \'copy\', overwrite: true' in align
        assert "pattern:" not in align
        assert ("    output:\n"
                "    tuple val(meta), path('aligned.bam'), emit: aligned_bam\n"
                "    tuple val(meta), path('aligned.bam.bai'), emit: aligned_bam_bai\n") in align
        wf = _workflow_block(main)
        assert "    COUNT(ALIGN.out.aligned_bam.join(ALIGN.out.aligned_bam_bai), file(params.gtf))" in wf


# ===========================================================================
# main.nf — the launch record
# ===========================================================================


LAUNCH_RECORD = (
    "    // runs/<stamp>/: every param as resolved and the samplesheet as read, written before\n"
    "    // any task runs; nextflow.config writes trace.txt, report.html and timeline.html there.\n"
    "    // The stamp is left out of params.json so the file re-runs as -params-file.\n"
    '    run_dir = file("runs/${params.run_stamp}")\n'
    "    run_dir.mkdirs()\n"
    "    file(params.samplesheet, checkIfExists: true).copyTo(run_dir.resolve('samples.csv'))\n"
    "    run_dir.resolve('params.json').text = groovy.json.JsonOutput.prettyPrint("
    "groovy.json.JsonOutput.toJson(params.findAll { k, v -> k != 'run_stamp' }))\n"
    '    log.info "run records: runs/${params.run_stamp}/\\n"\n'   # the newline keeps the banner off this line
    "\n")


class TestLaunchRecord:
    """The workflow opens by writing what the run was given — every param as resolved
    and the samplesheet as read — into runs/<stamp>/ before any task runs, so a run
    killed hard still has them. One block at the top of `workflow {}` after the
    container guard: no function, no import (the strict syntax refuses them), and no
    literal that could go stale — Nextflow's own history and report carry the launch
    line. The stamp stays out of params.json so the file re-runs as -params-file."""

    def test_the_block_opens_the_workflow_after_the_guard_and_before_the_rows_channel(self):
        wf = _workflow_block(render_nextflow(_record())["main.nf"])
        assert wf.startswith(GUARD_ONE + _guard_error(DIGEST) + LAUNCH_RECORD
                             + "    rows = channel.fromPath(params.samplesheet, checkIfExists: true)\n")

    def test_the_block_is_the_whole_record_no_function_no_import_no_literal(self):
        for make in (_record, lambda: _two_images(_record()), lambda: _record(compute_env="cluster")):
            main = render_nextflow(make())["main.nf"]
            assert main.count(LAUNCH_RECORD) == 1, make
            for gone in ("record_launch", "import ", "run.json", "workflow.commandLine", "sealed_workflow",
                         "pipeline: [", "slurm_job_id", "Channel."):
                assert gone not in main, gone

    def test_each_run_record_file_is_defined_where_the_list_says(self):
        files = render_nextflow(_record())
        wf = _workflow_block(files["main.nf"])
        launcher = render_nextflow(_record(), env=ENV)["launcher.sh"]
        for name, _, defined_in in RUN_RECORDS:
            by_block = f"'{name}'" in wf                                         # the block writes it
            by_observer = f'file = "runs/${{params.run_stamp}}/{name}"' in files["nextflow.config"]
            by_launcher = f'-log "runs/$RUN_STAMP/{name}"' in launcher
            assert (by_block, by_observer, by_launcher) == (
                defined_in == "params.yaml", defined_in == "nextflow.config", defined_in == "launcher.sh"), name
        assert RUN_RECORD_FILES == tuple(n for n, _, _ in RUN_RECORDS) == (
            "params.json", "samples.csv", "trace.txt", "report.html", "timeline.html", "nextflow.log")
        assert [d for _, _, d in RUN_RECORDS] == ["params.yaml", "params.yaml", "nextflow.config", "nextflow.config",
                                                  "nextflow.config", "launcher.sh"]

    def test_the_two_blocks_are_one_standard_verbatim_in_every_rendered_pipeline(self):
        assert RUN_RECORD_BLOCK + "\n" == LAUNCH_RECORD                  # the blank line after it is the join's
        for make in (_record, lambda: _two_images(_record()), lambda: _record(compute_env="cluster"), _threads):
            files = render_nextflow(make())
            assert files["nextflow.config"].endswith(RUN_RECORD_CONFIG + "\n"), make
            assert files["main.nf"].count(RUN_RECORD_BLOCK) == 1, make
        assert RUN_RECORD_CONFIG.count("runs/${params.run_stamp}/") == 3 and TRACE_FIELDS in RUN_RECORD_CONFIG

    def test_the_launcher_comment_names_the_run_records(self):
        sh = render_nextflow(_record(), env=ENV)["launcher.sh"]
        assert ("# fresh run. Each run leaves its own runs/<timestamp>/ (params.json, samples.csv, trace.txt,\n"
                "# report.html, timeline.html, nextflow.log), never overwritten; this job's .out file is\n"
                "# the manager's log.\n") in sh

    def test_the_launcher_says_what_the_dollar_at_forwards_right_above_the_line_that_uses_it(self):
        sh = render_nextflow(_record(), env=ENV)["launcher.sh"]
        assert ('# "$@" forwards whatever follows launcher.sh on the sbatch line to Nextflow, so a value for\n'
                "# this run only goes there and wins over params.yaml:  sbatch launcher.sh --<param> <value>\n"
                + RUN_HPC_NEXTFLOW + "\n") in sh

    def test_the_launcher_takes_the_run_stamp_itself_and_points_nextflows_log_into_the_run_dir(self):
        """`-log` is read before nextflow.config, so the config's stamp comes too late for
        it: the launcher takes the stamp, names the log with it, and hands it on as
        --run_stamp so params.json, the samplesheet copy and the three observers land in
        the same runs/<stamp>/. The caller's arguments still come last."""
        sh = render_nextflow(_record(), env=ENV)["launcher.sh"]
        assert RUN_STAMP_LINE == "RUN_STAMP=$(date +%Y%m%d_%H%M%S)"
        assert RUN_HPC_NEXTFLOW == ('nextflow -log "runs/$RUN_STAMP/nextflow.log" run main.nf -profile slurm '
                                    '-params-file params.yaml -resume --run_stamp "$RUN_STAMP" "$@"')
        assert sh.index(RUN_STAMP_LINE) < sh.index(RUN_HPC_NEXTFLOW)
        assert ("# One directory per run, runs/<stamp>/. The stamp is taken here rather than in\n"
                "# nextflow.config so Nextflow's own log can join the run records: -log is read before\n"
                "# anything else, and --run_stamp hands the same stamp to nextflow.config and main.nf.\n"
                + RUN_STAMP_LINE + "\n") in sh
        assert "params.run_stamp = new java.util.Date()" in render_nextflow(_record())["nextflow.config"]  # laptop default


# ===========================================================================
# nextflow.config — each profile names the image it runs
# ===========================================================================


class TestConfig:
    def test_the_two_profiles(self):
        cfg = render_nextflow(_record(), env=ENV)["nextflow.config"]
        assert "run_id" not in cfg and "params.sif" not in cfg
        assert ("profiles {\n"
                "    local {\n"
                "        docker.enabled = true\n"
                "        process {\n"
                "            executor = 'local'\n"
                f"            // the frozen image {DIGEST}, as docker names it here\n"
                "            container = 'bioinf_rnaseq_cli:latest'\n"
                "        }\n"
                "    }\n"
                "    slurm {\n"
                "        apptainer.enabled = true\n"
                "        apptainer.autoMounts = true\n"
                "        apptainer.runOptions = '--cleanenv'") in cfg
        assert ("        // Throughput: at most 500 jobs queued or running at once, submitted at no\n"
                "        // more than 100 a minute — one pipeline never floods the scheduler or pins a user's\n"
                "        // whole job allowance. Raise or lower them here, per pipeline.\n"
                "        executor.queueSize = 500\n"
                "        executor.submitRateLimit = '100/1min'\n"
                "        // work/ lives beside main.nf. It is the heavy directory: point it at scratch when\n"
                "        // this filesystem is quota-bound.\n"
                "        // workDir = '/path/on/scratch/rnaseq_counts/work'\n") in cfg
        assert (pr.NEXTFLOW_QUEUE_SIZE, pr.NEXTFLOW_SUBMIT_RATE) == (500, "100/1min")   # the comment spells these
        assert "workDir =" not in _profile(cfg, "local") and cfg.count("workDir") == 1   # a hint, not a setting
        assert ("            executor = 'slurm'\n"
                + _set_me(DIGEST, 12) +
                "            cache = 'lenient'\n"
                "            beforeScript = 'module load apptainer/1.5.0'\n"
                "            queue = 'cpu'\n"
                "            clusterOptions = '--account=acct'\n") in cfg

    def test_without_a_sif_the_slurm_container_is_empty_and_says_set_me(self):
        """An empty container is stated, with the digest the .sif must be built from and
        the two ways to fill it; nothing else in the directory carries a sif slot."""
        files = render_nextflow(_record())
        cfg = files["nextflow.config"]
        assert _set_me(DIGEST, 12) in _profile(cfg, "slurm")
        assert "container = ''" not in _profile(cfg, "local")
        assert not any("params.sif" in text or "sif_1" in text for text in files.values())

    def test_the_slurm_container_is_the_sif_when_the_record_carries_one(self):
        rec = _record(sif_paths={REQUEST_KEY: SIF}, compute_env="cluster")
        files = render_nextflow(rec, env=ENV)
        cfg = files["nextflow.config"]
        assert ("            executor = 'slurm'\n"
                + _sif_set(DIGEST, SIF, 12, " on cluster") +
                "            cache = 'lenient'\n") in _profile(cfg, "slurm")
        assert "SET ME" not in cfg and "container = ''" not in cfg
        # the local profile still runs the docker tag; the .sif reaches no other file
        local = _profile(cfg, "local")
        assert "container = 'bioinf_rnaseq_cli:latest'" in local and SIF not in local
        assert SIF not in files["params.yaml"] and SIF not in files["main.nf"]

    def test_the_sif_comment_stops_at_where_it_was_put_when_no_compute_env_is_named(self):
        cfg = render_nextflow(_record(sif_paths={REQUEST_KEY: SIF}))["nextflow.config"]
        assert _sif_set(DIGEST, SIF, 12) in _profile(cfg, "slurm")
        assert " on " not in _profile(cfg, "slurm").split("container = ")[0].splitlines()[-2]

    def test_a_predicted_sif_path_never_claims_the_file_was_staged(self):
        """`render_pipeline(env=…)` fills the slurm container with the path
        stage_apptainer_image WOULD write, computed from the env block and the local
        cache alone — no cluster is contacted. The comment must say so, not report a
        staging that never happened."""
        cfg = render_nextflow(_record(sif_paths={REQUEST_KEY: SIF}, compute_env="cluster"), env=ENV)["nextflow.config"]
        slurm = _profile(cfg, "slurm")
        assert "put it" not in slurm and "staged on" not in slurm and "(sha256" not in slurm
        assert "Not staged yet — run stage_apptainer_image before sbatch launcher.sh." in slurm

    def test_a_staged_sif_is_stated_as_a_fact_with_its_sha256(self):
        """When the sealed steps carry `cluster_sif_sha256` the record's stages carry
        `sif_sha256`: the file was observed on the cluster, and the comment says so."""
        rec = _record(sif_paths={REQUEST_KEY: SIF}, compute_env="cluster")
        for st in rec.stages:
            st.sif_sha256 = "ab" * 32
        cfg = render_nextflow(rec, env=ENV)["nextflow.config"]
        slurm = _profile(cfg, "slurm")
        assert ("            executor = 'slurm'\n"
                + _sif_staged(DIGEST, SIF, "ab" * 32, 12, " on cluster") +
                "            cache = 'lenient'\n") in slurm
        assert "Not staged yet" not in slurm and "writes" not in slurm

    def test_the_run_records_land_under_a_timestamped_run_dir_trace_report_and_timeline(self):
        cfg = render_nextflow(_record())["nextflow.config"]
        assert "params.run_stamp = new java.util.Date().format('yyyyMMdd_HHmmss')" in cfg
        assert "def " not in cfg                           # the strict config parser allows no declarations
        assert ('trace {\n    enabled = true\n    file = "runs/${params.run_stamp}/trace.txt"\n'
                f"    fields = '{TRACE_FIELDS}'\n}}") in cfg
        # which task, which SLURM job, how it ended, when, how long, what it cost, where, and the command
        assert TRACE_FIELDS == ("task_id,native_id,name,status,exit,submit,start,complete,realtime,%cpu,peak_rss,"
                                "container,workdir")
        assert 'report {\n    enabled = true\n    file = "runs/${params.run_stamp}/report.html"\n}' in cfg
        assert 'timeline {\n    enabled = true\n    file = "runs/${params.run_stamp}/timeline.html"\n}' in cfg
        assert "dag {" not in cfg                          # the page's picture is the DAG; Nextflow's needs a CDN
        assert cfg.count("runs/${params.run_stamp}/") == 3
        assert "// One directory per run, runs/<stamp>/, named by its launch time and never overwritten:" in cfg
        assert "main.nf adds params.json and the samplesheet" in cfg

    def test_without_an_env_there_is_no_module_load_no_account_no_queue(self):
        cfg = render_nextflow(_record())["nextflow.config"]
        assert "module load" not in cfg and "--account" not in cfg and "queue =" not in cfg
        assert "beforeScript" not in cfg

    def test_unsized_stages_share_the_default_request_and_say_so(self):
        cfg = render_nextflow(_record())["nextflow.config"]
        assert ("process {\n"
                "    // One failing sample stops nothing already running: in-flight tasks complete, nothing\n"
                "    // new is submitted, and -resume re-runs the failed tasks and what follows them.\n"
                "    errorStrategy = 'finish'\n"
                "    // DEFAULT request — not sized for your data\n"
                "    cpus = 1\n"
                "    memory = '8 GB'\n"
                "    time = '4h'\n"
                "}") in cfg
        assert "withName" not in cfg

    def test_a_caller_sized_stage_gets_a_with_name_block_quoting_the_measurement(self):
        cfg = render_nextflow(_record(resources=SIZED))["nextflow.config"]
        assert ("    withName: 'HISAT2' {   // measured on 3 sealed steps on the workflow's test data: "
                "wall 68 s · peak RSS 1570 MB · max CPU 100% (authoritative) — a measurement, never the request\n"
                "        cpus = 8\n"
                "        memory = '32 GB'\n"
                "        time = '4h'\n"
                "    }\n") in cfg
        assert "// DEFAULT request — not sized for your data" in cfg   # the other two stages

    def test_when_every_stage_is_sized_there_is_no_default_block_and_units_convert(self):
        cfg = render_nextflow(_record(resources=ALL_SIZED))["nextflow.config"]
        assert "DEFAULT request" not in cfg
        assert "time = '1d 12h 30m'" in cfg
        assert "memory = '12000 MB'" in cfg and "time = '20m'" in cfg
        assert "memory = '4 GB'" in cfg and "time = '45s'" in cfg

    def test_a_gpu_stage_asks_for_the_device_and_binds_the_driver_in(self):
        cfg = render_nextflow(_record(resources=GPU), env=ENV)["nextflow.config"]
        assert ("            withName: 'HISAT2' {\n"
                "                queue = 'gpu'\n"
                "                clusterOptions = '--gres=gpu:1 --qos=gpu_access --account=acct'\n"
                "                containerOptions = '--nv'\n"
                "            }\n") in cfg
        local = _profile(cfg, "local")
        assert "--nv" not in local and "gres" not in local

    def test_a_gpu_stage_with_no_placement_states_that_the_scheduler_chooses(self):
        cfg = render_nextflow(_record(resources=GPU))["nextflow.config"]
        assert "clusterOptions = '--gres=gpu:1'\n" in cfg
        assert "containerOptions = '--nv'" in cfg
        assert "// GPU placement: --gres only, no --partition/--qos — the scheduler chooses." in cfg
        assert "--account" not in cfg and "queue =" not in cfg

    def test_several_images_name_a_container_per_stage_in_both_profiles(self):
        """Two frozen envs in one chain: neither profile has a profile-wide container to
        fall back on — every stage names its own, the docker tag locally and the .sif
        slot (empty, with its digest) on the cluster."""
        files = render_nextflow(_two_images(_record()))
        cfg = files["nextflow.config"]
        local, slurm = _profile(cfg, "local"), _profile(cfg, "slurm")
        assert ("        process {\n"
                "            executor = 'local'\n"
                "            withName: 'HISAT2' { container = 'bioinf_rnaseq_cli:latest' }\n"
                "            withName: 'SAMTOOLS' { container = 'bioinf_rnaseq_cli:latest' }\n"
                "            withName: 'HTSEQ_COUNT' { container = 'bioinf_counts:latest' }\n"
                "        }\n") in local
        assert "\n            container =" not in local
        assert ("            executor = 'slurm'\n"
                "            cache = 'lenient'\n"
                "            withName: 'HISAT2' {\n"
                + _set_me(DIGEST, 16) +
                "            }\n"
                "            withName: 'SAMTOOLS' {\n"
                + _set_me(DIGEST, 16) +
                "            }\n"
                "            withName: 'HTSEQ_COUNT' {\n"
                + _set_me(OTHER, 16) +
                "            }\n") in slurm
        assert "\n            container =" not in slurm
        assert not any("params.sif" in text or "sif_1" in text or "sif_2" in text
                       for text in files.values())

    def test_several_images_with_sifs_put_each_stages_sif_inside_its_with_name_block(self):
        rec = _two_images(_record(compute_env="cluster"))
        for st in rec.stages:                # one .sif per IMAGE: stages sharing a digest share it
            st.sif_path = "/cluster/containers/a.sif" if st.image_digest == DIGEST else "/cluster/containers/b.sif"
        cfg = render_nextflow(rec, env=ENV)["nextflow.config"]
        slurm = _profile(cfg, "slurm")
        assert ("            withName: 'HISAT2' {\n"
                + _sif_set(DIGEST, "/cluster/containers/a.sif", 16, " on cluster") +
                "            }\n"
                "            withName: 'SAMTOOLS' {\n"
                + _sif_set(DIGEST, "/cluster/containers/a.sif", 16, " on cluster") +
                "            }\n"
                "            withName: 'HTSEQ_COUNT' {\n"
                + _sif_set(OTHER, "/cluster/containers/b.sif", 16, " on cluster") +
                "            }\n") in slurm
        assert "SET ME" not in cfg and "container = ''" not in cfg

    def test_a_gpu_stage_in_a_multi_image_chain_keeps_its_container_and_placement_in_one_block(self):
        slurm = _profile(render_nextflow(_two_images(_record(resources=GPU)), env=ENV)["nextflow.config"],
                         "slurm")
        assert ("            withName: 'HISAT2' {\n"
                + _set_me(DIGEST, 16) +
                "                queue = 'gpu'\n"
                "                clusterOptions = '--gres=gpu:1 --qos=gpu_access --account=acct'\n"
                "                containerOptions = '--nv'\n"
                "            }\n") in slurm


# ===========================================================================
# params.yaml — the pipeline's parameters, nothing about where it runs
# ===========================================================================


class TestParamsYaml:
    def test_the_shared_params_with_their_sealed_defaults_each_with_a_comment(self):
        y = render_nextflow(_record())["params.yaml"]
        assert y.splitlines()[0] == ("# rendered from sealed workflow rnaseq_counts_workflow (pipeline "
                                     "rnaseq_counts) — edit this file and samples.csv, not main.nf")
        assert ("# HISAT2_INDEX — a prefix naming a family of files; produced by sealed step 1 (hisat2-build); "
                "format hisat2_index; HISAT2 index prefix; used by HISAT2\n"
                f"hisat2_index: {INDEX}\n") in y
        assert "\nstranded: reverse\n" in y
        assert f"\ngtf: {GTF}\n" in y
        assert "\nsamplesheet: samples.csv\n" in y
        assert "\noutdir: results\n" in y
        assert "reads:" not in y and "sample:" not in y

    @pytest.mark.parametrize("shape", ["no_env", "env_and_sif", "two_images"])
    def test_it_carries_only_pipeline_params_and_never_says_where_the_pipeline_runs(self, shape):
        """The header says where the pipeline runs is nextflow.config's business, and the
        body proves it: the shared params, the samplesheet and the output directory —
        no sif slot, no digest, no image name, whatever the record carries."""
        rec, env = {
            "no_env": (_record(), None),
            "env_and_sif": (_record(sif_paths={REQUEST_KEY: SIF}, compute_env="cluster"), ENV),
            "two_images": (_two_images(_record()), None),
        }[shape]
        y = render_nextflow(rec, env=env)["params.yaml"]
        header = " ".join(y.splitlines()[1:3])
        assert header.startswith("# Every value is what the sealed run was validated with.")
        assert "WHERE the pipeline runs" in header and "nextflow.config's business" in header
        keys = [ln.split(":", 1)[0] for ln in y.splitlines() if ln and not ln.startswith("#")]
        assert keys == ["hisat2_index", "stranded", "gtf", "samplesheet", "outdir"]
        assert "sif" not in y.lower()
        assert DIGEST not in y and OTHER not in y and SIF not in y
        assert "bioinf_rnaseq_cli" not in y and OTHER_IMAGE not in y
        assert "REQUIRED" not in y


# ===========================================================================
# the thread slot — the command's thread count IS the stage's cpus request
# ===========================================================================


class TestThreadSlot:
    """A how-to input declared `format: threads` binds to `${task.cpus}` and sizes the
    stage's cpus from the sealed count, so the command and the request cannot
    disagree; it is never a params.yaml key. A literal count stays a literal."""

    def test_the_slot_binds_to_task_cpus_and_the_stage_requests_the_sealed_count(self):
        rec = _threads()
        files = render_nextflow(rec)
        assert _script_lines(files["main.nf"], "HISAT2") == [ALIGN_LINE.replace("-p 4", "-p ${task.cpus}")]
        assert bound_commands(rec, rec.stage("HISAT2")) == _script_lines(files["main.nf"], "HISAT2")
        assert ("    withName: 'HISAT2' {   // measured on 3 sealed steps on the workflow's test data: "
                "wall 68 s · peak RSS 1570 MB · max CPU 100% (authoritative) — a measurement, never the request\n"
                "        cpus = 4   // the thread count the command runs with, through task.cpus; the sealed run used 4\n"
                "    }\n") in files["nextflow.config"]
        assert "// DEFAULT request — not sized for your data" in files["nextflow.config"]   # mem and time still default
        assert "THREADS" not in "".join(files.values())
        assert "threads" not in files["params.yaml"] and "params.threads" not in files["main.nf"]
        keys = [ln.split(":", 1)[0] for ln in files["params.yaml"].splitlines() if ln and not ln.startswith("#")]
        assert keys == ["hisat2_index", "stranded", "gtf", "samplesheet", "outdir"]

    def test_a_caller_sized_stage_keeps_the_binding_and_runs_with_its_own_count(self):
        files = render_nextflow(_threads(resources=SIZED))
        assert "hisat2 -p ${task.cpus} " in files["main.nf"]
        assert "        cpus = 8   // the thread count the command runs with, through task.cpus\n" in files["nextflow.config"]
        assert "the sealed run used" not in files["nextflow.config"]

    def test_a_literal_count_stays_a_literal_and_no_stage_mentions_task_cpus(self):
        files = render_nextflow(_record())
        assert "hisat2 -p 4 " in files["main.nf"] and "task.cpus" not in "".join(files.values())

    def test_the_directory_lint_accepts_the_binding(self):
        from agent.skills.pipeline_render import render_pipeline_files
        assert "hisat2 -p ${task.cpus} " in render_pipeline_files(_threads())["main.nf"]


# ===========================================================================
# launcher.sh — the manager job
# ===========================================================================


class TestLauncher:
    def test_the_manager_job_header_follows_the_env_policy(self):
        sh = render_nextflow(_record(), env=ENV)["launcher.sh"]
        assert sh.startswith(
            "#!/usr/bin/env bash\n"
            "# rendered from sealed workflow rnaseq_counts_workflow (pipeline rnaseq_counts) — "
            "edit params.yaml / samples.csv, not this file\n"
            "# The manager job: it submits one job per stage and sample, and does none of the work.\n"
            "#SBATCH --job-name=rnaseq_counts\n"
            "#SBATCH --time=2-00:00:00\n"
            "#SBATCH --mem=4G\n"
            "#SBATCH --nodes=1\n"
            "#SBATCH --ntasks=1\n"
            "#SBATCH --cpus-per-task=1\n"
            "#SBATCH --partition=cpu\n"
            "#SBATCH --account=acct\n"
            "#SBATCH --output=%x-%j.out\n"
            "#SBATCH --error=%x-%j.err\n"
            "#SBATCH --mail-type=END\n"
            "#SBATCH --mail-user=someone@example.org\n")

    def test_the_manager_job_body_is_strict_mode_modules_nxf_home_and_one_nextflow_line(self):
        sh = render_nextflow(_record(), env=ENV)["launcher.sh"]
        body = sh.split("#SBATCH --mail-user=someone@example.org\n", 1)[1]
        assert body.startswith(
            f"\n{STRICT_MODE_LINE}\n"
            "\n"
            "module load apptainer/1.5.0 nextflow/25.04.7\n")
        assert 'export NXF_HOME="$PWD/.nextflow_home"\n' in body
        commands = [ln for ln in body.splitlines() if ln.strip() and not ln.startswith("#")]
        assert commands == [STRICT_MODE_LINE,
                            "module load apptainer/1.5.0 nextflow/25.04.7",
                            'export NXF_HOME="$PWD/.nextflow_home"',
                            RUN_STAMP_LINE,
                            RUN_HPC_NEXTFLOW]
        assert "cd " not in body and "RUN_ID" not in body and "cp " not in body
        assert "nextflow clean -f" in sh and "never cleaned for you" in sh

    def test_the_nextflow_line_is_run_local_on_the_slurm_profile_with_the_callers_arguments(self):
        """The launcher runs the ONE launch line the page shows for a laptop, with the
        cluster profile in place of the local one and the caller's arguments passed
        through — one spelling, two profiles."""
        sh = render_nextflow(_record(), env=ENV)["launcher.sh"]
        assert RUN_HPC_NEXTFLOW + "\n" in sh
        assert RUN_LOCAL.replace("local", "slurm").removeprefix("nextflow ") in RUN_HPC_NEXTFLOW
        assert RUN_HPC_NEXTFLOW.endswith('--run_stamp "$RUN_STAMP" "$@"')
        assert RUN_LOCAL not in sh
        assert sh.count("nextflow -log") == 1 and sh.count("nextflow run") == 0

    def test_the_strict_mode_line_is_bash_strict_mode_with_its_reason(self):
        assert STRICT_MODE_LINE.startswith("set -euo pipefail")
        assert "# bash strict mode" in STRICT_MODE_LINE

    def test_without_an_env_no_modules_no_policy_lines_and_the_gap_is_stated(self):
        sh = render_nextflow(_record())["launcher.sh"]
        assert not re.search(r"^module ", sh, re.M)
        assert "--partition" not in sh and "--account" not in sh and "--mail" not in sh
        assert "# Rendered without a compute env: make apptainer and nextflow available before" in sh

    def test_an_env_missing_a_module_name_gets_no_module_lines_and_a_comment(self):
        env = {k: v for k, v in ENV.items() if k != "nextflow_module"}
        sh = render_nextflow(_record(), env=env)["launcher.sh"]
        assert not re.search(r"^module ", sh, re.M)
        assert "# The env declares no apptainer_module / nextflow_module" in sh
        assert "#SBATCH --account=acct" in sh                 # the policy still applies


# ===========================================================================
# run_lines / bound_commands — what the page reads
# ===========================================================================


class TestPublicHelpers:
    """`run_lines` and `bound_commands` are what pipeline.html reads, so the page shows
    the line that starts a run and the lines main.nf runs — never a paraphrase of
    either. Both are pinned against the rendered files themselves."""

    def test_run_local_and_run_hpc_are_the_one_spelling_of_how_a_run_starts(self):
        assert RUN_LOCAL == "nextflow run main.nf -profile local -params-file params.yaml -resume"
        assert RUN_HPC == "sbatch launcher.sh"
        rec = _record()
        assert run_lines(rec, "local") == [RUN_LOCAL]
        assert run_lines(rec, "hpc") == [RUN_HPC]

    def test_a_locus_that_is_neither_local_nor_hpc_is_refused(self):
        with pytest.raises(ValueError) as e:
            run_lines(_record(), "mars")
        assert "mars" in str(e.value) and "'local'" in str(e.value) and "'hpc'" in str(e.value)

    def test_bound_commands_are_exactly_the_script_lines_main_nf_runs(self):
        rec = _record()
        main = render_nextflow(rec)["main.nf"]
        assert bound_commands(rec, rec.stage("HTSEQ_COUNT")) == [COUNT_LINE]
        for st in rec.stages:
            assert bound_commands(rec, st) == _script_lines(main, st.name), st.name
        one = _one_row()
        main = render_nextflow(one)["main.nf"]
        for st in one.stages:
            assert bound_commands(one, st) == _script_lines(main, st.name), st.name

    def test_a_merged_stage_binds_each_of_its_commands_in_order(self):
        rec = _record(stages=[[0, 1], [2]], stage_names=["ALIGN", "COUNT"])
        main = render_nextflow(rec)["main.nf"]
        assert bound_commands(rec, rec.stage("ALIGN")) == [ALIGN_LINE, INDEX_LINE]
        assert _script_lines(main, "ALIGN") == [ALIGN_LINE, INDEX_LINE]

    def test_bound_commands_refuse_a_placeholder_the_record_does_not_know(self):
        rec = _record()
        rec.stages[0].commands = ["hisat2 -x {NOPE} -U {READS} | samtools sort -o {OUTPUT_DIR}/aligned.bam"]
        with pytest.raises(ValueError) as e:
            bound_commands(rec, rec.stages[0])
        assert "stage HISAT2" in str(e.value) and "{NOPE}" in str(e.value)
        assert "re-derive the record" in str(e.value)


# ===========================================================================
# Refusals — each names the remedy
# ===========================================================================


class TestRefusals:
    @pytest.mark.parametrize("command, what", [
        ("awk '{print $1}' {OUTPUT_DIR}/aligned.bam", "a `$`"),
        ("printf 'a\\tb' > {OUTPUT_DIR}/aligned.bam", "a backslash"),
        ('echo """x""" > {OUTPUT_DIR}/aligned.bam', 'a `"""`'),
    ])
    def test_a_sealed_command_the_script_block_would_rewrite_is_refused(self, command, what):
        """The script block is a Groovy triple-quoted string: `$` interpolates, a
        backslash escapes, `\"\"\"` ends it. Each would reach the compute node as a
        different command from the sealed one, so the render refuses and names the
        only remedy — a how-to free of it."""
        rec = _record()
        rec.stages[0].commands = [command]
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert str(e.value) == (
            f"stage HISAT2: the sealed command {command!r} contains {what}, which a Nextflow "
            f"script block rewrites in transit; re-seal the how-to with a command free of it")

    def test_a_single_quote_is_fine_now_that_the_script_block_is_a_bash_script(self):
        rec = _record()
        rec.stages[0].commands = ["echo 'quoted' > {OUTPUT_DIR}/aligned.bam"]
        assert "    echo 'quoted' > aligned.bam\n" in render_nextflow(rec)["main.nf"]

    def test_a_command_binding_no_per_sample_value_still_runs_per_sample_as_the_seal_ran_it(self):
        """The seal's self-test ran every how-to command once per trial; a cohort stage is
        never inferred from a command's placeholders — it comes from a workflow attached
        with cohort= (TestCohort)."""
        spec = sealed_rnaseq_spec(templates=[*TEMPLATES, "multiqc {OUTPUT_DIR}"])
        rec = pr.derive_pipeline_record(spec, name="with_multiqc")
        assert rec.stage("MULTIQC").scope == "per_sample"
        main = render_nextflow(rec)["main.nf"]
        assert "process MULTIQC {\n    tag { meta.sample }" in main
        assert "    multiqc .\n" in main

    def test_a_stage_that_names_no_image_is_refused(self):
        rec = _record()
        rec.stages[0].image, rec.stages[0].image_digest = None, None
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert "stage HISAT2 names no image" in str(e.value)
        assert "ran in a container" in str(e.value)

    def test_a_stage_name_that_is_not_a_groovy_identifier_is_refused_naming_stage_names(self):
        rec = _record(stages=[[0, 1], [2]], stage_names=["ALIGN-1", "COUNT"])
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert "ALIGN-1" in str(e.value) and "stage_names=" in str(e.value)

    def test_a_pipeline_name_with_shell_metacharacters_is_refused(self):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq;rm -rf")
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert "record.name" in str(e.value)

    def test_an_env_module_name_with_shell_metacharacters_is_refused(self):
        env = {**ENV, "apptainer_module": "apptainer/1.5.0; rm -rf /"}
        with pytest.raises(ValueError) as e:
            render_nextflow(_record(), env=env)
        assert "apptainer_module" in str(e.value)

    def test_an_artifact_that_is_not_a_bare_filename_is_refused(self):
        rec = _record()
        rec.stages[0].outputs[0].artifact = "sub/aligned.bam"
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert "sub/aligned.bam" in str(e.value) and "bare filename" in str(e.value)

    def test_an_artifact_naming_a_placeholder_the_record_lacks_is_refused(self):
        rec = _record()
        rec.stages[2].outputs[0].artifact = "{NOPE}.counts.tsv"
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert "['NOPE']" in str(e.value) and "not parameters of the record" in str(e.value)


# ===========================================================================
# The real thing — the rendered directory under the runtime env's nextflow
# ===========================================================================

_RUNTIME = Path(__file__).resolve().parents[1] / ".conda_runtime"
_NEXTFLOW = _RUNTIME / "bin" / "nextflow"
_JVM = _RUNTIME / "lib" / "jvm"
needs_nextflow = pytest.mark.skipif(
    not (_NEXTFLOW.is_file() and os.access(_NEXTFLOW, os.X_OK) and (_JVM / "bin" / "java").is_file()),
    reason="the runtime env carries no nextflow binary (./scripts/setup.sh installs it)")


def _nextflow(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the runtime env's nextflow in `cwd`, offline, with NXF_HOME inside the
    pipeline directory (as launcher.sh does) so nothing touches ~/.nextflow. The
    launcher honours JAVA_HOME / JAVA_CMD over the JDK beside it, so both name the
    bundled JVM."""
    env = dict(os.environ, JAVA_HOME=str(_JVM), JAVA_CMD=str(_JVM / "bin" / "java"),
               NXF_HOME=str(cwd / ".nextflow_home"), NXF_OFFLINE="true", NXF_ANSI_LOG="false")
    return subprocess.run([str(_NEXTFLOW), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=300)


def _write(files: dict, d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


def _point_at_real_files(rec, data: Path):
    """The fixture's paths are absolute and nowhere; give the shared params and every
    samplesheet row a file that exists, so the channels can be built."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "chr22.gtf").write_text("x\n")
    for i in range(1, 9):
        (data / f"chr22.{i}.ht2").write_text("x")
    rec.param("GTF").default = str(data / "chr22.gtf")
    rec.param("HISAT2_INDEX").default = str(data / "chr22")
    for row in rec.samplesheet.rows:
        reads = data / f"{row['sample']}.fastq.gz"
        reads.write_text("x")
        row["reads"] = str(reads)
    return rec


@needs_nextflow
class TestRealNextflow:
    """The rendered directory, parsed and previewed by the real binary: the config
    resolves under both profiles to the containers the contract names, the guard stops
    a cluster run whose container is empty before any job exists, and a run whose
    .sif is named gets past it. `-preview` evaluates the script and builds the
    channels without executing a process, so no image, docker or SLURM is needed."""

    def test_the_config_resolves_under_each_profile_to_the_container_it_names(self, tmp_path):
        rec = _record(sif_paths={REQUEST_KEY: SIF}, compute_env="cluster")
        d = _write(render_nextflow(rec, env=ENV), tmp_path / "pipe")
        local = _nextflow(d, "config", "-flat", "-profile", "local")
        assert local.returncode == 0, local.stdout + local.stderr
        assert "process.executor = 'local'\n" in local.stdout
        assert "process.container = 'bioinf_rnaseq_cli:latest'\n" in local.stdout
        assert "docker.enabled = true\n" in local.stdout
        slurm = _nextflow(d, "config", "-flat", "-profile", "slurm")
        assert slurm.returncode == 0, slurm.stdout + slurm.stderr
        assert "process.executor = 'slurm'\n" in slurm.stdout
        assert f"process.container = '{SIF}'\n" in slurm.stdout
        assert "process.cache = 'lenient'\n" in slurm.stdout
        assert "process.beforeScript = 'module load apptainer/1.5.0'\n" in slurm.stdout
        assert "process.queue = 'cpu'\n" in slurm.stdout
        assert "process.clusterOptions = '--account=acct'\n" in slurm.stdout
        assert "apptainer.enabled = true\n" in slurm.stdout
        for out in (local.stdout, slurm.stdout):
            assert "params.sif" not in out and "params.samplesheet" not in out   # params.yaml's, not the config's

    def test_on_the_cluster_profile_an_empty_container_stops_the_run_before_any_job(self, tmp_path):
        d = _write(render_nextflow(_record()), tmp_path / "pipe")
        run = _nextflow(d, "run", "main.nf", "-profile", "slurm", "-params-file", "params.yaml", "-preview")
        assert run.returncode != 0
        assert ("nextflow.config, profile slurm: process.container is empty; set it to the .sif "
                f"built from image {DIGEST}") in run.stdout + run.stderr

    def test_with_the_sif_named_the_run_passes_the_guard_under_both_profiles_and_leaves_its_launch_record(self, tmp_path):
        """The record block runs in the preview too — the workflow body does — so the
        strict syntax is known to accept it and the record is known to be written:
        params.json is params.yaml's own mapping as Nextflow resolved it with the stamp
        left out, the samplesheet is copied as read, and each run names its own directory."""
        import json
        rec = _point_at_real_files(
            _record(sif_paths={REQUEST_KEY: str(tmp_path / "data" / "img.sif")}, compute_env="cluster"),
            tmp_path / "data")
        d = _write(render_nextflow(rec, env=ENV), tmp_path / "pipe")
        declared = yaml.safe_load((d / "params.yaml").read_text())
        for profile in ("slurm", "local"):
            run = _nextflow(d, "run", "main.nf", "-profile", profile, "-params-file", "params.yaml", "-preview",
                            "--run_stamp", f"preview_{profile}")
            assert run.returncode == 0, profile + "\n" + run.stdout + run.stderr
            assert "process.container is empty" not in run.stdout + run.stderr
            assert f"run records: runs/preview_{profile}/" in run.stdout + run.stderr
            run_dir = d / "runs" / f"preview_{profile}"
            written = json.loads((run_dir / "params.json").read_text())
            assert written == declared and "run_stamp" not in written
            assert written["samplesheet"] == "samples.csv" and written["outdir"] == "results"
            assert (run_dir / "samples.csv").read_text() == (d / "samples.csv").read_text()
            assert (run_dir / "trace.txt").is_file()
        assert sorted(p.name for p in (d / "runs").iterdir()) == ["preview_local", "preview_slurm"]

    def test_several_images_stop_the_run_when_no_stage_names_a_sif_and_pass_when_every_stage_does(self, tmp_path):
        empty = _write(render_nextflow(_two_images(_record())), tmp_path / "empty")
        run = _nextflow(empty, "run", "main.nf", "-profile", "slurm", "-params-file", "params.yaml", "-preview")
        assert run.returncode != 0
        assert f"set it to the .sif built from image {DIGEST}, {OTHER}" in run.stdout + run.stderr
        rec = _point_at_real_files(_two_images(_record(compute_env="cluster")), tmp_path / "data")
        for st in rec.stages:
            st.sif_path = str(tmp_path / "data" / f"{st.image_digest[-4:]}.sif")
        filled = _write(render_nextflow(rec, env=ENV), tmp_path / "filled")
        run = _nextflow(filled, "run", "main.nf", "-profile", "slurm", "-params-file", "params.yaml", "-preview")
        assert run.returncode == 0, run.stdout + run.stderr
        assert "process.container is empty" not in run.stdout + run.stderr

    def test_several_images_stop_the_run_while_any_stage_still_lacks_its_sif(self, tmp_path):
        """The guard exists so a cluster run with an unfilled container fails once, up
        front, instead of submitting the stages that ARE configured and failing the
        one that is not on the bare node. With two images and one .sif still empty
        it must therefore refuse, exactly as it does when every .sif is empty."""
        rec = _point_at_real_files(_two_images(_record(compute_env="cluster")), tmp_path / "data")
        for st in rec.stages:
            st.sif_path = str(tmp_path / "data" / "a.sif") if st.image_digest == DIGEST else None
        d = _write(render_nextflow(rec, env=ENV), tmp_path / "pipe")
        assert _set_me(OTHER, 16) in (d / "nextflow.config").read_text()    # the last stage is unfilled
        run = _nextflow(d, "run", "main.nf", "-profile", "slurm", "-params-file", "params.yaml", "-preview")
        assert run.returncode != 0, run.stdout + run.stderr
        assert "process.container is empty" in run.stdout + run.stderr
