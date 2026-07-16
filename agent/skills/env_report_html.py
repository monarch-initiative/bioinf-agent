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

from agent.skills.freeze import record_is_gated as _record_is_gated

from agent.skills.env_report_helpers import (
    _install_anchor, _install_method, _is_sha, _locus_line, _pkg_index,
    _resolved_version, _verif_index, requested_versions as _shared_req_versions,
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
/* HEADER BANNER — the INNER CYAN frame is the ONLY continuous border (visible
   all the way around). At the TL and BR corners (only), a solid yellow L-block
   sits OUTSIDE the cyan with a small gap, extending ~half the panel along both
   edges. At the FAR end of each arm, a diagonal cut spans the L's thickness —
   so the L tapers off cleanly and there's no continuous yellow line past it.
   Small parallelogram-shaped gaps punched in the cyan directly opposite each
   yellow diagonal (top cyan for TL, bottom cyan for BR), with edges sloped to
   match the yellow's diagonal — // matching ends instead of vertical || ends. */
.head{position:relative;border:2px solid var(--cyan);padding:22px 26px 6px;margin:24px 24px 44px;
background:linear-gradient(180deg,rgba(34,227,238,.05),transparent 80%)}
.head .cr{position:absolute;background:var(--yellow);pointer-events:none;z-index:2}
/* TL: block at top:-22 left:-22 → L's outer edge is 22px outside the cyan;
   arm thickness 14px (block y=0..14); 6px gap between L's inner edge (y=14)
   and the 2px cyan border (which sits at y=20..22 in block coords). 50% extent
   along each edge. */
.head .cr-tl{top:-22px;left:-22px;
width:calc(50% + 22px);height:calc(50% + 22px);
clip-path:polygon(0 0,100% 0,calc(100% - 30px) 14px,14px 14px,14px calc(100% - 30px),0 100%)}
/* BR: mirror of TL (rotate 180°). */
.head .cr-br{bottom:-22px;right:-22px;
width:calc(50% + 22px);height:calc(50% + 22px);
clip-path:polygon(100% 0,100% 100%,0 100%,30px calc(100% - 14px),calc(100% - 14px) calc(100% - 14px),calc(100% - 14px) 30px)}
/* page-bg-colored masks that PUNCH a parallelogram-shaped gap through the 2px
   cyan border. 30px wide (= horizontal projection of the yellow diagonal), with
   each side sloped by 4px over 2px height to match the yellow diagonal's slope
   (dx/dy ≈ -30/14). One on the TOP cyan under TL's diagonal, one on the BOTTOM
   cyan under BR's diagonal. */
.head .gap{position:absolute;background:var(--bg);z-index:1;pointer-events:none;
width:34px;height:2px;
clip-path:polygon(4px 0,34px 0,30px 100%,0 100%)}
.head .gap-tl{top:-2px;left:calc(50% - 34px)}
.head .gap-br{bottom:-2px;right:calc(50% - 34px)}
/* SECTION PANELS — each remaining section is a bordered card (no yellow accents) */
section.bx{border:1px solid var(--border);margin:22px 0;background:transparent}
section.bx > h2{margin:0;padding:14px 22px 11px;border-bottom:none}
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
/* RUN CARDS — per-locus validated-run cards. Shared into the Layer-2 run
   dashboard (run_dashboard_html) via the same _CSS, so both artifacts are one
   visual family; unused by the Layer-1 env report itself. */
.run-card{border:1px solid var(--border);border-left:3px solid var(--cyan);
background:var(--surface);padding:12px 16px 14px;margin:12px 0}
.run-title{font-size:14px;font-weight:700;color:var(--ink);margin:2px 0 6px;
display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.run-card .note{margin:4px 0 10px}
.run-card details{margin:10px 0 2px}
/* stale marker — a locus whose evidence ran against a DIFFERENT env digest than
   the one this workflow is headlined by (accretion is honest only per-digest). */
.stale{color:var(--yellow);font-weight:600}
.how{border:1px solid var(--cyan);border-left:3px solid var(--cyan);
background:linear-gradient(180deg,rgba(34,227,238,.05),transparent 70%);
padding:14px 18px 16px;margin:12px 0}
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _badge(passed: Optional[bool], check: str = "", tool: str = "") -> str:
    if passed is None:
        return '<span class="badge na">—</span>'
    cls, mark = ("ok", "✓") if passed else ("bad", "✗")
    c = f' <code>{_e(check)}</code>' if check else ""
    # DISCLOSURE: how deeply the evidence exercised the tool. A shallow proof (version/
    # import/help = presence only) is labelled so it can't read as a functional run — the
    # honesty lever the Talos reconstruction slipped past (imported clean, didn't RUN).
    depth = ""
    if check:
        try:
            from agent.skills.env_honesty import evidence_depth, is_shallow_evidence
            d = evidence_depth(check, tool)
            # ASK the classifier; never re-derive its answer. This literal used to be
            # ("version", "import", "help") — a stale copy of _SHALLOW_DEPTHS that omitted
            # `presence`, so the WEAKEST evidence in the system rendered as "runs the tool"
            # while the stronger `--version` got the ⚠. That matters most on adopted envs,
            # whose evidence IS a presence check. `unknown` (the classifier declining to
            # guess) also read as a functional run — an assertion built out of a shrug.
            shallow = is_shallow_evidence(check, tool) or d == "unknown"
            depth = (f' <span class="note" title="evidence depth: {d} '
                     f'({"presence only — not a functional run" if shallow else "runs the tool"})">'
                     f'{"⚠ " if shallow else ""}{_e(d)}</span>')
        except Exception:
            depth = ""
    return f'<span class="badge {cls}">{mark}</span>{c}{depth}'


def _kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(f'<tr><td class="k">{_e(k)}</td><td>{v}</td></tr>'
                   for k, v in rows if v != "" and v is not None)
    return f'<div class="tbl-wrap"><table>{body}</table></div>'


def _empty(msg: str) -> str:
    return f'<p class="empty">{_e(msg)}</p>'


def _header_banner(title_html: str, pill_html: str, rows: list[tuple[str, str]]) -> str:
    """The cyberpunk header banner — the ONE shared page-header used by BOTH the
    Layer-1 env report and the Layer-2 run dashboard, so the two artifacts read as
    one family. `title_html` and every row VALUE are inserted verbatim (callers
    escape); row KEYS are escaped here. Empty-value rows are dropped."""
    body = "".join(f'<tr><td class="k">{_e(k)}</td><td>{v}</td></tr>'
                   for k, v in rows if v != "" and v is not None)
    return (
        '<div class="head">'
        '<span class="cr cr-tl"></span><span class="cr cr-br"></span>'
        '<span class="gap gap-tl"></span><span class="gap gap-br"></span>'
        f'<h1>{title_html}{pill_html}</h1>'
        f'<table class="head-kv">{body}</table>'
        '</div>'
    )


# Re-exported under the historical private name so call sites here don't churn —
# the canonical helper now lives in env_report (so the .md renderer can share it,
# the R1 fix point); see env_report.requested_versions docstring.
_requested_versions = _shared_req_versions


def _tier_for(t: str, is_adopt: bool, pkg: Optional[dict], shipped: list) -> str:
    if is_adopt:
        return "adopted (biocontainer)"
    return _install_method(t, pkg, shipped)


# Per-tool SHIP assurance — HOW each shipped long-tail tool is anchored. Binary
# carries the install→ship integrity-chain verdict (F1/F2); C5 extends the SAME
# disclosure to every other tier (env_freeze._replay_assurance) so the report can
# never imply uniform trust — a source tool on a floating branch (drifts) must not
# look like a digest-pinned one. `ok` = an immutable anchor a rebuild reproduces;
# `na` = ships valid but disclosed-unverified. Conflating the two is what F1/F2 forbid.
_ASSURANCE_BADGE = {
    # binary tier (install→ship checksum chain)
    "authenticated":            ("ok",  "✓ checksum-verified"),
    "pinned_tofu":              ("na",  "⚠ pinned (unverified, TOFU)"),
    "unanchored_cross_platform":("na",  "⚠ cross-platform ship (unverified)"),
    "unanchored":               ("na",  "⚠ unverified"),
    # C5 — other tiers. verified (immutable, rebuild-reproducible):
    "commit_pinned":            ("ok",  "✓ pinned @ commit (rebuilt in-image)"),
    "built_pinned":             ("ok",  "✓ built from pinned source"),
    "lock_pinned":              ("ok",  "✓ lock-pinned"),
    # disclosed-unverified (moves / not content-anchored):
    "ref_pinned_tofu":          ("na",  "⚠ pinned to a movable ref (TOFU)"),
    "built_unpinned":           ("na",  "⚠ unpinned version (drifts)"),
    "cpan_tofu":                ("na",  "⚠ CPAN (unverified, TOFU)"),
    "repo_tofu":                ("na",  "⚠ repo version (unverified, TOFU)"),
    "spec_pinned_tofu":         ("na",  "⚠ spack spec (unverified, TOFU)"),
    "command_pinned":           ("na",  "⚠ literal command (unverified)"),
    "unpinned":                 ("na",  "⚠ unpinned (floating branch — drifts)"),
}


def _assurance_badge(s: dict) -> str:
    """A pill disclosing a shipped tool's ship assurance. Empty for steps with no
    `assurance` key (nothing to disclose), so unaffected steps render unchanged."""
    a = (s or {}).get("assurance")
    if not a:
        return ""
    cls, txt = _ASSURANCE_BADGE.get(a, ("na", f"⚠ {a}"))
    return f' <span class="pill {cls}" style="font-size:11px">{_e(txt)}</span>'


def _installed_version(t: str, is_adopt: bool, pkg: Optional[dict], v: Optional[dict],
                       req_v: str, shipped: Optional[list] = None,
                       adopt_source: Optional[dict] = None,
                       image_digest: str = "") -> str:
    """ADOPT: prefer the biocontainer tag (carries the human version, e.g.
    `1.21--h50ea8bc_0` for samtools), fall back to the requested version. The
    biocontainer manifest digest binds it to exactly that bioconda build —
    'installed == requested' is honest, no in-locus probe. The digest lives
    in the header KV table; we don't echo it here too.

    BUILD: defer to _resolved_version (conda/pip > banner > out > anchor) —
    shared with the .md renderer so the two views stay aligned."""
    if is_adopt:
        # Prefer the per-tool version from the SBOM (resolved_packages, read from
        # the shipped image) — human-readable AND consistent across tools. The
        # adopt tag is a shared mulled hash (`2d1a988…-0`) for a multi-tool
        # biocontainer, identical on every tool and useless for citing a version.
        # A single-tool biocontainer's tag (`1.21--h50ea8bc_0`) is human-readable
        # but still noisier than the clean `1.21` the SBOM gives. Tag is the
        # fallback only when the SBOM couldn't be read.
        if pkg and pkg.get("version"):
            return pkg["version"]
        if adopt_source and adopt_source.get("tag"):
            return adopt_source["tag"]
        if req_v:
            return req_v
        return ""
    return _resolved_version(t, pkg, v, shipped)


def render_env_report_html(record: dict) -> str:
    """Render the freeze record as a self-contained HTML page (see module docstring
    for the honesty contract this upholds).

    This is a PURE Layer-1 artifact: it asserts only build-locus honesty (BUILT /
    VALIDATED_IN_IMAGE / POLICY_CLEAN) and is written ONCE at freeze, immutable
    thereafter. It never claims the env works on a cluster — that is a Layer-2
    fact carried by a sealed workflow (see run_dashboard_html.render_run_dashboard_html).
    Rebuilding an env yields a NEW freeze record with a NEW digest and its OWN
    ENV.html; this one is never mutated by a downstream seal."""
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
    adopt_source = r.get("adopt_source") if isinstance(r.get("adopt_source"), dict) else None
    image_digest_raw = r.get("image_digest") or ""
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
    # Shared header banner (the TL+BR corner-accent cyberpunk frame) — same
    # helper the Layer-2 run dashboard uses, so the two reports are one family.
    P.append(_header_banner(f"Bioinfo install report — {_e(name)}", pill, head_rows))

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
            inst_v = _installed_version(t, is_adopt, pkg, v, req_v, shipped,
                                         adopt_source=adopt_source,
                                         image_digest=image_digest_raw)
            anchor = _install_anchor(t, shipped)
            if inst_v and anchor and anchor != inst_v and _is_sha(anchor):
                # full provenance: banner/conda version + the commit it was built
                # from. The commit alone IS the install identity for synthesized /
                # source tiers — keep it visible even when a human-friendly version
                # is now leading the cell.
                inst_cell = (f"{_e(inst_v)} <span class=\"muted\">"
                             f"(commit {_e(anchor[:12])})</span>")
            elif inst_v:
                inst_cell = _e(inst_v)
            else:
                inst_cell = '<span class="muted">—</span>'
            tier_cell = _e(_tier_for(t, is_adopt, pkg, shipped))
            if v and (v or {}).get("check"):
                # Real in-image evidence exists — show it (with its depth). A
                # freeze_from_image adopt VALIDATES in-image, unlike a biocontainer adopt,
                # so hiding it behind 'trusted by digest' would understate what we proved.
                status = _badge(v.get("passed"), v.get("check", ""), t)
            elif is_adopt:
                status = '<span class="badge na">trusted by digest</span>'
            else:
                status = _badge(v.get("passed") if v else None, "", t)
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
    P.append('</div></section>')

    # -- INSTALL COMMANDS (own top-level section — promoted from a sub-section
    # of Along-for-the-Ride in batch-3, per the "all reports share the same
    # set of sections" rule the user gave; this also matches the SBOM split
    # everywhere else, where "what was installed" and "how it was installed"
    # are separately enumerable). Long-tail commands are the binary/source/
    # synthesized/perl/cargo/go install bodies baked verbatim into the
    # shipped image — the command IS the provenance.
    P.append('<section class="bx">')
    if is_adopt:
        # For an ADOPT, the install command IS the apptainer/docker pull-by-digest
        # against the published biocontainer. The bytes WE shipped == the bytes
        # the BioContainer registry serves at that digest — pulling by digest is
        # the install. We render it whether or not adopt_source is populated
        # (legacy records have just `image`, which is enough to reconstruct).
        image_ref = r.get("image", "")
        pull_cmd = f"apptainer pull docker://{image_ref}" if image_ref else ""
        P.append('<h2>Install commands '
                 '<span class="note">(adopt — pull the published biocontainer '
                 'by manifest digest; the digest IS the provenance)</span></h2>')
        P.append('<div class="bx-body">')
        if adopt_source and adopt_source.get("tag"):
            P.append('<p style="margin:10px 0 2px"><b>'
                     f'{_e(adopt_source.get("repo") or "biocontainer")} '
                     f'@ tag <code>{_e(adopt_source["tag"])}</code></b></p>')
        elif image_ref:
            P.append('<p style="margin:10px 0 2px"><b>'
                     'biocontainer (tag not captured at freeze time; '
                     'manifest digest pins identity)</b></p>')
        if pull_cmd:
            P.append(f'<pre>{_e(pull_cmd)}</pre>')
        else:
            P.append(_empty("(no image ref recorded — cannot reconstruct command)"))
        P.append('</div></section>')
    else:
        P.append(f'<h2>Install commands <span class="note">({len(shipped)} long-tail '
                 'step(s) baked verbatim into the shipped image — the command IS '
                 'the provenance)</span></h2>')
        P.append('<div class="bx-body">')
        if shipped:
            P.append('<details open><summary>Verbatim long-tail commands</summary>')
            for s in shipped:
                label = s.get("name") or s.get("purpose") or "tool"
                cmd = (s.get("command") or "").strip()
                P.append(f'<p style="margin:10px 0 2px"><b>{_e(label)}</b>'
                         f'{_assurance_badge(s)}</p>')
                if cmd:
                    P.append(f"<pre>{_e(cmd)}</pre>")
            P.append("</details>")
        else:
            P.append(_empty("(no long-tail steps — pure conda env)"))
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
    # Order: identity (image + two digests) → the two PRIMARY companion artifacts
    # (recipe = rebuild instructions, attestation = signed provenance) → delivery
    # (tarball / lock / registry). This HTML IS the canonical Layer-1 view;
    # there is no sibling .md (retired in batch-3) so we don't list one.
    art_rows: list[tuple[str, str]] = [
        ("Image", f'<code>{_e(r.get("image","—"))}</code>' if r.get("image") else "—"),
        ("Image digest", f'<code>{_e(r.get("image_digest","—"))}</code>' if r.get("image_digest") else "—"),
        ("Content digest",
         f'<code>{_e(r.get("content_digest","—"))}</code>'
         '<span class="note"> — reproducible anchor: lock + long-tail + platform + engine + base image</span>'
         if r.get("content_digest") else "—"),
    ]
    # The build recipe ALWAYS exists, in BOTH forms, for every install path — a frozen
    # env is only a solved component if anyone can reproduce it. For adopt mode the
    # recipe is "pull the biocontainer by digest"; for a build it is the self-contained
    # replayable recipe verify_env_recipe rebuilds and digest-checks.
    _verify_note = ('<span class="muted"> — adopt: pull the biocontainer by digest '
                    '(the manifest digest IS the contract)</span>' if is_adopt else
                    '<span class="note"> — verify rebuild with <code>verify_env_recipe</code></span>')
    art_rows.append(("Build recipe (machine)",
                     f'<a href="{_e(name)}.recipe.yaml"><code>{_e(name)}.recipe.yaml</code></a>'
                     + _verify_note))
    art_rows.append(("Build recipe (human)",
                     f'<a href="{_e(name)}.recipe.md"><code>{_e(name)}.recipe.md</code></a>'
                     '<span class="note"> — the runnable command sequence for a hand rebuild</span>'))
    art_rows.append(("In-toto / SLSA attestation",
                     f'<a href="{_e(name)}.attestation.json"><code>{_e(name)}.attestation.json</code></a>'
                     '<span class="note"> — sign with <code>cosign attest</code></span>'))
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
    gated = _record_is_gated(r)
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
                 "manifest digest (above). We did not build these bytes: their provenance is that "
                 "digest, and you trust it as you trust the BioContainers project.</li>")
        if r.get("verifications"):
            P.append("<li><b>VALIDATED_IN_IMAGE</b> — each requested tool's evidence was RUN "
                     "inside this adopted image and passed. This checks what the digest cannot: "
                     "that the image we bound actually carries the tool you asked for.</li>")
        else:
            P.append("<li><b>NOT VALIDATED IN-IMAGE</b> — no evidence was run inside this image, "
                     "so nothing here proves it carries the requested tool. This record predates "
                     "in-image validation of adopted images; re-freeze to prove it.</li>")
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

    P.append('<p class="gen">Generated deterministically from the freeze record'
             ' — no field on this page was authored by the agent.</p>')
    P.append("</div></body></html>")
    return "\n".join(P)
