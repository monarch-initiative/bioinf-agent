"""The cohort layer: a second sealed workflow attached to a per-sample one as stages that
run ONCE over every sample, after the per-sample stage they collect from.

Pinned here, through the public functions on the model-built fixtures (`sealed_rnaseq_spec`
per sample, `sealed_deseq2_spec` over the cohort): the record composes the two how-tos
without inventing anything — the fan-in is DECLARED (`collect=`) and refused when it names
an artifact no per-sample stage writes or one not named after the sample; a `format:
samplesheet` input is the pipeline's own samples.csv, and the columns the sealed trial
sheet carries beyond the per-sample ones join the samplesheet with their example values;
an authored script is carried verbatim into `bin/` and its param points there. The
Nextflow files put the fan-in as `.collect()` with every row's copy staged into one
directory, give a cohort process no meta and a flat publish, and still pass the honesty
lint; the page draws the fan-in dashed and says which stages run once; the MCP primitive
takes `cohort=` and reads the trial sheets off disk so the derivation stays pure. The
last class previews the composed directory under the real nextflow binary.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from pipeline_fixtures import (DESEQ2_SCRIPT, DESEQ2_TEXT, DESIGN, DIGEST, DIGEST_DE, MERGE_SCRIPT, MERGE_TEXT,
                               REQUEST_KEY, REQUEST_KEY_DE, SHEET, TEMPLATES_DE, sealed_deseq2_spec,
                               sealed_rnaseq_spec, sheet_text)

from agent.skills import pipeline_record as pr
from agent.skills.pipeline_page_html import directory_tree, picture_layout, render_pipeline_page
from agent.skills.pipeline_render import PipelineRenderError, parse_manifest, render_pipeline_dir, render_pipeline_files
from agent.skills.pipeline_render_nextflow import bound_commands, render_nextflow

SAMPLES = list(DESIGN)
DE_PATH = "/ws/reports/rnaseq_de_workflow.workflow.yaml"
COLLECT = {"COUNTS_DIR": "{SAMPLE}.counts.tsv"}
_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


def _request(spec=None, **kw) -> pr.CohortRequest:
    return pr.CohortRequest(spec=spec or sealed_deseq2_spec(), spec_path=DE_PATH, spec_sha256="de" * 32,
                            collect=kw.pop("collect", COLLECT), **kw)


def _composed(cohort=None, samples=None, sheet_files=None, **kw) -> pr.PipelineRecord:
    return pr.derive_pipeline_record(
        sealed_rnaseq_spec(samples if samples is not None else SAMPLES), name="rnaseq_counts_de",
        spec_path="/ws/reports/rnaseq_counts_workflow.workflow.yaml",
        samplesheet_files=sheet_files if sheet_files is not None else {SHEET: sheet_text()},
        cohort=cohort if cohort is not None else [_request()], **kw)


def _refusal(**kw) -> pr.PipelineDerivationError:
    with pytest.raises(pr.PipelineDerivationError) as e:
        _composed(**kw)
    return e.value


# ── the record ────────────────────────────────────────────────────────────────


class TestRecord:
    def test_the_cohort_stages_follow_the_per_sample_ones_and_run_over_the_cohort(self):
        rec = _composed()
        assert [(s.name, s.index, s.scope) for s in rec.stages] == [
            ("HISAT2", 0, "per_sample"), ("SAMTOOLS", 1, "per_sample"), ("HTSEQ_COUNT", 2, "per_sample"),
            ("MERGE_COUNTS", 3, "cohort"), ("DESEQ2", 4, "cohort")]
        # the stage is named after the script, the tool says what runs it
        assert rec.stage("MERGE_COUNTS").tool == "Rscript merge_counts.R"
        assert rec.stage("DESEQ2").tool == "Rscript deseq2.R"
        assert rec.stage("MERGE_COUNTS").image_digest == DIGEST_DE and rec.stage("HISAT2").image_digest == DIGEST
        assert rec.env_digests == sorted([DIGEST, DIGEST_DE])
        assert rec.stage("MERGE_COUNTS").sealed_steps == [1] and rec.stage("DESEQ2").sealed_steps == [2]

    def test_the_fan_in_is_the_declared_collect_wired_to_the_per_sample_producer(self):
        rec = _composed()
        merge = rec.stage("MERGE_COUNTS")
        collect = [i for i in merge.inputs if i.origin == "collect"]
        assert [(i.name, i.from_stage, i.artifact) for i in collect] == [("COUNTS_DIR", "HTSEQ_COUNT", "{SAMPLE}.counts.tsv")]
        counts = next(o for o in rec.stage("HTSEQ_COUNT").outputs if o.artifact == "{SAMPLE}.counts.tsv")
        assert counts.consumed_by == ["MERGE_COUNTS"]
        assert "COUNTS_DIR" not in [p.name for p in rec.params]          # wiring, never a params.yaml key
        assert rec.cohort_workflows[0].model_dump() == {
            "sealed_workflow": "rnaseq_de_workflow", "sealed_workflow_path": DE_PATH, "sealed_workflow_sha256": "de" * 32,
            "stages": ["MERGE_COUNTS", "DESEQ2"],
            "collect": [{"placeholder": "COUNTS_DIR", "artifact": "{SAMPLE}.counts.tsv", "from_stage": "HTSEQ_COUNT",
                         "stage": "MERGE_COUNTS"}]}
        assert any("COUNTS_DIR = every sample's {SAMPLE}.counts.tsv from HTSEQ_COUNT" in n for n in rec.notes)

    def test_a_cohort_artifact_flows_between_cohort_stages_by_name(self):
        rec = _composed()
        de = rec.stage("DESEQ2")
        assert [(i.name, i.origin, i.from_stage) for i in de.inputs if i.origin == "stage"] == [
            ("counts_matrix.tsv", "stage", "MERGE_COUNTS")]
        assert next(o for o in rec.stage("MERGE_COUNTS").outputs).consumed_by == ["DESEQ2"]
        assert [o.artifact for o in de.outputs] == ["deseq2_results.tsv", "normalized_counts.tsv", "ma_plot.png",
                                                    "pca.png", "deseq2_session.txt"]
        assert de.consumes_workdir                                    # `{OUTPUT_DIR}` bare at the end

    def test_the_samplesheet_slot_adds_the_design_columns_with_the_sealed_sheets_values(self):
        rec = _composed()
        assert [(c.name, c.read_by) for c in rec.samplesheet.columns] == [
            ("sample", []), ("reads", []), ("donor", ["DESEQ2"]), ("condition", ["DESEQ2"])]
        assert [(r["sample"], r["donor"], r["condition"]) for r in rec.samplesheet.rows] == [
            (s, d, c) for s, (d, c) in DESIGN.items()]
        sheet = rec.param("SAMPLESHEET")
        assert sheet.kind == "samplesheet" and sheet.default == SHEET
        assert [i.origin for i in rec.stage("DESEQ2").inputs if i.name == "SAMPLESHEET"] == ["samplesheet"]
        assert any("column `condition`: read by DESEQ2 through SAMPLESHEET" in n for n in rec.notes)
        assert rec.param("DESIGN").kind == "shared" and rec.param("DESIGN").default == "condition"

    def test_a_sample_the_sealed_sheet_lacks_gets_empty_design_cells_and_a_note(self):
        rec = _composed(sheet_files={SHEET: sheet_text(SAMPLES[:2])})
        assert rec.samplesheet.rows[3] == {"sample": SAMPLES[3], "reads": f"/data/reads/{SAMPLES[3]}_10K_R1.fastq.gz",
                                           "donor": "", "condition": ""}
        assert any(f"no row for {SAMPLES[2:]}" in n for n in rec.notes)

    def test_scripts_are_carried_verbatim_and_their_params_point_into_bin(self):
        rec = _composed()
        assert [(sc.param, sc.name, sc.sealed_path, sc.content) for sc in rec.scripts] == [
            ("MERGE_SCRIPT", "merge_counts.R", MERGE_SCRIPT, MERGE_TEXT),
            ("DESEQ2_SCRIPT", "deseq2.R", DESEQ2_SCRIPT, DESEQ2_TEXT)]
        assert rec.scripts[0].sha256 == __import__("hashlib").sha256(MERGE_TEXT.encode()).hexdigest()
        p = rec.param("MERGE_SCRIPT")
        assert p.kind == "shared" and p.value_kind == "path"
        assert p.default == "bin/merge_counts.R" and p.source == "authored_artifact:merge_counts.R"
        assert p.used_by == ["MERGE_COUNTS"]

    def test_the_cohort_params_say_which_workflow_and_the_provenance_steps_their_workflow(self):
        rec = _composed()
        assert all(ps.workflow == "rnaseq_counts_workflow" for ps in rec.provenance_steps)
        assert any(d.key == "cohort" and "runs once" in d.value for d in rec.defaults)
        assert all(not p.source.startswith("sealed_step:") or p.source.endswith("@rnaseq_de_workflow")
                   for p in rec.params if p.name in ("MERGE_SCRIPT", "DESEQ2_SCRIPT", "DESIGN"))

    def test_the_composed_record_round_trips_through_yaml(self, tmp_path):
        rec = _composed()
        path = pr.write_pipeline_record(rec, tmp_path)
        assert pr.load_pipeline_record(path) == rec

    def test_a_one_sample_per_sample_seal_composes_too(self):
        rec = _composed(samples=SAMPLES[:1], sheet_files={SHEET: sheet_text(SAMPLES[:1])})
        assert len(rec.samplesheet.rows) == 1 and rec.samplesheet.rows[0]["condition"] == "untrt"
        assert [s.scope for s in rec.stages] == ["per_sample"] * 3 + ["cohort"] * 2

    def test_a_base_command_binding_no_per_sample_value_still_runs_per_sample(self):
        """The seal's self-test ran every how-to command once per trial."""
        from pipeline_fixtures import TEMPLATES
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(templates=[*TEMPLATES, "multiqc {OUTPUT_DIR}"]), name="x")
        assert [s.scope for s in rec.stages] == ["per_sample"] * 4


class TestRefusals:
    def test_a_collect_naming_an_artifact_no_per_sample_stage_writes_lists_them(self):
        e = _refusal(cohort=[_request(collect={"COUNTS_DIR": "counts.txt"})])
        assert e.code == "pipeline.collect_unknown_artifact"
        assert "['aligned.bam', 'aligned.bam.bai', '{SAMPLE}.counts.tsv']" in e.error
        assert "as the record spells it" in e.remedy

    def test_a_collect_of_an_artifact_not_named_after_the_sample_is_refused(self):
        e = _refusal(cohort=[_request(collect={"COUNTS_DIR": "aligned.bam"})])
        assert e.code == "pipeline.collect_not_unique"
        assert "overwrite each other" in e.error and "{SAMPLE}.counts.tsv" in e.remedy

    def test_a_collect_naming_a_placeholder_the_cohort_howto_lacks_is_refused(self):
        e = _refusal(cohort=[_request(collect={"COUNTS_DIR": "{SAMPLE}.counts.tsv", "NOPE": "{SAMPLE}.counts.tsv"})])
        assert e.code == "pipeline.collect_unknown_placeholder" and "['NOPE']" in e.error

    def test_a_fan_in_not_declared_is_a_per_sample_input_and_refused_as_such(self):
        """Without collect= the cohort how-to's directory input is just a shared path."""
        rec = _composed(cohort=[_request(collect={})])
        assert rec.param("COUNTS_DIR").kind == "shared"       # honest: the sealed directory, as a params.yaml key
        assert not any(i.origin == "collect" for s in rec.stages for i in s.inputs)

    def test_a_placeholder_both_howtos_use_is_refused(self):
        """The cohort how-to with its design input named STRANDED — a placeholder the
        per-sample how-to already owns; the sealed commands are unchanged, only the name."""
        from agent.models.core_data import WorkflowSpec
        d = sealed_deseq2_spec().model_dump()
        d["usage"]["command_template"] = [t.replace("{DESIGN}", "{STRANDED}") for t in d["usage"]["command_template"]]
        for i in d["usage"]["inputs"]:
            i["name"] = i["name"].replace("DESIGN", "STRANDED")
        for t in d["usage"]["trials"] + d["usage_verification"]["trials"]:
            t["substitutions"]["STRANDED"] = t["substitutions"].pop("DESIGN")
        e = _refusal(cohort=[_request(WorkflowSpec.model_validate(d))])
        assert e.code == "pipeline.param_collision" and "STRANDED" in e.error and "rnaseq_de_workflow" in e.remedy

    def test_a_cohort_howto_binding_a_value_per_trial_is_refused_naming_shared(self):
        subs = {"MERGE_SCRIPT": MERGE_SCRIPT, "COUNTS_DIR": "/data/de/counts", "DESEQ2_SCRIPT": DESEQ2_SCRIPT,
                "SAMPLESHEET": SHEET, "DESIGN": "condition"}
        spec = sealed_deseq2_spec(trials=[{"name": "a", "substitutions": subs},
                                          {"name": "b", "substitutions": {**subs, "DESIGN": "donor + condition"}}])
        spec.usage_verification = {"status": "not_attempted", "reason": "fixture", "trials": []}
        e = _refusal(cohort=[_request(spec)])
        assert e.code == "pipeline.cohort_binds_per_sample" and "['DESIGN']" in e.error
        rec = _composed(cohort=[_request(spec, shared=["DESIGN"])])
        assert rec.param("DESIGN").default == "condition"

    def test_a_samplesheet_slot_whose_sheet_was_not_handed_over_is_refused(self):
        e = _refusal(sheet_files={})
        assert e.code == "pipeline.samplesheet_slot_unread" and SHEET in e.error

    def test_a_trial_sheet_without_a_sample_column_is_refused(self):
        e = _refusal(sheet_files={SHEET: "id,condition\nx,untrt\n"})
        assert e.code == "pipeline.samplesheet_slot_no_key" and "['id', 'condition']" in e.error

    def test_a_script_the_seal_only_excerpted_is_refused(self):
        e = _refusal(cohort=[_request(sealed_deseq2_spec(script_text=None))])
        assert e.code == "pipeline.script_not_carried" and MERGE_SCRIPT in e.error


# ── the Nextflow files ────────────────────────────────────────────────────────


class TestNextflow:
    def test_the_file_set_gains_one_bin_file_per_script_verbatim(self):
        files = render_nextflow(_composed())
        assert set(files) == {"main.nf", "nextflow.config", "params.yaml", "samples.csv", "launcher.sh",
                              "bin/merge_counts.R", "bin/deseq2.R"}
        assert files["bin/merge_counts.R"] == MERGE_TEXT and files["bin/deseq2.R"] == DESEQ2_TEXT

    def test_a_cohort_process_collects_its_fan_in_into_one_directory_has_no_meta_and_publishes_flat(self):
        main = render_nextflow(_composed())["main.nf"]
        i = main.index("process MERGE_COUNTS {")
        block = main[main.rindex("// stage 4 of 5", 0, i): main.index("}\n", i) + 2]
        assert "// A cohort stage: runs ONCE, over every row, after HTSEQ_COUNT has finished for every sample." in block
        assert "tag {" not in block
        assert '    publishDir { "${params.outdir}" }, mode: \'copy\', overwrite: true\n' in block
        assert "    path 'counts_dir/*'" in block and "// every row's <sample>.counts.tsv, from HTSEQ_COUNT, in one directory" in block
        assert "    path merge_script\n" in block
        assert "    path('counts_matrix.tsv'), emit: counts_matrix_tsv\n" in block
        assert "    Rscript ${merge_script} counts_dir counts_matrix.tsv\n" in block
        assert "val(meta)" not in block

    def test_the_second_cohort_process_takes_the_first_ones_artifact_the_sheet_and_its_script(self):
        main = render_nextflow(_composed())["main.nf"]
        i = main.index("process DESEQ2 {")
        block = main[i: main.index("}\n", i) + 2]
        assert ("    input:\n    path 'counts_matrix.tsv'\n    path samplesheet"
                "            // the samplesheet itself, as this run read it\n    path deseq2_script\n") in block
        assert '    Rscript ${deseq2_script} counts_matrix.tsv ${samplesheet} --design "${params.design}" .\n' in block
        assert "    path('pca.png'), emit: pca_png\n" in block

    def test_the_workflow_block_wires_collect_the_sheet_and_the_scripts(self):
        main = render_nextflow(_composed())["main.nf"]
        assert ("    MERGE_COUNTS(HTSEQ_COUNT.out.counts_tsv.map { meta, f -> f }.collect(), file(params.merge_script))"
                "   // runs once, after every row's <sample>.counts.tsv from HTSEQ_COUNT\n") in main
        assert ("    DESEQ2(MERGE_COUNTS.out.counts_matrix_tsv, file(params.samplesheet, checkIfExists: true), "
                "file(params.deseq2_script))\n") in main
        assert ".map { r -> tuple([sample: r.sample, donor: r.donor, condition: r.condition], " in main
        assert "3 per-sample stage(s) run once per samples.csv row and 2 cohort stage(s) run once over every row" in main

    def test_params_yaml_and_main_nf_carry_the_cohort_params_and_point_at_bin(self):
        files = render_nextflow(_composed())
        assert "merge_script: bin/merge_counts.R\n" in files["params.yaml"]
        assert "deseq2_script: bin/deseq2.R\n" in files["params.yaml"]
        assert "design: condition\n" in files["params.yaml"]
        assert "# The samplesheet: one row per sample, columns sample, reads, donor, condition.\n" in files["params.yaml"]
        assert "params.merge_script = 'bin/merge_counts.R'\n" in files["main.nf"]
        assert ("// MERGE_SCRIPT — a path; an authored script, bin/merge_counts.R beside this file — edit it there; "
                "format r_script; merge_counts.R; used by MERGE_COUNTS.\n"
                "// The default is the script as the sealed run validated it, copied beside this file.\n") in files["main.nf"]
        assert "a cohort stage publishes flat into it." in files["main.nf"]
        assert files["params.yaml"].count("\nsamplesheet: ") == 1       # the slot is never its own key
        assert "counts_dir" not in files["params.yaml"]

    def test_the_config_names_each_stages_image_and_the_guard_names_every_stage(self):
        files = render_nextflow(_composed())
        for st, img in (("HISAT2", "bioinf_rnaseq_cli:latest"), ("MERGE_COUNTS", "bioinf_rnaseq_de:latest"),
                        ("DESEQ2", "bioinf_rnaseq_de:latest")):
            assert f"withName: '{st}' {{ container = '{img}' }}" in files["nextflow.config"]
        assert "['HISAT2', 'SAMTOOLS', 'HTSEQ_COUNT', 'MERGE_COUNTS', 'DESEQ2'].every" in files["main.nf"]

    def test_the_samplesheet_is_the_records_rendering_with_the_design_columns(self):
        csv = render_nextflow(_composed())["samples.csv"]
        assert csv.splitlines()[0] == "sample,reads,donor,condition"
        assert csv.splitlines()[2] == "SRR1039509,/data/reads/SRR1039509_10K_R1.fastq.gz,N61311,dex"

    def test_no_placeholder_survives_in_any_rendered_file(self):
        for name, text in render_nextflow(_composed()).items():
            assert not _PLACEHOLDER_RE.search(text), (name, _PLACEHOLDER_RE.findall(text))

    def test_bound_commands_are_the_cohort_script_lines(self):
        rec = _composed()
        assert bound_commands(rec, rec.stage("MERGE_COUNTS")) == ["Rscript ${merge_script} counts_dir counts_matrix.tsv"]
        assert bound_commands(rec, rec.stage("DESEQ2")) == [
            'Rscript ${deseq2_script} counts_matrix.tsv ${samplesheet} --design "${params.design}" .']

    def test_a_script_whose_name_is_not_a_bare_filename_is_refused(self):
        rec = _composed()
        rec.scripts[0].name = "../x.R"
        with pytest.raises(ValueError) as e:
            render_nextflow(rec)
        assert "not a bare filename" in str(e.value)

    def test_the_cluster_render_names_each_images_sif(self):
        rec = _composed(env_names={REQUEST_KEY: "rnaseq_cli", REQUEST_KEY_DE: "rnaseq_de"},
                        sif_paths={REQUEST_KEY: "/c/rnaseq_cli_1.sif", REQUEST_KEY_DE: "/c/rnaseq_de_2.sif"},
                        compute_env="hpc", modules=["apptainer/1.5.0", "nextflow/25.04.7"])
        cfg = render_nextflow(rec, env={"name": "hpc", "type": "ssh", "host": "h", "user": "u",
                                        "apptainer_module": "apptainer/1.5.0", "nextflow_module": "nextflow/25.04.7"})["nextflow.config"]
        assert "withName: 'DESEQ2' {\n                // the .sif built from image " + DIGEST_DE in cfg
        assert "container = '/c/rnaseq_de_2.sif'" in cfg and "container = '/c/rnaseq_cli_1.sif'" in cfg


# ── the directory and its lint ────────────────────────────────────────────────


class TestDirectory:
    def test_the_lint_accepts_the_cohort_bindings_and_the_manifest_lists_bin(self):
        files = render_pipeline_files(_composed())
        assert {"bin/merge_counts.R", "bin/deseq2.R"} <= set(files)
        assert {"bin/merge_counts.R", "bin/deseq2.R"} <= set(parse_manifest(files[".pipeline/MANIFEST.sha256"]))

    def test_scripts_are_written_executable(self, tmp_path):
        out = render_pipeline_dir(_composed(), tmp_path / "p")
        assert "bin/merge_counts.R" in out["files"]
        mode = (tmp_path / "p" / "bin" / "merge_counts.R").stat().st_mode
        assert mode & stat.S_IXUSR and (tmp_path / "p" / "bin" / "deseq2.R").read_text() == DESEQ2_TEXT

    def test_a_cohort_command_edited_in_main_nf_is_refused_by_the_lint(self, monkeypatch):
        from agent.skills import pipeline_render as mod
        real = mod.render_nextflow

        def tampered(record, **kw):
            files = real(record, **kw)
            files["main.nf"] = files["main.nf"].replace("Rscript ${merge_script}", "Rscript --vanilla ${merge_script}")
            return files
        monkeypatch.setattr(mod, "render_nextflow", tampered)
        with pytest.raises(PipelineRenderError) as e:
            render_pipeline_files(_composed())
        assert e.value.code == "pipeline.render_drift" and "MERGE_COUNTS" in e.value.error


# ── the page ──────────────────────────────────────────────────────────────────


def _section(html: str, sid: str) -> str:
    start = html.index(f'<section class="bx" id="{sid}"')
    return html[start: html.index("</section>", start) + len("</section>")]


#: The SVG attributes that address a node or an edge for the hover JS — keyed by the
#: record's own names, never shown to a reader.
_MACHINE_KEY_RE = re.compile(r' data-(?:id|name|from|to|artifacts)="[^"]*"')


class _Spoken(HTMLParser):
    """Everything a reader can see or hover: text nodes and the human-facing attributes."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in ("title", "aria-label") and v:
                self.parts.append(v)


def _spoken(html: str) -> str:
    p = _Spoken()
    p.feed(html)
    p.close()
    return "\n".join(p.parts)


class TestPage:
    def test_the_header_names_both_workflows_the_scripts_and_marks_the_cohort_stages(self):
        html = render_pipeline_page(_composed())
        head = html[html.index('<div class="head">'): html.index("</table></div>")]
        assert "sealed workflow <b>rnaseq_counts_workflow</b>" in head
        assert f"cohort stages from sealed workflow <b>rnaseq_de_workflow</b> — <code>{DE_PATH}</code>" in head
        assert "<code>bin/merge_counts.R</code>, <code>bin/deseq2.R</code> — authored scripts" in head
        assert '<code>MERGE_COUNTS</code> <span class="muted">(cohort)</span>' in head
        assert "every stage runs inside it" not in head                 # two images

    def test_the_picture_draws_the_fan_in_dashed_and_labelled_every_samples_file(self):
        rec = _composed()
        layout = picture_layout(rec)
        collect = [e for e in layout.edges if e.kind == "collect"]
        assert [(e.src, e.dst, e.label) for e in collect] == [("s:HTSEQ_COUNT", "s:MERGE_COUNTS", "every sample's <sample>.counts.tsv")]
        assert [e.label for e in layout.edges if e.kind == "stage" and e.dst == "s:DESEQ2"] == ["counts_matrix.tsv"]
        assert layout.rows == [["HISAT2"], ["SAMTOOLS"], ["HTSEQ_COUNT"], ["MERGE_COUNTS"], ["DESEQ2"]]
        svg = render_pipeline_page(rec)
        svg = svg[svg.index('<svg id="pipeline-picture"'): svg.index("</svg>")]
        assert 'class="edge collect" data-edge="collect" data-from="s:HTSEQ_COUNT" data-to="s:MERGE_COUNTS"' in svg
        assert 'class="node stage unsized cohort"' in svg
        assert "Rscript merge_counts.R · cohort" in svg

    def test_the_design_columns_are_wired_to_the_stage_that_reads_the_sheet(self):
        layout = picture_layout(_composed())
        into_de = sorted(e.src for e in layout.edges if e.kind == "input" and e.dst == "s:DESEQ2")
        assert into_de == ["c:condition", "c:donor", "p:DESEQ2_SCRIPT", "p:DESIGN"]
        assert not any(e.src == "c:SAMPLE" for e in layout.edges)        # the row key is never wired
        node = next(n for n in layout.nodes if n.id == "c:condition")
        assert "read by DESEQ2 through the samplesheet" in node.tooltip

    def test_the_legend_says_what_a_cohort_stage_is(self):
        sec = _section(render_pipeline_page(_composed()), "picture")
        assert "a <b>cohort</b> stage (thick border) runs once, over every row, after the stage it collects from" in sec
        assert "A dashed cyan line: every sample's copy of a file, collected into one cohort stage." in sec
        assert "Every stage runs once per row" not in sec

    def test_the_directory_lists_the_scripts_and_the_flat_results(self):
        rec = _composed()
        entries = {e.name: e for e in directory_tree(rec)}
        assert entries["bin/merge_counts.R"].group == "files" and entries["bin/merge_counts.R"].role == "script"
        assert "results/" in entries and "published flat beside the per-sample directories" in entries["results/"].what
        sec = _section(render_pipeline_page(rec), "directory")
        assert "Copy these files (<code>bin/</code> with them) into the directory" in sec

    def test_the_example_samplesheet_carries_the_design_columns_and_says_who_reads_them(self):
        sec = _section(render_pipeline_page(_composed()), "samplesheet")
        assert "<pre>sample,reads,donor,condition\nSRR1039508,/data/reads/SRR1039508_10K_R1.fastq.gz,N61311,untrt</pre>" in sec
        assert "<code>condition</code> — a value · read by DESEQ2 through the samplesheet" in sec

    def test_the_stages_table_says_which_run_once_and_after_what(self):
        sec = _section(render_pipeline_page(_composed()), "stages")
        assert "<th>Runs</th>" in sec
        assert "<td>once per sample</td>" in sec
        assert "<td>once over the cohort, after HTSEQ_COUNT</td>" in sec
        assert "<td>once over the cohort</td>" in sec
        assert 'Rscript ${deseq2_script} counts_matrix.tsv ${samplesheet} --design &quot;${params.design}&quot; .' in sec

    def test_no_placeholder_reaches_the_reader(self):
        html = render_pipeline_page(_composed())
        assert not _PLACEHOLDER_RE.search(_spoken(_MACHINE_KEY_RE.sub("", html)))


# ── the MCP primitive ─────────────────────────────────────────────────────────


class TestTool:
    @pytest.fixture
    def sealed_pair(self, tmp_path):
        """Both specs written as seal writes them, the cohort trial's sheet on disk."""
        from agent.skills.spec_writer import write_workflow_spec
        sheet = tmp_path / "samples.csv"
        sheet.write_text(sheet_text())
        de = sealed_deseq2_spec(sheet=str(sheet))
        for spec in (sealed_rnaseq_spec(SAMPLES), de):
            assert "workflow_spec_path" in write_workflow_spec(spec.model_dump(), {})
        return "rnaseq_counts_workflow", "rnaseq_de_workflow"

    def test_cohort_renders_the_composed_directory_and_reports_it(self, sealed_pair):
        from agent.mcp_tools import pipeline_tools as PT
        base, de = sealed_pair
        out = PT.render_pipeline(sealed_workflow=base, name="composed", cohort=[{"sealed_workflow": de, "collect": COLLECT}])
        assert out["outcome"] == "proven", out
        d = Path(out["dir"])
        assert (d / "bin" / "merge_counts.R").is_file() and "bin/deseq2.R" in out["files"]
        assert out["scripts"] == ["bin/merge_counts.R", "bin/deseq2.R"]
        assert out["cohort_workflows"][0]["stages"] == ["MERGE_COUNTS", "DESEQ2"]
        assert out["samplesheet_columns"] == ["sample", "reads", "donor", "condition"]
        assert [s["scope"] for s in out["stages"]] == ["per_sample"] * 3 + ["cohort"] * 2
        rec = pr.load_pipeline_record(Path(out["record"]))
        assert rec.cohort_workflows[0].sealed_workflow == de and rec.cohort_workflows[0].sealed_workflow_sha256

    def test_a_malformed_cohort_entry_and_an_unsealed_cohort_workflow_are_refused(self, sealed_pair):
        from agent.mcp_tools import pipeline_tools as PT
        base, _ = sealed_pair
        out = PT.render_pipeline(sealed_workflow=base, cohort=[{"collect": COLLECT}])
        assert out["outcome"] == "refused" and out["code"] == "pipeline.bad_cohort"
        out = PT.render_pipeline(sealed_workflow=base, cohort=[{"sealed_workflow": "nope", "collect": COLLECT}])
        assert out["outcome"] == "refused" and out["code"] == "pipeline.no_sealed_workflow"
        assert "rnaseq_de_workflow" in out["available_workflows"]

    def test_a_derivation_refusal_reaches_the_caller_with_its_code(self, sealed_pair):
        from agent.mcp_tools import pipeline_tools as PT
        base, de = sealed_pair
        out = PT.render_pipeline(sealed_workflow=base, cohort=[{"sealed_workflow": de, "collect": {"COUNTS_DIR": "aligned.bam"}}])
        assert out["outcome"] == "refused" and out["code"] == "pipeline.collect_not_unique"


# ── the real thing ────────────────────────────────────────────────────────────

_RUNTIME = Path(__file__).resolve().parents[1] / ".conda_runtime"
_NEXTFLOW = _RUNTIME / "bin" / "nextflow"
_JVM = _RUNTIME / "lib" / "jvm"
needs_nextflow = pytest.mark.skipif(
    not (_NEXTFLOW.is_file() and os.access(_NEXTFLOW, os.X_OK) and (_JVM / "bin" / "java").is_file()),
    reason="the runtime env carries no nextflow binary (./scripts/setup.sh installs it)")


def _nextflow(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """The runtime env's nextflow in `cwd`, offline, NXF_HOME inside the directory."""
    env = dict(os.environ, JAVA_HOME=str(_JVM), JAVA_CMD=str(_JVM / "bin" / "java"),
               NXF_HOME=str(cwd / ".nextflow_home"), NXF_OFFLINE="true", NXF_ANSI_LOG="false")
    return subprocess.run([str(_NEXTFLOW), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300)


def _point_at_real_files(rec: pr.PipelineRecord, data: Path) -> pr.PipelineRecord:
    """The fixtures' paths are absolute and nowhere; give every path param and every
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


def _write(files: dict, d: Path) -> Path:
    """Like the renderer test's writer, with bin/ created for the scripts."""
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    return d


@needs_nextflow
class TestRealNextflow:
    def test_the_composed_pipeline_previews_under_the_real_binary(self, tmp_path):
        """`-preview` builds every channel — the collect, the sheet file, the scripts under
        bin/ — without running a process; the strict parser accepts the cohort processes."""
        rec = _point_at_real_files(_composed(), tmp_path / "data")
        d = _write(render_nextflow(rec), tmp_path / "pipe")
        run = _nextflow(d, "run", "main.nf", "-profile", "local", "-params-file", "params.yaml", "-preview")
        assert run.returncode == 0, run.stdout + run.stderr
        import json
        runs = sorted((d / "runs").iterdir())
        params = json.loads((runs[-1] / "params.json").read_text())
        assert params["merge_script"] == "bin/merge_counts.R" and params["design"] == "condition"
        assert (runs[-1] / "samples.csv").read_text().splitlines()[0] == "sample,reads,donor,condition"
