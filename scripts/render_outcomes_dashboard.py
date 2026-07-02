#!/usr/bin/env python3
"""
render_outcomes_dashboard — the system health panel (the "digital twin" view).

Renders docs/outcomes_ledger.json (produced by scripts/extract_outcomes.py) into
a single self-contained HTML page: docs/outcomes_dashboard.html.

It is a PROJECTION OF THE CODE, not a hand-drawn diagram — every terminal shown
was harvested from source by the extractor, so the picture can't lie or rot. The
panel makes the honesty holes visible at a glance:

  - vanished terminals  — a failure with NO durable trace (should be zero)
  - untagged terminals  — a terminal the model can't classify yet (should be zero)
  - untested terminals  — a terminal no test references: CLASSIFIED but UNPROVEN.
                          This is the live hardening worklist.

Deterministic (sorted), fully escaped, no network/CDN. Regenerate with:

    python scripts/extract_outcomes.py            # refresh the ledger
    python scripts/render_outcomes_dashboard.py   # re-render this panel
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "outcomes_ledger.json"
OUT = ROOT / "docs" / "outcomes_dashboard.html"

# Outcome class → (glyph, colour var, one-line meaning). Order = display order.
OUTCOMES = {
    "proven":   ("✅", "ok",   "validated success"),
    "refused":  ("⛔", "cyan", "clean gate, before any side-effect"),
    "broke":    ("💥", "bad",  "hard failure, recorded"),
    "degraded": ("⚠️", "warn", "proceeded with reduced assurance"),
    "loop":     ("🔁", "loop", "recoverable, feeds a retry"),
    "vanished": ("👻", "ghost", "failure with NO durable trace — an honesty hole"),
}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _load() -> list[dict]:
    return json.loads(LEDGER.read_text())


def _subsystem(code: str | None) -> str:
    return code.split(".", 1)[0] if code else "«untagged»"


def _counts(entries: list[dict]) -> dict[str, int]:
    c: dict[str, int] = {}
    for e in entries:
        c[e["outcome"]] = c.get(e["outcome"], 0) + 1
    return c


def _bar(counts: dict[str, int], total: int) -> str:
    """A stacked outcome bar, widths proportional to counts."""
    if total == 0:
        return '<div class="bar"></div>'
    segs = []
    for oc in OUTCOMES:
        n = counts.get(oc, 0)
        if not n:
            continue
        pct = 100.0 * n / total
        segs.append(f'<span class="seg {oc}" style="width:{pct:.3f}%" '
                    f'title="{oc}: {n}"></span>')
    return f'<div class="bar">{"".join(segs)}</div>'


def _chips(counts: dict[str, int]) -> str:
    out = []
    for oc, (glyph, _cls, _m) in OUTCOMES.items():
        n = counts.get(oc, 0)
        if not n:
            continue
        out.append(f'<span class="chip {oc}">{glyph} {n}</span>')
    return "".join(out)


def _cov_class(tested: int, total: int) -> str:
    if total == 0:
        return "cov-none"
    r = tested / total
    return "cov-good" if r >= 0.66 else "cov-mid" if r >= 0.33 else "cov-low"


def render(entries: list[dict]) -> str:
    total = len(entries)
    tally = _counts(entries)
    tested = sum(1 for e in entries if e.get("tested"))
    untested = [e for e in entries if e.get("tested") is False]
    untagged = [e for e in entries if not e.get("tagged")]
    vanished = [e for e in entries if e.get("outcome") == "vanished"]
    subs = sorted({_subsystem(e.get("code")) for e in entries})

    # group by subsystem
    by_sub: dict[str, list[dict]] = {}
    for e in entries:
        by_sub.setdefault(_subsystem(e.get("code")), []).append(e)

    # Subsystem order: worst-hardened first — most untested FAILURE paths
    # (broke/vanished with no test) at the top, so the panel reads as a
    # prioritised hardening worklist, then by size, then name.
    def risk(name: str) -> tuple:
        rows = by_sub[name]
        unproven_fail = sum(
            1 for e in rows
            if e.get("tested") is False and e["outcome"] in ("broke", "vanished"))
        return (-unproven_fail, -len(rows), name)

    order = sorted(by_sub, key=risk)

    # ---- header stat tiles -------------------------------------------------
    cov_pct = round(100 * tested / total) if total else 0
    hole_tiles = [
        ("VANISHED", len(vanished), "ghost",
         "failures with no durable trace"),
        ("UNTAGGED", len(untagged), "bad",
         "terminals the model can't classify"),
        ("UNTESTED", len(untested), "warn",
         "classified but no test proves the branch fires"),
    ]
    tiles_html = "".join(
        f'<div class="tile {"tile-zero" if n == 0 else cls}">'
        f'<div class="tn">{n}</div><div class="tl">{_e(label)}</div>'
        f'<div class="td">{_e(desc)}</div></div>'
        for label, n, cls, desc in hole_tiles
    )

    tally_html = "".join(
        f'<span class="chip {oc}">{glyph} {_e(oc)} {tally.get(oc, 0)}</span>'
        for oc, (glyph, _c, _m) in OUTCOMES.items()
    )

    # ---- subsystem cards ---------------------------------------------------
    cards = []
    for name in order:
        rows = by_sub[name]
        n = len(rows)
        cc = _counts(rows)
        t_tested = sum(1 for e in rows if e.get("tested"))
        covcls = _cov_class(t_tested, n)
        rows_sorted = sorted(
            rows, key=lambda e: (list(OUTCOMES).index(e["outcome"])
                                 if e["outcome"] in OUTCOMES else 99,
                                 not (e.get("tested") is False),  # untested first
                                 e.get("code") or e.get("where")))
        lis = []
        for e in rows_sorted:
            oc = e["outcome"]
            glyph = OUTCOMES.get(oc, ("•",))[0]
            tst = e.get("tested")
            tcls = "t-yes" if tst else "t-no" if tst is False else "t-na"
            tlab = "tested" if tst else "UNTESTED" if tst is False else "—"
            code = e.get("code") or "«untagged raw terminal»"
            lis.append(
                f'<li class="{oc}">'
                f'<span class="g">{glyph}</span>'
                f'<code class="cd">{_e(code)}</code>'
                f'<span class="td2 {tcls}">{tlab}</span>'
                f'<span class="wh">{_e(e.get("where"))}</span>'
                f'</li>')
        cards.append(
            f'<details class="card" data-untested="{sum(1 for e in rows if e.get("tested") is False)}">'
            f'<summary>'
            f'<span class="sname">{_e(name)}</span>'
            f'<span class="scount">{n}</span>'
            f'{_bar(cc, n)}'
            f'<span class="schips">{_chips(cc)}</span>'
            f'<span class="scov {covcls}">{t_tested}/{n} tested</span>'
            f'</summary>'
            f'<ul class="terms">{"".join(lis)}</ul>'
            f'</details>')

    legend = "".join(
        f'<span class="lg"><span class="chip {oc}">{glyph}</span>'
        f'<b>{_e(oc)}</b> — {_e(meaning)}</span>'
        for oc, (glyph, _c, meaning) in OUTCOMES.items())

    return _PAGE.format(
        total=total,
        subs=len(subs),
        cov_pct=cov_pct,
        tested=tested,
        tiles=tiles_html,
        tally=tally_html,
        cards="".join(cards),
        legend=legend,
    )


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>System Decision Surface — Outcome Health Panel</title>
<style>
:root{{
  --bg:#0a0c14;--surface:#13151f;--surface-2:#1a1d29;--border:#262a3a;
  --cyan:#22e3ee;--yellow:#fff200;--ink:#e6e9f0;--muted:#8e98ad;
  --ok:#3ce086;--bad:#ff4b6e;--warn:#ffa940;--loop:#b37bff;--ghost:#ff3df0;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--mono);
  padding:26px 24px 60px}}
h1{{font-size:22px;font-weight:800;color:var(--yellow);letter-spacing:.02em;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin:0 0 22px}}
.sub b{{color:var(--cyan)}}
/* stat row */
.stats{{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 20px}}
.big{{border:1px solid var(--border);background:var(--surface);padding:14px 18px;min-width:120px}}
.big .n{{font-size:30px;font-weight:800;color:var(--cyan);line-height:1}}
.big .l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.13em;color:var(--muted);margin-top:6px}}
.covwrap{{flex:1;min-width:220px;border:1px solid var(--border);background:var(--surface);padding:14px 18px}}
.covwrap .l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.13em;color:var(--muted)}}
.covbar{{height:12px;background:var(--surface-2);margin-top:8px;position:relative;overflow:hidden}}
.covbar>span{{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--ok),var(--cyan))}}
.covnum{{font-size:20px;font-weight:800;color:var(--ink);margin-top:8px}}
/* hole tiles */
.tiles{{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 26px}}
.tile{{flex:1;min-width:180px;border:1px solid var(--border);border-left-width:4px;
  background:var(--surface);padding:14px 18px}}
.tile .tn{{font-size:30px;font-weight:800;line-height:1}}
.tile .tl{{font-size:11px;text-transform:uppercase;letter-spacing:.14em;margin-top:6px}}
.tile .td{{font-size:11px;color:var(--muted);margin-top:5px}}
.tile.ghost{{border-left-color:var(--ghost)}} .tile.ghost .tn{{color:var(--ghost)}}
.tile.bad{{border-left-color:var(--bad)}} .tile.bad .tn{{color:var(--bad)}}
.tile.warn{{border-left-color:var(--warn)}} .tile.warn .tn{{color:var(--warn)}}
.tile.tile-zero{{border-left-color:var(--ok)}} .tile.tile-zero .tn{{color:var(--ok)}}
/* tally + legend */
.tally{{margin:0 0 8px}}
.chip{{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:2px;margin:0 6px 6px 0;
  border:1px solid var(--border);background:var(--surface-2)}}
.chip.proven{{color:var(--ok)}} .chip.refused{{color:var(--cyan)}}
.chip.broke{{color:var(--bad)}} .chip.degraded{{color:var(--warn)}}
.chip.loop{{color:var(--loop)}} .chip.vanished{{color:var(--ghost)}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;margin:6px 0 22px;font-size:11.5px;color:var(--muted)}}
.legend b{{color:var(--ink)}}
.toolbar{{margin:0 0 14px}}
.toolbar button{{font:inherit;font-size:11px;color:var(--ink);background:var(--surface-2);
  border:1px solid var(--border);padding:6px 12px;cursor:pointer;margin-right:8px}}
.toolbar button:hover{{border-color:var(--cyan);color:var(--cyan)}}
.toolbar button.on{{border-color:var(--yellow);color:var(--yellow)}}
/* subsystem cards */
.card{{border:1px solid var(--border);background:var(--surface);margin:0 0 8px}}
.card>summary{{list-style:none;cursor:pointer;display:grid;
  grid-template-columns:180px 44px 1fr auto auto;gap:14px;align-items:center;padding:11px 16px}}
.card>summary::-webkit-details-marker{{display:none}}
.card>summary:hover{{background:var(--surface-2)}}
.sname{{font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.scount{{color:var(--muted);text-align:right;font-size:12px}}
.bar{{height:12px;background:var(--surface-2);display:flex;overflow:hidden;min-width:120px}}
.seg{{height:100%}}
.seg.proven{{background:var(--ok)}} .seg.refused{{background:var(--cyan)}}
.seg.broke{{background:var(--bad)}} .seg.degraded{{background:var(--warn)}}
.seg.loop{{background:var(--loop)}} .seg.vanished{{background:var(--ghost)}}
.schips{{white-space:nowrap}}
.scov{{font-size:11px;white-space:nowrap;padding:2px 8px;border:1px solid var(--border)}}
.cov-good{{color:var(--ok)}} .cov-mid{{color:var(--warn)}}
.cov-low{{color:var(--bad)}} .cov-none{{color:var(--muted)}}
/* terminal list */
ul.terms{{list-style:none;margin:0;padding:4px 0 10px;border-top:1px solid var(--border)}}
ul.terms li{{display:grid;grid-template-columns:22px 1fr 88px auto;gap:12px;align-items:center;
  padding:4px 16px}}
ul.terms li:hover{{background:var(--surface-2)}}
.g{{text-align:center}}
.cd{{color:var(--ink);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
li.broke .cd,li.vanished .cd{{color:#ffd7de}}
.td2{{font-size:10px;text-transform:uppercase;letter-spacing:.1em;text-align:center;
  padding:1px 0;border-radius:2px}}
.t-yes{{color:var(--ok)}} .t-no{{color:var(--bg);background:var(--warn);font-weight:700}}
.t-na{{color:var(--muted)}}
.wh{{color:var(--muted);font-size:11px;text-align:right;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
body.only-untested li:not(.u){{display:none}}
body.only-untested details[data-untested="0"]{{display:none}}
.foot{{color:var(--muted);font-size:11px;margin-top:26px;border-top:1px solid var(--border);padding-top:14px}}
</style></head><body>
<h1>System Decision Surface</h1>
<p class="sub">The outcome health panel — a <b>projection of the code</b>, harvested
straight from source by <b>scripts/extract_outcomes.py</b>. Every terminal shown is a
place the system succeeds, refuses, or fails. Nothing here is hand-drawn.</p>

<div class="stats">
  <div class="big"><div class="n">{total}</div><div class="l">terminals</div></div>
  <div class="big"><div class="n">{subs}</div><div class="l">subsystems</div></div>
  <div class="covwrap"><div class="l">test coverage</div>
    <div class="covnum">{tested} / {total} &nbsp;·&nbsp; {cov_pct}%</div>
    <div class="covbar"><span style="width:{cov_pct}%"></span></div>
  </div>
</div>

<div class="tiles">{tiles}</div>

<div class="tally">{tally}</div>
<div class="legend">{legend}</div>

<div class="toolbar">
  <button onclick="document.querySelectorAll('details.card').forEach(d=>d.open=true)">expand all</button>
  <button onclick="document.querySelectorAll('details.card').forEach(d=>d.open=false)">collapse all</button>
  <button id="ut" onclick="toggleUntested(this)">show only untested</button>
</div>

{cards}

<p class="foot">Subsystems are ordered by <b>hardening priority</b>: those with the most
untested failure paths (broke/vanished with no test) first. Regenerate:
<code>python scripts/extract_outcomes.py &amp;&amp; python scripts/render_outcomes_dashboard.py</code></p>

<script>
// mark untested <li> so the filter can hide the rest
document.querySelectorAll('ul.terms li').forEach(li=>{{
  if(li.querySelector('.t-no')) li.classList.add('u');
}});
function toggleUntested(btn){{
  document.body.classList.toggle('only-untested');
  btn.classList.toggle('on');
  if(document.body.classList.contains('only-untested'))
    document.querySelectorAll('details.card').forEach(d=>{{if(d.dataset.untested!=='0')d.open=true;}});
}}
</script>
</body></html>"""


def main() -> int:
    if not LEDGER.exists():
        print(f"  ! {LEDGER} not found — run scripts/extract_outcomes.py first",
              file=sys.stderr)
        return 1
    entries = _load()
    OUT.write_text(render(entries))
    tested = sum(1 for e in entries if e.get("tested"))
    untested = sum(1 for e in entries if e.get("tested") is False)
    vanished = sum(1 for e in entries if e.get("outcome") == "vanished")
    print(f"  wrote {OUT.relative_to(ROOT)}  "
          f"({len(entries)} terminals · {tested} tested · {untested} untested · "
          f"{vanished} vanished)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
