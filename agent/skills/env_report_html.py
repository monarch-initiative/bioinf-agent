"""
env_report_html — the Layer-1 env report as a self-contained HTML page, rendered
PURELY from the verified freeze record.

Clean tables, no decorative tiles. SAME sections + SAME columns for every install
(adopt and build), so two reports compare cell-for-cell. Cyberpunk-inspired dark
palette (black, cyan, yellow) — meant to give each section visual weight without
adding any unverifiable content.

Honesty guarantees, made structural:

  • PURE over the record — render_env_report_html(record) reads ONLY the freeze
    record. No field is agent-authored.
  • ESCAPED — every value is HTML-escaped; a package name / command / digest can
    never inject markup.
  • DETERMINISTIC — no clock is read here (the only time shown is the record's own
    captured `created_at`); stable ordering. Same record → same bytes.
  • MODE-HONEST — a container-native BUILD shows per-tool in-image evidence
    (validated == shipped). An ADOPTED biocontainer keeps the same columns but its
    "Validated in image" cell is a "trusted by digest" badge — the page never
    claims a validation it did not run.
  • VERIFIED vs DECLARED — runtime-verified facts and submitter-DECLARED policy
    (license-gating, accelerator) live in separate, labelled sections.

Self-contained: inline CSS, zero external resources, no JS. Companion-artifact
links resolve relative to env_reports/ where the file lives. One public fn:
render_env_report_html.
"""

from __future__ import annotations

from html import escape
from typing import Any, Optional

from agent.skills.env_report import (
    _extract_version, _install_method, _locus_line, _pkg_index, _verif_index,
)

_CSS = """
:root{
  --bg:#0a0c14;--surface:#13151f;--surface-2:#1a1d29;--border:#262a3a;
  --cyan:#22e3ee;--cyan-soft:rgba(34,227,238,.16);
  --yellow:#fff200;--yellow-soft:rgba(255,242,0,.18);
  --ink:#e6e9f0;--muted:#8e98ad;
  --ok:#3ce086;--ok-bg:rgba(60,224,134,.14);
  --bad:#ff4b6e;--bad-bg:rgba(255,75,110,.14);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14.5px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:30px 22px 64px;background:transparent}
/* HEADER BANNER — yellow title, cyan border, tiny yellow corner brackets */
.head{position:relative;border:1px solid var(--cyan);padding:22px 26px 6px;margin:0 0 28px;
background:linear-gradient(180deg,rgba(34,227,238,.05),transparent 80%)}
.head .cr{position:absolute;width:14px;height:14px;pointer-events:none}
.head .cr-tl{top:-1px;left:-1px;border-top:2px solid var(--yellow);border-left:2px solid var(--yellow)}
.head .cr-tr{top:-1px;right:-1px;border-top:2px solid var(--yellow);border-right:2px solid var(--yellow)}
.head .cr-bl{bottom:-1px;left:-1px;border-bottom:2px solid var(--yellow);border-left:2px solid var(--yellow)}
.head .cr-br{bottom:-1px;right:-1px;border-bottom:2px solid var(--yellow);border-right:2px solid var(--yellow)}
/* SECTION PANELS — each remaining section is a bordered card (no yellow accents) */
section.bx{border:1px solid var(--border);margin:22px 0;background:transparent}
section.bx > h2{margin:0;padding:14px 22px 11px;border-bottom:1px solid var(--cyan)}
section.bx > .bx-body{padding:14px 22px 18px}
section.bx > .bx-body > *:first-child{margin-top:0}
section.bx > .bx-body > *:last-child{margin-bottom:0}
/* sub-heading inside a section (e.g. "Install commands" under Along for the ride) */
h3.sub{font-size:11.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);
margin:22px 0 8px;font-weight:600}
.head h1{font-size:24px;font-weight:800;color:var(--yellow);margin:0 0 14px;letter-spacing:.01em}
table.head-kv{width:100%;border:none;background:transparent;font-size:12.5px;
border-collapse:collapse}
table.head-kv td{padding:6px 0;border-bottom:1px solid var(--border);vertical-align:top;line-height:1.5}
table.head-kv tr:last-child td{border-bottom:none}
table.head-kv td.k{background:transparent;border:none;width:185px;color:var(--muted);
font-weight:500;padding-right:18px}
/* SECTION HEADINGS — cyan, uppercase, just an underline (no yellow left bar) */
h2{font-size:12.5px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;
color:var(--cyan);margin:34px 0 12px;padding:0 0 9px 0;border-bottom:1px solid var(--cyan)}
h2 .note{color:var(--muted);font-weight:400;letter-spacing:0;text-transform:none;font-size:12px;margin-left:8px}
a{color:var(--yellow);text-decoration:none;border-bottom:1px dashed transparent}
a:hover{border-bottom-color:var(--yellow)}
code{background:#0e1019;color:var(--cyan);padding:1px 6px;border:1px solid var(--border);
border-radius:2px;font:12.5px/1.4 var(--mono);word-break:break-all}
pre{background:#0c0e16;color:var(--ink);padding:10px 12px;margin:4px 0;border:1px solid var(--border);
border-left:3px solid var(--cyan);border-radius:0;overflow-x:auto;
font:12px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word}
.tbl-wrap{overflow-x:auto;margin:4px 0}
table{width:100%;border-collapse:collapse;background:var(--surface);
border:1px solid var(--border);border-top:2px solid var(--cyan);font-size:13.5px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:top;line-height:1.5}
tr:last-child td{border-bottom:none}
th{background:var(--surface-2);color:var(--cyan);font-size:10.5px;font-weight:700;
letter-spacing:.12em;text-transform:uppercase;border-bottom:1px solid var(--border)}
/* LEFTMOST COLUMN — same muted color as the build-details key column, in EVERY table */
table:not(.head-kv) td:first-child{color:var(--muted);font-weight:500}
td.k{color:var(--muted);width:225px;background:var(--surface-2);font-weight:500;
border-right:1px solid var(--border)}
.pill{display:inline-block;font-size:11px;font-weight:800;padding:3px 12px;
vertical-align:middle;margin-left:10px;letter-spacing:.14em;text-transform:uppercase;border-radius:0}
.pill.ok{background:var(--cyan);color:#000}
.pill.adopt{background:var(--yellow);color:#000}
.pill.bad{background:var(--bad);color:#fff}
.pill.na{background:var(--surface-2);color:var(--muted);border:1px solid var(--border)}
.badge{display:inline-block;font-size:11.5px;font-weight:700;padding:1px 9px;border-radius:0;
margin-right:4px}
.badge.ok{background:var(--ok-bg);color:var(--ok);border:1px solid var(--ok)}
.badge.bad{background:var(--bad-bg);color:var(--bad);border:1px solid var(--bad)}
.badge.na{background:var(--surface-2);color:var(--muted);border:1px solid var(--border)}
.note{color:var(--muted);font-size:12.5px;margin:6px 0}
.muted{color:var(--muted)}
.empty{background:var(--surface);border:1px dashed var(--border);padding:13px 16px;
color:var(--muted);font-size:13px;font-style:italic;margin:4px 0}
details{margin:6px 0;background:var(--surface);border:1px solid var(--border);
padding:0 14px;border-radius:0}
summary{cursor:pointer;padding:10px 0;font-weight:600;font-size:13.5px;color:var(--ink);
list-style:none;outline:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ ";color:var(--cyan);margin-right:4px}
details[open] summary::before{content:"▾ "}
details table{margin:4px 0 10px}
ul.foot{padding-left:18px;margin:6px 0 0;font-size:13.5px;line-height:1.6}
ul.foot li{margin:6px 0}
ul.foot li::marker{color:var(--yellow)}
ul.foot b{color:var(--yellow);font-weight:700;letter-spacing:.04em}
.gen{color:var(--muted);font-size:11.5px;margin-top:38px;border-top:1px solid var(--border);
padding-top:14px;letter-spacing:.04em}
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _badge(passed: Optional[bool], check: str = "") -> str:
    if passed is None:
        return '<span class="badge na">—</span>'
    cls, mark = ("ok", "✓") if passed else ("bad", "✗")
    c = f' <code>{_e(check)}</code>' if check else ""
    return f'<span class="badge {cls}">{mark}</span>{c}'


def _kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(f'<tr><td class="k">{_e(k)}</td><td>{v}</td></tr>'
                   for k, v in rows if v != "" and v is not None)
    return f'<div class="tbl-wrap"><table>{body}</table></div>'


def _empty(msg: str) -> str:
    return f'<p class="empty">{_e(msg)}</p>'


def _requested_versions(record: dict) -> dict[str, str]:
    """tool → user-asked version constraint (empty if any). The request_key holds
    the original ask for every record (build OR adopt); conda_specs is a fallback
    for older records that lack one."""
    out: dict[str, str] = {}
    rk = record.get("request_key", "") or ""
    if "|" in rk:
        spec = rk.split("|", 1)[0]
        for tok in spec.split(","):
            n, _, v = tok.replace("==", "=").partition("=")
            if n.strip():
                out[n.strip()] = v.strip()
    for s in record.get("conda_specs", []) or []:
        if isinstance(s, str):
            n, _, v = s.replace("==", "=").partition("=")
            if n.strip() and n.strip() not in out:
                out[n.strip()] = v.strip()
    return out


def _tier_for(t: str, is_adopt: bool, pkg: Optional[dict], shipped: list) -> str:
    if is_adopt:
        return "adopted (biocontainer)"
    return _install_method(t, pkg, shipped)


def _installed_version(t: str, is_adopt: bool, pkg: Optional[dict], v: Optional[dict],
                       req_v: str) -> str:
    """For BUILD: the actually-resolved version (pidx) or the version the tool
    printed in its in-image evidence (real captured output). For ADOPT: the
    requested version — the biocontainer's manifest digest binds it to exactly that
    bioconda build, so 'installed' == 'requested' is honest (no in-locus probe)."""
    if is_adopt:
        return req_v or ""
    return (pkg or {}).get("version", "") or _extract_version((v or {}).get("out", "")) or ""


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
    req_versions = _requested_versions(r)
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

    # -- HEADER BANNER ------------------------------------------------------
    # Yellow title "Bioinfo install report — {name}" + status pill, with a small
    # kv table of the run's at-a-glance facts inside the cyan-bordered banner.
    if is_adopt:
        pill = '<span class="pill adopt">Adopted by digest</span>'
    elif total and passed == total:
        pill = '<span class="pill ok">✓ Validated in shipped image</span>'
    elif total:
        pill = '<span class="pill bad">✗ Validation incomplete</span>'
    else:
        pill = '<span class="pill na">No tools recorded</span>'
    mode_desc = mode
    if r.get("build_method"):
        mode_desc += f" · {r['build_method']}"
    if r.get("engine") and r.get("engine") != "none":
        mode_desc += f" · engine {r['engine']}"
    summary_parts = [f"{len(requested)} requested"]
    summary_parts.append("adopted by digest" if is_adopt else f"{passed}/{total} validated in image")
    summary_parts.append(f"{len(ride)} along for the ride")
    summary_parts.append(f"{len(system)} system (apt)")
    head_rows = [
        ("Image", f'<code>{_e(r.get("image",""))}</code>' if r.get("image") else "—"),
        ("Created", _e(r.get("created_at", "—"))),
        ("Platform", _e(r.get("platform", "—"))),
        ("Mode", _e(mode_desc) or "—"),
        ("Validation locus", _e(_locus_line(r.get("validation_locus", ""))) or "—"),
        ("Summary", " · ".join(_e(p) for p in summary_parts)),
    ]
    P.append('<div class="head">')
    P.append('<span class="cr cr-tl"></span><span class="cr cr-tr"></span>'
             '<span class="cr cr-bl"></span><span class="cr cr-br"></span>')
    P.append(f"<h1>Bioinfo install report — {_e(name)}{pill}</h1>")
    body = "".join(f'<tr><td class="k">{_e(k)}</td><td>{v}</td></tr>' for k, v in head_rows)
    P.append(f'<table class="head-kv">{body}</table>')
    P.append('</div>')

    # -- TOOLS (centerpiece — SAME columns for build & adopt) ---------------
    P.append('<section class="bx">')
    P.append(f'<h2>Tools <span class="note">({len(requested)} requested)</span></h2>')
    P.append('<div class="bx-body">')
    if requested:
        P.append('<div class="tbl-wrap"><table>')
        P.append("<tr><th>Requested tool</th><th>Requested version</th>"
                 "<th>Installed version</th><th>Install tier</th>"
                 "<th>Validated in image</th></tr>")
        for t in requested:
            pkg = pidx.get(t.lower())
            v = vidx.get(t.lower())
            req_v = req_versions.get(t, "")
            req_cell = f"={_e(req_v)}" if req_v else '<span class="muted">(any)</span>'
            inst_v = _installed_version(t, is_adopt, pkg, v, req_v)
            inst_cell = _e(inst_v) if inst_v else '<span class="muted">—</span>'
            tier_cell = _e(_tier_for(t, is_adopt, pkg, shipped))
            if is_adopt:
                status = '<span class="badge na">trusted by digest</span>'
            else:
                status = _badge(v.get("passed") if v else None, (v or {}).get("check", ""))
            P.append(f"<tr><td>{_e(t)}</td><td>{req_cell}</td>"
                     f"<td>{inst_cell}</td><td>{tier_cell}</td><td>{status}</td></tr>")
        P.append("</table></div>")
    else:
        P.append(_empty("(no tools recorded)"))
    P.append('</div></section>')

    # -- ALONG FOR THE RIDE + INSTALL COMMANDS (same bordered panel) --------
    P.append('<section class="bx">')
    P.append(f'<h2>Along for the ride <span class="note">'
             f'({len(ride)} transitive dependencies)</span></h2>')
    P.append('<div class="bx-body">')
    if ride:
        P.append('<div class="tbl-wrap"><table>')
        P.append("<tr><th>Package</th><th>Version</th><th>Kind</th></tr>")
        for p in ride:
            P.append(f"<tr><td>{_e(p['name'])}</td><td>{_e(p.get('version',''))}</td>"
                     f"<td>{_e(p.get('kind',''))}</td></tr>")
        P.append("</table></div>")
    else:
        P.append(_empty("(closure not captured in-locus — an adopted image is trusted "
                        "by its published digest, not introspected here)" if is_adopt else
                        "(none — every resolved package was directly requested)"))
    # -- install commands as a sub-section of the same bordered panel -------
    P.append(f'<h3 class="sub">Install commands · {len(shipped)} long-tail step(s) '
             'baked verbatim into the image</h3>')
    if shipped:
        P.append('<details open><summary>Verbatim long-tail commands — the command IS the provenance</summary>')
        for s in shipped:
            label = s.get("name") or s.get("purpose") or "tool"
            cmd = (s.get("command") or "").strip()
            P.append(f'<p style="margin:10px 0 2px"><b>{_e(label)}</b></p>')
            if cmd:
                P.append(f"<pre>{_e(cmd)}</pre>")
        P.append("</details>")
    else:
        P.append(_empty("(no long-tail steps — pure conda env or adopted biocontainer)"))
    P.append('</div></section>')

    # -- SYSTEM (apt) PACKAGES (always shown; foldable when present) --------
    P.append('<section class="bx">')
    P.append(f'<h2>System packages (apt) <span class="note">'
             f'({len(system)} captured — OS layer; SBOM only, NOT pinned in the content digest)</span></h2>')
    P.append('<div class="bx-body">')
    if system:
        P.append('<details><summary>System (apt) packages</summary>')
        P.append('<div class="tbl-wrap"><table>')
        P.append("<tr><th>Package</th><th>Version</th></tr>")
        for p in system:
            if isinstance(p, dict) and p.get("name"):
                P.append(f"<tr><td>{_e(p['name'])}</td><td>{_e(p.get('version',''))}</td></tr>")
        P.append("</table></div></details>")
    else:
        P.append(_empty("(no apt SBOM captured — adopted image; the apt layer was not "
                        "introspected in-locus)" if is_adopt else
                        "(no system packages recorded)"))
    P.append('</div></section>')

    # -- ARTIFACTS (one table — image · digests · files · delivery) ---------
    P.append('<section class="bx">')
    P.append('<h2>Artifacts <span class="note">'
             'companion files link relative to env_reports/; tarball/lock are absolute file://</span></h2>')
    P.append('<div class="bx-body">')
    art_rows: list[tuple[str, str]] = [
        ("Image", f'<code>{_e(r.get("image","—"))}</code>' if r.get("image") else "—"),
        ("Image digest", f'<code>{_e(r.get("image_digest","—"))}</code>' if r.get("image_digest") else "—"),
        ("Content digest",
         f'<code>{_e(r.get("content_digest","—"))}</code>'
         '<span class="note"> — reproducible anchor: lock + long-tail + platform + engine + base image</span>'
         if r.get("content_digest") else "—"),
        ("Markdown report",
         f'<a href="{_e(name)}.ENV.md"><code>{_e(name)}.ENV.md</code></a>'),
        ("In-toto / SLSA attestation",
         f'<a href="{_e(name)}.attestation.json"><code>{_e(name)}.attestation.json</code></a>'
         '<span class="note"> — sign with <code>cosign attest</code></span>'),
    ]
    if is_adopt:
        art_rows.append(("Self-contained rebuild recipe",
                         '<span class="muted">— not applicable to adopt mode '
                         '(no in-locus build to replay; the biocontainer\'s manifest digest IS the contract)</span>'))
    else:
        art_rows.append(("Self-contained rebuild recipe",
                         f'<a href="{_e(name)}.recipe.yaml"><code>{_e(name)}.recipe.yaml</code></a>'
                         '<span class="note"> — verify rebuild with <code>verify_env_recipe</code></span>'))
    if r.get("tarball"):
        tb = r["tarball"]
        art_rows.append(("docker-save tarball",
                         f'<a href="file://{_e(tb)}"><code>{_e(tb)}</code></a>'))
    else:
        art_rows.append(("docker-save tarball",
                         '<span class="muted">— not produced for this mode</span>' if is_adopt else
                         '<span class="muted">— not produced (registry-only delivery, or build skipped tarball)</span>'))
    if r.get("conda_lock"):
        cl = r["conda_lock"]
        art_rows.append(("Conda lock",
                         f'<a href="file://{_e(cl)}"><code>{_e(cl)}</code></a>'))
    else:
        art_rows.append(("Conda lock",
                         '<span class="muted">— not produced for this env</span>'))
    hpc = r.get("hpc_delivery") or {}
    if hpc.get("get_image"):
        art_rows.append(("Apptainer pull (HPC)", f"<pre>{_e(hpc['get_image'])}</pre>"))
    if hpc.get("run_example"):
        art_rows.append(("Run example", f"<pre>{_e(hpc['run_example'])}</pre>"))
    push = r.get("push_status", "")
    if push:
        art_rows.append(("Registry status", _e(push)))
    P.append(_kv_table(art_rows))
    P.append('</div></section>')

    # -- DECLARED POLICY (verified vs declared — plain table + note) --------
    gated = bool(r.get("gated"))
    licenses = list(r.get("licenses") or [])
    accel = r.get("accelerator") if isinstance(r.get("accelerator"), dict) else None
    accel_type = (accel or {}).get("type") or "none"
    P.append('<section class="bx">')
    P.append('<h2>Declared policy <span class="note">submitter-declared; the contract checks '
             'these for consistency (I12/I13), <b>not</b> a runtime-verified fact — a caller assertion</span></h2>')
    P.append('<div class="bx-body">')
    pol_rows = [
        ("License-gated", "yes" if gated else "no"),
        ("Redistributable", "yes" if r.get("redistributable", not gated) else "no"),
        ("Licenses", ", ".join(_e(x) for x in licenses) if licenses
                     else '<span class="muted">— (none declared)</span>'),
        ("Accelerator", _e(accel_type)),
    ]
    P.append(_kv_table(pol_rows))
    P.append('</div></section>')

    # -- HOW VERIFIED (per-mode footer — never over-claim for adopt) --------
    P.append('<section class="bx">')
    P.append('<h2 id="verify">How this was verified</h2>')
    P.append('<div class="bx-body">')
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
    P.append("<li><b>Reproducibility</b> — the content digest binds the conda/PyPI lock, the "
             "long-tail commands, the platform, and the digest-pinned base image. Release binaries "
             "are sha256-anchored. The apt runtime layer is captured but not version-pinned "
             "(<code>apt-get</code> is not reproducible across time).</li>")
    P.append("</ul>")
    P.append('</div></section>')

    P.append('<p class="gen">Generated deterministically from the freeze record — '
             'no field on this page was authored by the agent.</p>')
    P.append("</div></body></html>")
    return "\n".join(P)
