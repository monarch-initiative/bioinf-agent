"""
pipeline_page_html — the EXPLAIN page of a rendered pipeline: the one page a human
reads before running it on real data, rendered PURELY from the typed
`PipelineRecord` (agent/skills/pipeline_record.py).

The page has one fixed shape: the header banner, the picture (what runs, per
sample), the directory (a box naming the files you copy in on the left and what a run
adds on the right, beside one line per directory saying what it is and where it is
set), an example samples.csv, how to run it locally, how to run it on the cluster,
the stages, the footer. A pipeline
directory offers ONE way to run — every row of samples.csv, with Nextflow — and the
page shows it at each locus as the steps a person follows without thinking: change
directory, make nextflow available (locally: source the checkout's runtime env; on the
cluster: the launcher loads the modules), run. It speaks the files' own vocabulary —
`params.gtf`, the `reads` column, `results/<sample>/` — never the seal's
`{PLACEHOLDER}`s, and the command it shows per stage is the line main.nf runs, bound
by the Nextflow renderer itself (`bound_commands`), so the page and the files cannot
disagree. What params.yaml holds is read in params.yaml itself (every value carries
its comment there), so the page names its keys and does not repeat them.

Honesty guarantees, made structural:
  • PURE — reads only the record. No clock, no disk, no network.
  • ESCAPED — every value passes through the shared escaper, in the HTML and inside
    the SVG; a command, a path or a description can never inject markup.
  • ABSENCE IS STATED — a None renders as "unrecorded" / "never observed" / "not
    requested" / a note that says where the value will come from; it is never
    rounded up to a number or a path.
  • DETERMINISTIC — same record → same bytes. The picture is laid out arithmetically
    from label lengths: no layout library, no font metrics read.

Self-contained: the shared shell's CSS plus the picture's own (scoped inside the
<svg>), one inline SVG, and a few lines of inline JS that only toggle CSS classes on
hover — with JS off the page reads the same, minus the highlighting. Same shell,
banner and palette as the ENV report and the RUN dashboard, so the three pages are
one family. One public fn: render_pipeline_page; `picture_layout` is the picture's
geometry and `directory_tree` the directory's entries, exposed so a test can measure
them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent.skills.env_report_html import _close_page, _e, _empty, _header_banner, _open_page
from agent.skills.pipeline_record import (DEFAULT_STAGE_REQUEST, PipelineParam, PipelineRecord,
                                          PipelineStage, StageResources, _PLACEHOLDER_RE)
from agent.skills.pipeline_render_nextflow import (OUTDIR_PARAM, RUN_RECORDS, SAMPLESHEET_PARAM, bound_commands,
                                                   run_lines)

#: The standing footer, verbatim — the claim the page makes and the one it does not.
FOOTER = ("This page shows a pipeline derived from a sealed run; it proves the render is "
          "mechanical and traceable, never that the analysis is biologically right.")

#: `StageResources.measured_authority` → how it is shown. Every state the record can
#: carry has its own rendering; a state that rendered like another state would BE that
#: state to the reader. An unknown value falls back to its raw word in warning colour
#: rather than to a KeyError or a neighbour's badge.
_AUTHORITY_HTML = {
    "authoritative": '<span class="ok">authoritative</span>',
    "not_authoritative": '<span class="warn">NOT authoritative</span>',
    "unrecorded": '<span class="muted">authority unrecorded</span>',
    "mixed": '<span class="warn">mixed authority</span>',
    "none": '<span class="muted">no sealed step measured this stage</span>',
}
#: The states under which a measurement must not be used to size a request.
_UNTRUSTED_AUTHORITY = ("not_authoritative", "unrecorded", "mixed")
#: A value's kind, in words.
_VALUE_KIND = {"path": "a path", "prefix": "a prefix — a family of files named after it",
               "value": "a value"}
_TITLE_LEFT = "samples.csv & params.yaml"
_TITLE_MID = "stages, in execution order"
_TITLE_RIGHT = "published to results/"
#: The files a run directory is made of, as the renderer names them: what a person
#: copies, the launcher on top for the cluster, and what stays with the template.
_COPY_FILES = ("main.nf", "nextflow.config", "params.yaml")
_LAUNCHER = "launcher.sh"
_PAGE, _RECORD_DIR = "pipeline.html", ".pipeline/"
#: The directory's two columns, in order: what you put there, what a run adds.
_DIR_GROUPS = (("files", "you put here"), ("written", "a run adds"))
#: The section's own rules: the box hugs its names, the legend takes the rest of the
#: width beside it, and both stack on a narrow screen; a name never breaks mid-word.
_DIR_STYLE = (
    "<style>#directory .dirwrap{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:16px 36px;"
    "align-items:start;margin:12px 0 4px}"
    "#directory .dir{border:1px solid var(--border);background:var(--surface);justify-self:start;max-width:100%}"
    "#directory .dir .path{padding:8px 18px;border-bottom:1px solid var(--border);font-family:var(--mono);"
    "font-size:12.5px;color:var(--muted)}"
    "#directory .dir .cols{display:grid;grid-template-columns:max-content max-content}"
    "#directory .dir .col{padding:14px 18px 8px}#directory .dir .col+.col{border-left:1px solid var(--border)}"
    "#directory .dir .col-title{margin:0 0 12px;font-size:10.5px;font-weight:700;letter-spacing:.08em;"
    "text-transform:uppercase;color:var(--cyan)}"
    "#directory .dir ul{margin:0;padding:0;list-style:none}"
    "#directory .dir li{margin:0 0 10px;line-height:1.5;white-space:nowrap}"
    "#directory .legend{margin:0;padding:0 0 0 18px;line-height:1.55}"
    "#directory .legend>li{margin:0 0 10px}#directory .legend li::marker{color:var(--muted)}"
    "#directory .legend ul{margin:4px 0 0;padding-left:18px;list-style:circle}"
    "#directory .legend ul li{margin:3px 0;font-size:12.5px}"
    "#directory code{white-space:nowrap}"
    "@media(max-width:820px){#directory .dirwrap{grid-template-columns:1fr}}"
    "@media(max-width:520px){#directory .dir .cols{grid-template-columns:1fr}"
    "#directory .dir .col+.col{border-left:none;border-top:1px solid var(--border)}}</style>")
#: How the override example is spelled per value kind.
_SLOT = {"path": "<path>", "prefix": "<prefix>", "value": "<value>"}

# ── the picture: geometry ────────────────────────────────────────────────────
#
# Three columns. LEFT: the samplesheet's columns and the shared params, as small nodes.
# MIDDLE: the stages, one row per rank (rank = longest path from a source stage over
# the stage→stage edges), stages of one rank side by side. RIGHT: the published
# outputs. Every width is computed from the labels it must hold, so the text never
# overlaps; the SVG carries a viewBox and scales down on a narrow screen.

_MONO_EM = 0.64          # px per character per px of font size — an upper bound for ui-monospace
_NAME_PT = 12.0          # a node's primary line
_SUB_PT = 10.0           # a node's secondary line, group headers
_LABEL_PT = 10.5         # edge labels
_TITLE_PT = 10.0         # the three column titles
_PAD_X = 12.0
_LEFT_H, _LEFT_GAP, _GROUP_GAP = 34.0, 8.0, 18.0
_STAGE_H, _STAGE_HGAP, _RANK_GAP = 46.0, 36.0, 66.0
_OUT_H, _OUT_GAP = 34.0, 8.0
_COL_GAP_LM = 110.0      # room for the input S-curves
_COL_GAP_MR = 90.0       # room for the publish S-curves; a long edge's bow widens it
_BOW_STEP = 14.0
_MARGIN = 16.0
_TOP = 34.0              # first content row, under the column titles
_TITLE_Y = 16.0
_LABEL_H = 15.0


def _tw(text: str, pt: float) -> float:
    return len(text) * pt * _MONO_EM


def _short_digest(d: Optional[str]) -> str:
    if not d:
        return ""
    body = d.split(":", 1)[1] if ":" in d else d
    return body[:12]


def _key(p: PipelineParam) -> str:
    """A shared parameter as params.yaml and main.nf name it."""
    return f"params.{p.name.lower()}"


def _display(record: PipelineRecord, artifact: str) -> str:
    """An artifact as the files name it: a placeholder becomes the samplesheet column
    it binds to (`{SAMPLE}.counts.tsv` → `<sample>.counts.tsv`) or the params key."""
    col_of = {c.placeholder: c.name for c in record.samplesheet.columns}

    def sub(m) -> str:
        ph = m.group(1)
        return f"<{col_of[ph]}>" if ph in col_of else f"<params.{ph.lower()}>"
    return _PLACEHOLDER_RE.sub(sub, artifact)


def _results_dir(stage: PipelineStage) -> str:
    """Where a stage's outputs land: one directory per row, shared by every stage of
    that row — the layout the sealed how-to ran in. A cohort stage runs once, so it
    publishes flat."""
    return "results/<sample>/" if stage.scope == "per_sample" else "results/"


def _scope_words(stage: PipelineStage) -> str:
    return "per sample" if stage.scope == "per_sample" else "cohort"


@dataclass
class _Node:
    id: str                      # data-id: p:<param> | c:<placeholder> | s:<stage> | o:<stage>/<artifact>
    kind: str                    # param | column | stage | output
    name: str                    # data-name
    line1: str
    line2: str
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    tooltip: str = ""
    classes: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class _Edge:
    kind: str                    # input | stage | publish
    src: str
    dst: str
    path: str = ""
    label: str = ""              # stage edges: the artifact names
    lx: float = 0.0
    ly: float = 0.0
    anchor: str = "middle"
    artifacts: list[str] = field(default_factory=list)


@dataclass
class _Text:
    """One piece of text the picture draws and the box it occupies (top-left, width,
    height) — what the overlap test measures."""
    x: float
    y: float
    w: float
    h: float
    text: str
    role: str


@dataclass
class _Box:
    x: float
    y: float
    w: float
    h: float


@dataclass
class _Layout:
    width: float
    height: float
    nodes: list[_Node]
    edges: list[_Edge]
    texts: list[_Text]
    rows: list[list[str]]        # stage names per rank row, execution order
    titles: list[tuple[float, str]]
    headers: list[tuple[float, float, str]]


def _overlaps(a: _Box, b: _Box) -> bool:
    return not (a.x + a.w <= b.x or b.x + b.w <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y)


def _node_w(n: _Node, pt1: float = _NAME_PT) -> float:
    return max(_tw(n.line1, pt1), _tw(n.line2, _SUB_PT)) + 2 * _PAD_X


def _label_left(x: float, w: float, anchor: str) -> float:
    """Where a label's box starts for its text anchor; the drawn rect pads 4px each side."""
    if anchor == "middle":
        return x - w / 2
    if anchor == "end":
        return x - w + 4
    return x - 4


def _place_label(x: float, y: float, w: float, anchor: str, obstacles: list[_Box]) -> tuple[float, float, _Box]:
    """The first of a few vertical nudges at which a label box clears every obstacle
    (nodes and labels already placed). Best effort past the last nudge."""
    left = _label_left(x, w, anchor)
    chosen = None
    for dy in (0.0, 16.0, -16.0, 32.0, -32.0, 48.0, -48.0):
        box = _Box(left, y + dy - _LABEL_PT - 2, w, _LABEL_H)
        chosen = (x, y + dy, box)
        if not any(_overlaps(box, o) for o in obstacles):
            break
    return chosen


def picture_layout(record: PipelineRecord) -> _Layout:
    """The picture's geometry, computed from the record alone."""
    nodes: list[_Node] = []
    texts: list[_Text] = []
    edges: list[_Edge] = []
    headers: list[tuple[float, float, str]] = []

    # ── left column: the samplesheet's columns, then the shared params ───
    # The row key is drawn as a column but never as an edge: it is not data a stage
    # reads, it is what every task is tagged by and every results directory named
    # after — the legend says so.
    sheet = record.samplesheet
    key = sheet.columns[0]
    cols: list[_Node] = []
    for c in sheet.columns:
        if c is key:
            n = _Node(id=f"c:{c.placeholder}", kind="column", name=c.placeholder, line1=c.name,
                      line2="row key", classes="key",
                      tooltip=f"samples.csv column {c.name} — the row key: it tags every task and "
                              f"names results/<{c.name}>/")
        else:
            n = _Node(id=f"c:{c.placeholder}", kind="column", name=c.placeholder, line1=c.name,
                      line2=f"column · {c.value_kind}",
                      tooltip=f"samples.csv column {c.name} · {c.value_kind}"
                              + (f" · {c.format}" if c.format else "")
                              + (f" — {c.description}" if c.description else ""))
        cols.append(n)
    params: list[_Node] = []
    for p in record.params:
        if p.kind != "shared":
            continue                      # a samplesheet column (drawn above) or a thread slot (the stage's own cpus)
        tip = (f"{_key(p)} · {_VALUE_KIND.get(p.value_kind, p.value_kind)}"
               + (f" · example {p.default}" if p.default is not None else "")
               + (f" — {p.description}" if p.description else ""))
        params.append(_Node(id=f"p:{p.name}", kind="param", name=p.name, line1=_key(p),
                            line2=p.value_kind, tooltip=tip))
    groups: list[tuple[str, list[_Node]]] = [("samples.csv", cols), ("params.yaml", params)]
    left_nodes = [n for _, ns in groups for n in ns]
    left_by_name = {n.name: n for n in left_nodes}
    left_w = max([_node_w(n) for n in left_nodes]
                 + [_tw(g, _SUB_PT) for g, _ in groups]
                 + [_tw(_TITLE_LEFT, _TITLE_PT)])
    y = _TOP
    for title, ns in groups:
        headers.append((_MARGIN, y + 10, title))
        texts.append(_Text(_MARGIN, y, _tw(title, _SUB_PT), _SUB_PT + 2, title, "group"))
        y += 16
        if not ns:
            texts.append(_Text(_MARGIN, y, _tw("(none)", _SUB_PT), _SUB_PT + 2, "(none)", "group"))
            headers.append((_MARGIN, y + 10, "(none)"))
            y += 16
        for n in ns:
            n.x, n.y, n.w, n.h = _MARGIN, y, left_w, _LEFT_H
            y += _LEFT_H + _LEFT_GAP
        y += _GROUP_GAP
    left_bottom = y
    nodes.extend(left_nodes)

    # ── middle column: stages by rank ────────────────────────────────────
    pair_artifacts: dict[tuple[str, str], list[str]] = {}
    for st in record.stages:
        for i in st.inputs:
            if i.origin == "stage" and i.from_stage:
                pair_artifacts.setdefault((i.from_stage, st.name), []).append(i.artifact or i.name)
    rank: dict[str, int] = {}
    for st in record.stages:                       # index order: a producer precedes its consumer
        preds = [f for (f, t) in pair_artifacts if t == st.name]
        rank[st.name] = 0 if not preds else 1 + max(rank.get(f, 0) for f in preds)
    n_rows = (max(rank.values()) + 1) if rank else 0
    rows: list[list[PipelineStage]] = [[] for _ in range(n_rows)]
    for st in sorted(record.stages, key=lambda s: s.index):
        rows[rank[st.name]].append(st)
    stage_node: dict[str, _Node] = {}
    for st in record.stages:
        n = _Node(id=f"s:{st.name}", kind="stage", name=st.name, line1=st.name,
                  line2=f"{st.tool} · {_scope_words(st)}",
                  classes="unsized" if st.resources.requested_by == "default" else "",
                  extra={"data-index": str(st.index), "data-rank": str(rank[st.name])})
        r = st.resources
        if r.requested_by == "default":
            req = "unsized (default request)"
        elif r.requested_by == "seal":
            req = f"request cpus {r.cpus}, the sealed thread count · mem and time default · gpus {r.gpus}"
        else:
            req = f"request cpus {r.cpus} · mem {r.mem} · time {r.time} · gpus {r.gpus}"
        runs = "once per sample" if st.scope == "per_sample" else "once over the cohort"
        n.tooltip = (f"stage {st.index + 1} {st.name} · {st.tool} · runs {runs} · "
                     f"image {_short_digest(st.image_digest) or 'unrecorded'} · {req} · "
                     f"measured authority {r.measured_authority}")
        n.w, n.h = _node_w(n, 12.5), _STAGE_H
        stage_node[st.name] = n
    row_w = [sum(stage_node[s.name].w for s in row) + _STAGE_HGAP * (len(row) - 1) for row in rows]
    mid_w = max(row_w + [_tw(_TITLE_MID, _TITLE_PT)])
    mid_x0 = _MARGIN + left_w + _COL_GAP_LM
    col_of: dict[str, int] = {}
    for ri, row in enumerate(rows):
        x = mid_x0 + (mid_w - row_w[ri]) / 2
        yy = _TOP + 16 + ri * (_STAGE_H + _RANK_GAP)
        for ci, st in enumerate(row):
            n = stage_node[st.name]
            n.x, n.y = x, yy
            col_of[st.name] = ci
            x += n.w + _STAGE_HGAP
    mid_bottom = (_TOP + 16 + (n_rows - 1) * (_STAGE_H + _RANK_GAP) + _STAGE_H) if n_rows else _TOP
    nodes.extend(stage_node[s.name] for s in record.stages)

    # ── right column: published outputs ──────────────────────────────────
    # Edge labels speak the files' vocabulary, as the output boxes do: one file, one name.
    def edge_label(pair: tuple[str, str]) -> str:
        return ", ".join(_display(record, a) for a in pair_artifacts[pair])
    long_pairs = [(f, t) for (f, t) in pair_artifacts if rank[t] - rank[f] >= 2]
    long_label_w = max([_tw(edge_label(p), _LABEL_PT) for p in long_pairs] + [0.0])
    bow_room = (22 + _BOW_STEP * (len(long_pairs) - 1) + long_label_w + 24) if long_pairs else 0.0
    gap_mr = max(_COL_GAP_MR, bow_room + 30)
    outs: list[_Node] = []
    for st in record.stages:
        for o in st.outputs:
            d = _results_dir(st)
            shown = _display(record, o.artifact)
            outs.append(_Node(id=f"o:{st.name}/{o.artifact}", kind="output", name=o.artifact,
                              line1=shown, line2=d,
                              tooltip=f"{d}{shown} · published by {st.name}"
                                      + (f" · declared as {o.declared_pattern}" if o.declared_pattern else "")
                                      + (f" · observed as {o.observed} in the sealed run" if o.observed
                                         else " · never observed in the sealed run"),
                              extra={"data-stage": st.name}))
    right_w = max([_node_w(n) for n in outs] + [_tw(_TITLE_RIGHT, _TITLE_PT),
                                                _tw("(nothing published)", _SUB_PT)])
    right_x = mid_x0 + mid_w + gap_mr
    last_bottom = _TOP + 16 - _OUT_GAP
    for n in outs:
        st = stage_node[n.extra["data-stage"]]
        n.x, n.w, n.h = right_x, right_w, _OUT_H
        n.y = max(st.y + st.h / 2 - _OUT_H / 2, last_bottom + _OUT_GAP)
        last_bottom = n.y + n.h
    if not outs:
        headers.append((right_x, _TOP + 26, "(nothing published)"))
        texts.append(_Text(right_x, _TOP + 16, _tw("(nothing published)", _SUB_PT), _SUB_PT + 2,
                           "(nothing published)", "group"))
    nodes.extend(outs)

    # node text boxes
    for n in nodes:
        pt1 = 12.5 if n.kind == "stage" else _NAME_PT
        texts.append(_Text(n.x + _PAD_X, n.y + n.h / 2 - 2 - pt1, _tw(n.line1, pt1), pt1 + 2, n.line1, n.kind))
        texts.append(_Text(n.x + _PAD_X, n.y + n.h / 2 + 11 - _SUB_PT, _tw(n.line2, _SUB_PT), _SUB_PT + 2,
                           n.line2, n.kind))
    titles = [(_MARGIN, _TITLE_LEFT), (mid_x0, _TITLE_MID), (right_x, _TITLE_RIGHT)]
    for tx, t in titles:
        texts.append(_Text(tx, _TITLE_Y - _TITLE_PT, _tw(t, _TITLE_PT), _TITLE_PT + 2, t, "title"))
    obstacles = [_Box(n.x, n.y, n.w, n.h) for n in nodes]

    # ── edges: column / param → stage (never the row key) ────────────────
    for st in record.stages:
        s = stage_node[st.name]
        ins = [i for i in st.inputs if i.origin in ("param", "column")
               and i.name in left_by_name and i.name != key.placeholder]
        step = min(8.0, (0.4 * s.w - 10) / max(1, len(ins) - 1))
        bus_y = s.y - 10 - 2 * col_of[st.name]
        xb = mid_x0 - 12
        for k, i in enumerate(ins):
            ln = left_by_name[i.name]
            x0, y0 = ln.x + ln.w, ln.y + ln.h / 2
            xh = s.x + 10 + step * k
            dx = (xb - x0) / 2
            path = (f"M{x0:.1f} {y0:.1f} C{x0 + dx:.1f} {y0:.1f}, {xb - dx:.1f} {bus_y:.1f}, {xb:.1f} {bus_y:.1f} "
                    f"C{xh:.1f} {bus_y:.1f}, {xh:.1f} {bus_y:.1f}, {xh:.1f} {s.y:.1f}")
            edges.append(_Edge(kind="input", src=ln.id, dst=s.id, path=path))

    # ── edges: stage → stage, labelled with the artifacts ────────────────
    out_pairs: dict[str, list[tuple[str, str]]] = {}
    in_pairs: dict[str, list[tuple[str, str]]] = {}
    ordered_pairs = sorted(pair_artifacts, key=lambda p: (record.stage(p[0]).index, record.stage(p[1]).index))
    for f, t in ordered_pairs:
        out_pairs.setdefault(f, []).append((f, t))
        in_pairs.setdefault(t, []).append((f, t))
    long_i = 0
    for f, t in ordered_pairs:
        src, dst = stage_node[f], stage_node[t]
        j, n_out = out_pairs[f].index((f, t)), len(out_pairs[f])
        k, n_in = in_pairs[t].index((f, t)), len(in_pairs[t])
        x0 = src.x + src.w * 0.5 + (j - (n_out - 1) / 2) * 10
        y0 = src.y + src.h
        x1 = dst.x + dst.w * 0.62 + (k - (n_in - 1) / 2) * 10
        y1 = dst.y
        label = edge_label((f, t))
        lw = _tw(label, _LABEL_PT) + 8
        if rank[t] - rank[f] <= 1:
            ym = (y0 + y1) / 2
            path = f"M{x0:.1f} {y0:.1f} C{x0:.1f} {ym:.1f}, {x1:.1f} {ym:.1f}, {x1:.1f} {y1:.1f}"
            lx, ly, box = _place_label((x0 + x1) / 2, ym + 4, lw, "middle", obstacles)
            anchor = "middle"
        else:
            apex = mid_x0 + mid_w + 22 + _BOW_STEP * long_i
            long_i += 1
            cx = (8 * apex - x0 - x1) / 6
            path = f"M{x0:.1f} {y0:.1f} C{cx:.1f} {y0 + 40:.1f}, {cx:.1f} {y1 - 40:.1f}, {x1:.1f} {y1:.1f}"
            lx, ly, box = _place_label(apex + 6, (y0 + y1) / 2 + 4, lw, "start", obstacles)
            anchor = "start"
        obstacles.append(box)
        texts.append(_Text(box.x, box.y, box.w, box.h, label, "label"))
        edges.append(_Edge(kind="stage", src=src.id, dst=dst.id, path=path, label=label,
                           lx=lx, ly=ly, anchor=anchor, artifacts=list(pair_artifacts[(f, t)])))

    # ── edges: stage → published output ──────────────────────────────────
    for st in record.stages:
        s = stage_node[st.name]
        mine = [o for o in outs if o.extra["data-stage"] == st.name]
        for k, o in enumerate(mine):
            x0 = s.x + s.w
            y0 = s.y + s.h * 0.5 + (k - (len(mine) - 1) / 2) * 8
            x1, y1 = o.x, o.y + o.h / 2
            xm = (x0 + x1) / 2
            path = f"M{x0:.1f} {y0:.1f} C{xm:.1f} {y0:.1f}, {xm:.1f} {y1:.1f}, {x1:.1f} {y1:.1f}"
            edges.append(_Edge(kind="publish", src=s.id, dst=o.id, path=path))

    width = right_x + right_w + _MARGIN
    height = max([left_bottom, mid_bottom, last_bottom, _TOP + 40]
                 + [t.y + t.h for t in texts]) + _MARGIN
    return _Layout(width=width, height=height, nodes=nodes, edges=edges, texts=texts,
                   rows=[[s.name for s in row] for row in rows], titles=titles, headers=headers)


# ── the picture: SVG ─────────────────────────────────────────────────────────

_SVG_CSS = """
.node rect{fill:var(--surface);stroke:var(--border);stroke-width:1}
.node.column.key rect{stroke:var(--cyan)}
.node.file rect{stroke:var(--border);stroke-width:1.2}
.node.file.engine rect{stroke:var(--cyan);stroke-width:1.4}
.node.file.written rect{stroke:var(--yellow)}
.node.file text.l1{font:700 12.5px var(--mono)}
.edge.wire{stroke:var(--muted);stroke-width:1.2;marker-end:url(#arr-input)}
.edge.wire.out{stroke:var(--yellow);marker-end:url(#arr-publish)}
.node.column.key text.l1{font-weight:700}
.node.stage rect{stroke:var(--cyan);stroke-width:1.4}
.node.stage.unsized rect{stroke-dasharray:5 3}
.node.output rect{stroke:var(--yellow)}
.node text{fill:var(--ink);font:12px var(--mono)}
.node.stage text.l1{font:700 12.5px var(--mono)}
.node text.sub{fill:var(--muted);font-size:10px}
.edge{fill:none;stroke:var(--muted);stroke-width:1.1;marker-end:url(#arr-input)}
.edge.stage{stroke:var(--cyan);stroke-width:1.6;marker-end:url(#arr-stage)}
.edge.publish{stroke:var(--yellow);stroke-width:1.2;marker-end:url(#arr-publish)}
.lbl rect{fill:var(--bg)}
.lbl text{fill:var(--ink);font:10.5px var(--mono)}
.title{fill:var(--cyan);font:700 10px sans-serif;letter-spacing:.12em;text-transform:uppercase}
.grp{fill:var(--muted);font:600 10px sans-serif;letter-spacing:.1em}
.arr-input{fill:var(--muted)}.arr-stage{fill:var(--cyan)}.arr-publish{fill:var(--yellow)}.arr-hl{fill:var(--yellow)}
.node{cursor:default;outline:none}
.edge.hl{stroke:var(--yellow);stroke-width:2.6;marker-end:url(#arr-hl)}
.node.hl rect{stroke:var(--yellow);stroke-width:2}
.node.src rect{fill:var(--yellow-soft);stroke:var(--yellow);stroke-width:2}
svg.active .node:not(.hl):not(.src){opacity:.35}
svg.active .edge:not(.hl){opacity:.2}
svg.active .lbl:not(.hl){opacity:.3}
"""

_HOVER_JS_BODY = (
    'var N=s.querySelectorAll("[data-id]"),E=s.querySelectorAll("[data-edge]");'
    'function off(){s.classList.remove("active");N.forEach(function(n){n.classList.remove("hl","src")});'
    'E.forEach(function(e){e.classList.remove("hl")})}'
    'function on(n){off();var id=n.getAttribute("data-id");s.classList.add("active");n.classList.add("src");'
    'E.forEach(function(e){var f=e.getAttribute("data-from"),t=e.getAttribute("data-to");'
    'if(f!==id&&t!==id)return;e.classList.add("hl");var o=f===id?t:f;'
    'N.forEach(function(m){if(m.getAttribute("data-id")===o)m.classList.add("hl")})})}'
    'N.forEach(function(n){n.addEventListener("mouseenter",function(){on(n)});'
    'n.addEventListener("focus",function(){on(n)});n.addEventListener("mouseleave",off);'
    'n.addEventListener("blur",off)})})()</script>'
)


def _hover_js(svg_id: str) -> str:
    """The hover script bound to one picture: it only toggles CSS classes."""
    return '<script>(function(){var s=document.getElementById("' + svg_id + '");if(!s)return;' + _HOVER_JS_BODY


def _marker(mid: str, cls: str) -> str:
    return (f'<marker id="{mid}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" '
            f'orient="auto" markerUnits="userSpaceOnUse"><path class="{cls}" d="M0 0.5 L7.5 4 L0 7.5 Z"/></marker>')


def _svg(layout: _Layout, record: PipelineRecord) -> str:
    W, H = layout.width, layout.height
    P: list[str] = []
    P.append(f'<svg id="pipeline-picture" viewBox="0 0 {W:.0f} {H:.0f}" width="100%" '
             f'style="max-width:{W:.0f}px;height:auto;display:block" role="img" '
             f'aria-label="pipeline {_e(record.name)}: samplesheet columns and params feeding stages '
             f'in execution order, and the published outputs">')
    P.append(f"<style>{_SVG_CSS}</style>")
    P.append("<defs>" + _marker("arr-input", "arr-input") + _marker("arr-stage", "arr-stage")
             + _marker("arr-publish", "arr-publish") + _marker("arr-hl", "arr-hl") + "</defs>")
    for tx, t in layout.titles:
        P.append(f'<text class="title" x="{tx:.1f}" y="{_TITLE_Y:.1f}">{_e(t)}</text>')
    for hx, hy, t in layout.headers:
        P.append(f'<text class="grp" x="{hx:.1f}" y="{hy:.1f}">{_e(t)}</text>')
    for e in layout.edges:                        # edges under the nodes
        extra = f' data-artifacts="{_e(" ".join(e.artifacts))}"' if e.artifacts else ""
        P.append(f'<path class="edge {e.kind}" data-edge="{e.kind}" data-from="{_e(e.src)}" '
                 f'data-to="{_e(e.dst)}"{extra} d="{e.path}"/>')
    for e in layout.edges:
        if not e.label:
            continue
        lw = _tw(e.label, _LABEL_PT) + 8
        left = _label_left(e.lx, lw, e.anchor)
        P.append(f'<g class="lbl" data-edge="{e.kind}-label" data-from="{_e(e.src)}" data-to="{_e(e.dst)}">'
                 f'<rect x="{left:.1f}" y="{e.ly - _LABEL_PT - 2:.1f}" width="{lw:.1f}" height="{_LABEL_H:.1f}" rx="2"/>'
                 f'<text x="{e.lx:.1f}" y="{e.ly:.1f}" text-anchor="{e.anchor}">{_e(e.label)}</text></g>')
    for n in layout.nodes:
        attrs = "".join(f' {k}="{_e(v)}"' for k, v in sorted(n.extra.items()))
        cls = f"node {n.kind}" + (f" {n.classes}" if n.classes else "")
        P.append(f'<g class="{cls}" data-node="{n.kind}" data-id="{_e(n.id)}" data-name="{_e(n.name)}"{attrs} '
                 f'tabindex="0"><title>{_e(n.tooltip)}</title>'
                 f'<rect x="{n.x:.1f}" y="{n.y:.1f}" width="{n.w:.1f}" height="{n.h:.1f}" rx="3"/>'
                 f'<text class="l1" x="{n.x + _PAD_X:.1f}" y="{n.y + n.h / 2 - 2:.1f}">{_e(n.line1)}</text>'
                 f'<text class="sub" x="{n.x + _PAD_X:.1f}" y="{n.y + n.h / 2 + 11:.1f}">{_e(n.line2)}</text></g>')
    P.append("</svg>")
    return "".join(P)



# ── the files picture: how the directory fits together ──────────────────────
#
# ── how the files fit together: the launch line and the directory listing ──────
#
# No wiring diagram: the one line that runs every Nextflow pipeline, then the
# directory as a listing — the files a person copies, the one they write, the record,
# and what a run adds — each row saying how the run pulls the file in and what it
# holds, read off the record. A deviation the record can express (several images, a
# cohort stage, no cluster named) changes a cell; one it cannot express was refused
# before this page existed.


@dataclass(frozen=True)
class DirEntry:
    """One entry of the directory. `group` is "files" (what you put there) or
    "written" (what a run adds); `role` is the verdict beside a file's name — copy ·
    copy, then edit · write your own · copy, cluster only — and empty for what a run
    writes. The box names an entry and no more. A directory also has a legend line:
    `what` says what it is and where it is set, `command` spans in backticks, and
    `files` lists what it holds — (name, what it holds, which rendered file writes
    it) — for the run records alone. Both are empty for a file."""
    group: str
    name: str
    role: str
    what: str
    files: tuple[tuple[str, str, str], ...]


def directory_tree(record: PipelineRecord) -> list[DirEntry]:
    """The directory, read off the record alone: the files you put there, each with
    its role, then what a run adds — each directory with one line saying what it is
    and where it is set; a results directory only when a stage publishes into it."""
    per_sample = any(o for s in record.stages if s.scope == "per_sample" for o in s.outputs)
    cohort = any(o for s in record.stages if s.scope == "cohort" for o in s.outputs)
    rows = [DirEntry("files", _COPY_FILES[0], "copy", "", ()),
            DirEntry("files", _COPY_FILES[1], "copy", "", ()),
            DirEntry("files", _COPY_FILES[2], "copy, then edit", "", ()),
            DirEntry("files", SAMPLESHEET_PARAM[1], "write your own", "", ()),
            DirEntry("files", _LAUNCHER, "copy, cluster only", "", ())]
    if per_sample:
        rows.append(DirEntry("written", "results/<sample>/", "",
                             "where each sample's results are published, always the latest run's; set by "
                             f"`{OUTDIR_PARAM[0]}:` in params.yaml", ()))
    if cohort:
        rows.append(DirEntry("written", "results/", "",
                             "where the cohort stages' results are published, beside the per-sample directories", ()))
    rows += [DirEntry("written", "runs/<timestamp>/", "", "one directory per run, never overwritten", RUN_RECORDS),
             DirEntry("written", "work/", "", "Nextflow's canonical work directory", ())]
    return rows


def _directory_box(rows: list[DirEntry]) -> str:
    """The directory drawn as a box: its path on top, the files you put there on the
    left with their role, what a run adds on the right — names only."""
    out = ['<div class="dir"><div class="path">path/to/your/project/</div><div class="cols">']
    for group, title in _DIR_GROUPS:
        out.append(f'<div class="col"><div class="col-title">{_e(title)}</div><ul>')
        for r in (r for r in rows if r.group == group):
            role = f' <span class="muted">· {_e(r.role)}</span>' if r.role else ""
            out.append(f"<li><code>{_e(r.name)}</code>{role}</li>")
        out.append("</ul></div>")
    out.append("</div></div>")
    return "".join(out)


def _directory_legend(rows: list[DirEntry]) -> str:
    """One line per directory a run adds, and under the run records one per file:
    what it holds and, muted, which rendered file writes it."""
    out = ['<ul class="legend">']
    for r in (r for r in rows if r.group == "written"):
        files = "".join(f'<li><code>{_e(n)}</code> — {_ticks_to_code(what)} <span class="muted">· from {_e(by)}</span></li>'
                        for n, what, by in r.files)
        out.append(f"<li><code>{_e(r.name)}</code> — {_ticks_to_code(r.what)}{f'<ul>{files}</ul>' if files else ''}</li>")
    out.append("</ul>")
    return "".join(out)


def _override_example(record: PipelineRecord) -> str:
    """One value overridden on the command line, spelled with this pipeline's own key:
    a literal-valued shared param reads best, then any shared param, then outdir."""
    shared = [p for p in record.params if p.kind == "shared"]
    pick = next((p for p in shared if p.value_kind == "value"), shared[0] if shared else None)
    if pick is None:
        return f"--{OUTDIR_PARAM[0]} <directory>"
    return f"--{pick.name.lower()} {_SLOT.get(pick.value_kind, '<value>')}"


# ── the sections ─────────────────────────────────────────────────────────────


def _section(sid: str, title: str, note: str, body: str) -> str:
    n = f' <span class="note">{_e(note)}</span>' if note else ""
    return f'<section class="bx" id="{sid}"><h2>{_e(title)}{n}</h2><div class="bx-body">{body}</div></section>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Cells are inserted verbatim (callers escape); headers are escaped here."""
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tbl-wrap"><table><tr>{head}</tr>{body}</table></div>'


def _muted(text: str) -> str:
    return f'<span class="muted">{_e(text)}</span>'


def _code(v: Optional[str], absent: str = "unrecorded") -> str:
    return f"<code>{_e(v)}</code>" if v else _muted(absent)


def _digest_cell(d: Optional[str]) -> str:
    if not d:
        return _muted("unrecorded")
    return f"<b>{_e(_short_digest(d))}</b> <code>{_e(d)}</code>"


def _steps(steps: list[tuple[str, list[str]]]) -> str:
    """Numbered steps: each entry is its sentence (HTML, callers escape) and the lines
    to type, in one <pre>; a step with nothing to type shows the sentence alone."""
    items: list[str] = []
    for text, lines in steps:
        typed = "\n".join(lines)
        pre = f"<pre>{_e(typed)}</pre>" if lines else ""
        items.append(f"<li>{text}{pre}</li>")
    return "<ol>" + "".join(items) + "</ol>"


def _ordered(record: PipelineRecord) -> list[PipelineStage]:
    return sorted(record.stages, key=lambda s: s.index)


def _images(record: PipelineRecord) -> list[PipelineStage]:
    """One stage per distinct image, in stage order — each image's first appearance."""
    seen: set[tuple[Optional[str], Optional[str]]] = set()
    out: list[PipelineStage] = []
    for st in _ordered(record):
        k = (st.image, st.image_digest)
        if k not in seen:
            seen.add(k)
            out.append(st)
    return out


def _image_cell(st: PipelineStage) -> str:
    if not st.image and not st.image_digest:
        return _muted("unrecorded")
    tag = f"<code>{_e(st.image)}</code>" if st.image else _muted("tag unrecorded")
    short = (f' <span class="muted" title="{_e(st.image_digest)}">{_e(_short_digest(st.image_digest))}</span>'
             if st.image_digest else "")
    return tag + short


def _header(record: PipelineRecord) -> str:
    n = len(record.samplesheet.rows)
    rows_word = f"{n} example row{'s' if n != 1 else ''}"
    n_st = len(record.stages)
    pill = f'<span class="pill na">{rows_word} · {n_st} stage{"s" if n_st != 1 else ""}</span>'
    path = f"<code>{_e(record.sealed_workflow_path)}</code>" if record.sealed_workflow_path else _muted("path unrecorded")
    digests = "<br>".join(_digest_cell(d) for d in record.env_digests) or _muted("unrecorded")
    if len(record.env_digests) == 1:
        digests += ' <span class="muted">— every stage runs inside it</span>'
    rows = [
        ("Rendered from", f"sealed workflow <b>{_e(record.sealed_workflow)}</b> — {path}"),
        ("Sealed workflow sha256", _code(record.sealed_workflow_sha256)),
        ("Created", _e(record.created_at)),
        ("Image", digests),
        ("Stages", f"{n_st} in execution order: "
                   + " → ".join(f"<code>{_e(s.name)}</code>" for s in _ordered(record))),
        ("Samples", f"{rows_word} in <code>samples.csv</code> — the sealed run's own; replace them with yours"),
    ]
    if record.compute_env:
        mods = " ".join(f"<code>{_e(m)}</code>" for m in record.modules)
        rows.append(("Cluster", f"<b>{_e(record.compute_env)}</b> — "
                                + (f"module load {mods}" if mods else "no modules to load")))
    return _header_banner(f"Pipeline — {_e(record.name)}", pill, rows)


def _ticks_to_code(text: str) -> str:
    """A record value's `command` spans as <code>, everything escaped."""
    parts = text.split("`")
    return "".join(f"<code>{_e(s)}</code>" if i % 2 else _e(s) for i, s in enumerate(parts))


def _directory_section(record: PipelineRecord) -> str:
    intro = ('<p class="note">Copy these files into the directory where you want to run Nextflow, and write '
             'your <code>samples.csv</code> there. Launch from inside it: Nextflow works out of the directory '
             'it is started in.</p>')
    stays = (f'<p class="note"><code>{_PAGE}</code> (this page) and <code>{_RECORD_DIR}</code> (the record it '
             'was rendered from) stay with the template.</p>')
    rows = directory_tree(record)
    return _section("directory", "The directory", "what you put there, what a run adds",
                    _DIR_STYLE + intro + '<div class="dirwrap">' + _directory_box(rows) + _directory_legend(rows)
                    + "</div>" + stays)


def _samplesheet_section(record: PipelineRecord) -> str:
    sheet = record.samplesheet
    cols = [c.name for c in sheet.columns]
    lines = [",".join(cols)]
    if sheet.rows:
        lines.append(",".join(str(sheet.rows[0].get(c, "")) for c in cols))
    words = []
    for c in sheet.columns:
        bits = [c.description or _VALUE_KIND.get(c.value_kind, c.value_kind)]
        if c.format:
            bits.append(c.format)
        words.append(f"<code>{_e(c.name)}</code> — {_e(' · '.join(bits))}")
    note = '<p class="note">' + "; ".join(words) + ". One row per sample, paths absolute.</p>"
    return _section("samplesheet", "Example samples.csv", "one sample: the header and the sealed run's first row",
                    f"<pre>{_e(chr(10).join(lines))}</pre>" + note)


def _picture_section(record: PipelineRecord) -> str:
    layout = picture_layout(record)
    legend = ('<p class="note">Every stage runs once per row of <code>samples.csv</code>; '
              '<code>sample</code> is the row key — it tags each task and names '
              '<code>results/&lt;sample&gt;/</code>. Thin grey lines: a column or <code>params.*</code> '
              'value a stage reads. Cyan lines: a file flowing from the stage that writes it to a stage '
              'that reads it, labelled with its name. Yellow lines: what a stage publishes. A dashed stage '
              'box is <b>unsized</b> (default request). Stages on one row have no edge between them and '
              'may run side by side. Hover or focus a node to trace it.</p>')
    return _section("picture", "The picture",
                    "samples.csv and params.yaml → stages in execution order → published outputs",
                    _svg(layout, record) + _hover_js("pipeline-picture") + legend)


def _run_local_section(record: PipelineRecord) -> str:
    imgs = _images(record)
    if len(imgs) == 1 and imgs[0].image:
        present = f"Docker must be running with <code>{_e(imgs[0].image)}</code> present"
    elif len(imgs) == 1:
        present = "Docker must be running with the frozen image present"
    else:
        present = ("Docker must be running with every frozen image present "
                   "(<code>nextflow.config</code> names them)")
    rt = record.local_runtime
    if rt is not None:
        make = ("Make <code>nextflow</code> available. This checkout's runtime env carries it with its own "
                "Java; sourcing the line below puts both on your PATH for this shell. " + present + ".")
        if rt.nextflow is None:
            make += (' <span class="warn">nextflow was not in the runtime env when this page was rendered: '
                     'run <code>./scripts/setup.sh</code> first.</span>')
        make_lines = [f"source {rt.activate}"]
    else:
        make = ("Make <code>nextflow</code> available on your PATH — this machine's runtime env was not "
                "recorded when the page was rendered. " + present + ".")
        make_lines = []
    steps = _steps([
        ("Change into the run directory: the files above, with your <code>samples.csv</code> beside them and "
         "<code>params.yaml</code> edited where your data differs.",
         [f"cd /path/to/{record.name}"]),
        (make, make_lines),
        ("Run. Nextflow starts every stage inside the frozen image through docker; <code>-resume</code> "
         "re-runs only the stages whose inputs or parameters changed. A value for this run only goes after the "
         f"line — <code>{_e(_override_example(record))}</code> — and wins over <code>params.yaml</code>.",
         run_lines(record, "local")),
    ])
    return _section("run-local", "Run it locally", "every row of samples.csv, with Nextflow through docker", steps)


def _run_hpc_section(record: PipelineRecord) -> str:
    env = record.compute_env
    imgs = _images(record)
    key = next((s.request_key for s in _ordered(record) if s.request_key), None)
    env_arg = f'"{env}"' if env else "<the cluster's env>"
    key_arg = f'"{key}"' if key else "<the env's freeze_request_key>"
    call = _e(f"stage_apptainer_image(project=<your project>, env={env_arg}, freeze_request_key={key_arg})")
    notes: list[str] = []
    for st in imgs:
        label = _short_digest(st.image_digest) or st.image or ""
        which = f" for image <code>{_e(label)}</code>" if len(imgs) > 1 else ""
        if st.sif_path:
            notes.append(f'<p class="note">The <code>slurm</code> profile in <code>nextflow.config</code> runs '
                         f'the <code>.sif</code>{which} at <code>{_e(st.sif_path)}</code> — where '
                         f'<code>stage_apptainer_image</code> put it on <b>{_e(env or "the cluster")}</b>.</p>')
        else:
            notes.append(f'<p class="warn-note">The <code>slurm</code> profile\'s <code>container</code> in '
                         f'<code>nextflow.config</code> is empty{which}: re-render with <code>env=</code> naming '
                         f'the cluster, or set it to the <code>.sif</code> that <code>{call}</code> reports. '
                         f'The workflow refuses to start until it is set.</p>')
    if record.modules:
        mods = " ".join(f"<code>{_e(m)}</code>" for m in record.modules)
        submit = (f"Submit. <code>launcher.sh</code> loads {mods} and runs Nextflow as a small manager "
                  "job; every stage of every sample is its own SLURM job. Nothing to activate by hand.")
    else:
        submit = ("Submit. <code>launcher.sh</code> runs Nextflow as a small manager job — make apptainer "
                  "and nextflow available first"
                  + ("" if env else ", it was rendered without a cluster named and loads no modules")
                  + "; every stage of every sample is its own SLURM job.")
    passthrough = (f" Flags after <code>{_LAUNCHER}</code> go through to Nextflow: "
                   f"<code>{_e(_override_example(record))}</code> overrides <code>params.yaml</code> for this run.")
    steps = _steps([
        ("Change into the run directory on the cluster: the files above, with your <code>samples.csv</code> "
         "beside them, every path in it and in <code>params.yaml</code> a cluster path. SLURM starts the "
         "manager job here; the launcher keeps Nextflow's own files under <code>.nextflow_home</code> inside it.",
         [f"cd /path/in/your/project/{record.name}"]),
        (submit + passthrough, run_lines(record, "hpc")),
        ("Watch it; <code>sacct -j &lt;jobid&gt;</code> once it has ended.", ["squeue -u $USER"]),
    ])
    return _section("run-hpc", "Run it on the cluster", "the same files, through the .sif and SLURM",
                    "".join(notes) + steps)


def _request_html(r: StageResources) -> str:
    d = DEFAULT_STAGE_REQUEST
    gpus = f" · {r.gpus} gpu{'s' if r.gpus != 1 else ''}" if r.gpus else ""
    if r.requested_by == "default":
        cpus = d.get("cpus")
        return (f'<span class="warn">unsized</span> <span class="muted">(DEFAULT {_e(d.get("time"))} · '
                f'{_e(d.get("mem"))} · {_e(cpus)} cpu{"s" if cpus != 1 else ""}{_e(gpus)})</span>')
    if r.requested_by == "seal":
        words = (f"— the command's thread count, as the sealed run used it; mem and time DEFAULT "
                 f"{d.get('mem')} · {d.get('time')}{gpus}")
        return f'cpus {_e(r.cpus)} <span class="muted">{_e(words)}</span>'
    cpus = (f"cpus {r.cpus}" + (" (the command's thread count)" if r.threads_slot else "")
            if r.cpus is not None else "cpus not requested")
    parts = [cpus, f"mem {r.mem}" if r.mem else "mem not requested",
             f"time {r.time}" if r.time else "time not requested", f"gpus {r.gpus}"]
    return _e(" · ".join(parts))


def _measured_html(r: StageResources) -> str:
    auth = _AUTHORITY_HTML.get(r.measured_authority) or f'<span class="warn">{_e(r.measured_authority)}</span>'
    if all(v is None for v in (r.measured_wall_seconds, r.measured_peak_rss_mb, r.measured_max_cpu_percent)):
        return auth if r.measured_authority == "none" else f"{_muted('no measurement')} — {auth}"
    nums = " · ".join([
        f"wall {r.measured_wall_seconds:.1f} s" if r.measured_wall_seconds is not None else "wall unrecorded",
        f"peak RSS {r.measured_peak_rss_mb:.0f} MB" if r.measured_peak_rss_mb is not None else "peak RSS unrecorded",
        f"CPU {r.measured_max_cpu_percent:.0f}%" if r.measured_max_cpu_percent is not None else "CPU unrecorded"])
    return f"{_e(nums)} — {auth}"


def _command_cell(record: PipelineRecord, st: PipelineStage) -> str:
    """The line(s) main.nf runs for the stage — bound by the renderer, never retyped."""
    try:
        lines = bound_commands(record, st)
    except ValueError as e:
        return f'<span class="warn">{_e(f"cannot be bound: {e}")}</span>'
    return "<pre>" + _e("\n".join(lines)) + "</pre>"


def _stages_section(record: PipelineRecord) -> str:
    if not record.stages:
        return _section("stages", "Stages", "", _empty("the record holds no stages"))
    stages = _ordered(record)
    imgs = _images(record)
    single = len(imgs) == 1
    headers = (["Stage", "Tool", "Command, as main.nf runs it"] + ([] if single else ["Image"])
               + ["Request", "Measured"])
    rows: list[list[str]] = []
    for st in stages:
        row = [f"<code>{_e(st.name)}</code>", _e(st.tool), _command_cell(record, st)]
        if not single:
            row.append(_image_cell(st))
        row += [_request_html(st.resources), _measured_html(st.resources)]
        rows.append(row)
    body = (f'<p class="note">Every stage runs inside {_image_cell(imgs[0])}.</p>' if single else "")
    body += _table(headers, rows)
    untrusted = [st.name for st in stages if st.resources.measured_authority in _UNTRUSTED_AUTHORITY]
    if untrusted:
        body += (f'<p class="warn-note">{_e(", ".join(untrusted))}: measured under emulation, or of unrecorded '
                 'or mixed authority — do not size from these; size from a run on hardware matching the '
                 'image.</p>')
    return _section("stages", "Stages", "one row per stage, in execution order", body)


def render_pipeline_page(record: PipelineRecord) -> str:
    """Render the pipeline record as a self-contained HTML page (see the module
    docstring for the honesty contract this upholds)."""
    P: list[str] = [
        _open_page(f"Pipeline — {record.name}"),
        _header(record),
        _picture_section(record),
        _directory_section(record),
        _samplesheet_section(record),
        _run_local_section(record),
        _run_hpc_section(record),
        _stages_section(record),
        _close_page(f'<p class="gen">{_e(FOOTER)}</p>'),
    ]
    return "\n".join(P)


__all__ = ["render_pipeline_page", "picture_layout", "directory_tree", "DirEntry", "FOOTER"]
