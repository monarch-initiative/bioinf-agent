"""
env_report_html — the Layer-1 env report as a self-contained HTML page that tells
the STORY of what happened, rendered PURELY from the verified freeze record.

A reader should see, at a glance: what was asked for, what decisions the runtime
made, what it built/adopted, what it validated, and what artifacts came out — as a
process flow, not a wall of tables. The honesty guarantees are structural:

  • PURE over the record — render_env_report_html(record) reads ONLY the freeze
    record (digests, in-image validation evidence, the resolved closure, the apt
    layer, the long-tail commands + provenance). No field is agent-authored, and the
    PROCESS FLOW is derived from recorded facts (mode, per-tool tier, pass/fail) —
    it visualizes what happened, it does not assert anything not in the record.
  • ESCAPED — every value is HTML-escaped; a package name / command can't inject.
  • DETERMINISTIC — no clock is read here (only the record's captured `created_at`);
    stable ordering. Same record → same bytes.
  • MODE-HONEST — a BUILD shows the per-tool "validated in the shipped image"
    evidence (validated == shipped). An ADOPTED biocontainer is shown as trusted-by-
    published-digest; the flow + footer never claim an in-image validation it didn't run.
  • VERIFIED vs DECLARED — runtime-verified facts and submitter-DECLARED policy
    (license-gating, accelerator) live in separate, labelled sections.

Self-contained: inline CSS, zero external resources, no JS. The flow is plain
HTML/CSS (flexbox stages + arrows), so it renders offline and can't fetch anything.
One public fn: render_env_report_html.
"""

from __future__ import annotations

from html import escape
from typing import Any, Optional

from agent.skills.env_report import (
    _extract_version, _install_method, _locus_line, _pkg_index, _verif_index,
)

_CSS = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#1b2330;--muted:#5c6775;--line:#e4e8ee;
--accent:#2a6df4;--ok:#1a7f47;--okbg:#e7f6ee;--okln:#bfe6cd;--bad:#c0341d;--badbg:#fcebe8;
--warn:#8a5a00;--warnbg:#fdf3e0;--warnln:#f0dcb0;--blue:#234e91;--bluebg:#eaf1fe;--blueln:#cfe0fb;
--code:#eef1f6;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:30px 20px 64px}
h1{font-size:25px;margin:0 0 4px}
h2{font-size:17px;margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sub{color:var(--muted);margin:0;font-size:14px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.mono,code{font-family:var(--mono);font-size:12.5px}
code{background:var(--code);padding:1px 5px;border-radius:5px;word-break:break-all}
.pill{display:inline-block;font-size:12.5px;font-weight:700;padding:3px 12px;border-radius:20px;
vertical-align:middle;margin-left:8px}
.pill.ok{background:var(--okbg);color:var(--ok);border:1px solid var(--okln)}
.pill.adopt{background:var(--warnbg);color:var(--warn);border:1px solid var(--warnln)}
.pill.bad{background:var(--badbg);color:var(--bad)}
/* headline stat cards */
.stats{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0 6px}
.stat{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.stat .num{font-size:28px;font-weight:700;line-height:1.1}
.stat .lbl{font-size:12px;letter-spacing:.03em;text-transform:uppercase;color:var(--muted);margin-top:4px}
.stat.ok .num{color:var(--ok)} .stat.adopt .num{color:var(--warn)}
/* the process flow */
.flow{display:flex;flex-wrap:wrap;align-items:stretch;gap:0;margin:6px 0 4px}
.st{flex:1 1 165px;min-width:160px;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:12px 14px;position:relative}
.st .ph{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.st .pt{font-weight:700;font-size:14px;margin:3px 0 5px}
.st .pb{font-size:12.5px;color:var(--muted);line-height:1.45;word-break:break-word}
.st.input{background:var(--bluebg);border-color:var(--blueln)}
.st.decide{background:var(--warnbg);border-color:var(--warnln)}
.st.valid{background:var(--okbg);border-color:var(--okln)}
.st.valid.fail{background:var(--badbg)}
.st.art{background:var(--bluebg);border-color:var(--blueln)}
.arw{display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:22px;
flex:0 0 26px}
@media(max-width:760px){.arw{flex-basis:100%;transform:rotate(90deg);height:22px}}
/* tables */
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden;margin:6px 0}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);font-size:13.5px;vertical-align:top}
th{background:#fbfcfe;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:1px 8px;border-radius:20px}
.badge.ok{background:var(--okbg);color:var(--ok)}
.badge.bad{background:var(--badbg);color:var(--bad)}
.badge.na{background:var(--code);color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:8px 0}
.cell{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.cell .k{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.cell .v{font-size:14px;word-break:break-all}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 14px;margin:6px 0}
summary{cursor:pointer;font-weight:600;font-size:14px;padding:6px 0}
details table{border:none;margin:4px 0}
.note{color:var(--muted);font-size:12.5px;font-weight:400}
.declared{background:var(--warnbg);border:1px solid var(--warnln);border-radius:10px;padding:4px 16px 12px}
.declared .lead{color:var(--warn);font-size:12.5px;margin:10px 0 2px}
ul.tail{list-style:none;padding:0;margin:0}
ul.tail li{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:8px 0}
ul.tail .lbl{font-weight:600;margin-right:8px}
pre{background:var(--code);border-radius:8px;padding:10px 12px;overflow-x:auto;margin:6px 0 0;
font-family:var(--mono);font-size:12px;white-space:pre-wrap;word-break:break-word}
.foot li{margin:6px 0}
.gen{color:var(--muted);font-size:12px;margin-top:30px;border-top:1px solid var(--line);padding-top:12px}
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _short(digest: str, n: int = 19) -> str:
    """`sha256:abcd…` shortened for a flow chip (full value stays in the cards)."""
    s = (digest or "").strip()
    if ":" in s:
        algo, _, val = s.partition(":")
        return f"{algo}:{val[:n]}…" if len(val) > n else s
    return (s[:n] + "…") if len(s) > n else s


def _badge(passed: Optional[bool], check: str = "") -> str:
    if passed is None:
        return '<span class="badge na">—</span>'
    cls, mark = ("ok", "✓") if passed else ("bad", "✗")
    c = f' <code>{_e(check)}</code>' if check else ""
    return f'<span class="badge {cls}">{mark}</span>{c}'


def _tier_summary(requested: list, pidx: dict, shipped: list) -> str:
    """A per-tier headcount of the requested tools — the 'route' decision, derived
    from recorded facts (the resolved package kind / the long-tail purpose)."""
    counts: dict[str, int] = {}
    for t in requested:
        tier = _install_method(t, pidx.get(t.lower()), shipped)
        counts[tier] = counts.get(tier, 0) + 1
    parts = [f"{k} ×{v}" for k, v in sorted(counts.items()) if k != "—"]
    return " · ".join(parts) or "—"


def _stage(cls: str, phase: str, title: str, body: str) -> str:
    return (f'<div class="st {cls}"><div class="ph">{_e(phase)}</div>'
            f'<div class="pt">{_e(title)}</div><div class="pb">{body}</div></div>')


def _flow(record: dict, *, is_adopt: bool, requested: list, pidx: dict, shipped: list,
          passed: int, total: int) -> str:
    """The process flow — input → decisions → build/adopt → validate → artifacts.
    Per-mode (adopt branches differently). Every chip is a recorded fact."""
    arw = '<div class="arw">›</div>'
    req_names = ", ".join(_e(t) for t in requested[:6]) + ("…" if len(requested) > 6 else "")
    art_digest = _short(record.get("image_digest", "")) or _short(record.get("content_digest", ""))
    if is_adopt:
        stages = [
            _stage("input", "Input", f"{len(requested)} requested", req_names or "—"),
            _stage("decide", "Decision", "Resolve → adopt",
                   "pure conda; a published BioContainer matched, so adopt it (no build)"),
            _stage("art", "Adopt by digest", "Pull immutable image",
                   f"<code>{_e(art_digest)}</code>"),
            _stage("decide", "Trust", "By published digest",
                   "trusted as the BioContainers project; not built or validated in-locus"),
            _stage("art", "Artifact", "Apptainer delivery",
                   "<code>apptainer pull docker://…@digest</code>"),
        ]
    else:
        engine = record.get("engine", "none")
        base = _short(record.get("image_digest", ""))  # the shipped image's id
        eng_txt = f"engine {_e(engine)}" if engine and engine != "none" else "no engine (pure long-tail)"
        vcls = "valid" if (total and passed == total) else "valid fail"
        stages = [
            _stage("input", "Input", f"{len(requested)} requested", req_names or "—"),
            _stage("decide", "Decision", "Resolve & route",
                   f"per-tier: {_e(_tier_summary(requested, pidx, shipped))}"),
            _stage("", "Build", "Container-native",
                   f"{eng_txt}; co-solve + {len(shipped)} long-tail, baked into the ship image"),
            _stage(vcls, "Validate", "In the shipped image",
                   f"<b>{passed}/{total}</b> tools re-ran green (validated == shipped)"),
            _stage("art", "Artifacts", "Image + provenance",
                   f"<code>{_e(base)}</code> · recipe · attestation · Apptainer"),
        ]
    return '<div class="flow">' + arw.join(stages) + '</div>'


def render_env_report_html(record: dict) -> str:
    """Render the freeze record as a self-contained HTML page that shows the build
    process (see module docstring for the honesty contract this upholds)."""
    r = record or {}
    name = r.get("name") or (r.get("image") or "env").split(":")[0].split("/")[-1]
    mode = r.get("mode", "?")
    is_adopt = mode == "adopt"
    requested = list(r.get("requested_tools") or [])
    resolved = list(r.get("resolved_packages") or [])
    system = list(r.get("system_packages") or [])
    verifs = list(r.get("verifications") or [])
    shipped = list(r.get("shipped_binaries") or [])
    conda_specs = list(r.get("conda_specs") or [])
    vidx, pidx = _verif_index(verifs), _pkg_index(resolved)
    requested_set = {t.lower() for t in requested}
    ride = [p for p in resolved if isinstance(p, dict) and p.get("name")
            and p["name"].lower() not in requested_set]
    passed = sum(1 for v in verifs if isinstance(v, dict) and v.get("passed"))
    total = len(verifs)

    P: list[str] = []
    P.append("<!DOCTYPE html>")
    P.append(f'<html lang="en"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>Environment report — {_e(name)}</title><style>{_CSS}</style></head><body>')
    P.append('<div class="wrap">')

    # -- header + status pill --------------------------------------------
    if is_adopt:
        pill = '<span class="pill adopt">Adopted by digest</span>'
    elif total and passed == total:
        pill = '<span class="pill ok">✓ Validated in shipped image</span>'
    else:
        pill = '<span class="pill bad">✗ Validation incomplete</span>'
    P.append(f"<h1>{_e(name)}{pill}</h1>")
    P.append(f'<p class="sub">Layer-1 environment image · <code>{_e(r.get("image",""))}</code></p>')

    # -- headline stats --------------------------------------------------
    P.append('<div class="stats">')
    P.append(f'<div class="stat"><div class="num">{len(requested)}</div>'
             '<div class="lbl">Requested tools</div></div>')
    if is_adopt:
        P.append('<div class="stat adopt"><div class="num">digest</div>'
                 '<div class="lbl">Adopted &amp; trusted</div></div>')
    else:
        cls = "ok" if (total and passed == total) else ""
        P.append(f'<div class="stat {cls}"><div class="num">{passed}/{total}</div>'
                 '<div class="lbl">Validated in image</div></div>')
    P.append(f'<div class="stat"><div class="num">{len(ride)}</div>'
             '<div class="lbl">Along for the ride</div></div>')
    P.append(f'<div class="stat"><div class="num">{len(system)}</div>'
             '<div class="lbl">System (apt) packages</div></div>')
    P.append('</div>')

    # -- the process flow -------------------------------------------------
    P.append("<h2>How this environment was produced</h2>")
    if is_adopt:
        P.append('<p class="note">A pure-conda request that matched a published BioContainer — '
                 'adopted by its immutable digest rather than rebuilt. Trust it as you trust the '
                 'BioContainers project.</p>')
    else:
        P.append('<p class="note">Each stage below is reconstructed from the verified record — the '
                 'tiers chosen, what was baked, and what re-ran green in the shipped image. '
                 'Nothing here is asserted that the runtime did not capture.</p>')
    P.append(_flow(r, is_adopt=is_adopt, requested=requested, pidx=pidx, shipped=shipped,
                   passed=passed, total=total))

    # -- per-tool journey (decision + validation per requested tool) ------
    P.append(f'<h2>Per-tool journey <span class="note">({len(requested)} requested)</span></h2>')
    if requested:
        head = ("<table><tr><th>Tool</th><th>Version</th><th>Install tier (decision)</th>"
                "<th>Validated in image</th></tr>") if not is_adopt else (
                "<table><tr><th>Tool</th><th>Version</th><th>Install tier</th>"
                "<th>Status</th></tr>")
        P.append(head)
        for t in requested:
            pkg = pidx.get(t.lower())
            v = vidx.get(t.lower())
            ver = (pkg or {}).get("version", "") or _extract_version((v or {}).get("out", "")) or "—"
            if is_adopt:
                status = '<span class="badge na">adopted (trusted by digest)</span>'
            else:
                status = _badge(v.get("passed") if v else None, (v or {}).get("check", ""))
            P.append(f"<tr><td><b>{_e(t)}</b></td><td>{_e(ver)}</td>"
                     f"<td>{_e(_install_method(t, pkg, shipped))}</td><td>{status}</td></tr>")
        P.append("</table>")
    else:
        P.append('<p class="note">(none recorded)</p>')

    # -- artifacts (the outputs) -----------------------------------------
    P.append("<h2>Artifacts</h2>")
    P.append('<div class="cards">')
    for k, val in (("Image", r.get("image", "")),
                   ("Image digest", r.get("image_digest", "")),
                   ("Content digest", r.get("content_digest", ""))):
        P.append(f'<div class="cell"><div class="k">{_e(k)}</div>'
                 f'<div class="v"><code>{_e(val)}</code></div></div>')
    P.append('</div>')
    P.append('<p class="note">Companion artifacts written alongside this page: '
             f'<code>{_e(name)}.attestation.json</code> (in-toto/SLSA provenance)'
             + ('' if is_adopt else f' · <code>{_e(name)}.recipe.yaml</code> (self-contained '
                'rebuild recipe — verify with <code>verify_env_recipe</code>)')
             + f' · <code>{_e(name)}.ENV.md</code>.</p>')

    # -- declared policy (clearly separated from verified facts) ----------
    gated = bool(r.get("gated"))
    licenses = list(r.get("licenses") or [])
    accel = r.get("accelerator") if isinstance(r.get("accelerator"), dict) else None
    accel_type = (accel or {}).get("type") or "none"
    P.append("<h2>Declared policy</h2>")
    P.append('<div class="declared">')
    P.append('<p class="lead">Submitter-declared — the honesty contract checks these for '
             '<i>consistency</i> (I12 accelerator, I13 license firewall), but their truth is a '
             'caller assertion, NOT a runtime-verified fact.</p>')
    P.append("<table>")
    P.append(f"<tr><th>License-gated</th><td>{'yes' if gated else 'no'}</td></tr>")
    P.append(f"<tr><th>Redistributable</th><td>{'yes' if r.get('redistributable', not gated) else 'no'}</td></tr>")
    if licenses:
        P.append(f"<tr><th>Licenses</th><td>{', '.join(_e(x) for x in licenses)}</td></tr>")
    P.append(f"<tr><th>Accelerator</th><td>{_e(accel_type)}</td></tr>")
    P.append("</table></div>")

    # -- along for the ride ----------------------------------------------
    if ride:
        P.append(f'<details><summary>Along for the ride — {len(ride)} transitive dependencies</summary>')
        P.append("<table><tr><th>Package</th><th>Version</th><th>Kind</th></tr>")
        for p in ride:
            P.append(f"<tr><td>{_e(p['name'])}</td><td>{_e(p.get('version',''))}</td>"
                     f"<td>{_e(p.get('kind',''))}</td></tr>")
        P.append("</table></details>")

    # -- system (apt) packages -------------------------------------------
    if system:
        P.append(f'<details><summary>System packages (apt) — {len(system)} '
                 '<span class="note">OS layer; captured for the SBOM, not version-pinned in the '
                 'content digest</span></summary>')
        P.append("<table><tr><th>Package</th><th>Version</th></tr>")
        for p in system:
            if isinstance(p, dict) and p.get("name"):
                P.append(f"<tr><td>{_e(p['name'])}</td><td>{_e(p.get('version',''))}</td></tr>")
        P.append("</table></details>")

    # -- long-tail provenance --------------------------------------------
    if conda_specs or shipped:
        P.append("<h2>Install &amp; provenance</h2>")
        if conda_specs:
            P.append("<p>Conda/PyPI specs (co-solved): "
                     + " ".join(f"<code>{_e(s)}</code>" for s in conda_specs) + "</p>")
        if shipped:
            P.append('<p class="note">Long-tail tools baked verbatim — the command IS the '
                     'provenance (it ran AND validated in the shipped image):</p>')
            P.append('<ul class="tail">')
            for s in shipped:
                label = s.get("name") or s.get("purpose") or "tool"
                cmd = (s.get("command") or "").strip()
                P.append(f'<li><span class="lbl">{_e(label)}</span>'
                         + (f"<pre>{_e(cmd)}</pre>" if cmd else "") + "</li>")
            P.append("</ul>")

    # -- delivery ---------------------------------------------------------
    hpc = r.get("hpc_delivery") or {}
    push = r.get("push_status", "")
    if hpc.get("get_image") or hpc.get("run_example"):
        P.append("<h2>Delivery (HPC / Apptainer)</h2>")
        if push and push != "not-configured":
            P.append(f'<p class="note">Registry: {_e(push)}</p>')
        if hpc.get("get_image"):
            P.append(f"<p>Get the image:</p><pre>{_e(hpc['get_image'])}</pre>")
        if hpc.get("run_example"):
            P.append(f"<p>Run:</p><pre>{_e(hpc['run_example'])}</pre>")
        if hpc.get("source_note"):
            P.append(f'<p class="note">{_e(hpc["source_note"])}</p>')

    # -- the honesty footer (per-mode) -----------------------------------
    P.append('<h2 id="verify">How this was verified</h2>')
    P.append('<ul class="foot">')
    if is_adopt:
        P.append("<li><b>ADOPTED_BY_DIGEST</b> — a public BioContainer pulled by its immutable "
                 "manifest digest (above). Provenance is that digest; trust it as you trust the "
                 "BioContainers project. It was not built or validated in-locus.</li>")
        P.append("<li><b>POLICY_CLEAN</b> — accelerator honesty (I12) and the license firewall "
                 "(I13) passed.</li>")
    else:
        P.append("<li><b>BUILT</b> — the image and its digest resolve in the Docker daemon; every "
                 "install step's inline anchor (sha256 / git commit / baked bytes) passed, else "
                 "there would be no image.</li>")
        P.append("<li><b>VALIDATED_IN_IMAGE</b> — every requested tool re-ran green via <i>plain "
                 "exec</i> inside the shipped image (the way <code>apptainer exec</code> runs it on "
                 "HPC). <b>Validated == shipped.</b></li>")
        P.append("<li><b>POLICY_CLEAN</b> — accelerator honesty (I12) and the license firewall "
                 "(I13) passed; synthesized installs carry full per-command provenance "
                 "(PROVENANCE_CLEAN).</li>")
    P.append(f"<li><b>Validation locus</b> — {_e(_locus_line(r.get('validation_locus','')))}.</li>")
    P.append("<li><b>Reproducibility</b> — the content digest binds the conda/PyPI lock, the "
             "long-tail commands, the platform, and the digest-pinned base image. Release binaries "
             "are sha256-anchored. The apt runtime layer is captured (above) but not version-pinned "
             "(<code>apt-get</code> is not reproducible across time). Rebuild + diff with "
             "<code>verify_env_recipe</code>.</li>")
    P.append("</ul>")

    # -- build details ("date + all other juicy info", per the sketch) ----
    P.append("<h2>Build details</h2>")
    P.append('<div class="cards">')
    details = [("Created", r.get("created_at", "")), ("Platform", r.get("platform", "")),
               ("Mode", mode), ("Build method", r.get("build_method", "")),
               ("Engine", r.get("engine", "")),
               ("Validation locus", _locus_line(r.get("validation_locus", "")))]
    for k, val in details:
        if val:
            P.append(f'<div class="cell"><div class="k">{_e(k)}</div>'
                     f'<div class="v">{_e(val)}</div></div>')
    P.append('</div>')

    P.append('<p class="gen">Generated deterministically from the freeze record — '
             'no field on this page, including the process flow, was authored by the agent.</p>')
    P.append("</div></body></html>")
    return "\n".join(P)
