#!/usr/bin/env python3
"""
render_freeze_tier_grid — the freeze-tier BREADTH map (docs/freeze_tier_grid.html).

Renders docs/freeze_tier_coverage.json (real container-build results, measured by
scripts/measure_freeze_tier_coverage.py) into one self-contained page: for every
freeze tier, whether a REAL docker build has baked it and passed the honesty
contract, its measured pass-rate vs its reviewed floor, and the probe tool + last
error. It is a PROJECTION of the measured JSON — nothing hand-drawn, so it can't
flatter us.

This meter is "tiers with a real green bake." It is deliberately NOT the intent
grid (resolver decisions) nor the outcomes dashboard (terminal coverage) — three
orthogonal maps. Real builds can't run on the free CI, so this page can go STALE
between opt-in generator runs; it leads with the measured-at sha + a loud banner
when that sha != HEAD, so a reader checks instead of trusting.

Deterministic, fully escaped, no network/CDN. Regenerate:

    python scripts/measure_freeze_tier_coverage.py   # re-measure (real Docker) + re-render
"""
from __future__ import annotations

import html
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "freeze_tier_coverage.json"
OUT = ROOT / "docs" / "freeze_tier_grid.html"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freeze_tiers as ft  # noqa: E402

STATUS = {
    "proven":     ("✅", "ok",   "a real docker build baked it AND check_build was clean"),
    "broke":      ("💥", "bad",  "a real build ran but failed the honesty contract"),
    "unmeasured": ("·",  "muted", "no real build yet (declared row, or Docker was down)"),
}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _rate(rec: dict) -> float | None:
    if rec.get("attempts"):
        return rec["passed"] / rec["attempts"]
    return None


def _row_class(tier: str, rec: dict) -> str:
    """below-floor is a REGRESSION (red) even if the tier once passed; a floored
    tier that reads unmeasured is also suspect (a floor should be earned)."""
    floor = ft.FLOORS.get(tier, 0.0)
    r = _rate(rec)
    if rec["status"] == "proven" and (r is None or r >= floor):
        return "ok"
    if rec["status"] == "broke":
        return "bad"
    if rec["status"] == "unmeasured" and floor > 0:
        return "bad"          # claims a floor but has no measurement backing it
    if r is not None and r < floor:
        return "bad"
    return "muted"


def render(data: dict, current_sha: str = "") -> str:
    tiers = data.get("tiers") or {}
    install = [t for t in ft.INSTALL_TIERS if t in tiers]
    methods = [t for t in ft.BUILD_METHODS if t in tiers]
    proven_install = sum(1 for t in install if tiers[t]["status"] == "proven")
    total_install = len(ft.INSTALL_TIERS)

    measured_sha = data.get("git_sha") or "unknown"
    stale = bool(current_sha) and measured_sha not in ("unknown", "") and \
        measured_sha != current_sha
    empty = not tiers
    if empty:
        banner = ('<div class="warn">⚠ NOT measured — no committed results. Run '
                  '<code>python scripts/measure_freeze_tier_coverage.py</code>.</div>')
    elif stale:
        banner = (f'<div class="warn">⚠ measured at <b>{_e(measured_sha)}</b>, but HEAD is '
                  f'<b>{_e(current_sha)}</b> — if any build code changed since, re-run '
                  f'<code>python scripts/measure_freeze_tier_coverage.py</code>. Real '
                  f'builds can\'t run in CI, so this page only refreshes on an opt-in run.</div>')
    else:
        banner = ""

    def _bar(tier: str, rec: dict) -> str:
        r = _rate(rec)
        floor = ft.FLOORS.get(tier, 0.0)
        pct = 0.0 if r is None else 100.0 * r
        fill = _row_class(tier, rec)
        floor_mark = (f'<span class="floormark" style="left:{100*floor:.1f}%" '
                      f'title="floor {floor:g}"></span>' if floor > 0 else "")
        return (f'<div class="bar"><span class="fill {fill}" '
                f'style="width:{pct:.1f}%"></span>{floor_mark}</div>')

    def _rows(names: list[str]) -> str:
        out = []
        for t in names:
            rec = tiers[t]
            g, cls, _ = STATUS.get(rec["status"], ("?", "muted", ""))
            floor = ft.FLOORS.get(t, 0.0)
            r = _rate(rec)
            rate_txt = "—" if r is None else f"{r:.0%}"
            locus = rec.get("validation_locus")
            locus_txt = f' · {_e(locus)}' if locus else ""
            probe = rec.get("probe_tool") or "—"
            detail = rec.get("last_error") or rec.get("note") or ""
            out.append(
                f'<tr class="{_row_class(t, rec)}">'
                f'<td class="tier"><span class="g">{g}</span>{_e(t)}</td>'
                f'<td class="probe">{_e(probe)}</td>'
                f'<td class="status {cls}">{_e(rec["status"])}{locus_txt}</td>'
                f'<td class="ratecell">{_bar(t, rec)}<span class="ratenum">{rate_txt}'
                f'</span></td>'
                f'<td class="floor">{floor:g}</td>'
                f'<td class="detail">{_e(detail)}</td>'
                f'</tr>')
        return "".join(out)

    return _PAGE.format(
        banner=banner,
        proven_install=proven_install, total_install=total_install,
        methods_n=len(ft.BUILD_METHODS),
        measured_on=_e(data.get("measured_on") or "—"),
        measured_sha=_e(measured_sha),
        platform=_e(data.get("platform") or "—"),
        host_arch=_e(data.get("host_arch") or "—"),
        docker=("yes" if data.get("docker_available") else "no"),
        install_rows=_rows(install),
        method_rows=_rows(methods),
    )


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Freeze Tier Coverage — real container-build breadth</title>
<style>
:root{{
  --bg:#0a0c14;--surface:#13151f;--surface-2:#1a1d29;--border:#262a3a;
  --cyan:#22e3ee;--yellow:#fff200;--ink:#e6e9f0;--muted:#8e98ad;
  --ok:#3ce086;--bad:#ff4b6e;--warn:#ffa940;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--mono);padding:26px 24px 60px}}
h1{{font-size:22px;font-weight:800;color:var(--yellow);letter-spacing:.02em;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin:0 0 18px;max-width:900px}} .sub b{{color:var(--cyan)}}
.warn{{border:1px solid var(--warn);color:var(--warn);background:rgba(255,169,64,.08);
  padding:10px 14px;margin:0 0 18px;font-size:12px}} .warn code{{color:var(--ink)}} .warn b{{color:var(--ink)}}
.stats{{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 22px}}
.big{{border:1px solid var(--border);background:var(--surface);padding:14px 18px;min-width:120px}}
.big .n{{font-size:30px;font-weight:800;color:var(--cyan);line-height:1}}
.big .n b{{color:var(--ok)}}
.big .l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.13em;color:var(--muted);margin-top:6px}}
.meta{{flex:1;min-width:280px;border:1px solid var(--border);background:var(--surface);padding:14px 18px;font-size:12px;color:var(--muted)}}
.meta b{{color:var(--ink)}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);margin:22px 0 8px;font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;
  padding:6px 10px;border-bottom:1px solid var(--border);font-weight:600}}
td{{padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr.ok .tier{{color:var(--ok)}} tr.bad .tier{{color:var(--bad)}} tr.muted .tier{{color:var(--muted)}}
.tier{{font-weight:700;white-space:nowrap}} .g{{margin-right:7px}}
.probe{{color:var(--ink)}}
.status{{white-space:nowrap;font-size:12px}}
.status.ok{{color:var(--ok)}} .status.bad{{color:var(--bad)}} .status.muted{{color:var(--muted)}}
.ratecell{{min-width:180px}}
.bar{{position:relative;height:12px;background:var(--surface-2);overflow:hidden;display:inline-block;
  width:130px;vertical-align:middle;margin-right:8px}}
.fill{{display:block;height:100%}} .fill.ok{{background:var(--ok)}} .fill.bad{{background:var(--bad)}}
.fill.muted{{background:var(--surface-2)}}
.floormark{{position:absolute;top:-2px;height:16px;width:2px;background:var(--yellow)}}
.ratenum{{font-size:12px;color:var(--muted)}}
.floor{{text-align:center;color:var(--muted)}}
.detail{{color:var(--muted);font-size:11.5px;max-width:520px}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0 0;font-size:11.5px;color:var(--muted)}}
.legend b{{color:var(--ink)}}
.foot{{color:var(--muted);font-size:11px;margin-top:26px;border-top:1px solid var(--border);padding-top:14px;max-width:900px}}
.foot code{{color:var(--ink)}}
</style></head><body>
<h1>Freeze Tier Coverage</h1>
<p class="sub">The freeze BREADTH map — for every install tier + build method, has a
<b>real container-native docker build</b> baked it and passed the honesty contract
(<b>check_build == BUILT · VALIDATED_IN_IMAGE · POLICY_CLEAN</b>)? This is a
projection of measured build results, not the intent grid (resolver decisions) or
the outcomes dashboard (terminal coverage). The meter that matters: <b>tiers with a
real green bake</b>.</p>
{banner}
<div class="stats">
  <div class="big"><div class="n"><b>{proven_install}</b> / {total_install}</div>
    <div class="l">install tiers proven by a real build</div></div>
  <div class="big"><div class="n">{methods_n}</div>
    <div class="l">adopt / authors build methods</div></div>
  <div class="meta">
    measured <b>{measured_on}</b> @ <b>{measured_sha}</b><br>
    platform <b>{platform}</b> · host arch <b>{host_arch}</b> · Docker at measure time: <b>{docker}</b><br>
    The yellow tick on a bar is the tier's reviewed <b>floor</b> (the ratchet — it only rises).
  </div>
</div>
<h2>Install tiers — proven by a container-native build</h2>
<table>
<tr><th>tier</th><th>probe tool</th><th>status</th><th>pass-rate · floor</th><th>floor</th><th>detail / last error</th></tr>
{install_rows}
</table>
<h2>Build methods — adopt / authors (no reconstruction)</h2>
<table>
<tr><th>method</th><th>probe tool</th><th>status</th><th>pass-rate · floor</th><th>floor</th><th>detail / last error</th></tr>
{method_rows}
</table>
<div class="legend">
  <span><b>✅ proven</b> — real build baked it + check_build clean</span>
  <span><b>💥 broke</b> — real build failed the contract</span>
  <span><b>· unmeasured</b> — no real build yet (declared row, or Docker down)</span>
</div>
<p class="foot">A tier is <b>proven</b> only when a real docker build returned success
AND <code>env_honesty.check_build</code> returned no violations — never on a
Docker-less run (that would read green while proving nothing). Floors are a reviewed
ratchet in <code>scripts/freeze_tiers.py</code> (raised by a human, never by the
generator); <code>tests/test_freeze_tier_coverage.py</code> asserts
<code>floor ≤ measured</code> and makes an unbacked floor a build failure. Regenerate:
<code>python scripts/measure_freeze_tier_coverage.py</code>. Real builds can't run in
CI, so if the measured sha above isn't HEAD, re-run before trusting these bars.</p>
</body></html>"""


def _current_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def write(data: dict) -> Path:
    OUT.write_text(render(data, _current_sha()))
    return OUT


def main() -> int:
    if not DATA.exists():
        print(f"  ! {DATA} not found — run scripts/measure_freeze_tier_coverage.py first",
              file=sys.stderr)
        return 1
    data = json.loads(DATA.read_text())
    write(data)
    print(f"  wrote {OUT.relative_to(ROOT)}  "
          f"({data.get('install_tiers_proven', '?')}/{data.get('install_tiers_total','?')} "
          f"install tiers proven)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
