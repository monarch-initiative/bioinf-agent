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


def _first_line(text: str) -> str:
    """A tool's `--version` prints ITS OWN version on the first line; every later
    line is about something else — its dependencies, its copyright, its usage.

    Scraping the whole blob is what made the ENV report cite `bcftools 1.23.1`, a
    version bcftools does not have: the real banner is

        bcftools 9cef4057            <- the populationgenomics csq fork's commit
        Using htslib 1.23.1          <- a DIFFERENT PROJECT's version

    and `9cef4057` is not version-shaped, so a whole-text regex skipped line 1 and
    confidently returned HTSLIB's version under bcftools' name. Restricting to line 1
    is deliberately NARROWER, not smarter: when the tool's own version isn't a
    version-shaped token we now return '' (absence — rendered as "unrecorded") rather
    than wander down the output and find a number that belongs to someone else.
    Making the regex cleverer cannot fix this; only the producer capturing the version
    can, and until it does, absence beats a confident lie."""
    return (text or "").strip().split("\n", 1)[0]


def _extract_version(text: str) -> str:
    m = _VER_RE.search(_first_line(text))
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
    matches the strict shape — agent can't smuggle arbitrary text into this cell.

    FIRST LINE ONLY — see `_first_line`. A version-shaped token further down the
    banner belongs to a dependency, not to this tool."""
    if not banner:
        return ""
    head = _first_line(banner)
    m = re.search(r"[Vv]ersion[: ]+[Vv]?(\d+\.\d+[A-Za-z0-9._-]*)", head)
    if m:
        return m.group(1)
    m = _VER_TOKEN.search(head)
    return m.group(1) if m else ""


def _install_anchor(tool: str, shipped: Optional[list]) -> str:
    """This tool's recorded version, else the pinned commit/ref/tag from its long-tail
    provenance (the install identity for source / synthesized / script-repo tiers).
    Empty for conda / pip / binary tiers (their provenance carries no `@ <ref>`).

    `shipped` holds `ShippedBinary`-shaped dicts. Matching is on the `tool` field —
    the exact PATH command the generator recorded — NOT a substring scan of prose.
    The old `s.get("name") or s.get("purpose")` read a key one of the two producers
    never wrote, so this rung never fired for authors'-image envs and the banner
    scraper below won by default (audit 2026-07-16)."""
    low = (tool or "").lower()
    if not low:
        return ""
    for s in shipped or []:
        if not isinstance(s, dict) or (s.get("tool") or "").lower() != low:
            continue
        # The producer's captured version outranks any anchor we could parse: it is
        # recorded, not inferred. None means NOT CAPTURED — fall through to the ref.
        if s.get("version"):
            return str(s["version"])
        return _version_from_purpose(s.get("provenance") or "")
    return ""


def _is_sha(s: str) -> bool:
    """Looks like a git commit SHA (≥7 hex chars). Used to decide whether to
    label an anchor as 'commit <hex>' (vs a tag like 'v1.4' that we'd suppress
    when a banner version is already showing)."""
    return bool(s) and len(s) >= 7 and bool(re.fullmatch(r"[0-9a-fA-F]+", s))


def _resolved_version(tool: str, pkg: Optional[dict], v: Optional[dict],
                      shipped: Optional[list]) -> str:
    """THE single definition of a tool's installed-version cell — used by every
    renderer, so the views can't disagree. (They did: `resources._semantic_versions`
    forked this chain in tier 7, read only rung 1, and reported `bcftools: null`
    while the ENV report said `1.23.1`. Two readings of one fact, both wrong,
    disagreeing. Add a consumer? Call THIS. Don't fork it.)

    Ordered RECORDED-BEFORE-SCRAPED, which is the honesty ordering:

      1. conda/pip metadata version   — RECORDED by the package manager
      2. shipped_binaries[].version   — RECORDED by the long-tail producer
      3. banner version, first line    — SCRAPED from what the binary printed
      4. evidence `out`, first line    — SCRAPED; the evidence command is agent-supplied
      5. install anchor (`@ <ref>`)    — the build-time pin from the provenance string

    1-2 are facts someone wrote down. 3-4 are inferences from prose, and an inference
    is exactly what cited htslib's version for bcftools — so anything recorded must
    outrank them. This ordering used to put the banner second, which meant a scraped
    guess beat a captured fact whenever both existed.

    Returns '' when nothing resolves — and '' means UNRECORDED. Render it as absence.
    Never let a caller substitute a plausible number for it (Rule 2)."""
    cv = (pkg or {}).get("version", "")
    if cv:
        return cv
    rv = _recorded_version(tool, shipped)
    if rv:
        return rv
    bv = _version_from_banner((v or {}).get("banner", ""))
    if bv:
        return bv
    ev = _extract_version((v or {}).get("out", ""))
    if ev:
        return ev
    return _install_anchor(tool, shipped)


def _recorded_version(tool: str, shipped: Optional[list]) -> str:
    """This tool's version as CAPTURED by its long-tail producer — a fact, not a
    parse. '' when the producer did not capture one (which several tiers currently do
    not; that is a known gap, and stating it beats guessing)."""
    low = (tool or "").lower()
    if not low:
        return ""
    for s in shipped or []:
        if isinstance(s, dict) and (s.get("tool") or "").lower() == low and s.get("version"):
            return str(s["version"])
    return ""


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
    """How this tool got into the image. Matches on the recorded `tool` field, not a
    substring scan of prose — `low in purpose` also matched any tool whose name was a
    substring of another's provenance line."""
    if pkg:
        return "pip (PyPI)" if pkg.get("kind") == "pypi" else "conda"
    low = name.lower()
    for s in shipped or []:
        if not isinstance(s, dict) or (s.get("tool") or "").lower() != low:
            continue
        if s.get("tier"):                    # the tier disclosed itself — believe it
            return str(s["tier"])
        return _tier_from_purpose((s.get("provenance") or "").lower())
    return "—"


def _locus_line(locus: str) -> str:
    return {
        "native":   "native — I7 resource numbers are authoritative",
        "emulated": "emulated — pass/fail is sound (faithful CPU emulation); I7 timings are NOT authoritative",
        "adopted":  "adopted — image trusted by its published digest (not built/validated in-locus)",
    }.get(locus or "", f"{locus or 'unknown'}")
