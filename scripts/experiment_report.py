#!/usr/bin/env python3
"""experiment_report — the rows and the scoreboard, as one page a reader can compare on.

Reads what experiment_metrics produces and writes `report.html` (plus `metrics.csv` and
`metrics.json`) with, in order: the experiment setup (every knob a run was given, the prompt
verbatim, the isolation record the runner wrote, the code and client it ran on — the record a
reader needs to tell a change in the numbers from a change in the conditions); the scoreboard
per experiment × model; the same experiment across code revisions (the view that says whether
a change to the system helped); the cost-vs-success chart (one point per group — the
cost-accuracy view leaderboards publish, where a model that is a little better for a lot more
money is visible as such); bars per model for the four efficiency numbers (cost, output
tokens, tool calls, wall time) with the spread across repeats; the MCP outcome mix per group;
every run as a row; what tripped the agent (every non-proven outcome code, by group); and the
metric glossary. Inline SVG, no plotting dependency, the same theme tokens as the other pages.
"""
from __future__ import annotations

import html
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiment_metrics import (METRIC_COLUMNS, METRIC_NAMES, OUTCOME_CLASSES, aggregate,
                                rows_to_csv)

_CSS = """
:root, :root[data-theme="cyber"]{
  --bg:#0a0c14;--surface:#13151f;--surface-2:#1a1d29;--border:#262a3a;
  --cyan:#22e3ee;--cyan-soft:rgba(34,227,238,.16);
  --yellow:#fff200;--title:#fff200;--link:#fff200;
  --ink:#e6e9f0;--muted:#8e98ad;
  --ok:#3ce086;--ok-bg:rgba(60,224,134,.14);--bad:#ff4b6e;--bad-bg:rgba(255,75,110,.14);
  --warn:#ffb02e;--code-bg:#0e1019;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
:root[data-theme="light"]{
  --bg:#f4f6f9;--surface:#ffffff;--surface-2:#eef2f7;--border:#d5dce6;
  --cyan:#0b5fb4;--cyan-soft:rgba(11,95,180,.10);
  --yellow:#1f3a5f;--title:#14213d;--link:#0b5fb4;
  --ink:#1a2233;--muted:#5b6675;
  --ok:#1a7f4b;--ok-bg:rgba(26,127,75,.12);--bad:#c0304a;--bad-bg:rgba(192,48,74,.10);
  --warn:#9a6700;--code-bg:#eef2f7;
}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans);padding:24px 16px 64px}
main{max-width:1180px;margin:0 auto}
h1{font:600 24px/1.2 var(--sans);color:var(--title);margin:0 0 4px}
h2{font:600 17px/1.3 var(--sans);color:var(--cyan);margin:36px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.sub{color:var(--muted);margin:0 0 18px}
.toggle{float:right;background:var(--surface);border:1px solid var(--border);color:var(--muted);
  font:600 11px/1 var(--mono);padding:6px 10px;border-radius:6px;cursor:pointer}
table{border-collapse:collapse;width:100%;font-size:13px;background:var(--surface);border:1px solid var(--border)}
th,td{padding:6px 9px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;background:var(--surface-2)}
th:first-child,td:first-child,th.l,td.l{text-align:left}
td.ok{color:var(--ok)} td.bad{color:var(--bad)} td.na{color:var(--muted)}
code{font:12px var(--mono);background:var(--code-bg);padding:1px 5px;border-radius:4px}
.note{color:var(--muted);font-size:13px;margin:6px 0 0}
.setup{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:4px 16px 12px;margin:12px 0}
.setup h3{font:600 14px/1.3 var(--sans);color:var(--ink);margin:12px 0 6px}
.setup table{border:0;background:transparent}
.setup td{white-space:normal;vertical-align:top}
.setup td.k{color:var(--muted);width:150px;text-align:left}
.setup pre{margin:0;white-space:pre-wrap;font:12.5px/1.45 var(--mono);background:var(--code-bg);padding:8px 10px;border-radius:6px}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.fig{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px}
.fig h3{font:600 13px/1.3 var(--sans);color:var(--ink);margin:0 0 6px}
.fig svg{width:100%;height:auto;display:block}
svg text{font:11px var(--mono);fill:var(--muted)}
svg .lbl{fill:var(--ink)} svg .axis{stroke:var(--border)} svg .tick{stroke:var(--border);stroke-dasharray:2 3}
svg .pt{stroke:var(--bg);stroke-width:1}
details{margin:8px 0} summary{cursor:pointer;color:var(--muted)}
.wrap{overflow-x:auto}
.final{white-space:pre-wrap;font:12px var(--mono);color:var(--muted);max-width:70ch}
@media (max-width:700px){body{padding:16px 16px 48px} .figs{grid-template-columns:1fr}}
"""

_PALETTE = ("#22e3ee", "#fff200", "#3ce086", "#ff4b6e", "#b388ff", "#ffb02e", "#4fc3f7", "#f48fb1")


def _e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _fmt(v, kind: str = "num") -> str:
    if v is None:
        return "—"
    if kind == "usd":
        return f"${v:,.3f}" if v < 10 else f"${v:,.2f}"
    if kind == "pct":
        return f"{100 * v:.0f}%"
    if kind == "ms":
        s = v / 1000
        return f"{s:.0f}s" if s < 120 else f"{s / 60:.1f}m"
    if kind == "k":
        return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


# ---------------------------------------------------------------------------
# SVG figures
# ---------------------------------------------------------------------------

def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def _bars(groups: list[dict], key: str, kind: str, title: str) -> str:
    """One bar per (experiment, model), grouped by experiment, with the sd as a whisker."""
    if not groups:
        return ""
    W, H, L, B, T = 520, 220, 48, 46, 12
    vals = [(g[f"{key}_mean"] or 0.0) for g in groups]
    sds = [(g[f"{key}_sd"] or 0.0) for g in groups]
    top = _nice_max(max(v + s for v, s in zip(vals, sds)) or 1.0)
    n = len(groups)
    slot = (W - L - 10) / n
    bw = min(44, slot * 0.7)
    exps = sorted({g["experiment"] for g in groups})
    colour = {e: _PALETTE[i % len(_PALETTE)] for i, e in enumerate(exps)}
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_e(title)}">']
    for i in range(5):
        y = T + (H - T - B) * (1 - i / 4)
        out.append(f'<line class="tick" x1="{L}" x2="{W - 6}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{_e(_fmt(top * i / 4, kind))}</text>')
    for i, g in enumerate(groups):
        v, s = vals[i], sds[i]
        x = L + slot * i + (slot - bw) / 2
        h = (H - T - B) * (v / top) if top else 0
        y = H - B - h
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{colour[g["experiment"]]}" opacity=".85">'
                   f'<title>{_e(g["experiment"])} · {_e(g["model"])}: {_e(_fmt(v, kind))} ± {_e(_fmt(s, kind))}</title></rect>')
        if s:
            cx = x + bw / 2
            y1 = H - B - (H - T - B) * min((v + s) / top, 1)
            y2 = H - B - (H - T - B) * max((v - s) / top, 0)
            out.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y1:.1f}" y2="{y2:.1f}" stroke="var(--ink)" opacity=".6"/>')
        out.append(f'<text class="lbl" x="{x + bw / 2:.1f}" y="{H - B + 14}" text-anchor="middle">{_e(g["model"])}</text>')
        if len(exps) > 1:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{H - B + 27}" text-anchor="middle" font-size="9.5">{_e(g["experiment"][:14])}</text>')
    out.append(f'<line class="axis" x1="{L}" x2="{W - 6}" y1="{H - B}" y2="{H - B}"/></svg>')
    return "\n".join(out)


def _scatter(groups: list[dict]) -> str:
    """Mean cost against pass@1, one point per group: the cost-accuracy view."""
    judged = [g for g in groups if g["pass_at_1"] is not None]
    if not judged:
        return '<p class="note">No judged runs yet.</p>'
    W, H, L, B, T, R = 520, 260, 48, 40, 14, 24
    xmax = _nice_max(max(g["cost_usd_mean"] or 0 for g in judged) or 1.0) * 1.15
    exps = sorted({g["experiment"] for g in judged})
    colour = {e: _PALETTE[i % len(_PALETTE)] for i, e in enumerate(exps)}
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="cost against success rate">']
    for i in range(5):
        y = T + (H - T - B) * (1 - i / 4)
        out.append(f'<line class="tick" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{25 * i}%</text>')
        x = L + (W - L - R) * i / 4
        out.append(f'<text x="{x:.1f}" y="{H - B + 16}" text-anchor="middle">{_e(_fmt(xmax * i / 4, "usd"))}</text>')
    out.append(f'<line class="axis" x1="{L}" x2="{W - R}" y1="{H - B}" y2="{H - B}"/>'
               f'<line class="axis" x1="{L}" x2="{L}" y1="{T}" y2="{H - B}"/>'
               f'<text x="{(L + W - R) / 2:.1f}" y="{H - 6}" text-anchor="middle">mean cost per run</text>')
    for g in judged:
        x = L + (W - L - R) * ((g["cost_usd_mean"] or 0) / xmax)
        y = T + (H - T - B) * (1 - g["pass_at_1"])
        r = 5 + 2 * math.sqrt(g["n"])
        # a label to the right of a point near the right edge would leave the panel
        flip = x + r + 4 + 6.6 * len(g["model"]) > W - R
        lx, anchor = (x - r - 4, "end") if flip else (x + r + 4, "start")
        out.append(f'<circle class="pt" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{colour[g["experiment"]]}" opacity=".9">'
                   f'<title>{_e(g["experiment"])} · {_e(g["model"])}: pass@1 {_e(_fmt(g["pass_at_1"], "pct"))}, '
                   f'{_e(_fmt(g["cost_usd_mean"], "usd"))}/run, n={g["n"]}</title></circle>'
                   f'<text class="lbl" x="{lx:.1f}" y="{y + 4:.1f}" text-anchor="{anchor}">{_e(g["model"])}</text>')
    if len(exps) > 1:
        for i, e in enumerate(exps):
            out.append(f'<rect x="{L + 4 + 120 * i}" y="{T}" width="9" height="9" fill="{colour[e]}"/>'
                       f'<text x="{L + 17 + 120 * i}" y="{T + 9}">{_e(e[:16])}</text>')
    out.append("</svg>")
    return "\n".join(out)


_OUTCOME_COLOUR = {"proven": "var(--ok)", "degraded": "var(--warn)", "refused": "var(--cyan)",
                   "broke": "var(--bad)", "loop": "#b388ff", "vanished": "#777", "unstated": "var(--muted)"}


def _stacked_outcomes(groups: list[dict]) -> str:
    """The MCP outcome mix per group, as a share of that group's bioinf calls."""
    if not groups:
        return ""
    classes = OUTCOME_CLASSES + ("unstated",)
    labels = [f'{g["model"]} · {g["experiment"]}' for g in groups]
    W, rowh = 520, 22
    L = min(300, 12 + int(6.6 * max(len(s) for s in labels)))      # 11px mono ≈ 6.6px a glyph
    # the legend: one entry per class, laid out left to right, wrapped when it would leave the panel
    legend, lx, ly = [], 8, 13
    for c in classes:
        w = 16 + int(6.6 * len(c)) + 10
        if lx + w > W - 4:
            lx, ly = 8, ly + 15
        legend.append(f'<rect x="{lx}" y="{ly - 9}" width="9" height="9" fill="{_OUTCOME_COLOUR[c]}"/>'
                      f'<text x="{lx + 13}" y="{ly}">{c}</text>')
        lx += w
    top = ly + 10
    H = top + rowh * len(groups) + 4
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="MCP outcomes per group">', *legend]
    for r, g in enumerate(groups):
        y = top + rowh * r
        total = sum(g[f"mcp_{c}_mean"] or 0 for c in classes) or 1.0
        x = L
        out.append(f'<text class="lbl" x="{L - 6}" y="{y + 14}" text-anchor="end">{_e(labels[r])}</text>')
        for c in classes:
            v = g[f"mcp_{c}_mean"] or 0
            w = (W - L - 8) * v / total
            if w > 0:
                out.append(f'<rect x="{x:.1f}" y="{y + 2}" width="{w:.1f}" height="{rowh - 6}" fill="{_OUTCOME_COLOUR[c]}" opacity=".85">'
                           f'<title>{_e(c)}: {v:.1f} calls per run</title></rect>')
                x += w
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def _scoreboard(groups: list[dict]) -> str:
    head = ("experiment", "model", "n", "pass@1", "pass^k", "cost/run", "cost of pass", "in (uncached)",
            "cache read", "cache write", "out", "peak ctx", "api calls", "tool calls", "mcp", "search",
            "refused", "broke", "wall")
    rows = []
    for g in groups:
        p = g["pass_at_1"]
        cls = "na" if p is None else ("ok" if p >= 0.999 else ("bad" if p == 0 else ""))
        rows.append("<tr>" + "".join([
            f'<td class="l">{_e(g["experiment"])} <code>{_e(g["tier"])}</code></td>',
            f'<td class="l">{_e(g["model"])}<br><span class="note">{_e(g["model_id"])}</span></td>',
            f'<td>{g["n"]}</td>',
            f'<td class="{cls}">{_e(_fmt(p, "pct"))}</td>',
            f'<td class="{cls}">{_e(_fmt(g["pass_pow_k"], "pct"))}<span class="note"> k={g["k"]}</span></td>',
            f'<td>{_e(_fmt(g["cost_usd_mean"], "usd"))}</td>',
            f'<td>{_e(_fmt(g["cost_of_pass"], "usd"))}</td>',
            f'<td>{_e(_fmt(g["input_tokens_mean"], "k"))}</td>',
            f'<td>{_e(_fmt(g["cache_read_tokens_mean"], "k"))}</td>',
            f'<td>{_e(_fmt(g["cache_creation_tokens_mean"], "k"))}</td>',
            f'<td>{_e(_fmt(g["output_tokens_mean"], "k"))}</td>',
            f'<td>{_e(_fmt(g["context_peak_mean"], "k"))}</td>',
            f'<td>{_e(_fmt(g["api_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["tool_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["mcp_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["tool_search_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["mcp_refused_mean"]))}</td>',
            f'<td>{_e(_fmt(g["mcp_broke_mean"]))}</td>',
            f'<td>{_e(_fmt(g["wall_ms_mean"], "ms"))}</td>',
        ]) + "</tr>")
    return ('<div class="wrap"><table><thead><tr>' + "".join(f"<th>{_e(h)}</th>" for h in head)
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def _ladder(r: dict) -> str:
    steps = (("frozen", "F"), ("sealed", "S"), ("usage_verified", "V"), ("env_report", "E"),
             ("run_report", "R"), ("pipeline_rendered", "P"))
    return " ".join(f'<span title="{_e(k)}" style="color:var(--{"ok" if r.get(k) else "muted"})">{m}</span>'
                    for k, m in steps)


def _runs_table(rows: list[dict]) -> str:
    head = ("run", "model", "rev", "success", "ladder", "ended", "cost", "in (uncached)", "cache read", "cache write",
            "out", "peak ctx", "api", "tools", "mcp", "search", "repeat", "err", "deny", "refused", "broke", "wall")
    body = []
    for r in rows:
        s = r["success"]
        cls = "na" if s is None else ("ok" if s else "bad")
        body.append("<tr>" + "".join([
            f'<td class="l"><code title="{_e(r["run_id"])}">{_e(r["experiment"])} #{_e(r["repeat"])}</code></td>',
            f'<td class="l">{_e(r["model"])}</td>',
            f'<td class="l"><code>{_e((r.get("code_rev") or "?")[:9])}</code>{"*" if r.get("code_dirty") else ""}</td>',
            f'<td class="{cls}">{"—" if s is None else ("yes" if s else "no")}</td>',
            f'<td class="l">{_ladder(r)}</td>',
            f'<td class="l">{_e(r["terminal_reason"])}{" · timed out" if r.get("timed_out") else ""}</td>',
            f'<td>{_e(_fmt(r["cost_usd"], "usd"))}</td>',
            f'<td>{_e(_fmt(r["input_tokens"], "k"))}</td>',
            f'<td>{_e(_fmt(r["cache_read_tokens"], "k"))}</td>',
            f'<td>{_e(_fmt(r["cache_creation_tokens"], "k"))}</td>',
            f'<td>{_e(_fmt(r["output_tokens"], "k"))}</td>',
            f'<td>{_e(_fmt(r["context_peak"], "k"))}</td>',
            f'<td>{r["api_calls"]}</td>', f'<td>{r["tool_calls"]}</td>', f'<td>{r["mcp_calls"]}</td>',
            f'<td>{r["tool_search_calls"]}</td>', f'<td>{r["repeated_calls"]}</td>',
            f'<td>{r["tool_errors"]}</td>', f'<td>{r["permission_denials"]}</td>',
            f'<td>{r["mcp_refused"]}</td>', f'<td>{r["mcp_broke"]}</td>',
            f'<td>{_e(_fmt(r["wall_ms"], "ms"))}</td>',
        ]) + "</tr>")
        detail = []
        if r.get("mcp_by_tool"):
            detail.append("tools: " + ", ".join(f"{k}×{v}" for k, v in sorted(r["mcp_by_tool"].items(), key=lambda kv: -kv[1])))
        if r.get("outcome_codes"):
            detail.append("codes: " + ", ".join(f"{k}×{v}" for k, v in sorted(r["outcome_codes"].items())))
        if r.get("sealed_names"):
            detail.append("sealed: " + ", ".join(r["sealed_names"]))
        if r.get("final_text"):
            detail.append("said: " + r["final_text"][:600])
        if detail:
            body.append(f'<tr><td colspan="{len(head)}" class="l"><details><summary>detail</summary>'
                        f'<div class="final">{_e(chr(10).join(detail))}</div></details></td></tr>')
    return ('<div class="wrap"><table><thead><tr>' + "".join(f"<th>{_e(h)}</th>" for h in head)
            + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
            '<p class="note">ladder: F frozen · S sealed · V usage verified · E ENV report · R RUN report · P pipeline rendered. '
            'rev*: the checkout had uncommitted changes.</p>')


def _tripped_table(groups: list[dict]) -> str:
    """Every outcome code the server answered with that was not `proven`, per group — the
    catalogue of what the agent ran into — and, under it, the per-run signs of a model
    working around the server: shell fallbacks, ToolSearch calls, denials, errors."""
    classes: dict[str, str] = {}
    for g in groups:
        classes.update(g.get("code_classes") or {})
    codes = sorted(c for c, cls in classes.items() if cls != "proven")
    parts = []
    if not codes:
        parts.append('<p class="note">No refusals or breakages.</p>')
    else:
        head = "".join(f"<th>{_e(g['model'])}<br><span class='note'>{_e(g['experiment'][:14])}</span></th>" for g in groups)
        body = "".join("<tr><td class='l'><code>" + _e(c) + "</code> <span class='note'>" + _e(classes[c]) + "</span></td>"
                       + "".join(f"<td>{(g.get('outcome_codes') or {}).get(c, 0) or '·'}</td>" for g in groups) + "</tr>"
                       for c in codes)
        parts.append(f'<div class="wrap"><table><thead><tr><th>outcome code</th>{head}</tr></thead>'
                     f'<tbody>{body}</tbody></table></div>')
    head2 = ("group", "n", "shell / own-tool calls", "ToolSearch calls", "repeated calls", "tool errors", "denials",
             "refused", "broke")
    body2 = "".join("<tr>" + "".join([
        f"<td class='l'>{_e(g['experiment'])} · {_e(g['model'])}</td>", f"<td>{g['n']}</td>",
        f"<td>{_e(_fmt(g['other_tool_calls_mean']))}</td>", f"<td>{_e(_fmt(g['tool_search_calls_mean']))}</td>",
        f"<td>{_e(_fmt(g['repeated_calls_mean']))}</td>", f"<td>{_e(_fmt(g['tool_errors_mean']))}</td>",
        f"<td>{_e(_fmt(g['permission_denials_mean']))}</td>", f"<td>{_e(_fmt(g['mcp_refused_mean']))}</td>",
        f"<td>{_e(_fmt(g['mcp_broke_mean']))}</td>"]) + "</tr>" for g in groups)
    parts.append('<div class="wrap"><table><thead><tr>' + "".join(f"<th>{_e(h)}</th>" for h in head2)
                 + f"</tr></thead><tbody>{body2}</tbody></table></div>"
                 "<p class='note'>Means per run. Shell and own-tool calls are what the agent did outside the server; "
                 "ToolSearch calls are the cost of finding the deferred menu.</p>")
    return "\n".join(parts)


def _revisions_table(rows: list[dict]) -> str:
    """One row per (experiment, code revision): the same prompt put to the system as it
    changed. The numbers that move when the system improves: pass@1, cost, the context
    re-read per call (cache read), tool calls, ToolSearch calls, shell fallbacks, refusals
    and breakages."""
    groups = aggregate(rows, by=("experiment", "code_rev"))
    head = ("experiment", "code rev", "runs", "models", "pass@1", "cost/run", "cache read", "peak ctx", "api calls",
            "tool calls", "mcp", "search", "shell", "refused", "broke", "wall")
    body = []
    for g in groups:
        p = g["pass_at_1"]
        cls = "na" if p is None else ("ok" if p >= 0.999 else ("bad" if p == 0 else ""))
        body.append("<tr>" + "".join([
            f'<td class="l">{_e(g["experiment"])}</td>',
            f'<td class="l"><code>{_e((g["code_rev"] or "?")[:9])}</code>{" + uncommitted" if g["code_dirty"] else ""}</td>',
            f'<td>{g["n"]}</td>',
            f'<td class="l">{_e(", ".join(g["models"]))}</td>',
            f'<td class="{cls}">{_e(_fmt(p, "pct"))}</td>',
            f'<td>{_e(_fmt(g["cost_usd_mean"], "usd"))}</td>',
            f'<td>{_e(_fmt(g["cache_read_tokens_mean"], "k"))}</td>',
            f'<td>{_e(_fmt(g["context_peak_mean"], "k"))}</td>',
            f'<td>{_e(_fmt(g["api_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["tool_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["mcp_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["tool_search_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["other_tool_calls_mean"]))}</td>',
            f'<td>{_e(_fmt(g["mcp_refused_mean"]))}</td>',
            f'<td>{_e(_fmt(g["mcp_broke_mean"]))}</td>',
            f'<td>{_e(_fmt(g["wall_ms_mean"], "ms"))}</td>',
        ]) + "</tr>")
    return ('<div class="wrap"><table><thead><tr>' + "".join(f"<th>{_e(h)}</th>" for h in head)
            + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
            "<p class='note'>Means over every run of the experiment at that revision, all models together. A revision "
            "marked <i>+ uncommitted</i> had changes beyond the commit, so two runs at it may not have run the same code.</p>")


def _glossary() -> str:
    body = "".join(f"<tr><td class='l'><code>{_e(k)}</code></td><td class='l'>{_e(d)}</td></tr>" for k, d in METRIC_COLUMNS)
    extra = (("pass@1", "share of judged runs that met the success rule"),
             ("pass^k", "τ-bench reliability: the chance that all k repeats succeed, C(c,k)/C(n,k) with k = n here"),
             ("cost of pass", "expected dollars per success: mean cost ÷ pass@1"),
             ("peak ctx", "the largest single request the model saw — how close a run came to the context limit"),
             ("in (uncached)", "input_tokens: what was sent fresh, outside the prompt cache"),
             ("cache write", "cache_creation_tokens: what was added to the prompt cache"))
    body += "".join(f"<tr><td class='l'><code>{_e(k)}</code></td><td class='l'>{_e(d)}</td></tr>" for k, d in extra)
    return f'<details><summary>metric glossary</summary><div class="wrap"><table><tbody>{body}</tbody></table></div></details>'


# ---------------------------------------------------------------------------
# The setup
# ---------------------------------------------------------------------------

#: The fields of a run's experiment.json that define its conditions. Two runs of one
#: experiment that differ in any of these were not run under the same conditions, and the
#: setup section says so rather than averaging them quietly.
CONDITION_FIELDS = ("prompt", "tier", "success", "expected_codes", "isolation", "budget_usd", "timeout_s", "effort",
                    "allowed_tools", "disallowed_tools", "code_rev", "code_dirty")


def conditions_of(meta: dict) -> dict:
    return {k: meta.get(k) for k in CONDITION_FIELDS}


def _command_line(meta: dict) -> str:
    """The client command with the prompt and the model elided — the part every run of the
    experiment shares."""
    cmd = list(meta.get("command") or [])
    out = []
    for i, c in enumerate(cmd):
        if c == meta.get("prompt"):
            out.append("<prompt>")
        elif i > 0 and cmd[i - 1] == "--model":
            out.append("<model>")
        else:
            out.append(c)
    return " ".join(out)


_SEAM_WORDS = {
    "workspace": {"fresh": "an empty workspace per run"},
    "envs": {"fresh": "an empty envs directory per run", "host": "the host's envs directory"},
    "resources": {"host": "the host's test-data corpus", "fresh": "an empty resources directory (the run fetches its own data)"},
}


def _isolation_cell(iso: dict | None) -> str:
    """The isolation record a run was written with, one seam per line. A run from before
    the record existed says so instead of being described from memory."""
    if not isinstance(iso, dict):
        return "<i>not recorded for this run</i>"
    lines = []
    for seam in ("workspace", "envs", "resources"):
        v = iso.get(seam)
        lines.append(f"<b>{_e(seam)}</b>: {_e(_SEAM_WORDS.get(seam, {}).get(v, v))}")
    pa = iso.get("projects_access") or {}
    kind = pa.get("kind") if isinstance(pa, dict) else pa
    if kind == "prepared":
        lines.append(f"<b>compute access</b>: a prepared projects_access.yaml copied into the run from "
                     f"<code>{_e(Path(str(pa.get('template', ''))).name)}</code>, declaring compute env(s) "
                     f"<code>{_e(', '.join(pa.get('compute_envs') or []) or '—')}</code>"
                     + (f" and project(s) <code>{_e(', '.join(pa['projects']))}</code>" if pa.get("projects") else ""))
    elif kind == "host":
        lines.append("<b>compute access</b>: the host's projects_access.yaml")
    else:
        lines.append("<b>compute access</b>: none — no projects_access.yaml in the run, so no compute env is "
                     "declared and the compute tools refuse")
    lines.append(f"<b>Docker</b>: {_e(iso.get('docker'))}")
    lines.append(f"<b>auto-memory</b>: {_e(iso.get('memory'))}")
    return "<br>".join(lines)


def _setup_one(name: str, metas: list[dict], rows: list[dict]) -> str:
    """One experiment's conditions, from its runs' experiment.json records. `metas` is
    every run's record for this experiment, newest last; the section shows the latest and
    names the fields that varied across runs."""
    m = metas[-1]
    rs = [r for r in rows if r["experiment"] == name]
    by_model: dict[str, int] = {}
    for r in rs:
        key = r.get("model_id") or r["model"]
        by_model[key] = by_model.get(key, 0) + 1
    versions = sorted({r.get("claude_code_version", "") for r in rs if r.get("claude_code_version")})
    varied = sorted({k for mm in metas for k in CONDITION_FIELDS if conditions_of(mm)[k] != conditions_of(m)[k]})
    expected = m.get("expected_codes") or []
    kv = [
        ("prompt", f"<pre>{_e((m.get('prompt') or '').rstrip())}</pre>"),
        ("tier · judged by", f"<code>{_e(m.get('tier'))}</code> · the <code>{_e(m.get('success'))}</code> rule"
                             + (f", expecting a refusal coded <code>{_e(', '.join(expected))}</code>" if expected else "")),
        ("models × repeats", ", ".join(f"<code>{_e(k)}</code> ×{v}" for k, v in sorted(by_model.items())) or "—"),
        ("tools", f"the agent may call <code>{_e(', '.join(m.get('allowed_tools') or []) or 'nothing')}</code> "
                  f"without asking"
                  + (f"; it may not call <code>{_e(', '.join(m.get('disallowed_tools')))}</code>"
                     if m.get("disallowed_tools") else "")
                  + (f"; effort <code>{_e(m.get('effort'))}</code>" if m.get("effort") else "")
                  + ". Claude Code also runs its own read-only shell commands without asking; "
                    "the runs table counts those under <i>tools</i>."),
        ("isolation", _isolation_cell(m.get("isolation"))),
        ("shared, unavoidably", "the network, and the Docker daemon's layer cache — a base image pulled by an earlier run "
                                "is not pulled again, so a later run's build is warmer than a true cold start even when "
                                "the run's own images were removed"),
        ("caps", f"${_e(m.get('budget_usd'))} per run (the client stops at it), {_e(m.get('timeout_s'))}s wall "
                 f"(the runner kills at it and the row says <code>timed_out</code>)"),
        ("scheduling", "runs are sequential, one session at a time, so wall times do not contend"),
        ("code under test", f"<code>{_e((m.get('code_rev') or '')[:12])}</code>"
                            f"{' with uncommitted changes' if m.get('code_dirty') else ''} · "
                            f"Claude Code {_e(', '.join(versions) or '?')} · working directory <code>{_e(m.get('cwd'))}</code>"),
        ("client command", f"<code>{_e(_command_line(m))}</code>"),
        ("definition", f"<code>{_e(m.get('source'))}</code>"),
    ]
    if m.get("notes"):
        kv.append(("notes", _e(m["notes"])))
    if varied:
        kv.append(("conditions varied", f"<span style='color:var(--warn)'>not every run of this experiment shared "
                                        f"these settings: {_e(', '.join(varied))}. The scoreboard averages them anyway; "
                                        f"read the runs table.</span>"))
    body = "".join(f"<tr><td class='k'>{_e(k)}</td><td class='l'>{v}</td></tr>" for k, v in kv)
    return f"<h3>{_e(name)}</h3><table><tbody>{body}</tbody></table>"


def _setup_section(setups: dict[str, list[dict]], rows: list[dict]) -> str:
    if not setups:
        return ""
    return ("<h2>Experiment setup</h2><div class='setup'>"
            + "".join(_setup_one(name, metas, rows) for name, metas in sorted(setups.items()))
            + "</div>")


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def render(rows: list[dict], setups: dict[str, list[dict]] | None = None, title: str = "Agent experiments") -> str:
    """`setups` maps an experiment name to its runs' experiment.json records, oldest first."""
    groups = aggregate(rows)
    exps = sorted({r["experiment"] for r in rows})
    versions = sorted({r.get("claude_code_version", "") for r in rows if r.get("claude_code_version")})
    revs = sorted({r.get("code_rev", "") for r in rows if r.get("code_rev")})
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>",
        "<button class='toggle' onclick=\"var r=document.documentElement;r.dataset.theme=r.dataset.theme==='light'?'cyber':'light'\">theme</button>",
        f"<h1>{_e(title)}</h1>",
        f"<p class='sub'>{len(rows)} runs · {len(groups)} groups · experiments: {_e(', '.join(exps) or '—')} · "
        f"Claude Code {_e(', '.join(versions) or '?')} · code {_e(', '.join(r[:9] for r in revs) or '?')} · "
        f"rendered {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>",
    ]
    parts.append(_setup_section(setups or {}, rows))
    parts += [
        "<h2>Scoreboard</h2>", _scoreboard(groups),
        "<p class='note'>One row per experiment × model, means over its runs. Success is the experiment's rule judged on the "
        "artifacts the run wrote, never on what the model said. Token columns: nearly all input is served from the prompt "
        "cache, so <i>in (uncached)</i> is small by design; <i>cache read</i> is the context re-read on every call and "
        "the cost driver; <i>cache write</i> is what was added to the cache; <i>peak ctx</i> is the largest single request.</p>",
        "<h2>Across code revisions</h2>", _revisions_table(rows),
        "<h2>Cost against success</h2>",
        "<div class='figs'><div class='fig'><h3>pass@1 by mean cost per run (point size: n)</h3>", _scatter(groups), "</div>",
        "<div class='fig'><h3>MCP outcomes per run, by class</h3>", _stacked_outcomes(groups), "</div></div>",
        "<h2>Efficiency by model</h2><div class='figs'>",
        "<div class='fig'><h3>cost per run</h3>", _bars(groups, "cost_usd", "usd", "cost per run"), "</div>",
        "<div class='fig'><h3>output tokens</h3>", _bars(groups, "output_tokens", "k", "output tokens"), "</div>",
        "<div class='fig'><h3>tool calls</h3>", _bars(groups, "tool_calls", "num", "tool calls"), "</div>",
        "<div class='fig'><h3>wall time</h3>", _bars(groups, "wall_ms", "ms", "wall time"), "</div>",
        "</div><p class='note'>Whiskers: one standard deviation across repeats.</p>",
        "<h2>Runs</h2>", _runs_table(rows),
        "<h2>What tripped the agent</h2>", _tripped_table(groups),
        "<h2>Metrics</h2>", _glossary(),
        "</main></body></html>",
    ]
    return "\n".join(parts)


def write_report(rows: list[dict], out_dir: Path, setups: dict[str, list[dict]] | None = None,
                 title: str = "Agent experiments") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.csv").write_text(rows_to_csv(rows), encoding="utf-8")
    (out_dir / "metrics.json").write_text(
        json.dumps({"rows": rows, "groups": aggregate(rows), "columns": list(METRIC_NAMES)}, indent=1, default=str) + "\n",
        encoding="utf-8")
    page = out_dir / "report.html"
    page.write_text(render(rows, setups, title), encoding="utf-8")
    return page
