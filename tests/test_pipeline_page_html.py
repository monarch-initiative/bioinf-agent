"""The explain page: the header banner, the picture, the parameters and samples, how
to run it locally, how to run it on the cluster, the stages, the footer — and nothing
else. Every run line it prints is the one `pipeline_commands` spells, so the page and
`commands.sh` cannot disagree; and the picture is a faithful, non-overlapping drawing
of the record's graph.

Rendered from the same fixture the record tests use (`sealed_rnaseq_spec`), plus a
hand-built wide record — 12 stages, 12 params, a fan-out into three side-by-side
stages, a fan-in, two long edges — for the layout guarantees a three-stage chain
cannot exercise.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from pipeline_fixtures import DIGEST, GTF, INDEX, REQUEST_KEY, SAMPLES, TEMPLATES, sealed_rnaseq_spec

from agent.skills import pipeline_record as pr
from agent.skills.env_report_html import _e
from agent.skills.pipeline_commands import enter_image, nextflow_run
from agent.skills.pipeline_page_html import FOOTER, picture_layout, render_pipeline_page

SPEC_PATH = "/ws/reports/rnaseq_counts_workflow.workflow.yaml"
SIF = "/cluster/containers/rnaseq_cli_48ac8c5b25d2.sif"
MODULES = ["apptainer/1.3.2", "nextflow/24.10.0"]
SECTION_IDS = ["picture", "params", "run-local", "run-hpc", "stages"]


def _record(**kw) -> pr.PipelineRecord:
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", spec_path=SPEC_PATH, **kw)


def _linear() -> pr.PipelineRecord:
    return pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one", spec_path=SPEC_PATH)


def _cluster(**kw) -> pr.PipelineRecord:
    """The record rendered with a cluster named: env names, .sif paths and modules."""
    return _record(env_names={REQUEST_KEY: "rnaseq_cli"}, sif_paths={REQUEST_KEY: SIF},
                   compute_env="hpc", modules=MODULES, **kw)


def _page(rec: pr.PipelineRecord | None = None) -> str:
    return render_pipeline_page(rec or _record())


def _svg(html: str) -> str:
    return html[html.index("<svg"): html.index("</svg>") + len("</svg>")]


def _section(html: str, sid: str) -> str:
    start = html.index(f'<section class="bx" id="{sid}"')
    return html[start: html.index("</section>", start) + len("</section>")]


def _stage_row(html: str, name: str) -> str:
    sec = _section(html, "stages")
    i = sec.index(f"<tr><td><code>{name}</code></td>")
    return sec[i: sec.index("</tr>", i)]


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
        outputs = [pr.StageOutput(artifact=f"artifact_{i:02d}.bam", observed=None, consumed_by=consumers,
                                  declared_pattern="*.bam" if i % 3 == 0 else None)]
        for p in uses:
            params[p].used_by.append(name)
        stages.append(pr.PipelineStage(
            name=name, index=i, scope="per_sample", templates=[i],
            commands=[f"tool{i} " + " ".join("{" + p + "}" for p in uses) + f" > {{OUTPUT_DIR}}/artifact_{i:02d}.bam"],
            tool=f"tool{i}", sealed_steps=[], image=None, image_digest=None, request_key=None, env_name=None,
            sif_sha256=None, sif_path=None, inputs=inputs, outputs=outputs, consumes_workdir=False,
            stage_in_copy=False,
            resources=pr.StageResources(cpus=None, mem=None, time=None, gpus=0, requested_by="default",
                                        measured_wall_seconds=None, measured_peak_rss_mb=None,
                                        measured_max_cpu_percent=None, measured_authority="none",
                                        measured_on=None)))
    return pr.PipelineRecord(
        name="wide", version="1", created_at="2026-09-29T00:00:00+00:00", sealed_workflow="wide_workflow",
        sealed_workflow_path="", sealed_workflow_sha256=None, env_digests=["sha256:" + "b" * 64],
        shape="linear", params=list(params.values()), samplesheet=None, output_slots=["OUTPUT_DIR"],
        compute_env=None, modules=[], stages=stages, provenance_steps=[], unmatched_steps=[], defaults=[],
        notes=[])


# ── the header banner ──────────────────────────────────────────────────────────


class TestHeader:
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

    def test_the_forms_and_record_version_rows_are_gone(self):
        html = _page()
        assert "Forms rendered" not in html and "Record version" not in html

    def test_the_cluster_row_names_the_env_and_its_modules_only_when_a_cluster_was_named(self):
        html = _page(_cluster())
        assert ('<td class="k">Cluster</td><td><b>hpc</b> — module load <code>apptainer/1.3.2</code> '
                '<code>nextflow/24.10.0</code></td>') in html
        assert '<td class="k">Cluster</td><td><b>hpc</b> — no modules to load</td>' in _page(
            _record(compute_env="hpc", modules=[]))
        assert '<td class="k">Cluster</td>' not in _page()


# ── the picture ────────────────────────────────────────────────────────────────


class TestThePicture:
    def test_one_node_per_stage_column_param_and_output(self):
        rec, svg = _record(), _svg(_page())
        assert svg.count('data-node="stage"') == len(rec.stages)
        assert svg.count('data-node="column"') == len(rec.samplesheet.columns)
        # a per-sample param IS its samplesheet column; the shared params get their own nodes
        shared = [p for p in rec.params if p.name not in {c.placeholder for c in rec.samplesheet.columns}]
        assert svg.count('data-node="param"') == len(shared)
        assert svg.count('data-node="column"') + svg.count('data-node="param"') == len(rec.params)
        outputs = [o for s in rec.stages for o in s.outputs]   # every artifact a stage writes is published
        assert svg.count('data-node="output"') == len(outputs) == 3
        for p in rec.params:
            assert f'data-name="{p.name}"' in svg

    def test_a_linear_record_draws_every_param_as_a_param_node_and_no_column(self):
        rec, svg = _linear(), _svg(_page(_linear()))
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
        lin = _svg(_page(_linear()))
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
        html = render_pipeline_page(_wide_record())
        assert _svg(html).count('data-node="stage"') == 12
        assert _svg(html).count('data-edge="stage"') == 15

    def test_the_unsized_stage_is_drawn_dashed_and_the_sized_one_is_not(self):
        assert 'class="node stage unsized" data-node="stage" data-id="s:HISAT2"' in _svg(_page())
        sized = _record(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}})
        assert 'class="node stage" data-node="stage" data-id="s:HISAT2"' in _svg(_page(sized))

    def test_hovering_is_a_class_toggle_and_the_page_needs_nothing_from_the_network(self):
        html = _page(_cluster())
        assert "<script src=" not in html and "<link " not in html
        assert "@import" not in html and "url(http" not in html and "url(//" not in html
        assert "http://" not in html and "https://" not in html
        assert 'getElementById("pipeline-picture")' in html
        assert 'classList.add("hl")' in html
        assert "fetch(" not in html and "XMLHttpRequest" not in html


# ── parameters and samples ─────────────────────────────────────────────────────


class TestParametersAndSamples:
    def test_every_param_is_a_row_with_its_scope_and_the_sealed_runs_example_value(self):
        rec, sec = _record(), _section(_page(), "params")
        for p in rec.params:
            assert f"<tr><td><code>{p.name}</code></td>" in sec
        assert f"<td><code>HISAT2_INDEX</code></td><td>shared</td><td><code>{INDEX}</code></td>" in sec
        assert f"<td><code>GTF</code></td><td>shared</td><td><code>{GTF}</code></td>" in sec
        assert "<td><code>STRANDED</code></td><td>shared</td><td><code>reverse</code></td>" in sec
        assert ("<td><code>READS</code></td><td>per sample (column <code>reads</code>)</td>"
                f"<td><code>/data/reads/{SAMPLES[0]}_10K_R1.fastq.gz</code></td>") in sec
        assert (f"<td><code>SAMPLE</code></td><td>per sample (column <code>sample</code>)</td>"
                f"<td><code>{SAMPLES[0]}</code></td>") in sec
        assert sec.count("<tr><td><code>") == len(rec.params)

    def test_what_it_is_carries_the_value_kind_the_description_and_the_producing_sealed_step(self):
        sec = _section(_page(), "params")
        assert "a prefix — a family of files named after it · HISAT2 index prefix · produced by sealed step 1" in sec
        assert "a value · htseq-count -s: yes | no | reverse" in sec
        assert "a path · gene annotation" in sec
        assert "sealed_step:1" not in sec                    # the source is read out, not echoed

    def test_the_samplesheet_columns_and_example_rows_follow_the_table_for_a_per_row_pipeline(self):
        sec = _section(_page(), "params")
        assert "<code>samples.csv</code> — one row per sample, columns: <code>sample</code>, <code>reads</code>." in sec
        assert "The example rows are the seal's own trials — replace them with your samples." in sec
        assert "<tr><th>sample</th><th>reads</th></tr>" in sec
        for s in SAMPLES:
            assert f"<tr><td>{s}</td><td>/data/reads/{s}_10K_R1.fastq.gz</td></tr>" in sec

    def test_a_linear_pipeline_states_its_one_implicit_row_and_shows_per_sample_values_as_examples(self):
        sec = _section(_page(_linear()), "params")
        assert ("one implicit row; the per-sample values are set at the top of <code>commands.sh</code> "
                "(by hand) and in <code>params.yaml</code> (Nextflow).") in sec
        assert "replace them with your samples" not in sec and "samples.csv" not in sec
        assert (f"<td><code>READS</code></td><td>per sample</td>"
                f"<td><code>/data/reads/{SAMPLES[0]}_10K_R1.fastq.gz</code></td>") in sec
        assert f"<td><code>SAMPLE</code></td><td>per sample</td><td><code>{SAMPLES[0]}</code></td>" in sec

    def test_a_samplesheet_column_that_is_not_a_placeholder_of_the_howto_still_gets_a_row(self):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(templates=TEMPLATES[:2]), name="two")
        assert "SAMPLE" not in {p.name for p in rec.params}
        sec = _section(_page(rec), "params")
        assert (f"<td><code>SAMPLE</code></td><td>per sample (column <code>sample</code>)</td>"
                f"<td><code>{SAMPLES[0]}</code></td><td>a value · row identifier</td>") in sec
        assert sec.count("<tr><td><code>") == len(rec.params) + 1

    def test_a_record_with_no_params_says_output_slots_only(self):
        rec = _wide_record(n_stages=2).model_copy(update={"params": []})
        stages = [s.model_copy(update={"inputs": [i for i in s.inputs if i.origin == "stage"]}) for s in rec.stages]
        sec = _section(_page(rec.model_copy(update={"stages": stages})), "params")
        assert "output slots only" in sec and "<table>" not in sec


# ── run it locally ─────────────────────────────────────────────────────────────


class TestRunLocally:
    def test_by_hand_is_three_steps_cd_enter_run_with_the_one_spelling_of_the_docker_line(self):
        rec, html = _record(), _page()
        sec = _section(html, "run-local")
        by_hand = sec[sec.index("A. One sample by hand"): sec.index("B. Every sample")]
        assert by_hand.count("<li>") == 3
        assert "<pre>cd /path/to/rnaseq_counts</pre>" in by_hand
        assert "next to your data" in by_hand
        line = enter_image(rec, "local")
        assert len(line) == 1 and line[0].startswith("docker run ")
        assert f"<pre>{_e(line[0])}</pre>" in by_hand
        assert "add a <code>-v</code> for each" in by_hand
        assert "<pre>bash commands.sh</pre>" in by_hand
        assert "Edit the values at the top of <code>commands.sh</code>" in by_hand
        assert "Results land in <code>results/&lt;sample&gt;/</code>" in by_hand
        assert html.count("docker run") == 1                    # one spelling on the whole page

    def test_nextflow_is_three_steps_with_the_one_spelling_of_the_run_line_and_resume(self):
        rec, html = _record(), _page()
        sec = _section(html, "run-local")
        nf = sec[sec.index("B. Every sample in <code>samples.csv</code>, with Nextflow"):]
        assert nf.count("<li>") == 3
        assert "<pre>cd /path/to/rnaseq_counts</pre>" in nf
        assert "Nothing to enter: docker and nextflow on this machine." in nf
        assert f"<pre>{_e(nextflow_run(rec, 'local')[0])}</pre>" in nf
        assert "<pre>nextflow run main.nf -profile local -params-file params.yaml -resume</pre>" in nf
        assert "<code>-resume</code> re-runs only the stages whose inputs or parameters changed" in nf
        assert "Results land in <code>results/&lt;sample&gt;/</code>" in nf
        assert "<code>runs/&lt;timestamp&gt;/trace.txt</code> lists every task's command" in nf
        assert "<code>runs/&lt;timestamp&gt;/report.html</code> the resources" in nf
        assert "<code>nextflow log</code> the launch line" in nf
        assert html.count("nextflow run main.nf") == 1

    def test_a_linear_pipeline_lands_its_results_flat_and_runs_its_one_row(self):
        sec = _section(_page(_linear()), "run-local")
        assert "Results land in <code>results/</code>." in sec and "&lt;sample&gt;" not in sec
        assert "B. The one row, with Nextflow" in sec

    def test_the_local_section_never_mentions_the_cluster_and_the_cluster_section_never_docker(self):
        html = _page(_cluster())
        local, hpc = _section(html, "run-local"), _section(html, "run-hpc")
        assert "sbatch" not in local and "apptainer" not in local and "module load" not in local
        assert "docker" not in hpc


# ── run it on the cluster ──────────────────────────────────────────────────────


class TestRunOnTheCluster:
    def test_with_a_cluster_named_the_module_line_and_the_real_sif_path_are_printed(self):
        rec = _cluster()
        html = _page(rec)
        sec = _section(html, "run-hpc")
        assert "Rendered without a cluster named" not in sec
        assert (f'<code>stage_apptainer_image(project=&lt;your project&gt;, env=&quot;hpc&quot;, '
                f'freeze_request_key=&quot;{REQUEST_KEY}&quot;)</code> puts it at <code>{SIF}</code>.') in sec
        lines = enter_image(rec, "hpc")
        assert lines[0] == "module load apptainer/1.3.2 nextflow/24.10.0" and lines[1].startswith("apptainer shell ")
        assert f"<pre>{_e(chr(10).join(lines))}</pre>" in sec
        assert SIF in lines[1]
        assert "Load the modules and enter the image." in sec
        assert "<pre>bash commands.sh</pre>" in sec
        assert html.count("apptainer shell") == 1 and html.count("module load") == 2   # the step and the header row

    def test_without_a_cluster_named_the_section_still_renders_with_a_placeholder_sif_and_no_module_line(self):
        rec = _record()
        html = _page(rec)
        sec = _section(html, "run-hpc")
        assert sec.index('class="warn-note">Rendered without a cluster named') < sec.index("<b>0.</b>")
        assert "module load" not in html
        assert "env=&lt;the cluster&#x27;s env&gt;" in sec
        assert f"freeze_request_key=&quot;{REQUEST_KEY}&quot;" in sec
        assert "rendered with <code>env=</code> naming the cluster" in sec
        assert "<code>sif:</code> in <code>params.yaml</code> must be set by hand" in sec
        assert SIF not in html
        assert f"<pre>{_e(enter_image(rec, 'hpc')[0])}</pre>" in sec
        assert "&lt;the .sif that stage_apptainer_image put in the cluster&#x27;s container zone&gt;" in sec
        assert "Enter the image. Apptainer must see every directory" in sec

    def test_a_cluster_with_no_modules_prints_no_module_line(self):
        sec = _section(_page(_record(compute_env="hpc", modules=[])), "run-hpc")
        assert "module load" not in sec and "Rendered without a cluster named" not in sec
        assert "Enter the image. Apptainer" in sec

    def test_nextflow_is_three_steps_ending_in_sbatch_and_says_how_to_watch_it(self):
        rec = _cluster()
        sec = _section(_page(rec), "run-hpc")
        nf = sec[sec.index("B. Every sample in <code>samples.csv</code>, with Nextflow"):]
        assert nf.count("<li>") == 3
        assert "<pre>cd /path/in/your/project/rnaseq_counts</pre>" in nf
        assert "Nothing to enter: <code>launcher.sh</code> loads the modules." in nf
        assert f"<pre>{_e(nextflow_run(rec, 'hpc')[0])}</pre>" == "<pre>sbatch launcher.sh</pre>"
        assert "<pre>sbatch launcher.sh</pre>" in nf
        assert "<code>runs/&lt;timestamp&gt;/trace.txt</code>" in nf and "<code>nextflow log</code>" in nf
        assert "<code>squeue -u $USER</code>" in nf and "<code>sacct -j &lt;jobid&gt;</code>" in nf

    def test_both_cluster_items_are_three_steps_with_a_cd_into_the_project_copy(self):
        sec = _section(_page(_cluster()), "run-hpc")
        assert sec.count("<ol>") == 2 and sec.count("<li>") == 6
        assert sec.count("<pre>cd /path/in/your/project/rnaseq_counts</pre>") == 2
        assert sec.count("in your project directory on the cluster") == 2


# ── stages ─────────────────────────────────────────────────────────────────────


class TestStages:
    def test_one_row_per_stage_in_execution_order_with_tool_and_howto_command_number(self):
        rec, html = _record(), _page()
        sec = _section(html, "stages")
        assert sec.count("<tr><td><code>") == len(rec.stages) == 3
        assert [sec.index(f"<tr><td><code>{s}</code></td>") for s in ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT")] == sorted(
            sec.index(f"<tr><td><code>{s}</code></td>") for s in ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT"))
        assert "<tr><td><code>HISAT2</code></td><td>hisat2</td><td>1</td>" in sec
        assert "<tr><td><code>SAMTOOLS</code></td><td>samtools</td><td>2</td>" in sec
        assert "<tr><td><code>HTSEQ_COUNT</code></td><td>htseq-count</td><td>3</td>" in sec
        merged = _record(stages=[[0, 1], [2]])
        assert "<td>1, 2</td>" in _stage_row(_page(merged), merged.stages[0].name)

    def test_the_image_cell_is_the_tag_and_short_digest_with_the_full_digest_in_a_title(self):
        row = _stage_row(_page(), "HISAT2")
        assert (f'<code>bioinf_rnaseq_cli:latest</code> <span class="muted" title="{DIGEST}">'
                f'{DIGEST.split(":")[1][:12]}</span>') in row

    def test_a_default_request_is_unsized_and_names_the_default_it_will_run_with(self):
        row = _stage_row(_page(), "HISAT2")
        assert '<span class="warn">unsized</span>' in row
        assert "(DEFAULT 4:00:00 · 8G · 1 cpu)" in row
        assert "cpus 8" not in row

    def test_a_caller_request_replaces_unsized_on_that_stage_only(self):
        html = _page(_record(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}}))
        assert "cpus 8 · mem 32G · time 4:00:00 · gpus 0" in _stage_row(html, "HISAT2")
        assert "unsized" not in _stage_row(html, "HISAT2")
        assert "unsized" in _stage_row(html, "SAMTOOLS")

    def test_a_partial_caller_request_states_what_was_not_requested(self):
        html = _page(_record(resources={"HISAT2": {"cpus": 4}}))
        assert "cpus 4 · mem not requested · time not requested · gpus 0" in _stage_row(html, "HISAT2")

    def test_a_gpu_the_sealed_run_used_is_shown_on_an_unsized_request(self):
        row = _stage_row(_page(_with_resources(_record(), "HISAT2", gpus=2)), "HISAT2")
        assert "(DEFAULT 4:00:00 · 8G · 1 cpu · 2 gpus)" in row

    def test_measured_numbers_are_printed_with_their_authority_word(self):
        row = _stage_row(_page(), "HISAT2")
        assert 'wall 68.0 s · peak RSS 1570 MB · CPU 100% — <span class="ok">authoritative</span>' in row

    #: The five states the record can carry, each with a marker no other state's rendering
    #: contains — so the assertion below can say "this one, and none of the others".
    AUTHORITY_MARKERS = {
        "authoritative": 'class="ok">authoritative</span>',
        "not_authoritative": 'class="warn">NOT authoritative</span>',
        "unrecorded": 'class="muted">authority unrecorded</span>',
        "mixed": 'class="warn">mixed authority</span>',
        "none": "no sealed step measured this stage",
    }

    @pytest.mark.parametrize("authority", sorted(AUTHORITY_MARKERS))
    def test_every_authority_state_renders_as_its_own_phrase_and_no_other(self, authority):
        rec = _with_resources(_record(), "HISAT2", measured_authority=authority)
        row = _stage_row(_page(rec), "HISAT2")
        assert self.AUTHORITY_MARKERS[authority] in row
        for other, marker in self.AUTHORITY_MARKERS.items():
            if other != authority:
                assert marker not in row, (authority, other)

    def test_an_authority_the_page_does_not_know_is_printed_raw_in_warning_colour_not_dropped(self):
        rec = _with_resources(_record(), "HISAT2", measured_authority="some_future_state")
        assert '<span class="warn">some_future_state</span>' in _stage_row(_page(rec), "HISAT2")

    def test_one_do_not_size_note_under_the_table_names_every_stage_whose_measurement_is_untrusted(self):
        assert 'class="warn-note"' not in _section(_page(), "stages")
        for authority in ("not_authoritative", "unrecorded", "mixed"):
            sec = _section(_page(_with_resources(_record(), "HISAT2", measured_authority=authority)), "stages")
            assert sec.count('class="warn-note"') == 1, authority
            note = sec[sec.index('class="warn-note"'):]
            assert note.startswith('class="warn-note">HISAT2: ') and "measured under emulation" in note
            assert "do not size from these; size from a run on hardware matching the image." in note
        two = _with_resources(_with_resources(_record(), "HISAT2", measured_authority="mixed"),
                              "HTSEQ_COUNT", measured_authority="not_authoritative")
        sec = _section(_page(two), "stages")
        assert sec.count('class="warn-note"') == 1 and 'class="warn-note">HISAT2, HTSEQ_COUNT: ' in sec

    def test_absent_measurements_say_so_instead_of_printing_a_number(self):
        rec = _with_resources(_record(), "HISAT2", measured_wall_seconds=None, measured_peak_rss_mb=None,
                              measured_max_cpu_percent=None, measured_authority="none", measured_on=None)
        row = _stage_row(_page(rec), "HISAT2")
        assert row.endswith('<td><span class="muted">no sealed step measured this stage</span></td>')
        assert "no measurement" not in row and " 0 MB" not in row and "peak RSS 1570" not in row
        rec = _with_resources(_record(), "HISAT2", measured_wall_seconds=None, measured_peak_rss_mb=None,
                              measured_max_cpu_percent=None)
        assert 'no measurement</span> — <span class="ok">authoritative</span>' in _stage_row(_page(rec), "HISAT2")
        rec = _with_resources(_record(), "HISAT2", measured_peak_rss_mb=None)
        assert "wall 68.0 s · peak RSS unrecorded · CPU 100%" in _stage_row(_page(rec), "HISAT2")

    def test_an_unrecorded_image_is_stated_not_blank(self):
        row = _stage_row(_page(_wide_record(n_stages=2)), "STAGE_00")
        assert '<td><span class="muted">unrecorded</span></td>' in row

    def test_there_are_no_per_stage_cards_or_sub_tables(self):
        html = _page()
        assert 'class="run-card"' not in html and 'id="stage-HISAT2"' not in html
        assert "<h3" not in _section(html, "stages")
        assert _section(html, "stages").count("<table>") == 1


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


_RECORDS = {"per_row": _record, "linear": _linear, "wide": _wide_record, "cluster": _cluster}


class TestPageShape:
    def test_the_footer_sentence_is_verbatim(self):
        assert FOOTER == ("This page shows a pipeline derived from a sealed run; it proves the render is "
                          "mechanical and traceable, never that the analysis is biologically right.")
        for make in _RECORDS.values():
            assert f'<p class="gen">{FOOTER}</p>' in _page(make())

    @pytest.mark.parametrize("which", sorted(_RECORDS))
    def test_every_tag_the_page_opens_is_closed_in_order(self, which):
        html = _page(_RECORDS[which]())
        b = _Balance()
        b.feed(html)
        b.close()
        assert not b.errors, b.errors[:5]
        assert not b.stack, b.stack
        for tag in ("table", "svg", "section", "div", "g", "pre", "tr", "td", "ol", "li"):
            assert html.count(f"<{tag}") == html.count(f"</{tag}>"), tag

    @pytest.mark.parametrize("which", sorted(_RECORDS))
    def test_the_sections_are_exactly_these_in_this_order_then_the_footer(self, which):
        html = _page(_RECORDS[which]())
        assert re.findall(r'<section class="bx" id="([^"]+)"', html) == SECTION_IDS
        assert html.index('id="stages"') < html.index(f'<p class="gen">{FOOTER}')

    def test_the_removed_sections_are_gone(self):
        html = _page(_cluster())
        for sid in ("samplesheet", "stages-cards", "defaults", "provenance", "howto", "notes"):
            assert f'id="{sid}"' not in html
        for gone in ("Bytes", "MANIFEST", "sha256sum -c", "run_all.sh", "run_local.sh", "nextflow_local.sh",
                     "--stages", "every derivation the caller did not dictate", "Inputs</h3>", "Outputs</h3>"):
            assert gone not in html, gone

    def test_the_same_record_renders_the_same_bytes(self):
        for make in (_record, _cluster):
            rec = make()
            assert render_pipeline_page(rec) == render_pipeline_page(rec)

    def test_the_page_renders_from_a_record_that_round_tripped_through_yaml(self, tmp_path):
        rec = _cluster()
        back = pr.load_pipeline_record(pr.write_pipeline_record(rec, tmp_path / "p"))
        assert render_pipeline_page(back) == render_pipeline_page(rec)

    def test_a_hostile_value_is_escaped_everywhere_including_inside_the_svg(self):
        rec = _record()
        params = [p.model_copy(update={"description": "<script>alert(1)</script>"}) if p.name == "GTF" else p
                  for p in rec.params]
        rows = [{**rec.samplesheet.rows[0], "sample": "<script>x</script>"}] + rec.samplesheet.rows[1:]
        sheet = rec.samplesheet.model_copy(update={"rows": rows})
        stages = [s.model_copy(update={"tool": 'x"><script>y</script>'}) if s.name == "SAMTOOLS" else s
                  for s in rec.stages]
        html = _page(rec.model_copy(update={"name": "<script>z</script>", "params": params,
                                            "samplesheet": sheet, "stages": stages}))
        for raw in ("<script>alert(1)</script>", "<script>x</script>", "<script>y</script>", "<script>z</script>"):
            assert raw not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in _section(html, "params")
        assert _section(html, "params").count("&lt;script&gt;x&lt;/script&gt;") == 2   # the example value + the row
        assert "&lt;script&gt;y" in _svg(html) and "&lt;script&gt;y" in _section(html, "stages")
        assert "<pre>cd /path/to/&lt;script&gt;z&lt;/script&gt;</pre>" in html
        assert html.count("<script>") == _page().count("<script>")                      # the shell's own JS only

    def test_a_record_with_no_outputs_still_renders(self):
        rec = _wide_record(n_stages=2)
        stages = [s.model_copy(update={"outputs": []}) for s in rec.stages]
        html = _page(rec.model_copy(update={"stages": stages}))
        assert "(nothing published)" in html
        assert re.findall(r'<section class="bx" id="([^"]+)"', html) == SECTION_IDS
