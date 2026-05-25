"""
env_report_html — the Layer-1 env report as a self-contained HTML page, rendered
PURELY from the verified freeze record.

Same contract as the markdown report (env_report.py), but a human actually wants to
look at it. The honesty guarantees, made structural:

  • PURE over the record — render_env_report_html(record) reads ONLY the freeze
    record (what the build runtime captured: digests, in-image validation evidence,
    the resolved package closure, the apt layer, the long-tail commands + their
    provenance). The agent never passes free-text in; there is no field it can author.
  • ESCAPED — every value is HTML-escaped, so a package name / command / digest can
    never inject markup. The page is what the record says, nothing more.
  • DETERMINISTIC — no clock is read here (the only time shown is the record's own
    captured `created_at`); lists render in a stable order. Same record → same bytes.
  • MODE-HONEST — a container-native BUILD shows the per-tool "validated in the
    shipped image" evidence (validated == shipped). An ADOPTED biocontainer is shown
    as trusted-by-published-digest; it does NOT claim in-image validation it never ran.
  • VERIFIED vs DECLARED — runtime-verified facts and submitter-DECLARED policy
    (license-gating, accelerator) live in separate, labelled sections so a reader is
    never misled into reading a caller assertion as a machine-checked fact.

Self-contained: inline CSS, zero external resources (renders offline; nothing is
fetched, so nothing can change what you see). One public fn: render_env_report_html.
"""

from __future__ import annotations

from html import escape
from typing import Any, Optional

from agent.skills.env_report import (
    _extract_version, _install_method, _locus_line, _pkg_index, _verif_index,
)

# ---------------------------------------------------------------------------
# CSS — inline, light, system-font; the page must look decent with zero network.
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2330;--muted:#5b6675;--line:#e4e8ee;
--accent:#2a6df4;--ok:#1a7f47;--okbg:#e7f6ee;--bad:#c0341d;--badbg:#fcebe8;
--warn:#8a5a00;--warnbg:#fdf3e0;--code:#eef1f6;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:26px;margin:0 0 2px}
h2{font-size:18px;margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sub{color:var(--muted);margin:0 0 16px}
.banner{background:#eef3fe;border:1px solid #d3e0fb;color:#26426f;border-radius:10px;
padding:12px 14px;font-size:13.5px;margin:14px 0 6px}
.banner.adopt{background:var(--warnbg);border-color:#f0dcb0;color:var(--warn)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:8px 0}
.cell{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.cell .k{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.cell .v{font-size:14px;word-break:break-all}
.mono,code{font-family:var(--mono);font-size:12.5px}
code{background:var(--code);padding:1px 5px;border-radius:5px}
.counts{color:var(--muted);font-size:13.5px;margin:10px 0 0}
.counts b{color:var(--ink)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden;margin:6px 0}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);font-size:13.5px;vertical-align:top}
th{background:#fbfcfe;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:1px 8px;border-radius:20px}
.badge.ok{background:var(--okbg);color:var(--ok)}
.badge.bad{background:var(--badbg);color:var(--bad)}
.badge.na{background:var(--code);color:var(--muted)}
.prov{font-size:11px;font-weight:600;padding:1px 7px;border-radius:20px;background:var(--code);color:var(--muted)}
.prov.extracted{background:var(--okbg);color:var(--ok)}
.prov.agent_authored{background:var(--warnbg);color:var(--warn)}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 14px;margin:6px 0}
summary{cursor:pointer;font-weight:600;font-size:14px;padding:6px 0}
details table{border:none;margin:4px 0}
.note{color:var(--muted);font-size:12.5px;font-weight:400}
.declared{background:var(--warnbg);border:1px solid #f0dcb0;border-radius:10px;padding:4px 16px 12px}
.declared .lead{color:var(--warn);font-size:12.5px;margin:10px 0 2px}
ul.tail{list-style:none;padding:0;margin:0}
ul.tail li{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:8px 0}
ul.tail .lbl{font-weight:600;margin-right:8px}
pre{background:var(--code);border-radius:8px;padding:10px 12px;overflow-x:auto;margin:6px 0 0;
font-family:var(--mono);font-size:12px;white-space:pre-wrap;word-break:break-word}
.foot{margin-top:14px}
.foot li{margin:6px 0}
.gen{color:var(--muted);font-size:12px;margin-top:30px;border-top:1px solid var(--line);padding-top:12px}
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _badge(passed: Optional[bool], check: str = "") -> str:
    if passed is None:
        return '<span class="badge na">—</span>'
    cls, mark = ("ok", "✓") if passed else ("bad", "✗")
    c = f' <code>{_e(check)}</code>' if check else ""
    return f'<span class="badge {cls}">{mark}</span>{c}'


def render_env_report_html(record: dict) -> str:
    """Render the freeze record as a self-contained HTML page (see module docstring
    for the honesty contract this upholds)."""
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

    P: list[str] = []
    P.append("<!DOCTYPE html>")
    P.append(f'<html lang="en"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>Environment report — {_e(name)}</title><style>{_CSS}</style></head><body>')
    P.append('<div class="wrap">')

    # -- header ----------------------------------------------------------
    build_desc = mode
    if r.get("build_method"):
        build_desc += f" · {r['build_method']}"
    if r.get("engine") and r.get("engine") != "none":
        build_desc += f" · engine {r['engine']}"
    P.append(f"<h1>{_e(name)}</h1>")
    P.append(f'<p class="sub">Layer-1 environment image · {_e(build_desc)}</p>')
    if is_adopt:
        P.append('<div class="banner adopt">Adopted public BioContainer — trusted by its '
                 'immutable published digest. It was <b>not built or validated in-locus</b>, '
                 'so the per-tool in-image evidence below is not collected for this artifact.</div>')
    else:
        P.append('<div class="banner">Machine-generated from the verified freeze record. Every fact '
                 'below was captured by the build runtime — not authored — so it can\'t be faked. '
                 '<a href="#verify">How this was verified ↓</a></div>')

    # -- artifact facts (cards) ------------------------------------------
    cards = [
        ("Image", f'<code>{_e(r.get("image",""))}</code>'),
        ("Image digest", f'<code>{_e(r.get("image_digest",""))}</code>'),
        ("Content digest", f'<code>{_e(r.get("content_digest",""))}</code>'),
        ("Platform", _e(r.get("platform", ""))),
        ("Build", _e(build_desc)),
        ("Validation locus", _e(_locus_line(r.get("validation_locus", "")))),
        ("Created", _e(r.get("created_at", ""))),
    ]
    P.append('<div class="grid">')
    for k, v in cards:
        P.append(f'<div class="cell"><div class="k">{_e(k)}</div><div class="v">{v}</div></div>')
    P.append('</div>')
    P.append(f'<p class="counts"><b>{len(requested)}</b> requested · '
             f'<b>{len(ride)}</b> along for the ride · <b>{len(resolved)}</b> total packages'
             + (f' · <b>{len(system)}</b> system (apt)' if system else '') + '</p>')

    # -- requested tools --------------------------------------------------
    P.append(f'<h2>Requested tools <span class="note">({len(requested)})</span></h2>')
    if requested:
        P.append("<table><tr><th>Tool</th><th>Version</th><th>Install</th>"
                 "<th>Validated in image</th></tr>")
        for t in requested:
            pkg = pidx.get(t.lower())
            v = vidx.get(t.lower())
            ver = (pkg or {}).get("version", "") or _extract_version((v or {}).get("out", "")) or "—"
            cell = _badge(v.get("passed") if v else None, (v or {}).get("check", ""))
            P.append(f"<tr><td><b>{_e(t)}</b></td><td>{_e(ver)}</td>"
                     f"<td>{_e(_install_method(t, pkg, shipped))}</td><td>{cell}</td></tr>")
        P.append("</table>")
    else:
        P.append('<p class="note">(none recorded)</p>')

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
        P.append("<li><b>ADOPTED_BY_DIGEST</b> — this is a public BioContainer pulled by its "
                 "immutable manifest digest (shown above). Provenance is that digest; trust it as "
                 "you trust the BioContainers project. It was not built or validated in-locus.</li>")
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
    P.append(f"<li><b>Attestation</b> — an in-toto/SLSA provenance Statement of all the above is "
             f"emitted to <code>env_reports/{_e(name)}.attestation.json</code> "
             f"(sign with <code>cosign attest</code>).</li>")
    P.append("</ul>")

    P.append('<p class="gen">Generated deterministically from the freeze record — '
             'no field on this page was authored by the agent.</p>')
    P.append("</div></body></html>")
    return "\n".join(P)
