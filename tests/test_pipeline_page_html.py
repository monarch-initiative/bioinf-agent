"""The explain page: the header banner, the picture, the directory, an example
samples.csv, how to run it locally, how to run it on the cluster, the stages, the
footer — and nothing else. A pipeline directory offers ONE way to run —
every row of samples.csv, with Nextflow — so the page shows three steps at home
(change directory, make nextflow available, run) and three on the cluster (change
directory, submit, watch), and never a by-hand item. Every run line it prints is the
one `pipeline_render_nextflow` spells (`RUN_LOCAL` / `RUN_HPC`), every stage command
is the line main.nf runs (`bound_commands`), and the two standing params are the pair
the renderer writes (`SAMPLESHEET_PARAM` / `OUTDIR_PARAM`), so the page and the files
cannot disagree. The page speaks the FILES' vocabulary — `params.gtf`, the `reads`
column, `<sample>.counts.tsv`, `results/<sample>/` — never the seal's `{PLACEHOLDER}`s.
The picture is a faithful, non-overlapping drawing of the record's graph, in which the
row key is drawn but never wired; the directory section draws nothing — it is a
two-column box, the files you put there and what a run adds, every line read off the
record; what params.yaml holds is read in params.yaml, so the page names its keys and
repeats nothing. A how-to whose thread count is a declared slot (`format: threads`)
renders the stage's command with `${task.cpus}` and its request as the sealed count.

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
from agent.skills.pipeline_page_html import (FOOTER, DirEntry, _ticks_to_code, directory_tree, picture_layout,
                                             render_pipeline_page)
from agent.skills.pipeline_render_nextflow import RUN_HPC, RUN_LOCAL, RUN_RECORD_FILES, bound_commands

SPEC_PATH = "/ws/reports/rnaseq_counts_workflow.workflow.yaml"
SIF = "/cluster/containers/rnaseq_cli_48ac8c5b25d2.sif"
MODULES = ["apptainer/1.3.2", "nextflow/24.10.0"]
SECTION_IDS = ["picture", "directory", "samplesheet", "run-local", "run-hpc", "stages"]
DIGEST_2 = "sha256:" + "c" * 64
#: How this machine provides nextflow, as the render tool records it: the checkout's
#: activate script, and the binary it puts on PATH — or None when it was not there.
ACTIVATE = "/ck/scripts/activate.sh"
NEXTFLOW = "/ck/.conda_runtime/bin/nextflow"
RUNTIME = pr.LocalRuntime(activate=ACTIVATE, nextflow=NEXTFLOW)
RUNTIME_NO_NEXTFLOW = pr.LocalRuntime(activate=ACTIVATE, nextflow=None)
#: The local first step, verbatim: change directory — the directory section said what goes there.
_LOCAL_CD = ("Change into the run directory: the files above, with your <code>samples.csv</code> beside them and "
             "<code>params.yaml</code> edited where your data differs.")
#: The seal's placeholders for the fixture and its thread-slot variant — words the page must never speak.
PLACEHOLDERS = ["{SAMPLE}", "{READS}", "{GTF}", "{STRANDED}", "{HISAT2_INDEX}", "{OUTPUT_DIR}", "{THREADS}"]
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


def _threads(**kw) -> pr.PipelineRecord:
    """The record of the how-to with its aligner's thread count declared as a slot
    (`format: threads`), bound 4 in every trial."""
    return pr.derive_pipeline_record(sealed_rnaseq_spec(threads=True), name="rnaseq_counts", spec_path=SPEC_PATH, **kw)


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
    """The page's one picture."""
    start = html.index(f'<svg id="{sid}"')
    return html[start: html.index("</svg>", start) + len("</svg>")]


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


def _entries(rec: pr.PipelineRecord) -> dict[str, DirEntry]:
    """The directory box's entries, by name."""
    return {e.name: e for e in directory_tree(rec)}


def _box(sec: str) -> str:
    """The directory box, out of the directory section."""
    start = sec.index('<div class="dir">')
    return sec[start: sec.index('<p class="note"><code>pipeline.html</code>', start)]


def _box_items(box: str) -> dict[str, list[tuple[str, str, str]]]:
    """Per column title, the entries as (name, role, what), cells verbatim."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for title, body in re.findall(r'<div class="col"><div class="col-title">(.*?)</div><ul>(.*?)</ul></div>', box, re.S):
        out[title] = re.findall(r'<li><code>(.*?)</code>(?: <span class="muted">· (.*?)</span>)?<div class="what">(.*?)</div></li>',
                                body, re.S)
    return out


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
                                        threads_slot=None, measured_wall_seconds=None, measured_peak_rss_mb=None,
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


# ── the directory ────────────────────────────────────────────────────────────


_DIR_RECORDS = {"three_rows": _record, "one_row": _one_row, "cluster": _cluster, "two_images": _two_images,
                "staged_but_one": _staged_but_one, "cohort": _cohort, "no_shared": _no_shared, "wide": _wide_record,
                "no_outputs": _no_outputs, "threads": _threads}
_INTRO = ('<p class="note">Copy these files into the directory where you want to run Nextflow, and write your '
          '<code>samples.csv</code> there. Launch from inside it: Nextflow works out of the directory it is started '
          'in.</p>')
_STAYS = ('<p class="note"><code>pipeline.html</code> (this page) and <code>.pipeline/</code> (the record it was '
          'rendered from) stay with the template.</p>')
_RUNS_ENTRY = ("one directory per run, never overwritten: `run.json`, `samples.csv`, `trace.txt`, `report.html`, "
               "`timeline.html` — `run.json` (the launch line, every param as resolved, the pipeline's provenance) "
               "and the samplesheet as read are written by main.nf at launch; the trace (every task: status, when, "
               "how long, resources, work dir, command), report and timeline are set in nextflow.config")


class TestTheDirectory:
    def test_the_section_is_the_copy_note_the_box_then_what_stays_with_the_template(self):
        sec = _section(_page(_cluster()), "directory")
        assert '<h2>The directory <span class="note">what you put there, what a run adds</span></h2>' in sec
        body = sec[sec.index('<div class="bx-body">') + len('<div class="bx-body">'):]
        assert body.startswith("<style>#directory .dir{")
        after_style = body[body.index("</style>") + len("</style>"):]
        assert after_style.startswith(_INTRO + '<div class="dir"><div class="path">path/to/your/project/</div>')
        assert body.endswith(_STAYS + "</div></section>")
        assert sec.count('<div class="col">') == 2 and sec.count("<style>") == 1
        for gone in ("<table", "<pre>", "<svg", "<script", "<h3", "<dl"):
            assert gone not in sec, gone

    def test_the_box_lists_the_five_files_you_put_there_then_what_a_run_adds(self):
        items = _box_items(_box(_section(_page(_cluster()), "directory")))
        assert list(items) == ["you put here", "a run adds"]
        assert [(n, role) for n, role, _ in items["you put here"]] == [
            ("main.nf", "copy"), ("nextflow.config", "copy"), ("params.yaml", "copy, then edit"),
            ("samples.csv", "write your own"), ("launcher.sh", "copy, cluster only")]
        assert [(n, role) for n, role, _ in items["a run adds"]] == [
            ("results/&lt;sample&gt;/", ""), ("runs/&lt;timestamp&gt;/", ""), ("work/", "")]
        entries = directory_tree(_cluster())
        assert [e.group for e in entries] == ["files"] * 5 + ["written"] * 3
        assert all(e.role == "" for e in entries if e.group == "written")

    def test_each_entry_is_the_records_own_words_with_command_spans_as_code(self):
        for make in (_cluster, _record, _cohort, _threads):
            rec = make()
            items = _box_items(_box(_section(_page(rec), "directory")))
            shown = {n: what for col in items.values() for n, _, what in col}
            for e in directory_tree(rec):
                assert shown[_e(e.name)] == _ticks_to_code(e.what), (make.__name__, e.name)
            assert len(shown) == len(directory_tree(rec))

    def test_main_nf_names_the_stages_in_order_and_how_often_each_runs(self):
        assert _entries(_record())["main.nf"].what == (
            "the stages HISAT2 → SAMTOOLS → HTSEQ_COUNT, one process each, run once per samples.csv row")
        assert _entries(_cohort())["main.nf"].what == (
            "the stages STAGE_00 → STAGE_01 → STAGE_02, one process each; a per-sample stage runs once per "
            "samples.csv row, a cohort stage once")
        empty = _wide_record(n_stages=2).model_copy(update={"stages": []})
        assert _entries(empty)["main.nf"].what == "the stages (no stages), one process each, run once per samples.csv row"

    def test_nextflow_config_names_what_each_profile_runs_in_and_says_set_me_when_no_sif_is_recorded(self):
        assert _entries(_record())["nextflow.config"].what == (
            "where it runs — `-profile local`: docker, `bioinf_rnaseq_cli:latest` · `-profile slurm`: apptainer, "
            "`container = ''` (SET ME) — each stage's request · what a run records")
        assert _entries(_cluster())["nextflow.config"].what == (
            "where it runs — `-profile local`: docker, `bioinf_rnaseq_cli:latest` · `-profile slurm`: apptainer, "
            "`rnaseq_cli_48ac8c5b25d2.sif` on hpc — each stage's request · what a run records")
        rec = _record()
        untagged = rec.model_copy(update={"stages": [s.model_copy(update={"image": None}) for s in rec.stages]})
        assert "docker, `aaaa1111aaaa` ·" in _entries(untagged)["nextflow.config"].what
        assert "docker, image unrecorded ·" in _entries(_wide_record(n_stages=2))["nextflow.config"].what

    def test_with_several_images_nextflow_config_names_one_image_per_stage_under_each_profile(self):
        what = _entries(_staged_but_one())["nextflow.config"].what
        assert ("docker, HISAT2 `bioinf_rnaseq_cli:latest`, SAMTOOLS `bioinf_rnaseq_cli:latest`, HTSEQ_COUNT "
                "`other_img:2` · `-profile slurm`: apptainer, HISAT2 `rnaseq_cli_48ac8c5b25d2.sif` on hpc, SAMTOOLS "
                "`rnaseq_cli_48ac8c5b25d2.sif` on hpc, HTSEQ_COUNT `container = ''` (SET ME) — each stage's request") in what

    def test_a_thread_slot_is_named_on_nextflow_config_as_threads_equal_cpus(self):
        assert _entries(_threads())["nextflow.config"].what.endswith(
            "— each stage's request · threads = cpus (HISAT2 4) · what a run records")
        assert "threads = cpus" not in _entries(_record())["nextflow.config"].what
        assert "threads = cpus (HISAT2 8)" in _entries(_threads(resources={"HISAT2": {"cpus": 8}}))["nextflow.config"].what

    def test_params_yaml_names_its_keys_then_the_samplesheet_and_the_output_directory(self):
        assert _entries(_record())["params.yaml"].what == (
            "hisat2_index, stranded, gtf · `samplesheet: samples.csv` · `outdir: results`")
        assert _entries(_no_shared())["params.yaml"].what == (
            "no shared parameters · `samplesheet: samples.csv` · `outdir: results`")
        assert _entries(_threads())["params.yaml"].what == _entries(_record())["params.yaml"].what   # a slot is no key

    def test_samples_csv_says_its_columns_and_its_row_key_and_points_at_the_example(self):
        assert _entries(_record())["samples.csv"].what == (
            "one row per sample, columns `sample`, `reads`; `sample` is the row key — example below")
        assert _entries(_one_row())["samples.csv"].what == _entries(_record())["samples.csv"].what
        assert _entries(_no_shared())["samples.csv"].what == (
            "one row per sample, columns `sample`; `sample` is the row key — example below")

    def test_launcher_sh_says_what_it_loads_in_each_cluster_state(self):
        tail = " the launch line with `-profile slurm`; flags after its name go through to Nextflow"
        assert _entries(_cluster())["launcher.sh"].what == (
            "`sbatch launcher.sh`: the manager job — `module load apptainer/1.3.2 nextflow/24.10.0`, then" + tail)
        assert _entries(_record(compute_env="hpc", modules=[]))["launcher.sh"].what == (
            "`sbatch launcher.sh`: the manager job — loads no modules (the env declares none), then" + tail)
        assert _entries(_record())["launcher.sh"].what == (
            "`sbatch launcher.sh`: the manager job — rendered without a cluster named, so it loads no modules; then"
            + tail)

    def test_what_a_run_adds_is_results_per_sample_the_run_records_and_work(self):
        e = _entries(_record())
        assert e["results/<sample>/"].what == (
            "aligned.bam, aligned.bam.bai, <sample>.counts.tsv — copied there by Nextflow as each stage finishes "
            "(`publishDir` in main.nf, under `outdir` from params.yaml); always the latest run's")
        assert e["runs/<timestamp>/"].what == _RUNS_ENTRY
        assert RUN_RECORD_FILES == ("run.json", "samples.csv", "trace.txt", "report.html", "timeline.html")
        assert e["work/"].what == (
            "each task's sandbox and what `-resume` reads, with `.nextflow/`; never cleaned for you — "
            "`nextflow clean -f` once the results are where you want them")
        assert "results/" not in e                                     # no cohort stage: no flat results entry
        assert _entries(_no_outputs())["results/<sample>/"].what.startswith("(nothing published) — copied there")

    def test_a_cohort_stage_adds_a_flat_results_entry_and_only_then(self):
        e = _entries(_cohort())
        assert [x.name for x in directory_tree(_cohort()) if x.group == "written"] == [
            "results/<sample>/", "results/", "runs/<timestamp>/", "work/"]
        assert e["results/"].what == "artifact_01.bam — the cohort stages' outputs, published the same way"
        assert e["results/<sample>/"].what.startswith("artifact_00.bam, artifact_02.bam — copied there")

    def test_the_section_speaks_the_files_words_and_no_placeholder(self):
        for make in _DIR_RECORDS.values():
            spoken = _spoken(_section(_page(make()), "directory"))
            assert not _PLACEHOLDER_RE.search(spoken), (make.__name__, _PLACEHOLDER_RE.findall(spoken))
        spoken = _spoken(_section(_page(_cluster()), "directory"))
        for word in ("params.yaml", "samples.csv", "-profile local", "-profile slurm", "sbatch launcher.sh",
                     "publishDir", "-resume", "nextflow clean -f", "run.json", "trace.txt", "timeline.html",
                     "module load apptainer/1.3.2 nextflow/24.10.0", "results/<sample>/", "runs/<timestamp>/"):
            assert word in spoken, word
        for gone in ("How it is used", "What it holds", "Our conventions", "Decided by", "nextflow run main.nf",
                     "MANIFEST", "copy this directory", "Copy this directory"):
            assert gone not in spoken, gone

    @pytest.mark.parametrize("which", sorted(_DIR_RECORDS))
    def test_the_box_is_computed_from_the_record_alone_and_is_the_same_every_time(self, which):
        rec = _DIR_RECORDS[which]()
        assert directory_tree(rec) == directory_tree(rec)
        assert all(isinstance(e, DirEntry) for e in directory_tree(rec))
        assert _section(_page(rec), "directory") == _section(_page(rec), "directory")

    def test_a_one_row_seal_lists_the_same_directory_as_the_three_row_one(self):
        assert directory_tree(_one_row()) == directory_tree(_record())

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
        assert html.count('getElementById("pipeline-') == 1 and 'getElementById("pipeline-picture")' in html
        assert 'classList.add("hl")' in html
        assert "fetch(" not in html and "XMLHttpRequest" not in html


    def test_a_thread_slot_is_not_drawn_as_a_params_yaml_value_nor_wired(self):
        svg = _svg(_page(_threads()))
        assert "THREADS" not in svg
        assert re.findall(r'data-node="param" data-id="([^"]+)"', svg) == ["p:HISAT2_INDEX", "p:STRANDED", "p:GTF"]
        assert "request cpus 4, the sealed thread count · mem and time default · gpus 0" in svg
        assert 'class="node stage unsized"' not in _svg(_page(_threads())).split('data-id="s:HISAT2"')[0].rsplit("<g", 1)[1]


# ── the example samplesheet ──────────────────────────────────────────────────


class TestExampleSamplesheet:
    def test_the_section_is_one_pre_with_the_header_and_the_first_example_row_then_the_columns_in_words(self):
        sec = _section(_page(), "samplesheet")
        assert ('<h2>Example samples.csv <span class="note">one sample: the header and the sealed run&#x27;s first '
                "row</span></h2>") in sec
        assert sec.count("<pre>") == 1
        assert "<pre>sample,reads\nSRR1039508,/data/reads/SRR1039508_10K_R1.fastq.gz</pre>" in sec
        assert sec.endswith(
            '<p class="note"><code>sample</code> — the row key: it tags every task and names results/&lt;sample&gt;/; '
            "<code>reads</code> — single-end reads · fastq. One row per sample, paths absolute.</p></div></section>")
        assert "SRR1039509" not in sec and "SRR1039512" not in sec                 # one sample, not the sheet
        assert "<table" not in sec and "<ul>" not in sec

    def test_the_example_is_the_samplesheets_own_first_row_whatever_the_record(self):
        for make in (_record, _one_row, _cluster, _cohort, _no_shared, _threads):
            rec = make()
            cols = [c.name for c in rec.samplesheet.columns]
            first = ",".join(str(rec.samplesheet.rows[0].get(c, "")) for c in cols)
            assert f"<pre>{_e(','.join(cols))}\n{_e(first)}</pre>" in _section(_page(rec), "samplesheet"), make.__name__
        assert "<pre>sample\nS1</pre>" in _section(_page(_no_shared()), "samplesheet")

    def test_a_column_without_a_description_or_format_is_still_named_by_its_kind(self):
        rec = _record()
        cols = [c.model_copy(update={"description": None, "format": None}) if c.name == "reads" else c
                for c in rec.samplesheet.columns]
        bare = rec.model_copy(update={"samplesheet": rec.samplesheet.model_copy(update={"columns": cols})})
        assert "<code>reads</code> — a path. One row per sample, paths absolute." in _section(_page(bare), "samplesheet")

    def test_a_record_with_no_example_rows_shows_the_header_alone(self):
        rec = _record()
        empty = rec.model_copy(update={"samplesheet": rec.samplesheet.model_copy(update={"rows": []})})
        assert "<pre>sample,reads</pre>" in _section(_page(empty), "samplesheet")

# ── run it locally ─────────────────────────────────────────────────────────────


class TestRunLocally:
    def test_it_is_three_steps_change_directory_make_nextflow_available_run_with_the_one_run_line(self):
        html = _page()
        sec = _section(html, "run-local")
        assert ('<h2>Run it locally <span class="note">every row of samples.csv, with Nextflow through '
                "docker</span></h2>") in sec
        assert sec.count("<ol>") == 1 and sec.count("<li>") == 3
        cd, make, run = _steps(sec)
        assert cd == _LOCAL_CD + "<pre>cd /path/to/rnaseq_counts</pre>"
        assert "launcher.sh" not in cd                                  # the launcher is the cluster's
        assert make.startswith("Make <code>nextflow</code> available")
        assert run == ("Run. Nextflow starts every stage inside the frozen image through docker; <code>-resume</code> "
                       "re-runs only the stages whose inputs or parameters changed. A value for this run only goes "
                       "after the line — <code>--stranded &lt;value&gt;</code> — and wins over "
                       f"<code>params.yaml</code>.<pre>{RUN_LOCAL}</pre>")
        assert RUN_LOCAL == "nextflow run main.nf -profile local -params-file params.yaml -resume"
        # one spelling on the whole page, in this step alone
        assert set(re.findall(r"nextflow run main\.nf[^<]*", html)) == {RUN_LOCAL}
        assert html.count(f"<pre>{RUN_LOCAL}</pre>") == 1
        assert sec.endswith("</ol></div></section>")                     # the steps are the whole section

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

    def test_the_first_step_points_at_the_files_above_whatever_the_record(self):
        for rec in (_record(), _local(), _cluster(), _one_row(), _cohort(), _threads()):
            sec = _section(_page(rec), "run-local")
            assert _steps(sec)[0] == _LOCAL_CD + f"<pre>cd /path/to/{rec.name}</pre>"
            assert sec.endswith("</ol></div></section>")
            assert "Results land in" not in sec and "What ran" not in sec   # the directory section says both

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


#: The cluster's first step, verbatim: change directory, every path a cluster path, and what lives there.
_HPC_CD = ("Change into the run directory on the cluster: the files above, with your <code>samples.csv</code> beside "
           "them, every path in it and in <code>params.yaml</code> a cluster path. SLURM starts the manager job here; "
           "the launcher keeps Nextflow's own files under <code>.nextflow_home</code> inside it.")
#: The sentence every submit step ends with: extra flags reach Nextflow, and win.
_PASSTHROUGH = (" Flags after <code>launcher.sh</code> go through to Nextflow: <code>--stranded &lt;value&gt;</code> "
                "overrides <code>params.yaml</code> for this run.")


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
                "Nothing to activate by hand." + _PASSTHROUGH + f"<pre>{RUN_HPC}</pre>") in sec
        assert "stage_apptainer_image(" not in sec                      # nothing to stage: the .sif is there
        assert "module load" not in sec                                 # the launcher does the loading
        assert _banner(html).count("module load") == 1 and _section(html, "directory").count("module load") == 1
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
                "stage of every sample is its own SLURM job." + _PASSTHROUGH + f"<pre>{RUN_HPC}</pre>") in sec
        assert "Nothing to activate by hand." not in sec

    def test_an_env_named_without_a_sif_names_that_env_in_the_call_and_loads_no_modules(self):
        sec = _section(_page(_record(compute_env="hpc", modules=[])), "run-hpc")
        assert 'class="warn-note"' in sec
        assert f'env=&quot;hpc&quot;, freeze_request_key=&quot;{REQUEST_KEY}&quot;)</code> reports.' in sec
        assert ("Submit. <code>launcher.sh</code> runs Nextflow as a small manager job — make apptainer and "
                "nextflow available first; every stage of every sample is its own SLURM job." + _PASSTHROUGH
                + f"<pre>{RUN_HPC}</pre>") in sec
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
            assert submit.endswith(_PASSTHROUGH + f"<pre>{RUN_HPC}</pre>")
            assert RUN_HPC == "sbatch launcher.sh"
            assert watch == "Watch it; <code>sacct -j &lt;jobid&gt;</code> once it has ended.<pre>squeue -u $USER</pre>"
            # one spelling: the directory box's launcher entry and this step
            assert html.count(f"<pre>{RUN_HPC}</pre>") == 1 and html.count(RUN_HPC) == 2
            assert "apptainer shell" not in html

    def test_the_first_step_says_every_path_is_a_cluster_path_and_where_the_launcher_keeps_nextflows_files(self):
        cd = _steps(_section(_page(_cluster()), "run-hpc"))[0]
        assert cd.startswith("Change into the run directory on the cluster: the files above")
        assert ("every path in it and in <code>params.yaml</code> a cluster path. SLURM starts the manager job here; "
                "the launcher keeps Nextflow's own files under <code>.nextflow_home</code> inside it."
                "<pre>cd /path/in/your/project/rnaseq_counts</pre>") in cd

    def test_the_steps_are_the_whole_section_and_neither_run_section_repeats_the_directory(self):
        html = _page(_cluster())
        for sid in ("run-hpc", "run-local"):
            sec = _section(html, sid)
            assert sec.endswith("</ol></div></section>")
            assert "Results land in" not in sec and "What ran" not in sec and "runs/&lt;timestamp&gt;" not in sec


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

    def test_a_thread_slot_makes_the_request_the_sealed_count_and_says_the_rest_is_default(self):
        row = _stage_row(_page(_threads()), "HISAT2")
        assert ("<td>cpus 4 <span class=\"muted\">— the command&#x27;s thread count, as the sealed run used it; mem and "
                "time DEFAULT 8G · 4:00:00</span></td>") in row
        assert "unsized" not in row
        assert ("<pre>hisat2 -p ${task.cpus} -x ${file(params.hisat2_index).name} -U ${reads} | samtools sort -o "
                "aligned.bam</pre>") in row
        assert "unsized" in _stage_row(_page(_threads()), "SAMTOOLS")
        sized = _page(_threads(resources={"HISAT2": {"cpus": 8, "mem": "32G", "time": "4:00:00"}}))
        assert "<td>cpus 8 (the command&#x27;s thread count) · mem 32G · time 4:00:00 · gpus 0</td>" in _stage_row(sized, "HISAT2")
        gpu = _page(_with_resources(_threads(), "HISAT2", gpus=1))
        assert "mem and time DEFAULT 8G · 4:00:00 · 1 gpu</span></td>" in _stage_row(gpu, "HISAT2")

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
            "cohort": _cohort, "threads": _threads}


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
        assert html.count("<svg") == 1 and html.index('id="picture"') < html.index('id="directory"')
        assert html.index('id="directory"') < html.index('id="samplesheet"') < html.index('id="run-local"')

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
                     "samplesheet: samples.csv", "outdir: results", "runs/<timestamp>/", "run.json"):
            assert word in spoken, word
        for ph in ("HISAT2_INDEX", "STRANDED", "GTF", "READS", "OUTPUT_DIR"):
            assert ph not in spoken, ph
        assert "SAMPLE" not in spoken.replace("SAMPLES", "")

    def test_the_removed_items_are_gone(self):
        html = _page(_cluster())
        for sid in ("files", "params", "stages-cards", "defaults", "provenance", "howto", "notes", "by-hand", "commands"):
            assert f'id="{sid}"' not in html
        for gone in ("Bytes", "sha256sum -c", "run_all.sh", "run_local.sh", "nextflow_local.sh",
                     "--stages", "every derivation the caller did not dictate", "Inputs</h3>", "Outputs</h3>",
                     "A. One sample by hand", "B. Every sample", "B. The one row", "commands.sh", "docker run",
                     "apptainer shell", "Enter the image", "Nothing to enter", "Output slots", "How-to command",
                     "<td>shared</td>", "params only", "implicit row", "{OUTPUT_DIR}",
                     '<p class="note"><code>params.yaml</code> also names', "one row per sample, columns:",
                     "must be on your PATH", "carries no shared parameters", "Copy the whole directory next to the data",
                     "Hover a box to trace what it feeds", 'id="pipeline-files"', "what you copy and edit → the engine",
                     "Copy this directory next to your data", "Copy this directory into your project directory",
                     "How the files fit together", "Our conventions", "Parameters and samples", "Decided by",
                     "What it holds", "How it is used", "Results land in", "What ran:", "MANIFEST",
                     "Make a directory for the run", "<h3", "<dl"):
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
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in _svg(html)                    # the param's tooltip
        assert _section(html, "samplesheet").count("&lt;script&gt;x&lt;/script&gt;") == 1  # the example row
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
        directory = _section(html, "directory")
        assert "<code>&lt;script&gt;s.sif</code> on &lt;script&gt;e&lt;/script&gt;" in directory
        assert "<code>module load &lt;script&gt;m&lt;/script&gt;</code>" in directory
        assert "script&gt;k" not in html and "script&gt;d" not in html     # the defaults table is gone, not escaped
        assert html.count("<script>") == _page().count("<script>")          # the shell's and the hover JS only

    def test_a_record_with_no_outputs_still_renders(self):
        html = _page(_no_outputs())
        assert _svg(html).count("(nothing published)") == 1                 # the picture and the directory say so, once each
        assert _section(html, "directory").count("(nothing published)") == 1
        assert re.findall(r'<section class="bx" id="([^"]+)"', html) == SECTION_IDS
