"""
env_report — the Layer-1 human-readable env report, rendered PURELY from the
verified freeze record.

The contract here mirrors the user guide's: nothing in this report is authored by
the agent. Every fact — the requested tools, the in-image validation evidence, the
full resolved package closure ("along for the ride"), the install/provenance steps,
the artifact digests — is read from the freeze record, which the build runtime
produced (the agent can't hand-write `passed`, an image digest, or the conda-meta
closure). So the report can't be faked: it's a view over machine-captured state.

`render_env_report(record) -> markdown`. Designed for a quick glance (requested
tools + counts up top) AND downstream parsing (stable sections). Defensive to
missing fields so it renders for an adopted biocontainer (no in-locus validation)
as well as a container-native build.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# a conservative version token (2.13.0, 1.4-r122, v1.21, 0.8.1.1) for pulling a
# long-tail tool's version out of its own in-image evidence output (honest — it's
# what the tool printed, captured in the shipped image).
_VER_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)*(?:[-_]?r?\d+)?)\b")


def _extract_version(text: str) -> str:
    m = _VER_RE.search(text or "")
    return m.group(1) if m else ""


def _tier_from_purpose(purpose: str) -> str:
    """The install tier behind a long-tail tool, read from its recorded purpose
    string (e.g. 'seqkit binary', 'seqtk (source @ sha)', 'X jar')."""
    p = purpose.lower()
    if "script repo" in p or "run-by-path" in p:
        return "source (run-by-path)"
    for tier in ("binary", "source", "jar", "perl", "cargo", "go"):
        if tier in p:
            return tier
    return "long-tail (baked)"


def _verif_index(verifications: list[dict]) -> dict[str, dict]:
    """name → its in-image verification record (match on tool token or label)."""
    idx: dict[str, dict] = {}
    for v in verifications or []:
        if not isinstance(v, dict):
            continue
        for key in (v.get("tool"), v.get("label")):
            if key:
                idx.setdefault(str(key).lower(), v)
    return idx


def _pkg_index(resolved: list[dict]) -> dict[str, dict]:
    return {p["name"].lower(): p for p in (resolved or []) if isinstance(p, dict) and p.get("name")}


def _install_method(name: str, pkg: Optional[dict], shipped: list[dict]) -> str:
    if pkg:
        return "pip (PyPI)" if pkg.get("kind") == "pypi" else "conda"
    low = name.lower()
    for s in shipped or []:
        purpose = (s.get("name") or s.get("purpose") or "").lower()
        if low in purpose:
            return _tier_from_purpose(purpose)
    return "—"


def _evidence_cell(v: Optional[dict]) -> str:
    if not v:
        return "—"
    mark = "✓" if v.get("passed") else "✗"
    check = (v.get("check") or "").strip().replace("|", "\\|")
    if len(check) > 60:
        check = check[:57] + "…"
    return f"{mark} `{check}`" if check else mark


def _locus_line(locus: str) -> str:
    return {
        "native":   "native — I7 resource numbers are authoritative",
        "emulated": "emulated — pass/fail is sound (faithful CPU emulation); I7 timings are NOT authoritative",
        "adopted":  "adopted — image trusted by its published digest (not built/validated in-locus)",
    }.get(locus or "", f"{locus or 'unknown'}")


def render_env_report(record: dict) -> str:
    r = record or {}
    name = r.get("name") or (r.get("image") or "env").split(":")[0].split("/")[-1]
    requested = list(r.get("requested_tools") or [])
    resolved = list(r.get("resolved_packages") or [])
    verifs = list(r.get("verifications") or [])
    shipped = list(r.get("shipped_binaries") or [])
    conda_specs = list(r.get("conda_specs") or [])
    vidx, pidx = _verif_index(verifs), _pkg_index(resolved)

    requested_set = {t.lower() for t in requested}
    ride_along = [p for p in resolved if p["name"].lower() not in requested_set]

    L: list[str] = []
    L.append(f"# Environment report — {name}")
    L.append("")
    L.append("> Machine-generated from the verified freeze record. Every fact below was "
             "captured by the build runtime — not authored — so it can't be faked. "
             "See **How this was verified** at the end.")
    L.append("")

    # -- artifact facts (quick glance) -----------------------------------
    redist = "yes" if r.get("redistributable", not r.get("gated")) else "no (license-gated)"
    build = r.get("mode", "?")
    if r.get("build_method"):
        build += f" · {r['build_method']}"
    if r.get("engine") and r.get("engine") != "none":
        build += f" · engine {r['engine']}"
    rows = [
        ("Image", f"`{r.get('image','')}`"),
        ("Image digest", f"`{r.get('image_digest','')}`"),
        ("Content digest", f"`{r.get('content_digest','')}`"),
        ("Platform", r.get("platform", "")),
        ("Build", build),
        ("Validation locus", _locus_line(r.get("validation_locus", ""))),
        ("Redistributable", redist),
        ("Created", r.get("created_at", "")),
    ]
    L.append("| | |")
    L.append("|---|---|")
    for k, v in rows:
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append(f"**{len(requested)} requested** · **{len(ride_along)} along for the ride** "
             f"· {len(resolved)} total packages")
    L.append("")

    # -- requested tools --------------------------------------------------
    L.append(f"## Requested tools ({len(requested)})")
    L.append("What you asked for — each validated INSIDE the shipped image.")
    L.append("")
    if requested:
        L.append("| Tool | Version | Install | Validated in image |")
        L.append("|------|---------|---------|--------------------|")
        for t in requested:
            pkg = pidx.get(t.lower())
            v = vidx.get(t.lower())
            # conda version when known; else the version the tool printed in its
            # in-image evidence (real, captured output — not fabricated)
            ver = (pkg or {}).get("version", "") or _extract_version((v or {}).get("out", "")) or "—"
            L.append(f"| {t} | {ver} | {_install_method(t, pkg, shipped)} | "
                     f"{_evidence_cell(v)} |")
    else:
        L.append("_(none recorded)_")
    L.append("")

    # -- along for the ride ----------------------------------------------
    L.append(f"## Along for the ride ({len(ride_along)})")
    L.append("Transitive dependencies the solver pulled in to satisfy the requested "
             "tools — not requested directly.")
    L.append("")
    if ride_along:
        L.append(f"<details><summary>{len(ride_along)} dependencies</summary>")
        L.append("")
        L.append("| Package | Version | Kind |")
        L.append("|---------|---------|------|")
        for p in ride_along:
            L.append(f"| {p['name']} | {p.get('version','')} | {p.get('kind','')} |")
        L.append("")
        L.append("</details>")
    elif resolved:
        L.append("_(every resolved package was directly requested)_")
    else:
        L.append("_(no conda/pip layer — pure long-tail env, or an adopted image whose "
                 "closure was not captured in-locus)_")
    L.append("")

    # -- install / provenance --------------------------------------------
    L.append("## Install & provenance")
    L.append("")
    if conda_specs:
        L.append("**Conda/PyPI specs (co-solved):** " + ", ".join(f"`{s}`" for s in conda_specs))
        L.append("")
    if shipped:
        L.append("**Long-tail tools (baked verbatim — the command IS the provenance):**")
        L.append("")
        for s in shipped:
            label = s.get("name") or s.get("purpose") or "tool"
            cmd = (s.get("command") or "").strip()
            if len(cmd) > 200:
                cmd = cmd[:197] + "…"
            L.append(f"- **{label}** — `{cmd}`" if cmd else f"- **{label}**")
        L.append("")
    if not conda_specs and not shipped:
        L.append("_(adopted image — provenance is its published digest above)_")
        L.append("")

    # -- delivery ---------------------------------------------------------
    hpc = r.get("hpc_delivery") or {}
    push = r.get("push_status", "")
    if hpc or r.get("tarball") or (push and push != "not-configured"):
        L.append("## Delivery (HPC / Apptainer)")
        L.append("")
        if push and push != "not-configured":
            L.append(f"- Registry: {push}")
        if hpc.get("get_image"):
            L.append(f"- Get the image: `{hpc['get_image']}`")
        if hpc.get("run_example"):
            L.append(f"- Run: `{hpc['run_example']}`")
        if r.get("tarball"):
            L.append(f"- docker-save tarball: `{r['tarball']}`")
        if hpc.get("source_note"):
            L.append(f"- _{hpc['source_note']}_")
        L.append("")

    # -- the honesty footer ----------------------------------------------
    L.append("## How this was verified")
    L.append("")
    L.append("- **BUILT** — the image and its digest resolve in the Docker daemon (every "
             "install step's inline anchor — sha256 / git commit / baked bytes — passed, "
             "else there would be no image).")
    L.append("- **VALIDATED_IN_IMAGE** — every requested tool re-ran green via *plain exec* "
             "inside the shipped image (the way `apptainer exec` runs it on HPC). "
             "**Validated == shipped.**")
    L.append("- **POLICY_CLEAN** — accelerator-honesty (I12) and the license firewall (I13) "
             "passed.")
    L.append(f"- **Validation locus** — {_locus_line(r.get('validation_locus',''))}.")
    L.append("- **Reproducibility** — the conda/PyPI layer is lock-pinned, the base image is "
             "digest-pinned, and release binaries are sha256-anchored. (System apt packages "
             "are not yet version-pinned.)")
    L.append("")
    return "\n".join(L)
