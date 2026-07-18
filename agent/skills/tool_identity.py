"""Identity disclosure (audit finding #8): what a REQUESTED tool says it IS.

`resolve()` surfaces a tool's self-description at resolve time and it dies there — no
frozen artifact carries it, so a human reading a frozen env's ENV.html cannot tell that
`cellranger` resolved to CRAN's "Translate Spreadsheet Cell Ranges" parser. The Layer-1
contract proves a tool WORKS (VALIDATED_IN_IMAGE); it never proved it is the tool you
MEANT. This module is the PRODUCER that puts identity on disk: freeze calls `capture()`
with the requested tools + the in-image SBOM, and it reads each tool's own words.

Two rules make the disclosure honest rather than a liability:

  1. TIED TO THE INSTALL, never a bare-name guess. A tool present in the SBOM as a
     conda/pip package gets THAT package's registry summary. A tool installed from a
     non-registry tier (binary / source / jar) gets `self_description=None` — honest
     silence, not a borrowed identity from a same-named package that was never installed.
     This is the trap the naive version falls into: a correctly-installed ONT `dorado`
     binary must NOT be labelled with astronomy-PyPI `dorado`'s summary.

  2. AGENT-ASSERTED, best-effort, gates NOTHING. Any probe failure leaves that tool's
     `self_description` None and NEVER fails a freeze. Identity DISCLOSES; it does not
     validate. The `ToolIdentity` model this feeds is labelled OUTSIDE the verified
     surface everywhere it renders.

The dicts returned here are validated against `core_data.ToolIdentity` at
`EnvCache.register` — this module states facts, the seam enforces the shape.
"""
from __future__ import annotations

from typing import Optional

# R / Bioconductor / Perl conda packages carry a channel prefix the requested tool name
# omits (`seurat` -> `r-seurat`, `limma` -> `bioconductor-limma`). Match these forms so a
# conda-installed R tool still resolves to its package + description; a miss still stays a
# miss — we never fall through to a bare-name registry guess, which is how a same-name
# squatter's blurb gets misattributed to the tool that actually shipped.
_SBOM_PREFIXES = ("", "r-", "bioconductor-", "perl-")


def _match_sbom(tool: str, sbom: dict[str, dict]) -> Optional[dict]:
    """The installed package for `tool`, or None. Exact name first, then the conda
    channel-prefix forms. No bare-name fallback: an unmatched tool discloses nothing."""
    t = (tool or "").lower()
    if not t:
        return None
    for pre in _SBOM_PREFIXES:
        hit = sbom.get(f"{pre}{t}")
        if hit:
            return hit
    return None


def _registry_summary(kind: str, name: str) -> tuple[Optional[str], Optional[str]]:
    """(self_description, source) from the registry the SBOM says the package came from.

    Reuses the resolver's OWN registry probes so "how we read a package summary" has a
    single home — the same `about.summary` / PyPI `Summary` that `resolve()` surfaces.
    Best-effort: a network/parse failure returns (None, None), never raises."""
    from agent.skills import resolver as _R
    try:
        if kind == "conda":
            d = _R.probe_conda(name)
            if d.get("available"):
                return (((d.get("summary") or "").strip() or None), "conda")
        elif kind == "pypi":
            d = _R.probe_pypi(name)
            if d.get("available"):
                return (((d.get("summary") or "").strip() or None), "pypi")
    except Exception:
        pass
    return None, None


def capture(requested_tools: list[str], resolved_packages: list[dict],
            nonregistry_tools: Optional[list[str]] = None) -> list[dict]:
    """One ToolIdentity dict per requested tool — the producer for `record["tool_identities"]`.

    EXHAUSTIVE and best-effort: every requested tool gets a record (the disclosure is never
    silently short), and a tool with no registry match or a failed probe gets
    `self_description=None` + `source=None`. Returns plain dicts; `EnvCache.register`
    validates each against `ToolIdentity` (forbid-extras, no fabricated defaults).

    `nonregistry_tools` is the set of requested tools installed from a NON-REGISTRY tier
    (binary / source / jar / cargo / go / perl — `freeze.non_conda_installs`). Such a tool
    is NOT its own provider in the conda/pip closure, so a bare name-match against that
    closure would borrow a SAME-NAMED transitive dependency's identity — the exact trap the
    module exists to avoid (a correctly-installed ONT `dorado` binary vs astronomy-PyPI
    `dorado` that rode in as another tool's dep; a source-built `cluster` vs the base-R
    `r-cluster` dep). These tools skip the SBOM match entirely and get honest `None`.

    KNOWN LIMITATION (channel axis): `_registry_summary` probes conda by name across both
    bioconda and conda-forge and takes the higher-version channel's summary — the SBOM does
    not record which channel shipped. For a name that hosts DIFFERENT projects across
    channels with an inverted version, this could surface the wrong channel's blurb. Rare
    (the two channels keep a largely collision-free namespace) and it never fabricates a
    capability — the field stays labelled unverified — so it is documented, not gated."""
    nonregistry = {(t or "").lower() for t in (nonregistry_tools or [])}
    sbom: dict[str, dict] = {}
    for p in (resolved_packages or []):
        n = (p.get("name") or "").lower()
        if n and n not in sbom:
            sbom[n] = p

    out: list[dict] = []
    for tool in (requested_tools or []):
        desc: Optional[str] = None
        source: Optional[str] = None
        pkg: Optional[str] = None
        ver: Optional[str] = None
        # A non-registry-tier tool gets honest silence — never a same-named closure entry's
        # identity. Only registry-tier tools (conda/pip) are matched against the SBOM.
        if (tool or "").lower() not in nonregistry:
            entry = _match_sbom(tool, sbom)
            if entry:
                pkg = entry.get("name") or None
                ver = entry.get("version") or None
                desc, source = _registry_summary(entry.get("kind") or "", pkg or tool)
        out.append({
            "tool": tool,
            "self_description": desc,
            "source": source,
            "package": pkg,
            "version": ver,
        })
    return out
