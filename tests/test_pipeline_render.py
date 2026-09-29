"""pipeline_render — the pipeline directory and its honesty lint.

The module under test writes nothing it did not lint: every command a form will execute
must be its sealed template with only the placeholders rebound, the forms must agree
about every shared file, and a directory holding the user's edits is never replaced
silently. These tests pin the file set, the manifest, the lint's refusals (a tampered
flag, a dropped command, an added command, a samplesheet that is not the record's), and
the overwrite policy — through the public functions, on the model-built fixture.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.skills import pipeline_render as pr
from agent.skills.pipeline_record import derive_pipeline_record, load_pipeline_record, render_samplesheet
from agent.skills.pipeline_render import (MANIFEST_FILENAME, PAGE_FILENAME, PipelineRenderError,
                                          check_rendered_commands, command_matches_template,
                                          parse_manifest, render_pipeline_dir, render_pipeline_files)
from pipeline_fixtures import sealed_rnaseq_spec

PLAIN_FILES = {"params.env", "samples.csv", "run_local.sh", "run_all.sh", "HOWTO.md",
               "stages/01_hisat2.sh", "stages/01_hisat2.sbatch",
               "stages/02_samtools.sh", "stages/02_samtools.sbatch",
               "stages/03_htseq_count.sh", "stages/03_htseq_count.sbatch"}
NEXTFLOW_FILES = {"main.nf", "nextflow.config", "params.yaml", "launcher.sh",
                  "nextflow_local.sh", "samples.csv"}
OWN_FILES = {"pipeline.yaml", PAGE_FILENAME, MANIFEST_FILENAME}


@pytest.fixture
def record():
    return derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts",
                                  forms=("plain", "nextflow"))


@pytest.fixture
def plain_record():
    return derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", forms=("plain",))


# ── the file set ────────────────────────────────────────────────────────────


class TestFileSet:
    def test_both_forms_plus_the_record_page_and_manifest(self, record):
        files = render_pipeline_files(record)
        assert set(files) == PLAIN_FILES | NEXTFLOW_FILES | OWN_FILES

    def test_forms_default_to_the_records_own(self, plain_record):
        files = render_pipeline_files(plain_record)
        assert set(files) == PLAIN_FILES | OWN_FILES
        assert "main.nf" not in files

    def test_forms_argument_overrides_the_record(self, plain_record):
        files = render_pipeline_files(plain_record, forms=("nextflow",))
        assert "main.nf" in files and "run_all.sh" not in files

    def test_unknown_form_is_refused_with_the_menu(self, record):
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("snakemake",))
        assert ei.value.code == "pipeline.unknown_form"
        assert "plain" in ei.value.remedy and "nextflow" in ei.value.remedy

    def test_the_record_on_disk_round_trips(self, record):
        files = render_pipeline_files(record)
        import yaml
        raw = yaml.safe_load(files["pipeline.yaml"])
        assert raw["name"] == "rnaseq_counts" and raw["shape"] == "per_row"
        assert [s["name"] for s in raw["stages"]] == ["HISAT2", "SAMTOOLS", "HTSEQ_COUNT"]

    def test_the_page_is_rendered_over_every_other_file(self, record):
        files = render_pipeline_files(record)
        page = files[PAGE_FILENAME]
        for name in (PLAIN_FILES | NEXTFLOW_FILES | {"pipeline.yaml", MANIFEST_FILENAME}):
            assert name in page, f"the page's file table does not list {name}"

    def test_rendering_is_deterministic(self, record):
        assert render_pipeline_files(record) == render_pipeline_files(record)


# ── the manifest ────────────────────────────────────────────────────────────


class TestManifest:
    def test_lists_every_file_but_itself_with_its_real_hash(self, record):
        files = render_pipeline_files(record)
        manifest = parse_manifest(files[MANIFEST_FILENAME])
        assert set(manifest) == set(files) - {MANIFEST_FILENAME}
        for path, digest in manifest.items():
            assert digest == hashlib.sha256(files[path].encode()).hexdigest(), path

    def test_the_page_lists_the_manifests_final_byte_size(self, record):
        """The page is rendered before the manifest can carry the page's hash; the
        stand-in it is rendered over must have the byte count the manifest ends up
        with, or the page's own file table is wrong about one file."""
        files = render_pipeline_files(record)
        size = len(files[MANIFEST_FILENAME].encode())
        assert f"{size:,}" in files[PAGE_FILENAME] or str(size) in files[PAGE_FILENAME]

    def test_malformed_manifest_line_is_a_value_error(self):
        with pytest.raises(ValueError):
            parse_manifest("not a manifest\n")

    @pytest.mark.integration
    def test_sha256sum_c_passes_on_the_written_directory(self, record, tmp_path):
        tool = shutil.which("sha256sum") or shutil.which("shasum")
        if tool is None:
            pytest.skip("no sha256sum/shasum on PATH")
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        args = [tool, "-c", MANIFEST_FILENAME] if tool.endswith("sha256sum") \
            else [tool, "-a", "256", "-c", MANIFEST_FILENAME]
        run = subprocess.run(args, cwd=d, capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr


# ── the honesty lint ────────────────────────────────────────────────────────


HISAT = "hisat2 -p 4 -x {HISAT2_INDEX} -U {READS} | samtools sort -o {OUTPUT_DIR}/aligned.bam"
COUNT = "htseq-count -s {STRANDED} -f bam {OUTPUT_DIR}/aligned.bam {GTF} > {OUTPUT_DIR}/{SAMPLE}.counts.tsv"
SLOTS = ["OUTPUT_DIR"]


class TestCommandMatchesTemplate:
    @pytest.mark.parametrize("rendered", [
        "hisat2 -p 4 -x ${HISAT2_INDEX} -U ${READS} | samtools sort -o ${OUTPUT_DIR}/aligned.bam",
        "hisat2 -p 4 -x ${file(params.hisat2_index).name} -U ${reads} | samtools sort -o aligned.bam",
        "  hisat2  -p 4 -x X -U Y | samtools sort -o Z  ",
    ])
    def test_accepts_any_spelling_of_a_binding(self, rendered):
        assert command_matches_template(rendered, HISAT, SLOTS)

    @pytest.mark.parametrize("rendered", [
        "hisat2 -p 8 -x ${HISAT2_INDEX} -U ${READS} | samtools sort -o ${OUTPUT_DIR}/aligned.bam",  # a flag changed
        "hisat2 -p 4 -x ${HISAT2_INDEX} -U ${READS} | samtools sort -@ 4 -o ${OUTPUT_DIR}/aligned.bam",  # an option added
        "hisat2 -p 4 -x ${HISAT2_INDEX} -U ${READS} > ${OUTPUT_DIR}/aligned.bam",  # the pipe rewritten
        "hisat2 -p 4 -x ${HISAT2_INDEX} -U ${READS} | samtools sort -o ${OUTPUT_DIR}/aligned.bam && echo done",
    ])
    def test_rejects_any_change_outside_the_bindings(self, rendered):
        assert not command_matches_template(rendered, HISAT, SLOTS)

    def test_an_artifact_with_a_placeholder_inside_is_one_binding_unit(self):
        assert command_matches_template(
            "htseq-count -s ${params.stranded} -f bam aligned.bam ${gtf} > ${meta.sample}.counts.tsv",
            COUNT, SLOTS)
        assert not command_matches_template(
            "htseq-count -s ${params.stranded} -f sam aligned.bam ${gtf} > ${meta.sample}.counts.tsv",
            COUNT, SLOTS)


def _tampering(form: str, mutate):
    """A form renderer whose output is edited after the fact — what the lint exists to catch."""
    real = pr.FORM_RENDERERS[form]

    def tampered(record, *, env=None):
        files = real(record, env=env)
        return mutate(files)
    return tampered


class TestLint:
    def test_the_rendered_forms_pass(self, record):
        files = {}
        for form in ("plain", "nextflow"):
            files.update(pr.FORM_RENDERERS[form](record))
        check_rendered_commands(record, files, ("plain", "nextflow"))

    def test_a_flag_changed_in_a_stage_script_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["stages/01_hisat2.sh"] = files["stages/01_hisat2.sh"].replace("-p 4", "-p 8")
            return files
        monkeypatch.setitem(pr.FORM_RENDERERS, "plain", _tampering("plain", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("plain",))
        assert ei.value.code == "pipeline.render_drift"
        assert "-p 8" in ei.value.error and "-p 4" in ei.value.error

    def test_a_command_added_to_a_stage_script_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["stages/02_samtools.sh"] += "rm -rf ${OUTPUT_DIR}\n"
            return files
        monkeypatch.setitem(pr.FORM_RENDERERS, "plain", _tampering("plain", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("plain",))
        assert ei.value.code == "pipeline.render_drift"

    def test_a_process_script_edited_in_main_nf_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["main.nf"] = files["main.nf"].replace("-f bam", "-f sam")
            return files
        monkeypatch.setitem(pr.FORM_RENDERERS, "nextflow", _tampering("nextflow", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("nextflow",))
        assert ei.value.code == "pipeline.render_drift"
        assert "HTSEQ_COUNT" in ei.value.error

    def test_a_process_dropped_from_main_nf_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["main.nf"] = files["main.nf"].replace("process SAMTOOLS {", "process SAMTOOLS_X {")
            return files
        monkeypatch.setitem(pr.FORM_RENDERERS, "nextflow", _tampering("nextflow", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("nextflow",))
        assert ei.value.code == "pipeline.render_drift"
        assert "SAMTOOLS" in ei.value.error

    def test_a_samplesheet_that_is_not_the_records_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["samples.csv"] = files["samples.csv"] + "extra,/x.fq\n"
            return files
        monkeypatch.setitem(pr.FORM_RENDERERS, "plain", _tampering("plain", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("plain",))
        assert ei.value.code == "pipeline.render_drift"
        assert "samples.csv" in ei.value.error

    def test_forms_disagreeing_about_a_shared_file_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["samples.csv"] = files["samples.csv"].replace("\n", "\r\n")
            return files
        monkeypatch.setitem(pr.FORM_RENDERERS, "nextflow", _tampering("nextflow", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("plain", "nextflow"))
        assert ei.value.code == "pipeline.render_drift"
        assert "disagree" in ei.value.error

    def test_a_form_that_refuses_the_record_surfaces_as_one_family(self, record, monkeypatch):
        def refusing(record, *, env=None):
            raise ValueError("cannot carry a cohort stage; render the plain form")
        monkeypatch.setitem(pr.FORM_RENDERERS, "nextflow", refusing)
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("nextflow",))
        assert ei.value.code == "pipeline.form_refused"
        assert "cohort" in ei.value.error

    def test_a_form_rendering_a_reserved_file_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["pipeline.yaml"] = "name: forged\n"
            return files
        monkeypatch.setitem(pr.FORM_RENDERERS, "plain", _tampering("plain", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record, forms=("plain",))
        assert ei.value.code == "pipeline.render_drift"


# ── the directory ───────────────────────────────────────────────────────────


class TestDirectory:
    def test_writes_every_file_and_makes_scripts_executable(self, record, tmp_path):
        d = tmp_path / "rnaseq_counts"
        out = render_pipeline_dir(record, d)
        assert Path(out["dir"]) == d
        assert set(out["files"]) == PLAIN_FILES | NEXTFLOW_FILES | OWN_FILES
        for rel in out["files"]:
            assert (d / rel).is_file(), rel
            if rel.endswith(".sh"):
                assert os.access(d / rel, os.X_OK), rel
        assert out["forms"] == ["plain", "nextflow"]
        assert out["replaced_previous_render"] is False and out["removed"] == []
        assert load_pipeline_record(d / "pipeline.yaml").name == "rnaseq_counts"
        assert (d / "samples.csv").read_text() == render_samplesheet(record)

    def test_never_creates_runs(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        assert not (d / "runs").exists()

    def test_re_rendering_an_unedited_directory_replaces_it(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        out = render_pipeline_dir(record, d)
        assert out["replaced_previous_render"] is True and out["removed"] == []

    def test_dropping_a_form_removes_its_files_and_nothing_else(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        (d / "runs" / "r1").mkdir(parents=True)
        (d / "runs" / "r1" / "jobs.tsv").write_text("stage\tjob\n")
        (d / "my_notes.txt").write_text("mine\n")
        out = render_pipeline_dir(record, d, forms=("plain",))
        assert set(out["removed"]) == {"main.nf", "nextflow.config", "params.yaml",
                                       "launcher.sh", "nextflow_local.sh"}
        assert not (d / "main.nf").exists()
        assert (d / "runs" / "r1" / "jobs.tsv").exists() and (d / "my_notes.txt").exists()

    def test_an_edited_file_refuses_the_re_render_and_names_it(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        (d / "params.env").write_text((d / "params.env").read_text() + "STRANDED=yes\n")
        (d / "samples.csv").unlink()
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_dir(record, d)
        assert ei.value.code == "pipeline.dir_edited"
        assert "params.env" in ei.value.error and "samples.csv" in ei.value.error
        assert "overwrite=True" in ei.value.remedy
        # the refusal wrote nothing: the edit is still there
        assert "STRANDED=yes" in (d / "params.env").read_text()

    def test_overwrite_replaces_the_edit(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        (d / "params.env").write_text("edited\n")
        out = render_pipeline_dir(record, d, overwrite=True)
        assert out["replaced_previous_render"] is True
        assert (d / "params.env").read_text() != "edited\n"

    def test_a_directory_that_is_not_a_render_is_refused(self, record, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        (d / "something.txt").write_text("not ours\n")
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_dir(record, d)
        assert ei.value.code == "pipeline.dir_not_a_render"
        assert (d / "something.txt").exists() and not (d / "pipeline.yaml").exists()

    def test_a_corrupt_manifest_is_refused_not_trusted(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        (d / MANIFEST_FILENAME).write_text("garbage\n")
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_dir(record, d)
        assert ei.value.code == "pipeline.dir_not_a_render"

    def test_an_empty_existing_directory_is_fine(self, record, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        out = render_pipeline_dir(record, d)
        assert out["replaced_previous_render"] is False

    def test_the_written_manifest_verifies_against_the_written_files(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        manifest = parse_manifest((d / MANIFEST_FILENAME).read_text())
        for rel, digest in manifest.items():
            assert hashlib.sha256((d / rel).read_bytes()).hexdigest() == digest, rel
