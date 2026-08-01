#!/usr/bin/env python3
"""
render_outcomes_dashboard — the system health panel (the "battleship" view).

Renders docs/outcomes_ledger.json (what the code's decision surface IS, harvested
by scripts/extract_outcomes.py) FUSED with docs/terminal_coverage.json (what the
test suite actually EXECUTES, measured by scripts/measure_terminal_coverage.py)
into one self-contained page: docs/outcomes_dashboard.html.

Every terminal is classified on two orthogonal axes:

  outcome  — proven / refused / broke / degraded / loop / vanished  (what it IS)
  coverage — verified   executed AND named in a test  (triggered + asserted)
             exercised  executed but not named        (runs, no code-specific check)
             dark       never executed by any test    (the real hardening worklist)

It is a PROJECTION OF THE CODE + THE TEST RUN, not a hand-drawn diagram — so it
can't rot or flatter us. If the coverage overlay is absent, the panel says so
loudly and falls back to the weak grep signal.

Deterministic, fully escaped, no network/CDN. Regenerate:

    python scripts/extract_outcomes.py            # refresh the ledger (fast/static)
    python scripts/measure_terminal_coverage.py   # measure real coverage + re-render
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "outcomes_ledger.json"
OVERLAY = ROOT / "docs" / "terminal_coverage.json"
OUT = ROOT / "docs" / "outcomes_dashboard.html"

# The Seaworthy milestone scope lives in a sibling module (single source of
# truth). Import it whether we're run as a script or loaded via importlib.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seaworthy_scope as _sc  # noqa: E402

# outcome class → (glyph, css, meaning). Order = display order.
OUTCOMES = {
    "proven":   ("✅", "ok",    "validated success"),
    "refused":  ("⛔", "cyan",  "clean gate, before any side-effect"),
    "broke":    ("💥", "bad",   "hard failure, recorded"),
    "degraded": ("⚠️", "warn",  "proceeded with reduced assurance"),
    "loop":     ("🔁", "loop",  "recoverable, feeds a retry"),
    "vanished": ("👻", "ghost", "failure with NO durable trace — an honesty hole"),
}
# coverage state → (glyph, css, meaning)
COVER = {
    "verified":  ("✓", "cov-v", "executed by a test AND named — triggered + asserted"),
    "exercised": ("~", "cov-e", "executed, but no test names it — runs, unasserted"),
    "dark":      ("✗", "cov-d", "never executed by any test — the hardening worklist"),
    "unknown":   ("?", "cov-u", "coverage not measured"),
}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _subsystem(code) -> str:
    return code.split(".", 1)[0] if code else "«untagged»"


def _cover_state(entry: dict, overlay: dict | None) -> str:
    if overlay is None:
        return "unknown"
    ex = overlay.get(entry["where"])
    if ex is None:
        # measured run existed but this exact line wasn't in it → treat as dark
        # (it did not execute); only a wholly-absent overlay is "unknown".
        return "dark"
    if ex is False:
        return "dark"
    return "verified" if entry.get("named_in_test") else "exercised"


def _counts(entries, key):
    c = {}
    for e in entries:
        c[e[key]] = c.get(e[key], 0) + 1
    return c


def _outcome_bar(entries) -> str:
    total = len(entries) or 1
    segs = []
    cc = _counts(entries, "outcome")
    for oc in OUTCOMES:
        n = cc.get(oc, 0)
        if n:
            segs.append(f'<span class="seg {oc}" style="width:{100*n/total:.3f}%" '
                        f'title="{oc}: {n}"></span>')
    return f'<div class="bar">{"".join(segs)}</div>'


def _source_stamp() -> str:
    """The commit this page describes, + whether the tree was dirty when it was
    rendered. A number with no idea what code it refers to is how docs rot: the
    ledger is a CACHE of the code, and a stale cache renders a confident fiction
    (tier 7 caught the ledger listing 3 terminals that no longer existed while
    omitting 3 live cache-gates). The stamp lets a reader check, instead of
    trusting. Kept OUT of render() so render stays pure + deterministic."""
    import subprocess
    def _git(*a):
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    sha = _git("rev-parse", "--short", "HEAD")
    if not sha:
        return "source commit UNKNOWN (not a git checkout)"
    dirty = bool(_git("status", "--porcelain"))
    return f"rendered from {sha}{' + uncommitted changes' if dirty else ''}"


def render(entries: list[dict], overlay: dict | None, source_stamp: str = "",
           carried: dict | None = None) -> str:
    total = len(entries)
    for e in entries:
        e["_cov"] = _cover_state(e, overlay)

    tally = _counts(entries, "outcome")
    cov = _counts(entries, "_cov")
    verified = cov.get("verified", 0)
    exercised = cov.get("exercised", 0)
    dark = cov.get("dark", 0)
    covered = verified + exercised
    covered_pct = round(100 * covered / total) if total else 0

    untagged = [e for e in entries if not e.get("tagged")]
    vanished = [e for e in entries if e["outcome"] == "vanished"]
    subs = sorted({_subsystem(e.get("code")) for e in entries})

    by_sub: dict[str, list[dict]] = {}
    for e in entries:
        by_sub.setdefault(_subsystem(e.get("code")), []).append(e)

    def darkfail(rows):
        return sum(1 for e in rows
                   if e["_cov"] == "dark" and e["outcome"] in ("broke", "vanished"))

    # hardening priority: most DARK failure paths first, then most dark, then size.
    order = sorted(by_sub, key=lambda n: (-darkfail(by_sub[n]),
                                          -sum(1 for e in by_sub[n] if e["_cov"] == "dark"),
                                          -len(by_sub[n]), n))

    measured = overlay is not None
    # THE OVERLAY IS KEYED ON `file:line`, SO IT GOES STALE ON LINE MOTION ALONE.
    # `_cover_state` maps a key the overlay has never heard of to "dark" — the right call
    # for one row, and a fabricated blackout when a whole file shifted down by two lines.
    # Measured on real history: the ledger at 4adf7c8 joined against the overlay from
    # 513fbbd renders 95/149/310 where the truth is 129/160/265 — a 45-terminal phantom
    # regression on a page whose entire purpose is to be believed. A CI-blocking test
    # (test_coverage_overlay_keys_resolve_against_the_ledger) stops that reaching main,
    # so this banner is not the trust boundary; it is for the hours BEFORE the push, when
    # the page is the only thing you are reading and it is quietly lying to you. The
    # freeze grid has had its own staleness banner all along — this one did not.
    unjoined = sorted({e["where"] for e in entries} - set(overlay)) if measured else []
    if not measured:
        banner = ('<div class="nocov">⚠ coverage NOT measured — showing the weak grep '
                  'signal only. Run <code>python scripts/measure_terminal_coverage.py'
                  '</code> for the real verified / exercised / dark picture.</div>')
    elif unjoined:
        banner = (f'<div class="nocov">⚠ the coverage overlay does not match this ledger — '
                  f'<b>{len(unjoined)} of {total}</b> terminals have no measurement and are '
                  f'counted DARK below, which understates coverage by up to that many. The '
                  f'overlay is keyed on <code>file:line</code>, so moving a line invalidates '
                  f'it even when nothing about the code\'s behaviour changed. Re-run '
                  f'<code>python scripts/measure_terminal_coverage.py</code> (~3.5 min) '
                  f'before reading these numbers. First unjoined: '
                  f'<code>{_e(unjoined[0])}</code>.</div>')
    elif carried:
        # A CARRIED overlay joins perfectly — that is what the re-key is for — so the
        # staleness check above cannot see it. Without this branch the re-key would be a
        # SILENT under-report, i.e. it would introduce the exact defect the branch above
        # was just added to remove. The numbers are still honest (unmeasured terminals are
        # counted dark, never lit), but "honest" and "silent about its own provenance"
        # are not the same thing.
        n_unmeasured = len(carried.get("unmeasured") or [])
        banner = (f'<div class="nocov">⚑ these verdicts were <b>carried</b>, not measured '
                  f'here. Last observed at <code>{_e(carried.get("measured_sha"))}</code> '
                  f'and re-keyed onto <code>{_e(carried.get("rekeyed_at"))}</code> across '
                  f'{carried.get("moved_lines", 0)} moved line(s)'
                  + (f'; <b>{n_unmeasured}</b> terminal(s) had no transferable verdict and '
                     f'are counted DARK, so real coverage is at least this good and may be '
                     f'better' if n_unmeasured else '')
                  + f'. Run <code>python scripts/measure_terminal_coverage.py</code> '
                    f'(~3.5 min) for an observed number.</div>')
    else:
        banner = ""

    # ---- coverage headline bar (verified | exercised | dark) --------------
    def seg(pct, cls):
        return f'<span class="cseg {cls}" style="width:{pct:.3f}%"></span>' if pct else ""
    cbar = (seg(100*verified/total if total else 0, "cov-v")
            + seg(100*exercised/total if total else 0, "cov-e")
            + seg(100*dark/total if total else 0, "cov-d"))

    # ---- SEAWORTHY v1 meter (the milestone needle) ------------------------
    # Mark every terminal so the cards can badge load-bearing + certified.
    for e in entries:
        e["_lb"] = _sc.is_load_bearing(e)
        e["_arena"] = _sc.arena(e)
        e["_cert"] = e["_lb"] and _sc.is_certified(e, overlay)
    m = _sc.summarize(entries, overlay)
    sw_done = m["seaworthy_v1"]
    sw_pct = m["local_pct"]
    seaworthy = (
        f'<div class="sea {"sea-done" if sw_done else ""}">'
        f'<div class="sea-h">⚓ SEAWORTHY v1 '
        f'<span class="sea-sub">— certify the LOAD-BEARING surface an autonomous '
        f'agent trusts (every proven + every honesty gate + every firewall)</span></div>'
        f'<div class="sea-row">'
        f'<div class="sea-num">{m["local_certified"]} / {m["local_total"]}'
        f'<span class="sea-pct"> · {sw_pct}% local certified</span></div>'
        f'<div class="sea-bar"><span style="width:{sw_pct}%"></span></div>'
        f'</div>'
        f'<div class="sea-foot">'
        f'{m["local_total"] - m["local_certified"]} local load-bearing terminals to certify '
        f'· HPC (v2, needs a cluster): {m["hpc_certified"]}/{m["hpc_total"]} '
        f'· {"✅ SEA TRIAL READY" if sw_done else "milestone open"}'
        f'</div></div>')

    # ---- hole tiles -------------------------------------------------------
    tiles = [
        ("VANISHED", len(vanished), "ghost", "failures with no durable trace"),
        ("UNTAGGED", len(untagged), "bad", "terminals the model can't classify"),
        ("DARK", dark, "warn", "never executed by any test — hardening worklist"),
    ]
    tiles_html = "".join(
        f'<div class="tile {"tile-zero" if n == 0 else cls}">'
        f'<div class="tn">{n}</div><div class="tl">{_e(label)}</div>'
        f'<div class="td">{_e(desc)}</div></div>'
        for label, n, cls, desc in tiles)

    tally_html = "".join(
        f'<span class="chip {oc}">{g} {_e(oc)} {tally.get(oc, 0)}</span>'
        for oc, (g, _c, _m) in OUTCOMES.items())

    # ---- subsystem cards --------------------------------------------------
    cards = []
    for name in order:
        rows = by_sub[name]
        n = len(rows)
        cs = _counts(rows, "_cov")
        v, x, dk = cs.get("verified", 0), cs.get("exercised", 0), cs.get("dark", 0)
        covcls = "cov-good" if (v + x) / n >= 0.66 else "cov-mid" if (v + x) / n >= 0.33 else "cov-low"
        rows_sorted = sorted(
            rows, key=lambda e: (["dark", "exercised", "verified", "unknown"].index(e["_cov"]),
                                 list(OUTCOMES).index(e["outcome"]) if e["outcome"] in OUTCOMES else 99,
                                 e.get("code") or e.get("where")))
        lis = []
        for e in rows_sorted:
            oc = e["outcome"]
            og = OUTCOMES.get(oc, ("•",))[0]
            st = e["_cov"]
            cg, ccls, _m = COVER[st]
            code = e.get("code") or "«untagged raw terminal»"
            lb = e.get("_lb")
            cert = e.get("_cert")
            anchor = ('<span class="lb" title="load-bearing — Seaworthy scope">⚓</span>'
                      if lb else '<span class="lb0"></span>')
            lis.append(
                f'<li class="{st}" data-lb="{1 if lb else 0}" '
                f'data-cert="{1 if cert else 0}" data-arena="{e.get("_arena","")}">'
                f'<span class="g">{og}</span>{anchor}'
                f'<code class="cd">{_e(code)}</code>'
                f'<span class="cbadge {ccls}">{cg} {_e(st)}</span>'
                f'<span class="wh">{_e(e.get("where"))}</span></li>')
        df = darkfail(rows)
        cards.append(
            f'<details class="card" data-dark="{dk}">'
            f'<summary>'
            f'<span class="sname">{_e(name)}</span>'
            f'<span class="scount">{n}</span>'
            f'{_outcome_bar(rows)}'
            f'<span class="cov3"><b class="cov-v">{v}</b>·<b class="cov-e">{x}</b>·'
            f'<b class="cov-d">{dk}</b></span>'
            f'<span class="scov {covcls}">{v+x}/{n} run</span>'
            f'{f"<span class=df>{df} dark ✗</span>" if df else "<span class=df0>0 dark fails</span>"}'
            f'</summary>'
            f'<ul class="terms">{"".join(lis)}</ul></details>')

    legend_out = "".join(
        f'<span class="lg"><span class="chip {oc}">{g}</span><b>{_e(oc)}</b> — {_e(m)}</span>'
        for oc, (g, _c, m) in OUTCOMES.items())
    legend_cov = "".join(
        f'<span class="lg"><span class="cbadge {c}">{g} {_e(k)}</span> {_e(m)}</span>'
        for k, (g, c, m) in COVER.items() if k != "unknown")

    return _PAGE.format(
        total=total, subs=len(subs), covered=covered, covered_pct=covered_pct,
        verified=verified, exercised=exercised, dark=dark,
        cbar=cbar, banner=banner, seaworthy=seaworthy, tiles=tiles_html, tally=tally_html,
        legend_out=legend_out, legend_cov=legend_cov, cards="".join(cards),
        source_stamp=_e(source_stamp or "source commit not stamped"),
        measured=("measured" if measured else "NOT measured"))


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>System Decision Surface — Outcome + Coverage Health Panel</title>
<style>
:root{{
  --bg:#0a0c14;--surface:#13151f;--surface-2:#1a1d29;--border:#262a3a;
  --cyan:#22e3ee;--yellow:#fff200;--ink:#e6e9f0;--muted:#8e98ad;
  --ok:#3ce086;--bad:#ff4b6e;--warn:#ffa940;--loop:#b37bff;--ghost:#ff3df0;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--mono);padding:26px 24px 60px}}
h1{{font-size:22px;font-weight:800;color:var(--yellow);letter-spacing:.02em;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin:0 0 18px}} .sub b{{color:var(--cyan)}}
.nocov{{border:1px solid var(--warn);color:var(--warn);background:rgba(255,169,64,.08);
  padding:10px 14px;margin:0 0 18px;font-size:12px}} .nocov code{{color:var(--ink)}}
.stats{{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 20px}}
.big{{border:1px solid var(--border);background:var(--surface);padding:14px 18px;min-width:110px}}
.big .n{{font-size:30px;font-weight:800;color:var(--cyan);line-height:1}}
.big .l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.13em;color:var(--muted);margin-top:6px}}
.covwrap{{flex:1;min-width:280px;border:1px solid var(--border);background:var(--surface);padding:14px 18px}}
.covwrap .l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.13em;color:var(--muted)}}
.covnum{{font-size:20px;font-weight:800;margin:6px 0}}
.cbar{{height:14px;background:var(--surface-2);display:flex;overflow:hidden}}
.cseg{{height:100%}} .cseg.cov-v{{background:var(--ok)}} .cseg.cov-e{{background:var(--warn)}}
.cseg.cov-d{{background:var(--bad)}}
.ckey{{font-size:11px;color:var(--muted);margin-top:7px}}
.ckey b.cov-v{{color:var(--ok)}} .ckey b.cov-e{{color:var(--warn)}} .ckey b.cov-d{{color:var(--bad)}}
.tiles{{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 24px}}
.tile{{flex:1;min-width:180px;border:1px solid var(--border);border-left-width:4px;background:var(--surface);padding:14px 18px}}
.tile .tn{{font-size:30px;font-weight:800;line-height:1}} .tile .tl{{font-size:11px;text-transform:uppercase;letter-spacing:.14em;margin-top:6px}}
.tile .td{{font-size:11px;color:var(--muted);margin-top:5px}}
.tile.ghost{{border-left-color:var(--ghost)}} .tile.ghost .tn{{color:var(--ghost)}}
.tile.bad{{border-left-color:var(--bad)}} .tile.bad .tn{{color:var(--bad)}}
.tile.warn{{border-left-color:var(--warn)}} .tile.warn .tn{{color:var(--warn)}}
.tile.tile-zero{{border-left-color:var(--ok)}} .tile.tile-zero .tn{{color:var(--ok)}}
.chip{{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:2px;margin:0 6px 6px 0;border:1px solid var(--border);background:var(--surface-2)}}
.chip.proven{{color:var(--ok)}} .chip.refused{{color:var(--cyan)}} .chip.broke{{color:var(--bad)}}
.chip.degraded{{color:var(--warn)}} .chip.loop{{color:var(--loop)}} .chip.vanished{{color:var(--ghost)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin:4px 0 8px;font-size:11.5px;color:var(--muted)}}
.legend b{{color:var(--ink)}} .legend.cov{{margin-bottom:20px}}
.cbadge{{display:inline-block;font-size:10.5px;padding:1px 7px;border-radius:2px;border:1px solid var(--border)}}
.cbadge.cov-v{{color:var(--ok);border-color:rgba(60,224,134,.4)}}
.cbadge.cov-e{{color:var(--warn);border-color:rgba(255,169,64,.4)}}
.cbadge.cov-d{{color:var(--bad);border-color:rgba(255,75,110,.45);background:rgba(255,75,110,.08)}}
.cbadge.cov-u{{color:var(--muted)}}
.toolbar{{margin:0 0 14px}}
.toolbar button{{font:inherit;font-size:11px;color:var(--ink);background:var(--surface-2);border:1px solid var(--border);padding:6px 12px;cursor:pointer;margin-right:8px}}
.toolbar button:hover{{border-color:var(--cyan);color:var(--cyan)}}
.toolbar button.on{{border-color:var(--yellow);color:var(--yellow)}}
.card{{border:1px solid var(--border);background:var(--surface);margin:0 0 8px}}
.card>summary{{list-style:none;cursor:pointer;display:grid;grid-template-columns:170px 40px 1fr 78px 74px 96px;gap:12px;align-items:center;padding:11px 16px}}
.card>summary::-webkit-details-marker{{display:none}}
.card>summary:hover{{background:var(--surface-2)}}
.sname{{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.scount{{color:var(--muted);text-align:right;font-size:12px}}
.bar{{height:12px;background:var(--surface-2);display:flex;overflow:hidden;min-width:100px}}
.seg{{height:100%}} .seg.proven{{background:var(--ok)}} .seg.refused{{background:var(--cyan)}}
.seg.broke{{background:var(--bad)}} .seg.degraded{{background:var(--warn)}} .seg.loop{{background:var(--loop)}} .seg.vanished{{background:var(--ghost)}}
.cov3{{font-size:12px;white-space:nowrap;text-align:right}} .cov3 b{{font-weight:800}}
.cov3 b.cov-v{{color:var(--ok)}} .cov3 b.cov-e{{color:var(--warn)}} .cov3 b.cov-d{{color:var(--bad)}}
.scov{{font-size:11px;white-space:nowrap;padding:2px 8px;border:1px solid var(--border);text-align:center}}
.cov-good{{color:var(--ok)}} .cov-mid{{color:var(--warn)}} .cov-low{{color:var(--bad)}}
.df{{font-size:11px;color:var(--bad);white-space:nowrap;text-align:right}}
.df0{{font-size:11px;color:var(--muted);white-space:nowrap;text-align:right}}
ul.terms{{list-style:none;margin:0;padding:4px 0 10px;border-top:1px solid var(--border)}}
ul.terms li{{display:grid;grid-template-columns:22px 16px 1fr 116px auto;gap:12px;align-items:center;padding:4px 16px}}
ul.terms li:hover{{background:var(--surface-2)}}
ul.terms li.dark{{background:rgba(255,75,110,.05)}}
.g{{text-align:center}}
.cd{{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
li.dark .cd{{color:#ffd7de}}
.wh{{color:var(--muted);font-size:11px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
body.only-dark li:not(.dark){{display:none}}
body.only-dark details[data-dark="0"]{{display:none}}
body.only-lb li:not([data-lb="1"]){{display:none}}
body.only-lb li[data-cert="1"]{{display:none}}
/* Seaworthy milestone banner */
.sea{{border:2px solid var(--cyan);background:linear-gradient(180deg,rgba(34,227,238,.06),transparent);
  padding:16px 20px;margin:0 0 22px}}
.sea.sea-done{{border-color:var(--ok)}}
.sea-h{{font-size:15px;font-weight:800;color:var(--cyan);letter-spacing:.03em}}
.sea.sea-done .sea-h{{color:var(--ok)}}
.sea-sub{{font-weight:400;color:var(--muted);font-size:11.5px;letter-spacing:0}}
.sea-row{{display:flex;align-items:center;gap:18px;margin:12px 0 6px}}
.sea-num{{font-size:26px;font-weight:800;color:var(--ink);white-space:nowrap}}
.sea-pct{{font-size:13px;font-weight:400;color:var(--muted)}}
.sea-bar{{flex:1;height:14px;background:var(--surface-2);overflow:hidden;min-width:160px}}
.sea-bar>span{{display:block;height:100%;background:linear-gradient(90deg,var(--cyan),var(--ok))}}
.sea-foot{{font-size:11.5px;color:var(--muted)}}
.lb{{color:var(--cyan);font-size:11px;text-align:center}} .lb0{{display:inline-block}}
.foot{{color:var(--muted);font-size:11px;margin-top:26px;border-top:1px solid var(--border);padding-top:14px}}
</style></head><body>
<h1>System Decision Surface</h1>
<p class="sub">Outcome + coverage health panel — a <b>projection of the code</b>
(harvested by extract_outcomes.py) fused with <b>real test execution</b> ({measured}
by measure_terminal_coverage.py). Every terminal is a place the system succeeds,
refuses, or fails. Nothing is hand-drawn.</p>
{banner}
{seaworthy}
<div class="stats">
  <div class="big"><div class="n">{total}</div><div class="l">terminals</div></div>
  <div class="big"><div class="n">{subs}</div><div class="l">subsystems</div></div>
  <div class="covwrap"><div class="l">real test coverage (executed)</div>
    <div class="covnum">{covered} / {total} &nbsp;·&nbsp; {covered_pct}% run by tests</div>
    <div class="cbar">{cbar}</div>
    <div class="ckey"><b class="cov-v">{verified}</b> verified &nbsp;·&nbsp;
      <b class="cov-e">{exercised}</b> exercised &nbsp;·&nbsp;
      <b class="cov-d">{dark}</b> dark</div>
  </div>
</div>
<div class="tiles">{tiles}</div>
<div class="legend">{tally}</div>
<div class="legend">{legend_out}</div>
<div class="legend cov">{legend_cov}</div>
<div class="toolbar">
  <button onclick="document.querySelectorAll('details.card').forEach(d=>d.open=true)">expand all</button>
  <button onclick="document.querySelectorAll('details.card').forEach(d=>d.open=false)">collapse all</button>
  <button id="ud" onclick="toggleDark(this)">show only dark (worklist)</button>
  <button id="ul" onclick="toggleLB(this)">⚓ show only uncertified load-bearing (Seaworthy v1 worklist)</button>
</div>
{cards}
<p class="foot">Cards are ordered by <b>hardening priority</b>: most DARK failure
paths (broke/vanished never executed) first. The <b>v·e·d</b> column is
verified·exercised·dark per subsystem. Regenerate:
<code>python scripts/extract_outcomes.py &amp;&amp; python scripts/measure_terminal_coverage.py</code><br>
<b>{source_stamp}</b> — this page describes THAT code and nothing else. If it is
stale, every count above is a confident fiction; the ledger is a cache of the
source, so regenerate before believing it.
(<code>tests/test_outcome_tags.py</code> makes staleness a build failure.)</p>
<script>
function toggleDark(btn){{
  document.body.classList.toggle('only-dark');
  btn.classList.toggle('on');
  if(document.body.classList.contains('only-dark'))
    document.querySelectorAll('details.card').forEach(d=>{{if(d.dataset.dark!=='0')d.open=true;}});
}}
function toggleLB(btn){{
  const on=document.body.classList.toggle('only-lb');
  btn.classList.toggle('on');
  // a card is relevant if it has any uncertified load-bearing li
  document.querySelectorAll('details.card').forEach(d=>{{
    const has=[...d.querySelectorAll('li')].some(li=>li.dataset.lb==='1'&&li.dataset.cert==='0');
    d.style.display = (on&&!has)?'none':'';
    if(on&&has) d.open=true;
  }});
}}
</script>
</body></html>"""


def main() -> int:
    if not LEDGER.exists():
        print(f"  ! {LEDGER} not found — run scripts/extract_outcomes.py first",
              file=sys.stderr)
        return 1
    entries = json.loads(LEDGER.read_text())
    overlay = carried = None
    if OVERLAY.exists():
        doc = json.loads(OVERLAY.read_text())
        overlay = doc.get("by_where")
        carried = doc.get("carried_forward")
    OUT.write_text(render(entries, overlay, _source_stamp(), carried))
    if overlay is None:
        print(f"  wrote {OUT.relative_to(ROOT)}  (coverage NOT measured — grep only; "
              f"run measure_terminal_coverage.py)")
    else:
        cov = {}
        for e in entries:
            e["_cov"] = _cover_state(e, overlay)
            cov[e["_cov"]] = cov.get(e["_cov"], 0) + 1
        print(f"  wrote {OUT.relative_to(ROOT)}  ({len(entries)} terminals · "
              f"{cov.get('verified',0)} verified · {cov.get('exercised',0)} exercised · "
              f"{cov.get('dark',0)} dark)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
