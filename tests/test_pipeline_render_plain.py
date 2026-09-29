"""The plain form renders a sealed pipeline as bash + SLURM job arrays, binds every
placeholder only through the record, and refuses what it cannot carry.

Two tiers. The string tier pins the literal rendered text — the commands, the
preconditions, the SBATCH headers, the env merge — so a change in the renderer is a
change in a test. The executed tier runs the rendered scripts under bash with a fake
`docker` / `apptainer` / `sbatch` on PATH: the shell control flow (pre-flight, the
header-driven samplesheet, the per-row loop, the exit-code records, the dependency
chain) is what a user actually depends on, and no string assertion proves it.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from pipeline_fixtures import DIGEST, GTF, INDEX, REQUEST_KEY, SAMPLES, sealed_rnaseq_spec

from agent.skills import pipeline_record as pr
from agent.skills.pipeline_render_plain import render_plain

ENV = {"apptainer_module": "apptainer/1.4.1",
       "slurm": {"account": "acct_demo", "partition": "cpu_part",
                 "gpu": {"partition": "gpu_part", "qos": "gpu_qos"}},
       "email": "someone@example.org"}

PER_ROW_FILES = {"params.env", "samples.csv", "run_local.sh", "run_all.sh", "HOWTO.md",
                 "stages/01_hisat2.sh", "stages/01_hisat2.sbatch",
                 "stages/02_samtools.sh", "stages/02_samtools.sbatch",
                 "stages/03_htseq_count.sh", "stages/03_htseq_count.sbatch"}

#: A `{NAME}` that is not the `{NAME}` of a shell `${NAME}` — an unbound placeholder.
UNBOUND = re.compile(r"(?<!\$)\{[A-Z]")


def _record(**kw) -> pr.PipelineRecord:
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", **kw)


def _linear(**kw) -> pr.PipelineRecord:
    return pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="rnaseq_one", **kw)


def _sbatch_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("#SBATCH")]


# ===========================================================================
# The file set and the header every file carries
# ===========================================================================


class TestFileSet:
    def test_a_per_row_pipeline_renders_the_full_set_with_a_samplesheet(self):
        files = render_plain(_record())
        assert set(files) == PER_ROW_FILES

    def test_a_linear_pipeline_renders_the_same_set_without_a_samplesheet(self):
        files = render_plain(_linear())
        assert set(files) == PER_ROW_FILES - {"samples.csv"}

    def test_every_file_names_the_pipeline_and_the_sealed_workflow_and_says_it_is_rendered(self):
        files = render_plain(_record())
        for name, text in files.items():
            if name == "samples.csv":
                continue                      # a CSV cannot carry a comment: its header is line 1
            lines = text.splitlines()
            first = lines[1] if lines[0].startswith("#!") else lines[0]
            assert "rnaseq_counts" in first and "rnaseq_counts_workflow" in first, name
            assert "rendered from sealed workflow" in first, name
            if name != "params.env":
                assert "do not hand-edit; edit params.env / samples.csv" in first, name
        for name in ("run_local.sh", "run_all.sh", "stages/01_hisat2.sh", "stages/01_hisat2.sbatch"):
            assert files[name].startswith("#!/usr/bin/env bash\n"), name

    def test_the_render_is_pure_and_deterministic(self):
        rec = _record()
        assert render_plain(rec, env=ENV) == render_plain(rec, env=ENV)


# ===========================================================================
# Stage scripts: the sealed commands with placeholders bound through the record
# ===========================================================================


class TestStageScripts:
    def test_the_align_stage_is_the_sealed_command_with_placeholders_as_shell_variables(self):
        sh = render_plain(_record())["stages/01_hisat2.sh"]
        lines = sh.splitlines()
        assert lines[0] == "#!/usr/bin/env bash"
        assert "set -euo pipefail" in lines
        assert lines[-1] == ("hisat2 -p 4 -x ${HISAT2_INDEX} -U ${READS} | "
                             "samtools sort -o ${OUTPUT_DIR}/aligned.bam")
        assert f"# image: bioinf_rnaseq_cli:latest (digest {DIGEST})" in lines
        assert "# derives from sealed steps 2, 5, 8" in lines
        assert "# measured on test data: wall 68.0s, peak RSS 1570 MB (authoritative)" in lines
        assert "# expects (set by its caller; this script sources nothing): HISAT2_INDEX OUTPUT_DIR READS" in lines
        assert "# writes into OUTPUT_DIR: aligned.bam [published]" in lines

    def test_the_count_stage_binds_a_value_param_a_path_param_and_a_column_inside_an_artifact(self):
        sh = render_plain(_record())["stages/03_htseq_count.sh"]
        assert sh.splitlines()[-1] == ("htseq-count -s ${STRANDED} -f bam ${OUTPUT_DIR}/aligned.bam "
                                       "${GTF} > ${OUTPUT_DIR}/${SAMPLE}.counts.tsv")
        assert "# writes into OUTPUT_DIR: ${SAMPLE}.counts.tsv [published]" in sh

    def test_a_stage_guards_every_artifact_it_consumes_from_an_earlier_stage(self):
        files = render_plain(_record())
        assert ('[ -e "${OUTPUT_DIR}/aligned.bam" ] || { echo "stage SAMTOOLS needs aligned.bam '
                'from stage HISAT2; run that stage first" >&2; exit 3; }') in files["stages/02_samtools.sh"]
        count = files["stages/03_htseq_count.sh"]
        assert ('[ -e "${OUTPUT_DIR}/aligned.bam" ] || { echo "stage HTSEQ_COUNT needs aligned.bam '
                'from stage HISAT2; run that stage first" >&2; exit 3; }') in count
        assert ('[ -e "${OUTPUT_DIR}/aligned.bam.bai" ] || { echo "stage HTSEQ_COUNT needs '
                'aligned.bam.bai from stage SAMTOOLS; run that stage first" >&2; exit 3; }') in count
        # the guards come before the command, and the first stage has none
        assert count.index("needs aligned.bam") < count.index("htseq-count -s")
        assert "run that stage first" not in files["stages/01_hisat2.sh"]

    def test_a_glob_artifact_is_guarded_with_compgen(self):
        rec = _record()
        rec.stage("SAMTOOLS").inputs[0].artifact = "*.bam"
        sh = render_plain(rec)["stages/02_samtools.sh"]
        assert 'compgen -G "${OUTPUT_DIR}/*.bam" >/dev/null || { echo "stage SAMTOOLS needs *.bam' in sh

    def test_a_per_sample_identifier_the_record_folded_into_the_sample_column_binds_to_SAMPLE(self):
        rec = _record()
        # the how-to said {SAMPLE_ID}; the record's sheet still has one `sample` column
        rec.param("SAMPLE").name = "SAMPLE_ID"
        count = rec.stage("HTSEQ_COUNT")
        count.commands = [c.replace("{SAMPLE}", "{SAMPLE_ID}") for c in count.commands]
        count.outputs[0].artifact = "{SAMPLE_ID}.counts.tsv"
        for i in count.inputs:
            if i.name == "SAMPLE":
                i.name = "SAMPLE_ID"
        sh = render_plain(rec)["stages/03_htseq_count.sh"]
        assert "> ${OUTPUT_DIR}/${SAMPLE}.counts.tsv" in sh
        assert "SAMPLE_ID" not in sh

    def test_a_shell_variable_the_sealed_command_already_carried_is_left_alone(self):
        rec = _record()
        rec.stage("SAMTOOLS").commands = ["samtools index -@ ${NTHREADS:-1} {OUTPUT_DIR}/aligned.bam"]
        sh = render_plain(rec)["stages/02_samtools.sh"]
        assert "samtools index -@ ${NTHREADS:-1} ${OUTPUT_DIR}/aligned.bam" in sh

    def test_no_rendered_file_leaves_a_placeholder_unbound(self):
        for rec in (_record(), _linear()):
            for env in (None, ENV):
                for name, text in render_plain(rec, env=env).items():
                    hit = UNBOUND.search(text)
                    assert hit is None, f"{name}: unbound placeholder at {text[hit.start():hit.start() + 30]!r}"


# ===========================================================================
# params.env and samples.csv
# ===========================================================================


class TestParamsEnv:
    def test_shared_params_carry_the_sealed_defaults_with_their_source(self):
        env = render_plain(_record())["params.env"].splitlines()
        assert "STRANDED=reverse" in env
        assert f"GTF={GTF}" in env
        assert f"HISAT2_INDEX={INDEX}" in env
        assert ("# HISAT2_INDEX: a prefix (a family of files named after it); source: produced by "
                "sealed step 1 of the workflow; format hisat2_index — HISAT2 index prefix") in env
        assert "# GTF: a path; source: a declared how-to input; format gtf — gene annotation" in env
        assert "Each default is the value the sealed\n#    run was validated with (test data)" in "\n".join(env)
        # per-sample inputs live in the sheet, not here
        assert not any(ln.startswith(("READS=", "SAMPLE=")) for ln in env)

    def test_results_dir_is_overridable_and_the_sheet_is_named(self):
        env = render_plain(_record())["params.env"].splitlines()
        assert 'RESULTS_DIR="${RESULTS_DIR:-$PWD/results}"' in env
        assert "SAMPLESHEET=samples.csv" in env
        assert "SAMPLESHEET=" not in render_plain(_linear())["params.env"]

    def test_one_container_pair_per_stage_with_the_sif_left_empty_and_its_digest_named(self):
        env = render_plain(_record())["params.env"].splitlines()
        for stage in ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT"):
            assert f"IMAGE_{stage}=bioinf_rnaseq_cli:latest" in env
            assert f"SIF_{stage}=" in env
            assert (f"# SIF_{stage}: build from image digest {DIGEST} "
                    f"(freeze request {REQUEST_KEY})") in env

    def test_a_linear_pipeline_puts_its_per_sample_inputs_in_params_env_as_the_worked_example(self):
        """A one-row pipeline has no samplesheet to carry the seal's trial, so the
        per-sample values ride in params.env — set to the sealed run's own, the way the
        sheet's rows are in a per_row pipeline."""
        env = render_plain(_linear())["params.env"]
        assert "READS=/data/reads/SRR1039508_10K_R1.fastq.gz\n" in env and "SAMPLE=SRR1039508\n" in env
        assert "the sealed run's own trial — the worked example; replace them" in env
        assert "REQUIRED" not in env

    def test_a_per_sample_value_with_no_default_is_required(self):
        """A record that carries no value for a one-row per-sample input (one built by
        hand, or by an older derivation) renders the slot empty and says so; the
        pre-flight refuses to run with it empty."""
        rec = _linear()
        rec = rec.model_copy(update={"params": [
            p.model_copy(update={"default": None}) if p.kind == "per_sample" else p
            for p in rec.params]})
        env = render_plain(rec)["params.env"]
        assert "READS=\n" in env and "SAMPLE=\n" in env
        assert "REQUIRED — the record carries no default for a per-sample value" in env

    def test_samples_csv_is_the_record_module_rendering(self):
        rec = _record()
        assert render_plain(rec)["samples.csv"] == pr.render_samplesheet(rec)
        assert render_plain(rec)["samples.csv"].splitlines()[0] == "sample,reads"


# ===========================================================================
# The cluster job per stage
# ===========================================================================


class TestStageSbatch:
    def test_an_unsized_stage_carries_the_default_request_and_says_so(self):
        job = render_plain(_record())["stages/02_samtools.sbatch"]
        header = _sbatch_lines(job)
        assert header[:6] == ["#SBATCH --job-name=SAMTOOLS", "#SBATCH --time=4:00:00", "#SBATCH --mem=8G",
                              "#SBATCH --nodes=1", "#SBATCH --ntasks=1", "#SBATCH --cpus-per-task=1"]
        body = job.splitlines()
        i = body.index("#SBATCH --error=%x-%j.err")
        assert body[i + 1] == "# DEFAULT request — not sized for your data"
        assert body[i + 2] == "# measured on test data: wall 2.0s, peak RSS 40 MB (authoritative)"
        assert not any(ln.startswith("#SBATCH --array") for ln in header)   # the driver passes it

    def test_a_caller_sized_stage_carries_its_own_request_and_no_default_label(self):
        rec = _record(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "12:00:00"}})
        job = render_plain(rec)["stages/01_hisat2.sbatch"]
        assert "#SBATCH --time=12:00:00" in job and "#SBATCH --mem=32G" in job
        assert "#SBATCH --cpus-per-task=8" in job
        assert "DEFAULT request" not in job
        assert "# measured on test data: wall 68.0s, peak RSS 1570 MB (authoritative)" in job

    def test_a_partially_sized_stage_names_the_slots_the_default_filled(self):
        rec = _record(resources={"HISAT2": {"cpus": 8}})
        job = render_plain(rec)["stages/01_hisat2.sbatch"]
        assert "#SBATCH --cpus-per-task=8" in job and "#SBATCH --mem=8G" in job
        assert "# DEFAULT time, mem — the caller's request left them unsized" in job

    def test_the_env_merges_account_partition_email_and_the_apptainer_module(self):
        with_env = render_plain(_record(), env=ENV)["stages/01_hisat2.sbatch"]
        without = render_plain(_record())["stages/01_hisat2.sbatch"]
        for line in ("#SBATCH --account=acct_demo", "#SBATCH --partition=cpu_part",
                     "#SBATCH --mail-user=someone@example.org", "module purge",
                     "module load apptainer/1.4.1"):
            assert line in with_env.splitlines(), line
            assert line not in without.splitlines(), line
        assert "--account" not in without and "module" not in without

    def test_a_gpu_stage_gets_gres_the_gpu_placement_and_nv(self):
        rec = _record(resources={"HISAT2": {"cpus": 4, "mem": "16G", "time": "1:00:00", "gpus": 2}})
        job = render_plain(rec, env=ENV)["stages/01_hisat2.sbatch"]
        assert "#SBATCH --gres=gpu:2" in job
        assert "#SBATCH --partition=gpu_part" in job and "#SBATCH --qos=gpu_qos" in job
        assert 'apptainer exec --nv --cleanenv "${BIND[@]}"' in job
        cpu_job = render_plain(rec, env=ENV)["stages/02_samtools.sbatch"]
        assert "--nv" not in cpu_job and "--gres" not in cpu_job
        local = render_plain(rec)["run_local.sh"]
        assert 'docker run --rm --gpus all "${BIND[@]}"' in local
        assert local.count("--gpus all") == 1

    def test_the_job_runs_the_stage_script_inside_the_sif_with_explicit_variables(self):
        job = render_plain(_record(), env=ENV)["stages/03_htseq_count.sbatch"]
        assert ('apptainer exec --cleanenv "${BIND[@]}" --pwd "$OUTPUT_DIR" "$SIF_HTSEQ_COUNT" \\\n'
                '  env GTF="$GTF" OUTPUT_DIR="$OUTPUT_DIR" SAMPLE="$SAMPLE" STRANDED="$STRANDED" \\\n'
                '  bash "$PIPELINE_DIR/stages/03_htseq_count.sh"') in job
        assert 'cd "${SLURM_SUBMIT_DIR:-$(dirname "$(dirname "$(readlink -f "$0")")")}"' in job
        assert "source ./params.env" in job
        assert 'OUTPUT_DIR="$RESULTS_DIR/$SAMPLE"' in job
        assert 'sheet_row "$SLURM_ARRAY_TASK_ID"' in job
        assert 'echo "$rc" > "runs/$RUN_ID/HTSEQ_COUNT.$ROW.rc"' in job
        assert ('[ -n "${SIF_HTSEQ_COUNT:-}" ] || die "SIF_HTSEQ_COUNT is empty in params.env: '
                f'build the .sif from image digest {DIGEST} and set its absolute path"') in job

    def test_a_linear_job_is_not_an_array_and_writes_into_results_dir_itself(self):
        job = render_plain(_linear())["stages/01_hisat2.sbatch"]
        assert "SLURM_ARRAY_TASK_ID" not in job and "sheet_row" not in job
        assert 'ROW="single"' in job and 'OUTPUT_DIR="$RESULTS_DIR"' in job


# ===========================================================================
# The runners
# ===========================================================================


class TestRunners:
    def test_run_local_binds_the_stages_dir_read_only_the_results_dir_and_every_input_parent(self):
        local = render_plain(_record())["run_local.sh"]
        assert 'BIND=(-v "$PIPELINE_DIR/stages:$PIPELINE_DIR/stages:ro" -v "$RESULTS_DIR:$RESULTS_DIR")' in local
        assert 'add_bind "$(dirname "$HISAT2_INDEX")"   # HISAT2_INDEX (prefix)' in local
        assert 'add_bind "$(dirname "$GTF")"   # GTF (path)' in local
        assert 'add_bind "$(dirname "$READS")"   # READS (column, path)' in local
        assert 'add_bind "$(dirname "$STRANDED")"' not in local        # a value has no dir

    def test_run_local_passes_each_stage_exactly_the_variables_its_script_expects(self):
        local = render_plain(_record())["run_local.sh"]
        assert ('stage_HISAT2() {\n'
                '  docker run --rm "${BIND[@]}" -w "$OUTPUT_DIR" \\\n'
                '    -e HISAT2_INDEX="$HISAT2_INDEX" -e OUTPUT_DIR="$OUTPUT_DIR" -e READS="$READS" \\\n'
                '    "$IMAGE_HISAT2" bash "$PIPELINE_DIR/stages/01_hisat2.sh"\n'
                '}') in local
        assert '-e OUTPUT_DIR="$OUTPUT_DIR" \\\n    "$IMAGE_SAMTOOLS"' in local
        assert 'echo "$rc" > "$RUN_DIR/$1.$ROW.rc"' in local

    def test_run_all_submits_arrays_chained_with_afterok_and_records_jobs(self):
        driver = render_plain(_record())["run_all.sh"]
        assert '[ -z "$2" ] || dep=(--dependency="afterok:$2")' in driver
        assert '--array="1-$NROWS" ${dep[@]+"${dep[@]}"} "$script")"' in driver
        assert 'sbatch --parsable --export="ALL,RUN_ID=$RUN_ID"' in driver
        assert "printf 'stage\\tjob_id\\tarray_size\\n' > \"$RUN_DIR/jobs.tsv\"" in driver
        assert "HTSEQ_COUNT) script=stages/03_htseq_count.sbatch;;" in driver
        assert "rm -rf" not in driver and "rm -rf" not in render_plain(_record())["run_local.sh"]
        linear = render_plain(_linear())["run_all.sh"]
        assert "--array" not in linear


# ===========================================================================
# Refusals
# ===========================================================================


class TestRefusals:
    def test_a_cohort_stage_is_refused(self):
        rec = _record()
        rec.stage("HTSEQ_COUNT").scope = "cohort"
        with pytest.raises(ValueError, match="cohort stages are not supported yet in the plain form"):
            render_plain(rec)

    def test_a_default_with_whitespace_or_a_metacharacter_is_refused(self):
        rec = _record()
        rec.param("STRANDED").default = "rev erse"
        with pytest.raises(ValueError, match="default of STRANDED='rev erse' contains whitespace"):
            render_plain(rec)
        rec = _record()
        rec.param("GTF").default = "/data/a;rm -rf /"
        with pytest.raises(ValueError, match="shell metacharacter"):
            render_plain(rec)

    def test_a_relative_path_default_is_refused(self):
        rec = _record()
        rec.param("GTF").default = "annotation/chr22.gtf"
        with pytest.raises(ValueError, match="must be an absolute POSIX path"):
            render_plain(rec)

    def test_a_stage_name_that_is_not_upper_snake_is_refused(self):
        for bad in ("align-reads", "align", "Align_Reads"):
            rec = _record(stage_names=[bad, "INDEX", "COUNT"])
            with pytest.raises(ValueError, match=f"stage name '{bad}' must be UPPER_SNAKE_CASE"):
                render_plain(rec)
        rec = _record(stage_names=["align reads", "INDEX", "COUNT"])
        with pytest.raises(ValueError, match="stage name='align reads' must be alnum"):
            render_plain(rec)

    def test_a_placeholder_that_shadows_a_script_variable_is_refused(self):
        rec = _record()
        rec.param("GTF").name = "RESULTS_DIR"
        with pytest.raises(ValueError, match="param='RESULTS_DIR' collides with a variable"):
            render_plain(rec)

    def test_an_unsafe_artifact_and_an_unsafe_image_are_refused(self):
        rec = _record()
        rec.stage("SAMTOOLS").inputs[0].artifact = "aligned bam"
        with pytest.raises(ValueError, match="is not a safe filename"):
            render_plain(rec)
        rec = _record()
        rec.stage("SAMTOOLS").image = "img:latest; true"
        with pytest.raises(ValueError, match="image of stage SAMTOOLS"):
            render_plain(rec)

    def test_a_command_placeholder_the_record_does_not_know_is_refused(self):
        rec = _record()
        rec.stage("SAMTOOLS").commands = ["samtools index {OUTPUT_DIR}/aligned.bam -@ {THREADS}"]
        with pytest.raises(ValueError, match=r"names \{THREADS\}, which is neither a param"):
            render_plain(rec)

    def test_a_sample_identifier_that_is_not_a_plain_name_is_refused(self):
        rec = _record()
        rec.samplesheet.rows[0]["sample"] = "a/b"
        with pytest.raises(ValueError, match="must be a plain name"):
            render_plain(rec)

    def test_a_non_mapping_env_is_refused(self):
        with pytest.raises(ValueError, match="env must be a mapping"):
            render_plain(_record(), env="hpc")     # type: ignore[arg-type]


# ===========================================================================
# Executed: the scripts under bash with fake container runtimes and a fake scheduler
# ===========================================================================

#: Host-runnable stand-ins for the fixture's commands, keeping its artifact names so the
#: record derives with the same shape (aligned.bam → aligned.bam.bai → {SAMPLE}.counts.tsv).
RUNNABLE = [
    "cat {HISAT2_INDEX}.1.ht2 {READS} > {OUTPUT_DIR}/aligned.bam",
    "cp {OUTPUT_DIR}/aligned.bam {OUTPUT_DIR}/aligned.bam.bai",
    "wc -c {OUTPUT_DIR}/aligned.bam {GTF} > {OUTPUT_DIR}/{SAMPLE}.counts.tsv && test -n {STRANDED}",
]

FAKE_DOCKER = r"""#!/usr/bin/env bash
# fake docker: `docker run --rm [--gpus all] -v … -w DIR -e K=V … IMAGE bash SCRIPT` runs the
# command on the host, in DIR, with ONLY the -e variables in its environment.
[ "$1" = run ] || { echo "fake docker: only run is supported" >&2; exit 9; }
shift
printf '%s\n' "$*" >> "$FAKE_LOG"
envs=(); wd=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rm) shift;;
    -w) wd="$2"; shift 2;;
    -e) envs+=("$2"); shift 2;;
    --gpus|-v) shift 2;;
    *) break;;
  esac
done
shift   # the image
cd "$wd" && exec env -i PATH="$PATH" ${envs[@]+"${envs[@]}"} "$@"
"""

FAKE_APPTAINER = r"""#!/usr/bin/env bash
# fake apptainer: `apptainer exec [--nv] --cleanenv --bind … --pwd DIR SIF env K=V … bash SCRIPT`
[ "$1" = exec ] || { echo "fake apptainer: only exec is supported" >&2; exit 9; }
shift
printf '%s\n' "$*" >> "$FAKE_LOG"
wd=""
while [ $# -gt 0 ]; do
  case "$1" in
    --cleanenv|--nv) shift;;
    --pwd) wd="$2"; shift 2;;
    --bind) shift 2;;
    *) break;;
  esac
done
shift   # the .sif
cd "$wd" && exec env -i PATH="$PATH" "$@"
"""

FAKE_SBATCH = r"""#!/usr/bin/env bash
# fake sbatch --parsable: logs its arguments, prints the next job id.
printf '%s\n' "$*" >> "$FAKE_LOG"
n="$(cat "$FAKE_COUNTER" 2>/dev/null || echo 1000)"
n=$((n + 1)); echo "$n" > "$FAKE_COUNTER"; echo "$n"
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Pipeline:
    """A rendered pipeline on disk, its data, and the fakes on PATH."""

    def __init__(self, tmp_path: Path, record: pr.PipelineRecord, env=None):
        self.root = tmp_path / "pipeline"
        for name, text in render_plain(record, env=env).items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        data = tmp_path / "data"
        (data / "idx").mkdir(parents=True)
        (data / "idx" / "chr22.1.ht2").write_text("IDX\n")
        (data / "annotation").mkdir()
        (data / "annotation" / "chr22.gtf").write_text("chr22\tgene\n")
        (data / "reads").mkdir()
        for s in SAMPLES:
            (data / "reads" / f"{s}.fastq.gz").write_text(f"reads of {s}\n")
        self.data = data
        self.set_params(HISAT2_INDEX=str(data / "idx" / "chr22"),
                        GTF=str(data / "annotation" / "chr22.gtf"))
        if (self.root / "samples.csv").exists():
            self.write_sheet(["sample,reads"] + [f"{s},{data / 'reads' / (s + '.fastq.gz')}" for s in SAMPLES])
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_exec(bin_dir / "docker", FAKE_DOCKER)
        _write_exec(bin_dir / "apptainer", FAKE_APPTAINER)
        _write_exec(bin_dir / "sbatch", FAKE_SBATCH)
        self.log = tmp_path / "fake.log"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_LOG": str(self.log), "FAKE_COUNTER": str(tmp_path / "counter")}
        self.env.pop("RESULTS_DIR", None)

    def set_params(self, **values: str) -> None:
        lines = (self.root / "params.env").read_text().splitlines()
        for i, ln in enumerate(lines):
            key = ln.split("=", 1)[0]
            if "=" in ln and not ln.startswith("#") and key in values:
                lines[i] = f"{key}={values.pop(key)}"
        assert not values, f"not in params.env: {values}"
        (self.root / "params.env").write_text("\n".join(lines) + "\n")

    def write_sheet(self, lines: list[str]) -> None:
        (self.root / "samples.csv").write_text("\n".join(lines) + "\n")

    def run(self, *argv: str, **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", *argv], cwd=self.root, env={**self.env, **extra_env},
                              capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run_dir(self) -> Path:
        runs = sorted((self.root / "runs").iterdir())
        assert len(runs) == 1, runs
        return runs[0]

    @property
    def results(self) -> Path:
        return self.root / "results"


def _runnable_record(samples=None, **kw) -> pr.PipelineRecord:
    return pr.derive_pipeline_record(sealed_rnaseq_spec(samples, templates=RUNNABLE),
                                     name="runnable", stage_names=["ALIGN", "INDEX", "COUNT"], **kw)


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@needs_bash
@pytest.mark.integration
class TestEveryScriptParses:
    @pytest.mark.parametrize("shape", ["per_row", "linear"])
    @pytest.mark.parametrize("with_env", [False, True], ids=["no_env", "env"])
    def test_bash_n_accepts_every_rendered_script(self, tmp_path, shape, with_env):
        rec = _record() if shape == "per_row" else _linear()
        for name, text in render_plain(rec, env=ENV if with_env else None).items():
            if not name.endswith((".sh", ".sbatch")):
                continue
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            res = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
            assert res.returncode == 0, f"{name}: {res.stderr}"


@needs_bash
@pytest.mark.integration
class TestRunLocalExecutes:
    def test_every_row_runs_every_stage_in_its_own_directory_and_records_exit_codes(self, tmp_path):
        pipe = Pipeline(tmp_path, _runnable_record())
        res = pipe.run("run_local.sh")
        assert res.returncode == 0, res.stderr + res.stdout
        for s in SAMPLES:
            out = pipe.results / s
            assert (out / "aligned.bam").read_text() == f"IDX\nreads of {s}\n"
            assert (out / "aligned.bam.bai").read_text() == (out / "aligned.bam").read_text()
            assert (out / f"{s}.counts.tsv").read_text().strip().endswith("total")
        run_dir = pipe.run_dir()
        assert {p.name for p in run_dir.iterdir()} == (
            {"params.env", "samples.csv"} | {f"{st}.{s}.rc" for st in ("ALIGN", "INDEX", "COUNT") for s in SAMPLES})
        assert all(p.read_text().strip() == "0" for p in run_dir.glob("*.rc"))
        # row-major: every stage of row 1 before row 2
        calls = pipe.calls()
        assert len(calls) == 9
        assert "01_align.sh" in calls[0] and "02_index.sh" in calls[1] and "03_count.sh" in calls[2]
        assert f"-w {pipe.results}/SRR1039508" in calls[0] and f"-w {pipe.results}/SRR1039509" in calls[3]

    def test_the_container_sees_the_inputs_parents_the_results_dir_and_only_explicit_variables(self, tmp_path):
        pipe = Pipeline(tmp_path, _runnable_record())
        assert pipe.run("run_local.sh").returncode == 0
        first = pipe.calls()[0]
        for d in (pipe.data / "idx", pipe.data / "annotation", pipe.data / "reads"):
            assert f"-v {d}:{d}" in first, first
        assert f"-v {pipe.root}/stages:{pipe.root}/stages:ro" in first
        assert f"-v {pipe.results}:{pipe.results}" in first
        assert first.count("-v ") == 5                       # no duplicate mounts
        assert (f"-e HISAT2_INDEX={pipe.data}/idx/chr22 -e OUTPUT_DIR={pipe.results}/SRR1039508 "
                f"-e READS={pipe.data}/reads/SRR1039508.fastq.gz") in first
        assert "-e GTF" not in first                          # ALIGN does not use it

    def test_a_selected_stage_and_row_run_alone_and_a_missing_input_is_a_named_refusal(self, tmp_path):
        pipe = Pipeline(tmp_path, _runnable_record())
        res = pipe.run("run_local.sh", "--stages", "COUNT", "--rows", "SRR1039509")
        assert res.returncode != 0
        assert "stage COUNT needs aligned.bam from stage ALIGN; run that stage first" in res.stderr
        assert "stage COUNT failed for row SRR1039509 (rc=3)" in res.stderr
        assert (pipe.run_dir() / "COUNT.SRR1039509.rc").read_text().strip() == "3"
        assert len(pipe.calls()) == 1
        # the stages it needs, then the stage alone: the guard passes
        assert pipe.run("run_local.sh", "--stages", "ALIGN,INDEX", "--rows", "SRR1039509").returncode == 0
        assert pipe.run("run_local.sh", "--stages", "COUNT", "--rows", "SRR1039509").returncode == 0
        assert (pipe.results / "SRR1039509" / "SRR1039509.counts.tsv").exists()
        assert not (pipe.results / "SRR1039508").exists()

    def test_preflight_refuses_before_anything_runs(self, tmp_path):
        pipe = Pipeline(tmp_path, _runnable_record())
        pipe.write_sheet(["sample,reads", f"S1,{pipe.data}/reads/nope.fastq.gz"])
        res = pipe.run("run_local.sh")
        assert res.returncode == 2 and f"READS={pipe.data}/reads/nope.fastq.gz does not exist" in res.stderr
        pipe.write_sheet(["sample,fastq", f"S1,{pipe.data}/reads/SRR1039508.fastq.gz"])
        assert "samplesheet lacks the required column 'reads'" in pipe.run("run_local.sh").stderr
        pipe.write_sheet(["sample,reads", f"S1,{pipe.data}/reads/SRR1039508.fastq.gz",
                          f"S1,{pipe.data}/reads/SRR1039509.fastq.gz"])
        assert "samplesheet row 2 repeats sample S1" in pipe.run("run_local.sh").stderr
        pipe.write_sheet(["sample,reads", f"S1,{pipe.data}/reads/SRR1039508.fastq.gz"])
        pipe.set_params(STRANDED='"rev erse"')
        assert "STRANDED=rev erse contains whitespace or a shell metacharacter" in pipe.run("run_local.sh").stderr
        pipe.set_params(STRANDED="reverse", IMAGE_INDEX="")
        assert "IMAGE_INDEX is empty" in pipe.run("run_local.sh").stderr
        assert "unknown stage NOPE" in pipe.run("run_local.sh", "--stages", "NOPE").stderr
        assert pipe.calls() == [] and not (pipe.root / "runs").exists()

    def test_a_linear_pipeline_runs_its_one_implicit_row_from_params_env(self, tmp_path):
        pipe = Pipeline(tmp_path, _runnable_record(["SRR1039508"]))
        # params.env carries the seal's own trial (a path that does not exist here), so
        # the pre-flight names it; then the user's values run the one implicit row.
        res = pipe.run("run_local.sh")
        assert res.returncode == 2 and "READS=/data/reads/SRR1039508_10K_R1.fastq.gz does not exist" in res.stderr
        pipe.set_params(READS=str(pipe.data / "reads" / "SRR1039508.fastq.gz"), SAMPLE="S1")
        res = pipe.run("run_local.sh")
        assert res.returncode == 0, res.stderr
        assert (pipe.results / "S1.counts.tsv").exists()
        assert (pipe.run_dir() / "COUNT.single.rc").read_text().strip() == "0"
        assert "one implicit row; --rows does not apply" in pipe.run("run_local.sh", "--rows", "x").stderr


@needs_bash
@pytest.mark.integration
class TestClusterFilesExecute:
    def _with_sifs(self, tmp_path, record) -> Pipeline:
        pipe = Pipeline(tmp_path, record)
        sifs = {}
        for st in ("ALIGN", "INDEX", "COUNT"):
            p = tmp_path / f"{st.lower()}.sif"
            p.write_text("sif")
            sifs[f"SIF_{st}"] = str(p)
        pipe.set_params(**sifs)
        return pipe

    def test_the_driver_submits_one_array_per_stage_chained_with_afterok(self, tmp_path):
        pipe = self._with_sifs(tmp_path, _runnable_record())
        res = pipe.run("run_all.sh")
        assert res.returncode == 0, res.stderr
        calls = pipe.calls()
        run_dir = pipe.run_dir()
        assert len(calls) == 3
        assert calls[0] == (f"--parsable --export=ALL,RUN_ID={run_dir.name} --output={run_dir}/%x-%j.out "
                            f"--error={run_dir}/%x-%j.err --array=1-3 stages/01_align.sbatch")
        assert calls[1].endswith("--array=1-3 --dependency=afterok:1001 stages/02_index.sbatch")
        assert calls[2].endswith("--array=1-3 --dependency=afterok:1002 stages/03_count.sbatch")
        assert (run_dir / "jobs.tsv").read_text() == (
            "stage\tjob_id\tarray_size\nALIGN\t1001\t3\nINDEX\t1002\t3\nCOUNT\t1003\t3\n")
        assert (run_dir / "params.env").exists() and (run_dir / "samples.csv").exists()
        assert "sacct -j 1001,1002,1003" in res.stdout

    def test_selected_stages_chain_among_themselves(self, tmp_path):
        pipe = self._with_sifs(tmp_path, _runnable_record())
        assert pipe.run("run_all.sh", "--stages", "ALIGN,COUNT").returncode == 0
        calls = pipe.calls()
        assert len(calls) == 2
        assert "--dependency" not in calls[0] and calls[0].endswith("stages/01_align.sbatch")
        assert calls[1].endswith("--dependency=afterok:1001 stages/03_count.sbatch")

    def test_the_driver_refuses_a_missing_sif_and_a_bad_sheet_before_submitting(self, tmp_path):
        pipe = self._with_sifs(tmp_path, _runnable_record())
        pipe.set_params(SIF_INDEX="")
        assert "SIF_INDEX is empty" in pipe.run("run_all.sh").stderr
        pipe.set_params(SIF_INDEX=str(tmp_path / "missing.sif"))
        assert f"SIF_INDEX={tmp_path}/missing.sif does not exist" in pipe.run("run_all.sh").stderr
        assert "every samplesheet row is one array task" in pipe.run("run_all.sh", "--rows", "S1").stderr
        assert pipe.calls() == []

    def test_an_array_task_runs_its_row_of_the_sheet_inside_the_sif(self, tmp_path):
        pipe = self._with_sifs(tmp_path, _runnable_record())
        res = pipe.run("stages/01_align.sbatch", SLURM_SUBMIT_DIR=str(pipe.root),
                       SLURM_ARRAY_TASK_ID="2", RUN_ID="t42")
        assert res.returncode == 0, res.stderr
        assert (pipe.results / "SRR1039509" / "aligned.bam").read_text() == "IDX\nreads of SRR1039509\n"
        assert (pipe.root / "runs" / "t42" / "ALIGN.SRR1039509.rc").read_text().strip() == "0"
        call = pipe.calls()[0]
        assert call.startswith(f"--cleanenv --bind {pipe.root}/stages:{pipe.root}/stages:ro --bind {pipe.results} ")
        assert f"--bind {pipe.data}/idx" in call and f"--bind {pipe.data}/reads" in call
        assert f"--pwd {pipe.results}/SRR1039509 {tmp_path}/align.sif env HISAT2_INDEX={pipe.data}/idx/chr22" in call
        assert call.endswith(f"READS={pipe.data}/reads/SRR1039509.fastq.gz bash {pipe.root}/stages/01_align.sh")
        # outside SLURM the job script still finds the pipeline dir from its own location
        res = pipe.run("stages/02_index.sbatch", SLURM_ARRAY_TASK_ID="2")
        assert res.returncode == 0, res.stderr
        assert (pipe.root / "runs" / "manual" / "INDEX.SRR1039509.rc").read_text().strip() == "0"

    def test_an_array_task_refuses_a_missing_task_id_an_empty_sif_and_a_missing_input(self, tmp_path):
        pipe = self._with_sifs(tmp_path, _runnable_record())
        res = pipe.run("stages/03_count.sbatch", SLURM_SUBMIT_DIR=str(pipe.root))
        assert res.returncode == 2 and "this stage is a job array" in res.stderr
        res = pipe.run("stages/03_count.sbatch", SLURM_SUBMIT_DIR=str(pipe.root), SLURM_ARRAY_TASK_ID="1", RUN_ID="t1")
        assert res.returncode == 3 and "stage COUNT needs aligned.bam from stage ALIGN" in res.stderr
        assert (pipe.root / "runs" / "t1" / "COUNT.SRR1039508.rc").read_text().strip() == "3"
        pipe.set_params(SIF_COUNT="")
        res = pipe.run("stages/03_count.sbatch", SLURM_SUBMIT_DIR=str(pipe.root), SLURM_ARRAY_TASK_ID="1")
        assert res.returncode == 2 and "SIF_COUNT is empty in params.env" in res.stderr
