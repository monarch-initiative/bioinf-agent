#!/usr/bin/env python3
"""
render_intent_grid — the INPUT-side map: does a user's intent reach what they meant?

The sibling of `render_outcomes_dashboard.py`, and deliberately its mirror image:

    outcomes_dashboard  — the OUTPUT side. Every place the code can END (swept from the
                          AST). "What can the system emit, and has it ever run?"
    intent_grid (here)  — the INPUT side. Every thing a user can MEAN (live-probed against
                          real registries). "Does that meaning reach the right outcome?"

(No counts in this docstring on purpose — a number in prose rots, which is Rule 4's
corollary and the reason the page renders every figure from the corpus at run time.)

They are duals, and the second is not implied by the first. A map of ANSWERS cannot show
a QUESTION that has no answer: an intent reaching no terminal is not a dark cell, it is
not a cell at all. And a cell can be GREEN WITH THE WRONG ANSWER IN IT — `resolve('cellranger')`
returns a clean, fully-tagged `proven` install_call for a CRAN spreadsheet-range parser
when the user meant 10x Genomics' scRNA-seq pipeline. Every terminal behaved perfectly.
Terminal coverage cannot see it, because nothing is broken except the meaning.

THE THREE GAPS this draws — the user's own model, plus one the Talos bug forced:

    intent  --[1]-->  interpretation  --[2]-->  outcome  --[3]-->  report

  [1] interpretation — we resolved to the WRONG TOOL. Ships green; nothing downstream
                       re-checks it, so the honesty contract validates the wrong software
                       perfectly. The single most consequential gap, hence rendered first.
  [2] outcome        — right tool, the install/delivery fails. At least it is loud.
  [3] report         — right tool, right bytes, FALSE DESCRIPTION of them. The nastiest,
                       because nothing is broken: the ENV report cited `bcftools 1.23.1`
                       (htslib's version) for a fork whose whole point is differing from
                       upstream. The env was perfect. Only the paperwork lied — and for a
                       component whose promise is "call once, never look again", the
                       paperwork IS the product.

WHY THIS IS DERIVED AND NOT DRAWN. This project already hand-authored an intent map once —
`docs/scenario_decision_tree.{md,html,json}` — and it rotted; tier 7 deleted 2,653 lines of
it. What survived that effort was the outcomes ledger, and it survived *because* it is
projected from code and drift-linted. So this page is a pure projection of
`docs/intent_corpus.json`, which is itself executed by `tests/live/test_intent_corpus.py`.
Nothing here is authored. If the corpus is stale, the page says so rather than flattering us.

Deterministic, fully escaped, no network/CDN. Regenerate:

    python scripts/build_intent_corpus.py     # re-probe the corpus (live registries)
    python scripts/render_intent_grid.py      # re-render this page
"""
from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "docs" / "intent_corpus.json"
OUT = ROOT / "docs" / "intent_grid.html"

# gap → (glyph, css class, what it means). Ordered by CONSEQUENCE: a silently wrong tool
# outranks a loud failure, because a failure tells you it failed.
GAPS = {
    "interpretation": ("🎯", "bad",  "intent → interpretation: we resolved to the WRONG TOOL. Ships green."),
    "report":         ("📄", "warn", "outcome → report: right bytes, FALSE description of them."),
    "outcome":        ("💥", "warn", "interpretation → outcome: right tool, delivery fails. At least it's loud."),
    "none":           ("✅", "ok",   "no gap — a regression guard for behaviour that is already correct."),
}

_CSS = """
:root{--bg:#0b0f14;--fg:#c8d6e5;--dim:#5c7080;--line:#1c2733;--card:#111820;
--ok:#00e5a0;--warn:#ffb454;--bad:#ff4d6d;--acc:#38bdf8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:1180px;margin:0 auto;padding:28px 18px 64px}
h1{font-size:22px;margin:0 0 4px;color:#fff;letter-spacing:.02em}
h2{font-size:15px;margin:30px 0 10px;color:var(--acc);text-transform:uppercase;letter-spacing:.08em}
.sub{color:var(--dim);margin:0 0 22px}
.meter{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0 26px}
.m{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;min-width:132px}
.m .n{font-size:26px;font-weight:700;color:#fff;line-height:1.2}
.m .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.07em}
.bar{display:flex;height:9px;border-radius:5px;overflow:hidden;margin:6px 0 24px;border:1px solid var(--line)}
.bar i{display:block}
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.bg-ok{background:var(--ok)} .bg-warn{background:var(--warn)} .bg-bad{background:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:400;border-bottom:1px solid var(--line);
padding:7px 8px;text-transform:uppercase;font-size:10.5px;letter-spacing:.07em}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:#0f1620}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
code{background:#0a1017;padding:1.5px 5px;border-radius:3px;color:var(--acc);font-size:12px}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;border:1px solid currentColor}
.note{color:var(--dim);font-size:11.5px}
.stamp{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);color:var(--dim);font-size:11.5px}
.fam{color:#fff;font-weight:600}
.arrow{color:var(--dim);font-size:12px;margin:0 4px}
"""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _chain(row: dict) -> str:
    """The row's journey, drawn: what they typed → what we chose → what they meant.
    The whole grid in one cell."""
    call = row.get("call", {})
    typed = call.get("tool", "?")
    if call.get("version"):
        typed += f"=={call['version']}"
    if call.get("github_repo"):
        typed += f" +repo"
    got = row.get("actual_today", {}).get("chosen")
    want = row.get("expect", {}).get("chosen")
    got_s = f'<span class="bad">{_e(got)}</span>' if got != want else f'<span class="ok">{_e(got)}</span>'
    if got is None:
        got_s = '<span class="ok">refuses+asks</span>'
    want_s = "refuse+ask" if want is None else _e(want)
    return (f'<code>{_e(typed)}</code><span class="arrow">→</span>{got_s}'
            f'<span class="arrow">·want</span><span class="note">{want_s}</span>')


def render(corpus: dict) -> str:
    rows = [r for r in corpus.get("rows", []) if isinstance(r, dict)]
    total = len(rows)
    ok = [r for r in rows if r.get("is_correct_today")]
    wrong = [r for r in rows if not r.get("is_correct_today")]
    unassertable = [r for r in rows if not r.get("expect", {}).get("assertable", True)]

    by_gap: dict[str, list] = {}
    for r in wrong:
        by_gap.setdefault(r.get("gap_class", "?"), []).append(r)

    P: list[str] = []
    P.append(f"<style>{_CSS}</style>")
    P.append('<div class="wrap">')
    P.append("<h1>Intent grid — does a user's input reach what they meant?</h1>")
    P.append('<p class="sub">The INPUT side. Its sibling '
             '<code>outcomes_dashboard.html</code> maps every place the code can END; this '
             'maps every thing a user can MEAN. A map of answers cannot show a question with '
             'no answer — and a terminal can be green with the wrong tool in it.</p>')

    # -- the meter -----------------------------------------------------------
    pct = (100 * len(ok) // total) if total else 0
    P.append('<div class="meter">')
    P.append(f'<div class="m"><div class="n">{total}</div><div class="l">scenarios</div></div>')
    P.append(f'<div class="m"><div class="n ok">{len(ok)}</div><div class="l">reach intent</div></div>')
    P.append(f'<div class="m"><div class="n bad">{len(wrong)}</div><div class="l">do not</div></div>')
    P.append(f'<div class="m"><div class="n">{pct}%</div><div class="l">correct today</div></div>')
    if unassertable:
        P.append(f'<div class="m"><div class="n warn">{len(unassertable)}</div>'
                 f'<div class="l">unassertable</div></div>')
    P.append("</div>")
    if total:
        w_ok = 100 * len(ok) // total
        P.append(f'<div class="bar"><i class="bg-ok" style="width:{w_ok}%"></i>'
                 f'<i class="bg-bad" style="width:{100 - w_ok}%"></i></div>')

    # -- the three gaps ------------------------------------------------------
    P.append("<h2>Where the meaning is lost</h2>")
    P.append('<p class="sub">intent <span class="arrow">→[1]→</span> interpretation '
             '<span class="arrow">→[2]→</span> outcome <span class="arrow">→[3]→</span> report. '
             'Ordered by consequence: a silently wrong tool outranks a loud failure, because '
             'a failure tells you it failed.</p>')
    P.append('<div class="scroll"><table>')
    P.append("<tr><th></th><th>gap</th><th>count</th><th>what it means</th></tr>")
    for gap, (glyph, cls, meaning) in GAPS.items():
        n = len(by_gap.get(gap, [])) if gap != "none" else len(ok)
        P.append(f'<tr><td>{glyph}</td><td class="{cls}">{_e(gap)}</td>'
                 f'<td class="{cls}"><b>{n}</b></td><td class="note">{_e(meaning)}</td></tr>')
    P.append("</table></div>")

    # -- what this page CANNOT see ------------------------------------------
    # Rendered BEFORE the scenario table, not after, and never folded into a footnote.
    # Tier 6's lesson, applied to a page instead of an install_call: a warning parked in a
    # sibling key is a warning that gets skipped. A grid that shows only what it can measure
    # reads as "this is everything" — which is the exact failure mode the whole audit is
    # about, one level up.
    gaps = corpus.get("known_gaps") or []
    if gaps:
        P.append("<h2>What this grid cannot see</h2>")
        P.append('<p class="sub">Read this BEFORE the numbers above. Every meter on this page '
                 'measures only what the harness can assert; these are the things we know are '
                 'wrong and cannot yet mechanically check. Listing them here is the point — a '
                 'grid silent about its blind spots reads as complete.</p>')
        P.append('<div class="scroll"><table>')
        P.append("<tr><th>blind spot</th><th>why it isn't fixed here</th>"
                 "<th>what closes it</th></tr>")
        for g in gaps:
            P.append(f'<tr><td class="warn"><b>{_e(g.get("gap"))}</b><br>'
                     f'<span class="note">{_e(g.get("detail"))}</span></td>'
                     f'<td class="note">{_e(g.get("why_not_fixed_here"))}</td>'
                     f'<td class="note">{_e(g.get("what_tier_8_must_do"))}</td></tr>')
        P.append("</table></div>")


    # -- the rows, worst first ----------------------------------------------
    order = {"interpretation": 0, "report": 1, "outcome": 2, "none": 3}
    rows_sorted = sorted(rows, key=lambda r: (order.get(r.get("gap_class"), 9),
                                              r.get("is_correct_today", False),
                                              r.get("id", "")))
    P.append("<h2>Every scenario</h2>")
    P.append('<div class="scroll"><table>')
    P.append("<tr><th>gap</th><th>what the user typed → what we do → what they meant</th>"
             "<th>the intent</th><th>why it's hard</th></tr>")
    for r in rows_sorted:
        gap = r.get("gap_class", "?")
        glyph, cls, _ = GAPS.get(gap, ("?", "warn", ""))
        flag = "" if r.get("expect", {}).get("assertable", True) else \
            ' <span class="pill warn">unassertable</span>'
        P.append(f'<tr><td>{glyph}</td><td>{_chain(r)}{flag}<br>'
                 f'<span class="note">{_e(r.get("id"))}</span></td>'
                 f'<td class="note">{_e(r.get("intent", ""))[:150]}</td>'
                 f'<td class="note">{_e(r.get("why_hard", ""))[:190]}</td></tr>')
    P.append("</table></div>")

    # -- the stamp -----------------------------------------------------------
    probed = corpus.get("probed_on", "UNKNOWN")
    P.append('<div class="stamp">')
    P.append(f"Projected from <code>docs/intent_corpus.json</code>, probed against live "
             f"registries on <b>{_e(probed)}</b>. Executed by "
             f"<code>tests/live/test_intent_corpus.py</code> (<code>pytest -m live</code>).<br>"
             f"Nothing on this page is authored — it is a projection of that corpus, exactly "
             f"as the outcomes dashboard is a projection of the code. This project "
             f"hand-drew an intent map once; it rotted, and tier 7 deleted 2,653 lines of it. "
             f"<b>A scenario probed long ago is a claim about the past</b> — if the date "
             f"above is stale, re-probe before believing a single number here.")
    P.append("</div></div>")
    return "\n".join(P)


def main() -> int:
    if not CORPUS.exists():
        print(f"  no corpus at {CORPUS.relative_to(ROOT)} — run scripts/build_intent_corpus.py")
        return 1
    corpus = json.loads(CORPUS.read_text())
    OUT.write_text(render(corpus))
    rows = corpus.get("rows", [])
    ok = sum(1 for r in rows if r.get("is_correct_today"))
    print(f"  wrote {OUT.relative_to(ROOT)}  ({len(rows)} scenarios · {ok} reach intent · "
          f"{len(rows) - ok} do not · probed {corpus.get('probed_on', '?')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
