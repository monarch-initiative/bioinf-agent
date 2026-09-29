"""The Nextflow form is rendered from the pipeline record and nothing else: every
file is pinned here as the literal text a human reads, copies and re-runs."""
from __future__ import annotations

import re

import pytest
from pipeline_fixtures import DIGEST, GTF, INDEX, REQUEST_KEY, sealed_rnaseq_spec

from agent.skills import pipeline_record as pr
from agent.skills.pipeline_render_nextflow import render_nextflow

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

ALIGN_LINE = ("hisat2 -p 4 -x ${file(params.hisat2_index).name} -U ${reads} "
              "| samtools sort -o aligned.bam")
INDEX_LINE = "samtools index aligned.bam"
COUNT_LINE = ("htseq-count -s ${params.stranded} -f bam aligned.bam ${gtf} "
              "> ${meta.sample}.counts.tsv")


def _record(**kw):
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", **kw)


def _linear(**kw):
    return pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one", **kw)


def _process_block(main_nf: str, name: str) -> str:
    """The text of ONE process, `process NAME {` through its closing brace."""
    m = re.search(rf"^process {name} \{{\n(.*?)^\}}", main_nf, re.S | re.M)
    assert m, f"no process {name} in main.nf"
    return m.group(1)


def _workflow_block(main_nf: str) -> str:
    m = re.search(r"^workflow \{\n(.*?)^\}", main_nf, re.S | re.M)
    assert m, "no workflow block in main.nf"
    return m.group(1)


# ===========================================================================
# The file set
# ===========================================================================


class TestFileSet:
    def test_a_per_row_record_renders_five_files_and_a_linear_one_four(self):
        assert set(render_nextflow(_record())) == {
            "main.nf", "nextflow.config", "params.yaml", "launcher.sh", "samples.csv"}
        assert set(render_nextflow(_linear())) == {
            "main.nf", "nextflow.config", "params.yaml", "launcher.sh"}

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
        lin = render_nextflow(_linear())
        assert "edit params.yaml, not this file" in lin["main.nf"].splitlines()[0]
        assert "samples.csv" not in lin["main.nf"].splitlines()[0]

    def test_samples_csv_is_the_records_one_samplesheet_rendering(self):
        rec = _record()
        assert render_nextflow(rec)["samples.csv"] == pr.render_samplesheet(rec)
        assert render_nextflow(rec)["samples.csv"].splitlines()[0] == "sample,reads"

    @pytest.mark.parametrize("shape", ["per_row", "linear"])
    @pytest.mark.parametrize("with_env", [True, False])
    def test_no_placeholder_survives_in_any_rendered_file(self, shape, with_env):
        rec = _record(resources=GPU) if shape == "per_row" else _linear(resources=GPU)
        files = render_nextflow(rec, env=ENV if with_env else None)
        for name, text in files.items():
            assert pr.placeholders(text) == [], f"{name} still carries a placeholder"

    @pytest.mark.parametrize("shape", ["per_row", "linear"])
    def test_every_stage_name_appears_exactly_once_as_a_process(self, shape):
        rec = _record() if shape == "per_row" else _linear()
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
# main.nf — per_row
# ===========================================================================


class TestMainNfPerRow:
    def test_dsl2_and_the_shared_params_with_their_sealed_defaults(self):
        main = render_nextflow(_record())["main.nf"]
        assert "nextflow.enable.dsl = 2" in main
        assert f"params.hisat2_index = '{INDEX}'" in main
        assert "params.stranded = 'reverse'" in main
        assert f"params.gtf = '{GTF}'" in main
        assert "params.samplesheet = 'samples.csv'" in main
        assert "params.outdir = 'results'" in main
        assert "run_id" not in main
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
        assert ("    rows = Channel.fromPath(params.samplesheet, checkIfExists: true)\n"
                "        .splitCsv(header: true)\n"
                "        .map { r -> tuple([sample: r.sample], file(r.reads, checkIfExists: true)) }"
                "   // one (meta, reads) per samples.csv row\n") in wf
        assert '    HISAT2(rows, file("${params.hisat2_index}*"))\n' in wf
        assert "    SAMTOOLS(HISAT2.out.aligned_bam)\n" in wf
        assert ("    HTSEQ_COUNT(HISAT2.out.aligned_bam.join(SAMTOOLS.out.aligned_bam_bai), file(params.gtf))"
                "   // .join() on meta: (meta, aligned.bam) + (meta, aligned.bam.bai) "
                "-> (meta, aligned.bam, aligned.bam.bai)\n") in wf
        order = [wf.index(f"    {n}(") for n in ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT")]
        assert order == sorted(order)

    def test_the_workflow_refuses_to_start_on_the_cluster_without_a_sif(self):
        wf = _workflow_block(render_nextflow(_record())["main.nf"])
        assert ("    if (workflow.profile.tokenize(',').contains('slurm') && !params.sif)\n"
                f'        error "params.sif is empty: set it in params.yaml to the .sif built from image {DIGEST}"\n') in wf

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
# main.nf — linear
# ===========================================================================


class TestMainNfLinear:
    def test_no_samplesheet_no_meta_no_tag(self):
        main = render_nextflow(_linear())["main.nf"]
        assert "splitCsv" not in main and "meta" not in main and "tag " not in main
        assert "params.samplesheet" not in main

    def test_per_sample_inputs_become_params_carrying_the_seals_worked_example(self):
        """A one-row pipeline has no samplesheet, so the seal's single trial rides in
        params.* — the worked example, as the sheet's rows are in a per_row pipeline."""
        main = render_nextflow(_linear())["main.nf"]
        assert "params.reads = '/data/reads/SRR1039508_10K_R1.fastq.gz'\n" in main
        assert "params.sample = 'SRR1039508'\n" in main
        assert "REQUIRED" not in main and "is required" not in _workflow_block(main)

    def test_a_per_sample_input_with_no_default_is_required_and_the_workflow_checks_it(self):
        rec = _linear()
        rec = rec.model_copy(update={"params": [
            p.model_copy(update={"default": None}) if p.kind == "per_sample" else p
            for p in rec.params]})
        main = render_nextflow(rec)["main.nf"]
        assert ("// REQUIRED: the sealed record carries no default for a per-sample input of a "
                "one-row pipeline — set it in params.yaml.\nparams.reads = null\n") in main
        assert "params.sample = null" in main
        wf = _workflow_block(main)
        assert ("    if (!params.reads)\n"
                '        error "params.reads is required (the sealed record carries no default for it): '
                'set it in params.yaml"\n') in wf

    def test_plain_inputs_outputs_and_calls(self):
        main = render_nextflow(_linear())["main.nf"]
        assert ("    input:\n    path reads\n    path hisat2_index_files\n") in _process_block(main, "HISAT2")
        assert "    path 'aligned.bam', emit: aligned_bam\n" in _process_block(main, "HISAT2")
        assert ('    path "${params.sample}.counts.tsv", emit: counts_tsv\n') in _process_block(main, "HTSEQ_COUNT")
        assert 'publishDir "${params.outdir}", mode: \'copy\', overwrite: true' in _process_block(main, "HISAT2")
        wf = _workflow_block(main)
        assert '    HISAT2(file(params.reads), file("${params.hisat2_index}*"))\n' in wf
        assert "    HTSEQ_COUNT(HISAT2.out.aligned_bam, SAMTOOLS.out.aligned_bam_bai, file(params.gtf))\n" in wf
        assert ".join(" not in wf

    def test_the_script_lines_are_the_same_commands_with_params_where_meta_was(self):
        main = render_nextflow(_linear())["main.nf"]
        assert f"    {ALIGN_LINE}\n" in _process_block(main, "HISAT2")
        assert f"    {INDEX_LINE}\n" in _process_block(main, "SAMTOOLS")
        assert f"    {COUNT_LINE.replace('${meta.sample}', '${params.sample}')}\n" \
            in _process_block(main, "HTSEQ_COUNT")


# ===========================================================================
# nextflow.config
# ===========================================================================


class TestConfig:
    def test_the_two_profiles(self):
        cfg = render_nextflow(_record(), env=ENV)["nextflow.config"]
        assert "run_id" not in cfg
        assert "params.sif = ''" in cfg
        assert ("profiles {\n"
                "    local {\n"
                "        docker.enabled = true\n"
                "        process {\n"
                "            executor = 'local'\n"
                "            container = 'bioinf_rnaseq_cli:latest'\n"
                "        }\n"
                "    }\n"
                "    slurm {\n"
                "        apptainer.enabled = true\n"
                "        apptainer.autoMounts = true\n"
                "        apptainer.runOptions = '--cleanenv'") in cfg
        assert "        executor.queueSize = 50\n" in cfg
        assert ("            executor = 'slurm'\n"
                "            container = params.sif\n"
                "            cache = 'lenient'\n"
                "            beforeScript = 'module load apptainer/1.5.0'\n"
                "            queue = 'cpu'\n"
                "            clusterOptions = '--account=acct'\n") in cfg

    def test_trace_and_report_land_under_a_timestamped_run_dir_and_the_trace_carries_each_command(self):
        cfg = render_nextflow(_record())["nextflow.config"]
        assert "params.run_stamp = new java.util.Date().format('yyyyMMdd_HHmmss')" in cfg
        assert "def " not in cfg                           # the strict config parser allows no declarations
        assert ('trace {\n    enabled = true\n    file = "runs/${params.run_stamp}/trace.txt"\n'
                "    fields = 'task_id,name,status,exit,container,realtime,%cpu,peak_rss,workdir,script'\n}") in cfg
        assert 'report {\n    enabled = true\n    file = "runs/${params.run_stamp}/report.html"\n}' in cfg
        assert "timeline" not in cfg

    def test_the_sif_path_is_prefilled_when_the_render_named_a_cluster(self):
        sif = "/cluster/containers/rnaseq_cli_48ac8c5b25d2.sif"
        rec = _record(sif_paths={REQUEST_KEY: sif})
        files = render_nextflow(rec, env=ENV)
        assert f"params.sif = '{sif}'" in files["nextflow.config"]
        assert f"\nsif: {sif}\n" in files["params.yaml"]
        assert "stage_apptainer_image" in files["params.yaml"]

    def test_without_an_env_there_is_no_module_load_no_account_no_queue(self):
        cfg = render_nextflow(_record())["nextflow.config"]
        assert "module load" not in cfg and "--account" not in cfg and "queue =" not in cfg
        assert "beforeScript" not in cfg

    def test_unsized_stages_share_the_default_request_and_say_so(self):
        cfg = render_nextflow(_record())["nextflow.config"]
        assert ("process {\n"
                "    errorStrategy = 'terminate'\n"
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
        local = cfg[cfg.index("    local {"):cfg.index("    slurm {")]
        assert "--nv" not in local and "gres" not in local

    def test_a_gpu_stage_with_no_placement_states_that_the_scheduler_chooses(self):
        cfg = render_nextflow(_record(resources=GPU))["nextflow.config"]
        assert "clusterOptions = '--gres=gpu:1'\n" in cfg
        assert "containerOptions = '--nv'" in cfg
        assert "// GPU placement: --gres only, no --partition/--qos — the scheduler chooses." in cfg
        assert "--account" not in cfg and "queue =" not in cfg

    def test_two_images_mean_one_sif_param_per_digest_and_a_container_per_stage(self):
        rec = _record()
        other = "sha256:bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"
        rec.stages[2].image, rec.stages[2].image_digest = "bioinf_counts:latest", other
        files = render_nextflow(rec)
        cfg = files["nextflow.config"]
        assert "params.sif_1 = ''" in cfg and "params.sif_2 = ''" in cfg and "params.sif =" not in cfg
        assert "            withName: 'HISAT2' { container = 'bioinf_rnaseq_cli:latest' }" in cfg
        assert "            withName: 'HTSEQ_COUNT' { container = 'bioinf_counts:latest' }" in cfg
        assert "                container = params.sif_1\n" in cfg
        assert ("            withName: 'HTSEQ_COUNT' {\n"
                "                container = params.sif_2\n") in cfg
        assert "container = params.sif\n" not in cfg
        assert "sif_1: ''" in files["params.yaml"] and "sif_2: ''" in files["params.yaml"]
        assert f"built from {other}" in files["params.yaml"]
        assert "!params.sif_1" in files["main.nf"] and "!params.sif_2" in files["main.nf"]


# ===========================================================================
# params.yaml
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

    def test_the_sif_slot_is_empty_and_names_the_digest_it_must_be_built_from(self):
        y = render_nextflow(_record())["params.yaml"]
        assert (f"# The Apptainer image built from {DIGEST}; its path on the cluster is not known at "
                "render time — set it before -profile slurm.\nsif: ''\n") in y

    def test_the_linear_form_lists_the_per_sample_inputs_with_the_seals_values(self):
        y = render_nextflow(_linear())["params.yaml"]
        assert "\nreads: /data/reads/SRR1039508_10K_R1.fastq.gz\n" in y
        assert "\nsample: SRR1039508\n" in y
        assert "samplesheet" not in y and "REQUIRED" not in y

    def test_the_linear_form_lists_a_per_sample_input_with_no_default_empty_and_required(self):
        rec = _linear()
        rec = rec.model_copy(update={"params": [
            p.model_copy(update={"default": None}) if p.kind == "per_sample" else p
            for p in rec.params]})
        y = render_nextflow(rec)["params.yaml"]
        assert ("# REQUIRED: the sealed record carries no default for a per-sample input of a one-row "
                "pipeline.\nreads: ''\n") in y
        assert "\nsample: ''\n" in y


# ===========================================================================
# launcher.sh and nextflow_local.sh
# ===========================================================================


class TestLaunchers:
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
            "\nset -euo pipefail   # bash strict mode: stop at the first failing command, "
            "an unset variable, or a failure inside a pipe\n"
            "\n"
            "module load apptainer/1.5.0 nextflow/25.04.7\n")
        assert 'export NXF_HOME="$PWD/.nextflow_home"\n' in body
        assert 'nextflow run main.nf -profile slurm -params-file params.yaml -resume "$@"\n' in body
        commands = [ln for ln in body.splitlines() if ln.strip() and not ln.startswith("#")]
        assert commands == ["set -euo pipefail   # bash strict mode: stop at the first failing command, "
                            "an unset variable, or a failure inside a pipe",
                            "module load apptainer/1.5.0 nextflow/25.04.7",
                            'export NXF_HOME="$PWD/.nextflow_home"',
                            'nextflow run main.nf -profile slurm -params-file params.yaml -resume "$@"']
        assert "cd " not in body and "RUN_ID" not in body and "cp " not in body
        assert "nextflow clean -f" in sh and "never cleaned for you" in sh

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

    def test_the_launcher_is_the_same_for_a_linear_record(self):
        sh = render_nextflow(_linear())["launcher.sh"]
        assert 'nextflow run main.nf -profile slurm -params-file params.yaml -resume "$@"\n' in sh
        assert "samples.csv" not in sh


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
        rec = _record()
        rec.stages[0].commands = [command]
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert "stage HISAT2" in str(e.value) and what in str(e.value)
        assert "not expressible in the Nextflow form; render the plain form" in str(e.value)

    def test_a_single_quote_is_fine_now_that_the_script_block_is_a_bash_script(self):
        rec = _record()
        rec.stages[0].commands = ["echo 'quoted' > {OUTPUT_DIR}/aligned.bam"]
        assert "    echo 'quoted' > aligned.bam\n" in render_nextflow(rec)["main.nf"]

    def test_a_cohort_stage_is_refused(self):
        spec = sealed_rnaseq_spec(templates=[*pr.derive_pipeline_record(sealed_rnaseq_spec(), name="t")
                                             .stages[0].commands] + [
            "samtools index {OUTPUT_DIR}/aligned.bam",
            "htseq-count -s {STRANDED} -f bam {OUTPUT_DIR}/aligned.bam {GTF} > {OUTPUT_DIR}/{SAMPLE}.counts.tsv",
            "multiqc {OUTPUT_DIR}"])
        rec = pr.derive_pipeline_record(spec, name="with_cohort")
        assert rec.stage("MULTIQC").scope == "cohort"
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert "cohort stages are not supported yet in the Nextflow form" in str(e.value)
        assert "render the plain form" in str(e.value)

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
