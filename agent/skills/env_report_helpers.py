"""
env_report_helpers — the small, pure helper functions the HTML env report
renderer reads from. Extracted from env_report.py when the .md renderer was
retired (batch-3, 2026-05-27): the helpers were the ONLY shared surface, so
keeping a stub env_report module just to host them was dead weight. The .html
renderer is now the canonical Layer-1 deliverable.

The contract these uphold:
  - PURE over the freeze record (no I/O, no clock, no environment reads)
  - DETERMINISTIC — same record in, same string out
  - HONESTY-ORDERED for version resolution: conda/pip metadata > self-printed
    banner > evidence-output token > install anchor. The first three are
    runtime-captured (non-fakeable); the last is the build-time anchor.

If you need to read a value from the record into the env report, prefer one of
the helpers below over a fresh fork — keeping the honesty-ordering centralized
is what makes the report's claims reproducible across renderers.
"""

from __future__ import annotations

import re
from typing import Optional

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
    for source / synthesized / script-repo tiers — the ref IS the pinned
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
    """The single source of truth for a tool's installed-version cell. Honesty-
    ordered:

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

    Carried forward from the retired env_report module — was the R1 fix point
    that brought the .md renderer to parity with the .html one; .md is gone now
    (batch-3) but the helper stays because the .html still needs it for the
    requested-version cell in adopt mode (the biocontainer's digest binds the
    artifact to exactly that version, so 'installed == requested' is honest
    with no in-locus probe).
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


def _locus_line(locus: str) -> str:
    return {
        "native":   "native — I7 resource numbers are authoritative",
        "emulated": "emulated — pass/fail is sound (faithful CPU emulation); I7 timings are NOT authoritative",
        "adopted":  "adopted — image trusted by its published digest (not built/validated in-locus)",
    }.get(locus or "", f"{locus or 'unknown'}")
