"""
pipeline_page_html — the EXPLAIN page of a rendered pipeline: the one page a human
reads before running it on real data, rendered PURELY from the typed
`PipelineRecord` (agent/skills/pipeline_record.py).

The page has one fixed shape: the header banner, the picture, the parameters and
samples, how to run it locally, how to run it on the cluster, the stages, the footer.
A pipeline directory offers two ways to run and no more — ONE sample by hand
(`commands.sh`) and every sample with Nextflow — and the page shows each, at each
locus, as three steps a person can follow without thinking: change directory, enter
the environment, run. Every run line comes from `pipeline_commands`, the ONE
spelling, so the page and `commands.sh` cannot disagree.

Honesty guarantees, made structural:
  • PURE — reads only the record. No clock, no disk, no network.
  • ESCAPED — every value passes through the shared escaper, in the HTML and inside
    the SVG; a command, a path or a description can never inject markup.
  • ABSENCE IS STATED — a None renders as "unrecorded" / "never observed" / "not
    requested" / a placeholder that says where the value will come from; it is never
    rounded up to a number or a path.
  • DETERMINISTIC — same record → same bytes. The picture is laid out arithmetically
    from label lengths: no layout library, no font metrics read.

Self-contained: the shared shell's CSS plus the picture's own (scoped inside the
<svg>), one inline SVG, and a few lines of inline JS that only toggle CSS classes on
hover — with JS off the page reads the same, minus the highlighting. Same shell,
banner and palette as the ENV report and the RUN dashboard, so the three pages are
one family. One public fn: render_pipeline_page; `picture_layout` is the picture's
geometry, exposed so a test can measure it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent.skills.env_report_html import _close_page, _e, _empty, _header_banner, _open_page
from agent.skills.pipeline_commands import enter_image, example_values, nextflow_run, sif_for
from agent.skills.pipeline_record import (DEFAULT_STAGE_REQUEST, PipelineRecord,
                                          PipelineStage, StageResources)

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
#: A placeholder's value kind, in words.
_VALUE_KIND = {"path": "a path", "prefix": "a prefix — a family of files named after it",
               "value": "a value"}
#: Where the Nextflow form records what ran.
_WHAT_RAN = ("What ran: <code>runs/&lt;timestamp&gt;/trace.txt</code> lists every task's command, "
             "<code>runs/&lt;timestamp&gt;/report.html</code> the resources, and <code>nextflow log</code> "
             "the launch line.")

# ── the picture: geometry ────────────────────────────────────────────────────
#
# Three columns. LEFT: the samplesheet columns and the shared params, as small nodes.
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


def _results_dir(record: PipelineRecord, stage: PipelineStage) -> str:
    """Where a stage's outputs land: one directory per row, shared by every stage of
    that row — the layout the sealed how-to ran in. A cohort stage runs once and a
    linear pipeline has one implicit row, so both publish flat."""
    if record.shape == "per_row" and stage.scope == "per_sample":
        return "results/<sample>/"
    return "results/"


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


def _place_label(x: float, y: float, w: float, anchor: str, obstacles: list[_Box]) -> tuple[float, float, _Box]:
    """The first of a few vertical nudges at which a label box clears every obstacle
    (nodes and labels already placed). Best effort past the last nudge."""
    left = x - w / 2 if anchor == "middle" else x - 4       # the drawn rect pads 4px each side
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

    # ── left column: samplesheet columns, then shared params ─────────────
    groups: list[tuple[str, list[_Node]]] = []
    col_by_ph: dict[str, _Node] = {}
    if record.samplesheet is not None:
        cols: list[_Node] = []
        for c in record.samplesheet.columns:
            n = _Node(id=f"c:{c.placeholder}", kind="column", name=c.placeholder,
                      line1=c.name, line2=f"{{{c.placeholder}}} · {c.value_kind}",
                      tooltip=f"samples.csv column {c.name} → {{{c.placeholder}}} · {c.value_kind}"
                              + (f" · {c.format}" if c.format else "")
                              + (f" — {c.description}" if c.description else ""))
            col_by_ph[c.placeholder] = n
            cols.append(n)
        groups.append(("samples.csv", cols))
    params: list[_Node] = []
    for p in record.params:
        tip = (f"{p.name} · {p.kind} · {p.value_kind} · source {p.source}"
               + (f" · default {p.default}" if p.default is not None else "")
               + f" — {p.reason}")
        if p.name in col_by_ph:
            col_by_ph[p.name].extra["data-param"] = p.name
            col_by_ph[p.name].tooltip += f" — {p.reason}"
            continue
        params.append(_Node(id=f"p:{p.name}", kind="param", name=p.name, line1=p.name,
                            line2=f"{p.kind} · {p.value_kind}", tooltip=tip))
    if params or not groups:
        groups.append(("params", params))
    left_nodes = [n for _, ns in groups for n in ns]
    left_by_name = {n.name: n for n in left_nodes}
    left_w = max([_node_w(n) for n in left_nodes]
                 + [_tw(g, _SUB_PT) for g, _ in groups]
                 + [_tw("params & samplesheet", _TITLE_PT)])
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
                  line2=f"{st.tool} · {st.scope}",
                  classes="unsized" if st.resources.requested_by == "default" else "",
                  extra={"data-index": str(st.index), "data-rank": str(rank[st.name])})
        r = st.resources
        req = ("unsized (default request)" if r.requested_by == "default"
               else f"request cpus {r.cpus} · mem {r.mem} · time {r.time} · gpus {r.gpus}")
        n.tooltip = (f"stage {st.index + 1} {st.name} · {st.tool} · {st.scope} · "
                     f"image {_short_digest(st.image_digest) or 'unrecorded'} · {req} · "
                     f"measured authority {r.measured_authority}")
        n.w, n.h = _node_w(n, 12.5), _STAGE_H
        stage_node[st.name] = n
    row_w = [sum(stage_node[s.name].w for s in row) + _STAGE_HGAP * (len(row) - 1) for row in rows]
    mid_w = max(row_w + [_tw("stages, in execution order", _TITLE_PT)])
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
    long_pairs = [(f, t) for (f, t) in pair_artifacts if rank[t] - rank[f] >= 2]
    long_label_w = max([_tw(", ".join(pair_artifacts[p]), _LABEL_PT) for p in long_pairs] + [0.0])
    bow_room = (22 + _BOW_STEP * (len(long_pairs) - 1) + long_label_w + 24) if long_pairs else 0.0
    gap_mr = max(_COL_GAP_MR, bow_room + 30)
    outs: list[_Node] = []
    for st in record.stages:
        for o in st.outputs:
            d = _results_dir(record, st)
            outs.append(_Node(id=f"o:{st.name}/{o.artifact}", kind="output", name=o.artifact,
                              line1=o.artifact, line2=d,
                              tooltip=f"{d}{o.artifact} · published by {st.name}"
                                      + (f" · declared as {o.declared_pattern}" if o.declared_pattern else "")
                                      + (f" · observed as {o.observed}" if o.observed else " · never observed"),
                              extra={"data-stage": st.name}))
    right_w = max([_node_w(n) for n in outs] + [_tw("published outputs", _TITLE_PT),
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
    titles = [(_MARGIN, "params & samplesheet"), (mid_x0, "stages, in execution order"),
              (right_x, "published outputs")]
    for tx, t in titles:
        texts.append(_Text(tx, _TITLE_Y - _TITLE_PT, _tw(t, _TITLE_PT), _TITLE_PT + 2, t, "title"))
    obstacles = [_Box(n.x, n.y, n.w, n.h) for n in nodes]

    # ── edges: param/column → stage ──────────────────────────────────────
    for st in record.stages:
        s = stage_node[st.name]
        ins = [i for i in st.inputs if i.origin in ("param", "column") and i.name in left_by_name]
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
        label = ", ".join(pair_artifacts[(f, t)])
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

_HOVER_JS = (
    '<script>(function(){var s=document.getElementById("pipeline-picture");if(!s)return;'
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


def _marker(mid: str, cls: str) -> str:
    return (f'<marker id="{mid}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" '
            f'orient="auto" markerUnits="userSpaceOnUse"><path class="{cls}" d="M0 0.5 L7.5 4 L0 7.5 Z"/></marker>')


def _svg(layout: _Layout, record: PipelineRecord) -> str:
    W, H = layout.width, layout.height
    P: list[str] = []
    P.append(f'<svg id="pipeline-picture" viewBox="0 0 {W:.0f} {H:.0f}" width="100%" '
             f'style="max-width:{W:.0f}px;height:auto;display:block" role="img" '
             f'aria-label="pipeline {_e(record.name)}: params and samplesheet columns feeding stages '
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
        left = e.lx - lw / 2 if e.anchor == "middle" else e.lx - 4
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
    """The three steps as a numbered list: each entry is its sentence (HTML, callers
    escape) and the lines to type, in one <pre>; a step with nothing to type shows
    the sentence alone."""
    items: list[str] = []
    for text, lines in steps:
        typed = "\n".join(lines)
        pre = f"<pre>{_e(typed)}</pre>" if lines else ""
        items.append(f"<li>{text}{pre}</li>")
    return "<ol>" + "".join(items) + "</ol>"


def _results_html(record: PipelineRecord) -> str:
    """Where a run's results land, as the record lays them out."""
    if record.shape != "per_row":
        return "Results land in <code>results/</code>."
    cohort = [s.name for s in sorted(record.stages, key=lambda s: s.index) if s.scope == "cohort"]
    out = "Results land in <code>results/&lt;sample&gt;/</code>, one directory per sample"
    if cohort:
        out += (f" (cohort stage{'s' if len(cohort) != 1 else ''} {_e(', '.join(cohort))} "
                "in <code>results/</code>)")
    return out + "."


def _nextflow_title(record: PipelineRecord) -> str:
    return ("Every sample in <code>samples.csv</code>, with Nextflow" if record.samplesheet is not None
            else "The one row, with Nextflow")


def _header(record: PipelineRecord) -> str:
    if record.shape == "per_row":
        n = len(record.samplesheet.rows) if record.samplesheet else 0
        shape = f"per_row — {n} example row{'s' if n != 1 else ''} in <code>samples.csv</code>"
        pill = f'<span class="pill na">per_row · {n} rows</span>'
    else:
        shape = "linear — runs once, params only"
        pill = '<span class="pill na">linear</span>'
    path = f"<code>{_e(record.sealed_workflow_path)}</code>" if record.sealed_workflow_path else _muted("path unrecorded")
    cluster = ""
    if record.compute_env:
        mods = " ".join(f"<code>{_e(m)}</code>" for m in record.modules)
        cluster = f"<b>{_e(record.compute_env)}</b> — " + (f"module load {mods}" if mods else "no modules to load")
    rows = [
        ("Rendered from", f"sealed workflow <b>{_e(record.sealed_workflow)}</b> — {path}"),
        ("Sealed workflow sha256", _code(record.sealed_workflow_sha256)),
        ("Created", _e(record.created_at)),
        ("Env image digest(s)", "<br>".join(_digest_cell(d) for d in record.env_digests) or _muted("unrecorded")),
        ("Shape", shape),
        ("Stages", f"{len(record.stages)} in execution order: "
                   + " → ".join(f"<code>{_e(s.name)}</code>" for s in sorted(record.stages, key=lambda s: s.index))),
        ("Output slots", " ".join(f"<code>{{{_e(s)}}}</code>" for s in record.output_slots) or _muted("none")),
        ("Cluster", cluster),
    ]
    return _header_banner(f"Pipeline — {_e(record.name)}", pill, rows)


def _picture_section(record: PipelineRecord) -> str:
    layout = picture_layout(record)
    legend = ('<p class="note">Thin grey lines: a param or samplesheet column feeding a stage. Cyan lines: '
              'an artifact flowing from the stage that writes it to a stage that reads it, labelled with '
              'its name. Yellow lines: what a stage publishes. A dashed stage box is <b>unsized</b> (default '
              'request). Stages on one row have no edge between them and may run side by side; a row is '
              'one rank (longest path from a source stage). Hover or focus a node to trace it.</p>')
    return _section("picture", "The picture",
                    "params and samplesheet columns → stages in execution order → published outputs",
                    _svg(layout, record) + _HOVER_JS + legend)


def _what_it_is(value_kind: str, description: Optional[str], source: str) -> str:
    bits = [_VALUE_KIND.get(value_kind, value_kind)]
    if description:
        bits.append(description)
    if source.startswith("sealed_step:"):
        bits.append(f"produced by sealed step {source.split(':', 1)[1]}")
    return _e(" · ".join(bits))


def _params_section(record: PipelineRecord) -> str:
    sheet = record.samplesheet
    col_of = {c.placeholder: c for c in sheet.columns} if sheet is not None else {}
    values = example_values(record)
    rows: list[list[str]] = []
    for p in record.params:
        if p.kind == "shared":
            scope = "shared"
        elif p.name in col_of:
            scope = f"per sample (column <code>{_e(col_of[p.name].name)}</code>)"
        else:
            scope = "per sample"
        rows.append([f"<code>{_e(p.name)}</code>", scope, _code(values.get(p.name), "none"),
                     _what_it_is(p.value_kind, p.description, p.source)])
    named = {p.name for p in record.params}
    for c in (sheet.columns if sheet is not None else []):
        if c.placeholder in named:
            continue
        rows.append([f"<code>{_e(c.placeholder)}</code>", f"per sample (column <code>{_e(c.name)}</code>)",
                     _code(values.get(c.placeholder), "none"), _what_it_is(c.value_kind, c.description, "")])
    P: list[str] = []
    if rows:
        P.append(_table(["Placeholder", "Scope", "Example value", "What it is"], rows))
    else:
        P.append(_empty("the how-to takes no input placeholders — output slots only"))
    if sheet is not None:
        names = [c.name for c in sheet.columns]
        P.append('<p class="note"><code>samples.csv</code> — one row per sample, columns: '
                 + ", ".join(f"<code>{_e(n)}</code>" for n in names)
                 + '. <b>The example rows are the seal\'s own trials — replace them with your samples.</b></p>')
        P.append(_table(names, [[_e(r.get(c, "")) for c in names] for r in sheet.rows]))
    else:
        P.append('<p class="note">one implicit row; the per-sample values are set at the top of '
                 '<code>commands.sh</code> (by hand) and in <code>params.yaml</code> (Nextflow).</p>')
    return _section("params", "Parameters and samples",
                    "every placeholder of the how-to, with the sealed run's example value", "".join(P))


def _menu(record: PipelineRecord, into: str, cd: str, enter: str, enter_lines: list[str],
          nothing: str, run: str, run_lines: list[str], tail: str = "") -> str:
    """The two items on the menu at one locus — ONE sample by hand, every sample with
    Nextflow — each as three steps: change directory, enter the environment, run."""
    where = _results_html(record)
    by_hand = _steps([(into, [cd]), (enter, enter_lines),
                      ("Edit the values at the top of <code>commands.sh</code>, then run it.", ["bash commands.sh"])])
    nextflow = _steps([(into, [cd]), (nothing, []), (run, run_lines)])
    return (f'<h3 class="sub">A. One sample by hand</h3>{by_hand}<p class="note">{where}</p>'
            f'<h3 class="sub">B. {_nextflow_title(record)}</h3>{nextflow}'
            f'<p class="note">{where} {_WHAT_RAN}{tail}</p>')


def _run_local_section(record: PipelineRecord) -> str:
    body = _menu(
        record, "Change into the copy of this directory next to your data.", f"cd /path/to/{record.name}",
        "Enter the image. Docker must see every directory your values live in — add a <code>-v</code> for each.",
        enter_image(record, "local"),
        "Nothing to enter: docker and nextflow on this machine.",
        "Run. <code>-resume</code> re-runs only the stages whose inputs or parameters changed.",
        nextflow_run(record, "local"))
    return _section("run-local", "Run it locally", "one sample by hand, or every sample with Nextflow", body)


def _run_hpc_section(record: PipelineRecord) -> str:
    env, sif = record.compute_env, sif_for(record)
    key = next((s.request_key for s in sorted(record.stages, key=lambda s: s.index) if s.request_key), None)
    env_arg = f'"{env}"' if env else "<the cluster's env>"
    key_arg = f'"{key}"' if key else "<the env's freeze_request_key>"
    call = _e(f"stage_apptainer_image(project=<your project>, env={env_arg}, freeze_request_key={key_arg})")
    step0 = f'<p class="note"><b>0.</b> The image as a <code>.sif</code>: <code>{call}</code> puts it '
    if sif:
        step0 += f'at <code>{_e(sif)}</code>.</p>'
    else:
        step0 += ('in the cluster\'s container zone. The path is filled in here when the pipeline is '
                  'rendered with <code>env=</code> naming the cluster; until then <code>sif:</code> in '
                  '<code>params.yaml</code> must be set by hand.</p>')
    opening = ("" if env else
               '<p class="warn-note">Rendered without a cluster named: no module line below, and the '
               '<code>.sif</code> path is a placeholder.</p>')
    body = _menu(
        record, "Change into the copy of this directory in your project directory on the cluster.",
        f"cd /path/in/your/project/{record.name}",
        ("Load the modules and enter the image. " if record.modules else "Enter the image. ")
        + "Apptainer must see every directory your values live in — add each to <code>--bind</code>.",
        enter_image(record, "hpc"),
        "Nothing to enter: <code>launcher.sh</code> loads the modules.",
        "Submit.", nextflow_run(record, "hpc"),
        tail=" Watch it with <code>squeue -u $USER</code>, or <code>sacct -j &lt;jobid&gt;</code> once it has ended.")
    return _section("run-hpc", "Run it on the cluster", "the same two ways, through the .sif and SLURM",
                    opening + step0 + body)


def _image_cell(st: PipelineStage) -> str:
    if not st.image and not st.image_digest:
        return _muted("unrecorded")
    tag = f"<code>{_e(st.image)}</code>" if st.image else _muted("tag unrecorded")
    short = (f' <span class="muted" title="{_e(st.image_digest)}">{_e(_short_digest(st.image_digest))}</span>'
             if st.image_digest else "")
    return tag + short


def _request_html(r: StageResources) -> str:
    if r.requested_by == "default":
        d = DEFAULT_STAGE_REQUEST
        cpus = d.get("cpus")
        gpus = f" · {r.gpus} gpu{'s' if r.gpus != 1 else ''}" if r.gpus else ""
        return (f'<span class="warn">unsized</span> <span class="muted">(DEFAULT {_e(d.get("time"))} · '
                f'{_e(d.get("mem"))} · {_e(cpus)} cpu{"s" if cpus != 1 else ""}{_e(gpus)})</span>')
    parts = [f"cpus {r.cpus}" if r.cpus is not None else "cpus not requested",
             f"mem {r.mem}" if r.mem else "mem not requested",
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


def _stages_section(record: PipelineRecord) -> str:
    if not record.stages:
        return _section("stages", "Stages", "", _empty("the record holds no stages"))
    stages = sorted(record.stages, key=lambda s: s.index)
    rows = [[f"<code>{_e(st.name)}</code>", _e(st.tool), _e(", ".join(str(t + 1) for t in st.templates)),
             _image_cell(st), _request_html(st.resources), _measured_html(st.resources)] for st in stages]
    body = _table(["Stage", "Tool", "How-to command", "Image", "Request", "Measured"], rows)
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
        _params_section(record),
        _run_local_section(record),
        _run_hpc_section(record),
        _stages_section(record),
        _close_page(f'<p class="gen">{_e(FOOTER)}</p>'),
    ]
    return "\n".join(P)


__all__ = ["render_pipeline_page", "picture_layout", "FOOTER"]
