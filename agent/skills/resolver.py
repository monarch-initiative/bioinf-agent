"""
Resolver — given a requested tool, decide WHICH install tier to use and record
WHY. The one piece of genuine judgment in the system; every install flows from
the decision it produces.

It PROBES availability independently per tier (so it can rank AND record the
rejected alternatives), then RANKS by a preference order chosen to maximize
reproducibility + clean containerization + least build fragility:

    conda  >  language-registry (pip / CRAN / Bioconductor)
           >  release-binary  >  source-build  >  manual

The probes are observations (live registry lookups). The ranking is the
judgment. The ResolutionDecision (chosen tier + install call + rationale +
rejected alternatives) is the auditable output — the "why this method" that the
honesty contract otherwise lacked. Binary/source tiers only apply when a github
repo is supplied (you can't probe a release asset from a bare tool name).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

# Preference order — lower index wins. Cross-cutting concerns (gpu/service/
# license/db) are flags layered on top, not tiers.
TIER_ORDER = ["conda", "pip", "cran", "bioconductor", "binary", "source", "manual"]

_TIER_RATIONALE = {
    "conda":        "on bioconda/conda-forge — solver-managed, pinned, containerizes cleanly (preferred)",
    "pip":          "on PyPI — language registry; chosen when not on conda",
    "cran":         "on CRAN — R language registry via install_r_package(source=cran)",
    "bioconductor": "on Bioconductor — R via install_r_package(source=bioconductor)",
    "binary":       "precompiled release binary — exact bytes (sha256), but platform-specific",
    "source":       "build from source at a pinned commit — image digest becomes the lock",
    "manual":       "no automatable tier found — needs a hand-authored path",
}


def _get_json(url: str, timeout: int = 12) -> Optional[Any]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "bioinf-agent-resolver"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _head_ok(url: str, timeout: int = 12) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "bioinf-agent-resolver"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def probe_conda(name: str, timeout: int = 12) -> dict[str, Any]:
    """Available on bioconda or conda-forge? (anaconda.org package API)."""
    for channel in ("bioconda", "conda-forge"):
        data = _get_json(f"https://api.anaconda.org/package/{channel}/{name.lower()}", timeout)
        if isinstance(data, dict) and data.get("versions"):
            return {"available": True, "channel": channel,
                    "latest": data.get("latest_version") or (data["versions"][-1])}
    return {"available": False}


def probe_pypi(name: str, timeout: int = 12) -> dict[str, Any]:
    data = _get_json(f"https://pypi.org/pypi/{name}/json", timeout)
    if isinstance(data, dict) and data.get("info"):
        return {"available": True, "latest": data["info"].get("version")}
    return {"available": False}


def probe_cran(name: str, timeout: int = 12) -> dict[str, Any]:
    data = _get_json(f"https://crandb.r-pkg.org/{name}", timeout)
    if isinstance(data, dict) and data.get("Package"):
        return {"available": True, "latest": data.get("Version")}
    return {"available": False}


def probe_github(repo: str, timeout: int = 12) -> dict[str, Any]:
    """For a github 'owner/repo': does it exist (→ source tier) and does its
    latest release carry downloadable assets (→ binary tier)?"""
    out = {"repo_exists": False, "has_release_assets": False, "assets": []}
    if not repo or "/" not in repo:
        return out
    if _get_json(f"https://api.github.com/repos/{repo}", timeout) is None:
        return out
    out["repo_exists"] = True
    rel = _get_json(f"https://api.github.com/repos/{repo}/releases/latest", timeout)
    if isinstance(rel, dict):
        assets = [a.get("browser_download_url") for a in (rel.get("assets") or [])
                  if a.get("browser_download_url")]
        out["has_release_assets"] = bool(assets)
        out["assets"] = assets[:10]
        out["tag"] = rel.get("tag_name")
    return out


def probe_bioconductor(name: str, timeout: int = 12) -> dict[str, Any]:
    """Best-effort: does a release Bioconductor package page exist for `name`?"""
    ok = _head_ok(f"https://bioconductor.org/packages/release/bioc/html/{name}.html", timeout)
    return {"available": ok}


def _is_ambiguous(availability: dict[str, dict], language: str) -> bool:
    """A bare tool name is dangerously ambiguous when it resolves in BOTH a
    Python registry (PyPI) and an R registry (CRAN) with no language hint — they
    are almost certainly different packages (e.g. PyPI `ape` ≠ CRAN's R `ape`).
    A language hint removes the ambiguity by construction."""
    if language:
        return False
    return bool(availability.get("pip", {}).get("available")
                and availability.get("cran", {}).get("available"))


def rank_decision(availability: dict[str, dict], prefer: Optional[str] = None) -> dict[str, Any]:
    """Pure: given per-tier availability, pick the tier and explain. `prefer`
    forces a tier when it is available. Returns chosen tier (or None), the
    ordered available tiers, and the rejected alternatives."""
    available = [t for t in TIER_ORDER if availability.get(t, {}).get("available")]
    chosen = None
    if prefer and prefer in available:
        chosen = prefer
    elif available:
        chosen = available[0]

    if chosen is None:
        return {"chosen": None, "available": [], "alternatives": [],
                "rationale": "no automatable tier found — fall back to source (with a repo) or manual"}

    why = _TIER_RATIONALE.get(chosen, chosen)
    others = [t for t in available if t != chosen]
    rationale = f"{chosen}: {why}" + (
        f"; also available but lower-priority: {', '.join(others)}" if others else ""
    )
    if prefer == chosen and others:
        rationale = f"{chosen} (forced via prefer): {why}; would otherwise consider {', '.join(others)}"
    return {
        "chosen": chosen,
        "available": available,
        "alternatives": [{"tier": t, "detail": availability.get(t, {})} for t in others],
        "rationale": rationale,
    }


def _install_call(tier: str, tool: str, version: str, detail: dict, github_repo: str) -> str:
    v = version or detail.get("latest") or ""
    if tier == "conda":
        base = detail.get("r_spec") or tool          # r-{name} when resolving an R tool via conda
        spec = f"{base}={v}" if v else base
        return f'install_conda_packages(env, [{{"spec": "{spec}", "channel": "{detail.get("channel","bioconda")}"}}])'
    if tier == "pip":
        return f'install_pip_package(env, "{tool}"' + (f', version="{v}")' if v else ")")
    if tier == "cran":
        return f'install_r_package(env, "{tool}", source="cran")'
    if tier == "bioconductor":
        return f'install_r_package(env, "{tool}", source="bioconductor")'
    if tier == "binary":
        asset = (detail.get("assets") or ["<release-asset-url>"])[0]
        return f'install_release_binary(env, "{tool}", url="{asset}", sha256="<published>")'
    if tier == "source":
        return f'install_git_repo(env, "https://github.com/{github_repo}", "{tool}", ref="<tag/commit>")'
    return "# no automatable tier — author a manual path (and stage_authored_artifact for any scripts)"


def resolve(
    tool: str,
    version: str = "",
    github_repo: str = "",
    prefer: Optional[str] = None,
    language: str = "",
    timeout: int = 12,
) -> dict[str, Any]:
    """Probe the applicable tiers for `tool` and return a ResolutionDecision:
    chosen tier + the concrete install primitive call + rationale + the rejected
    alternatives that were actually available.

    `language` ('python' | 'r' | '') disambiguates cross-registry name
    collisions by restricting the tier set to one ecosystem — for 'r' it probes
    CRAN, Bioconductor, and conda's `r-{name}` (NOT bare PyPI). With no hint, a
    name found in BOTH PyPI and CRAN is flagged `ambiguous` (different packages
    likely) so the caller disambiguates rather than the resolver guessing.

    `github_repo` ('owner/repo') unlocks the binary/source tiers (a release
    asset can't be probed from a bare tool name). `prefer` forces a tier when
    available; it composes with `language`.
    """
    language = (language or "").lower().strip()
    availability: dict[str, dict] = {}
    if language == "r":
        rconda = probe_conda(f"r-{tool}", timeout)
        if rconda.get("available"):
            rconda["r_spec"] = f"r-{tool}"
        availability["conda"]        = rconda
        availability["cran"]         = probe_cran(tool, timeout)
        availability["bioconductor"] = probe_bioconductor(tool, timeout)
    elif language == "python":
        availability["conda"] = probe_conda(tool, timeout)
        availability["pip"]   = probe_pypi(tool, timeout)
    else:
        availability["conda"] = probe_conda(tool, timeout)
        availability["pip"]   = probe_pypi(tool, timeout)
        availability["cran"]  = probe_cran(tool, timeout)

    if github_repo:
        gh = probe_github(github_repo, timeout)
        availability["binary"] = {"available": gh["has_release_assets"], **gh}
        availability["source"] = {"available": gh["repo_exists"], **gh}

    decision = rank_decision(availability, prefer=prefer)
    ambiguous = _is_ambiguous(availability, language)
    chosen = decision["chosen"]
    decision.update({
        "tool": tool,
        "version": version or None,
        "language": language or None,
        "github_repo": github_repo or None,
        "ambiguous": ambiguous,
        "probed": availability,
    })
    if ambiguous:
        decision["rationale"] = (
            f"AMBIGUOUS: '{tool}' exists on BOTH PyPI (python) and CRAN (R) — likely "
            f"different packages. Pass language='python'|'r' (or prefer=) to disambiguate. "
            + decision["rationale"]
        )
    if chosen:
        decision["install_call"] = _install_call(
            chosen, tool, version, availability.get(chosen, {}), github_repo
        )
    return decision
