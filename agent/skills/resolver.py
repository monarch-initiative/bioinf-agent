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
    """PyPI metadata. Captures homepage + project_urls so a github_repo-supplied
    resolve() can confirm a same-name PyPI hit actually references the same
    project (and isn't a cross-namespace collision — PyPI's `gab` ≠ baumannlab's
    Genome_Assembly_Booster despite the name match)."""
    data = _get_json(f"https://pypi.org/pypi/{name}/json", timeout)
    if isinstance(data, dict) and data.get("info"):
        info = data["info"]
        return {
            "available": True,
            "latest": info.get("version"),
            "home_page": info.get("home_page") or "",
            "project_urls": info.get("project_urls") or {},
            "package_url": info.get("package_url") or "",
        }
    return {"available": False}


def probe_cran(name: str, timeout: int = 12) -> dict[str, Any]:
    """CRAN metadata. Captures URL + BugReports so a github_repo-supplied
    resolve() can confirm a same-name CRAN hit references the same project."""
    data = _get_json(f"https://crandb.r-pkg.org/{name}", timeout)
    if isinstance(data, dict) and data.get("Package"):
        return {
            "available": True,
            "latest": data.get("Version"),
            "url": data.get("URL") or "",
            "bug_reports": data.get("BugReports") or "",
        }
    return {"available": False}


def _anchored_to_github_repo(metadata_urls: list[str], github_repo: str) -> bool:
    """True if any URL in `metadata_urls` references `github_repo` (owner/repo).
    The substring check is case-insensitive on the repo path because GitHub repo
    URLs are case-preserving but case-insensitive: github.com/Brentp/Mosdepth ==
    github.com/brentp/mosdepth. Matches both github.com/owner/repo and
    github.io subpaths so a project's docs site at <owner>.github.io/<repo>
    still counts as anchored.

    This is the load-bearing check for the cross-namespace-collision guard:
    when `github_repo` is provided to resolve() AND a same-name pip/cran hit
    exists, the resolver MUST verify the registry's metadata actually
    references that repo before trusting the name match. Without verification,
    a same-name unrelated package gets confidently picked over the user's
    explicit repo (the 'GAB' resolver bug: PyPI's `gab` chat-bot library was
    picked over baumannlab's Genome_Assembly_Booster)."""
    if not github_repo or "/" not in github_repo:
        return False
    needle = github_repo.lower().strip()
    # also accept the github.io homepage form: <owner>.github.io/<repo>
    owner, repo = needle.split("/", 1)
    needle_io = f"{owner}.github.io/{repo}"
    for url in metadata_urls:
        if not isinstance(url, str):
            continue
        u = url.lower()
        if needle in u or needle_io in u:
            return True
    return False


def _pip_anchored_to_repo(probe: dict, github_repo: str) -> bool:
    """Does PyPI's metadata for this package actually reference the user-
    supplied github_repo? Walks home_page + project_urls.values() + package_url."""
    if not probe.get("available"):
        return False
    urls = [probe.get("home_page", ""), probe.get("package_url", "")]
    urls.extend((probe.get("project_urls") or {}).values())
    return _anchored_to_github_repo(urls, github_repo)


def _cran_anchored_to_repo(probe: dict, github_repo: str) -> bool:
    """Does CRAN's metadata for this package reference the user-supplied
    github_repo? CRAN's URL field is comma-separated; BugReports is a single URL."""
    if not probe.get("available"):
        return False
    urls = [probe.get("bug_reports", "")]
    # CRAN URL field is "https://a.example, https://b.example" — split on comma.
    for u in (probe.get("url", "") or "").split(","):
        urls.append(u.strip())
    return _anchored_to_github_repo(urls, github_repo)


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


def probe_github_search(name: str, timeout: int = 12, limit: int = 5) -> dict[str, Any]:
    """DISCOVERY (the router's reach beyond package registries). Search github for
    repos matching a bare tool NAME — so a tool that lives only as a repo (no conda/
    pip/cran/spack entry) can still reach the synthesis/source floor. This is the
    missing link between 'has a synthesis engine' and 'installs arbitrary tools':
    without it, an unregistered tool DEAD-ENDS until a human hand-supplies the repo
    (verified: GAPIT3 lives at jiabowang/GAPIT3, not on any registry).

    Rate-limited (unauthenticated github search = 10 req/min), so this is called
    ONLY when the registries dead-end — never on every resolve. Ranks by stars and
    flags exact name matches so the caller (or a human orchestrator) can confirm the
    right repo rather than guess a URL. Candidate order: exact-name first, then stars."""
    from urllib.parse import quote
    q = quote(f"{name} in:name,description")
    data = _get_json(
        f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc"
        f"&per_page={max(limit, 5)}", timeout)
    if not isinstance(data, dict) or not data.get("items"):
        return {"found": False, "candidates": []}
    cands = []
    for it in data["items"]:
        full = it.get("full_name") or ""
        if not full:
            continue
        cands.append({
            "repo": full,
            "stars": int(it.get("stargazers_count") or 0),
            "description": (it.get("description") or "")[:140],
            "language": it.get("language"),
            "exact_name_match": (it.get("name") or "").lower() == name.lower(),
        })
    # exact name matches first, then by stars — the honest "most likely THE tool" order.
    cands.sort(key=lambda c: (not c["exact_name_match"], -c["stars"]))
    return {"found": bool(cands), "candidates": cands[:limit]}


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
# OS/format tokens that mark an asset as a DIFFERENT platform than ours, so it can
# be excluded even from the permissive fallback (a darwin/windows asset is never a
# linux one). Bare 'mac' is omitted (substring-collision risk in real names).
_FOREIGN_OS_TOKENS = ("darwin", "macos", "osx", "windows", "win64", "win32",
                      ".exe", ".dmg", ".msi", "freebsd")


def _pick_platform_asset(
    assets: list[str], target_os: str = "linux", target_arch: str = "amd64"
) -> Optional[str]:
    """Pure selection (no network): from a list of asset URLs/names, choose the one
    matching target_os + target_arch. Returns the URL or None.

    Two passes. STRICT (high confidence): the OS token AND an arch alias are both
    present. If nothing matches strictly, a permissive FALLBACK accepts any asset
    that does NOT name a competing arch or a foreign OS — this is the bare-binary
    case (mosdepth ships `mosdepth`, somalier ships `somalier`: a single Linux
    static binary with NO platform tokens at all). The fallback is safe because the
    binary tier re-validates IN the linux ship image (the smoke verify rejects a
    wrong-arch/OS pick), so selection can be generous and let the contract be the
    net rather than refusing a whole class of popular tools. An asset naming the
    WRONG arch or a foreign OS is rejected in BOTH passes. Sidecars excluded. Ties
    break toward an archive, then the least-adorned (shortest) name."""
    arch_ok = _ARCH_ALIASES.get(target_arch, [target_arch])
    other_arch = [a for k, v in _ARCH_ALIASES.items() if k != target_arch for a in v]
    foreign_os = tuple(t for t in _FOREIGN_OS_TOKENS if t != target_os)
    strict: list[str] = []
    loose: list[str] = []
    for url in assets:
        if not url:
            continue
        name = url.rsplit("/", 1)[-1].lower()
        if name.endswith(_SIDECAR_SUFFIXES):
            continue
        if any(a in name for a in other_arch):
            continue                          # names a competing arch → never ours
        if any(t in name for t in foreign_os):
            continue                          # names a foreign OS → never ours
        if target_os in name and any(a in name for a in arch_ok):
            strict.append(url)                # explicit os+arch (highest confidence)
        else:
            loose.append(url)                 # untagged/partial — smoke verify is the net
    pool = strict or loose
    if not pool:
        return None
    pool.sort(key=lambda u: (0 if u.lower().endswith(_ARCHIVE_SUFFIXES) else 1, len(u)))
    return pool[0]


def resolve_linux_asset(
    binary_url: str, target_os: str = "linux", target_arch: str = "amd64", timeout: int = 12,
) -> dict[str, Any]:
    """Given the URL of an installed binary, return the SHIP-platform asset URL.

    Two routes (in order):

    A) GitHub release-download URL (the host-binary cross-arch bridge): a host
       build like *_darwin_arm64 maps to the same release's *_linux_amd64
       asset. Queries the release BY TAG (not 'latest') so the version matches
       what was installed. Returns {found, url, tag, repo, asset_name}.

    B) DIRECT vendor / CDN URL whose filename already identifies the ship
       platform (e.g. dorado's `https://cdn.oxfordnanoportal.com/.../
       dorado-2.0.0-linux-x64.tar.gz` — the URL IS the ship-platform asset).
       Many vendors (Oxford Nanopore, 10x Genomics, certain Illumina tools)
       publish only on a CDN, not as GitHub release assets, so the github-only
       gate (the dorado-stress D1 finding) blocked them with the diagnostic
       'not a github release-download URL — pass a linux asset URL
       explicitly'. We now accept the direct URL when its filename passes the
       SAME platform-selection rules used for github assets (_pick_platform_
       asset): names a foreign os/arch → rejected; names target os+arch →
       accepted (strict); platform-untagged → accepted (loose, the smoke
       verify is the net). Returns {found, url, tag, repo, asset_name} with
       tag/repo empty (the CDN URL doesn't expose them).

    Returns {found: False, reason, available?} when neither route resolves."""
    m = _GH_RELEASE_RE.match(binary_url or "")
    if m:
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
    # Direct vendor / CDN URL — accept when the URL itself passes platform
    # selection. Same rules as the github asset list, applied to the single URL.
    url = (binary_url or "").strip()
    if not url:
        return {"found": False,
                "reason": "no binary_url to resolve — pass an explicit asset URL"}
    pick = _pick_platform_asset([url], target_os, target_arch)
    if not pick:
        return {"found": False,
                "reason": (f"URL is not a github release-download AND its filename does not "
                           f"identify a {target_os}/{target_arch} asset — pass an asset URL "
                           f"whose name includes the OS/arch tokens"),
                "available": [url]}
    return {"found": True, "url": pick, "tag": "", "repo": "",
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

    # PROTECTIVE: cross-namespace name-collision guard. When `github_repo` is
    # provided, the user is signaling authoritative intent ("THIS repo is what I
    # want"). If a same-name hit on PyPI/CRAN exists but its metadata doesn't
    # reference github_repo, it's almost certainly a DIFFERENT project that
    # happens to share the name — picking it would silently install the wrong
    # tool (the 'GAB' bug: PyPI's chat-bot library `gab` confidently picked over
    # baumannlab/Genome_Assembly_Booster). We disqualify those tiers from
    # ranking AND record the collision so the caller can see what was rejected
    # and why. Without this guard, `chosen` could be a same-name unrelated
    # package; with it, the user-supplied repo wins by construction.
    cross_namespace_collisions: list[dict] = []
    if github_repo:
        if availability.get("pip", {}).get("available"):
            if not _pip_anchored_to_repo(availability["pip"], github_repo):
                cross_namespace_collisions.append({
                    "tier": "pip",
                    "name": tool,
                    "latest": availability["pip"].get("latest"),
                    "reason": (f"PyPI '{tool}' exists but its metadata does not "
                               f"reference github_repo '{github_repo}'; "
                               f"likely a same-name unrelated package."),
                })
                availability["pip"] = {**availability["pip"],
                                       "available": False,
                                       "cross_namespace_collision": True}
        if availability.get("cran", {}).get("available"):
            if not _cran_anchored_to_repo(availability["cran"], github_repo):
                cross_namespace_collisions.append({
                    "tier": "cran",
                    "name": tool,
                    "latest": availability["cran"].get("latest"),
                    "reason": (f"CRAN '{tool}' exists but its metadata does not "
                               f"reference github_repo '{github_repo}'; "
                               f"likely a same-name unrelated package."),
                })
                availability["cran"] = {**availability["cran"],
                                        "available": False,
                                        "cross_namespace_collision": True}

    decision = rank_decision(availability, prefer=prefer)

    # DISCOVERY: the registries dead-ended and no repo was supplied. Instead of
    # stopping at "pass a github_repo", SEARCH github for the tool by name so an
    # unregistered tool still reaches the synthesis floor. Turns the human's job
    # from "go find the repo + figure out its build" into "confirm this repo" —
    # the orchestrator-in-the-loop model. Only here (dead-end) so github search
    # rate limits never bite the common registry path.
    if decision["chosen"] is None and not github_repo:
        disc = probe_github_search(tool, timeout)
        if disc["found"]:
            cands = disc["candidates"]
            rec = cands[0]   # exact-name-then-stars sorted; the most-likely THE tool
            # A confident auto-adopt candidate: an EXACT name match that clearly
            # dominates. Else present candidates for a human/agent to confirm (the
            # GAB-collision guard: a same-name repo can still be the wrong project).
            auto_adoptable = bool(
                rec["exact_name_match"] and rec["stars"] >= 10 and (
                    len(cands) == 1 or not cands[1]["exact_name_match"]
                    or rec["stars"] >= 5 * max(cands[1]["stars"], 1)))
            if auto_adoptable:
                # AUTONOMY: it's confidently the tool → don't make a human re-run.
                # Re-resolve WITH the discovered repo so the caller gets a COMPLETE,
                # executable plan (synthesis/source/binary + install_call), not a
                # "re-run please" stub. The honesty contract validates the actual
                # build, so a rare wrong pick fails SAFE rather than shipping silently.
                auto = resolve(tool, version=version, prefer=prefer, language=language,
                               github_repo=rec["repo"], timeout=timeout)
                auto["discovered_repos"]    = cands
                auto["recommended_repo"]    = rec["repo"]
                auto["repo_auto_adoptable"] = True
                auto["auto_discovered"]     = True   # provenance: FOUND, not user-supplied
                auto["rationale"] = (
                    f"AUTO-DISCOVERED + adopted {rec['repo']} ({rec['stars']}★, exact-name) "
                    f"— no registry hit, but a dominant exact-name repo; proceeding without "
                    f"a human. " + auto.get("rationale", ""))
                return auto
            # Not confident → surface candidates for the orchestrator to confirm.
            decision["discovered_repos"] = cands
            decision["recommended_repo"] = rec["repo"]
            decision["repo_auto_adoptable"] = False
            decision["rationale"] = (
                f"no registry hit — DISCOVERED {len(cands)} candidate repo(s) via github "
                f"search, none dominant enough to auto-adopt. Recommended: {rec['repo']} "
                f"({rec['stars']}★{', exact-name' if rec['exact_name_match'] else ''}). "
                f"Confirm with github_repo='{rec['repo']}' (or pick another from "
                f"discovered_repos) to install via synthesis.")

    ambiguous = _is_ambiguous(availability, language)
    chosen = decision["chosen"]
    decision.update({
        "tool": tool,
        "version": version or None,
        "language": language or None,
        "github_repo": github_repo or None,
        "ambiguous": ambiguous,
        "probed": availability,
        "cross_namespace_collisions": cross_namespace_collisions,
    })
    if cross_namespace_collisions:
        # Surface the rejection prominently so an agent reading the rationale
        # sees WHY pip/cran was disqualified — silence here would mask the very
        # bug this guard exists to prevent (a confidently-wrong same-name pick).
        rejected = ", ".join(
            f"{c['tier']}/{c['name']}@{c['latest']}" for c in cross_namespace_collisions)
        decision["rationale"] = (
            f"REJECTED (cross-namespace collision): {rejected} — same-name hit(s) "
            f"whose registry metadata does NOT reference github_repo '{github_repo}'. "
            f"Trust the user-supplied repo: " + decision["rationale"]
        )
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
