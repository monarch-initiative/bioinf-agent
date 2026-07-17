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
import os
import re
import urllib.error
import urllib.request
from typing import Any, Optional

try:
    # the authors'-own-resources reliability gate (image / recipe completeness). Imported
    # softly so a resolver import never hard-depends on it; None disables the gate.
    from agent.skills.authors_sources import assess_tool_sources as probe_authors_sources
except Exception:  # pragma: no cover
    probe_authors_sources = None

# Preference order — lower index wins. Cross-cutting concerns (gpu/service/
# license/db) are flags layered on top, not tiers.
#
# `synthesis` is the UNIVERSAL repo tail (the "reduce infinity to 1" floor): for any
# fetchable repo it ranks ABOVE the conventional `source` generator, because the
# agent reads the tool's OWN build files (gated by provenance + grounding + the
# contract) and so handles EVERY repo — half-baked, run-by-path, custom build — not
# just the conventional make+binary case the `source` generator assumes. `source`
# (and binary) survive as opt-in FAST-PATHS, not the boundary of installable.
# `author_image` / `authors_recipe` sit ABOVE conda, but they are RELIABILITY-GATED,
# not unconditional: their `available` flag is only set when the tool actually ships a
# usable image, or when the authors' recipe installs pieces a conda/pip reconstruction
# would DROP (system/compiled/vendored/binary deps — the completeness gate in
# authors_sources). For a cleanly, completely bioconda-packaged tool the gate stays shut
# and conda wins as before. This encodes "use the authors' own machinery when
# reconstruction would be incomplete" (see [[feedback-prioritize-authors-own-env-recipe]])
# as ranking, not just prose — WITHOUT over-preferring a heavy author image for tools
# conda handles perfectly.
TIER_ORDER = ["author_image", "authors_recipe",
              "conda", "pip", "cran", "bioconductor", "r_github", "binary",
              "synthesis", "source", "manual"]

_TIER_RATIONALE = {
    "author_image": "the authors publish a container image — adopt it by digest (highest "
                    "fidelity: they built + tested the whole env; lowest cost: a pull)",
    "authors_recipe": "the authors' Dockerfile/recipe installs deps a registry reconstruction "
                    "would silently DROP (compiled/vendored/system/binary) — build THEIR recipe, "
                    "the reliable path a human would follow rather than reconstruct from scratch",
    "conda":        "on bioconda/conda-forge — solver-managed, pinned, containerizes cleanly (preferred)",
    "pip":          "on PyPI — language registry; chosen when not on conda",
    "cran":         "on CRAN — R language registry via install_r_package(source=cran)",
    "bioconductor": "on Bioconductor — R via install_r_package(source=bioconductor)",
    "r_github":     "an R package on github — install_r_package(source=github:owner/repo), the "
                    "purpose-built R path (remotes::install_github + load-or-die); beats generic "
                    "synthesis for a known R package",
    "binary":       "precompiled release binary — exact bytes (sha256), but platform-specific",
    "synthesis":    "agent reads the repo's OWN build files and synthesizes a grounded, contract-"
                    "gated install — the universal path for any source/bespoke tool (no per-tool recipe)",
    "source":       "conventional `make`+binary fast-path (install_git_repo) — faster than synthesis "
                    "when the repo builds conventionally; synthesis is the robust fallback otherwise",
    "manual":       "no fetchable source at all — needs a hand-authored path",
}


def _headers(url: str) -> dict[str, str]:
    """Request headers, with a GitHub token when the environment offers one.

    Unauthenticated GitHub allows 60 core req/hr and 10 search req/min — low enough
    that a normal session exhausts it, after which every repo probe 403s. A token
    raises that to 5000/hr. This is a MITIGATION, not the fix: the fix is that an
    exhausted quota reports UNCHECKED instead of masquerading as a finding (see
    `_fetch_json`). Raising a ceiling only makes the cliff rarer; it never makes
    falling off it honest."""
    h = {"Accept": "application/json", "User-Agent": "bioinf-agent-resolver"}
    if "github.com" in url:
        tok = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    return h


def _http_error_kind(e: urllib.error.HTTPError) -> str:
    """Name WHY a request failed, distinguishing a rate limit from a real denial.

    GitHub signals an exhausted quota as 403 (or 429) with X-RateLimit-Remaining: 0 —
    the same status code it uses for a genuine authorization denial. Telling them apart
    matters because they mean opposite things about the tool: 'come back later' vs
    'this is private'."""
    remaining = (e.headers or {}).get("X-RateLimit-Remaining")
    if e.code in (403, 429) and str(remaining) == "0":
        return "rate_limited"
    if e.code in (403, 429):
        return f"HTTP {e.code} (forbidden)"
    return f"HTTP {e.code}"


def _fetch_json(url: str, timeout: int = 12) -> tuple[Optional[Any], str]:
    """Fetch JSON and report WHETHER THE PROBE RAN — the (payload, error) seam.

    `error == ""` means the probe reached a conclusion, and the payload is that
    conclusion: the data, or None for a CHECKED, GENUINE absence (HTTP 404 — the
    registry answered, and its answer was "no such package").

    A NON-EMPTY error means the probe never concluded. The tool's availability is
    then UNKNOWN, not False.

    This distinction is the whole point of the function, and it replaces a
    `except (...): return None` that collapsed both into one value. That collapse was
    not cosmetic — it silently rewrote the routing decision. Measured: with a single
    transient 403 on api.anaconda.org and NOTHING about the tool changed, resolve
    ('samtools') stopped returning `conda` + a pinned `install_conda_packages(...)`
    and instead returned `binary`, recording `conda: {available: False}` on disk and
    announcing "AUTO-DISCOVERED + adopted samtools/samtools (1928*) — no registry hit
    ... proceeding without a human". There WAS a registry hit; we simply could not
    reach it. Every clause of that sentence was false, stated with full confidence, and
    the whole tier ladder slid a rung on a network blip.

    So: absence of data must never render as data (Rule 2), enforced at the bottom of
    the stack, because every tier's `available: False` is built on this return value."""
    try:
        req = urllib.request.Request(url, headers=_headers(url))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, ""                      # the registry ANSWERED: no such package
        return None, _http_error_kind(e)
    except urllib.error.URLError as e:
        return None, f"unreachable: {e.reason}"
    except ValueError as e:                      # malformed JSON — the host answered garbage
        return None, f"malformed response: {e}"
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"


def _fetch_ok(url: str, timeout: int = 12) -> tuple[bool, str]:
    """HEAD probe as (present, error) — same contract as `_fetch_json`: an error means
    UNCHECKED, and `present=False` is only meaningful when the error is empty."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers=_headers(url))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300, ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, ""                     # the host ANSWERED: not there
        return False, _http_error_kind(e)
    except urllib.error.URLError as e:
        return False, f"unreachable: {e.reason}"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


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
    best = None  # (version_key, channel, version, summary, repo, repo_field)
    errors: list[str] = []
    for channel in ("bioconda", "conda-forge"):
        data, err = _fetch_json(
            f"https://api.anaconda.org/package/{channel}/{name.lower()}", timeout)
        if err:
            errors.append(f"{channel}: {err}")
            continue
        if isinstance(data, dict) and data.get("versions"):
            ver = _pick_latest(data["versions"], data.get("latest_version") or "")
            key = _version_key(ver)
            if best is None or (key is not None and (best[0] is None or key > best[0])):
                # THE RECIPE'S OWN LINK TO THE PROJECT — free, in a response we already
                # fetch, and the strongest identity anchor available (a curated recipe
                # maintainer wrote it down). Prefer `dev_url` over `home`: multiqc's home is
                # seqera.io, its dev_url is MultiQC/MultiQC. Not every recipe carries one
                # (bioconda's `trinity` carries neither) — absent is a fact, and a repo we
                # do not have must never be substituted from somewhere else.
                repo, field = "", ""
                for f in ("dev_url", "home", "source_git_url"):
                    m = _GH_REPO_RE.search(str(data.get(f) or ""))
                    if m:
                        repo = f"{m.group(1)}/{re.sub(r'[.]git$', '', m.group(2))}"
                        field = f
                        break
                best = (key, channel, ver, data.get("summary") or "", repo, field)
    if best:
        out = {"available": True, "channel": best[1], "latest": best[2], "summary": best[3]}
        if best[4]:
            out["repo"] = best[4]
            out["repo_field"] = best[5]     # provenance: WHICH field vouched for it
        return out
    # A hit on either channel is a fact regardless of the other's health, so errors only
    # matter when NOTHING was found: then "not on conda" rests on a probe that never ran.
    # Reported, never inferred — a silent conda miss demotes the tool to the pip/cran
    # fall-through, which is the measured 9-in-10-wrong path.
    if errors:
        return {"available": False, "probe_error": "; ".join(errors)}
    return {"available": False}


def probe_pypi(name: str, timeout: int = 12) -> dict[str, Any]:
    """PyPI metadata. Captures homepage + project_urls so a github_repo-supplied
    resolve() can confirm a same-name PyPI hit actually references the same
    project (and isn't a cross-namespace collision — PyPI's `gab` ≠ baumannlab's
    Genome_Assembly_Booster despite the name match).

    Also captures `summary`, the package's OWN one-line description. That is the
    identity evidence: PyPI's `talos` says "Talos Hyperparameter Tuning for Keras",
    which is not the rare-disease pipeline anyone asking this agent for `talos`
    means. Without it the decision has no basis on which anything — the agent, the
    contract, or a human — could notice the wrong tool."""
    data, err = _fetch_json(f"https://pypi.org/pypi/{name}/json", timeout)
    if isinstance(data, dict) and data.get("info"):
        info = data["info"]
        return {
            "available": True,
            "latest": info.get("version"),
            "summary": (info.get("summary") or "").strip(),
            "home_page": info.get("home_page") or "",
            "project_urls": info.get("project_urls") or {},
            "package_url": info.get("package_url") or "",
        }
    return {"available": False, **({"probe_error": err} if err else {})}


def probe_cran(name: str, timeout: int = 12) -> dict[str, Any]:
    """CRAN metadata. Captures URL + BugReports so a github_repo-supplied
    resolve() can confirm a same-name CRAN hit references the same project.

    `summary` is CRAN's Title + Description — the identity evidence. CRAN's
    `cellranger` is "Translate Spreadsheet Cell Ranges to Rows and Columns", not
    10x Genomics' Cell Ranger; the name is all they share."""
    data, err = _fetch_json(f"https://crandb.r-pkg.org/{name}", timeout)
    if isinstance(data, dict) and data.get("Package"):
        summary = " — ".join(x for x in ((data.get("Title") or "").strip(),
                                         (data.get("Description") or "").strip()) if x)
        return {
            "available": True,
            "latest": data.get("Version"),
            "summary": re.sub(r"\s+", " ", summary)[:400],
            "url": data.get("URL") or "",
            "bug_reports": data.get("BugReports") or "",
        }
    return {"available": False, **({"probe_error": err} if err else {})}


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


def probe_github(repo: str, timeout: int = 12) -> dict[str, Any]:
    """For a github 'owner/repo': does it exist (→ source tier) and does its
    latest release carry downloadable assets (→ binary tier)?"""
    out = {"repo_exists": False, "has_release_assets": False, "assets": []}
    if not repo or "/" not in repo:
        return out
    data, err = _fetch_json(f"https://api.github.com/repos/{repo}", timeout)
    if err:
        # UNCHECKED, not absent. Left as repo_exists=False so the tiers built on it stay
        # unavailable (fail closed), but the reason is recorded so the decision can say
        # "we could not look" rather than "there is no repo" — the binary/source/synthesis
        # tiers ALL rest on this one call, and the unauthenticated quota is 60/hr.
        out["probe_error"] = err
        return out
    if data is None:
        return out                               # GitHub answered: no such repo
    out["repo_exists"] = True
    rel, rel_err = _fetch_json(
        f"https://api.github.com/repos/{repo}/releases/latest", timeout)
    if rel_err:
        out["releases_probe_error"] = rel_err     # "no release assets" would be a guess
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
    pip/cran entry) can still reach the synthesis/source floor. This is the
    missing link between 'has a synthesis engine' and 'installs arbitrary tools':
    without it, an unregistered tool DEAD-ENDS until a human hand-supplies the repo
    (verified: GAPIT3 lives at jiabowang/GAPIT3, not on any registry).

    Rate-limited (unauthenticated github search = 10 req/min), so this is called
    ONLY when the registries dead-end — never on every resolve. Ranks by stars and
    flags exact name matches so the caller (or a human orchestrator) can confirm the
    right repo rather than guess a URL. Candidate order: exact-name first, then stars."""
    from urllib.parse import quote
    q = quote(f"{name} in:name,description")
    data, err = _fetch_json(
        f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc"
        f"&per_page={max(limit, 5)}", timeout)
    if err:
        # "I searched and found nothing" and "I could not search" are opposite facts about
        # the tool, and this function used to return the same value for both. Search is the
        # tightest quota on the API (10 req/min unauthenticated), so the failure is common —
        # and it is the INVESTIGATION's own probe: a caller that refuses on an empty result
        # would be reporting a rate limit as a finding about the world.
        return {"found": False, "candidates": [], "probe_error": err}
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
        rel, err = _fetch_json(
            f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", timeout)
        if err:
            return {"found": False, "probe_error": err,
                    "reason": f"could not fetch release {owner}/{repo}@{tag}: {err} "
                              f"— UNCHECKED, not 'no such release'"}
        if not isinstance(rel, dict):
            return {"found": False, "reason": f"no release {owner}/{repo}@{tag}"}
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
    ok, err = _fetch_ok(
        f"https://bioconductor.org/packages/release/bioc/html/{name}.html", timeout)
    return {"available": ok, **({"probe_error": err} if err else {})}


def _is_ambiguous(availability: dict[str, dict], language: str) -> bool:
    """A bare tool name is dangerously ambiguous when it resolves in BOTH a
    Python registry (PyPI) and an R registry (CRAN) with no language hint — they
    are almost certainly different packages (e.g. PyPI `ape` ≠ CRAN's R `ape`).
    A language hint removes the ambiguity by construction."""
    if language:
        return False
    return bool(availability.get("pip", {}).get("available")
                and availability.get("cran", {}).get("available"))


_GH_REPO_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


# ---------------------------------------------------------------------------
# IDENTITY — is the registry entry we picked the tool the caller MEANT?
#
# Every other check in this codebase verifies INTEGRITY: that what we installed
# builds, runs, and ships as validated. None of them ask whether it is the RIGHT
# THING. That gap is not theoretical — it is the system's worst failure mode,
# because a wrong-but-working tool passes every gate and ships green:
#
#   resolve_tool("cellranger") -> CRAN 'cellranger' = an R spreadsheet-range
#     parser. `library(cellranger)` loads fine, so it BUILDs, it is
#     VALIDATED_IN_IMAGE, it is POLICY_CLEAN. It earns a content digest, an SLSA
#     attestation, an ENV report reading "validated in shipped image", and a .sif
#     on the cluster. A fully green artifact containing the wrong software — the
#     exact outcome this project exists to prevent.
#
# Identity cannot be machine-VERIFIED: "did you mean this one?" is a question
# about intent, and no invariant can see intent. So this does not gate. It does
# the one thing that actually helps — puts the package's OWN self-description in
# front of whoever decides, and says plainly when nothing anchors it. The Tier-0
# lesson applies exactly: don't blind-tighten a checker that can't see the
# difference; surface the evidence to the layer that can.
# ---------------------------------------------------------------------------

#: Terms that mark a package as plausibly bioinformatics. Deliberately biased toward
#: precision over recall: a term here CONFIRMS identity and silences the warning, so a
#: loose term ('assembly' also means a .NET assembly; 'read'/'align'/'sequence' are
#: generic English) would quietly reinstate the very hole this closes. Missing a term
#: only costs a warning on a real tool; a false confirm costs a wrong artifact.
_DOMAIN_TERMS = (
    # file formats / concrete artifacts — the strongest signals
    "sam", "bam", "cram", "vcf", "bcf", "fastq", "fasta", "bed", "gff", "gtf",
    "bigwig", "bedgraph", "pod5", "fast5", "mzml", "phenopacket", "newick", "pdb",
    # domain nouns
    "genome", "genomic", "genomics", "sequencing", "variant", "allele", "chromosome",
    "transcript", "transcriptome", "transcriptomics", "proteome", "proteomics",
    "metagenome", "metagenomic", "microbiome", "epigenome", "epigenetic", "methylation",
    "phylogenetic", "phylogenetics", "phylogeny", "taxonomy", "taxonomic", "ontology",
    "rna-seq", "rnaseq", "scrna", "single-cell", "single cell", "crispr", "gwas",
    "pedigree", "haplotype", "genotype", "peptide", "nucleotide", "amino acid",
    "bioinformatic", "bioinformatics", "computational biology", "molecular biology",
    "biological sequence", "gene expression", "read alignment", "sequence alignment",
    "basecalling", "basecaller", "de novo assembly", "genome assembly", "aligner",
    # ecosystem / platform names that only occur in this domain
    "htslib", "samtools", "bcftools", "bioconductor", "biopython", "bioperl",
    "illumina", "nanopore", "pacbio", "oxford nanopore", "10x genomics", "ensembl",
    "ncbi", "uniprot", "gnomad", "clinvar", "galaxy project", "nf-core",
)


def domain_signal(text: str) -> list[str]:
    """Bioinformatics terms present in `text`, as whole words/phrases. Pure.

    Returns the matched terms (not a bare bool) so the decision can SHOW its reasoning —
    'matched: vcf, htslib' is checkable by a reader; 'plausible: true' is another
    assertion to take on faith, which is what this codebase refuses to do."""
    low = (text or "").lower()
    hits = []
    for t in _DOMAIN_TERMS:
        # word-boundary match so 'sam' doesn't fire inside 'same' and 'bed' not in 'bedroom'
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low):
            hits.append(t)
    return hits


def assess_identity(tool: str, chosen: str, availability: dict,
                    github_repo: str = "") -> dict:
    """Do we have any anchor that the chosen registry entry IS the tool asked for?

    Anchors, strongest first:
      github_repo   — the caller named the repo and the entry's metadata references it.
                      Authoritative: they told us which project they mean.
      bioconda      — the package is ON bioconda. Bioconda is a bioinformatics-only
                      channel, so membership IS domain identity, by construction. Free
                      and exact — no text heuristics needed for the commonest path.
      bioconductor  — same argument, for R.
      domain-terms  — the entry's own self-description reads like bioinformatics.
                      Weakest, and honestly labelled as such.
      (none)        — we picked a package with nothing tying it to this domain. Not an
                      error and not a refusal: could be a generic dependency (numpy) or
                      a terse description. But it must be SAID, next to the evidence.

    Returns {confirmed, anchor, evidence, self_description, note}."""
    detail = availability.get(chosen) or {}
    desc = (detail.get("summary") or "").strip()
    out = {"confirmed": True, "anchor": "", "evidence": [],
           "self_description": desc, "note": ""}

    if chosen in ("author_image", "authors_recipe", "binary", "source", "synthesis",
                  "r_github"):
        # These tiers ARE a repo — so identity is whatever vouches for THAT repo, and it is
        # not automatically the caller. This returned confirmed=True unconditionally, which
        # made the claim `assess_identity('dorado', 'author_image', ...)` -> {confirmed:
        # True, anchor: 'repo', evidence: ['Mucephie/DORADO']}: an astronomy repo presented
        # as a confirmed identity anchor at the tier that outranks conda.
        # Read the ONE anchoring decision `repo_evidence` already made — never re-decide it
        # here. This checked `source in ("conda", "user")`, a second copy of the rule that
        # promptly disagreed with the first: a repo anchored via an anchored pip entry was
        # good enough to RUN the gate and then reported unconfirmed by the tier the gate
        # produced. One truth, one definition, read at every use (Rule 4).
        anchored = detail.get("repo_anchored")
        if anchored is None:
            anchored = bool(github_repo)          # a caller-named repo, no gate involved
        out["anchor"] = "repo"
        out["evidence"] = [e for e in (detail.get("repo") or github_repo or "",
                                       detail.get("repo_detail") or "") if e]
        if anchored:
            return out
        # A repo scraped off an unanchored registry entry is the same unanchored guess
        # wearing a different hat.
        out["confirmed"] = False
        out["anchor"] = "none"
        out["reason"] = "repo_not_anchored"
        out["note"] = (
            f"IDENTITY UNCONFIRMED: the {chosen} tier here rests on "
            f"{detail.get('repo') or '<repo>'}, which was not independently anchored to "
            f"'{tool}' — a repo is not the tool's just because it shares its name. "
            f"Pass github_repo='owner/repo' to name the project you mean.")
        return out
    if github_repo and chosen in ("pip", "cran"):
        anchored = (_pip_anchored_to_repo(detail, github_repo) if chosen == "pip"
                    else _cran_anchored_to_repo(detail, github_repo))
        if anchored:
            out["anchor"] = "github_repo"
            out["evidence"] = [github_repo]
            return out
    if chosen == "conda" and (detail.get("channel") == "bioconda"):
        out["anchor"] = "bioconda-channel"
        out["evidence"] = ["bioconda is a bioinformatics-only channel"]
        return out
    if chosen == "bioconductor":
        out["anchor"] = "bioconductor"
        out["evidence"] = ["Bioconductor is a bioinformatics-only repository"]
        return out

    hits = domain_signal(desc)
    if hits:
        out["anchor"] = "domain-terms"
        out["evidence"] = hits
        return out

    # NO evidence and CONTRARY evidence are different situations and must not read the
    # same. "this package says it parses spreadsheets" is a reason to suspect the pick;
    # "this tier publishes no description" is a reason to suspect our own probe. Saying
    # "may not be the tool you mean" about a tool we simply couldn't check is the kind of
    # noise that trains a reader to skip the warning — and a warning that gets skipped is
    # worth nothing.
    out["confirmed"] = False
    out["anchor"] = "none"
    if not desc:
        out["reason"] = "no_description"
        out["note"] = (
            f"IDENTITY UNCHECKED: the {chosen} tier publishes no description for '{tool}', "
            f"so there is nothing to check the pick against — this is missing evidence, "
            f"NOT evidence of a wrong tool. Anchor it with github_repo='owner/repo' if it "
            f"matters. Nothing downstream re-checks identity: a wrong tool that installs "
            f"cleanly ships green.")
        return out
    out["reason"] = "no_domain_signal"
    out["note"] = (
        f"IDENTITY UNCONFIRMED: the {chosen} entry for '{tool}' describes itself as "
        f'"{desc}" — nothing in it ties it to bioinformatics, and no github_repo was '
        f"given to anchor it. Same-name packages across registries are common and this "
        f"may not be the '{tool}' you mean (CRAN's 'cellranger' parses spreadsheet cell "
        f"ranges; PyPI's 'talos' tunes Keras hyperparameters). Check the description "
        f"above, then either pass github_repo='owner/repo' to pin the real project, or "
        f"prefer=<tier> if this entry IS correct. Nothing downstream re-checks this: "
        f"a wrong tool that installs cleanly ships green.")
    return out


#: The registry tiers a repo can be scraped FROM, best-vouched first. conda leads because
#: its entry is a curated recipe whose maintainer wrote the project link down by hand.
_REPO_SOURCES = ("conda", "pip", "cran")


def repo_evidence(availability: dict, github_repo: str = "") -> dict[str, Any]:
    """WHICH repo is this tool's, and WHAT vouches for that claim? Pure.

    Returns `{repo, source, detail}` — or `{}` when nothing vouches for any repo, which is
    a real and common answer (bioconda's `trinity` recipe carries no dev_url at all).

    THE RULE: **a repo may only be taken from the registry candidate that WON.** A repo
    scraped from a losing tier describes the LOSING tier's package, not the one we picked.

    This replaces `_github_owner_repo`, which read pip/cran metadata unconditionally and
    handed the result to the author tiers — which outrank conda. Live, that meant the
    authors' tiers probed the SQUATTER's repo:

        resolve('dorado')  -> author_image.repo = 'Mucephie/DORADO'  (an astronomy package)
        resolve('talos')   -> author_image.repo = 'autonomio/talos'  (a Keras tuner)
        resolve('trinity') -> author_image.repo = 'ethereum/trinity' (conda WON; the repo
                                                  came from the losing pip candidate)

    and `assess_identity` then stamped `confirmed: True, anchor: 'repo'` on them. The only
    thing keeping the right answers standing was the accident that those squat repos ship
    no image — had `Mucephie/DORADO` published one, dorado would have adopted an astronomy
    image by digest, at the TOP tier, validated in-image and shipped green.

    The winner's own entry is the answer, and for conda it is FREE: api.anaconda.org
    already returns `dev_url`/`home` in the response `probe_conda` was already fetching.
    Measured — uv -> astral-sh/uv, multiqc -> MultiQC/MultiQC, bakta -> oschwengers/bakta
    (all correct, all from conda's own recipe), while trinity's recipe names none, so
    nothing is eligible and ethereum/trinity never gets probed.

    `anchored` says whether that vouching is strong enough to hand the repo to the AUTHOR
    tiers, which outrank conda. A repo can never be vouched for more strongly than the
    entry it was scraped from: pip's `dorado` describes an astronomy package, so the repo
    in its metadata is an astronomy repo, and no amount of name-matching upgrades it.
    """
    if github_repo and "/" in github_repo:
        # The caller named the project. That is authoritative for WHICH repo to look at —
        # though NOT, on its own, evidence that the repo IS the tool (a 200 from
        # github.com/torvalds/linux proves the repo exists, nothing more). See
        # `assess_identity`, which must still find a repo<->tool link.
        return {"repo": github_repo.strip().strip("/"), "source": "user", "anchored": True,
                "detail": "caller-supplied github_repo"}

    winner = next((t for t in _REPO_SOURCES if availability.get(t, {}).get("available")), "")
    if not winner:
        return {}
    detail = availability[winner]

    if winner == "conda" and detail.get("repo"):
        # A curated recipe maintainer wrote this link by hand. bioconda: 39 names / 0 wrong;
        # conda-forge 9/1. Note this anchors on PROVENANCE, not domain: we never ask whether
        # `uv` is bioinformatics (it isn't), only whether the channel that packaged it names
        # a repo. "Did the authors publish this?" and "is this a bio tool?" are different
        # questions, and the author tiers only ever asked the first.
        return {"repo": detail["repo"], "source": "conda", "anchored": True,
                "detail": f"the {detail.get('channel', 'conda')} recipe's "
                          f"`{detail.get('repo_field', 'dev_url')}`"}
    if winner in ("pip", "cran"):
        urls = ([detail.get("home_page", ""), detail.get("package_url", "")]
                + [str(v) for v in (detail.get("project_urls") or {}).values()]
                if winner == "pip" else
                [detail.get("bug_reports", "")]
                + [u.strip() for u in (detail.get("url", "") or "").split(",")])
        for u in urls:
            m = _GH_REPO_RE.search(u or "")
            if m:
                # The entry's OWN identity confidence, and the repo inherits exactly it.
                # Read through domain_signal rather than re-deciding here — one definition,
                # one reading (Rule 4). This is the surface domain_signal was measured on
                # (registry prose, <=400 chars), NOT github's 140-char blurbs, where it
                # cannot carry weight.
                hits = domain_signal(detail.get("summary") or "")
                return {"repo": f"{m.group(1)}/{re.sub(r'[.]git$', '', m.group(2))}",
                        "source": winner, "anchored": bool(hits),
                        "detail": (f"the {winner} entry's own metadata; that entry "
                                   + (f"describes itself with {', '.join(hits)}"
                                      if hits else
                                      "has nothing tying it to this domain, so its repo "
                                      "is no better anchored than the entry itself"))}
    return {}


#: Tiers whose availability is decided by a GitHub API call — the only ones for which a
#: GITHUB_TOKEN is the right advice when a probe reports `rate_limited`.
_GITHUB_BACKED_TIERS = ("author_image", "authors_recipe", "r_github", "binary",
                        "synthesis", "source")


def _disclose(decision: dict, note: str, caveat: str) -> None:
    """Attach a caveat to the decision, in EVERY field a reader might stop at.

    `rationale` always; `install_call` when there is one to poison. That asymmetry is the
    whole point: `install_call` is None on a refusal, and each of this function's callers
    used to be guarded by `if ... decision.get("install_call")` — so every disclosure
    silently vanished on the one outcome it was written for. A caller was told "no
    registry/repo tier found" while the authors-path gate had crashed with a TypeError, and
    the note explaining that never rendered.

    One function rather than four copies of the pattern, because this file has now forked a
    disclosure rule three times and each fork drifted (audit 2026-07-16, Rule 4)."""
    if not note:
        return
    decision["rationale"] = note + " || " + decision.get("rationale", "")
    if decision.get("install_call"):
        decision["install_call"] = (
            f"# {note}\n" + (f"# ---- {caveat}: ----\n" if caveat else "")
            + decision["install_call"])


def unchecked_tiers(availability: dict[str, dict]) -> dict[str, str]:
    """Tiers whose probe never reached a conclusion, as {tier: why}. Pure.

    `available: False` answers two different questions with one value: "the registry said
    no" and "we never got an answer". Only the first is a fact about the tool. This reads
    back the `probe_error` each probe records so a caller can tell them apart — because
    ranking treats both as unavailable (fail closed, which is right) and would otherwise
    present a tier ladder with rungs missing as though they were never there."""
    out: dict[str, str] = {}
    for tier in TIER_ORDER:
        err = (availability.get(tier) or {}).get("probe_error")
        if err and not (availability.get(tier) or {}).get("available"):
            out[tier] = err
    return out


def rank_decision(availability: dict[str, dict], prefer: Optional[str] = None) -> dict[str, Any]:
    """Pure: given per-tier availability, pick the tier and explain. `prefer`
    forces a tier when it is available. Returns chosen tier (or None), the
    ordered available tiers, and the rejected alternatives.

    An IGNORED `prefer` is disclosed, never silent (`prefer_honored` +
    `prefer_ignored_reason`, and the caller stamps it onto `install_call`). It
    used to fall through without a word: `prefer='pip'` with pip unavailable
    returned conda, and a typo'd `prefer='spak'` returned conda — same output,
    two very different situations, and no way to tell either from "conda is
    simply what I wanted". The caller ASKED for something and did not get it;
    that is exactly the case where staying quiet is a lie of omission."""
    available = [t for t in TIER_ORDER if availability.get(t, {}).get("available")]
    chosen = None
    prefer_ignored_reason = ""
    if prefer and prefer in available:
        chosen = prefer
    elif available:
        chosen = available[0]

    if prefer and prefer != chosen:
        if prefer not in TIER_ORDER:
            prefer_ignored_reason = (
                f"prefer={prefer!r} is not a known tier (known: {', '.join(TIER_ORDER)}) "
                f"— check for a typo; it was IGNORED")
        else:
            prefer_ignored_reason = (
                f"prefer={prefer!r} was IGNORED: that tier is not available for this tool"
                + (f" (available: {', '.join(available)})" if available else ""))

    if chosen is None:
        rationale = ("no registry/repo tier found — pass a github_repo (or repo/archive URL) "
                     "to unlock synthesis (the universal agent-read path), else manual")
        if prefer_ignored_reason:
            rationale = f"{prefer_ignored_reason}. {rationale}"
        return {"chosen": None, "available": [], "alternatives": [],
                "prefer_honored": False if prefer else None,
                "prefer_ignored_reason": prefer_ignored_reason,
                "rationale": rationale}

    why = _TIER_RATIONALE.get(chosen, chosen)
    others = [t for t in available if t != chosen]
    rationale = f"{chosen}: {why}" + (
        f"; also available but lower-priority: {', '.join(others)}" if others else ""
    )
    if prefer == chosen and others:
        rationale = f"{chosen} (forced via prefer): {why}; would otherwise consider {', '.join(others)}"
    if prefer_ignored_reason:
        rationale = f"{prefer_ignored_reason} → fell back to {rationale}"
    return {
        "chosen": chosen,
        "available": available,
        "alternatives": [{"tier": t, "detail": availability.get(t, {})} for t in others],
        "prefer_honored": (prefer == chosen) if prefer else None,
        "prefer_ignored_reason": prefer_ignored_reason,
        "rationale": rationale,
    }


def _install_call(tier: str, tool: str, version: str, detail: dict, github_repo: str) -> str:
    v = version or detail.get("latest") or ""
    if tier == "author_image":
        img = detail.get("ref") or "<author-image-ref>"
        return (f'freeze_from_image(image="{img}", name="{tool}", '
                f'tools=[{{"name": "{tool}", "evidence": "<cmd that RUNS {tool} in-image>"}}])'
                '  # adopt the authors\' own image by digest')
    if tier == "authors_recipe":
        repo = detail.get("repo") or github_repo or "<owner/repo>"
        rec = (detail.get("recipe") or {})
        path = rec.get("path") or "Dockerfile"
        return (f'build_env_from_authors_recipe(repo="https://github.com/{repo}", name="{tool}", '
                f'recipe="{path}", ref="<tag/commit>", '
                f'tools=[{{"name": "{tool}", "evidence": "<cmd that RUNS {tool} in-image>"}}])'
                f'  # build the authors\' {path}, don\'t reconstruct')
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
    if tier == "r_github":
        return f'install_r_package(env, "{tool}", source="github:{github_repo}")'
    if tier == "binary":
        asset = (detail.get("assets") or ["<release-asset-url>"])[0]
        return f'install_release_binary(env, "{tool}", url="{asset}", sha256="<published>")'
    if tier == "synthesis":
        url = f"https://github.com/{github_repo}" if github_repo else "<repo-or-archive-url>"
        return (f'synth_fetch("{url}")  # read its build files, then '
                f'synth_build(env, "{url}", "{tool}", commit, commands=[...], evidence="...")')
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
        availability["binary"]    = {"available": gh["has_release_assets"], **gh}
        # synthesis ranks above source (both need only repo_exists): the agent reads
        # the repo's real build, so it's the robust default; source is the fast-path.
        availability["synthesis"] = {"available": gh["repo_exists"], **gh}
        availability["source"]    = {"available": gh["repo_exists"], **gh}
        # An R package on github → the PURPOSE-BUILT R path (remotes::install_github +
        # BiocManager bootstrap + load-or-die), which is far more reliable than making
        # synthesis improvise R CMD INSTALL. Only offered with a language='r' hint (a
        # bare github repo could be anything); ranks above synthesis via TIER_ORDER.
        if language == "r":
            availability["r_github"] = {"available": gh["repo_exists"], **gh}

    # AUTHORS' OWN RESOURCES (the reliability gate). Find the tool's repo — explicit or
    # extracted from registry metadata — and ask: does the tool publish an image, and
    # does its own recipe install pieces a conda/pip reconstruction would DROP? If so,
    # the authors' path outranks conda (build what they build, don't reconstruct). If
    # the recipe is trivially registry-equivalent, the gate stays SHUT and conda wins.
    # Best-effort: any probe failure simply leaves the author tiers unavailable, so the
    # registry route is unaffected. This is what makes the agent 'thread the needle'
    # automatically on tools like Talos (PyPI hit, but a Dockerfile compiling a fork).
    ev = repo_evidence(availability, github_repo)
    if ev and not ev.get("anchored"):
        # NOT ASSESSED — and that is a THIRD state, distinct from "fired" and "errored".
        # Running the gate here is what probed Mucephie/DORADO and autonomio/talos; skipping
        # it silently would be worse still, because "no authoring image/recipe found" would
        # then be reported about a repo we deliberately declined to look at. Say which.
        availability["authors_gate_not_assessed"] = {
            "available": False, "repo": ev["repo"], "repo_source": ev["source"],
            "reason": (f"no anchored repo for '{tool}': the only candidate is "
                       f"{ev['repo']} from {ev['detail']}. A repo is not the tool's just "
                       f"because it shares its name — so the authors' path was NOT "
                       f"assessed, and nothing here says they publish no image or recipe.")}
    eff_repo = ev.get("repo", "") if ev.get("anchored") else ""
    if eff_repo and "/" in eff_repo and probe_authors_sources is not None:
        try:
            owner, rp = eff_repo.split("/", 1)
            assessment = probe_authors_sources(tool, owner=owner, repo=rp, timeout=timeout)
            availability["author_image"] = {
                "available": bool(assessment.get("author_image")),
                "assessment": assessment, "repo": eff_repo,
                "repo_source": ev["source"], "repo_detail": ev["detail"],
                "repo_anchored": True,
                **(assessment.get("author_image") or {})}
            availability["authors_recipe"] = {
                "available": bool(assessment.get("reconstruction_incomplete")),
                "assessment": assessment, "repo": eff_repo,
                "repo_source": ev["source"], "repo_detail": ev["detail"],
                "repo_anchored": True,
                "recipe": assessment.get("authors_recipe")}
        except Exception as e:
            # Best-effort: a probe failure must not break registry routing. But it MUST
            # NOT be silent either — a bare `except: pass` here let a call-signature
            # TypeError disable this entire gate for every tool, invisibly, across five
            # commits and a green test suite (audit 2026-07-16). The gate is either
            # reported as fired or reported as errored; there is no third state.
            availability["authors_gate_error"] = {
                "available": False, "repo": eff_repo,
                "error": f"{type(e).__name__}: {e}"}

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
    if decision["chosen"] is None and not github_repo and not unchecked_tiers(availability):
        disc = probe_github_search(tool, timeout)
        if disc.get("probe_error"):
            # The investigation's OWN probe failed. "I searched github and found nothing"
            # is now unavailable to us as a claim — so say what actually happened instead
            # of letting an empty candidate list stand in for a finding.
            decision["discovery_error"] = disc["probe_error"]
            decision["rationale"] = (
                f"NO registry hit for '{tool}', AND the github fallback search FAILED "
                f"({disc['probe_error']}) — so we have NOT established that '{tool}' is "
                f"unfindable; we established nothing. Re-run"
                + (" (set GITHUB_TOKEN: unauthenticated github search is 10 req/min)"
                   if "rate_limited" in disc["probe_error"] else "")
                + f", or pass github_repo='owner/repo' to skip discovery. "
                + decision["rationale"])
        elif disc["found"]:
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
    unchecked = unchecked_tiers(availability)
    decision.update({
        "tool": tool,
        "version": version or None,
        "language": language or None,
        "github_repo": github_repo or None,
        "ambiguous": ambiguous,
        "probed": availability,
        "cross_namespace_collisions": cross_namespace_collisions,
        "unchecked_tiers": unchecked,
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
    # STATE the absence; never omit the key. A caller reading `decision.get("install_call")`
    # cannot tell "there is no call to make" from "this producer forgot the field", and the
    # difference is a KeyError at best and a skipped disclosure at worst (see the gate-error
    # block below, which used to hang its entire note off `if decision.get("install_call")`
    # and therefore vanished on every refusal — the one outcome where it matters most).
    decision["identity"] = None
    decision["install_call"] = None
    if chosen:
        identity = assess_identity(tool, chosen, availability, github_repo)
        decision["identity"] = identity
        decision["install_call"] = _install_call(
            chosen, tool, version, availability.get(chosen, {}), github_repo
        )
        # NOTE: the identity disclosure is applied LAST, at the end of this function —
        # every _disclose PREPENDS, so the last one applied is the one a reader sees first,
        # and "this package describes itself as a spreadsheet parser" outranks every other
        # caveat we might attach. It is the headline, not a footnote to the gate's status.

    # A TIER WE COULD NOT REACH IS NOT A TIER THAT SAID NO. Ranking silently skips an
    # unchecked tier, so the answer reads as "the best there is" while meaning "the best of
    # what we could reach". Only tiers ranked ABOVE the pick matter — one below it could not
    # have won anyway, and warning about it is the noise that trains readers to skip.
    if unchecked:
        cutoff = TIER_ORDER.index(chosen) if chosen in TIER_ORDER else len(TIER_ORDER)
        blocking = {t: e for t, e in unchecked.items() if TIER_ORDER.index(t) < cutoff}
        if blocking:
            detail = ", ".join(f"{t} ({e})" for t, e in blocking.items())
            # Only advise a GitHub token when GitHub is what ran out. Advice aimed at the
            # wrong host is worse than none: it sends the reader to fix something that was
            # never broken, and teaches them the next hint is noise too.
            gh_quota = any(t in _GITHUB_BACKED_TIERS and "rate_limited" in e
                           for t, e in blocking.items())
            retry = ("Re-run when the probe recovers"
                     + (" (set GITHUB_TOKEN to raise the GitHub quota from 60/hr to 5000/hr)"
                        if gh_quota else "") + ".")
            if chosen is None:
                # NOT a refusal — a refusal is a conclusion, and we reached none. "No tier
                # found" here would be the same lie one level up: we did not find that
                # there is nothing; we failed to look.
                _disclose(decision,
                          f"NOT A REFUSAL — UNRESOLVED: {detail}. We never reached a verdict "
                          f"on '{tool}' because those probes did not answer. Nothing here "
                          f"says '{tool}' is unavailable. {retry}", "")
            else:
                _disclose(decision,
                          f"UNCHECKED TIER(S) RANKED ABOVE THIS PICK: {detail}. Those probes "
                          f"never reached a conclusion, so '{tool}' is NOT known to be absent "
                          f"from them — this is the best answer among the tiers we could "
                          f"REACH, which is a different claim. {retry}",
                          "a higher-ranked tier was NOT ruled out, only unreachable")

    # A BROKEN RELIABILITY GATE MUST REACH THE CALLER, not sit in `probed`.
    # `authors_gate_error` was recorded and then consumed by nobody: resolve() went on to
    # return a clean, confident `chosen: conda` + install_call, with the failure buried in
    # a sibling dict no agent reads. For a tool like Talos that is the exact reconstruction
    # bug the gate exists to prevent, delivered with full confidence. (audit 2026-07-16
    # Tier 6: the fix for the silent gate was itself unconsumed and untested.)
    not_assessed = (availability.get("authors_gate_not_assessed") or {}).get("reason")
    if not_assessed:
        _disclose(decision,
                  f"AUTHORS-PATH NOT ASSESSED — {not_assessed} If this IS the project you "
                  f"mean, re-run with github_repo to unlock the authors' own image/recipe; "
                  f"if it is not, this pick is a same-name package from another domain.",
                  "we did NOT check the authors' path, and did NOT rule it out")
    gate_err = (availability.get("authors_gate_error") or {}).get("error")
    img_err = ((availability.get("author_image") or {}).get("assessment") or {}).get(
        "author_image_error")
    if gate_err:
        _disclose(decision,
                  f"AUTHORS-PATH GATE FAILED ({gate_err}) — we could NOT check whether the "
                  f"authors ship their own image/recipe, so this pick is the registry's "
                  f"answer to a question the gate never got to weigh in on. If '{tool}' "
                  f"bundles compiled or vendored pieces, a conda/pip reconstruction can "
                  f"silently drop them.",
                  "the authors' own install path was NOT ruled out")
    if img_err:
        _disclose(decision,
                  f"AUTHOR-IMAGE PROBE FAILED ({img_err}) — 'the authors publish no image' "
                  f"was NOT established here; it was unchecked.",
                  "the authors' own install path was NOT ruled out")

    # An IGNORED `prefer` gets the same treatment, for the same reason: the caller
    # explicitly asked for a tier and did NOT get it. Silently handing back a different
    # tier's install_call is indistinguishable from honoring the request — and a typo
    # ('spak') looked identical to a real fallback.
    if decision.get("prefer_ignored_reason"):
        _disclose(decision, decision["prefer_ignored_reason"],
                  f"this is the {decision.get('chosen')} tier, NOT the one you asked for")

    # IDENTITY LEADS. Applied last so it prepends in front of every other caveat: a reader
    # who stops after one sentence must get "this may not be the tool you mean", not "the
    # authors' path was not assessed" — the latter is a CONSEQUENCE of the former.
    # install_call is the field an agent copies and runs; leaving it a clean, confident
    # one-liner while the doubt sits in a sibling key is how a warning gets skipped.
    ident = decision.get("identity") or {}
    if ident and not ident.get("confirmed"):
        _disclose(decision, ident["note"],
                  "confirm the above IS the tool you mean before running")
    return decision
