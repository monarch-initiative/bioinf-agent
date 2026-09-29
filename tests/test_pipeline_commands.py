"""pipeline_commands — the by-hand item on the menu, and the run lines the page shows.

`commands.sh` is the sealed how-to for one sample: the example values at the top, the
commands in order below, each placeholder bound to a shell variable, nothing else. The
page's run lines (enter the image, run Nextflow) come from the same module, so the
page and the script cannot disagree. These tests pin the script's shape, the binding
rules, the marker the honesty lint reads the commands back by, the run lines at both
loci, and the refusals.
"""
from __future__ import annotations

import subprocess
import shutil

import pytest

from agent.skills import pipeline_commands as pc
from agent.skills import pipeline_record as pr
from pipeline_fixtures import GTF, INDEX, REQUEST_KEY, SAMPLES, sealed_rnaseq_spec

READS0 = "/data/reads/SRR1039508_10K_R1.fastq.gz"
SIF = "/cluster/CLAUDE_CONTAINERS/rnaseq_cli_48ac8c5b25d2.sif"


def _record(**kw):
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", **kw)


def _linear(**kw):
    return pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one", **kw)


def _cluster(**kw):
    return _record(env_names={REQUEST_KEY: "rnaseq_cli"}, sif_paths={REQUEST_KEY: SIF},
                   compute_env="cluster", modules=["apptainer/1.5.0", "nextflow/25.04.7"], **kw)


class TestExampleValues:
    def test_shared_defaults_plus_the_first_samplesheet_row(self):
        v = pc.example_values(_record())
        assert v == {"HISAT2_INDEX": INDEX, "STRANDED": "reverse", "GTF": GTF,
                     "SAMPLE": SAMPLES[0], "READS": READS0}

    def test_a_linear_record_uses_its_per_sample_defaults(self):
        v = pc.example_values(_linear())
        assert v["SAMPLE"] == "SRR1039508" and v["READS"] == READS0

    def test_data_dirs_are_the_parents_of_every_path_and_prefix_value(self):
        assert pc.data_dirs(_record()) == ["/data/annotation", "/data/idx", "/data/reads"]


class TestCommandsSh:
    def test_values_at_the_top_then_the_commands_in_stage_order(self):
        sh = pc.render_commands(_record())
        assert sh.startswith("#!/usr/bin/env bash\n# rnaseq_counts — ONE sample by hand — rendered from sealed workflow rnaseq_counts_workflow\n")
        assert pc.STRICT_MODE_LINE in sh
        assert "bash strict mode: stop at the first failing command, an unset variable, or a failure inside a pipe" in sh
        assert f"\nSAMPLE={SAMPLES[0]}   # " in sh
        assert f"\nREADS={READS0}   # " in sh
        assert f"\nHISAT2_INDEX={INDEX}   # a prefix (a family of files named after it); HISAT2 index prefix; produced by sealed step 1 of the workflow\n" in sh
        assert "\nSTRANDED=reverse   # a value; htseq-count -s: yes | no | reverse\n" in sh
        assert f"\nGTF={GTF}   # a path; gene annotation\n" in sh
        assert '\nOUTPUT_DIR="$PWD/results/$SAMPLE"\nmkdir -p "$OUTPUT_DIR"\n' in sh
        assert ("# 1. HISAT2 — hisat2\n"
                "hisat2 -p 4 -x ${HISAT2_INDEX} -U ${READS} | samtools sort -o ${OUTPUT_DIR}/aligned.bam\n"
                "\n"
                "# 2. SAMTOOLS — samtools\n"
                "samtools index ${OUTPUT_DIR}/aligned.bam\n"
                "\n"
                "# 3. HTSEQ_COUNT — htseq-count\n"
                "htseq-count -s ${STRANDED} -f bam ${OUTPUT_DIR}/aligned.bam ${GTF} > ${OUTPUT_DIR}/${SAMPLE}.counts.tsv\n") in sh
        assert "docker" not in sh and "apptainer" not in sh      # entering the image is the page's step

    def test_a_linear_record_reads_the_same(self):
        sh = pc.render_commands(_linear())
        assert "\nSAMPLE=SRR1039508" in sh and f"\nREADS={READS0}" in sh
        assert '\nOUTPUT_DIR="$PWD/results"\n' in sh          # one row: results/ itself, as the Nextflow form publishes

    def test_the_values_come_first_so_a_reader_edits_before_anything_runs(self):
        sh = pc.render_commands(_record())
        assert sh.index("SAMPLE=") < sh.index("mkdir -p") < sh.index("# 1. HISAT2")

    def test_the_lint_reads_each_stages_commands_back_by_its_marker(self):
        rec = _record()
        sh = pc.render_commands(rec)
        assert pc.commands_in_script(sh, rec.stage("SAMTOOLS")) == ["samtools index ${OUTPUT_DIR}/aligned.bam"]
        merged = _record(stages=[[0, 1], [2]], stage_names=["ALIGN", "COUNT"])
        got = pc.commands_in_script(pc.render_commands(merged), merged.stage("ALIGN"))
        assert got == ["hisat2 -p 4 -x ${HISAT2_INDEX} -U ${READS} | samtools sort -o ${OUTPUT_DIR}/aligned.bam",
                       "samtools index ${OUTPUT_DIR}/aligned.bam"]
        assert pc.commands_in_script("# nothing here\n", rec.stage("HISAT2")) is None

    def test_a_shell_variable_the_sealed_command_already_carried_is_left_alone(self):
        rec = _record()
        rec.stages[1].commands = ["samtools index -@ ${THREADS} {OUTPUT_DIR}/aligned.bam"]
        assert pc.bind(rec, rec.stages[1].commands[0]) == "samtools index -@ ${THREADS} ${OUTPUT_DIR}/aligned.bam"

    def test_an_unknown_placeholder_is_refused_not_bound(self):
        rec = _record()
        with pytest.raises(pc.CommandsRenderError) as e:
            pc.bind(rec, "tool {NOT_A_PARAM} > {OUTPUT_DIR}/x")
        assert "NOT_A_PARAM" in str(e.value) and "re-derive" in str(e.value)

    def test_a_value_with_whitespace_or_a_metacharacter_is_refused(self):
        rec = _record()
        rec.params[[p.name for p in rec.params].index("STRANDED")].default = "no; rm -rf /"
        with pytest.raises(pc.CommandsRenderError) as e:
            pc.render_commands(rec)
        assert "STRANDED" in str(e.value)

    def test_deterministic(self):
        assert pc.render_commands(_record()) == pc.render_commands(_record())

    @pytest.mark.integration
    def test_bash_parses_it(self, tmp_path):
        if not shutil.which("bash"):
            pytest.skip("no bash")
        f = tmp_path / "commands.sh"
        f.write_text(pc.render_commands(_record()))
        assert subprocess.run(["bash", "-n", str(f)], capture_output=True).returncode == 0


class TestEnterImage:
    def test_local_is_one_docker_line_with_the_pipeline_dir_and_every_data_dir_mounted(self):
        lines = pc.enter_image(_record(), "local")
        assert lines == ['docker run --rm -it -v "$PWD":"$PWD" -w "$PWD" -v "/data/annotation":"/data/annotation" '
                         '-v "/data/idx":"/data/idx" -v "/data/reads":"/data/reads" bioinf_rnaseq_cli:latest bash']

    def test_hpc_loads_the_modules_and_opens_the_sif(self):
        lines = pc.enter_image(_cluster(), "hpc")
        assert lines == ["module load apptainer/1.5.0 nextflow/25.04.7",
                         f'apptainer shell --cleanenv --bind /data/annotation,/data/idx,/data/reads --pwd "$PWD" {SIF}']

    def test_hpc_without_a_cluster_named_says_where_the_sif_comes_from(self):
        lines = pc.enter_image(_record(), "hpc")
        assert len(lines) == 1 and lines[0].startswith("apptainer shell --cleanenv")
        assert "stage_apptainer_image" in lines[0] and "module load" not in lines[0]

    def test_an_unknown_locus_is_an_error(self):
        with pytest.raises(ValueError):
            pc.enter_image(_record(), "mars")


class TestNextflowRun:
    def test_the_two_lines(self):
        assert pc.nextflow_run(_record(), "local") == [
            "nextflow run main.nf -profile local -params-file params.yaml -resume"]
        assert pc.nextflow_run(_record(), "hpc") == ["sbatch launcher.sh"]
