"""pipeline_render — the pipeline directory and its honesty lint.

The module under test writes nothing it did not lint: every command either item will
execute must be its sealed template with only the placeholders rebound, and a directory
holding the user's edits is never replaced silently. These tests pin the locked file
set, the manifest, the lint's refusals (a tampered flag, a dropped command, an added
command, a samplesheet that is not the record's), and the overwrite policy — through
the public functions, on the model-built fixture.
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
from agent.skills.pipeline_render import (COMMANDS_FILENAME, MANIFEST_FILENAME, PAGE_FILENAME,
                                          PipelineRenderError, check_rendered_commands,
                                          command_matches_template, parse_manifest,
                                          render_pipeline_dir, render_pipeline_files)
from pipeline_fixtures import sealed_rnaseq_spec

#: The directory, locked: what every rendered pipeline holds and nothing else.
PER_ROW_FILES = {"pipeline.html", "pipeline.yaml", "commands.sh", "samples.csv", "params.yaml",
                 "main.nf", "nextflow.config", "launcher.sh", "MANIFEST.sha256"}
LINEAR_FILES = PER_ROW_FILES - {"samples.csv"}


@pytest.fixture
def record():
    return derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts")


@pytest.fixture
def linear():
    return derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one")


# ── the file set ────────────────────────────────────────────────────────────


class TestFileSet:
    def test_the_locked_directory(self, record, linear):
        assert set(render_pipeline_files(record)) == PER_ROW_FILES
        assert set(render_pipeline_files(linear)) == LINEAR_FILES

    def test_the_record_on_disk_round_trips(self, record):
        import yaml
        raw = yaml.safe_load(render_pipeline_files(record)["pipeline.yaml"])
        assert raw["name"] == "rnaseq_counts" and raw["shape"] == "per_row"
        assert [s["name"] for s in raw["stages"]] == ["HISAT2", "SAMTOOLS", "HTSEQ_COUNT"]

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


def _tampered(module_attr: str, mutate):
    """A renderer whose output is edited after the fact — what the lint exists to catch."""
    real = getattr(pr, module_attr)

    def tampered(record, **kw):
        out = real(record, **kw)
        return mutate(out)
    return tampered


class TestLint:
    def test_the_rendered_items_pass(self, record):
        files = {COMMANDS_FILENAME: pr.render_commands(record), **pr.render_nextflow(record)}
        check_rendered_commands(record, files)

    def test_a_flag_changed_in_commands_sh_is_refused(self, record, monkeypatch):
        monkeypatch.setattr(pr, "render_commands",
                            _tampered("render_commands", lambda sh: sh.replace("-p 4", "-p 8")))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record)
        assert ei.value.code == "pipeline.render_drift"
        assert "-p 8" in ei.value.error and "-p 4" in ei.value.error

    def test_a_command_added_to_a_stage_block_is_refused(self, record, monkeypatch):
        def mutate(sh):
            return sh.replace("samtools index ${OUTPUT_DIR}/aligned.bam\n",
                              "samtools index ${OUTPUT_DIR}/aligned.bam\nrm -rf ${OUTPUT_DIR}\n")
        monkeypatch.setattr(pr, "render_commands", _tampered("render_commands", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record)
        assert ei.value.code == "pipeline.render_drift" and "SAMTOOLS" in ei.value.error

    def test_a_stage_marker_dropped_from_commands_sh_is_refused(self, record, monkeypatch):
        monkeypatch.setattr(pr, "render_commands",
                            _tampered("render_commands", lambda sh: sh.replace("# 2. SAMTOOLS", "# samtools")))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record)
        assert ei.value.code == "pipeline.render_drift" and "SAMTOOLS" in ei.value.error

    def test_a_process_script_edited_in_main_nf_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["main.nf"] = files["main.nf"].replace("-f bam", "-f sam")
            return files
        monkeypatch.setattr(pr, "render_nextflow", _tampered("render_nextflow", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record)
        assert ei.value.code == "pipeline.render_drift" and "HTSEQ_COUNT" in ei.value.error

    def test_a_process_dropped_from_main_nf_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["main.nf"] = files["main.nf"].replace("process SAMTOOLS {", "process SAMTOOLS_X {")
            return files
        monkeypatch.setattr(pr, "render_nextflow", _tampered("render_nextflow", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record)
        assert ei.value.code == "pipeline.render_drift" and "SAMTOOLS" in ei.value.error

    def test_a_samplesheet_that_is_not_the_records_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["samples.csv"] = files["samples.csv"] + "extra,/x.fq\n"
            return files
        monkeypatch.setattr(pr, "render_nextflow", _tampered("render_nextflow", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record)
        assert ei.value.code == "pipeline.render_drift" and "samples.csv" in ei.value.error

    def test_an_item_that_refuses_the_record_surfaces_as_one_family(self, record, monkeypatch):
        def refusing(record, **kw):
            raise ValueError("cannot carry a cohort stage")
        monkeypatch.setattr(pr, "render_nextflow", refusing)
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record)
        assert ei.value.code == "pipeline.form_refused" and "cohort" in ei.value.error

    def test_an_item_rendering_a_reserved_file_is_refused(self, record, monkeypatch):
        def mutate(files):
            files["pipeline.yaml"] = "name: forged\n"
            return files
        monkeypatch.setattr(pr, "render_nextflow", _tampered("render_nextflow", mutate))
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_files(record)
        assert ei.value.code == "pipeline.render_drift"


# ── the directory ───────────────────────────────────────────────────────────


class TestDirectory:
    def test_writes_every_file_and_makes_scripts_executable(self, record, tmp_path):
        d = tmp_path / "rnaseq_counts"
        out = render_pipeline_dir(record, d)
        assert Path(out["dir"]) == d
        assert set(out["files"]) == PER_ROW_FILES
        for rel in out["files"]:
            assert (d / rel).is_file(), rel
            if rel.endswith(".sh"):
                assert os.access(d / rel, os.X_OK), rel
        assert out["replaced_previous_render"] is False and out["removed"] == []
        assert load_pipeline_record(d / "pipeline.yaml").name == "rnaseq_counts"
        assert (d / "samples.csv").read_text() == render_samplesheet(record)
        assert Path(out["page"]) == d / PAGE_FILENAME

    def test_never_creates_results_runs_or_work(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        assert not any((d / sub).exists() for sub in ("results", "runs", "work"))

    def test_re_rendering_an_unedited_directory_replaces_it(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        out = render_pipeline_dir(record, d)
        assert out["replaced_previous_render"] is True and out["removed"] == []

    def test_a_re_render_removes_files_it_no_longer_produces_and_nothing_else(self, record, linear, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        (d / "runs" / "r1").mkdir(parents=True)
        (d / "runs" / "r1" / "trace.txt").write_text("task\n")
        (d / "my_notes.txt").write_text("mine\n")
        out = render_pipeline_dir(linear, d)
        assert out["removed"] == ["samples.csv"]
        assert not (d / "samples.csv").exists()
        assert (d / "runs" / "r1" / "trace.txt").exists() and (d / "my_notes.txt").exists()

    def test_an_edited_file_refuses_the_re_render_and_names_it(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        (d / "params.yaml").write_text((d / "params.yaml").read_text() + "stranded: 'yes'\n")
        (d / "samples.csv").unlink()
        with pytest.raises(PipelineRenderError) as ei:
            render_pipeline_dir(record, d)
        assert ei.value.code == "pipeline.dir_edited"
        assert "params.yaml" in ei.value.error and "samples.csv" in ei.value.error
        assert "overwrite=True" in ei.value.remedy
        assert "stranded: 'yes'" in (d / "params.yaml").read_text()   # the refusal wrote nothing

    def test_overwrite_replaces_the_edit(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        (d / "params.yaml").write_text("edited\n")
        out = render_pipeline_dir(record, d, overwrite=True)
        assert out["replaced_previous_render"] is True
        assert (d / "params.yaml").read_text() != "edited\n"

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
        assert render_pipeline_dir(record, d)["replaced_previous_render"] is False

    def test_the_written_manifest_verifies_against_the_written_files(self, record, tmp_path):
        d = tmp_path / "p"
        render_pipeline_dir(record, d)
        for rel, digest in parse_manifest((d / MANIFEST_FILENAME).read_text()).items():
            assert hashlib.sha256((d / rel).read_bytes()).hexdigest() == digest, rel
