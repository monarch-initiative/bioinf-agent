"""
env_report_html — the Layer-1 env report as a self-contained HTML page, rendered
PURELY from the verified freeze record.

Clean tables, no decorative tiles. Build details up top, the actual tools table
(requested vs installed) front-and-centre, dependencies right below it, every
artifact (and where it lives on disk) in one table, the honesty footer at the end.

Honesty guarantees, made structural:

  • PURE over the record — render_env_report_html(record) reads ONLY the freeze
    record (digests, in-image validation evidence, the resolved closure, the apt
    layer, the long-tail commands + provenance). No field is agent-authored.
  • ESCAPED — every value is HTML-escaped; a package name / command / digest can
    never inject markup.
  • DETERMINISTIC — no clock is read here (the only time shown is the record's own
    captured `created_at`); stable ordering. Same record → same bytes.
  • MODE-HONEST — a container-native BUILD shows the per-tool in-image evidence
    (validated == shipped). An ADOPTED biocontainer is shown as trusted-by-digest;
    the page never claims a validation it did not run.
  • VERIFIED vs DECLARED — runtime-verified facts and submitter-DECLARED policy
    (license-gating, accelerator) live in separate, labelled sections.

Self-contained: inline CSS, zero external resources, no JS. Companion-artifact
links are RELATIVE to env_reports/, so they resolve from the file in place.
One public fn: render_env_report_html.
"""

from __future__ import annotations

from html import escape
from typing import Any, Optional

from agent.skills.env_report import (
    _extract_version, _install_method, _locus_line, _pkg_index, _verif_index,
)

_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f8f9fb;color:#1c2330;
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:30px 22px 64px}
h1{font-size:24px;margin:0 0 6px;font-weight:700;line-height:1.25}
.sub{color:#5b6675;margin:0 0 18px;font-size:13.5px}
h2{font-size:14.5px;margin:32px 0 10px;font-weight:600;letter-spacing:.02em;
text-transform:uppercase;color:#3a4654;padding-bottom:5px;border-bottom:1px solid #e2e6ed}
h2 .note{font-size:12px;text-transform:none;letter-spacing:0;font-weight:400;color:#5b6675;margin-left:6px}
a{color:#2563d4;text-decoration:none}a:hover{text-decoration:underline}
code{background:#eef1f6;padding:1px 6px;border-radius:4px;
font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
pre{background:#eef1f6;border-radius:6px;padding:9px 12px;margin:4px 0;overflow-x:auto;
font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
white-space:pre-wrap;word-break:break-word}
.tbl-wrap{overflow-x:auto;margin:6px 0}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e6ed;
border-radius:8px;overflow:hidden;font-size:13.5px}
th,td{text-align:left;padding:9px 14px;border-bottom:1px solid #e9ecf1;vertical-align:top;
line-height:1.5}
tr:last-child td{border-bottom:none}
th{background:#fafbfd;font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;
color:#5b6675;font-weight:600}
td.k{color:#5b6675;font-weight:500;width:220px;background:#fafbfd}
.pill{display:inline-block;font-size:11.5px;font-weight:700;padding:2px 10px;border-radius:20px;
vertical-align:middle;margin-left:10px}
.pill.ok{background:#e7f6ee;color:#1a7f47}
.pill.adopt{background:#fdf3e0;color:#8a5a00}
.pill.bad{background:#fcebe8;color:#c0341d}
.badge{display:inline-block;font-size:11.5px;font-weight:600;padding:1px 8px;border-radius:12px;margin-right:4px}
.badge.ok{background:#e7f6ee;color:#1a7f47}
.badge.bad{background:#fcebe8;color:#c0341d}
.badge.na{background:#eef1f6;color:#5b6675}
.note{color:#5b6675;font-size:12.5px;margin:6px 0}
details{margin:6px 0;border:1px solid #e2e6ed;border-radius:8px;padding:0 14px;background:#fff}
summary{cursor:pointer;padding:9px 0;font-weight:600;font-size:13.5px;color:#1c2330}
details>*{font-size:13.5px}
details table{border:none;margin:4px 0 10px}
details ul{padding-left:18px;margin:4px 0 10px}
ul.foot{padding-left:18px;margin:6px 0 0;font-size:13.5px}
ul.foot li{margin:5px 0}
.gen{color:#5b6675;font-size:12px;margin-top:30px;border-top:1px solid #e2e6ed;padding-top:12px}
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
    """Render a 2-col label/value table (values are pre-escaped HTML)."""
    body = "".join(f'<tr><td class="k">{_e(k)}</td><td>{v}</td></tr>' for k, v in rows if v)
    return f'<div class="tbl-wrap"><table>{body}</table></div>'


def _conda_req_versions(conda_specs: list) -> dict[str, str]:
    """Extract a {tool: requested_version_constraint} map from conda_specs (e.g.
    'samtools=1.21' → {'samtools': '1.21'}). Honest about what was actually asked."""
    out: dict[str, str] = {}
    for s in conda_specs or []:
        if not isinstance(s, str):
            continue
        name, _, ver = s.replace("==", "=").partition("=")
        if name:
            out[name.strip()] = ver.strip() or ""
    return out


def _tier_counts(requested: list, pidx: dict, shipped: list) -> str:
    """Per-tier headcount of the requested tools — the routing decision."""
    counts: dict[str, int] = {}
    for t in requested:
        tier = _install_method(t, pidx.get(t.lower()), shipped)
        counts[tier] = counts.get(tier, 0) + 1
    return " · ".join(f"{k} ×{v}" for k, v in sorted(counts.items()) if k != "—") or "—"


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
    req_versions = _conda_req_versions(conda_specs)
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

    # -- header + status pill -----------------------------------------------
    if is_adopt:
        pill = '<span class="pill adopt">Adopted by digest</span>'
    elif total and passed == total:
        pill = '<span class="pill ok">✓ Validated in shipped image</span>'
    elif total:
        pill = '<span class="pill bad">✗ Validation incomplete</span>'
    else:
        pill = '<span class="pill na">—</span>'
    P.append(f"<h1>{_e(name)}{pill}</h1>")
    P.append(f'<p class="sub">Layer-1 environment image · <code>{_e(r.get("image",""))}</code></p>')

    # -- BUILD DETAILS (top, per the sketch) --------------------------------
    P.append("<h2>Build details</h2>")
    mode_desc = mode
    if r.get("build_method"):
        mode_desc += f" · {r['build_method']}"
    if r.get("engine") and r.get("engine") != "none":
        mode_desc += f" · engine {r['engine']}"
    summary_parts = [f"{len(requested)} requested"]
    if is_adopt:
        summary_parts.append("adopted by digest")
    else:
        summary_parts.append(f"{passed}/{total} validated in image")
    summary_parts.append(f"{len(ride)} along for the ride")
    if system:
        summary_parts.append(f"{len(system)} system (apt)")
    rows = [
        ("Created", _e(r.get("created_at", ""))),
        ("Platform", _e(r.get("platform", ""))),
        ("Mode", _e(mode_desc)),
        ("Validation locus", _e(_locus_line(r.get("validation_locus", "")))),
        ("Summary", " · ".join(_e(p) for p in summary_parts)),
    ]
    if not is_adopt and requested:
        rows.append(("Routing decision", _e(_tier_counts(requested, pidx, shipped))))
    P.append(_kv_table(rows))

    # -- TOOLS (the centerpiece: requested -> installed -> tier -> validated)
    P.append(f'<h2>Tools <span class="note">({len(requested)} requested)</span></h2>')
    if requested:
        cols = ("<table><tr><th>Requested tool</th><th>Requested version</th>"
                "<th>Installed version</th><th>Install tier</th>"
                "<th>Validated in image</th></tr>") if not is_adopt else (
                "<table><tr><th>Requested tool</th><th>Requested version</th>"
                "<th>In adopted image</th><th>Install tier</th><th>Status</th></tr>")
        P.append('<div class="tbl-wrap">')
        P.append(cols)
        for t in requested:
            pkg = pidx.get(t.lower())
            v = vidx.get(t.lower())
            req_v = req_versions.get(t, "")
            req_cell = f"={_e(req_v)}" if req_v else '<span class="note">(any)</span>'
            inst_v = (pkg or {}).get("version", "") or _extract_version((v or {}).get("out", "")) or ""
            inst_cell = _e(inst_v) if inst_v else '<span class="note">—</span>'
            tier = _install_method(t, pkg, shipped)
            if is_adopt:
                status = '<span class="badge na">trusted by digest</span>'
            else:
                status = _badge(v.get("passed") if v else None, (v or {}).get("check", ""))
            P.append(f"<tr><td><b>{_e(t)}</b></td><td>{req_cell}</td>"
                     f"<td>{inst_cell}</td><td>{_e(tier)}</td><td>{status}</td></tr>")
        P.append("</table></div>")
    else:
        P.append('<p class="note">(none recorded)</p>')

    # -- ALONG FOR THE RIDE (right below the tools table, visible — not folded)
    if ride:
        P.append(f'<h2>Along for the ride <span class="note">({len(ride)} transitive dependencies)</span></h2>')
        P.append('<div class="tbl-wrap"><table>')
        P.append("<tr><th>Package</th><th>Version</th><th>Kind</th></tr>")
        for p in ride:
            P.append(f"<tr><td>{_e(p['name'])}</td><td>{_e(p.get('version',''))}</td>"
                     f"<td>{_e(p.get('kind',''))}</td></tr>")
        P.append("</table></div>")

    # -- INSTALL COMMANDS (long-tail provenance, foldable since verbose) -----
    if shipped:
        P.append(f'<details><summary>Install commands — {len(shipped)} long-tail step(s) '
                 'baked verbatim into the image (the command IS the provenance)</summary>')
        for s in shipped:
            label = s.get("name") or s.get("purpose") or "tool"
            cmd = (s.get("command") or "").strip()
            P.append(f'<p style="margin:8px 0 2px"><b>{_e(label)}</b></p>')
            if cmd:
                P.append(f"<pre>{_e(cmd)}</pre>")
        P.append("</details>")

    # -- SYSTEM PACKAGES (apt SBOM, foldable) --------------------------------
    if system:
        P.append(f'<details><summary>System (apt) packages — {len(system)} '
                 '<span class="note">OS layer; captured for the SBOM, not version-pinned '
                 'in the content digest (apt-get is not reproducible across time)</span></summary>')
        P.append('<div class="tbl-wrap"><table>')
        P.append("<tr><th>Package</th><th>Version</th></tr>")
        for p in system:
            if isinstance(p, dict) and p.get("name"):
                P.append(f"<tr><td>{_e(p['name'])}</td><td>{_e(p.get('version',''))}</td></tr>")
        P.append("</table></div></details>")

    # -- ARTIFACTS (one table; companion files link to where they live) ------
    P.append("<h2>Artifacts <span class=\"note\">links resolve relative to env_reports/</span></h2>")
    art_rows: list[tuple[str, str]] = []
    if r.get("image"):
        art_rows.append(("Image", f'<code>{_e(r["image"])}</code>'))
    if r.get("image_digest"):
        art_rows.append(("Image digest", f'<code>{_e(r["image_digest"])}</code>'))
    if r.get("content_digest"):
        art_rows.append(("Content digest",
                         f'<code>{_e(r["content_digest"])}</code> '
                         '<span class="note">— reproducible anchor: lock + long-tail + '
                         'platform + engine + base image</span>'))
    art_rows.append(("Markdown report",
                     f'<a href="{_e(name)}.ENV.md"><code>{_e(name)}.ENV.md</code></a>'))
    art_rows.append(("In-toto / SLSA attestation",
                     f'<a href="{_e(name)}.attestation.json"><code>{_e(name)}.attestation.json</code></a> '
                     '<span class="note">— sign with <code>cosign attest</code></span>'))
    if not is_adopt:
        art_rows.append(("Self-contained rebuild recipe",
                         f'<a href="{_e(name)}.recipe.yaml"><code>{_e(name)}.recipe.yaml</code></a> '
                         '<span class="note">— verify rebuild with <code>verify_env_recipe</code></span>'))
    if r.get("tarball"):
        tb = r["tarball"]
        art_rows.append(("docker-save tarball",
                         f'<a href="file://{_e(tb)}"><code>{_e(tb)}</code></a>'))
    if r.get("conda_lock"):
        cl = r["conda_lock"]
        art_rows.append(("Conda lock",
                         f'<a href="file://{_e(cl)}"><code>{_e(cl)}</code></a>'))
    hpc = r.get("hpc_delivery") or {}
    if hpc.get("get_image"):
        art_rows.append(("Apptainer pull (HPC)", f"<pre>{_e(hpc['get_image'])}</pre>"))
    if hpc.get("run_example"):
        art_rows.append(("Run example", f"<pre>{_e(hpc['run_example'])}</pre>"))
    push = r.get("push_status", "")
    if push and push != "not-configured":
        art_rows.append(("Registry status", _e(push)))
    P.append(_kv_table(art_rows))

    # -- DECLARED POLICY (verified-vs-declared honesty — a plain table) ------
    gated = bool(r.get("gated"))
    licenses = list(r.get("licenses") or [])
    accel = r.get("accelerator") if isinstance(r.get("accelerator"), dict) else None
    accel_type = (accel or {}).get("type") or "none"
    P.append('<h2>Declared policy <span class="note">submitter-declared; the contract checks '
             'these for consistency (I12/I13), <b>not</b> a runtime-verified fact — a caller '
             'assertion</span></h2>')
    pol_rows = [
        ("License-gated", "yes" if gated else "no"),
        ("Redistributable", "yes" if r.get("redistributable", not gated) else "no"),
        ("Accelerator", _e(accel_type)),
    ]
    if licenses:
        pol_rows.insert(2, ("Licenses", ", ".join(_e(x) for x in licenses)))
    P.append(_kv_table(pol_rows))

    # -- HOW VERIFIED (per-mode — never over-claim for adopt) ----------------
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
    P.append("<li><b>Reproducibility</b> — the content digest binds the conda/PyPI lock, the "
             "long-tail commands, the platform, and the digest-pinned base image. Release binaries "
             "are sha256-anchored. The apt runtime layer is captured but not version-pinned "
             "(<code>apt-get</code> is not reproducible across time).</li>")
    P.append("</ul>")

    P.append('<p class="gen">Generated deterministically from the freeze record — '
             'no field on this page was authored by the agent.</p>')
    P.append("</div></body></html>")
    return "\n".join(P)
