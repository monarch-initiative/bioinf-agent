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
              "conda", "pip", "cran", "bioconductor", "r_github", "binary", "spack",
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
    best = None  # (version_key, channel, version, summary)
    for channel in ("bioconda", "conda-forge"):
        data = _get_json(f"https://api.anaconda.org/package/{channel}/{name.lower()}", timeout)
        if isinstance(data, dict) and data.get("versions"):
            ver = _pick_latest(data["versions"], data.get("latest_version") or "")
            key = _version_key(ver)
            if best is None or (key is not None and (best[0] is None or key > best[0])):
                best = (key, channel, ver, data.get("summary") or "")
    if best:
        return {"available": True, "channel": best[1], "latest": best[2],
                "summary": best[3]}
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
    data = _get_json(f"https://pypi.org/pypi/{name}/json", timeout)
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
    return {"available": False}


def probe_cran(name: str, timeout: int = 12) -> dict[str, Any]:
    """CRAN metadata. Captures URL + BugReports so a github_repo-supplied
    resolve() can confirm a same-name CRAN hit references the same project.

    `summary` is CRAN's Title + Description — the identity evidence. CRAN's
    `cellranger` is "Translate Spreadsheet Cell Ranges to Rows and Columns", not
    10x Genomics' Cell Ranger; the name is all they share."""
    data = _get_json(f"https://crandb.r-pkg.org/{name}", timeout)
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
        # these tiers ARE a repo — identity is whatever repo the caller/discovery named
        out["anchor"] = "repo"
        out["evidence"] = [detail.get("repo") or github_repo or ""]
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
    # worth nothing. (Live campaign: the only false alarm across 30 real tools was `vep`
    # via the spack tier, which carries no metadata at all.)
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


def _github_owner_repo(availability: dict, github_repo: str = "") -> str:
    """Best 'owner/repo' for the tool: the explicit github_repo, else extracted from the
    pip/cran registry metadata (home_page / project_urls / URL). This is what lets the
    authors-source gate fire AUTOMATICALLY on a tool that hits a registry — the common
    case (Talos hit PyPI) — without the caller having to hand us the repo."""
    if github_repo and "/" in github_repo:
        return github_repo.strip().strip("/")
    urls: list[str] = []
    pip = availability.get("pip", {})
    if pip.get("available"):
        urls.append(pip.get("home_page", ""))
        urls.append(pip.get("package_url", ""))
        urls += [str(v) for v in (pip.get("project_urls") or {}).values()]
    cran = availability.get("cran", {})
    if cran.get("available"):
        urls += [u.strip() for u in (cran.get("url", "") or "").split(",")]
        urls.append(cran.get("bug_reports", ""))
    for u in urls:
        m = _GH_REPO_RE.search(u or "")
        if m:
            return f"{m.group(1)}/{re.sub(r'[.]git$', '', m.group(2))}"
    return ""


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

    if tier == "r_github":
        if not repo:
            return {"kind": "defer", "tier": tier, "reason": "r_github tier needs github_repo"}
        return {"kind": "tool", "tier": tier, "spec": ic.r_package(tool, source=f"github:{repo}")}

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

    # The author tiers are not "not implemented" — they have REAL executors, they just
    # aren't reachable from this function. route() exists only to feed
    # env_freeze.build_env_from_tools, which bakes ONE image from conda/pip/tool specs;
    # the authors' path instead adopts or docker-builds the authors' OWN image, which is a
    # different build entirely (freeze_from_image / build_env_from_authors_recipe own it).
    # Say exactly that, and name the executor — the old text here blamed missing "engine
    # pypi support", which is both stale (pip routes fine, above) and irrelevant to these
    # tiers, so a caller who hit it would go looking in the wrong place.
    if tier in ("author_image", "authors_recipe"):
        executor = ("freeze_from_image" if tier == "author_image"
                    else "build_env_from_authors_recipe")
        return {"kind": "defer", "tier": tier,
                "reason": (f"tier {tier!r} is executed by {executor}(), not by a "
                           f"container-native bake — call it directly with the "
                           f"install_call resolve() returned. build_env_from_tools only "
                           f"bakes conda/pip/tool specs into one image.")}
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
        # An R package on github → the PURPOSE-BUILT R path (remotes::install_github +
        # BiocManager bootstrap + load-or-die), which is far more reliable than making
        # synthesis improvise R CMD INSTALL. Only offered with a language='r' hint (a
        # bare github repo could be anything); ranks above synthesis via TIER_ORDER.
        if language == "r":
            availability["r_github"] = {"available": gh["repo_exists"], **gh}

    # Spack is a real (curated, from-source) tier — ranks below precompiled binary
    # but above the agent-read synthesis fallback (a community recipe beats
    # improvisation). Needs only a name (registry probe), no github_repo.
    availability["spack"] = probe_spack(tool, timeout)

    # AUTHORS' OWN RESOURCES (the reliability gate). Find the tool's repo — explicit or
    # extracted from registry metadata — and ask: does the tool publish an image, and
    # does its own recipe install pieces a conda/pip reconstruction would DROP? If so,
    # the authors' path outranks conda (build what they build, don't reconstruct). If
    # the recipe is trivially registry-equivalent, the gate stays SHUT and conda wins.
    # Best-effort: any probe failure simply leaves the author tiers unavailable, so the
    # registry route is unaffected. This is what makes the agent 'thread the needle'
    # automatically on tools like Talos (PyPI hit, but a Dockerfile compiling a fork).
    eff_repo = _github_owner_repo(availability, github_repo)
    if eff_repo and "/" in eff_repo and probe_authors_sources is not None:
        try:
            owner, rp = eff_repo.split("/", 1)
            assessment = probe_authors_sources(tool, owner=owner, repo=rp, timeout=timeout)
            availability["author_image"] = {
                "available": bool(assessment.get("author_image")),
                "assessment": assessment, "repo": eff_repo,
                **(assessment.get("author_image") or {})}
            availability["authors_recipe"] = {
                "available": bool(assessment.get("reconstruction_incomplete")),
                "assessment": assessment, "repo": eff_repo,
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
        identity = assess_identity(tool, chosen, availability, github_repo)
        decision["identity"] = identity
        decision["install_call"] = _install_call(
            chosen, tool, version, availability.get(chosen, {}), github_repo
        )
        if not identity["confirmed"]:
            # Lead the rationale AND poison the install_call. install_call is the field an
            # agent copies and runs; leaving it a clean, confident one-liner while the
            # doubt sits in a sibling key is how a warning gets skipped. A caller who
            # pastes this now has to read why first — and if they strip the comment, they
            # did so deliberately, which is the whole point.
            decision["rationale"] = identity["note"] + " || " + decision["rationale"]
            decision["install_call"] = (
                f"# {identity['note']}\n"
                f"# ---- confirm the above IS the tool you mean before running: ----\n"
                + decision["install_call"])
    return decision
