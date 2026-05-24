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

import hashlib
import json
import re
import urllib.error
import urllib.request
from typing import Any, Optional

# Preference order — lower index wins. Cross-cutting concerns (gpu/service/
# license/db) are flags layered on top, not tiers.
#
# `synthesis` is the UNIVERSAL repo tail (the "reduce infinity to 1" floor): for any
# fetchable repo it ranks ABOVE the conventional `source` generator, because the
# agent reads the tool's OWN build files (gated by provenance + grounding + the
# contract) and so handles EVERY repo — half-baked, run-by-path, custom build — not
# just the conventional make+binary case the `source` generator assumes. `source`
# (and binary) survive as opt-in FAST-PATHS, not the boundary of installable.
TIER_ORDER = ["conda", "pip", "cran", "bioconductor", "binary", "spack",
              "synthesis", "source", "manual"]

_TIER_RATIONALE = {
    "conda":        "on bioconda/conda-forge — solver-managed, pinned, containerizes cleanly (preferred)",
    "pip":          "on PyPI — language registry; chosen when not on conda",
    "cran":         "on CRAN — R language registry via install_r_package(source=cran)",
    "bioconductor": "on Bioconductor — R via install_r_package(source=bioconductor)",
    "binary":       "precompiled release binary — exact bytes (sha256), but platform-specific",
    "spack":        "in the Spack HPC registry — a curated from-source recipe (community-maintained); "
                    "store baked under /opt/tools so RPATHs resolve in the slim image. Build is slow "
                    "(from source) — best on a native amd64 host",
    "synthesis":    "agent reads the repo's OWN build files and synthesizes a grounded, contract-"
                    "gated install — the universal path for any source/bespoke tool (no per-tool recipe)",
    "source":       "conventional `make`+binary fast-path (install_git_repo) — faster than synthesis "
                    "when the repo builds conventionally; synthesis is the robust fallback otherwise",
    "manual":       "no fetchable source at all — needs a hand-authored path",
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


def _looks_like_serial(v: str) -> bool:
    """A bare integer of 8+ digits (e.g. 20151031) is a date/serial 'version' from
    an abandoned build, NOT a semver — it sorts as a huge release number and would
    masquerade as 'latest'. (Real CalVer like xarray's 2026.4.0 has dots and is
    NOT caught.)"""
    s = str(v).strip()
    return s.isdigit() and len(s) >= 8


def _version_key(v: str):
    try:
        from packaging.version import Version
        return Version(str(v))
    except Exception:
        return None


def _pick_latest(versions: list, latest_hint: str = "") -> str:
    """Most recent NORMAL version: drop serial/date anomalies, then pick the max by
    PEP440 ordering. Falls back to the (non-serial) hint or last entry if nothing
    parses — so a genuinely odd-versioned package still resolves to something."""
    vs = [str(v) for v in (versions or []) if v]
    pool = [v for v in vs if not _looks_like_serial(v)] or vs
    parsed = [(k, v) for v in pool if (k := _version_key(v)) is not None]
    if parsed:
        return max(parsed, key=lambda kv: kv[0])[1]
    if latest_hint and not _looks_like_serial(latest_hint):
        return latest_hint
    return pool[-1] if pool else ""


def probe_conda(name: str, timeout: int = 12) -> dict[str, Any]:
    """Available on bioconda or conda-forge? Probes BOTH channels and picks the one
    with the higher REAL version — guards against an abandoned date-versioned build
    on one channel (e.g. bioconda's hmmlearn reports latest_version='20151031')
    shadowing the maintained package on the other. Ties keep bioconda (probed
    first), so bio-primary tools are unaffected."""
    best = None  # (version_key, channel, version)
    for channel in ("bioconda", "conda-forge"):
        data = _get_json(f"https://api.anaconda.org/package/{channel}/{name.lower()}", timeout)
        if isinstance(data, dict) and data.get("versions"):
            ver = _pick_latest(data["versions"], data.get("latest_version") or "")
            key = _version_key(ver)
            if best is None or (key is not None and (best[0] is None or key > best[0])):
                best = (key, channel, ver)
    if best:
        return {"available": True, "channel": best[1], "latest": best[2]}
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


def probe_spack(name: str, timeout: int = 12) -> dict[str, Any]:
    """Is `name` a Spack builtin package (the HPC from-source registry, ~thousands
    of recipes)? Checks the package recipe exists via raw.githubusercontent (no Spack
    install, and not the rate-limited GitHub API). Spack names are lowercase; C/C++/
    Fortran tools usually match the bare name (py-/r- prefixes exist but aren't probed
    here). ADVISORY for now — see _TIER_RATIONALE note: the buildable Spack tier
    needs slim-runtime relocation work before it can be a CHOSEN build tier."""
    # Spack v1.0 (2025) split builtin recipes into the spack/spack-packages repo:
    # repos/spack_repo/builtin/packages/<name>/package.py
    url = (f"https://raw.githubusercontent.com/spack/spack-packages/develop/"
           f"repos/spack_repo/builtin/packages/{name.lower()}/package.py")
    return {"available": _head_ok(url, timeout), "package": name.lower()}


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


# ---------------------------------------------------------------------------
# Ship-platform asset resolution — the release-binary tier's cross-arch bridge.
#
# A binary is installed from the HOST-platform asset (e.g. *_darwin_arm64, so it
# runs for validation on an Apple-Silicon dev box), but the SHIP target is
# linux/amd64. The host binary cannot containerize. freeze() therefore re-fetches
# the SAME release's linux/amd64 asset and bakes THAT into the image, sha256-
# anchored — same tool, same version, same publisher, correct platform.
# ---------------------------------------------------------------------------

_GH_RELEASE_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)$"
)

# Arch tokens as they appear in real release-asset filenames.
_ARCH_ALIASES = {
    "amd64": ["amd64", "x86_64", "x86-64", "x64", "linux64", "linux-64", "64bit", "64-bit"],
    "arm64": ["arm64", "aarch64"],
}
# Checksum / signature / metadata sidecars that sit next to the real asset.
_SIDECAR_SUFFIXES = (
    ".md5", ".sha256", ".sha256sum", ".sha1", ".sig", ".asc",
    ".txt", ".sbom", ".json", ".pem", ".cert", ".sha512",
)
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip")


def _pick_platform_asset(
    assets: list[str], target_os: str = "linux", target_arch: str = "amd64"
) -> Optional[str]:
    """Pure selection (no network): from a list of asset URLs/names, choose the
    one matching target_os + target_arch. Returns the URL or None.

    Rules: the OS token MUST be present; any arch alias counts; an asset naming
    the WRONG arch is rejected (so a linux/arm64 build is never picked for
    linux/amd64); checksum/signature sidecars are excluded. Ties break toward an
    archive, then the least-adorned (shortest) name."""
    arch_ok = _ARCH_ALIASES.get(target_arch, [target_arch])
    other_arch = [a for k, v in _ARCH_ALIASES.items() if k != target_arch for a in v]
    cands: list[str] = []
    for url in assets:
        if not url:
            continue
        name = url.rsplit("/", 1)[-1].lower()
        if name.endswith(_SIDECAR_SUFFIXES):
            continue
        if target_os not in name:
            continue
        if any(a in name for a in other_arch):
            continue
        if not any(a in name for a in arch_ok):
            continue
        cands.append(url)
    if not cands:
        return None
    cands.sort(key=lambda u: (0 if u.lower().endswith(_ARCHIVE_SUFFIXES) else 1, len(u)))
    return cands[0]


def resolve_linux_asset(
    binary_url: str, target_os: str = "linux", target_arch: str = "amd64", timeout: int = 12,
) -> dict[str, Any]:
    """Given the GitHub release-asset URL of an installed binary (typically a
    host build like *_darwin_arm64), find the SAME release's asset for the ship
    platform. Queries the release BY TAG (not 'latest') so the version matches
    what was installed. Returns {found, url, tag, repo, asset_name} or
    {found: False, reason, available?}."""
    m = _GH_RELEASE_RE.match(binary_url or "")
    if not m:
        return {"found": False,
                "reason": "not a github release-download URL — pass a linux asset URL explicitly"}
    owner, repo, tag = m.group(1), m.group(2), m.group(3)
    rel = _get_json(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", timeout)
    if not isinstance(rel, dict):
        return {"found": False, "reason": f"could not fetch release {owner}/{repo}@{tag}"}
    assets = [a.get("browser_download_url") for a in (rel.get("assets") or [])
              if a.get("browser_download_url")]
    pick = _pick_platform_asset(assets, target_os, target_arch)
    if not pick:
        return {"found": False,
                "reason": f"no {target_os}/{target_arch} asset in {owner}/{repo}@{tag}",
                "available": assets[:20]}
    return {"found": True, "url": pick, "tag": tag, "repo": f"{owner}/{repo}",
            "asset_name": pick.rsplit("/", 1)[-1]}


def sha256_of_url(url: str, timeout: int = 600) -> dict[str, Any]:
    """Stream-download an asset and return {ok, sha256, size} without keeping it
    — used to anchor a ship-platform binary at freeze time. Network failure →
    {ok: False, reason}."""
    h = hashlib.sha256()
    n = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bioinf-agent-resolver"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                n += len(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "sha256": h.hexdigest(), "size": n}


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
                "rationale": "no registry/repo tier found — pass a github_repo (or repo/archive URL) "
                             "to unlock synthesis (the universal agent-read path), else manual"}

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
    if tier == "spack":
        return f'install_spack_package(env, "{tool}")  # Spack curated recipe; best on a native amd64 host'
    if tier == "synthesis":
        url = f"https://github.com/{github_repo}" if github_repo else "<repo-or-archive-url>"
        return (f'synth_fetch("{url}")  # read its build files, then '
                f'synth_build(env, "{url}", "{tool}", commit, commands=[...], evidence="...")')
    if tier == "source":
        return f'install_git_repo(env, "https://github.com/{github_repo}", "{tool}", ref="<tag/commit>")'
    return "# no automatable tier — author a manual path (and stage_authored_artifact for any scripts)"


def route(decision: dict, platform: str = "linux/amd64") -> dict[str, Any]:
    """Container-native sibling of `_install_call`: map a resolve() decision to an
    EnvBuild action instead of a host-primitive call string. Pure (no network).

      conda        → {"kind":"conda", "spec","channel"}        — fed to EnvBuild.add_conda
      pip          → {"kind":"pip",   "spec"}                  — fed to EnvBuild.add_pip
      cran / bioc  → {"kind":"tool",  "spec": <r_package gen>} — fed to EnvBuild.add_tool
      binary       → {"kind":"tool",  "spec": <release_binary gen>}
      source       → {"kind":"tool",  "spec": <source gen>}

    pip uses the engine (pixi --pypi → into the lock); cran/bioconductor use the R
    install generator (engine-coupled Rscript) — both engine-native, so a `chosen` of
    cran/bioc is installed by Rscript, NOT a (would-fail) r-{tool} conda mapping."""
    from agent.skills import install_commands as ic
    tier = decision.get("chosen")
    tool = decision.get("tool") or ""
    version = decision.get("version") or ""
    detail = (decision.get("probed") or {}).get(tier, {}) if tier else {}
    repo = decision.get("github_repo") or ""

    if tier == "conda":
        base = detail.get("r_spec") or tool
        v = version or detail.get("latest") or ""
        spec = f"{base}={v}" if v else base
        return {"kind": "conda", "tier": tier, "spec": spec,
                "channel": detail.get("channel", "bioconda")}

    if tier == "pip":
        v = version or detail.get("latest") or ""
        return {"kind": "pip", "tier": tier, "spec": f"{tool}=={v}" if v else tool}

    if tier in ("cran", "bioconductor"):
        return {"kind": "tool", "tier": tier,
                "spec": ic.r_package(tool, source="bioconductor" if tier == "bioconductor" else "cran")}

    if tier == "binary":
        os_tok, _, arch_tok = platform.partition("/")
        url = _pick_platform_asset(detail.get("assets") or [], os_tok or "linux",
                                   arch_tok or "amd64")
        if not url:
            return {"kind": "defer", "tier": tier,
                    "reason": f"no {platform} asset among the release assets — pass an explicit "
                              f"linux URL or pick a different tier"}
        # sha256 anchoring is a network step (resolver.sha256_of_url) the caller adds
        # before build; absent, the in-image smoke evidence still catches a wrong arch.
        return {"kind": "tool", "tier": tier,
                "spec": ic.release_binary(tool, url, binary_in_archive=tool),
                "needs_sha256": True}

    if tier == "spack":
        return {"kind": "tool", "tier": tier,
                "spec": ic.spack(tool, package=detail.get("package") or tool)}

    if tier == "synthesis":
        # The universal repo tail is AGENT-DRIVEN (two-call: synth_fetch → read the
        # repo's build files → synth_build), so a declarative route can't auto-build
        # it — it hands off to the agent. The build itself still flows through the ONE
        # `synthesized` generator + the honesty contract (validated == shipped).
        url = f"https://github.com/{repo}" if repo else ""
        return {"kind": "synthesize", "tier": tier, "repo": url,
                "instruction": "agent: call synth_fetch(repo_url) to read the tool's own build "
                               "files, then synth_build(env, repo_url, tool, commit, commands, "
                               "evidence) — commands tagged extracted/agent_authored (grounded)."}

    if tier == "source":
        if not repo:
            return {"kind": "defer", "tier": tier,
                    "reason": "source tier needs github_repo to clone"}
        return {"kind": "tool", "tier": tier,
                "spec": ic.source(tool, f"https://github.com/{repo}",
                                  ref=detail.get("tag") or "")}

    return {"kind": "defer", "tier": tier,
            "reason": (f"tier {tier!r} has no container-native generator yet — pip needs engine "
                       f"pypi support; cran/bioconductor need an R install generator (engine-coupled). "
                       f"Next slice.") if tier else "no automatable tier found"}


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
        availability["binary"]    = {"available": gh["has_release_assets"], **gh}
        # synthesis ranks above source (both need only repo_exists): the agent reads
        # the repo's real build, so it's the robust default; source is the fast-path.
        availability["synthesis"] = {"available": gh["repo_exists"], **gh}
        availability["source"]    = {"available": gh["repo_exists"], **gh}

    # Spack is a real (curated, from-source) tier — ranks below precompiled binary
    # but above the agent-read synthesis fallback (a community recipe beats
    # improvisation). Needs only a name (registry probe), no github_repo.
    availability["spack"] = probe_spack(tool, timeout)

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
