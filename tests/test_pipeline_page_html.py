"""The explain page: the header banner, how the files fit together, the picture, the
parameters and samples, how to run it locally, how to run it on the cluster, the
stages, the footer — and nothing else. A pipeline directory offers ONE way to run —
every row of samples.csv, with Nextflow — so the page shows three steps at home
(change directory, make nextflow available, run) and three on the cluster (change
directory, submit, watch), and never a by-hand item. Every run line it prints is the
one `pipeline_render_nextflow` spells (`RUN_LOCAL` / `RUN_HPC`), every stage command
is the line main.nf runs (`bound_commands`), and the two standing params are the pair
the renderer writes (`SAMPLESHEET_PARAM` / `OUTDIR_PARAM`), so the page and the files
cannot disagree. The page speaks the FILES' vocabulary — `params.gtf`, the `reads`
column, `<sample>.counts.tsv`, `results/<sample>/` — never the seal's `{PLACEHOLDER}`s;
and both pictures are faithful, non-overlapping drawings: the files picture of the
directory's wiring (what you copy and edit → main.nf → what a run writes, every box
line read off the record) and the stage picture of the record's graph, in which the
row key is drawn but never wired.

Rendered from the same fixture the record tests use (`sealed_rnaseq_spec`: three rows,
three stages, one image), its one-row, cluster-named, two-image and runtime-env
variants, plus a hand-built wide record — 12 stages, 12 params, a fan-out into three
side-by-side stages, a fan-in, two long edges — for the layout guarantees a
three-stage chain cannot exercise.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from pipeline_fixtures import DIGEST, GTF, INDEX, REQUEST_KEY, SAMPLES, TEMPLATES, sealed_rnaseq_spec

from agent.skills import pipeline_record as pr
from agent.skills.env_report_html import _close_page, _e, _open_page
from agent.skills.pipeline_page_html import FOOTER, files_layout, picture_layout, render_pipeline_page
from agent.skills.pipeline_render_nextflow import (OUTDIR_PARAM, RUN_HPC, RUN_LOCAL, SAMPLESHEET_PARAM,
                                                   bound_commands)

SPEC_PATH = "/ws/reports/rnaseq_counts_workflow.workflow.yaml"
SIF = "/cluster/containers/rnaseq_cli_48ac8c5b25d2.sif"
MODULES = ["apptainer/1.3.2", "nextflow/24.10.0"]
SECTION_IDS = ["files", "picture", "params", "run-local", "run-hpc", "stages"]
DIGEST_2 = "sha256:" + "c" * 64
#: How this machine provides nextflow, as the render tool records it: the checkout's
#: activate script, and the binary it puts on PATH — or None when it was not there.
ACTIVATE = "/ck/scripts/activate.sh"
NEXTFLOW = "/ck/.conda_runtime/bin/nextflow"
RUNTIME = pr.LocalRuntime(activate=ACTIVATE, nextflow=NEXTFLOW)
RUNTIME_NO_NEXTFLOW = pr.LocalRuntime(activate=ACTIVATE, nextflow=None)
#: Why the launch directory matters, verbatim — both cd steps and the files caption carry it.
LAUNCH_NOTE = ("Nextflow works out of the directory it is started in: <code>work/</code> (each task's "
               "sandbox), <code>.nextflow/</code> (what <code>-resume</code> reads) and the run records "
               "appear here, so launch from this directory every time.")
#: The seal's placeholders for the fixture — words the page must never speak.
PLACEHOLDERS = ["{SAMPLE}", "{READS}", "{GTF}", "{STRANDED}", "{HISAT2_INDEX}", "{OUTPUT_DIR}"]
#: The I6 reading of a placeholder, as the seal scans it.
_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")
#: The SVG attributes that ADDRESS a node or an edge for the hover JS. They are keyed by
#: the record's own names (a param's placeholder, an artifact's templated name) and a
#: reader never sees them; TestPageShape pins that they are the ONLY place a placeholder
#: survives.
_MACHINE_KEY_RE = re.compile(r' data-(?:id|name|from|to|artifacts)="[^"]*"')


def _record(**kw) -> pr.PipelineRecord:
    return pr.derive_pipeline_record(sealed_rnaseq_spec(), name="rnaseq_counts", spec_path=SPEC_PATH, **kw)


def _one_row() -> pr.PipelineRecord:
    """A one-trial seal: the same pipeline over a one-row samplesheet."""
    return pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="one", spec_path=SPEC_PATH)


def _cluster(**kw) -> pr.PipelineRecord:
    """The record rendered with a cluster named: env names, .sif paths and modules."""
    return _record(env_names={REQUEST_KEY: "rnaseq_cli"}, sif_paths={REQUEST_KEY: SIF},
                   compute_env="hpc", modules=MODULES, **kw)


def _local() -> pr.PipelineRecord:
    """The record rendered on a machine whose runtime env was recorded, nextflow in it."""
    return _record(local_runtime=RUNTIME)


def _local_no_nextflow() -> pr.PipelineRecord:
    """The record rendered on a machine whose runtime env was recorded without nextflow."""
    return _record(local_runtime=RUNTIME_NO_NEXTFLOW)


def _two_images(rec: pr.PipelineRecord | None = None) -> pr.PipelineRecord:
    """The record with its last stage moved onto a second image: two digests in the
    header, an Image column in the stages table, one cluster note per image."""
    rec = rec or _record()
    stages = [s.model_copy(update={"image": "other_img:2", "image_digest": DIGEST_2}) if s.name == "HTSEQ_COUNT"
              else s for s in rec.stages]
    return rec.model_copy(update={"stages": stages, "env_digests": [DIGEST, DIGEST_2]})


def _staged_but_one() -> pr.PipelineRecord:
    """Two images on a named cluster, the .sif staged for the first only."""
    two = _two_images()
    return two.model_copy(update={
        "stages": [s.model_copy(update={"sif_path": SIF}) if s.name != "HTSEQ_COUNT" else s for s in two.stages],
        "compute_env": "hpc", "modules": ["apptainer/1.3.2"]})


def _page(rec: pr.PipelineRecord | None = None) -> str:
    return render_pipeline_page(rec or _record())


def _svg(html: str, sid: str = "pipeline-picture") -> str:
    """One of the page's two pictures, by id — the stage picture unless asked otherwise."""
    start = html.index(f'<svg id="{sid}"')
    return html[start: html.index("</svg>", start) + len("</svg>")]


def _files_svg(html: str) -> str:
    return _svg(html, "pipeline-files")


def _section(html: str, sid: str) -> str:
    start = html.index(f'<section class="bx" id="{sid}"')
    return html[start: html.index("</section>", start) + len("</section>")]


def _banner(html: str) -> str:
    start = html.index('<div class="head">')
    return html[start: html.index("</table></div>", start) + len("</table></div>")]


def _banner_keys(html: str) -> list[str]:
    return re.findall(r'<td class="k">([^<]*)</td>', _banner(html))


def _stage_row(html: str, name: str) -> str:
    sec = _section(html, "stages")
    i = sec.index(f"<tr><td><code>{name}</code></td>")
    return sec[i: sec.index("</tr>", i)]


def _steps(sec: str) -> list[str]:
    """The <li> bodies of a section's one ordered list, in order."""
    return re.findall(r"<li>(.*?)</li>", sec, re.S)


def _bullets(sec: str) -> list[str]:
    """The <li> bodies of a section's one unordered list, in order."""
    ul = sec[sec.index("<ul>"): sec.index("</ul>")]
    return re.findall(r"<li>(.*?)</li>", ul, re.S)


def _conventions(sec: str) -> list[tuple[str, str, str]]:
    """The conventions table's rows: (convention, setting, decided by), cells verbatim."""
    return re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>", sec)


def _fnodes(rec: pr.PipelineRecord) -> dict:
    return {n.id: n for n in files_layout(rec).nodes}


class _Spoken(HTMLParser):
    """Everything a reader can see or hover: text nodes (the SVG's <title> tooltips
    included) and the human-facing attributes, entities decoded."""

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


def _with_stage(rec: pr.PipelineRecord, name: str, **fields) -> pr.PipelineRecord:
    """The record with one stage's fields replaced — every other field untouched."""
    return rec.model_copy(update={"stages": [s.model_copy(update=fields) if s.name == name else s
                                             for s in rec.stages]})


def _with_resources(rec: pr.PipelineRecord, name: str, **fields) -> pr.PipelineRecord:
    """The record with one stage's resources replaced — every other field untouched."""
    st = rec.stage(name)
    return _with_stage(rec, name, resources=st.resources.model_copy(update=fields))


def _sample_named(rec: pr.PipelineRecord) -> pr.PipelineRecord:
    """The fixture with `aligned.bam` renamed `{SAMPLE}.bam` throughout — HISAT2 writing
    a BAM named after the sample is the commonest how-to shape there is — so the
    artifact flowing between stages carries a placeholder."""
    def ren(s):
        return s.replace("aligned.bam", "{SAMPLE}.bam") if s else s
    stages = [s.model_copy(update={
        "commands": [ren(c) for c in s.commands],
        "inputs": [i.model_copy(update={"name": ren(i.name), "artifact": ren(i.artifact)}) for i in s.inputs],
        "outputs": [o.model_copy(update={"artifact": ren(o.artifact),
                                         "observed": o.observed.replace("aligned.bam", f"{SAMPLES[0]}.bam")
                                         if o.observed else None}) for o in s.outputs],
    }) for s in rec.stages]
    return rec.model_copy(update={"stages": stages})


def _key_sheet(*samples: str) -> pr.Samplesheet:
    """A samplesheet of the row key alone — what a how-to whose every input is shared
    gets: one row per trial, nothing but the sample's name in it."""
    return pr.Samplesheet(
        columns=[pr.SamplesheetColumn(name="sample", placeholder="SAMPLE", value_kind="value", format=None,
                                      description="the row key: it tags every task and names results/<sample>/")],
        rows=[{"sample": s} for s in samples])


def _wide_record(n_stages: int = 12, n_params: int = 12) -> pr.PipelineRecord:
    """A record with a fan-out (2 → 3, 4, 5), a fan-in (3, 4, 5 → 6), two long edges
    (4 → 9 and 0 → 11), three shared params per stage and a two-row key-only
    samplesheet, built straight through the models so the page and the layout are
    exercised on the widest shape they promise."""
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
        params=list(params.values()), samplesheet=_key_sheet("S1", "S2"), output_slots=["OUTPUT_DIR"],
        compute_env=None, modules=[], local_runtime=None, stages=stages, provenance_steps=[], unmatched_steps=[],
        defaults=[], notes=[])


def _with_cohort(rec: pr.PipelineRecord, *names: str) -> pr.PipelineRecord:
    return rec.model_copy(update={"stages": [s.model_copy(update={"scope": "cohort"}) if s.name in names else s
                                             for s in rec.stages]})


def _cohort() -> pr.PipelineRecord:
    """Three stages, the middle one run once over the cohort."""
    return _with_cohort(_wide_record(n_stages=3), "STAGE_01")


def _no_shared() -> pr.PipelineRecord:
    """A record whose every input is a samplesheet column: no shared params at all."""
    rec = _wide_record(n_stages=2).model_copy(update={"params": []})
    stages = [s.model_copy(update={"inputs": [i for i in s.inputs if i.origin == "stage"]}) for s in rec.stages]
    return rec.model_copy(update={"stages": stages})


def _no_outputs() -> pr.PipelineRecord:
    """Two stages that publish nothing."""
    rec = _wide_record(n_stages=2)
    return rec.model_copy(update={"stages": [s.model_copy(update={"outputs": []}) for s in rec.stages]})


# ── the header banner ──────────────────────────────────────────────────────────


class TestHeader:
    def test_the_header_names_the_sealed_workflow_its_path_and_the_env_digest_short_and_full(self):
        html = _page()
        assert "sealed workflow <b>rnaseq_counts_workflow</b>" in html
        assert f"<code>{SPEC_PATH}</code>" in html
        assert f"<b>{DIGEST.split(':')[1][:12]}</b> <code>{DIGEST}</code>" in html   # short form beside the full one
        assert ("3 in execution order: <code>HISAT2</code> → <code>SAMTOOLS</code> → "
                "<code>HTSEQ_COUNT</code>") in html

    def test_the_rows_are_exactly_these_in_this_order_and_cluster_only_when_an_env_was_named(self):
        rows = ["Rendered from", "Sealed workflow sha256", "Created", "Image", "Stages", "Samples"]
        assert _banner_keys(_page()) == rows
        assert _banner_keys(_page(_one_row())) == rows
        assert _banner_keys(_page(_two_images())) == rows
        assert _banner_keys(_page(_local())) == rows                     # the runtime env is a local step, not a fact
        assert _banner_keys(_page(_cluster())) == rows + ["Cluster"]
        assert _banner_keys(_page(_record(compute_env="hpc", modules=[]))) == rows + ["Cluster"]

    def test_the_pill_counts_the_example_rows_and_the_stages(self):
        assert '<span class="pill na">3 example rows · 3 stages</span>' in _page()
        assert '<span class="pill na">1 example row · 3 stages</span>' in _page(_one_row())
        two = pr.derive_pipeline_record(sealed_rnaseq_spec(templates=TEMPLATES[:2]), name="two")
        assert '<span class="pill na">3 example rows · 2 stages</span>' in _page(two)

    def test_the_samples_row_counts_the_example_rows_and_says_they_are_the_sealed_runs_own(self):
        assert ('<td class="k">Samples</td><td>3 example rows in <code>samples.csv</code> — the sealed '
                "run's own; replace them with yours</td>") in _page()
        assert ('<td class="k">Samples</td><td>1 example row in <code>samples.csv</code> — the sealed '
                "run's own; replace them with yours</td>") in _page(_one_row())

    def test_the_image_row_says_every_stage_runs_inside_it_only_when_there_is_one_image(self):
        one = _banner(_page())
        assert (f'<td class="k">Image</td><td><b>aaaa1111aaaa</b> <code>{DIGEST}</code> '
                '<span class="muted">— every stage runs inside it</span></td>') in one
        two = _banner(_page(_two_images()))
        assert (f'<td class="k">Image</td><td><b>aaaa1111aaaa</b> <code>{DIGEST}</code><br>'
                f'<b>cccccccccccc</b> <code>{DIGEST_2}</code></td>') in two
        assert "every stage runs inside it" not in two

    def test_the_sha256_row_states_absence_and_the_created_row_prints_the_record_value(self):
        rec = _record()
        html = _page(rec)
        assert '<td class="k">Sealed workflow sha256</td><td><span class="muted">unrecorded</span></td>' in html
        assert f'<td class="k">Created</td><td>{rec.created_at}</td>' in html
        sha = "deadbeef" * 8
        assert f'<td class="k">Sealed workflow sha256</td><td><code>{sha}</code></td>' in _page(_record(spec_sha256=sha))

    def test_the_cluster_row_names_the_env_and_its_modules_only_when_a_cluster_was_named(self):
        html = _page(_cluster())
        assert ('<td class="k">Cluster</td><td><b>hpc</b> — module load <code>apptainer/1.3.2</code> '
                '<code>nextflow/24.10.0</code></td>') in html
        assert '<td class="k">Cluster</td><td><b>hpc</b> — no modules to load</td>' in _page(
            _record(compute_env="hpc", modules=[]))
        assert '<td class="k">Cluster</td>' not in _page()

    def test_the_shape_and_output_slot_rows_are_gone(self):
        html = _page(_cluster())
        for gone in ('<td class="k">Shape</td>', '<td class="k">Output slots</td>', "per_row", "params only",
                     "{OUTPUT_DIR}", "Forms rendered", "Record version"):
            assert gone not in html, gone


# ── how the files fit together ─────────────────────────────────────────────────


#: The records the files picture is measured on: every shape it promises to draw.
_FILES_RECORDS = {"three_rows": _record, "one_row": _one_row, "cluster": _cluster, "two_images": _two_images,
                  "staged_but_one": _staged_but_one, "cohort": _cohort, "no_shared": _no_shared,
                  "no_outputs": _no_outputs, "wide": _wide_record, "local": _local}


class TestHowTheFilesFitTogether:
    def test_the_section_is_the_files_picture_its_hover_script_the_caption_then_the_conventions(self):
        sec = _section(_page(), "files")
        assert ('<h2>How the files fit together <span class="note">what you copy and edit → the engine → '
                "what a run writes</span></h2>") in sec
        svg = _files_svg(sec)
        assert svg.startswith('<svg id="pipeline-files" viewBox="0 0 ')
        assert ('aria-label="pipeline rnaseq_counts: the files you copy and edit feeding main.nf, and what a '
                'run writes"') in svg
        script = '</svg><script>(function(){var s=document.getElementById("pipeline-files");if(!s)return;'
        assert sec.count(script) == 1 and sec.count("<script>") == 1
        caption = ('<p class="note">Copy the whole directory next to the data and launch from inside it. '
                   + LAUNCH_NOTE + " Where the pipeline runs is the profile's business: <code>-profile local</code> "
                   "runs every task in the docker image, <code>-profile slurm</code> in the <code>.sif</code> "
                   "through apptainer, one SLURM job per task; <code>params.yaml</code> never says where. Hover a "
                   "box to trace what it feeds.</p>")
        assert caption in sec
        assert sec.index("</script>") < sec.index(caption) < sec.index("Our conventions") < sec.index("<table>")
        assert sec.count("<svg") == 1 and sec.count("<table>") == 1

    def test_three_columns_titled_what_you_edit_the_engine_what_a_run_writes_left_to_right(self):
        layout = files_layout(_record())
        assert [t for _, t in layout.titles] == ["what you copy and edit", "the engine", "what a run writes"]
        xs = [x for x, _ in layout.titles]
        by = {n.id: n for n in layout.nodes}
        left = [by[i] for i in ("f:samples.csv", "f:params.yaml", "f:nextflow.config", "f:launcher.sh")]
        right = [by[i] for i in ("f:results", "f:runs", "f:work")]
        assert xs == [left[0].x, by["f:main.nf"].x, right[0].x]           # each title sits over its column
        assert xs == sorted(xs) and len(set(xs)) == 3
        assert len({n.x for n in left}) == 1 and len({n.x for n in right}) == 1
        assert [n.y for n in left] == sorted(n.y for n in left) and [n.y for n in right] == sorted(n.y for n in right)
        for column in (left, right):                                    # stacked, never overlapping
            assert all(a.y + a.h < b.y for a, b in zip(column, column[1:]))
        svg = _files_svg(_page())
        assert '<text class="title" x="16.0" y="16.0">what you copy and edit</text>' in svg
        assert ">the engine</text>" in svg and ">what a run writes</text>" in svg

    def test_one_box_per_file_in_this_order_the_engine_in_the_middle_and_what_a_run_writes_marked_written(self):
        layout = files_layout(_record())
        assert [n.id for n in layout.nodes] == ["f:samples.csv", "f:params.yaml", "f:nextflow.config", "f:launcher.sh",
                                                "f:main.nf", "f:results", "f:runs", "f:work"]
        assert [n.name for n in layout.nodes] == ["samples.csv", "params.yaml", "nextflow.config", "launcher.sh",
                                                  "main.nf", "results/<sample>/", "runs/<timestamp>/", "work/"]
        assert [n.classes for n in layout.nodes] == ["", "", "", "", "engine", "written", "written", "written"]
        svg = _files_svg(_page())
        assert svg.count('data-node="file"') == 8
        assert 'class="node file" data-node="file" data-id="f:samples.csv" data-name="samples.csv"' in svg
        assert 'class="node file engine" data-node="file" data-id="f:main.nf" data-name="main.nf"' in svg
        assert ('class="node file written" data-node="file" data-id="f:results" '
                'data-name="results/&lt;sample&gt;/"') in svg
        assert 'class="node file written" data-node="file" data-id="f:work" data-name="work/"' in svg
        for name in ("samples.csv", "params.yaml", "nextflow.config", "launcher.sh", "main.nf", "work/"):
            assert f'<text class="l1" x="' in svg and f">{name}</text>" in svg, name

    def test_samples_csv_says_its_columns_its_row_key_and_how_many_example_rows_it_holds(self):
        n = _fnodes(_record())["f:samples.csv"]
        assert n.lines == ["columns: sample, reads", "sample = row key", "3 example rows, the sealed run's own"]
        assert n.tooltip == "samples.csv: one row per sample under one header line; params.samplesheet names it"
        assert _fnodes(_one_row())["f:samples.csv"].lines[2] == "1 example row, the sealed run's own"
        assert _fnodes(_wide_record())["f:samples.csv"].lines == ["columns: sample", "sample = row key",
                                                                  "2 example rows, the sealed run's own"]
        svg = _files_svg(_page())
        assert ">columns: sample, reads</text>" in svg and ">sample = row key</text>" in svg
        assert ">3 example rows, the sealed run&#x27;s own</text>" in svg

    def test_params_yaml_lists_the_shared_params_then_the_samplesheet_and_the_output_directory(self):
        n = _fnodes(_record())["f:params.yaml"]
        assert n.lines == ["hisat2_index, stranded, gtf", "samplesheet: samples.csv", "outdir: results"]
        assert n.lines[1:] == [f"{SAMPLESHEET_PARAM[0]}: {SAMPLESHEET_PARAM[1]}", f"{OUTDIR_PARAM[0]}: {OUTDIR_PARAM[1]}"]
        assert n.tooltip == "params.yaml: the parameters the pipeline runs with, the samplesheet, the output directory"
        assert _fnodes(_no_shared())["f:params.yaml"].lines == ["no shared parameters", "samplesheet: samples.csv",
                                                                "outdir: results"]
        wide = _fnodes(_wide_record())["f:params.yaml"].lines
        assert " ".join(wide[:-2]).split(", ") == [f"param_{i:02d}_long" for i in range(12)]   # wrapped, nothing lost
        assert all(len(ln) <= 34 for ln in wide) and len(wide) == 8
        assert wide[-2:] == ["samplesheet: samples.csv", "outdir: results"]
        svg = _files_svg(_page())
        assert ">hisat2_index, stranded, gtf</text>" in svg and ">samplesheet: samples.csv</text>" in svg
        assert ">outdir: results</text>" in svg

    def test_nextflow_config_names_what_each_profile_runs_in_and_says_set_me_when_no_sif_is_recorded(self):
        n = _fnodes(_record())["f:nextflow.config"]
        assert n.lines == ["-profile local → docker", "↳ bioinf_rnaseq_cli:latest", "-profile slurm → apptainer",
                           "↳ container = '' (SET ME)", "sizing per stage · run records"]
        assert n.tooltip == ("nextflow.config: each profile names the executor and the image every task runs in. "
                             ".sif: no .sif path recorded")
        c = _fnodes(_cluster())["f:nextflow.config"]
        assert c.lines[3] == "↳ rnaseq_cli_48ac8c5b25d2.sif on hpc"       # the box: the basename and where
        assert c.tooltip.endswith(f".sif: {SIF}")                           # the tooltip: the full path
        assert c.lines[:3] == n.lines[:3] and c.lines[4:] == n.lines[4:]
        rec = _record()
        untagged = rec.model_copy(update={"stages": [s.model_copy(update={"image": None}) for s in rec.stages]})
        assert _fnodes(untagged)["f:nextflow.config"].lines[1] == "↳ aaaa1111aaaa"
        assert _fnodes(_wide_record())["f:nextflow.config"].lines[1] == "↳ image unrecorded"
        svg = _files_svg(_page(_cluster()))
        assert f".sif: {SIF}</title>" in svg and ">↳ rnaseq_cli_48ac8c5b25d2.sif on hpc</text>" in svg
        assert ">↳ container = &#x27;&#x27; (SET ME)</text>" in _files_svg(_page())
        assert ">-profile local → docker</text>" in svg and ">-profile slurm → apptainer</text>" in svg

    def test_with_several_images_nextflow_config_names_one_image_per_stage_under_each_profile(self):
        n = _fnodes(_two_images())["f:nextflow.config"]
        assert n.lines == ["-profile local → docker", "↳ HISAT2: bioinf_rnaseq_cli:latest",
                           "↳ SAMTOOLS: bioinf_rnaseq_cli:latest", "↳ HTSEQ_COUNT: other_img:2",
                           "-profile slurm → apptainer", "↳ HISAT2: container = '' (SET ME)",
                           "↳ SAMTOOLS: container = '' (SET ME)", "↳ HTSEQ_COUNT: container = '' (SET ME)",
                           "sizing per stage · run records"]
        assert n.tooltip.endswith(".sif: HISAT2: no .sif path recorded; SAMTOOLS: no .sif path recorded; "
                                  "HTSEQ_COUNT: no .sif path recorded")
        s = _fnodes(_staged_but_one())["f:nextflow.config"]
        assert s.lines[5:8] == ["↳ HISAT2: rnaseq_cli_48ac8c5b25d2.sif on hpc",
                                "↳ SAMTOOLS: rnaseq_cli_48ac8c5b25d2.sif on hpc",
                                "↳ HTSEQ_COUNT: container = '' (SET ME)"]
        assert s.tooltip.endswith(f".sif: HISAT2: {SIF}; SAMTOOLS: {SIF}; HTSEQ_COUNT: no .sif path recorded")

    def test_launcher_sh_says_what_it_loads_in_each_cluster_state(self):
        head = ["sbatch: the manager job", "runs nextflow -profile slurm"]
        assert _fnodes(_record())["f:launcher.sh"].lines == head + ["rendered without a cluster named:",
                                                                    "loads no modules"]
        assert _fnodes(_cluster())["f:launcher.sh"].lines == head + ["module load apptainer/1.3.2", "nextflow/24.10.0"]
        assert _fnodes(_record(compute_env="hpc", modules=[]))["f:launcher.sh"].lines == head + [
            "loads no modules: the env declares none"]
        assert _fnodes(_record(compute_env="hpc", modules=["apptainer/1.3.2"]))["f:launcher.sh"].lines == head + [
            "module load apptainer/1.3.2"]
        assert _fnodes(_record())["f:launcher.sh"].tooltip == (
            "launcher.sh: the SLURM manager job; it loads the modules and runs nextflow, which submits one job per task")
        html = _page(_cluster())
        fsvg = _files_svg(html)
        assert ">module load apptainer/1.3.2</text>" in fsvg and ">nextflow/24.10.0</text>" in fsvg
        assert "module load" not in _svg(html)

    def test_main_nf_is_the_engine_naming_the_stages_in_order_and_how_often_each_runs(self):
        n = _fnodes(_record())["f:main.nf"]
        assert n.classes == "engine"
        assert n.lines == ["HISAT2 → SAMTOOLS → HTSEQ_COUNT", "one process per stage,",
                           "run once per samples.csv row"]
        assert n.tooltip == "main.nf: one process per stage, the sealed command with its placeholders bound"
        assert _fnodes(_cohort())["f:main.nf"].lines == ["STAGE_00 → STAGE_01 → STAGE_02",
                                                         "per-sample stages: once per row;", "cohort stages: once"]
        wide = _fnodes(_wide_record())["f:main.nf"].lines
        assert " ".join(wide[:-2]) == " → ".join(f"STAGE_{i:02d}" for i in range(12))   # wrapped, in order
        assert all(len(ln) <= 34 for ln in wide) and len(wide) == 6
        none = _wide_record(n_stages=2).model_copy(update={"stages": []})
        assert _fnodes(none)["f:main.nf"].lines[0] == "(no stages)"
        assert ">HISAT2 → SAMTOOLS → HTSEQ_COUNT</text>" in _files_svg(_page())

    def test_what_a_run_writes_is_results_per_sample_the_run_records_and_work(self):
        by = _fnodes(_record())
        r = by["f:results"]
        assert (r.name, r.lines, r.classes) == ("results/<sample>/", ["aligned.bam, aligned.bam.bai,",
                                                                      "<sample>.counts.tsv"], "written")
        assert r.tooltip == "results/<sample>/: every file a per-sample stage writes, published by copy"
        assert by["f:runs"].lines == ["trace.txt: every task's command", "report.html: time, memory, CPU"]
        assert by["f:runs"].tooltip == "runs/<timestamp>/: the record of one run; the launch line is in nextflow log"
        assert by["f:work"].lines == ["every task's sandbox", "never cleaned for you:", "nextflow clean -f"]
        assert by["f:work"].tooltip == "work/: where tasks run and -resume finds their results"
        assert "f:results-cohort" not in by
        assert _fnodes(_sample_named(_record()))["f:results"].lines == ["<sample>.bam, <sample>.bam.bai,",
                                                                        "<sample>.counts.tsv"]
        assert _fnodes(_no_outputs())["f:results"].lines == ["(nothing published)"]
        wide = _fnodes(_wide_record())["f:results"].lines
        assert " ".join(wide).split(", ") == [f"artifact_{i:02d}.bam" for i in range(12)]
        svg = _files_svg(_page())
        assert ">aligned.bam, aligned.bam.bai,</text>" in svg and ">&lt;sample&gt;.counts.tsv</text>" in svg
        assert ">results/&lt;sample&gt;/</text>" in svg and ">runs/&lt;timestamp&gt;/</text>" in svg

    def test_a_cohort_stage_publishes_into_results_flat_a_second_written_box_drawn_only_then(self):
        layout = files_layout(_cohort())
        assert [n.id for n in layout.nodes] == ["f:samples.csv", "f:params.yaml", "f:nextflow.config", "f:launcher.sh",
                                                "f:main.nf", "f:results", "f:results-cohort", "f:runs", "f:work"]
        by = {n.id: n for n in layout.nodes}
        assert (by["f:results-cohort"].name, by["f:results-cohort"].lines, by["f:results-cohort"].classes) == (
            "results/", ["artifact_01.bam"], "written")
        assert by["f:results-cohort"].tooltip == "results/: what the cohort stage(s) publish"
        assert by["f:results"].lines == ["artifact_00.bam,", "artifact_02.bam"]      # the per-sample stages' only
        assert [(e.src, e.dst, e.label) for e in layout.edges if e.dst.startswith("f:results")] == [
            ("f:main.nf", "f:results", "publishDir"), ("f:main.nf", "f:results-cohort", "publishDir")]
        svg = _files_svg(_page(_cohort()))
        assert 'data-id="f:results-cohort" data-name="results/"' in svg and svg.count(">publishDir</text>") == 2
        silent = _cohort()
        silent = silent.model_copy(update={"stages": [s.model_copy(update={"outputs": []}) if s.name == "STAGE_01"
                                                      else s for s in silent.stages]})
        assert "f:results-cohort" not in _fnodes(silent)                      # a cohort stage publishing nothing
        assert _fnodes(silent)["f:main.nf"].lines[1:] == ["per-sample stages: once per row;", "cohort stages: once"]

    def test_one_wire_from_each_edited_file_into_main_nf_and_one_out_to_each_thing_a_run_writes(self):
        layout = files_layout(_record())
        wires = [("wire", "f:samples.csv", "f:main.nf", "params.samplesheet"),
                 ("wire", "f:params.yaml", "f:main.nf", "-params-file"),
                 ("wire", "f:nextflow.config", "f:main.nf", "-profile local | slurm"),
                 ("wire", "f:launcher.sh", "f:main.nf", "sbatch (cluster only)"),
                 ("wire out", "f:main.nf", "f:results", "publishDir"),
                 ("wire out", "f:main.nf", "f:runs", "trace · report"),
                 ("wire out", "f:main.nf", "f:work", "tasks run here")]
        assert [(e.kind, e.src, e.dst, e.label) for e in layout.edges] == wires
        assert layout.edges[0].label == f"params.{SAMPLESHEET_PARAM[0]}"
        assert all(e.path.startswith("M") and " C" in e.path for e in layout.edges)
        svg = _files_svg(_page())
        assert svg.count('data-edge="wire"') == 7 and svg.count('class="edge wire"') == 4
        assert svg.count('class="edge wire out"') == 3
        for kind, src, dst, label in wires:
            assert f'<path class="edge {kind}" data-edge="wire" data-from="{src}" data-to="{dst}" d="M' in svg
            assert re.search(rf'<g class="lbl" data-edge="wire-label" data-from="{re.escape(src)}" '
                             rf'data-to="{re.escape(dst)}"><rect [^>]*/><text [^>]*>{re.escape(_e(label))}</text></g>',
                             svg), label
        labels = re.findall(r'<g class="lbl" data-edge="wire-label"[^>]*>.*?<text[^>]*>([^<]*)</text></g>', svg)
        assert labels == [w[3] for w in wires]

    @pytest.mark.parametrize("which", sorted(_FILES_RECORDS))
    def test_no_text_overlaps_no_label_touches_a_box_and_nothing_leaves_the_viewbox(self, which):
        rec = _FILES_RECORDS[which]()
        layout = files_layout(rec)
        texts = layout.texts
        assert len(texts) >= 40
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
        svg = _files_svg(render_pipeline_page(rec))
        m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        assert m and (int(m.group(1)), int(m.group(2))) == (round(layout.width), round(layout.height))
        assert 'width="100%"' in svg and f"max-width:{m.group(1)}px" in svg

    def test_the_fixture_picture_stays_narrow_enough_to_render_at_natural_size_because_its_lines_wrap(self):
        layout = files_layout(_record())
        assert layout.width < 1100
        by = {n.id: n for n in layout.nodes}
        for nid in ("f:params.yaml", "f:main.nf"):                       # the wrapped lists stay within budget
            assert all(len(ln) <= 34 for ln in by[nid].lines), nid
        assert all(len(ln) <= 30 for ln in by["f:results"].lines)
        assert max(n.x + n.w for n in layout.nodes) < 1100
        m = re.search(r'viewBox="0 0 (\d+) ', _files_svg(_page()))
        assert m and int(m.group(1)) < 1100

    def test_the_layout_is_computed_from_the_record_alone_and_is_the_same_every_time(self):
        rec = _record()
        assert files_layout(rec) == files_layout(rec)
        assert files_layout(_record()) == files_layout(_record())      # two derivations, no clock in the geometry
        assert files_layout(_wide_record()) == files_layout(_wide_record())
        one, three = files_layout(_one_row()), files_layout(_record())
        assert [n.id for n in one.nodes] == [n.id for n in three.nodes]
        assert [(e.src, e.dst, e.label) for e in one.edges] == [(e.src, e.dst, e.label) for e in three.edges]
        for a, b in zip(one.nodes, three.nodes):                       # only the rows line differs
            if a.id == "f:samples.csv":
                assert (a.lines[2], b.lines[2]) == ("1 example row, the sealed run's own",
                                                    "3 example rows, the sealed run's own")
                assert a.lines[:2] == b.lines[:2]
            else:
                assert (a.lines, a.tooltip) == (b.lines, b.tooltip)

    def test_the_files_picture_speaks_the_files_words_and_no_placeholder_not_even_in_its_machine_keys(self):
        svg = _files_svg(_page(_sample_named(_cluster())))
        assert not _PLACEHOLDER_RE.search(svg)                            # a file id carries no record name
        spoken = _spoken(svg)
        assert not _PLACEHOLDER_RE.search(spoken)
        for word in ("samples.csv", "params.yaml", "nextflow.config", "launcher.sh", "main.nf", "results/<sample>/",
                     "runs/<timestamp>/", "work/", "params.samplesheet", "-params-file", "-profile local | slurm",
                     "sbatch (cluster only)", "publishDir", "trace · report", "tasks run here", "<sample>.bam",
                     "<sample>.counts.tsv", SIF, "module load apptainer/1.3.2"):
            assert word in spoken, word
        assert "SAMPLE" not in spoken.replace("SAMPLES", "") and "OUTPUT_DIR" not in spoken

    def test_each_picture_gets_its_own_hover_script_and_nothing_else_on_the_page_is_scripted(self):
        html = _page()
        for sid in ("pipeline-files", "pipeline-picture"):
            assert html.count(f'<svg id="{sid}"') == 1
            assert html.count(f'</svg><script>(function(){{var s=document.getElementById("{sid}");if(!s)return;') == 1
        assert html.count('getElementById("pipeline-') == 2 and html.index('id="pipeline-files"') < html.index(
            'id="pipeline-picture"')
        assert html.count("<script>") == (_open_page("x") + _close_page("")).count("<script>") + 2
        assert html.count('classList.add("src")') == 2                  # one hover routine per picture


class TestConventions:
    def test_the_table_is_every_record_default_in_words_with_who_decided_it(self):
        rec = _record()
        sec = _section(_page(rec), "files")
        assert ('<p class="note">Our conventions — the same for every pipeline this system renders, unless a row '
                'says otherwise:</p><div class="tbl-wrap"><table><tr><th>Convention</th><th>Setting</th>'
                "<th>Decided by</th></tr>") in sec
        rows = _conventions(sec)
        assert [d.key for d in rec.defaults] == ["stage_cut", "publish", "resume", "errors", "cache", "queue_size",
                                                 "run_records", "cleanup", "sheet_preflight", "resources"]
        assert [r[0] for r in rows] == ["Stage cut", "Publishing", "Resume", "Errors", "Cache", "Queue size",
                                        "Run records", "Cleanup", "Samplesheet check", "Resources"]
        assert all(d.source == "default" for d in rec.defaults) and all(r[2] == "our default" for r in rows)
        assert rows[0] == ("Stage cut", "one stage per how-to command", "our default")
        assert rows[5] == ("Queue size", "50", "our default")
        assert rows[8] == ("Samplesheet check", "the samplesheet and every file column are checked as the run starts",
                           "our default")
        assert [r[1] for r in rows] == [_e(d.value) if "`" not in d.value else r[1] for d, r in zip(rec.defaults, rows)]
        assert len(rows) == len(rec.defaults) == 10

    def test_a_values_backticked_spans_are_code_and_everything_is_escaped(self):
        rows = _conventions(_section(_page(), "files"))
        assert rows[7] == ("Cleanup", "never automatic; <code>nextflow clean -f</code> when you are done", "our default")
        assert rows[6] == ("Run records", "trace (each task&#x27;s command) + report under runs/&lt;timestamp&gt;/; "
                           "the launch line in <code>nextflow log</code>", "our default")
        assert "`" not in "".join("".join(r) for r in rows)

    def test_who_decided_reads_as_our_default_set_when_rendered_or_from_the_seal(self):
        assert _conventions(_section(_page(_record(stages=[[0, 1], [2]])), "files"))[0] == (
            "Stage cut", "explicit groups", "set when rendered")
        rec = _record().model_copy(update={"defaults": [
            pr.PipelineDefault(key="stage_cut", value="explicit groups", source="caller"),
            pr.PipelineDefault(key="publish", value="only what the seal declared", source="seal"),
            pr.PipelineDefault(key="future_key", value="a `quoted` value", source="default")]})
        assert _conventions(_section(_page(rec), "files")) == [
            ("Stage cut", "explicit groups", "set when rendered"),
            ("Publishing", "only what the seal declared", "from the seal"),
            ("future_key", "a <code>quoted</code> value", "our default")]   # an unknown key is printed raw, not dropped

    def test_a_record_stating_no_conventions_says_so(self):
        sec = _section(_page(_wide_record()), "files")
        assert '<p class="empty">the record states no conventions</p>' in sec
        assert "<table>" not in sec and "Our conventions" not in sec
        assert sec.endswith('<p class="empty">the record states no conventions</p></div></section>')


# ── the picture ────────────────────────────────────────────────────────────────


class TestThePicture:
    def test_one_node_per_stage_column_shared_param_and_output(self):
        rec, svg = _record(), _svg(_page())
        assert svg.count('data-node="stage"') == len(rec.stages) == 3
        assert svg.count('data-node="column"') == len(rec.samplesheet.columns) == 2
        # a per-sample param IS its samplesheet column; only the shared params get param nodes
        shared = [p for p in rec.params if p.kind == "shared"]
        assert svg.count('data-node="param"') == len(shared) == 3
        assert svg.count('data-node="column"') + svg.count('data-node="param"') == len(rec.params)
        outputs = [o for s in rec.stages for o in s.outputs]   # every artifact a stage writes is published
        assert svg.count('data-node="output"') == len(outputs) == 3
        for p in rec.params:
            assert f'data-name="{p.name}"' in svg
        assert 'data-node="file"' not in svg                                # the files boxes are the other picture's

    def test_the_column_titles_and_the_group_headers_are_the_files_names_left_to_right(self):
        layout = picture_layout(_record())
        assert [t for _, t in layout.titles] == ["samples.csv & params.yaml", "stages, in execution order",
                                                 "published to results/"]
        xs = [x for x, _ in layout.titles]
        assert xs == sorted(xs) and len(set(xs)) == 3
        assert [h for _, _, h in layout.headers] == ["samples.csv", "params.yaml"]
        svg = _svg(_page())
        assert '<text class="title" x="16.0" y="16.0">samples.csv &amp; params.yaml</text>' in svg
        assert ">stages, in execution order</text>" in svg and ">published to results/</text>" in svg
        assert svg.index('class="grp" x="16.0" y="44.0">samples.csv</text>') < svg.index(">params.yaml</text>")

    def test_the_row_key_is_drawn_as_the_key_column_and_never_wired_to_a_stage(self):
        """`sample` is what every task is tagged by and every results directory named
        after — not data a stage reads. It gets a node (bold, cyan) and no edge, even
        though HTSEQ_COUNT names `{SAMPLE}` in its command."""
        rec = _record()
        assert any(i.name == "SAMPLE" and i.origin == "column" for i in rec.stage("HTSEQ_COUNT").inputs)
        layout = picture_layout(rec)
        key = next(n for n in layout.nodes if n.id == "c:SAMPLE")
        assert (key.kind, key.classes, key.line1, key.line2) == ("column", "key", "sample", "row key")
        assert key.tooltip == "samples.csv column sample — the row key: it tags every task and names results/<sample>/"
        assert not [e for e in layout.edges if e.src == "c:SAMPLE" or e.dst == "c:SAMPLE"]
        assert not [e for e in layout.edges if e.kind == "input" and e.src == "c:SAMPLE"]
        svg = _svg(_page())
        assert 'class="node column key" data-node="column" data-id="c:SAMPLE" data-name="SAMPLE"' in svg
        assert 'data-from="c:SAMPLE"' not in svg and 'data-to="c:SAMPLE"' not in svg
        assert ">sample</text>" in svg and ">row key</text>" in svg

    def test_the_other_columns_and_the_shared_params_speak_the_files_vocabulary(self):
        layout = picture_layout(_record())
        by = {n.id: n for n in layout.nodes}
        assert (by["c:READS"].kind, by["c:READS"].line1, by["c:READS"].line2) == ("column", "reads", "column · path")
        assert by["c:READS"].tooltip == "samples.csv column reads · path · fastq — single-end reads"
        assert (by["p:GTF"].kind, by["p:GTF"].line1, by["p:GTF"].line2) == ("param", "params.gtf", "path")
        assert (by["p:HISAT2_INDEX"].line1, by["p:HISAT2_INDEX"].line2) == ("params.hisat2_index", "prefix")
        assert (by["p:STRANDED"].line1, by["p:STRANDED"].line2) == ("params.stranded", "value")
        assert by["p:GTF"].tooltip == f"params.gtf · a path · example {GTF} — gene annotation"
        assert "p:READS" not in by and "p:SAMPLE" not in by       # per-sample params are not drawn as params
        svg = _svg(_page())
        for shown in ("params.gtf", "params.hisat2_index", "params.stranded", "reads", "column · path"):
            assert f">{shown}</text>" in svg, shown

    def test_stage_nodes_say_the_tool_and_whether_they_run_per_sample_or_over_the_cohort(self):
        by = {n.id: n for n in picture_layout(_record()).nodes}
        assert by["s:HISAT2"].line2 == "hisat2 · per sample"
        assert by["s:HTSEQ_COUNT"].line2 == "htseq-count · per sample"
        assert by["s:HISAT2"].tooltip == ("stage 1 HISAT2 · hisat2 · runs once per sample · image aaaa1111aaaa · "
                                          "unsized (default request) · measured authority authoritative")
        sized = _record(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}})
        assert "request cpus 8 · mem 32G · time 4:00:00 · gpus 0" in picture_layout(sized).nodes[5].tooltip
        by = {n.id: n for n in picture_layout(_cohort()).nodes}
        assert by["s:STAGE_01"].line2 == "tool1 · cohort" and by["s:STAGE_00"].line2 == "tool0 · per sample"
        assert "runs once over the cohort" in by["s:STAGE_01"].tooltip
        assert ">hisat2 · per sample</text>" in _svg(_page())

    def test_output_nodes_are_named_as_the_files_and_placed_in_their_results_directory(self):
        by = {n.id: n for n in picture_layout(_record()).nodes}
        counts = by["o:HTSEQ_COUNT/{SAMPLE}.counts.tsv"]
        assert (counts.kind, counts.line1, counts.line2) == ("output", "<sample>.counts.tsv", "results/<sample>/")
        assert counts.tooltip == ("results/<sample>/<sample>.counts.tsv · published by HTSEQ_COUNT · declared as "
                                  f"*.counts.tsv · observed as {SAMPLES[0]}.counts.tsv in the sealed run")
        assert (by["o:HISAT2/aligned.bam"].line1, by["o:HISAT2/aligned.bam"].line2) == ("aligned.bam", "results/<sample>/")
        by = {n.id: n for n in picture_layout(_cohort()).nodes}
        assert by["o:STAGE_01/artifact_01.bam"].line2 == "results/"
        assert by["o:STAGE_00/artifact_00.bam"].line2 == "results/<sample>/"
        assert by["o:STAGE_01/artifact_01.bam"].tooltip.endswith(" · never observed in the sealed run")
        svg = _svg(_page())
        assert ">&lt;sample&gt;.counts.tsv</text>" in svg and ">results/&lt;sample&gt;/</text>" in svg
        assert 'data-edge="publish" data-from="s:HTSEQ_COUNT" data-to="o:HTSEQ_COUNT/{SAMPLE}.counts.tsv"' in svg

    def test_stage_to_stage_edges_carry_their_artifact_labels(self):
        svg = _svg(_page())
        edges = re.findall(r'<path class="edge stage" data-edge="stage" data-from="s:(\w+)" data-to="s:(\w+)" '
                           r'data-artifacts="([^"]*)"', svg)
        assert sorted(edges) == [("HISAT2", "HTSEQ_COUNT", "aligned.bam"), ("HISAT2", "SAMTOOLS", "aligned.bam"),
                                 ("SAMTOOLS", "HTSEQ_COUNT", "aligned.bam.bai")]
        labels = re.findall(r'<g class="lbl" data-edge="stage-label"[^>]*>.*?<text[^>]*>([^<]*)</text></g>', svg)
        assert sorted(labels) == ["aligned.bam", "aligned.bam", "aligned.bam.bai"]

    def test_an_artifact_named_after_the_sample_is_labelled_on_its_edges_as_the_files_name_it(self):
        """The vocabulary rule on the cyan edges. HISAT2 writing `{SAMPLE}.bam` publishes
        a node that reads `<sample>.bam`; the edge carrying the same file to SAMTOOLS
        must read the same — the picture cannot call one file by two names, and the
        seal's placeholder is not a name a reader of the files knows."""
        rec = _sample_named(_record())
        layout = picture_layout(rec)
        by = {n.id: n for n in layout.nodes}
        assert by["o:HISAT2/{SAMPLE}.bam"].line1 == "<sample>.bam"
        labels = {(e.src, e.dst): e.label for e in layout.edges if e.kind == "stage"}
        assert labels[("s:HISAT2", "s:SAMTOOLS")] == "<sample>.bam"
        assert labels[("s:HISAT2", "s:HTSEQ_COUNT")] == "<sample>.bam"
        assert labels[("s:SAMTOOLS", "s:HTSEQ_COUNT")] == "<sample>.bam.bai"
        assert [t.text for t in layout.texts if t.role == "label"] == ["<sample>.bam", "<sample>.bam", "<sample>.bam.bai"]
        assert "{SAMPLE}" not in _spoken(_svg(_page(rec)))

    def test_every_column_and_param_a_stage_reads_is_an_edge_into_it_except_the_row_key(self):
        rec, svg = _record(), _svg(_page())
        key = rec.samplesheet.columns[0].placeholder
        cols = {c.placeholder for c in rec.samplesheet.columns}
        expected = 0
        for s in rec.stages:
            for i in s.inputs:
                if i.origin in ("param", "column") and i.name != key:
                    src = f"c:{i.name}" if i.name in cols else f"p:{i.name}"
                    assert f'data-edge="input" data-from="{src}" data-to="s:{s.name}"' in svg
                    expected += 1
        assert svg.count('data-edge="input"') == expected == 4

    def test_the_machine_keys_carry_the_records_own_names_and_nothing_a_reader_sees_does(self):
        """The hover JS matches `data-from`/`data-to` against `data-id`, so the picture
        addresses nodes by the record's own names (a param's placeholder, an artifact's
        templated name); those attributes are never rendered. Strip them and no
        placeholder is left anywhere in the picture."""
        svg = _svg(_page())
        assert 'data-id="o:HTSEQ_COUNT/{SAMPLE}.counts.tsv" data-name="{SAMPLE}.counts.tsv"' in svg
        assert 'data-id="p:HISAT2_INDEX" data-name="HISAT2_INDEX"' in svg
        assert not _PLACEHOLDER_RE.search(_MACHINE_KEY_RE.sub("", svg))
        assert not _PLACEHOLDER_RE.search(_spoken(svg))

    def test_the_legend_says_every_stage_runs_per_row_and_that_sample_is_the_row_key(self):
        sec = _section(_page(), "picture")
        assert ('<h2>The picture <span class="note">samples.csv and params.yaml → stages in execution order → '
                "published outputs</span></h2>") in sec
        assert ('<p class="note">Every stage runs once per row of <code>samples.csv</code>; <code>sample</code> '
                "is the row key — it tags each task and names <code>results/&lt;sample&gt;/</code>. Thin grey "
                "lines: a column or <code>params.*</code> value a stage reads.") in sec
        assert "A dashed stage box is <b>unsized</b> (default request)." in sec
        assert "Hover or focus a node to trace it.</p>" in sec

    def test_a_record_with_no_shared_params_says_none_under_params_yaml(self):
        layout = picture_layout(_no_shared())
        assert [h for _, _, h in layout.headers] == ["samples.csv", "params.yaml", "(none)"]
        assert [n.id for n in layout.nodes if n.kind in ("column", "param")] == ["c:SAMPLE"]

    def test_the_svg_carries_a_viewbox_and_scales_down_to_its_container(self):
        svg = _svg(_page())
        m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        assert m and int(m.group(1)) > 0 and int(m.group(2)) > 0
        assert 'width="100%"' in svg and f"max-width:{m.group(1)}px" in svg

    def test_a_chain_is_one_stage_per_row_in_execution_order(self):
        assert picture_layout(_record()).rows == [["HISAT2"], ["SAMTOOLS"], ["HTSEQ_COUNT"]]

    def test_a_one_row_seal_draws_the_same_picture_as_the_three_row_one(self):
        """The example rows are data for the samplesheet table, not for the picture: a
        one-trial seal derives the same columns, params, stages and outputs."""
        assert picture_layout(_one_row()) == picture_layout(_record())

    def test_stages_of_one_rank_sit_side_by_side_on_a_wide_record(self):
        layout = picture_layout(_wide_record())
        assert ["STAGE_03", "STAGE_04", "STAGE_05"] in layout.rows
        row = [n for n in layout.nodes if n.name in ("STAGE_03", "STAGE_04", "STAGE_05")]
        assert len({n.y for n in row}) == 1
        xs = sorted((n.x, n.x + n.w) for n in row)
        assert all(a[1] < b[0] for a, b in zip(xs, xs[1:]))

    def test_no_text_overlaps_and_nothing_leaves_the_viewbox_on_a_wide_record(self):
        """Twelve stages, twelve params, the key column, a three-wide rank, two long
        edges: every label the picture draws must clear every other label and every
        node box."""
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
        assert _svg(html).count('data-node="column"') == 1 and 'data-from="c:SAMPLE"' not in _svg(html)

    def test_the_layout_is_computed_from_the_record_alone_and_is_the_same_every_time(self):
        rec = _record()
        assert picture_layout(rec) == picture_layout(rec)
        assert picture_layout(_record()) == picture_layout(_record())     # two derivations, no clock in the geometry
        assert picture_layout(_wide_record()) == picture_layout(_wide_record())

    def test_the_unsized_stage_is_drawn_dashed_and_the_sized_one_is_not(self):
        assert 'class="node stage unsized" data-node="stage" data-id="s:HISAT2"' in _svg(_page())
        sized = _record(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}})
        assert 'class="node stage" data-node="stage" data-id="s:HISAT2"' in _svg(_page(sized))

    def test_hovering_is_a_class_toggle_and_the_page_needs_nothing_from_the_network(self):
        html = _page(_cluster())
        assert "<script src=" not in html and "<link " not in html
        assert "@import" not in html and "url(http" not in html and "url(//" not in html
        assert "http://" not in html and "https://" not in html
        assert 'getElementById("pipeline-picture")' in html and 'getElementById("pipeline-files")' in html
        assert 'classList.add("hl")' in html
        assert "fetch(" not in html and "XMLHttpRequest" not in html


# ── parameters and samples ─────────────────────────────────────────────────────


class TestParametersAndSamples:
    def test_the_section_opens_with_two_bullets_what_params_yaml_is_then_what_samples_csv_is(self):
        sec = _section(_page(), "params")
        assert ('<h2>Parameters and samples <span class="note">what params.yaml and samples.csv hold, with the '
                "sealed run&#x27;s example values</span></h2>") in sec
        assert sec.count("<ul>") == 1 and sec.count("<li>") == 2
        one, two = _bullets(sec)
        assert one == ("<code>params.yaml</code> defines the parameters the pipeline runs with. It also names the "
                       "samplesheet to use (<code>samples.csv</code>) and the output directory (<code>results</code>). "
                       "Every value below is what the sealed run was validated with.")
        assert two == ("<code>samples.csv</code> holds one row per sample under a single header line naming the "
                       "columns (<code>sample</code>, <code>reads</code>); <code>sample</code> is the row key. "
                       "<b>The example rows are the sealed run's own trials — replace them with your samples.</b>")
        assert f"(<code>{SAMPLESHEET_PARAM[1]}</code>)" in one and f"(<code>{OUTDIR_PARAM[1]}</code>)" in one
        assert (sec.index("<ul>") < sec.index("</ul>") < sec.index("<th>params.yaml</th>")
                < sec.index("<tr><th>sample</th>"))
        assert sec.count('<p class="note">') == 0 and "<p>" not in sec     # the bullets are the section's only prose

    def test_the_shared_params_are_a_table_keyed_as_params_yaml_names_them_with_the_sealed_example_value(self):
        sec = _section(_page(), "params")
        assert "<tr><th>params.yaml</th><th>Example value</th><th>What it is</th></tr>" in sec
        assert f"<tr><td><code>params.hisat2_index</code></td><td><code>{INDEX}</code></td>" in sec
        assert "<tr><td><code>params.stranded</code></td><td><code>reverse</code></td>" in sec
        assert f"<tr><td><code>params.gtf</code></td><td><code>{GTF}</code></td>" in sec
        assert sec.count("<tr><td><code>params.") == 3 + 2             # the shared params, then the two standing rows
        assert sec.index("params.hisat2_index") < sec.index("params.stranded") < sec.index("params.gtf")
        for ph in ("HISAT2_INDEX", "STRANDED", "GTF", "READS", "SAMPLE"):
            assert f"<code>{ph}</code>" not in sec, ph
        assert "<td>shared</td>" not in sec and "<td>per sample" not in sec   # kind is the table you are in

    def test_two_standing_rows_the_samplesheet_and_the_output_directory_follow_the_shared_params(self):
        sec = _section(_page(), "params")
        rows = re.findall(r"<tr><td><code>(params\.\w+)</code></td><td>(.*?)</td><td>(.*?)</td></tr>", sec)
        assert [r[0] for r in rows] == ["params.hisat2_index", "params.stranded", "params.gtf",
                                        f"params.{SAMPLESHEET_PARAM[0]}", f"params.{OUTDIR_PARAM[0]}"]
        assert rows[-2:] == [("params.samplesheet", "<code>samples.csv</code>", "the samplesheet: one row per sample"),
                             ("params.outdir", "<code>results</code>",
                              "where published outputs land, one directory per sample")]
        assert rows[-2][1] == f"<code>{SAMPLESHEET_PARAM[1]}</code>" and rows[-1][1] == f"<code>{OUTDIR_PARAM[1]}</code>"

    def test_what_it_is_carries_the_value_kind_the_description_and_the_producing_sealed_step(self):
        sec = _section(_page(), "params")
        assert "a prefix — a family of files named after it · HISAT2 index prefix · produced by sealed step 1" in sec
        assert "a value · htseq-count -s: yes | no | reverse" in sec
        assert "a path · gene annotation" in sec
        assert "sealed_step:1" not in sec                    # the source is read out, not echoed
        assert "<td><code>none</code></td>" not in sec
        rec = _record()
        params = [p.model_copy(update={"default": None}) if p.name == "STRANDED" else p for p in rec.params]
        sec = _section(_page(rec.model_copy(update={"params": params})), "params")
        assert '<tr><td><code>params.stranded</code></td><td><span class="muted">none</span></td>' in sec

    def test_the_example_rows_follow_the_params_table_directly_one_row_per_trial(self):
        sec = _section(_page(), "params")
        assert ('</table></div><div class="tbl-wrap"><table><tr><th>sample</th><th>reads</th></tr>'
                f"<tr><td>{SAMPLES[0]}</td>") in sec                        # nothing between the two tables
        for s in SAMPLES:
            assert f"<tr><td>{s}</td><td>/data/reads/{s}_10K_R1.fastq.gz</td></tr>" in sec
        assert sec.count("<tr><td>SRR") == len(SAMPLES)
        assert sec.count("<table>") == 2
        assert sec.endswith("</tr></table></div></div></section>")

    def test_a_one_row_seal_renders_a_one_row_sheet_and_otherwise_the_same_section(self):
        three, one = _section(_page(), "params"), _section(_page(_one_row()), "params")
        assert one.count("<tr><td>SRR") == 1
        assert f"<tr><td>{SAMPLES[0]}</td><td>/data/reads/{SAMPLES[0]}_10K_R1.fastq.gz</td></tr>" in one
        rows = re.compile(r"<tr><td>SRR\w+</td><td>[^<]*</td></tr>")
        assert rows.sub("", one) == rows.sub("", three)
        assert "replace them with your samples" in one
        for gone in ("implicit row", "params only", "commands.sh", "linear"):
            assert gone not in one, gone

    def test_the_row_key_is_a_column_even_when_no_command_names_it(self):
        rec = pr.derive_pipeline_record(sealed_rnaseq_spec(templates=TEMPLATES[:2]), name="two")
        assert "SAMPLE" not in {p.name for p in rec.params}
        sec = _section(_page(rec), "params")
        assert sec.count("<tr><td><code>params.") == 1 + 2 and "<code>params.hisat2_index</code>" in sec
        assert ("naming the columns (<code>sample</code>, <code>reads</code>); <code>sample</code> is the row "
                "key.") in sec
        assert "<tr><th>sample</th><th>reads</th></tr>" in sec
        layout = picture_layout(rec)
        assert [n.id for n in layout.nodes if n.kind == "column"] == ["c:SAMPLE", "c:READS"]
        assert not [e for e in layout.edges if e.src == "c:SAMPLE"]

    def test_a_record_with_no_shared_params_keeps_the_two_standing_rows_and_the_samplesheet(self):
        sec = _section(_page(_no_shared()), "params")
        rows = re.findall(r"<tr><td><code>(params\.\w+)</code></td>", sec)
        assert rows == ["params.samplesheet", "params.outdir"]           # what params.yaml still holds
        assert sec.count("<table>") == 2
        assert "naming the columns (<code>sample</code>); <code>sample</code> is the row key." in sec
        assert "<tr><th>sample</th></tr><tr><td>S1</td></tr><tr><td>S2</td></tr>" in sec
        assert _fnodes(_no_shared())["f:params.yaml"].lines[0] == "no shared parameters"


# ── run it locally ─────────────────────────────────────────────────────────────


class TestRunLocally:
    def test_it_is_three_steps_change_directory_make_nextflow_available_run_with_the_one_run_line(self):
        html = _page()
        sec = _section(html, "run-local")
        assert ('<h2>Run it locally <span class="note">every row of samples.csv, with Nextflow through '
                "docker</span></h2>") in sec
        assert sec.count("<ol>") == 1 and sec.count("<li>") == 3
        cd, make, run = _steps(sec)
        assert cd == ("Copy this directory next to your data and change into it. Put your samples in "
                      "<code>samples.csv</code> and your paths in <code>params.yaml</code>. " + LAUNCH_NOTE
                      + "<pre>cd /path/to/rnaseq_counts</pre>")
        assert make.startswith("Make <code>nextflow</code> available")
        assert run == ("Run. Nextflow starts every stage inside the frozen image through docker; <code>-resume</code> "
                       f"re-runs only the stages whose inputs or parameters changed.<pre>{RUN_LOCAL}</pre>")
        assert RUN_LOCAL == "nextflow run main.nf -profile local -params-file params.yaml -resume"
        assert html.count("nextflow run main.nf") == 1                  # one spelling on the whole page

    def test_with_the_runtime_env_recorded_step_two_is_source_its_activate_script(self):
        html = _page(_local())
        sec = _section(html, "run-local")
        _, make, _ = _steps(sec)
        assert make == ("Make <code>nextflow</code> available. This checkout's runtime env carries it with its own "
                        "Java; sourcing the line below puts both on your PATH for this shell. Docker must be running "
                        f"with <code>bioinf_rnaseq_cli:latest</code> present.<pre>source {ACTIVATE}</pre>")
        assert 'class="warn"' not in sec and sec.count("<pre>") == 3
        assert html.count(f"source {ACTIVATE}") == 1 and NEXTFLOW not in html   # the binary's path is not a step
        assert "was not recorded" not in sec

    def test_when_nextflow_was_missing_from_the_runtime_env_the_step_warns_and_still_sources(self):
        sec = _section(_page(_local_no_nextflow()), "run-local")
        _, make, _ = _steps(sec)
        assert make.startswith("Make <code>nextflow</code> available. This checkout's runtime env carries it with its "
                               "own Java; sourcing the line below puts both on your PATH for this shell. Docker must "
                               "be running with <code>bioinf_rnaseq_cli:latest</code> present.")
        assert make.endswith(' <span class="warn">nextflow was not in the runtime env when this page was rendered: '
                             f'run <code>./scripts/setup.sh</code> first.</span><pre>source {ACTIVATE}</pre>')
        assert sec.count('class="warn"') == 1

    def test_with_no_runtime_env_recorded_the_step_says_so_and_types_nothing(self):
        sec = _section(_page(), "run-local")
        _, make, _ = _steps(sec)
        assert make == ("Make <code>nextflow</code> available on your PATH — this machine's runtime env was not "
                        "recorded when the page was rendered. Docker must be running with "
                        "<code>bioinf_rnaseq_cli:latest</code> present.")
        assert "<pre>" not in make and "source " not in sec and "activate" not in sec
        assert sec.count("<pre>") == 2 and 'class="warn"' not in sec

    def test_the_image_that_must_be_present_is_named_from_the_record_or_its_absence_stated_in_step_two(self):
        for rec in (_two_images(), _two_images(_local())):
            _, make, _ = _steps(_section(_page(rec), "run-local"))
            assert make.split("<pre>")[0].endswith("Docker must be running with every frozen image present "
                                                   "(<code>nextflow.config</code> names them).")
        rec = _record()
        untagged = rec.model_copy(update={"stages": [s.model_copy(update={"image": None}) for s in rec.stages]})
        sec = _section(_page(untagged), "run-local")
        cd, make, run = _steps(sec)
        assert make.endswith("Docker must be running with the frozen image present.")
        assert "bioinf_rnaseq_cli" not in sec
        assert "Docker" not in cd and "Docker" not in run                 # the requirement lives in step two only

    def test_the_first_step_carries_the_launch_directory_note(self):
        for rec in (_record(), _local(), _cluster(), _one_row()):
            cd = _steps(_section(_page(rec), "run-local"))[0]
            assert LAUNCH_NOTE + "<pre>cd /path/to/" in cd
            assert "<code>work/</code>" in cd and "<code>.nextflow/</code>" in cd and "<code>-resume</code>" in cd

    def test_the_note_says_where_results_land_and_where_the_run_records_what_ran(self):
        sec = _section(_page(), "run-local")
        assert sec.endswith(
            '<p class="note">Results land in <code>results/&lt;sample&gt;/</code>, one directory per sample. '
            "What ran: <code>runs/&lt;timestamp&gt;/trace.txt</code> lists every task's command, "
            "<code>runs/&lt;timestamp&gt;/report.html</code> the resources, and <code>nextflow log</code> "
            "the launch line.</p></div></section>")
        assert _section(_page(_one_row()), "run-local").count("Results land in") == 1

    def test_a_cohort_stage_publishes_flat_and_the_note_says_which(self):
        one = _section(_page(_cohort()), "run-local")
        assert ("Results land in <code>results/&lt;sample&gt;/</code>, one directory per sample (cohort stage "
                "STAGE_01 in <code>results/</code>).") in one
        two = _section(_page(_with_cohort(_wide_record(n_stages=3), "STAGE_01", "STAGE_02")), "run-local")
        assert "(cohort stages STAGE_01, STAGE_02 in <code>results/</code>)." in two

    def test_there_is_no_by_hand_item_and_no_docker_line_anywhere_on_the_page(self):
        for make in (_record, _one_row, _cluster, _two_images, _wide_record, _local, _local_no_nextflow):
            html = _page(make())
            for gone in ("docker run", "commands.sh", "One sample by hand", "B. Every sample", "B. The one row",
                         "enter the image", "Enter the image", "Nothing to enter", "apptainer shell",
                         "add a <code>-v</code>", "<h3"):
                assert gone not in html, (make.__name__, gone)
            assert not re.search(r"\s-v\s", html), make.__name__
            assert _section(html, "run-local").count("<ol>") == 1

    def test_the_local_section_never_mentions_the_cluster_and_the_cluster_section_never_docker(self):
        for rec in (_cluster(), _cluster(local_runtime=RUNTIME)):
            html = _page(rec)
            local, hpc = _section(html, "run-local"), _section(html, "run-hpc")
            assert "sbatch" not in local and "apptainer" not in local and "module load" not in local
            assert "docker" not in hpc and "source " not in hpc and ACTIVATE not in hpc


# ── run it on the cluster ──────────────────────────────────────────────────────


#: The cluster's first step, verbatim: change directory, and why that directory.
_HPC_CD = ("Copy this directory into your project directory on the cluster and change into it. "
           "<code>samples.csv</code> and <code>params.yaml</code> must name cluster paths. SLURM starts the "
           "manager job here and " + LAUNCH_NOTE[0].lower() + LAUNCH_NOTE[1:]
           + " The launcher keeps Nextflow's own files under <code>.nextflow_home</code> inside it.")


class TestRunOnTheCluster:
    def test_with_a_cluster_named_the_note_says_where_the_sif_is_and_the_launcher_loads_the_modules(self):
        html = _page(_cluster())
        sec = _section(html, "run-hpc")
        assert ('<h2>Run it on the cluster <span class="note">the same files, through the .sif and SLURM'
                "</span></h2>") in sec
        assert (f'<p class="note">The <code>slurm</code> profile in <code>nextflow.config</code> runs the '
                f"<code>.sif</code> at <code>{SIF}</code> — where <code>stage_apptainer_image</code> put it on "
                "<b>hpc</b>.</p>") in sec
        assert sec.count('class="note">The <code>slurm</code>') == 1 and 'class="warn-note"' not in sec
        assert sec.index("</p>") < sec.index("<ol>")                    # the note comes first
        assert ("Submit. <code>launcher.sh</code> loads <code>apptainer/1.3.2</code> <code>nextflow/24.10.0</code> "
                "and runs Nextflow as a small manager job; every stage of every sample is its own SLURM job. "
                f"Nothing to activate by hand.<pre>{RUN_HPC}</pre>") in sec
        assert "stage_apptainer_image(" not in sec                      # nothing to stage: the .sif is there
        assert "module load" not in sec                                 # the launcher does the loading
        assert _banner(html).count("module load") == 1 and _files_svg(html).count("module load") == 1
        assert html.count("module load") == 2

    def test_without_a_cluster_named_a_warning_says_the_container_is_empty_and_how_to_set_it(self):
        html = _page()
        sec = _section(html, "run-hpc")
        assert (
            '<p class="warn-note">The <code>slurm</code> profile\'s <code>container</code> in '
            "<code>nextflow.config</code> is empty: re-render with <code>env=</code> naming the cluster, or set it "
            "to the <code>.sif</code> that <code>stage_apptainer_image(project=&lt;your project&gt;, "
            f"env=&lt;the cluster&#x27;s env&gt;, freeze_request_key=&quot;{REQUEST_KEY}&quot;)</code> reports. "
            "The workflow refuses to start until it is set.</p>") in sec
        assert sec.index('class="warn-note"') < sec.index("<ol>")
        assert 'class="note">The <code>slurm</code>' not in sec
        assert SIF not in html and "module load" not in html
        assert ("Submit. <code>launcher.sh</code> runs Nextflow as a small manager job — make apptainer and "
                "nextflow available first, it was rendered without a cluster named and loads no modules; every "
                f"stage of every sample is its own SLURM job.<pre>{RUN_HPC}</pre>") in sec
        assert "Nothing to activate by hand." not in sec

    def test_an_env_named_without_a_sif_names_that_env_in_the_call_and_loads_no_modules(self):
        sec = _section(_page(_record(compute_env="hpc", modules=[])), "run-hpc")
        assert 'class="warn-note"' in sec
        assert f'env=&quot;hpc&quot;, freeze_request_key=&quot;{REQUEST_KEY}&quot;)</code> reports.' in sec
        assert ("Submit. <code>launcher.sh</code> runs Nextflow as a small manager job — make apptainer and "
                f"nextflow available first; every stage of every sample is its own SLURM job.<pre>{RUN_HPC}</pre>") in sec
        assert "rendered without a cluster named" not in sec and "module load" not in sec
        assert "Nothing to activate by hand." not in sec

    def test_a_record_without_a_request_key_states_the_slot_instead_of_inventing_one(self):
        sec = _section(_page(_wide_record(n_stages=2)), "run-hpc")
        assert "freeze_request_key=&lt;the env&#x27;s freeze_request_key&gt;)</code> reports." in sec

    def test_one_note_per_image_each_naming_its_image_when_there_are_several(self):
        sec = _section(_page(_staged_but_one()), "run-hpc")
        assert (f'<p class="note">The <code>slurm</code> profile in <code>nextflow.config</code> runs the '
                f"<code>.sif</code> for image <code>aaaa1111aaaa</code> at <code>{SIF}</code> — where "
                "<code>stage_apptainer_image</code> put it on <b>hpc</b>.</p>") in sec
        assert ('<p class="warn-note">The <code>slurm</code> profile\'s <code>container</code> in '
                "<code>nextflow.config</code> is empty for image <code>cccccccccccc</code>: re-render") in sec
        assert f'env=&quot;hpc&quot;, freeze_request_key=&quot;{REQUEST_KEY}&quot;)</code> reports.' in sec
        assert sec.count('class="note">The <code>slurm</code>') == 1 and sec.count('class="warn-note"') == 1
        assert sec.index("for image <code>aaaa1111aaaa</code>") < sec.index("for image <code>cccccccccccc</code>")
        assert "Submit. <code>launcher.sh</code> loads <code>apptainer/1.3.2</code> and runs Nextflow" in sec
        bare = _section(_page(_two_images()), "run-hpc")
        assert bare.count('class="warn-note"') == 2 and 'class="note">The <code>slurm</code>' not in bare

    def test_it_is_three_steps_copy_submit_watch_with_the_one_spelling_of_the_submit_line(self):
        for make in (_record, _cluster, _two_images, _local):
            html = _page(make())
            sec = _section(html, "run-hpc")
            assert sec.count("<ol>") == 1 and sec.count("<li>") == 3, make.__name__
            cd, submit, watch = _steps(sec)
            assert cd == _HPC_CD + "<pre>cd /path/in/your/project/rnaseq_counts</pre>", make.__name__
            assert submit.startswith("Submit. <code>launcher.sh</code> ")
            assert submit.endswith(f"<pre>{RUN_HPC}</pre>")
            assert RUN_HPC == "sbatch launcher.sh"
            assert watch == "Watch it; <code>sacct -j &lt;jobid&gt;</code> once it has ended.<pre>squeue -u $USER</pre>"
            assert html.count("sbatch launcher.sh") == 1 and "apptainer shell" not in html

    def test_the_first_step_says_why_the_launch_directory_matters_and_where_the_launcher_keeps_nextflows_files(self):
        cd = _steps(_section(_page(_cluster()), "run-hpc"))[0]
        assert ("SLURM starts the manager job here and nextflow works out of the directory it is started in: "
                "<code>work/</code> (each task's sandbox), <code>.nextflow/</code> (what <code>-resume</code> reads) "
                "and the run records appear here, so launch from this directory every time. The launcher keeps "
                "Nextflow's own files under <code>.nextflow_home</code> inside it.<pre>cd /path/in/your/project/"
                "rnaseq_counts</pre>") in cd
        assert "Nextflow works out of" not in cd                         # one sentence, joined mid-way

    def test_the_note_after_the_steps_is_the_results_and_what_ran_sentence_the_local_section_ends_with(self):
        html = _page(_cluster())
        hpc, local = _section(html, "run-hpc"), _section(html, "run-local")
        notes = re.findall(r'<p class="note">Results land in .*?</p>', hpc)
        assert len(notes) == 1 and notes == re.findall(r'<p class="note">Results land in .*?</p>', local)
        assert hpc.endswith(notes[0] + "</div></section>")


# ── stages ─────────────────────────────────────────────────────────────────────


class TestStages:
    def test_one_row_per_stage_in_execution_order_with_the_tool_and_the_command_main_nf_runs(self):
        rec, html = _record(), _page()
        sec = _section(html, "stages")
        assert ("<tr><th>Stage</th><th>Tool</th><th>Command, as main.nf runs it</th><th>Request</th>"
                "<th>Measured</th></tr>") in sec
        assert sec.count("<tr><td><code>") == len(rec.stages) == 3
        names = ("HISAT2", "SAMTOOLS", "HTSEQ_COUNT")
        assert [sec.index(f"<tr><td><code>{s}</code></td>") for s in names] == sorted(
            sec.index(f"<tr><td><code>{s}</code></td>") for s in names)
        for st in rec.stages:
            bound = "\n".join(bound_commands(rec, st))
            assert f"<tr><td><code>{st.name}</code></td><td>{st.tool}</td><td><pre>{_e(bound)}</pre></td>" in sec
        assert ("<pre>hisat2 -p 4 -x ${file(params.hisat2_index).name} -U ${reads} | samtools sort -o "
                "aligned.bam</pre>") in sec
        assert "<pre>samtools index aligned.bam</pre>" in sec
        assert "<pre>htseq-count -s ${params.stranded} -f bam aligned.bam ${gtf} &gt; ${meta.sample}.counts.tsv</pre>" in sec
        for t in TEMPLATES:                                   # never the seal's template, placeholders intact
            assert _e(t) not in sec
        assert "How-to command" not in sec and "<td>1</td>" not in sec

    def test_a_merged_stage_shows_its_commands_one_per_line_in_one_cell(self):
        merged = _record(stages=[[0, 1], [2]])
        first = merged.stages[0]
        assert first.name == "HISAT2" and len(first.commands) == 2
        row = _stage_row(_page(merged), "HISAT2")
        assert f"<pre>{_e(chr(10).join(bound_commands(merged, first)))}</pre>" in row
        assert ("<pre>hisat2 -p 4 -x ${file(params.hisat2_index).name} -U ${reads} | samtools sort -o aligned.bam\n"
                "samtools index aligned.bam</pre>") in row
        assert "<td>1, 2</td>" not in row

    def test_a_command_the_renderer_cannot_bind_is_a_warning_in_the_cell_not_a_paraphrase(self):
        rec = _with_stage(_record(), "SAMTOOLS", commands=["tool {UNDECLARED} > {OUTPUT_DIR}/x"])
        row = _stage_row(_page(rec), "SAMTOOLS")
        assert ('<td><span class="warn">cannot be bound: stage SAMTOOLS: placeholder {UNDECLARED} is not a '
                "parameter of the record; re-derive the record from the sealed workflow</span></td>") in row
        assert "<pre>" not in row

    def test_with_one_image_the_note_names_it_once_and_there_is_no_image_column(self):
        sec = _section(_page(), "stages")
        assert (f'<p class="note">Every stage runs inside <code>bioinf_rnaseq_cli:latest</code> '
                f'<span class="muted" title="{DIGEST}">aaaa1111aaaa</span>.</p><div class="tbl-wrap">') in sec
        assert "<th>Image</th>" not in sec and sec.count("bioinf_rnaseq_cli:latest") == 1

    def test_with_several_images_the_image_column_returns_between_command_and_request(self):
        html = _page(_two_images())
        sec = _section(html, "stages")
        assert ("<tr><th>Stage</th><th>Tool</th><th>Command, as main.nf runs it</th><th>Image</th><th>Request</th>"
                "<th>Measured</th></tr>") in sec
        assert "Every stage runs inside" not in sec
        assert (f'</pre></td><td><code>bioinf_rnaseq_cli:latest</code> <span class="muted" title="{DIGEST}">'
                'aaaa1111aaaa</span></td><td><span class="warn">unsized</span>') in _stage_row(html, "HISAT2")
        assert (f'</pre></td><td><code>other_img:2</code> <span class="muted" title="{DIGEST_2}">cccccccccccc'
                '</span></td><td><span class="warn">unsized</span>') in _stage_row(html, "HTSEQ_COUNT")

    def test_an_unrecorded_image_is_stated_not_blank(self):
        assert ('<p class="note">Every stage runs inside <span class="muted">unrecorded</span>.</p>'
                in _section(_page(_wide_record(n_stages=2)), "stages"))
        html = _page(_with_stage(_two_images(), "HTSEQ_COUNT", image=None, image_digest=None))
        assert '</pre></td><td><span class="muted">unrecorded</span></td>' in _stage_row(html, "HTSEQ_COUNT")
        html = _page(_with_stage(_two_images(), "HTSEQ_COUNT", image=None))
        assert (f'</pre></td><td><span class="muted">tag unrecorded</span> <span class="muted" title="{DIGEST_2}">'
                "cccccccccccc</span></td>") in _stage_row(html, "HTSEQ_COUNT")

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

    def test_there_are_no_per_stage_cards_or_sub_tables(self):
        html = _page()
        assert 'class="run-card"' not in html and 'id="stage-HISAT2"' not in html
        assert "<h3" not in _section(html, "stages")
        assert _section(html, "stages").count("<table>") == 1

    def test_a_record_with_no_stages_says_so(self):
        rec = _wide_record(n_stages=2).model_copy(update={"stages": []})
        assert '<p class="empty">the record holds no stages</p>' in _section(_page(rec), "stages")


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


_RECORDS = {"three_rows": _record, "one_row": _one_row, "wide": _wide_record, "cluster": _cluster,
            "two_images": _two_images, "local": _local, "local_no_nextflow": _local_no_nextflow,
            "cohort": _cohort}


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
        for tag in ("table", "svg", "section", "div", "g", "pre", "tr", "td", "ol", "ul", "li", "script"):
            assert html.count(f"<{tag}") == html.count(f"</{tag}>"), tag

    @pytest.mark.parametrize("which", sorted(_RECORDS))
    def test_the_sections_are_exactly_these_in_this_order_then_the_footer(self, which):
        html = _page(_RECORDS[which]())
        assert re.findall(r'<section class="bx" id="([^"]+)"', html) == SECTION_IDS
        assert html.index('id="stages"') < html.index(f'<p class="gen">{FOOTER}')
        assert html.count("<svg") == 2 and html.index('id="pipeline-files"') < html.index('id="pipeline-picture"')

    @pytest.mark.parametrize("which", sorted(_RECORDS))
    def test_the_page_speaks_the_files_vocabulary_and_never_the_seals_placeholders(self, which):
        """Nothing a reader sees or hovers is a `{PLACEHOLDER}`; in the raw bytes one
        survives only inside the picture's machine keys (data-id and friends), which
        address a node for the hover JS and are never rendered."""
        html = _page(_RECORDS[which]())
        spoken = _spoken(html)
        assert not _PLACEHOLDER_RE.search(spoken), _PLACEHOLDER_RE.findall(spoken)
        for ph in PLACEHOLDERS:
            assert ph not in spoken, ph
        assert not _PLACEHOLDER_RE.search(_MACHINE_KEY_RE.sub("", html)), _PLACEHOLDER_RE.findall(
            _MACHINE_KEY_RE.sub("", html))

    def test_the_fixture_page_says_params_gtf_the_reads_column_and_sample_counts_tsv(self):
        spoken = _spoken(_page())
        for word in ("params.gtf", "params.stranded", "params.hisat2_index", "reads", "<sample>.counts.tsv",
                     "results/<sample>/", "${params.stranded}", "${gtf}", "${reads}", "${meta.sample}.counts.tsv",
                     "params.samplesheet", "samplesheet: samples.csv", "outdir: results"):
            assert word in spoken, word
        for ph in ("HISAT2_INDEX", "STRANDED", "GTF", "READS", "OUTPUT_DIR"):
            assert ph not in spoken, ph
        assert "SAMPLE" not in spoken.replace("SAMPLES", "")

    def test_the_removed_items_are_gone(self):
        html = _page(_cluster())
        for sid in ("samplesheet", "stages-cards", "defaults", "provenance", "howto", "notes", "by-hand", "commands"):
            assert f'id="{sid}"' not in html
        for gone in ("Bytes", "MANIFEST", "sha256sum -c", "run_all.sh", "run_local.sh", "nextflow_local.sh",
                     "--stages", "every derivation the caller did not dictate", "Inputs</h3>", "Outputs</h3>",
                     "A. One sample by hand", "B. Every sample", "B. The one row", "commands.sh", "docker run",
                     "apptainer shell", "Enter the image", "Nothing to enter", "Output slots", "How-to command",
                     "<td>shared</td>", "params only", "implicit row", "{OUTPUT_DIR}",
                     '<p class="note"><code>params.yaml</code> also names', "one row per sample, columns:",
                     "must be on your PATH", "carries no shared parameters"):
            assert gone not in html, gone

    def test_the_same_record_renders_the_same_bytes(self):
        for make in _RECORDS.values():
            rec = make()
            assert render_pipeline_page(rec) == render_pipeline_page(rec)

    def test_the_page_renders_from_a_record_that_round_tripped_through_yaml(self, tmp_path):
        for rec in (_cluster(), _local_no_nextflow()):
            back = pr.load_pipeline_record(pr.write_pipeline_record(rec, tmp_path / rec.name))
            assert render_pipeline_page(back) == render_pipeline_page(rec)

    def test_a_hostile_value_is_escaped_everywhere_including_inside_both_svgs_and_the_command_cell(self):
        rec = _record()
        params = [p.model_copy(update={"description": "<script>alert(1)</script>"}) if p.name == "GTF" else p
                  for p in rec.params]
        rows = [{**rec.samplesheet.rows[0], "sample": "<script>x</script>"}] + rec.samplesheet.rows[1:]
        sheet = rec.samplesheet.model_copy(update={"rows": rows})
        stages = []
        for s in rec.stages:
            if s.name == "SAMTOOLS":
                s = s.model_copy(update={"tool": 'x"><script>y</script>'})
            if s.name == "HISAT2":
                s = s.model_copy(update={"sif_path": "/sif/<script>s.sif", "commands": [
                    "hisat2 -x {HISAT2_INDEX} -U {READS} <script>q</script> > {OUTPUT_DIR}/aligned.bam"]})
            stages.append(s)
        html = _page(rec.model_copy(update={
            "name": "<script>z</script>", "params": params, "samplesheet": sheet, "stages": stages,
            "compute_env": "<script>e</script>", "modules": ["<script>m</script>"],
            "local_runtime": pr.LocalRuntime(activate="/ck/<script>a</script>/activate.sh", nextflow=None),
            "defaults": [pr.PipelineDefault(key="<script>k</script>", value="<script>d</script> `x<y`", source="seal")]}))
        for raw in ("<script>alert(1)</script>", "<script>x</script>", "<script>y</script>", "<script>z</script>",
                    "<script>q</script>", "<script>s.sif", "<script>e</script>", "<script>m</script>",
                    "<script>a</script>", "<script>k</script>", "<script>d</script>", "x<y"):
            assert raw not in html, raw
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in _section(html, "params")
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in _svg(html)                    # the param's tooltip
        assert _section(html, "params").count("&lt;script&gt;x&lt;/script&gt;") == 1  # the example row
        assert "&lt;script&gt;y" in _svg(html) and "&lt;script&gt;y" in _section(html, "stages")
        assert "<pre>cd /path/to/&lt;script&gt;z&lt;/script&gt;</pre>" in html
        assert "<pre>cd /path/in/your/project/&lt;script&gt;z&lt;/script&gt;</pre>" in html
        assert "&lt;script&gt;q&lt;/script&gt; &gt; aligned.bam</pre>" in _section(html, "stages")
        hpc = _section(html, "run-hpc")
        assert "<code>/sif/&lt;script&gt;s.sif</code>" in hpc
        assert "put it on <b>&lt;script&gt;e&lt;/script&gt;</b>" in hpc
        assert "loads <code>&lt;script&gt;m&lt;/script&gt;</code> and runs" in hpc
        assert "<b>&lt;script&gt;e&lt;/script&gt;</b> — module load <code>&lt;script&gt;m&lt;/script&gt;</code>" in _banner(html)
        local = _section(html, "run-local")
        assert "<pre>source /ck/&lt;script&gt;a&lt;/script&gt;/activate.sh</pre>" in local
        files = _section(html, "files")
        assert ("<tr><td>&lt;script&gt;k&lt;/script&gt;</td><td>&lt;script&gt;d&lt;/script&gt; <code>x&lt;y</code></td>"
                "<td>from the seal</td></tr>") in files
        fsvg = _files_svg(html)
        assert 'aria-label="pipeline &lt;script&gt;z&lt;/script&gt;: the files you copy' in fsvg
        assert ">↳ &lt;script&gt;s.sif on &lt;script&gt;e&lt;/script&gt;</text>" in fsvg
        assert ".sif: /sif/&lt;script&gt;s.sif</title>" in fsvg
        assert ">module load &lt;script&gt;m&lt;/script&gt;</text>" in fsvg
        assert html.count("<script>") == _page().count("<script>")          # the shell's and the hover JS only

    def test_a_record_with_no_outputs_still_renders(self):
        html = _page(_no_outputs())
        assert _svg(html).count("(nothing published)") == 1                 # both pictures say so, once each
        assert _files_svg(html).count(">(nothing published)</text>") == 1
        assert re.findall(r'<section class="bx" id="([^"]+)"', html) == SECTION_IDS
