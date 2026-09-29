"""The explain page says everything the pipeline record knows, and its picture is a
faithful, non-overlapping drawing of the record's graph.

Rendered from the same fixture the record tests use (`sealed_rnaseq_spec`), plus a
hand-built wide record — 12 stages, 12 params, a fan-out into three side-by-side
stages, a fan-in, two long edges — for the layout guarantees a three-stage chain
cannot exercise.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from pipeline_fixtures import DIGEST, GTF, INDEX, REQUEST_KEY, SAMPLES, sealed_rnaseq_spec

from agent.skills import pipeline_record as pr
from agent.skills.pipeline_page_html import FOOTER, picture_layout, render_pipeline_page

SPEC_PATH = "/ws/reports/rnaseq_counts_workflow.workflow.yaml"
PLAIN = {"run_all.sh": "#!/bin/bash\n", "run_local.sh": "#!/bin/bash\n", "samples.csv": "sample,reads\n"}
NEXTFLOW = {"main.nf": "workflow {}\n", "launcher.sh": "#!/bin/bash\n", "nextflow_local.sh": "#!/bin/bash\n",
            "nextflow.config": "process {}\n"}


def _record(**kw) -> pr.PipelineRecord:
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", spec_path=SPEC_PATH, **kw)


def _linear() -> pr.PipelineRecord:
    return pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one", spec_path=SPEC_PATH)


def _page(rec: pr.PipelineRecord | None = None, files: dict | None = None) -> str:
    return render_pipeline_page(rec or _record(), PLAIN if files is None else files)


def _svg(html: str) -> str:
    return html[html.index("<svg"): html.index("</svg>") + len("</svg>")]


def _stage_block(html: str, name: str) -> str:
    start = html.index(f'id="stage-{name}"')
    nxt = html.find('<div class="run-card"', start + 1)
    end = nxt if nxt != -1 else html.index('id="defaults"')
    return html[start:end]


def _with_resources(rec: pr.PipelineRecord, name: str, **fields) -> pr.PipelineRecord:
    """The record with one stage's resources replaced — every other field untouched."""
    stages = [s.model_copy(update={"resources": s.resources.model_copy(update=fields)}) if s.name == name else s
              for s in rec.stages]
    return rec.model_copy(update={"stages": stages})


def _wide_record(n_stages: int = 12, n_params: int = 12) -> pr.PipelineRecord:
    """A linear record with a fan-out (2 → 3, 4, 5), a fan-in (3, 4, 5 → 6), two long
    edges (4 → 9 and 0 → 11) and three params per stage, built straight through the
    models so the page and the layout are exercised on the widest shape they promise."""
    P = [f"PARAM_{i:02d}_LONG" for i in range(n_params)]
    S = [f"STAGE_{i:02d}" for i in range(n_stages)]
    preds = {1: [0], 2: [1], 3: [2], 4: [2], 5: [2], 6: [3, 4, 5], 7: [6], 8: [7], 9: [8, 4],
             10: [9], 11: [10, 0]}
    params = {p: pr.PipelineParam(name=p, kind="shared", value_kind="path", default=f"/data/{p.lower()}",
                                  source="literal", format=None, description=None, used_by=[],
                                  reason="synthetic") for p in P}
    stages = []
    for i, name in enumerate(S):
        uses = sorted({P[i % n_params], P[(i * 5 + 1) % n_params], P[(i * 7 + 3) % n_params]})
        inputs = [pr.StageInput(name=p, origin="param", from_stage=None, artifact=None) for p in uses]
        for p in preds.get(i, []):
            if p < n_stages:
                inputs.append(pr.StageInput(name=f"artifact_{p:02d}.bam", origin="stage", from_stage=S[p],
                                            artifact=f"artifact_{p:02d}.bam"))
        consumers = [S[j] for j, ps in preds.items() if i in ps and j < n_stages]
        outputs = [pr.StageOutput(artifact=f"artifact_{i:02d}.bam", observed=None, published=(i % 3 == 0),
                                  consumed_by=consumers, declared_pattern="*.bam" if i % 3 == 0 else None)]
        for p in uses:
            params[p].used_by.append(name)
        stages.append(pr.PipelineStage(
            name=name, index=i, scope="per_sample", templates=[i],
            commands=[f"tool{i} " + " ".join("{" + p + "}" for p in uses) + f" > {{OUTPUT_DIR}}/artifact_{i:02d}.bam"],
            tool=f"tool{i}", sealed_steps=[], image=None, image_digest=None, request_key=None, sif_sha256=None,
            inputs=inputs, outputs=outputs, consumes_workdir=False, stage_in_copy=False,
            resources=pr.StageResources(cpus=None, mem=None, time=None, gpus=0, requested_by="default",
                                        measured_wall_seconds=None, measured_peak_rss_mb=None,
                                        measured_max_cpu_percent=None, measured_authority="none",
                                        measured_on=None)))
    return pr.PipelineRecord(
        name="wide", version="1", created_at="2026-09-29T00:00:00+00:00", sealed_workflow="wide_workflow",
        sealed_workflow_path="", sealed_workflow_sha256=None, env_digests=["sha256:" + "b" * 64],
        shape="linear", params=list(params.values()), samplesheet=None, output_slots=["OUTPUT_DIR"],
        stages=stages, provenance_steps=[], unmatched_steps=[], defaults=[], forms=["plain"], notes=[])


# ── every fact the record knows reaches the page ──────────────────────────────


class TestEveryFactReachesThePage:
    def test_every_stage_name_and_every_param_name_appears(self):
        rec, html = _record(), _page()
        for s in rec.stages:
            assert s.name in html
        for p in rec.params:
            assert p.name in html

    def test_the_header_names_the_sealed_workflow_its_path_and_the_env_digest_short_and_full(self):
        html = _page()
        assert "rnaseq_counts_workflow" in html
        assert SPEC_PATH in html
        assert DIGEST in html                                   # the full digest, in a <code>
        assert f"<b>{DIGEST.split(':')[1][:12]}</b>" in html    # and its short form beside it
        assert "3 in execution order" in html
        assert "HISAT2</code> → <code>SAMTOOLS</code> → <code>HTSEQ_COUNT" in html
        assert "{OUTPUT_DIR}" in html                            # the output slots

    def test_the_shape_line_counts_the_example_rows_for_per_row_and_says_params_only_for_linear(self):
        assert f"per_row — {len(SAMPLES)} example rows in <code>samples.csv</code>" in _page()
        assert "linear — runs once, params only" in _page(_linear())

    def test_the_params_table_carries_kind_default_value_kind_source_and_reason(self):
        html = _page()
        assert "identical across the 3 trials" in html
        assert "differs across the 3 trials" in html
        assert "sealed_step:1" in html                         # HISAT2_INDEX's source
        assert f"<code>{INDEX}</code>" in html and f"<code>{GTF}</code>" in html
        assert "prefix" in html                                 # HISAT2_INDEX's value kind
        assert "per row" in html                                # a per-sample param has no default

    def test_the_defaults_table_lists_every_key_with_its_source(self):
        rec, html = _record(), _page()
        from agent.skills.env_report_html import _e
        for d in rec.defaults:
            assert f"<code>{d.key}</code>" in html
            assert f"<td>{_e(d.value)}</td>" in html
        assert "<td>seal</td>" in html and "<td>default</td>" in html

    def test_provenance_step_1_is_shown_with_its_command_and_the_param_it_produced(self):
        html = _page()
        assert "sealed step 1 — <b>hisat2-build</b> produced <code>HISAT2_INDEX</code>" in html
        assert "hisat2-build /data/genome/chr22.fa /data/idx/chr22" in html
        assert "every successful sealed step is either a stage or the provenance" in html

    def test_unmatched_sealed_steps_are_stated_as_not_part_of_the_pipeline(self):
        rec = _record().model_copy(update={"unmatched_steps": [11, 12]})
        html = _page(rec)
        assert "sealed steps 11, 12 match no how-to command" in html
        assert "not part of the pipeline" in html

    def test_every_note_reaches_the_page(self):
        rec, html = _record(), _page()
        assert rec.notes
        for n in rec.notes:
            assert f"<li>{n}</li>" in html.replace("&#x27;", "'").replace("&quot;", '"')

    def test_a_hostile_value_is_escaped_everywhere_including_inside_the_svg(self):
        rec = _record()
        rec.notes.append("<script>alert(1)</script>")
        stages = [s.model_copy(update={"tool": 'x"><script>y</script>'}) if s.name == "SAMTOOLS" else s
                  for s in rec.stages]
        html = _page(rec.model_copy(update={"stages": stages}))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "<script>y</script>" not in _svg(html)
        assert "&lt;script&gt;y" in _svg(html)


# ── the picture ────────────────────────────────────────────────────────────────


class TestThePicture:
    def test_one_node_per_stage_column_param_and_published_output(self):
        rec, svg = _record(), _svg(_page())
        assert svg.count('data-node="stage"') == len(rec.stages)
        assert svg.count('data-node="column"') == len(rec.samplesheet.columns)
        # a per-sample param IS its samplesheet column; the shared params get their own nodes
        shared = [p for p in rec.params if p.name not in {c.placeholder for c in rec.samplesheet.columns}]
        assert svg.count('data-node="param"') == len(shared)
        assert svg.count('data-node="column"') + svg.count('data-node="param"') == len(rec.params)
        published = [o for s in rec.stages for o in s.outputs if o.published]
        assert svg.count('data-node="output"') == len(published) == 2
        for p in rec.params:
            assert f'data-name="{p.name}"' in svg

    def test_a_linear_record_draws_every_param_as_a_param_node_and_no_column(self):
        rec, svg = _linear(), _svg(_page(_linear(), files={}))
        assert svg.count('data-node="column"') == 0
        assert svg.count('data-node="param"') == len(rec.params)
        assert "samples.csv" not in svg

    def test_stage_to_stage_edges_carry_their_artifact_labels(self):
        svg = _svg(_page())
        edges = re.findall(r'<path class="edge stage" data-edge="stage" data-from="s:(\w+)" data-to="s:(\w+)" '
                           r'data-artifacts="([^"]*)"', svg)
        assert sorted(edges) == [("HISAT2", "HTSEQ_COUNT", "aligned.bam"), ("HISAT2", "SAMTOOLS", "aligned.bam"),
                                 ("SAMTOOLS", "HTSEQ_COUNT", "aligned.bam.bai")]
        labels = re.findall(r'<g class="lbl" data-edge="stage-label"[^>]*>.*?<text[^>]*>([^<]*)</text></g>', svg)
        assert sorted(labels) == ["aligned.bam", "aligned.bam", "aligned.bam.bai"]

    def test_every_param_and_column_input_is_an_edge_into_the_stage_that_uses_it(self):
        rec, svg = _record(), _svg(_page())
        cols = {c.placeholder for c in rec.samplesheet.columns}
        for s in rec.stages:
            for i in s.inputs:
                if i.origin in ("param", "column"):
                    src = f"c:{i.name}" if i.name in cols else f"p:{i.name}"
                    assert f'data-edge="input" data-from="{src}" data-to="s:{s.name}"' in svg
        assert svg.count('data-edge="input"') == sum(
            1 for s in rec.stages for i in s.inputs if i.origin in ("param", "column"))

    def test_published_outputs_are_labelled_with_their_results_path(self):
        svg = _svg(_page())
        assert "results/&lt;sample&gt;/aligned.bam" in svg
        assert "results/&lt;sample&gt;/{SAMPLE}.counts.tsv" in svg
        assert 'data-edge="publish" data-from="s:HTSEQ_COUNT" data-to="o:HTSEQ_COUNT/{SAMPLE}.counts.tsv"' in svg
        lin = _svg(_page(_linear(), files={}))
        assert "results/{SAMPLE}.counts.tsv" in lin and "&lt;sample&gt;" not in lin

    def test_the_svg_carries_a_viewbox_and_scales_down_to_its_container(self):
        svg = _svg(_page())
        m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        assert m and int(m.group(1)) > 0 and int(m.group(2)) > 0
        assert 'width="100%"' in svg and f"max-width:{m.group(1)}px" in svg

    def test_a_chain_is_one_stage_per_row_in_execution_order(self):
        assert picture_layout(_record()).rows == [["HISAT2"], ["SAMTOOLS"], ["HTSEQ_COUNT"]]

    def test_stages_of_one_rank_sit_side_by_side_on_a_wide_record(self):
        layout = picture_layout(_wide_record())
        assert ["STAGE_03", "STAGE_04", "STAGE_05"] in layout.rows
        row = [n for n in layout.nodes if n.name in ("STAGE_03", "STAGE_04", "STAGE_05")]
        assert len({n.y for n in row}) == 1
        xs = sorted((n.x, n.x + n.w) for n in row)
        assert all(a[1] < b[0] for a, b in zip(xs, xs[1:]))

    def test_no_text_overlaps_and_nothing_leaves_the_viewbox_on_a_wide_record(self):
        """Twelve stages, twelve params, a three-wide rank, two long edges: every label
        the picture draws must clear every other label and every node box."""
        layout = picture_layout(_wide_record())
        texts = layout.texts
        assert len(texts) > 60
        for i, a in enumerate(texts):
            for b in texts[i + 1:]:
                clear = (a.x + a.w <= b.x or b.x + b.w <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y)
                assert clear, f"{a.role} {a.text!r} overlaps {b.role} {b.text!r}"
        for lbl in (t for t in texts if t.role == "label"):
            for n in layout.nodes:
                clear = (lbl.x + lbl.w <= n.x or n.x + n.w <= lbl.x or lbl.y + lbl.h <= n.y or n.y + n.h <= lbl.y)
                assert clear, f"label {lbl.text!r} overlaps node {n.id}"
        for n in layout.nodes:
            assert 0 <= n.x and n.x + n.w <= layout.width and 0 <= n.y and n.y + n.h <= layout.height
        for t in texts:
            assert 0 <= t.x and t.x + t.w <= layout.width and 0 <= t.y and t.y + t.h <= layout.height
        html = render_pipeline_page(_wide_record(), PLAIN)
        assert _svg(html).count('data-node="stage"') == 12
        assert _svg(html).count('data-edge="stage"') == 15

    def test_hovering_is_a_class_toggle_and_the_page_needs_nothing_from_the_network(self):
        html = _page()
        assert "<script src=" not in html and "<link " not in html
        assert "@import" not in html and "url(http" not in html and "url(//" not in html
        assert 'getElementById("pipeline-picture")' in html
        assert 'classList.add("hl")' in html
        assert "fetch(" not in html and "XMLHttpRequest" not in html


# ── stages ─────────────────────────────────────────────────────────────────────


class TestStages:
    UNSIZED = "unsized — DEFAULT request; size before running on real data"

    def test_the_unsized_flag_appears_for_a_default_request_and_disappears_once_the_caller_sizes_it(self):
        html = _page()
        assert self.UNSIZED in _stage_block(html, "HISAT2")
        sized = _record(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}})
        html = _page(sized)
        assert self.UNSIZED not in _stage_block(html, "HISAT2")
        assert "requested by the caller: cpus 8 · mem 32G · time 4:00:00 · gpus 0" in _stage_block(html, "HISAT2")
        assert self.UNSIZED in _stage_block(html, "SAMTOOLS")

    def test_the_unsized_stage_is_drawn_dashed_and_the_sized_one_is_not(self):
        assert 'class="node stage unsized" data-node="stage" data-id="s:HISAT2"' in _svg(_page())
        sized = _record(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}})
        assert 'class="node stage" data-node="stage" data-id="s:HISAT2"' in _svg(_page(sized))

    def test_a_partial_caller_request_states_what_was_not_requested(self):
        html = _page(_record(resources={"HISAT2": {"cpus": 4}}))
        assert "cpus 4 · mem not requested · time not requested · gpus 0" in _stage_block(html, "HISAT2")

    def test_measured_numbers_are_printed_with_their_authority_word(self):
        block = _stage_block(_page(), "HISAT2")
        assert "peak RSS 1570 MB" in block
        assert '<span class="ok">authoritative</span>' in block
        assert "measured on 3 sealed steps on the workflow" in block

    #: The five states the record can carry, each with a marker no other state's rendering
    #: contains — so the assertion below can say "this one, and none of the others".
    AUTHORITY_MARKERS = {
        "authoritative": 'class="ok">authoritative</span>',
        "not_authoritative": 'class="warn">NOT authoritative</span>',
        "unrecorded": 'class="muted">authority unrecorded</span>',
        "mixed": "mixed authority across the sealed steps",
        "none": "no sealed step measured this stage",
    }

    @pytest.mark.parametrize("authority", sorted(AUTHORITY_MARKERS))
    def test_every_authority_state_renders_as_its_own_phrase_and_no_other(self, authority):
        rec = _with_resources(_record(), "HISAT2", measured_authority=authority)
        block = _stage_block(_page(rec), "HISAT2")
        assert self.AUTHORITY_MARKERS[authority] in block
        for other, marker in self.AUTHORITY_MARKERS.items():
            if other != authority:
                assert marker not in block, (authority, other)

    def test_an_authority_the_page_does_not_know_is_printed_raw_in_warning_colour_not_dropped(self):
        rec = _with_resources(_record(), "HISAT2", measured_authority="some_future_state")
        block = _stage_block(_page(rec), "HISAT2")
        assert '<span class="warn">some_future_state</span>' in block

    def test_untrustworthy_measurements_carry_a_do_not_size_warning(self):
        for authority in ("not_authoritative", "unrecorded", "mixed"):
            rec = _with_resources(_record(), "HISAT2", measured_authority=authority)
            assert 'class="warn-note"' in _stage_block(_page(rec), "HISAT2"), authority
        assert 'class="warn-note"' not in _stage_block(_page(), "HISAT2")

    def test_absent_measurements_say_so_instead_of_printing_a_number(self):
        rec = _with_resources(_record(), "HISAT2", measured_wall_seconds=None, measured_peak_rss_mb=None,
                              measured_max_cpu_percent=None, measured_authority="none", measured_on=None)
        block = _stage_block(_page(rec), "HISAT2")
        assert "no measurement" in block and "measured on: unrecorded" in block
        assert "peak RSS 1570" not in block and " 0 MB" not in block
        rec = _with_resources(_record(), "HISAT2", measured_peak_rss_mb=None)
        assert "peak RSS unrecorded" in _stage_block(_page(rec), "HISAT2")

    def test_the_block_carries_the_command_image_request_key_inputs_and_outputs(self):
        rec, html = _record(), _page()
        block = _stage_block(html, "HTSEQ_COUNT")
        assert "htseq-count -s {STRANDED} -f bam {OUTPUT_DIR}/aligned.bam {GTF}" in block
        assert DIGEST in block and REQUEST_KEY in block
        assert "none recorded" in block                           # no .sif sha256 on a local seal
        assert "4, 7, 10" in block                                # the sealed steps behind it
        assert "aligned.bam.bai" in block and "SAMTOOLS" in block  # the sidecar travels with its parent
        assert "samplesheet column" in block                     # SAMPLE's origin
        assert "declared as <code>*.counts.tsv</code>" in block
        assert "SRR1039508.counts.tsv" in block                  # observed in the sealed run
        assert "results/&lt;sample&gt;/{SAMPLE}.counts.tsv" in block
        idx = _stage_block(html, "SAMTOOLS")
        assert "an intermediate nobody declared" in idx

    def test_a_never_observed_output_says_never_observed(self):
        rec = _record()
        stages = [s.model_copy(update={"outputs": [o.model_copy(update={"observed": None}) for o in s.outputs]})
                  if s.name == "HISAT2" else s for s in rec.stages]
        block = _stage_block(_page(rec.model_copy(update={"stages": stages})), "HISAT2")
        assert "never observed" in block

    def test_stage_flags_are_shown_only_when_set(self):
        html = _page()
        assert "consumes workdir" not in html and "stage-in copy" not in html
        rec = _record()
        stages = [s.model_copy(update={"consumes_workdir": True, "stage_in_copy": True}) if s.name == "SAMTOOLS" else s
                  for s in rec.stages]
        block = _stage_block(_page(rec.model_copy(update={"stages": stages})), "SAMTOOLS")
        assert "consumes workdir" in block and "stage-in copy" in block


# ── the samplesheet ────────────────────────────────────────────────────────────


class TestSamplesheet:
    def test_the_example_rows_appear_for_a_per_row_pipeline_with_the_replace_sentence(self):
        html = _page()
        assert 'id="samplesheet"' in html
        for s in SAMPLES:
            assert f"<td>{s}</td>" in html
            assert f"/data/reads/{s}_10K_R1.fastq.gz" in html
        assert "The example rows are the seal's own trials — replace them with your samples." in html
        assert "<code>{READS}</code>" in html                       # the column's placeholder

    def test_the_section_is_absent_for_a_linear_pipeline(self):
        html = _page(_linear(), files={})
        assert 'id="samplesheet"' not in html
        assert "replace them with your samples" not in html
        assert "&lt;sample&gt;" not in html


# ── how to run ─────────────────────────────────────────────────────────────────


class TestHowToRun:
    def test_the_plain_section_appears_only_when_plain_files_were_rendered(self):
        html = _page(files=PLAIN)
        assert "Plain form" in html and "<pre>./run_local.sh</pre>" in html and "<pre>./run_all.sh</pre>" in html
        assert "<pre>./run_all.sh --stages HTSEQ_COUNT</pre>" in html
        assert "results/&lt;sample&gt;/</code>" in html
        assert "Nextflow form" not in html and "-resume" not in html
        assert "<b>plain</b> (<code>run_all.sh</code>, <code>run_local.sh</code>)" in html

    def test_the_nextflow_section_appears_only_when_nextflow_files_were_rendered(self):
        html = _page(_record(forms=("nextflow",)), files=NEXTFLOW)
        assert "Nextflow form" in html and "<pre>sbatch launcher.sh</pre>" in html
        assert "<pre>./nextflow_local.sh</pre>" in html
        assert "<code>-resume</code> re-runs only the tasks whose cache entry is missing" in html
        assert "Plain form" not in html and "run_all.sh" not in html

    def test_both_forms_get_their_own_subsection(self):
        html = _page(_record(forms=("plain", "nextflow")), files={**PLAIN, **NEXTFLOW})
        assert "Plain form" in html and "Nextflow form" in html

    def test_form_files_are_recognised_by_basename_so_a_nested_layout_still_counts(self):
        html = _page(files={"nextflow/main.nf": "", "nextflow/launcher.sh": ""})
        assert "Nextflow form" in html and "<code>nextflow/main.nf</code>" in html

    def test_no_files_says_there_is_nothing_to_run(self):
        html = _page(files={})
        assert "no form files were rendered beside this record" in html
        assert "Plain form" not in html and "Nextflow form" not in html
        assert "none — no form files were rendered" in html      # the header row agrees

    def test_the_manifest_check_is_printed_only_when_a_manifest_was_rendered(self):
        assert "<pre>sha256sum -c MANIFEST.sha256</pre>" in _page(files={**PLAIN, "MANIFEST.sha256": "x\n"})
        assert "sha256sum -c" not in _page(files=PLAIN)

    def test_a_form_the_record_declares_but_nobody_rendered_is_stated(self):
        html = _page(_record(forms=("plain", "nextflow")), files=PLAIN)
        assert "declared in the record but not rendered: nextflow" in html
        assert "declared in the record but not rendered" not in _page(_record(forms=("plain",)), files=PLAIN)

    def test_a_linear_pipeline_publishes_flat(self):
        html = _page(_linear(), files=PLAIN)
        assert "results/</code>" in html and "&lt;sample&gt;" not in html
        assert "Put your samples in" not in html


# ── the page as a whole ────────────────────────────────────────────────────────


_VOID = {"meta", "br", "path", "rect", "hr", "img", "input", "link"}


class _Balance(HTMLParser):
    """A stack of open tags; every end tag must close the tag on top."""

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1] if self.stack else 'nothing'}> at {self.getpos()}")
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


class TestPageShape:
    def test_the_footer_sentence_is_verbatim(self):
        assert FOOTER == ("This page shows a pipeline derived from a sealed run; it proves the render is "
                          "mechanical and traceable, never that the analysis is biologically right.")
        for html in (_page(), _page(_linear(), files={}), render_pipeline_page(_wide_record(), {})):
            assert f'<p class="gen">{FOOTER}</p>' in html

    @pytest.mark.parametrize("which", ["per_row", "linear", "wide"])
    def test_every_tag_the_page_opens_is_closed_in_order(self, which):
        rec = {"per_row": _record, "linear": _linear, "wide": _wide_record}[which]()
        html = render_pipeline_page(rec, {**PLAIN, **NEXTFLOW, "MANIFEST.sha256": "x\n"})
        b = _Balance()
        b.feed(html)
        b.close()
        assert not b.errors, b.errors[:5]
        assert not b.stack, b.stack
        for tag in ("table", "svg", "section", "div", "g", "pre", "tr", "td"):
            assert html.count(f"<{tag}") == html.count(f"</{tag}>"), tag

    def test_the_sections_come_in_the_agreed_order(self):
        html = _page(files={**PLAIN, **NEXTFLOW})
        order = ['id="picture"', 'id="params"', 'id="samplesheet"', 'id="stages"', 'id="defaults"',
                 'id="provenance"', 'id="howto"', 'id="notes"', f'<p class="gen">{FOOTER}']
        positions = [html.index(o) for o in order]
        assert positions == sorted(positions)

    def test_the_same_record_and_files_render_the_same_bytes(self):
        rec = _record()
        assert render_pipeline_page(rec, PLAIN) == render_pipeline_page(rec, PLAIN)

    def test_the_page_renders_from_a_record_that_round_tripped_through_yaml(self, tmp_path):
        rec = _record()
        back = pr.load_pipeline_record(pr.write_pipeline_record(rec, tmp_path / "p"))
        assert render_pipeline_page(back, PLAIN) == render_pipeline_page(rec, PLAIN)

    def test_a_record_with_no_params_no_outputs_and_no_notes_still_renders(self):
        rec = _wide_record(n_stages=2).model_copy(update={"params": [], "notes": [], "defaults": []})
        stages = [s.model_copy(update={"inputs": [i for i in s.inputs if i.origin == "stage"],
                                       "outputs": [o.model_copy(update={"published": False}) for o in s.outputs]})
                  for s in rec.stages]
        html = render_pipeline_page(rec.model_copy(update={"stages": stages}), {})
        assert "(nothing published)" in html
        assert "output slots only" in html
        assert "the record carries no notes" in html
        assert "no defaults recorded" in html
