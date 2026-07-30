#!/usr/bin/env python3
"""render_capability_map — the THIRD map (next to outcomes_dashboard + intent_grid).

Reads docs/capability_decisions.json (the curated source of truth) and renders
docs/capability_map.html: the action-verbs a user can ask for (capability cards) +
the flat inventory of every decision the system makes when you don't specify.

Pure: JSON in, HTML out. Deterministic (no timestamps — the version string lives in
the JSON's meta). Everything HTML-escaped. No external assets. Run:

    python scripts/render_capability_map.py
"""
from __future__ import annotations

import html
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "docs" / "capability_decisions.json"
_OUT = _ROOT / "docs" / "capability_map.html"

# status -> (label, accent colour, glyph)
_STATUS = {
    "coded_fixed":        ("CODED · fixed",        "#3ddc84", "●"),  # green
    "coded_configurable": ("CODED · configurable", "#4aa3ff", "◆"),  # blue
    "improvised":         ("IMPROVISED",           "#ffb020", "▲"),  # amber
    "missing":            ("MISSING",              "#ff4d6d", "✕"),  # red
}


def _e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _status_pill(status: str) -> str:
    label, colour, glyph = _STATUS.get(status, (status, "#888", "?"))
    return (f'<span class="pill" style="border-color:{colour};color:{colour}">'
            f'{glyph}&nbsp;{_e(label)}</span>')


def _exit_cell(v) -> str:
    if v is True:
        return '<span style="color:#3ddc84">caught ✓</span>'
    if v is False:
        return '<span style="color:#ff4d6d">NOT caught</span>'
    return '<span class="muted">n/a</span>'


def _render_matrix(meta: dict, decs: list) -> str:
    """The execution-shape grid — rendered PURELY from decisions tagged with
    `execution_row` + `by_locus`. Single source of truth: the cells ARE the
    tagged decisions, so there is no separate matrix blob to keep in sync."""
    rows = [d for d in decs
            if d.get("execution_row") and isinstance(d.get("by_locus"), dict)]
    if not rows:
        return ""
    cols_seen: list = []
    for d in rows:
        for k in d["by_locus"]:
            if k not in cols_seen:
                cols_seen.append(k)
    order = {"local": 0, "cluster": 1}
    cols = sorted(cols_seen, key=lambda c: (order.get(c, 9), c))
    head = "".join(f"<th>{_e(c)}</th>" for c in cols)
    body = []
    for d in rows:
        cells = []
        for c in cols:
            val = d["by_locus"].get(c, "—")
            nf = "nf" if "nextflow" in str(val).lower() else ""
            cells.append(f'<td class="mcell {nf}">{_e(val)}</td>')
        body.append(
            f'<tr><th class="mrow">{_e(d["execution_row"])}'
            f'<div class="did">{_e(d["id"])}</div></th>{"".join(cells)}</tr>')
    note = meta.get("execution_matrix_note", "")
    return (
        '<h2>How work actually runs</h2>'
        f'<div class="muted" style="margin-bottom:10px;font-size:12.5px">{_e(note)}</div>'
        '<div class="tablewrap"><table class="matrix">'
        f'<thead><tr><th></th>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>')


def render(data: dict) -> str:
    meta = data["meta"]
    caps = data["capabilities"]
    decs = data["decisions"]

    # index decisions by id for the capability -> decision cross-links
    dec_by_id = {d["id"]: d for d in decs}

    # ---- capability cards -------------------------------------------------
    cards = []
    for c in caps:
        missing = not c.get("exists", True)
        head_glyph = "✕" if missing else "▸"
        head_colour = "#ff4d6d" if missing else "#8be9fd"
        needs = "".join(f"<li>{_e(n)}</li>" for n in c.get("needs", [])) or "<li class='muted'>—</li>"
        # decisions this verb makes, as pills that name + colour by status
        dpills = []
        for did in c.get("decides", []):
            d = dec_by_id.get(did)
            st = d["status"] if d else "improvised"
            _, colour, glyph = _STATUS.get(st, ("", "#888", "?"))
            dpills.append(f'<span class="dchip" style="border-color:{colour};color:{colour}" '
                          f'title="{_e(d["question"]) if d else did}">{glyph}&nbsp;{_e(did)}</span>')
        dpills_html = " ".join(dpills) or "<span class='muted'>—</span>"
        cards.append(f"""
        <div class="card {'missing' if missing else ''}">
          <div class="card-h" style="color:{head_colour}">{head_glyph}&nbsp;{_e(c['ask'])}
            {'<span class="tag-missing">NOT A FIRST-CLASS VERB</span>' if missing else ''}
          </div>
          <div class="verb">verb: <code>{_e(c['verb'])}</code></div>
          <div class="row"><span class="k">You give</span><ul class="needs">{needs}</ul></div>
          <div class="row"><span class="k">It decides for you</span><div class="decides">{dpills_html}</div></div>
          <div class="row"><span class="k">Runs in</span><div>{_e(c['runs_in'])}</div></div>
          <div class="row"><span class="k">You get</span><div>{_e(c['produces'])}</div></div>
          <div class="note">{_e(c.get('note',''))}</div>
        </div>""")

    # ---- decisions table --------------------------------------------------
    # order: improvised first (the backlog), then configurable, then fixed
    order = {"improvised": 0, "missing": 0, "coded_configurable": 1, "coded_fixed": 2}
    decs_sorted = sorted(decs, key=lambda d: (order.get(d["status"], 9), d["id"]))
    rows = []
    for d in decs_sorted:
        rows.append(f"""
        <tr>
          <td class="q"><b>{_e(d['question'])}</b><div class="did">{_e(d['id'])}</div></td>
          <td class="def">{_e(d['default'])}</td>
          <td>{_status_pill(d['status'])}</td>
          <td class="exit">{_exit_cell(d.get('exit_caught'))}</td>
          <td class="src"><code>{_e(d.get('source_ref',''))}</code><div class="muted">{_e(d.get('source',''))}</div>
              {f'<div class="dnote">{_e(d["note"])}</div>' if d.get('note') else ''}</td>
        </tr>""")

    # counts for the summary strip
    n_imp = sum(1 for d in decs if d["status"] == "improvised")
    n_missing = sum(1 for c in caps if not c.get("exists", True))
    n_total = len(decs)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(meta['title'])}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0b0e14; color:#c9d1d9;
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:28px 20px 80px; }}
  h1 {{ font-size:22px; margin:0 0 4px; color:#e6edf3; letter-spacing:.2px; }}
  .ver {{ color:#6e7681; font-size:12px; margin-bottom:18px; }}
  .purpose {{ background:#11161f; border:1px solid #1f2733; border-left:3px solid #8be9fd;
              border-radius:8px; padding:14px 16px; margin:0 0 14px; color:#aeb8c4; }}
  .honesty {{ background:#1a1206; border:1px solid #3a2a0a; border-left:3px solid #ffb020;
              border-radius:8px; padding:12px 16px; margin:0 0 20px; color:#e0c890; font-size:13.5px; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:10px; margin:0 0 22px; font-size:12.5px; }}
  .legend .pill {{ font-size:12px; }}
  .strip {{ display:flex; gap:14px; flex-wrap:wrap; margin:0 0 26px; }}
  .stat {{ background:#11161f; border:1px solid #1f2733; border-radius:8px; padding:10px 16px; }}
  .stat b {{ font-size:22px; display:block; }}
  .stat.amber b {{ color:#ffb020; }} .stat.red b {{ color:#ff4d6d; }} .stat.dim b {{ color:#8be9fd; }}
  .stat span {{ font-size:12px; color:#8b949e; }}
  h2 {{ font-size:16px; margin:30px 0 12px; color:#e6edf3;
        border-bottom:1px solid #1f2733; padding-bottom:6px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:14px; }}
  .card {{ background:#11161f; border:1px solid #1f2733; border-radius:10px; padding:14px 16px; }}
  .card.missing {{ border-color:#3a1420; background:#170c11; }}
  .card-h {{ font-size:15px; font-weight:650; margin-bottom:8px; }}
  .tag-missing {{ font-size:10px; background:#ff4d6d; color:#160a0d; padding:1px 6px;
                  border-radius:4px; margin-left:6px; vertical-align:middle; letter-spacing:.4px; }}
  .verb {{ font-size:12px; color:#6e7681; margin-bottom:10px; }}
  .verb code, .src code {{ background:#0b0e14; border:1px solid #1f2733; border-radius:4px;
                           padding:1px 5px; color:#8be9fd; font-size:12px; }}
  .row {{ display:flex; gap:10px; margin:7px 0; align-items:flex-start; }}
  .row .k {{ flex:0 0 92px; color:#6e7681; font-size:12px; text-transform:uppercase;
             letter-spacing:.4px; padding-top:2px; }}
  ul.needs {{ margin:0; padding-left:16px; }} ul.needs li {{ font-size:13.5px; }}
  .decides {{ display:flex; flex-wrap:wrap; gap:5px; }}
  .dchip {{ font-size:11px; border:1px solid; border-radius:10px; padding:1px 8px; cursor:help; }}
  .note {{ margin-top:10px; font-size:12.5px; color:#8b949e; font-style:italic;
           border-top:1px dashed #1f2733; padding-top:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  th {{ text-align:left; color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:.5px;
        padding:8px 10px; border-bottom:1px solid #1f2733; position:sticky; top:0; background:#0b0e14; }}
  td {{ padding:11px 10px; border-bottom:1px solid #151b24; vertical-align:top; }}
  td.q b {{ color:#e6edf3; }} .did {{ font-size:11px; color:#6e7681; margin-top:2px; }}
  td.def {{ color:#aeb8c4; max-width:280px; }}
  .pill {{ display:inline-block; border:1px solid; border-radius:12px; padding:2px 9px;
           font-size:11.5px; white-space:nowrap; }}
  .src {{ max-width:340px; }} .src .muted {{ font-size:11.5px; margin-top:3px; }}
  .dnote {{ font-size:12px; color:#c98b9a; margin-top:6px; font-style:italic; }}
  .muted {{ color:#6e7681; }}
  .tablewrap {{ overflow-x:auto; }}
  table.matrix {{ border-collapse:collapse; }}
  table.matrix th {{ text-transform:none; letter-spacing:0; font-size:13px; color:#8be9fd;
                     position:static; background:transparent; border-bottom:1px solid #1f2733; }}
  table.matrix th.mrow {{ text-align:left; color:#e6edf3; padding:12px; white-space:nowrap;
                          vertical-align:top; border-bottom:1px solid #151b24; }}
  table.matrix td.mcell {{ border:1px solid #1f2733; padding:12px; vertical-align:top;
                           min-width:220px; color:#c9d1d9; }}
  table.matrix td.mcell.nf {{ border-color:#7c5cff; box-shadow: inset 0 0 0 1px #7c5cff44; }}
  table.matrix td.mcell.nf::after {{ content:"Nextflow"; display:inline-block; margin-top:8px;
                           font-size:10px; letter-spacing:.5px; color:#b8a6ff;
                           border:1px solid #7c5cff; border-radius:4px; padding:1px 6px; }}
</style></head>
<body><div class="wrap">
  <h1>{_e(meta['title'])}</h1>
  <div class="ver">{_e(meta['version'])} &nbsp;·&nbsp; rendered from <code>docs/capability_decisions.json</code></div>
  <div class="purpose">{_e(meta['purpose'])}</div>
  <div class="honesty">⚠ {_e(meta['honesty_note'])}</div>
  <div class="legend">
    {_status_pill('coded_fixed')} &nbsp; {_status_pill('coded_configurable')} &nbsp;
    {_status_pill('improvised')} &nbsp; {_status_pill('missing')}
  </div>
  <div class="strip">
    <div class="stat dim"><b>{len(caps)-n_missing}</b><span>capabilities you can ask for</span></div>
    <div class="stat red"><b>{n_missing}</b><span>expected verb still MISSING</span></div>
    <div class="stat"><b>{n_total}</b><span>decisions the system makes</span></div>
    <div class="stat amber"><b>{n_imp}</b><span>IMPROVISED (the determinism backlog)</span></div>
  </div>

  <h2>What you can ask this system to do</h2>
  <div class="cards">{''.join(cards)}</div>

  {_render_matrix(meta, decs)}

  <h2>Every decision it makes when you don't specify</h2>
  <div class="muted" style="margin-bottom:10px;font-size:12.5px">
    {_e(meta['exit_caught_note'])}</div>
  <div class="tablewrap"><table>
    <thead><tr><th>Decision</th><th>Default</th><th>Status</th><th>Wrong choice?</th><th>Source · notes</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
</div></body></html>
"""


def main() -> None:
    data = json.loads(_SRC.read_text())
    _OUT.write_text(render(data))
    n_imp = sum(1 for d in data["decisions"] if d["status"] == "improvised")
    n_missing = sum(1 for c in data["capabilities"] if not c.get("exists", True))
    print(f"wrote {_OUT.relative_to(_ROOT)}  "
          f"({len(data['capabilities'])} capabilities, {len(data['decisions'])} decisions, "
          f"{n_imp} improvised, {n_missing} missing verb)")


if __name__ == "__main__":
    main()
