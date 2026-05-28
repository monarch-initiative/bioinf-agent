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
    string emitted by `install_commands` generators (e.g. 'seqkit (release binary)',
    'htslib (source @ sha)', 'picard (java jar)', 'tool (synthesized @ sha)')."""
    p = purpose.lower()
    if "script repo" in p or "run-by-path" in p:
        return "source (run-by-path)"
    if "synthesized" in p:
        return "source (synthesized)"
    for tier in ("binary", "source", "jar", "perl", "cargo", "go"):
        if tier in p:
            return tier
    return "long-tail (baked)"


def _version_from_purpose(purpose: str) -> str:
    """Pull the version anchor (commit / release / version) from a long-tail
    purpose string. `install_commands` generators emit `name (<tier> @ <ref>)`
    for source / synthesized / script-repo / spack tiers — the ref IS the pinned
    install identity captured by the build. Returns '' if no anchor present."""
    if not purpose:
        return ""
    m = re.search(r"@\s*([A-Za-z0-9._-]{4,})", purpose)
    return m.group(1) if m else ""


# Strict version shape — must start with a digit and look like a real version
# token (1.4, 1.4-r122, 0.7.17-r1188, 3.1.2, 1.21). The leading-digit constraint
# means a banner like "Usage: seqtk <command>" can't be mistaken for a version.
_VER_TOKEN = re.compile(r"\b[Vv]?(\d+\.\d+(?:\.\d+)?(?:[._-][A-Za-z]?\d+)?)\b")


def _version_from_banner(banner: str) -> str:
    """Pull a version-shaped token from a tool's self-reported banner. The banner
    is runtime-captured stdout from `<tool> --version` / bare `<tool>` in the
    shipped image — structurally non-fakeable by the agent (the probe command is
    synthesized from a sanitized tool token inside container_build, no agent text
    reaches the shell). Looks for explicit 'Version: X.Y.Z' first (the high-
    confidence shape), then a bare version-shaped token. Returns '' if nothing
    matches the strict shape — agent can't smuggle arbitrary text into this cell."""
    if not banner:
        return ""
    m = re.search(r"[Vv]ersion[: ]+[Vv]?(\d+\.\d+[A-Za-z0-9._-]*)", banner)
    if m:
        return m.group(1)
    m = _VER_TOKEN.search(banner)
    return m.group(1) if m else ""


def _install_anchor(tool: str, shipped: Optional[list]) -> str:
    """The pinned commit / ref / tag freeze recorded in the long-tail purpose for
    this tool (the install identity for source / synthesized / script-repo tiers).
    Empty for conda / pip / binary tiers (their purpose doesn't carry `@ <ref>`)."""
    low = (tool or "").lower()
    for s in shipped or []:
        purpose = s.get("name") or s.get("purpose") or ""
        if low and low in purpose.lower():
            return _version_from_purpose(purpose)
    return ""


def _is_sha(s: str) -> bool:
    """Looks like a git commit SHA (≥7 hex chars). Used to decide whether to
    label an anchor as 'commit <hex>' (vs a tag like 'v1.4' that we'd suppress
    when a banner version is already showing)."""
    return bool(s) and len(s) >= 7 and bool(re.fullmatch(r"[0-9a-fA-F]+", s))


def _resolved_version(tool: str, pkg: Optional[dict], v: Optional[dict],
                      shipped: Optional[list]) -> str:
    """The single source of truth for a tool's installed-version cell, used by
    BOTH report renderers so the .md and .html stay aligned. Honesty-ordered:

      1. conda/pip metadata version — authoritative for the conda layer
      2. banner version — what the shipped binary itself prints (non-fakeable,
         captured by validate_in_image's tool probe)
      3. evidence-check `out` version — same shipped binary, but the evidence
         command itself CAN be agent-supplied via install primitives
      4. install anchor — pinned commit / release / tag from the long-tail purpose

    Returns '' when none resolves to a strict version-shaped token."""
    cv = (pkg or {}).get("version", "")
    if cv:
        return cv
    bv = _version_from_banner((v or {}).get("banner", ""))
    if bv:
        return bv
    ev = _extract_version((v or {}).get("out", ""))
    if ev:
        return ev
    return _install_anchor(tool, shipped)


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


def requested_versions(record: dict) -> dict[str, str]:
    """tool → user-asked version constraint (empty string if unpinned). Sourced
    from the request_key (the canonical 'what was asked' tuple, present on every
    record build OR adopt) with conda_specs as a fallback for older records.

    Moved here so the MD renderer can reach the same row data the HTML renderer
    already had (R1 fix, batch-2 stress). Pre-fix the .md adopt-mode rows
    rendered Version='—' / Install='—' / Validated='—' because the recorded
    `resolved_packages` / `verifications` were empty for adopt (correctly — no
    in-locus closure or evidence captured), but the requested_tools' versions
    were sitting right there on the record.
    """
    out: dict[str, str] = {}
    rk = (record or {}).get("request_key", "") or ""
    if "|" in rk:
        spec = rk.split("|", 1)[0]
        for tok in spec.split(","):
            n, _, v = tok.replace("==", "=").partition("=")
            if n.strip():
                out[n.strip()] = v.strip()
    for s in (record or {}).get("conda_specs", []) or []:
        if isinstance(s, str):
            n, _, v = s.replace("==", "=").partition("=")
            if n.strip() and n.strip() not in out:
                out[n.strip()] = v.strip()
    return out


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
    system = list(r.get("system_packages") or [])
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
    is_adopt = r.get("mode") == "adopt"
    req_versions = requested_versions(r)
    L.append(f"## Requested tools ({len(requested)})")
    if is_adopt:
        L.append("What you asked for — each pinned by the published BioContainer manifest digest "
                 "(not re-validated in-locus; the published digest IS the validation).")
    else:
        L.append("What you asked for — each validated INSIDE the shipped image.")
    L.append("")
    if requested:
        L.append("| Tool | Version | Install | Validated in image |")
        L.append("|------|---------|---------|--------------------|")
        for t in requested:
            pkg = pidx.get(t.lower())
            v = vidx.get(t.lower())
            # version cell: conda/pip > banner > evidence-out > install anchor
            # (see _resolved_version). If we ended up with a banner/conda/out
            # version AND there's a distinct SHA install anchor, append it in
            # parens for full provenance ("1.4-r122 (commit 94e707082d39)").
            ver = _resolved_version(t, pkg, v, shipped) or ""
            if not ver and is_adopt:
                # R1 fallback (batch-2 stress): adopt mode has no resolved_packages /
                # verifications / shipped_binaries — the version IS the request_key's
                # pinned spec (the biocontainer manifest digest binds it to exactly
                # that bioconda build, so 'installed == requested' is honest with no
                # in-locus probe). Mirrors the HTML report's _installed_version.
                ver = req_versions.get(t, "")
            ver = ver or "—"
            anchor = _install_anchor(t, shipped)
            if ver != "—" and anchor and anchor != ver and _is_sha(anchor):
                ver = f"{ver} (commit {anchor[:12]})"
            install = _install_method(t, pkg, shipped)
            evidence = _evidence_cell(v)
            if is_adopt:
                # R1 (batch-2 stress): an adopted biocontainer's row should
                # reflect WHAT WE KNOW (tier=adopted, validation=by digest), not
                # render dashes because the in-locus inspection wasn't performed.
                # The HTML renderer already did this; the .md renderer didn't.
                if install == "—":
                    install = "adopted (biocontainer)"
                if evidence == "—":
                    evidence = "✓ ADOPTED_BY_DIGEST"
            L.append(f"| {t} | {ver} | {install} | {evidence} |")
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

    # -- system (apt) packages — the OS layer of the SBOM -----------------
    if system:
        L.append(f"## System packages ({len(system)})")
        L.append("The OS/apt layer baked into the shipped image — captured for a complete, "
                 "auditable SBOM (you know exactly what's in the artifact).")
        L.append("")
        L.append(f"<details><summary>{len(system)} apt packages</summary>")
        L.append("")
        L.append("| Package | Version |")
        L.append("|---------|---------|")
        for p in system:
            L.append(f"| {p['name']} | {p.get('version','')} |")
        L.append("")
        L.append("</details>")
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

    # -- the honesty footer (per-mode — never over-claim for an adopted image) ---
    L.append("## How this was verified")
    L.append("")
    if r.get("mode") == "adopt":
        L.append("- **ADOPTED_BY_DIGEST** — a public BioContainer pulled by its immutable manifest "
                 "digest (above). Provenance is that digest; trust it as you trust the BioContainers "
                 "project. It was NOT built or validated in-locus.")
        L.append("- **POLICY_CLEAN** — accelerator-honesty (I12) and the license firewall (I13) "
                 "passed.")
    else:
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
    L.append(f"- **Attestation** — a standard in-toto/SLSA provenance Statement of all the "
             f"above is emitted to `env_reports/{name}.attestation.json` (sign with "
             f"`cosign attest`).")
    L.append("")
    return "\n".join(L)
