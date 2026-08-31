"""
Resolver — given a requested tool, decide WHICH install tier to use and record
WHY. The one piece of genuine judgment in the system; every install flows from
the decision it produces.

It PROBES availability independently per tier (so it can rank AND record the
rejected alternatives), then RANKS by a preference order chosen to maximize
reproducibility + clean containerization + least build fragility:

    pull an existing image  >  clean registry package  >  build from source

Concretely: the authors' OWN published image (adopt by digest — pull, don't
build) outranks everything; then a clean conda/bioconda package (which itself
ships as a pre-built BioContainer we ADOPT rather than rebuild); then the
language registries; and only when the registry route is a RECONSTRUCTION that
would drop deps do we build the authors' Dockerfile (`authors_recipe`). Building
from a recipe is the OPPOSITE of using an existing image, so it never outranks a
pullable image or a clean package — see `_pullable_image` and TIER_ORDER.

The probes are observations (live registry lookups). The ranking is the
judgment. The ResolutionDecision (chosen tier + install call + rationale +
rejected alternatives) is the auditable output — the "why this method" that the
honesty contract otherwise lacked. Binary/source tiers only apply when a github
repo is supplied (you can't probe a release asset from a bare tool name).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from agent.models import core_data as _core_data

try:
    # the authors'-own-resources reliability gate (image / recipe completeness). Imported
    # softly so a resolver import never hard-depends on it; None disables the gate.
    from agent.skills.authors_sources import assess_tool_sources as probe_authors_sources
except Exception:  # pragma: no cover
    probe_authors_sources = None

try:
    # the "is there a pullable BioContainer?" query — quay.io/biocontainers, anonymous,
    # returns an adopt-by-digest ref. Soft import so the resolver never hard-depends on it.
    from agent.skills.biocontainers import resolve_biocontainer as _resolve_biocontainer
except Exception:  # pragma: no cover
    _resolve_biocontainer = None

# Preference order — lower index wins. Cross-cutting concerns (gpu/service/
# license/db) are flags layered on top, not tiers.
#
# `synthesis` is the UNIVERSAL repo tail (the "reduce infinity to 1" floor): for any
# fetchable repo it ranks ABOVE the conventional `source` generator, because the
# agent reads the tool's OWN build files (gated by provenance + grounding + the
# contract) and so handles EVERY repo — half-baked, run-by-path, custom build — not
# just the conventional make+binary case the `source` generator assumes. `source`
# (and binary) survive as opt-in FAST-PATHS, not the boundary of installable.
# PULL-AN-EXISTING-IMAGE FIRST. `author_image` (the authors' OWN published image, adopted
# by digest) sits at the top: pulling a pre-built, tested image is the lowest-resistance and
# highest-fidelity path, and it can't drop deps because it isn't a reconstruction. A clean
# conda/bioconda package comes next — and it, too, ships as a pre-built BioContainer we ADOPT
# rather than rebuild (see `_pullable_image`), so "conda wins" already means "pull an image"
# downstream.
#
# `authors_recipe` — BUILDING the authors' Dockerfile from source — is the OPPOSITE of using
# an existing image, so it sits BELOW conda, not above it. It fires only when the registry
# route is a genuine RECONSTRUCTION that would DROP deps (system/compiled/vendored/binary —
# the completeness gate in authors_sources) AND there is no clean conda package to fall back
# on: i.e. a pip/CRAN-only tool like Talos whose Dockerfile compiles a vendored fork. For a
# cleanly bioconda-packaged tool (miniprot, gatk4, samtools) conda outranks it and wins — a
# Dockerfile build must never beat a package the ecosystem already built and containerized.
# (This corrects the over-fire where `analyze_recipe_completeness` flagged self-build `make`
# / a self-source tarball as "incomplete" and routed cleanly-packaged tools to a heavy
# Dockerfile build.) See [[project-reliability-gate-routing]] / [[feedback-prioritize-authors-own-env-recipe]].
TIER_ORDER = ["author_image",
              "conda", "authors_recipe", "pip", "cran", "bioconductor", "r_github", "binary",
              "synthesis", "source", "manual"]

_TIER_RATIONALE = {
    "author_image": "the authors publish a container image — adopt it by digest (highest "
                    "fidelity: they built + tested the whole env; lowest cost: a pull)",
    "authors_recipe": "no pullable image and no clean conda package, and the authors' "
                    "Dockerfile/recipe installs deps a language-registry (pip/CRAN) reconstruction "
                    "would silently DROP (compiled/vendored/system/binary) — build THEIR recipe, "
                    "the reliable path a human would follow rather than reconstruct from scratch. "
                    "Ranks BELOW conda: a Dockerfile BUILD never beats a package the ecosystem "
                    "already built + containerized",
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


def _version_present(requested: str, versions: list) -> bool:
    """Is `requested` among `versions`? Compares by PEP440 normalization so a request for
    `1.0` matches a stored `1.0.0` (PyPI normalises), with an exact-string fallback for
    versions neither side can parse (serials, odd build strings). Absence of the list is
    NOT handled here — the caller only asks when it HAS a list, because an unfetched list
    is 'we don't know', not 'the version is missing'."""
    if not requested or not versions:
        return False

    def _key(v):
        # conda encodes a revision separator as '_' — R's native `5.8-1` ships on conda-forge
        # as `5.8_1`, which PEP440 cannot parse at all (InvalidVersion → None), so it silently
        # never matches a user's `5.8.1` and a version that DOES exist is falsely refused
        # (the ape 5.8.1 regression). Retry with '_'→'.' ONLY on a parse failure, so a
        # parseable version's PEP440 semantics (e.g. a real `-1` post-release) stay untouched.
        k = _version_key(v)
        if k is None:
            k = _version_key(str(v).replace("_", "."))
        return k

    rk = _key(requested)
    if rk is not None:
        for v in versions:
            vk = _key(str(v))
            if vk is not None and vk == rk:
                return True
    return str(requested) in {str(v) for v in versions}


def _nearest_versions(requested: str, versions: list, n: int = 4) -> list[str]:
    """The available versions bracketing `requested` by PEP440 order — a 'did you mean'
    hint for a typo'd/absent pin. Falls back to the latest few when nothing sorts."""
    parsed = sorted(((k, str(v)) for v in versions if (k := _version_key(str(v))) is not None),
                    key=lambda kv: kv[0])
    strs = [s for _, s in parsed]
    rk = _version_key(requested)
    if rk is None or not parsed:
        return [str(v) for v in versions][-n:]
    import bisect
    i = bisect.bisect_left([k for k, _ in parsed], rk)
    lo = max(0, i - n // 2)
    return strs[lo:lo + n]


def probe_conda(name: str, timeout: int = 12) -> dict[str, Any]:
    """Available on bioconda or conda-forge? Probes BOTH channels and picks the one
    with the higher REAL version — guards against an abandoned date-versioned build
    on one channel (e.g. bioconda's hmmlearn reports latest_version='20151031')
    shadowing the maintained package on the other. Ties keep bioconda (probed
    first), so bio-primary tools are unaffected."""
    best = None  # (version_key, channel, version, summary, repo, repo_field)
    errors: list[str] = []
    # Kept BESIDE `errors` rather than parsed back out of it: the joined strings are for a
    # human, and re-splitting `"{channel}: {err}"` would break on the first err containing
    # ": " — which every `TimeoutError: …` does.
    channel_errors: dict[str, str] = {}
    for channel in ("bioconda", "conda-forge"):
        data, err = _fetch_json(
            f"https://api.anaconda.org/package/{channel}/{name.lower()}", timeout)
        if err:
            errors.append(f"{channel}: {err}")
            channel_errors[channel] = err
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
                # THE RECIPE'S OWN LICENSE — free, in this same response, and the ONLY
                # signal that separates a commercial tool from a free one BEFORE anything
                # is installed. bioconda's `novoalign` says "Commercial (requires license
                # for use)" and its `sentieon` says "…; redistribution allowed"; samtools
                # says "MIT". Every identity fact for novoalign is CORRECT — right tool,
                # right channel, right description — so the ride has no reason to hesitate,
                # and the artifact that comes out is a commercial binary in an image
                # stamped redistributable. Absent is a fact, never "free".
                best = (key, channel, ver, data.get("summary") or "", repo, field,
                        [str(v) for v in data["versions"]], str(data.get("license") or ""))
    if best:
        # `versions` = the WINNING channel's full list, so a version-existence check compares
        # against the channel actually being emitted (bioconda's abandoned hmmlearn ≠
        # conda-forge's maintained one — the pick already resolved that, and the list follows it).
        #
        # `license_source` NAMES WHERE THE STRING CAME FROM, and every tier that captures a
        # licence now sets it. A licence with no stated origin cannot be told apart from a
        # licence nobody published: both arrive as `""` and both used to read `unrecognized`,
        # which is a claim ("we read one and could not place it") about a field we never
        # looked at. Same discipline as `repo_field` two lines up.
        out = {"available": True, "channel": best[1], "latest": best[2], "summary": best[3],
               "versions": best[6], "license": best[7],
               "license_source": (f"the {best[1]} recipe's `license`" if best[7] else "")}
        if best[4]:
            out["repo"] = best[4]
            out["repo_field"] = best[5]     # provenance: WHICH field vouched for it
        if errors:
            # A HIT IS A FACT; "THIS IS THE NEWER BUILD" IS A COMPARISON, and a comparison
            # made against a channel that never answered is a claim about what we could
            # REACH. This function's whole reason for probing both channels is the second
            # thing — guarding against an abandoned build on one channel shadowing the
            # maintained package on the other — and that guard used to fail SILENTLY open.
            #
            # Measured live 2026-08-06 while driving `seurat` end to end: with conda-forge
            # timing out, `probe_conda('r-seurat')` returned bioconda's **3.0.2 (2019)** as a
            # clean pick, indistinguishable from a real answer, while conda-forge carries
            # **5.5.1**. Two calls one second apart — `language='r'` with the name spelled
            # `seurat` vs `Seurat`, which lowercase to the SAME URL — disagreed by two major
            # versions of the standard single-cell toolkit, and neither said why.
            #
            # The old comment here ("errors only matter when NOTHING was found") is right
            # about availability and wrong about the pick, so the error is recorded either
            # way now and `resolve` discloses it on a conda win.
            out["channel_errors"] = channel_errors
        return out
    # A hit on either channel is a fact regardless of the other's health, so errors only
    # matter when NOTHING was found: then "not on conda" rests on a probe that never ran.
    # Reported, never inferred — a silent conda miss demotes the tool to the pip/cran
    # fall-through, which is the measured 9-in-10-wrong path.
    if errors:
        return {"available": False, "probe_error": "; ".join(errors)}
    return {"available": False}


#: The longest string we will treat as a licence IDENTIFIER rather than as licence PROSE.
#: PyPI's `info.license` is free text: most packages put "MIT" in it and some paste the
#: entire licence body. Keyword-matching a licence BODY is the `bcftools --version` mistake
#: on a new axis — GPLv3's own text discusses commercial redistribution, so a regex over it
#: returns `restricted` for the most permissive licence in the field. Prose is reported AS
#: prose (`license_source` says so, `license` stays empty) rather than classified.
_LICENSE_IDENTIFIER_MAX = 120


def _pypi_license(info: dict) -> tuple[str, str]:
    """PyPI's licence for one package, as (string, where-it-came-from). Pure.

    Best-vouched first, and every branch NAMES its source, because "no licence" and
    "a licence we declined to read" are different facts about the package:

      * `license_expression` — PEP 639, an SPDX expression. Structured and authoritative.
      * the `License ::` trove classifiers — a controlled vocabulary, which is what most
        of PyPI actually publishes ("License :: OSI Approved :: MIT License").
      * `license` — free text, taken ONLY when it is short enough to be an identifier.
        The bare "OSI Approved" classifier is dropped: it names no licence.

    This exists because `identity.license` was fed by the conda probe alone, so
    `license_disposition` could only ever be `unrecognized` for anything off bioconda —
    and a tool is licence-gated PRECISELY because its bytes are not redistributable, which
    is why it is not on bioconda. The one fact that could flag a gated artifact was
    collected only on the tier gated artifacts cannot be on."""
    expr = str(info.get("license_expression") or "").strip()
    if expr:
        return expr, "PyPI's `license_expression` (SPDX)"
    trove = [c.split("::")[-1].strip() for c in (info.get("classifiers") or [])
             if isinstance(c, str) and c.startswith("License ::")]
    trove = [c for c in trove if c and c.lower() != "osi approved"]
    if trove:
        return "; ".join(trove), "PyPI's `License ::` classifiers"
    lic = str(info.get("license") or "").strip()
    if not lic:
        return "", ""
    if len(lic) <= _LICENSE_IDENTIFIER_MAX and "\n" not in lic:
        return lic, "PyPI's `license` field"
    # Present, and not something we will keyword-match. Reported, never inferred.
    return "", "PyPI's `license` field, which holds the licence TEXT rather than an identifier"


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
        # A version whose files are ALL yanked is effectively absent — installing it fails —
        # so it must not count as present in a version-existence check (anndata 0.12.15 was
        # 'released against the wrong branch' and yanked). A version with no files at all is
        # likewise not installable. releases: {version: [file, ...]}.
        rel = data.get("releases") or {}
        versions = [v for v, files in rel.items()
                    if files and any(not f.get("yanked") for f in files)]
        lic, lic_src = _pypi_license(info)
        return {
            "available": True,
            "latest": info.get("version"),
            "versions": versions,
            "summary": (info.get("summary") or "").strip(),
            "home_page": info.get("home_page") or "",
            "project_urls": info.get("project_urls") or {},
            "package_url": info.get("package_url") or "",
            "license": lic,
            "license_source": lic_src,
        }
    return {"available": False, **({"probe_error": err} if err else {})}


def _probe_cran_exact(name: str, timeout: int) -> dict[str, Any]:
    """One crandb lookup, exactly as spelled. `resolved_name` is ALWAYS set on a
    hit — the name CRAN actually knows the package by, which is not necessarily
    the name that was asked for (see `probe_cran`)."""
    data, err = _fetch_json(f"https://crandb.r-pkg.org/{name}", timeout)
    if isinstance(data, dict) and data.get("Package"):
        summary = " — ".join(x for x in ((data.get("Title") or "").strip(),
                                         (data.get("Description") or "").strip()) if x)
        # CRAN REQUIRES a License field of every package, so this is the one registry where
        # an empty licence is itself remarkable. Captured for the same reason conda's is:
        # the resolver's only gatedness signal must not depend on which tier we picked.
        _lic = str(data.get("License") or "").strip()
        return {
            "available": True,
            "latest": data.get("Version"),
            "summary": re.sub(r"\s+", " ", summary)[:400],
            "url": data.get("URL") or "",
            "bug_reports": data.get("BugReports") or "",
            "license": _lic,
            "license_source": "CRAN's `License` field" if _lic else "",
            # CRAN's own spelling, not ours. `install.packages()` is case-SENSITIVE,
            # so this is what has to reach the install_call — never the query.
            "resolved_name": data.get("Package"),
        }
    return {"available": False, **({"probe_error": err} if err else {})}


def _cran_case_variants(name: str) -> list[str]:
    """Mechanical capitalizations to retry a CLEAN CRAN miss with, at most 2.

    Deliberately mechanical and tiny rather than smart: CRAN package names are
    case-sensitive but conventionally either all-lower (`ggplot2`, `data.table`)
    or capitalized (`Seurat`, `Matrix`). A caller who typed mixed case meant it,
    so we do not second-guess them — only an all-one-case query is retried."""
    if not name or not (name.islower() or name.isupper()):
        return []
    lower = name.lower()
    out: list[str] = []
    for cand in (lower.capitalize(),
                 re.sub(r"(^|[-._])([a-z])", lambda m: m.group(1) + m.group(2).upper(), lower)):
        if cand != name and cand not in out:
            out.append(cand)
    return out[:2]


def probe_cran(name: str, timeout: int = 12) -> dict[str, Any]:
    """CRAN metadata. Captures URL + BugReports so a github_repo-supplied
    resolve() can confirm a same-name CRAN hit references the same project.

    `summary` is CRAN's Title + Description — the identity evidence. CRAN's
    `cellranger` is "Translate Spreadsheet Cell Ranges to Rows and Columns", not
    10x Genomics' Cell Ranger; the name is all they share.

    CASE. crandb.r-pkg.org is case-SENSITIVE: `/seurat` 404s while `/Seurat`
    returns 5.5.1. Before this, the resolver's answer depended on the shift key —
    `resolve('seurat')` returned `install_pip_package(env, "seurat", version="0.0.2")`,
    an empty PyPI squat with no summary at all and no warning, while `resolve('Seurat')`
    correctly refused as ambiguous. So a clean miss is retried against at most two
    mechanical capitalizations.

    TWO RULES, both load-bearing:
      * Only a CLEAN miss is retried. A `probe_error` means crandb did not ANSWER,
        and retrying it into an absence is the ABSENT-≠-UNCHECKED failure that
        `test_probe_honesty.py` exists to prevent. On an error we stop and report it.
      * The hit carries `resolved_name` — CRAN's spelling — and callers must build
        the install_call from THAT, never from the query. `install.packages()` is
        itself case-sensitive, so a probe that found `Seurat` while the call still
        said `seurat` would trade a wrong package for a broken one."""
    rec = _probe_cran_exact(name, timeout)
    if rec.get("available") or rec.get("probe_error"):
        return rec
    for cand in _cran_case_variants(name):
        alt = _probe_cran_exact(cand, timeout)
        if alt.get("available"):
            return {**alt, "queried_name": name}
        if alt.get("probe_error"):
            break                      # never retry an error into an absence
    return rec


def probe_package_family(tool: str, timeout: int = 12) -> dict[str, Any]:
    """SEARCH the channels for every package that shares this tool's name-family, and return
    them all. `{}` when the family has one member (the common case) — silence, not noise.

    THIS IS THE RESEARCH STEP, and it exists because the system used to stop investigating
    the moment it found ANYTHING. `resolve('gatk')` hits bioconda's `gatk`, so the dead-end
    discovery path never fires, and the answer is `gatk=3.8` — a 2017 release — because
    bioinformatics versions its tools by RENAMING the package and GATK4 ships as `gatk4`.
    Every identity fact about that pick is CORRECT: right project, right channel, right
    description, a decade stale. A registry hit is not the same as an answer, and "we found
    one, stop looking" is how a stale one gets shipped with full confidence.

    An earlier cut of this GUESSED one successor (`{base}{major+1}`) and probed for it. That
    worked for gatk by arithmetic accident and could not see `macs` 1.4.3 sitting under
    `macs2`/`macs3`. Searching returns the whole family for the same single HTTP call, and
    stops encoding an assumption about how projects number themselves.

    IT DOES NOT RANK, AND MUST NOT. The family is evidence, and reading it needs world
    knowledge the resolver has no business faking:

        gatk    -> gatk 3.8 "The full Genome Analysis Toolkit (GATK) framework"
                   gatk4 4.6.2.0 "Genome Analysis Toolkit (GATK4)"        <- same toolkit
        bowtie  -> bowtie 1.3.1 "An ultrafast memory-efficient short read aligner"
                   bowtie2 2.5.5 "A fast and sensitive gapped read aligner."
                   ...different repos, different capabilities: two TOOLS, not two versions
        macs2   -> macs 1.4.3 / macs2 2.2.9.1 / macs3 3.0.4, one project's whole history

    "Newest wins" would swap bowtie1 for bowtie2. Refusing would tax every one of these for
    a question most callers have already answered. Both were tried on paper; what the ride
    actually needs is the TABLE, in one call, so it can pick without a second round trip —
    which is the whole complaint against the previous behaviour.

    Absent is not unchecked: a failed search returns `{"probe_error": ...}` rather than an
    empty family, so "this tool has no siblings" is never inferred from a probe that did not
    run."""
    base = re.match(r"^(.*?)\d*$", tool.strip().lower()).group(1)
    if not base:
        return {}
    data, err = _fetch_json(
        f"https://api.anaconda.org/search?name={urllib.parse.quote(base)}&type=conda", timeout)
    if err:
        return {"probe_error": err}
    if not isinstance(data, list):
        return {}
    # `^{base}\d*$` — the family is the bare name plus its numbered siblings, nothing else.
    # A bare substring match drags in emacs/gromacs for `macs` and bioconductor-rbowtie2 for
    # `bowtie`; those are different projects that merely contain the letters.
    pat = re.compile(rf"^{re.escape(base)}\d*$")
    members = []
    for pkg in data:
        if pkg.get("owner") not in ("bioconda", "conda-forge"):
            continue
        if not pat.match(str(pkg.get("name") or "")):
            continue
        m = _GH_REPO_RE.search(str(pkg.get("dev_url") or pkg.get("home") or ""))
        members.append({
            "name": pkg["name"], "channel": pkg["owner"],
            "latest": pkg.get("latest_version"),
            "summary": re.sub(r"\s+", " ", str(pkg.get("summary") or "")).strip()[:200],
            "repo": f"{m.group(1)}/{re.sub(r'[.]git$', '', m.group(2))}" if m else "",
        })
    if len(members) < 2:
        return {}                      # a family of one is just the package; say nothing

    # ...and say nothing when the caller ALREADY NAMED the newest member. `resolve('gatk4')`
    # does not need to be told that `gatk` exists: the request names the current lineage, the
    # pick is that lineage, and there is no decision left to inform. Same for bowtie2, macs3,
    # hisat2. The bare name sorts lowest because it is the original — `gatk` is GATK3,
    # `bowtie` is bowtie1 — so "highest numeric suffix" is "newest lineage", which is the one
    # ordering claim this function makes and the only one the names actually support.
    def _suffix(n: str) -> int:
        m = re.match(rf"^{re.escape(base)}(\d*)$", n)
        return int(m.group(1)) if m and m.group(1) else 0

    asked = tool.strip().lower()
    if any(m["name"] == asked for m in members) and \
            _suffix(asked) == max(_suffix(m["name"]) for m in members):
        return {}
    members.sort(key=lambda x: (_suffix(x["name"]), x["name"]))
    return {"base": base, "members": members}


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
    latest release carry downloadable assets (→ binary tier)?

    Also captures the fork lineage the response ALREADY carries but used to discard —
    `is_fork` / `parent` (immediate) / `upstream` (GitHub's `source`, the fork-chain
    ROOT) / `full_name` (canonical, after the 301 a renamed/transferred repo issues) /
    `default_branch`. These feed the fork-anchor in resolve(): a user who names a
    divergent fork must not silently receive the registry's build of the PARENT. Free —
    no extra call. (`source` is renamed to `upstream` to avoid colliding with the
    `source` install-TIER concept.)"""
    out = {"repo_exists": False, "has_release_assets": False, "assets": [],
           "is_fork": False, "parent": "", "upstream": "", "full_name": "",
           "default_branch": ""}
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
    out["is_fork"] = bool(data.get("fork"))
    out["parent"] = ((data.get("parent") or {}).get("full_name") or "")
    out["upstream"] = ((data.get("source") or {}).get("full_name") or "")
    out["full_name"] = (data.get("full_name") or repo)   # canonical (301 followed)
    out["default_branch"] = (data.get("default_branch") or "")
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
    # VENDOR-CDN FALLBACK, asked HERE rather than in resolve() for two reasons. It is the
    # same question this function already answers — "what can this repo give us as a
    # binary?" — and it is the same I/O SEAM, so every existing stub of probe_github
    # covers it. Hanging it off a second probe in resolve() sent 29 hermetic tests to
    # api.github.com, which is the conftest guard telling us the seam was in the wrong
    # place. Only when the release offers nothing, so the better-anchored answer always
    # wins and the common path pays no extra request.
    if out["repo_exists"] and not out["has_release_assets"] and not out.get("releases_probe_error"):
        vend = probe_readme_assets(repo, timeout)
        if vend.get("probe_error"):
            out["vendor_probe_error"] = vend["probe_error"]
        elif vend.get("found"):
            out["vendor_asset"] = vend["url"]
            out["vendor_asset_version"] = vend["version"]
            out["vendor_asset_reachable"] = vend["reachable"]
            out["asset_source"] = "readme"
    return out


def _canon_repo(repo: str, timeout: int = 12) -> tuple[str, str]:
    """Canonicalize an 'owner/repo' to GitHub's own `full_name`, following the 301 a
    renamed/transferred repo issues (endrebak/pyranges → pyranges/pyranges0;
    theislab/scanpy → scverse/scanpy). Returns `(canonical_lower, status)` where status is:

      "ok"     — a 200; canonical_lower is the authoritative full_name, lowercased.
      "absent" — GitHub answered 404; the repo does not exist under that name (a FACT).
      "error"  — the probe never answered (rate-limit/timeout); UNCHECKED.

    The status is the whole point: the fork-anchor may only call a repo mismatch a
    CONTRADICTION when BOTH sides are "ok". An "absent" or "error" canon means we could
    not establish a distinct live lineage, so the caller degrades to keep-conda-and-
    disclose — never a refuse. (Rendering UNCHECKED as a contradiction is the exact
    Rule-2 violation this seam exists to prevent; a bare raw-lowercase fallback would do
    precisely that under the 60/hr quota.)"""
    norm = (repo or "").strip().strip("/").lower()
    if not norm or "/" not in norm:
        return norm, "absent"
    data, err = _fetch_json(f"https://api.github.com/repos/{repo.strip().strip('/')}", timeout)
    if err:
        return norm, "error"
    if data is None:
        return norm, "absent"
    return (data.get("full_name") or norm).lower(), "ok"


def _fork_diverges(parent_repo: str, fork_full_name: str, fork_default: str,
                   timeout: int = 12) -> tuple[Optional[bool], str]:
    """Does `fork_full_name` carry commits its `parent_repo` does not? Compares each
    side's ACTUAL default branch (a fork on `main` vs a parent on `master` is the common
    trap that makes a naive default...default compare 404). Returns `(diverged, error)`:

      (True,  "")      — GitHub compare status is "ahead" or "diverged": the fork has its
                         own commits. The user who named it opted into that divergence.
      (False, "")      — status "identical" or "behind": a pristine fork adds nothing, so
                         the registry's build of the parent is equivalent — keep conda.
      (None,  <err>)   — the parent lookup or the compare never answered (or 404'd on an
                         unrelated-history/branch-name mismatch): UNCHECKED. The caller
                         discloses and keeps conda; it never treats unchecked as pristine.

    Reads the qualitative `status` ONLY — never ahead_by/behind_by, which drift."""
    if not parent_repo or "/" not in parent_repo or not fork_full_name:
        return None, "no_parent"
    pdata, perr = _fetch_json(f"https://api.github.com/repos/{parent_repo}", timeout)
    if perr:
        return None, perr
    if pdata is None:
        return None, "parent_absent"
    parent_default = pdata.get("default_branch") or "master"
    parent_owner = parent_repo.split("/", 1)[0]
    fork_owner = fork_full_name.split("/", 1)[0]
    head_branch = fork_default or "master"
    base = f"{parent_owner}:{parent_default}"
    head = f"{fork_owner}:{head_branch}"
    cmp_data, cmp_err = _fetch_json(
        f"https://api.github.com/repos/{parent_repo}/compare/{base}...{head}", timeout)
    if cmp_err:
        return None, cmp_err
    if not isinstance(cmp_data, dict) or not cmp_data.get("status"):
        return None, "compare_unavailable"     # 404 = no common ancestor / bad branch name
    return (cmp_data.get("status") in ("ahead", "diverged")), ""


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


#: A URL in a README that looks like a downloadable build artifact rather than a doc link.
_DOWNLOADABLE = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip", ".deb", ".rpm", ".AppImage")


def probe_readme_assets(repo: str, timeout: int = 12, target_os: str = "linux",
                        target_arch: str = "amd64") -> dict[str, Any]:
    """WHERE DOES A VENDOR SAY TO DOWNLOAD, when the release carries no assets?

    The binary tier's availability was `gh["has_release_assets"]` and nothing else, so a
    project that publishes builds on its OWN CDN read as "no binary exists" and fell
    through to a multi-hour source build. Measured (Phase D): `nanoporetech/dorado` at
    v2.1.1 has **0 release assets** and a 402-byte release body with **no URLs in it** —
    while its README names
    `https://cdn.oxfordnanoportal.com/software/analysis/dorado-2.1.1-linux-x64.tar.gz`
    (3.47 GB, 200). `install_release_binary` and `resolve_linux_asset` route B already
    accept a direct vendor URL; only DISCOVERY was github-shaped.

    The README is the authors' own instruction for how to get their software
    ([[feedback-prioritize-authors-own-env-recipe]]), which is why it is the right place
    to look and not a scrape of convenience. Reading the release BODY first would be
    better anchored — it is tied to a tag — and it was tried: dorado's is empty, so this
    reads the README.

    SELECTION IS NOT A SECOND COPY of the platform rules — it calls `_pick_platform_asset`,
    the same function the github-asset path uses, so a URL naming a foreign os/arch is
    rejected identically. Only URLs that look like build artifacts are considered at all
    (`_DOWNLOADABLE`), which is what keeps a docs link out of the pool.

    KNOWN WEAKER THAN A RELEASE ASSET, and the caller must say so. A release asset is
    pinned to a tag; a README lives on the default branch and therefore tracks whatever
    the authors last shipped. So this returns the version token it found in the filename
    and refuses to guess: matching that against a requested pin is the caller's job (see
    resolve(), which will not offer a README asset for a pin the filename contradicts —
    the somalier 0.2.15→v0.3.3 failure class).

    Returns {found, url, version, candidates, reachable, probe_error}. A probe failure is
    UNCHECKED, never "there is no vendor build"."""
    out: dict[str, Any] = {"found": False, "url": "", "version": "", "candidates": [],
                           "reachable": None}
    if not repo or "/" not in repo:
        return out
    data, err = _fetch_json(f"https://api.github.com/repos/{repo}/readme", timeout)
    if err:
        out["probe_error"] = err
        return out
    if not isinstance(data, dict) or not data.get("content"):
        return out                                   # GitHub answered: no README
    try:
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    except Exception as e:                           # a README we cannot decode is UNCHECKED
        out["probe_error"] = f"readme undecodable: {type(e).__name__}: {e}"
        return out

    seen: set[str] = set()
    cands: list[str] = []
    for u in re.findall(r'https?://[^\s\)\]"\'<>]+', text):
        u = u.rstrip(".,;")
        # github.com/<repo>/releases/download/... is a RELEASE asset and belongs to the
        # tier above; if the release had one we would not be here. Anything else on
        # github.com is a doc/source link, never a vendor build.
        if u.startswith("https://github.com/") or not u.endswith(_DOWNLOADABLE):
            continue
        if u not in seen:
            seen.add(u)
            cands.append(u)
    out["candidates"] = cands
    pick = _pick_platform_asset(cands, target_os, target_arch)
    if not pick:
        return out
    out["url"] = pick
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", pick.rsplit("/", 1)[-1])
    out["version"] = m.group(1) if m else ""
    # "the README says" and "we checked" are different claims. A HEAD costs nothing next
    # to a multi-GB download and stops us advertising a link the vendor has since moved.
    ok, ok_err = _fetch_ok(pick, timeout)
    out["reachable"] = None if ok_err else ok
    if ok_err:
        out["reachable_probe_error"] = ok_err
    out["found"] = ok or bool(ok_err)   # unreachable-and-we-know-it is the one hard no
    return out


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


# ---------------------------------------------------------------------------
# BINARY-VERSION ANCHOR. A version-pinned request routed to the binary tier must
# resolve to THAT version's release asset — never the LATEST release's bytes under
# the requested version's name (the somalier 0.2.15→v0.3.3 bite: every integrity
# signal passes while the WRONG version ships, sha256 + SLSA + .sif and all). The
# probe_github fact the binary tier is built on reads releases/latest, so the
# pinned version is invisible to it. These helpers locate the pinned release
# DIRECTLY and drive both binary-tier availability and the emitted asset from it.
# ---------------------------------------------------------------------------

# mmseqs2 '15-6f452a0' — a bare version, a '-'/'_', then a git short hash. The separator is
# '-'/'_' ONLY, NEVER '.': a DOTTED suffix is a minor version, not a build hash, and since
# [0-9a-f] admits every decimal digit, '.' here would read '1.234567' (version 1.234567) as
# "version 1 + build-hash 234567" — a silent over-match shipping a different release's bytes.
_TAG_BUILDHASH_RE = re.compile(r"^(\d+)[-_][0-9a-f]{6,}$", re.I)   # e.g. mmseqs2 '15-6f452a0'


def _strip_tag_prefix(tag: str, tool: str = "") -> str:
    """Reduce a release tag to its version core: strip a leading 'v', a
    '{tool}-'/'{tool}_'/'{tool}.'(+optional 'v') prefix, or a generic
    'release-'/'rel-' prefix. 'Trinity-v2.15.1' → '2.15.1'; 'v0.2.15' → '0.2.15';
    '15-6f452a0' → '15-6f452a0' (no alpha prefix — the build-hash matcher handles
    it). Longest tool-prefix tried first so 'v' never wins over '{tool}-v'."""
    s = (tag or "").strip()
    low = s.lower()
    prefixes: list[str] = []
    t = (tool or "").lower().strip()
    if t:
        for sep in ("-", "_", "."):
            prefixes += [f"{t}{sep}v", f"{t}{sep}"]
    prefixes += ["release-", "release_", "rel-", "rel_", "v"]
    for p in prefixes:
        if low.startswith(p):
            return s[len(p):]
    return s


def _tag_matches(requested: str, tag: str, tool: str = "") -> bool:
    """Does release `tag` denote version `requested`? Layered, each rule SAFE
    against OVER-match (the false-green risk — requesting '1' must never match
    '1.2.3'; '2' must never match 'v2.28'; '0.2' must never match 'v0.2.15'):

      1. raw equality
      2. prefix-stripped equality  ('Trinity-v2.15.1' vs '2.15.1')
      3. PEP440 equality           ('1.10' vs 'v1.10.0' — the normalization PyPI
                                     and git tags disagree on)
      4. bare-number ↔ 'N-<githash>'  (mmseqs2 '15' vs '15-6f452a0', ONLY when the
                                     suffix is a sha-like hash — never a dotted minor)
      5. separator-normalized equality, but ONLY for the build-hash form where NEITHER
         side is PEP440-parseable ('15-6f452' vs requested '15.6f452') — a bare '15' can
         never collapse into a dotted tag it isn't. If EITHER side parses as a real
         version, rule 3's PEP440 verdict is authoritative and stands: '1.2-3' is
         1.2.post3, a DIFFERENT release than '1.2.3', so normalizing '-'→'.' must not forge
         a match rule 3 already refused."""
    if not requested or not tag:
        return False
    req = str(requested).strip()
    if tag == req:
        return True
    norm = _strip_tag_prefix(tag, tool)
    if norm == req:
        return True
    rk, nk = _version_key(req), _version_key(norm)
    if rk is not None and nk is not None and rk == nk:
        return True
    m = _TAG_BUILDHASH_RE.match(norm)
    if m and m.group(1) == req:
        return True
    # Rule 5 is the LAST resort and only for the genuinely un-PEP440-parseable build-hash
    # form. Gating on `rk is None and nk is None` is load-bearing: a request like '1.2.3'
    # parses, so it must NEVER reach the crude '-'/'_'→'.' normalization that would equate it
    # to '1.2-3' (=1.2.post3) or '1.2_3'. Both-None is exactly the '15.6f452'/'15-6f452' case
    # rule 4 can't reach (the hash is <6 chars).
    if (rk is None and nk is None
            and any(c in req for c in "-_.") and any(c in norm for c in "-_.")
            and norm.replace("-", ".").replace("_", ".")
                == req.replace("-", ".").replace("_", ".")):
        return True
    return False


def _release_for_version(repo: str, version: str, tool: str = "",
                         target_os: str = "linux", target_arch: str = "amd64",
                         timeout: int = 12) -> dict[str, Any]:
    """Locate the GitHub release for a PINNED `version` in `repo` and its
    ship-platform asset — the binary tier's honesty anchor. Answers "does THIS
    repo ship THIS version as a downloadable {os}/{arch} binary?" so a pin is
    never silently served the latest release's bytes. Returns {status, ...}:

      match      {tag, asset, asset_name} — resolves to a real platform binary;
                 PROCEED, pin this asset.
      no_asset   {tag, assets}           — the release exists but ships no
                 {os}/{arch} binary (source-only / foreign-OS / sidecars only);
                 cannot honor the pin on this tier — refuse, don't substitute.
      absent     {nearest, tags}         — no release matches across the fully-seen
                 list (the somalier==9.99 analog for the binary tier).
      incomplete {tags}                  — not on the newest 100 AND a direct tag
                 lookup missed; page 2+ MAY hold it. UNCHECKED — disclose, never
                 rendered as 'absent'.
      error      {error}                 — the release list never fetched
                 (rate-limit/timeout). UNCHECKED — never a refuse, never a
                 latest-substitution ((payload,error) seam).

    ONE GitHub call on the common path; a bounded ≤4 more only when the first page
    is full (100) and misses — the >100-release repo. Only ever called on the
    already-narrow version+github_repo path."""
    if not repo or "/" not in repo or not version:
        return {"status": "error", "error": "no repo/version"}
    data, err = _fetch_json(
        f"https://api.github.com/repos/{repo}/releases?per_page=100", timeout)
    if err:
        return {"status": "error", "error": err}
    if not isinstance(data, list):
        return {"status": "error", "error": "unexpected releases payload"}

    def _asset_result(rel: dict) -> Optional[dict]:
        assets = [a.get("browser_download_url") for a in (rel.get("assets") or [])
                  if a.get("browser_download_url")]
        pick = _pick_platform_asset(assets, target_os, target_arch)
        if pick:
            return {"status": "match", "tag": rel.get("tag_name") or "",
                    "asset": pick, "asset_name": pick.rsplit("/", 1)[-1]}
        return {"status": "no_asset", "tag": rel.get("tag_name") or "",
                "assets": [a.rsplit("/", 1)[-1] for a in assets][:10]}

    pairs = [(r.get("tag_name") or "", r) for r in data if isinstance(r, dict)]
    for tag, rel in pairs:
        if _tag_matches(version, tag, tool):
            return _asset_result(rel)
    # Not on page 1. A FULL page means older releases exist beyond it — try a
    # bounded direct tag lookup for the common forms before giving up (resolves
    # the >100-release exact-tag case without walking every page).
    if len(data) >= 100:
        cands = [version, f"v{version}"]
        if tool:
            cands += [f"{tool}-{version}", f"{tool}-v{version}"]
        for cand in cands:
            rel, terr = _fetch_json(
                f"https://api.github.com/repos/{repo}/releases/tags/{cand}", timeout)
            if terr:
                return {"status": "error", "error": terr}
            if isinstance(rel, dict) and rel.get("tag_name"):
                return _asset_result(rel)
        return {"status": "incomplete",
                "tags": [_strip_tag_prefix(t, tool) for t, _ in pairs][:8]}
    norms = [_strip_tag_prefix(t, tool) for t, _ in pairs if t]
    return {"status": "absent",
            "nearest": _nearest_versions(version, norms),
            "tags": norms[:8]}


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


# The tiers `_mute_registry_tiers` arbitrates between: exactly the ones whose probe RETURNS
# METADATA. Deliberately not every tier, and the exclusions are the whole correctness of the
# rule — muting a tier whose probe never collects a description would report a fact about OUR
# PROBE as a fact about the PACKAGE, which is a report lie whatever it does to the ranking.
#
#   binary/synthesis/source — a github tier carries no registry metadata at all.
#   bioconductor            — `probe_bioconductor` is an EXISTENCE check: `_fetch_ok` on the
#                             release HTML page, returning `{"available": bool}` and nothing
#                             else. Caught here before landing: with it in this tuple,
#                             `resolve('deseq2', language='r')` disqualified the bioconductor
#                             tier and said "the bioconductor entry states nothing about
#                             itself", about a probe that is not built to read a description.
#                             Harmless to the pick today (cran outranks bioconductor either
#                             way) and false on the page regardless. Teach the probe to
#                             capture a summary and it can rejoin this list.
_REGISTRY_TIERS = ("conda", "pip", "cran")


def _states_a_meaning(detail: dict) -> bool:
    """Does this registry entry say what it IS? A DEGENERATE STUB — a reserved name with no
    description, no homepage, no project URLs, no repo and no channel — states nothing.

    PyPI `seurat` is the measured case: version 0.0.2, `summary` empty, `home_page` empty,
    `project_urls` {}. Everything a reader could use to tell what it is, is absent.

    Deliberately a test of PRESENCE, not of content: a one-word summary is real and counts,
    and nothing here judges whether the meaning stated is the RIGHT one — that is the ride's
    call, made on `identity.self_description`, which this does not touch. `channel` counts,
    which is why a conda hit can never be muted by this: being carried on a curated
    bioinformatics channel is itself a statement (ECOSYSTEM, not identity — the distinction
    the corpus's `bioconda-membership-is-ecosystem-not-identity` row draws), and conda-first
    is a measured, load-bearing rule that a metadata sniff has no business overturning."""
    return bool((detail.get("summary") or "").strip()
                or (detail.get("home_page") or "").strip()
                or (detail.get("url") or "").strip()
                or (detail.get("bug_reports") or "").strip()
                or (detail.get("repo") or "").strip()
                or (detail.get("channel") or "").strip()
                or detail.get("project_urls"))


def _mute_registry_tiers(availability: dict[str, dict]) -> list[dict]:
    """Disqualify an available registry tier that states NO meaning when another one does.
    Mutates `availability`; returns what was muted, for disclosure. Never silent.

    A package that says nothing about itself is a name reservation, not a rival project, and
    it must lose BOTH ways — it may not win the ranking, and it may not be counted as the
    second meaning that makes a name ambiguous. `resolve('seurat')` failed both ways at once,
    which is why one gate was not enough: PyPI's blank `seurat` 0.0.2 first collided with
    CRAN's `Seurat` 5.5.1 ("Tools for Single Cell Genomics …") into a refusal that asked the
    user to choose between a described bio tool and a blank — a question with one possible
    answer — and when that ambiguity was lifted, the blank simply WON on tier order and
    emitted `install_pip_package(env, "seurat", version="0.0.2")`. Two mechanisms would have
    been two readings of one fact; this is the one, placed before ranking so both consumers
    read the same disqualification.

    Only fires when something else survives: if every registry hit is mute, they are all we
    have and muting them all would manufacture a dead end out of a thin one."""
    live = [t for t in _REGISTRY_TIERS if availability.get(t, {}).get("available")]
    speaking = [t for t in live if _states_a_meaning(availability[t])]
    if not speaking or len(speaking) == len(live):
        return []
    muted = []
    for t in live:
        if t in speaking:
            continue
        d = availability[t]
        muted.append({"tier": t, "latest": d.get("latest"),
                      "reason": (f"the {t} entry states nothing about itself — no summary, "
                                 f"homepage, project URL or repo — while "
                                 f"{'/'.join(speaking)} does. A name with no stated meaning "
                                 f"is a reservation, not a competing project, so it neither "
                                 f"wins the ranking nor makes this name ambiguous.")})
        availability[t] = {**d, "available": False, "degenerate_stub": True}
    return muted


def _is_ambiguous(availability: dict[str, dict], language: str) -> bool:
    """A bare tool name is dangerously ambiguous when it resolves in BOTH a
    Python registry (PyPI) and an R registry (CRAN) with no language hint — they
    are almost certainly different packages (e.g. PyPI `ape` ≠ CRAN's R `ape`).
    A language hint removes the ambiguity by construction.

    Reads `available` AFTER `_mute_registry_tiers`, so a stub that states no meaning is
    already gone — one disqualification, read here and by the ranking."""
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
# THING — and a wrong-but-working tool passes every gate and ships green:
# resolve_tool("cellranger") -> CRAN's spreadsheet-range parser loads fine, so it
# BUILDs, is VALIDATED_IN_IMAGE, is POLICY_CLEAN, and earns a digest + attestation.
# A fully green artifact containing the wrong software — the exact outcome this
# project exists to prevent.
#
# Identity cannot be machine-VERIFIED: "did you mean this one?" is a question about
# intent, and no invariant sees intent. It also should not be machine-GUESSED: a
# word-list that stamps `confirmed: true/false` is fake intelligence — worse at the
# judgment than the LLM in the loop, which already knows the context is
# bioinformatics and that ONT's `dorado` is a basecaller, not the PyPI astronomy
# package a bare name resolves to. So the resolver does NOT decide identity and does
# NOT gate on it. It surfaces the FACTS the judgment needs — the entry's OWN
# self-description, the repo it points at and what vouches for that, whether any
# description exists at all — and the ride (the LLM) decides PROCEED or ASK. When the
# ride lands on a tool it is probably right, and the honesty contract downstream is
# the net that catches a mechanical error; when it genuinely cannot tell even after
# investigating, it ASKs. (This replaced `assess_identity` + the 86-word
# `_DOMAIN_TERMS` list, deleted 2026-07-17 — the reverse-theme-park Phase 2: judgment
# moves to the ride, the resolver returns facts.)
# ---------------------------------------------------------------------------

#: Tiers whose probe READS a licence field off the entry. Membership is a fact about our
#: PROBES, not about the packages — which is the whole reason `bioconductor` is absent:
#: `probe_bioconductor` is an existence check (`_fetch_ok` on the release HTML page) and
#: collects nothing, so reporting "the bioconductor entry publishes no licence" would state
#: a fact about our probe as a fact about the package. Exactly the exclusion, and exactly
#: the reason, that `_REGISTRY_TIERS` records for the description sniff.
_LICENSE_READING_TIERS = ("conda", "pip", "cran")

#: Tiers that are a REGISTRY ENTRY at all. Everything else — binary, source, synthesis,
#: author_image, authors_recipe, r_github — is a repo or a release asset, and nothing on
#: that route publishes a licence for the bytes. That is not an oversight to fix by probing
#: harder; it is the shape of the thing, and it must be SAID rather than left as an empty
#: string that reads like a registry answered "none".
_REGISTRY_ENTRY_TIERS = _LICENSE_READING_TIERS + ("bioconductor",)


def license_evidence(chosen: str, license_text: str, license_source: str) -> str:
    """WHY the chosen entry's licence reads the way it does — five states. Pure.

    `""` answered five different questions with one value, and `license_disposition("")`
    rounded all five to `unrecognized`, which is a claim about a field nobody read. See
    `identity_facts` for the states and the measurement that forced them apart."""
    if license_text.strip():
        return "published"
    if license_source:
        # A source with no string: the entry published something we deliberately declined
        # to classify (a licence BODY rather than an identifier). Evidence exists; we are
        # saying we will not keyword-match it, which is a different fact from its absence.
        return "unclassified_text"
    if chosen in _LICENSE_READING_TIERS:
        return "not_published"
    if chosen in _REGISTRY_ENTRY_TIERS:
        return "not_probed"
    return "no_channel"


def identity_facts(tool: str, chosen: str, availability: dict,
                   github_repo: str = "", repo_ev: Optional[dict] = None,
                   discovery: Optional[dict] = None) -> dict:
    """The evidence an identity judgment needs — FACTS, never a verdict.

    "Is this the tool you MEANT?" is the ride's call to make (see the block comment
    above): the LLM knows the domain and reads these facts to decide PROCEED or ASK.
    This function's whole job is to put the evidence on the table, honestly:

      self_description — the chosen entry's OWN words. The single most useful signal:
                         CRAN cellranger says "Translate Spreadsheet Cell Ranges",
                         PyPI talos says "Hyperparameter Tuning for Keras". The ride
                         reads this and knows.
      has_description  — did the tier publish anything to read? MISSING evidence
                         (nothing to check) must never read as CONTRARY evidence (it
                         describes something else). The ride is told which it is, and
                         never asked to treat one as the other.
      repo/repo_source — which repo this points at, and what vouches for that link (a
                         curated conda `dev_url`, the caller's own `github_repo`, or
                         merely scraped from pip/cran metadata — a candidate, not an
                         anchor). Provenance is a fact; whether the repo IS the tool's
                         is the ride's judgment.
      repo_anchored    — did something trustworthy vouch for the repo (so the authors'
                         image/recipe was safe to auto-probe), or is it a scraped
                         candidate the ride must confirm first? A fact about how we got
                         the repo, not a claim about identity.
      channel          — bioconda/bioconductor membership is a bio-only-channel fact
                         the ride will weigh heavily; conda-forge/PyPI is not.
      license          — the registry's OWN published licence string, and our reading of
                         it (`license_disposition`). The only fact here that is not about
                         IDENTITY: bioconda's `novoalign` and `sentieon` are the RIGHT
                         tools, correctly described, on the right channel — every identity
                         signal says proceed, and they are commercial. Gatedness is a
                         property of the ARTIFACT, invisible to a question about which
                         package this is, so it needs its own fact or it has none.
      license_evidence — WHY the licence reads the way it does, in five states, because
                         `unrecognized` was carrying two opposite meanings and only one of
                         them is a finding. Measured 2026-08-06 across six probes: only
                         conda ever populated `license`, so every pip / cran / binary /
                         synthesis pick came back `unrecognized` — a claim ("we read a
                         licence and could not place it") about a field nothing had read.
                         And it is structural, not incidental: a tool is licence-gated
                         BECAUSE its bytes may not be redistributed, which is exactly why
                         it is not on bioconda, so the tier that carried the fact and the
                         tier gated tools live on were disjoint by construction. The
                         states — `published` · `unclassified_text` (prose, not an
                         identifier; we refuse to keyword-match a licence BODY) ·
                         `not_published` (the tier publishes the field and this entry left
                         it blank) · `not_probed` (OUR probe for this tier reads no licence
                         — bioconductor's is an existence check) · `no_channel` (the pick
                         is a repo/release, and nothing on that route publishes a licence
                         at all). Only `published` yields a disposition; the rest yield
                         `license_disposition: "unobserved"`, which is not one of the
                         leaf's three values and is not meant to be — the leaf answers
                         "what does this STRING permit", and with no string the honest
                         answer is that nobody said.

    No `confirmed` boolean, no note, no poisoning of `install_call`. The resolver
    states what is true and gets out of the way (Phase 2, 2026-07-17)."""
    detail = availability.get(chosen) or {}
    desc = (detail.get("summary") or "").strip()
    # A DISCOVERED tool has no registry entry, so no tier has a `summary` and this — the
    # signal the docstring above calls the single most useful one — came back empty on
    # exactly the route with the least corroboration. The repo's own blurb is the only
    # self-description that exists there, and we already fetched it. `or`, not override:
    # a real registry summary is the chosen ENTRY's words and outranks a repo blurb.
    if not desc and discovery:
        desc = (discovery.get("description") or "").strip()
    ev = repo_ev or {}
    repo = detail.get("repo") or ev.get("repo") or github_repo or ""
    repo_source = (detail.get("repo_source") or ev.get("source")
                   or ("user" if github_repo else ""))
    repo_anchored = (bool(github_repo) or bool(detail.get("repo_anchored"))
                     or bool(ev.get("anchored")))
    lic = str(detail.get("license") or "")
    evidence = license_evidence(chosen, lic, str(detail.get("license_source") or ""))
    return {
        "chosen_tier": chosen,
        "self_description": desc,
        "has_description": bool(desc),
        "repo": repo,
        "repo_source": repo_source,
        "repo_anchored": repo_anchored,
        "channel": detail.get("channel") or "",
        "license": lic,
        "license_evidence": evidence,
        "license_source": str(detail.get("license_source") or ""),
        # read through the core_data leaf, never re-spelled here: the CONTRACT reads the
        # same function over the licence observed in the shipped image, and a tool that
        # is commercial at freeze and free at resolve is the drift this codebase keeps
        # paying for. UNOBSERVED short-circuits BEFORE the leaf rather than reinterpreting
        # its answer: with no string there is nothing for it to read, and calling it on ""
        # to then relabel its `unrecognized` would be a second reading of the field.
        "license_disposition": (_core_data.license_disposition(lic)
                                if evidence == "published" else "unobserved"),
    }


#: `name_corroboration` statuses. Two are findings, three are states of the investigation,
#: and keeping them apart is the point — `unobserved` is not a quieter `no_same_name_repo`.
NAME_CORROBORATION_STATUSES = ("corroborated", "diverges", "no_same_name_repo",
                               "unobserved", "not_applicable")


def name_corroboration(tool: str, entry_repo: str, search: dict) -> dict[str, Any]:
    """Does anything ELSE on github own this name, and does the chosen entry point at it?
    Pure — `search` is a `probe_github_search` result, already fetched.

    THE DEFECT THIS EXISTS FOR (measured 2026-08-06, five tools back to back): github
    discovery ran ONLY at a registry dead-end, so the investigative effort was inversely
    proportional to how wrong the answer was about to be.

        annovar   no registry hit -> 5 repos investigated -> earned refusal, names the ask
        signalp   no registry hit -> 5 repos investigated -> earned refusal
        exomiser  no registry hit -> 5 repos investigated -> the RIGHT tool, adopted
        cellranger  CRAN hit      -> 0 repos, none run    -> clean install_call for a
                                                             SPREADSHEET-RANGE PARSER
        dorado      PyPI hit      -> 0 repos, none run    -> clean install_call for an
                                                             ASTRONOMY package

    A same-name registry entry SUPPRESSED the investigation that would have surfaced the
    real tool — and a squatter existing is the single strongest signal that a name is
    contested. So the search runs on a hit too. Same doctrine as `probe_package_family`
    one screen down ("a registry hit is not an answer"), on the github axis instead of the
    channel axis.

    NO JUDGEMENT HERE, and none is needed. The comparison is mechanical: which repos own
    the name EXACTLY, and is the chosen entry's own repo one of them?

      corroborated       every exact-name repo on github IS the repo this entry points at.
      diverges           at least one exact-name repo is NOT this entry's — the contested
                         name. Their own descriptions ride along; reading them against the
                         request is the ride's call (the 2026-08-06 ruling), and that
                         ruling was about what the resolver may CONCLUDE, never about how
                         hard it should LOOK.
      no_same_name_repo  the search ran and nothing on github owns the name exactly.
      unobserved         the search itself did not answer (github search is 10 req/min
                         unauthenticated). NOT a finding, and never rendered as one.
      not_applicable     nothing to corroborate — no pick, or the caller named the repo.

    `matched` is stated separately from the status because "the entry is one of THREE
    projects with this name" and "the entry names a repo none of them is" are different
    situations with the same verdict, and the message has to say which. PyPI's `dorado`
    points at Mucephie/DORADO — an exact-name repo — so a rule that stopped at "is the
    entry's repo among the hits" would have called the astronomy package CORROBORATED."""
    entry = (entry_repo or "").strip().strip("/").lower()
    # `matched: None` is STATED, not omitted: with no exact-name repo there is nothing to
    # have matched, and a reader must be able to tell that from `False` (there were, and
    # this entry is none of them) without knowing which statuses carry the key.
    base = {"entry_repo": entry_repo or "", "same_name_repos": [], "divergent_repos": [],
            "matched": None}
    if search.get("probe_error"):
        return {**base, "status": "unobserved", "probe_error": search["probe_error"],
                "detail": (f"the github name search for '{tool}' did not answer "
                           f"({search['probe_error']}) — nothing here says the name is "
                           f"uncontested; we did not get to look")}
    exact = [c for c in (search.get("candidates") or []) if c.get("exact_name_match")]
    divergent = [c for c in exact
                 if (c.get("repo") or "").strip().strip("/").lower() != entry]
    base["same_name_repos"] = exact
    base["divergent_repos"] = divergent
    if not exact:
        return {**base, "status": "no_same_name_repo",
                "detail": f"a github name search found no repo named exactly '{tool}'"}
    if not divergent:
        return {**base, "status": "corroborated", "matched": True,
                "detail": (f"the only repo on github named exactly '{tool}' is "
                           f"{exact[0]['repo']}, which is the repo this entry points at")}
    matched = len(divergent) < len(exact)
    return {**base, "status": "diverges", "matched": matched,
            "detail": (f"{len(exact)} repo(s) on github own the name '{tool}' exactly; "
                       + ("this entry points at one of them, and the other(s) are "
                          "different projects" if matched else
                          f"this entry points at {entry_repo}" if entry_repo else
                          "this entry names no repo at all"))}


#: The registry tiers a repo can be scraped FROM, best-vouched first. conda leads because
#: its entry is a curated recipe whose maintainer wrote the project link down by hand.
_REPO_SOURCES = ("conda", "pip", "cran")


#: How we came to be looking at a repo. `github_repo` alone cannot say: the auto-adopt
#: path re-enters `resolve` WITH the repo it just guessed, so inside that call a repo
#: found by a name search is byte-identical to one the user named — and "the user named
#: it" is the strongest anchor in this file. That laundering is what let the least
#: corroborated case produce the most confident output (Phase D, bcl2fastq).
REPO_FROM_USER = "user"                    # the caller stated it — a deliberate act
REPO_FROM_GITHUB_SEARCH = "github_search"  # we guessed it from a name search


def repo_evidence(availability: dict, github_repo: str = "",
                  discovery: Optional[dict] = None) -> dict[str, Any]:
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
        #
        # ...unless WE supplied it. `anchored` stays True either way, deliberately: it
        # governs whether the repo is worth handing to the author tiers, and a dominant
        # exact-name hit IS worth probing — refusing to look would cost auto-discovery
        # the whole point of [[feedback-prioritize-authors-own-env-recipe]]. What changes
        # is the SOURCE we report, because "user" is a claim about who vouched, and
        # nobody did. The disclosure hangs off this, not off routing.
        if discovery:
            return {"repo": github_repo.strip().strip("/"), "source": REPO_FROM_GITHUB_SEARCH,
                    "anchored": True,
                    "detail": "a github name search we ran ourselves — nobody vouched for it"}
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
                # A repo SCRAPED from pip/cran metadata is a CANDIDATE, never an anchor.
                # It is only as trustworthy as the entry it came from, and a bare-name
                # entry may be a different project entirely (PyPI 'dorado' is astronomy;
                # its metadata repo is Mucephie/DORADO, not ONT's). We used to anchor it
                # on a word-list hit in the summary; that word-list is gone (Phase 2 —
                # identity is the ride's judgment, not a regex). So a scraped repo is
                # surfaced for the ride to CONFIRM (the INVESTIGATE flow) and never
                # auto-handed to the author tiers, which outrank conda. The safe anchors
                # remain: a curated conda `dev_url` (above) and an explicit github_repo.
                return {"repo": f"{m.group(1)}/{re.sub(r'[.]git$', '', m.group(2))}",
                        "source": winner, "anchored": False,
                        "detail": (f"the {winner} entry's own metadata — a candidate repo "
                                   f"for the ride to confirm, not an anchor (a scraped repo "
                                   f"is no better vouched-for than a bare-name entry)")}
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


#: The refusal taxonomy. resolve() sets decision["refusal_reason"] to exactly one of these
#: whenever `chosen` is None — the machine-readable WHY behind an ask, so a caller (and the
#: intent corpus's earned-vs-lazy check) can tell an EARNED refusal from a lazy one. The three
#: EARNED reasons map to the user's rule (feedback-earn-the-refusal): investigate first, then
#: name (a) the user gave too little, or (b) we looked and came up empty / found contradicting
#: evidence. The fourth, `investigation_incomplete`, is the honest state the (a)/(b) dichotomy
#: omits: a probe we could NOT reach is not a probe that said no. Calling that
#: `investigation_empty` would be the same lie the `unchecked_tiers` disclosure exists to
#: prevent ("we did not find that there is nothing; we failed to look"), so it gets its own
#: value — and the corpus leaves those rows unassertable rather than demand a settled reason
#: for an unsettled investigation.
REFUSAL_REASONS = ("needs_user_input", "investigation_empty",
                   "investigation_contradicted", "investigation_incomplete",
                   # The first value that describes THE WORLD rather than our
                   # investigation. The other four all say something about how far WE got
                   # looking; this one says what the artifact IS. That asymmetry was a
                   # named gap (G3 phase 1): a resolver whose whole refusal vocabulary is
                   # self-referential can report a completed, uncontradicted, fully-probed
                   # investigation and still have no way to say "the thing you named is
                   # not the kind of thing that installs".
                   "artifact_is_a_workflow")


def _classify_refusal(decision: dict) -> str:
    """Name WHY resolve() is refusing (chosen is None), from the structured signals the
    resolve() body has already recorded on `decision`. Pure; reads only those keys.

    Precedence is deliberate and load-bearing:

    1. UNREACHABLE first. A tier whose probe never answered (`unchecked_tiers`) or a github
       fallback search that itself failed (`discovery_error`) means the investigation did not
       COMPLETE — no earned verdict is available. This outranks everything below because a
       contradiction found on tier X does not make an UNREACHED tier Y known-absent; the
       answer is still unsettled. It matches the resolve() body, which stamps exactly this
       case 'NOT A REFUSAL — UNRESOLVED' rather than 'nothing found'.
    2. ARTIFACT_IS_A_WORKFLOW. The repo declares itself a Nextflow/Snakemake workflow and
       carries nothing installable (`workflow_repo`). Ranked below UNREACHABLE for the same
       reason everything else is — a pipeline repo can also be packaged, so an unreached
       registry probe still leaves that open — but above the three below, because those all
       describe how far we got and this one describes what the thing IS. Telling a caller
       "we found nothing" about a repo whose own manifest names it a pipeline is a true
       sentence that teaches the wrong lesson.
    3. CONTRADICTED. A same-name registry hit was disqualified because its metadata does not
       reference the caller's authoritative repo (`cross_namespace_collisions`) — positive
       contrary evidence, stronger than a bare empty.
    4. NEEDS_USER_INPUT. Discovery surfaced candidate repos but none dominant enough to
       auto-adopt (`discovered_repos`); the collision risk means we refuse to guess and ask
       the user to confirm one.
    5. EMPTY. Every reachable tier answered AND discovery reached and found nothing — the one
       genuine dead end.
    """
    if (decision.get("unchecked_tiers") or decision.get("discovery_error")
            or decision.get("binary_version_unchecked")):
        # ...including a version pin the registry tier lacks whose pin-honoring binary tier
        # we could NOT reach (rate-limit / >100-release pagination): the version might exist
        # there, so the investigation did not COMPLETE — not a settled contradiction.
        return "investigation_incomplete"
    if decision.get("workflow_repo"):
        return "artifact_is_a_workflow"
    if (decision.get("cross_namespace_collisions") or decision.get("version_absent")
            or decision.get("declared_repo_contradiction")):
        # contrary evidence: a same-name hit that isn't the tool, a requested version the
        # chosen tier verifiably does not carry, or a user-named repo that is a distinct live
        # lineage from the one the registry builds — the request conflicts with reality.
        return "investigation_contradicted"
    if decision.get("discovered_repos"):
        return "needs_user_input"
    if decision.get("ambiguous"):
        # a bare name that resolves on BOTH PyPI and CRAN — two ecosystems, no basis to
        # choose. The user must name one (language=/prefer=); same class as discovered_repos.
        return "needs_user_input"
    return "investigation_empty"


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
        # "pass a github_repo to unlock synthesis" is only true advice when the repo tiers
        # were never walked. A workflow repo reaches this branch with synthesis PRESENT and
        # deliberately unavailable, so the stock sentence would invite the caller to re-run
        # the one route the decision just ruled out — advice aimed at the wrong problem,
        # which the rate-limit hint above already establishes is worse than none.
        _wf = (availability.get("synthesis") or {}).get("workflow_repo")
        rationale = (
            "no INSTALL tier applies: the repo is a workflow, not a tool — synthesis and "
            "source were withheld deliberately, not found missing"
            if _wf else
            "no registry/repo tier found — pass a github_repo (or repo/archive URL) "
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


def registry_name(detail: dict, tool: str) -> str:
    """THE one reading of "what does this registry ACTUALLY call this package?".

    A registry's name for a package is frequently not the name a user types:
      * bioconda ships DESeq2 as `bioconductor-deseq2` — there is no bare `deseq2`
      * conda-forge ships R packages as `r-{name}`
      * CRAN knows Seurat as `Seurat`, and `install.packages()` is case-SENSITIVE

    Three keys, one question. They were spelled separately at three call sites and
    the third — the DISCLOSURE that tells the user the name changed — read only
    `bioc_spec`, so `resolve('Seurat', language='r')` emitted an install of
    `r-Seurat` with no mention that the name had been rewritten at all. That is the
    `feedback-reports-never-lie` failure (requested-vs-installed at a glance) and
    the repo's signature defect (one truth in N places, drifting in N−1) in one
    line. Add a fourth registry convention HERE, never at a call site."""
    return (detail.get("bioc_spec") or detail.get("r_spec")
            or detail.get("resolved_name") or tool)


def registry_name_note(detail: dict, tool: str) -> str:
    """The disclosure for `registry_name`, or "" when the name was not rewritten.
    Always emitted alongside a rewritten name — a silent rewrite is the thing this
    pair exists to prevent."""
    name = registry_name(detail, tool)
    if name == tool:
        return ""
    return (f"  # NAME MAPPED: you asked for '{tool}'; this registry's package is "
            f"'{name}' — verify that is the tool you meant")


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
        base = registry_name(detail, tool)
        spec = f"{base}={v}" if v else base
        return f'install_conda_packages(env, [{{"spec": "{spec}", "channel": "{detail.get("channel","bioconda")}"}}])'
    if tier == "pip":
        return f'install_pip_package(env, "{tool}"' + (f', version="{v}")' if v else ")")
    if tier == "cran":
        # `registry_name`, never `tool`. crandb is case-SENSITIVE and so is
        # `install.packages()`, and `probe_cran` retries a clean miss against mechanical
        # capitalizations — so a query of `seurat` legitimately resolves CRAN's `Seurat` and
        # carries `resolved_name` to say so. This branch read `tool` anyway and emitted
        # `install_r_package(env, "seurat", source="cran")`, which does not install. The bug
        # was UNREACHABLE until the degenerate-stub disqualification let `seurat` reach the
        # cran tier at all: the case-retry landed the right package and the call spelled it
        # wrong, trading a wrong tool for a broken one — exactly what `probe_cran`'s own
        # docstring warns about, two functions away.
        return f'install_r_package(env, "{registry_name(detail, tool)}", source="cran")'
    if tier == "bioconductor":
        return f'install_r_package(env, "{registry_name(detail, tool)}", source="bioconductor")'
    if tier == "r_github":
        return f'install_r_package(env, "{tool}", source="github:{github_repo}")'
    if tier == "binary":
        # Prefer the version-anchored asset (the PINNED release's linux binary); else a
        # platform-selected asset (never a raw assets[0], which can be a .sha256 sidecar or
        # a foreign-OS build); else, when the version lookup was UNCHECKED, a loud
        # verify-manually placeholder rather than latest's bytes masquerading as version v.
        asset = detail.get("resolved_asset")
        if not asset and version and detail.get("binary_version_unverified"):
            asset = (f"<{tool} {version} linux asset — UNVERIFIED: enumerate "
                     f"https://github.com/{github_repo}/releases; do NOT use latest>")
        if not asset:
            asset = (_pick_platform_asset(detail.get("assets") or [])
                     # ...then the vendor's own CDN, named in their README. Last among the
                     # real answers because a release asset is tag-pinned and this is not.
                     or detail.get("vendor_asset")
                     or (detail.get("assets") or ["<release-asset-url>"])[0])
        return f'install_release_binary(env, "{tool}", url="{asset}", sha256="<published>")'
    if tier == "synthesis":
        url = f"https://github.com/{github_repo}" if github_repo else "<repo-or-archive-url>"
        return (f'synth_fetch("{url}")  # read its build files, then '
                f'synth_build(env, "{url}", "{tool}", commit, commands=[...], evidence="...")')
    if tier == "source":
        return f'install_git_repo(env, "https://github.com/{github_repo}", "{tool}", ref="<tag/commit>")'
    return "# no automatable tier — author a manual path (and stage_authored_artifact for any scripts)"


def pullable_image(availability: dict[str, dict], tool: str,
                   version: str = "", timeout: int = 12,
                   chosen: Optional[str] = None) -> dict[str, Any]:
    """The unified "is there a pre-built image/container we can PULL (not build)?" answer.

    Spans the two TRUSTED image sources, in preference order:
      1. the authors' OWN published image — `availability['author_image']` (ghcr, adopt by
         digest; provenance = the authors' own namespace, and only for an ANCHORED repo)
      2. a BioContainer — quay.io/biocontainers, the bioconda auto-build (provenance = the
         curated biocontainers namespace; nf-core pins these same images per process)

    Returns {found, source, image, image_by_digest, digest, adopt_call, provenance} — a FACT
    on the decision, unifying "is there a pullable image?" in one place. The authors'-image
    case is ALSO a `chosen` tier (`author_image`) because adopting their whole env is a
    distinct route; a bioconda tool's biocontainer is the conda tier's DELIVERY (freeze
    adopts it by digest for a pure-conda env), so it ENRICHES the conda pick rather than
    replacing it. Either way the agent sees the pull-don't-build shortcut up front.

    The biocontainer is surfaced ONLY when `chosen == "conda"`: it is the conda pick's image,
    and since the bioconductor->conda fold can make `availability["conda"]` available even
    when conda is NOT the pick (e.g. `prefer='pip'` forced pip), advertising it otherwise would
    name a DIFFERENT package than the install_call installs — a `bioconductor-{tool}` adopt_call
    beside an `install_pip_package({tool})` (which for `edger` is a browser-redirect utility).

    PROVENANCE GUARDRAIL: both sources are trusted by construction — the authors' own repo
    namespace (anchored) and the curated biocontainers registry. We NEVER surface a random
    third party's image that merely claims to be the tool (the cellranger-third-party-image
    trap). And a pulled image is still VALIDATED_IN_IMAGE at freeze — pull, then prove it runs.
    """
    def _adopt_call(img: str) -> str:
        return (f'freeze_from_image(image="{img}", name="{tool}", '
                f'tools=[{{"name": "{tool}", "evidence": "<cmd that RUNS {tool} in-image>"}}], '
                f'build_method="adopt-image")')

    ai = availability.get("author_image") or {}
    if ai.get("available"):
        ref = ai.get("ref") or ai.get("image") or ""
        return {"found": True, "source": "author_image", "image": ref or None,
                "image_by_digest": None,   # the ghcr probe confirms pullability, not a digest
                "digest": None, "adopt_call": _adopt_call(ref or "<author-image-ref>"),
                "provenance": "the authors' own published image (ghcr, anchored repo)"}

    conda = availability.get("conda") or {}
    if chosen == "conda" and conda.get("available") and _resolve_biocontainer is not None:
        spec = registry_name(conda, tool)
        try:
            bc = _resolve_biocontainer(
                [(spec, version or conda.get("latest") or None)], timeout=timeout)
        except Exception:
            bc = {"found": False}
        if bc.get("found") and bc.get("image_by_digest"):
            return {"found": True, "source": "biocontainer",
                    "image": bc.get("image"), "image_by_digest": bc.get("image_by_digest"),
                    "digest": bc.get("digest"), "adopt_call": _adopt_call(bc["image_by_digest"]),
                    "provenance": "quay.io/biocontainers (the bioconda auto-build)"}
    return {"found": False}


def _normalize_github_repo(github_repo: str) -> str:
    """Any way a human holds a GitHub repo → the `owner/repo` this module expects.

    Accepts `owner/repo`, `https://github.com/owner/repo`, the `.git` clone URL,
    `git@github.com:owner/repo.git`, a trailing path (`/tree/main`, `/releases`),
    and a trailing slash. Anything that is not recognisably a GitHub reference is
    returned UNCHANGED — this normalizes a spelling, it does not invent a repo, and
    a caller who passed something else entirely must still see their own input in
    the refusal that follows.
    """
    raw = (github_repo or "").strip()
    if not raw:
        return ""
    s = raw
    for prefix in ("git+https://", "git+ssh://", "https://", "http://", "ssh://", "git@"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    # A github HOST must be present before anything is stripped off the front. Without
    # this gate a gitlab/bitbucket URL loses its scheme and is then sliced to its first
    # two segments — `https://gitlab.com/a/b` -> `gitlab.com/a`, a repo that does not
    # exist, invented by a normalizer. Anything not recognisably GitHub comes back
    # verbatim so the caller sees their own input in whatever refusal follows.
    host = next((h for h in ("www.github.com/", "github.com/",
                             "www.github.com:", "github.com:")
                 if s.lower().startswith(h)), "")
    if not host:
        return raw
    s = s[len(host):].strip("/")
    if s.lower().endswith(".git"):
        s = s[:-4]
    parts = [p for p in s.split("/") if p]
    # `owner/repo` and nothing more — a trailing /tree/main or /releases/tag/v1 is a
    # location inside the repo, not part of its name.
    return "/".join(parts[:2]) if len(parts) >= 2 else raw


def resolve(
    tool: str,
    version: str = "",
    github_repo: str = "",
    prefer: Optional[str] = None,
    language: str = "",
    timeout: int = 12,
    user_said: str = "",
    *,
    repo_discovery: Optional[dict] = None,
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

    `user_said` is THE USER'S OWN WORD when `tool` is the caller's NORMALIZATION of it —
    'gatk' alongside `tool='gatk4'`. It turns a silent substitution into a checked one:
    the two must belong to the same package family, and if they do not, this REFUSES
    (`refusal_reason='investigation_contradicted'`) instead of installing something the
    user never named. Passing it is how the ride's world knowledge gets USED without being
    TRUSTED — the same posture every other agent-authored field in this codebase gets. Omit
    it when `tool` is already the user's word."""
    language = (language or "").lower().strip()
    # F8. NORMALIZED HERE, ONCE, at the input boundary — before any reader sees it.
    #
    # `github_repo` is documented as 'owner/repo', and a pasted URL is how a human
    # actually holds a repo. A URL contains "/", so it flowed through the
    # owner/repo branch verbatim and downstream probes built
    # `api.github.com/repos/https://github.com/owner/repo` -> 404 -> `repo_exists:
    # false` for a repo that exists, refused as `investigation_empty` ("we looked and
    # found nothing") when the truth was "we mangled the identifier". Measured on the
    # same repo in one session: bare `owner/repo` resolved a tag and emitted an
    # install_call; the URL form refused.
    #
    # Worse, the two forms disagreed WITHIN a single call: `author_image` and
    # `authors_recipe` accepted the URL happily while binary/synthesis/source reported
    # the repo absent — two readings of one input, the disease
    # `test_one_reading_per_field.py` exists to prevent, here on an argument rather
    # than a record field. Normalizing at the boundary is what makes that structurally
    # impossible rather than merely fixed in the three places that were wrong.
    github_repo = _normalize_github_repo(github_repo)
    availability: dict[str, dict] = {}
    if language == "r":
        rconda = probe_conda(f"r-{tool}", timeout)
        if rconda.get("available"):
            # LOWERCASE, like the bioc producer below. The anaconda API is case-TOLERANT
            # (`r-Seurat` and `r-seurat` both answer 5.5.1), so the probe passes either way and
            # the bug hides — but `spec` flows verbatim into `conda install` (env_manager.py),
            # where channel package names are canonically lowercase. Without this,
            # `resolve('Seurat', language='r')` emitted `r-Seurat=5.5.1`: the CALLER's spelling
            # presented as the channel's.
            rconda["r_spec"] = f"r-{tool.lower()}"
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

    # BIOCONDUCTOR. The field's core R stats tools — DESeq2, edgeR, limma — live ONLY on
    # bioconda under a `bioconductor-{name}` prefix: never the bare name, and not on CRAN.
    # Without this fallback a bare `deseq2` misses every registry and slides to github
    # discovery/synthesis, and `edger` resolves to a same-name junk PyPI package ("Redirect
    # Microsoft Edge") — a clean-green install of the WRONG tool. Fold the hit INTO the conda
    # tier (it IS an `install_conda_packages` from bioconda) so it inherits conda's top rank
    # AND conda's standing as the pip/cran ambiguity disambiguator: an ambiguous name that
    # resolved to conda PROCEEDS (see rank_decision + `if ambiguous and chosen != "conda"`),
    # so `limma` resolves instead of refusing. It is a real CHECKED probe (bioconductor-samtools
    # 404s), never a guess. Fires only when the bare-name conda probe already missed, and never
    # under an explicit `language='python'` (that hint means "the PyPI package", not an R tool).
    # It DOES fire under `language='r'` (where the r-branch probed conda as `r-{tool}` and missed):
    # that is DELIBERATE — the pinned bioconda `bioconductor-{name}` beats the dedicated
    # `bioconductor` tier's unpinned BiocManager build, so conda-first wins there too.
    if language != "python" and not availability.get("conda", {}).get("available"):
        bioc = probe_conda(f"bioconductor-{tool}", timeout)
        if bioc.get("available"):
            bioc["bioc_spec"] = f"bioconductor-{tool.lower()}"
            availability["conda"] = bioc
        elif bioc.get("probe_error") and not availability.get("conda", {}).get("probe_error"):
            # ABSENT ≠ UNCHECKED (the whole point of test_probe_honesty.py). The
            # bioconductor-variant probe ERRORED — a transient anaconda 403, not "answered
            # no" — so bioconda has NOT been cleanly ruled out. Carry the error onto the conda
            # tier so `unchecked_tiers` DISCLOSES it. This does not change the pick (a same-name
            # junk PyPI package like edgeR's `edger` — a browser-redirect utility — may still be
            # the only available tier and win), but it stops that win from being SILENT: conda
            # is reported unchecked-and-ranked-above, not cleanly ruled out, so the install_call
            # carries the UNCHECKED-TIER disclosure instead of a bare `install_pip_package`.
            # Measured live: one transient blip on this exact probe routed `edger` to pip.
            availability["conda"] = {**availability.get("conda", {}),
                                     "probe_error": bioc["probe_error"]}

    binary_ver: Optional[dict] = None   # BINARY-VERSION ANCHOR (set below when version + repo)
    if github_repo:
        gh = probe_github(github_repo, timeout)
        availability["binary"]    = {"available": gh["has_release_assets"], **gh}
        # THE VENDOR-CDN DECISION. `probe_github` OBSERVED the README build; whether it
        # may be offered is a ranking question and belongs here (this file's own split:
        # "the probes are observations, the ranking is the decision").
        #
        # A README tracks the DEFAULT BRANCH, not a tag, so if a pin was requested and the
        # filename does not carry it, this URL is latest's bytes wearing the requested
        # version's name — the somalier 0.2.15→v0.3.3 class, which shipped fully green.
        # Record the candidate and WHY it was not offered; never quietly serve it.
        if gh.get("vendor_asset"):
            # `_version_present`, not a string ==: it already normalises PEP440 so a pin
            # of `2.1` matches a filename's `2.1.0`, with the exact-string fallback for
            # what neither side can parse. A second comparison rule here is how this
            # file's defects start.
            pin_ok = (not version) or _version_present(version,
                                                       [gh.get("vendor_asset_version") or ""])
            availability["binary"] = {
                **availability["binary"],
                "available": bool(pin_ok),
                **({} if pin_ok else {"vendor_asset_pin_mismatch": (
                    f"the vendor build named in the README is "
                    f"{gh.get('vendor_asset_version') or 'un-versioned'}, not the requested "
                    f"{version} — a README tracks the default branch, not a tag, so this URL "
                    f"cannot honor the pin. Enumerate the vendor's archive for {version}.")}),
            }
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
        # BINARY-VERSION ANCHOR. probe_github read releases/LATEST, so a version pin is
        # invisible to it — the binary tier would ship latest's bytes under the requested
        # version's name (somalier 0.2.15→v0.3.3, fully green). When a version is pinned,
        # decide the binary tier from the PINNED release instead of latest: available (with
        # the exact asset) IFF that version ships a linux binary here; NOT available if it
        # doesn't (so the tier can't win and then substitute); latest-derived but FLAGGED
        # when the lookup was UNCHECKED (so the emitted call never claims latest IS version X).
        if version and gh.get("repo_exists") and not gh.get("probe_error"):
            binary_ver = _release_for_version(github_repo, version, tool, timeout=timeout)
            if binary_ver["status"] == "match":
                availability["binary"] = {
                    **availability["binary"], "available": True,
                    "resolved_asset": binary_ver["asset"],
                    "resolved_tag": binary_ver["tag"], "binary_version": binary_ver}
            elif binary_ver["status"] in ("absent", "no_asset"):
                # These two were one branch, and collapsing them is what made the
                # vendor-CDN fallback unreachable for a PINNED version. `no_asset` is
                # exactly the vendor shape — the tag EXISTS, github just carries no bytes
                # for it — so a README build already version-matched above is a real
                # answer and must survive. `absent` is a different fact (no such tag) and
                # no vendor URL rescues it: dorado==2.0.0 must still refuse.
                vendor_holds = (binary_ver["status"] == "no_asset"
                                and availability["binary"].get("asset_source") == "readme"
                                and availability["binary"].get("available"))
                availability["binary"] = {
                    **availability["binary"], "available": bool(vendor_holds),
                    "binary_version": binary_ver}
            else:   # error / incomplete → UNCHECKED: keep the latest-derived availability
                    # but flag it, so _install_call emits a verify-manually placeholder rather
                    # than latest's asset masquerading as the pinned version.
                availability["binary"] = {
                    **availability["binary"], "binary_version_unverified": binary_ver}

    # AUTHORS' OWN RESOURCES (the reliability gate). Find the tool's repo — explicit or
    # extracted from registry metadata — and ask: does the tool publish an image, and
    # does its own recipe install pieces a conda/pip reconstruction would DROP? If so,
    # the authors' path outranks conda (build what they build, don't reconstruct). If
    # the recipe is trivially registry-equivalent, the gate stays SHUT and conda wins.
    # Best-effort: any probe failure simply leaves the author tiers unavailable, so the
    # registry route is unaffected. This is what makes the agent 'thread the needle'
    # automatically on tools like Talos (PyPI hit, but a Dockerfile compiling a fork).
    ev = repo_evidence(availability, github_repo, repo_discovery)
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
    authors_assessment: dict = {}
    if eff_repo and "/" in eff_repo and probe_authors_sources is not None:
        try:
            owner, rp = eff_repo.split("/", 1)
            assessment = probe_authors_sources(tool, owner=owner, repo=rp, timeout=timeout)
            authors_assessment = assessment or {}
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

    # A WORKFLOW REPO HAS NO BUILD TO SYNTHESIZE.
    #
    # `synthesis` and `source` are the only two tiers whose availability test is, verbatim,
    # "does the repo exist" (see where they are set above). For everything with a real build
    # that is a fine default — the agent reads the repo's own files and grounds an install in
    # them. For a repo whose own manifest says it is a PIPELINE it is a category error, and
    # it is not a quiet one: `synth_fetch("https://github.com/nf-core/rnaseq")` returns
    # `outcome: proven` over a 41k-char corpus of nine CI workflows (top-ranked "most
    # authoritative recipe": the check that PRs target `dev`), a `pyproject.toml` holding
    # `[tool.black] line-length = 120`, and two READMEs. main.nf and nextflow.config are not
    # in it. The agent is handed the pipeline's release-announcement tweet and asked how the
    # pipeline installs.
    #
    # `workflow_only` is read, never re-derived — assess_tool_sources computes it beside the
    # manifest it depends on, so the "is this installable" judgement has exactly one author.
    if authors_assessment.get("workflow_only"):
        wf_manifest = authors_assessment.get("workflow_manifest") or {}
        for _t in ("synthesis", "source"):
            if _t in availability:
                availability[_t] = {**availability[_t], "available": False,
                                    "workflow_repo": wf_manifest}

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

    # FORK-ANCHOR / DECLARED-REPO RECONCILIATION. A user-supplied github_repo is
    # authoritative intent. If a registry tier would build a DIFFERENT project than the one
    # named, it must not win silently. Three outcomes, split by the fork edge:
    #   • a DIVERGENT FORK of a packaged tool → the user opted into the fork's changes (the
    #     Talos bcftools-csq bite), so disqualify the registry tiers and route to the fork's
    #     own build (synthesis/source);
    #   • a DISTINCT same-name lineage (not a fork) → a contradiction we REFUSE and ask about;
    #   • CONVERGENCE (same repo after a 301-follow) → inert, conda wins as today.
    # UNCHECKED (a probe that never answered) NEVER refuses and NEVER reroutes: it discloses
    # and keeps conda, because rendering "we could not look" as "the repos conflict" is the
    # Rule-2 lie the (payload,error) seam exists to prevent. Costs at most two GitHub calls
    # (canon(conda_repo) + one compare), and only on the already-narrow github_repo path.
    fork_divergence: dict = {}
    declared_repo_contradiction: dict = {}
    fork_disclosure = ""
    if github_repo and gh.get("repo_exists") and not gh.get("probe_error"):
        cu = (gh.get("full_name") or github_repo).strip().strip("/").lower()
        conda_av = availability.get("conda", {})
        r_conda = conda_av.get("repo", "") if conda_av.get("available") else ""
        cc, cc_status = _canon_repo(r_conda, timeout) if r_conda else ("", "none")
        if r_conda and cc_status == "ok" and cu == cc:
            pass                                       # CONVERGE — conda builds this repo.
        elif gh.get("is_fork"):
            # Reroute ONLY on a CONFIRMED divergence from the fork's own parent (independent of
            # whether conda declared a repo — a divergent fork is a hard pin either way).
            parent = gh.get("parent") or gh.get("upstream")
            diverged, div_err = _fork_diverges(
                parent, gh.get("full_name") or github_repo, gh.get("default_branch"), timeout)
            if diverged is True:
                fork_divergence = {"fork": cu, "parent": parent, "status": "diverged",
                                   "registry_repo": (cc if cc_status == "ok" else r_conda) or None}
                for t in ("conda", "pip", "cran", "bioconductor"):
                    if availability.get(t, {}).get("available"):
                        availability[t] = {**availability[t], "available": False,
                                           "fork_divergence_disqualified": True}
            elif div_err:
                fork_disclosure = (
                    f"you named a fork ({cu}); its divergence from {parent or 'its parent'} could "
                    f"not be verified ({div_err}) — keeping conda; re-run, or pass "
                    f"prefer='synthesis' if the fork's changes are the point.")
            # diverged is False → a pristine fork adds nothing; conda's build is equivalent.
        elif r_conda and cc_status == "ok" and cu != cc:
            # NOT a fork, both repos live, canonicals differ → distinct same-name lineages that
            # nothing reconciles. Refuse and ASK (handled by the nuller after rank_decision).
            declared_repo_contradiction = {"user_repo": cu, "registry_repo": cc,
                                           "registry_tier": "conda"}
        elif r_conda and cc_status in ("error", "absent") and cu != (cc or ""):
            fork_disclosure = (
                f"the conda recipe names {r_conda} but you named {cu}; whether they are the same "
                f"project could not be verified ({cc_status}) — keeping conda; re-run or pass "
                f"prefer='synthesis' to build the repo you named.")

    # DEGENERATE STUBS. A registry hit that says nothing about itself loses to one that does
    # — for the ranking AND for the ambiguity test below, which reads the same `available`.
    # Runs after every probe and after the cross-namespace/fork guards, so it arbitrates over
    # the tiers that actually survived to be ranked.
    muted_stubs = _mute_registry_tiers(availability)

    decision = rank_decision(availability, prefer=prefer)
    if muted_stubs:
        decision["degenerate_stubs"] = muted_stubs
        decision["rationale"] = (
            "; ".join(f"{m['tier'].upper()} '{tool}' ({m['latest']}) DISQUALIFIED: {m['reason']}"
                      for m in muted_stubs) + " || " + decision.get("rationale", ""))

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
            # A confident auto-adopt candidate: an EXACT name match with NOTHING ELSE
            # claiming the name. Else present candidates for a human/agent to confirm (the
            # GAB-collision guard: a same-name repo can still be the wrong project).
            #
            # POPULARITY IS NOT IDENTITY, AND IT NEVER BREAKS A TIE HERE. A third disjunct
            # used to read `rec["stars"] >= 5 * cands[1]["stars"]` — adopt on star dominance
            # when the runner-up ALSO matches the name exactly. That is the resolver settling
            # a question about which PROJECT the user meant with a measure of how many people
            # starred a repo, and the disclosure it then printed said so in its own words
            # ("Stars measure popularity, not identity") while the pick rested on exactly
            # that. Measured: `resolve('talos', language='r')` adopted siderolabs/talos, a
            # Kubernetes Linux distribution, at 10893★ over autonomio/talos at 1636★ — 6.6:1,
            # clearing the 5:1 bar — when the tool meant is populationgenomics/talos, a
            # rare-disease variant-reanalysis pipeline that is this repo's own authors-recipe
            # exemplar. Star rank in a bio-tool search is if anything ANTI-correlated with the
            # answer: general-purpose software outstars every domain tool. Worse, adoption
            # then re-enters resolve() with the repo as an anchor, so the author tiers grant
            # it a free identity pass and a pure popularity guess ships as an anchored repo.
            #
            # What survives is not a weaker version of the same guess: `len(cands) == 1 or not
            # cands[1]["exact_name_match"]` means NOTHING ELSE ON GITHUB CLAIMS THIS NAME, so
            # there is no choice being made silently. The `stars >= 10` floor stays as a
            # liveness sniff on that sole candidate, not as a ranking. When two projects do
            # both own the name, that is a question, and asking it is the point — the ride
            # gets `discovered_repos` with each repo's own description and re-calls with
            # `github_repo=`, which is where the judgment belongs.
            auto_adoptable = bool(
                rec["exact_name_match"] and rec["stars"] >= 10 and (
                    len(cands) == 1 or not cands[1]["exact_name_match"]))
            if auto_adoptable:
                # AUTONOMY: it's confidently the tool → don't make a human re-run.
                # Re-resolve WITH the discovered repo so the caller gets a COMPLETE,
                # executable plan (synthesis/source/binary + install_call), not a
                # "re-run please" stub. The honesty contract validates the actual
                # build, so a rare wrong pick fails SAFE rather than shipping silently.
                # `repo_discovery` is what stops the re-entry laundering our own guess
                # into a caller-supplied anchor. Without it the inner call sees only
                # `github_repo=` and reports `repo_source: "user"` about a repo the user
                # never named — and suppresses the not-assessed disclaimer on the way.
                # It carries the whole candidate, not a provenance flag, so the repo's
                # OWN description reaches the disclosure and `identity.self_description`
                # instead of being thrown away here and asked for again downstream.
                auto = resolve(tool, version=version, prefer=prefer, language=language,
                               github_repo=rec["repo"], timeout=timeout,
                               repo_discovery=rec)
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
                # NOT "to install via synthesis". Nothing has read that repo yet — the
                # authors' assessment only runs once a repo is ANCHORED — so naming the
                # landing tier here promises an outcome we have not investigated. For a
                # pipeline repo the honest landing is a refusal, and this sentence used to
                # advertise the exact route that refusal exists to withhold.
                f"discovered_repos) to route against that repo.")

    ambiguous = _is_ambiguous(availability, language)
    chosen = decision["chosen"]
    # GENUINE AMBIGUITY IS A REFUSAL, not a quiet guess. A bare name that resolves on BOTH
    # PyPI and CRAN is two different packages in two ecosystems (PyPI `ape` build-system ≠
    # CRAN `ape` phylogenetics). With no language/prefer hint and no conda pick to
    # disambiguate in-context, tipping to pip is exactly the confident-wrong-ecosystem pick
    # that ships the wrong tool green — so null it and ASK (refusal_reason=needs_user_input;
    # the AMBIGUOUS rationale below already says how). conda winning IS a disambiguator (the
    # bioinformatics-channel package), so only a pip/cran pick is nulled; `available` keeps
    # BOTH tiers so the caller sees the two options it must choose between.
    if ambiguous and chosen == "conda":
        # RESOLVED, SO DO NOT ALSO ACCUSE. conda winning IS the disambiguation — this branch
        # has always PROCEEDED on it — but `ambiguous` stayed True on the way out, so the
        # decision said "here is your package" and "this name is dangerously ambiguous" in
        # the same breath. `anndata` is the measured case: conda-forge anndata 0.13.2
        # (scverse/anndata) is exactly what was asked for, and it shipped flagged because
        # CRAN also has an `anndata` — which is a reticulate WRAPPER around that same Python
        # project, i.e. the collision is not even a collision.
        #
        # This is the false-accuse half that makes the true-positive half worthless: the
        # flag is identical in shape to the one raised over a genuine PyPI-vs-CRAN split, so
        # a ride that sees it on a correct pick learns it means nothing and stops reading it
        # on the pick where it does. A warning that fires on the healthy case is not a
        # cautious warning, it is a broken one. The FACT survives where it belongs and is not
        # lost: both tiers stay in `available`/`alternatives`, and the rationale names them.
        ambiguous = False
    elif ambiguous:
        chosen = None
        decision["chosen"] = None
    # VERSION EXISTENCE. A requested version the chosen tier does NOT carry must not be
    # emitted as a byte-identical pin — `install_conda_packages(samtools=9.99)` looks
    # exactly like a valid `samtools=1.21` but never solves. Refuse and name the nearest
    # real versions. Only conda/pip surface a version list; a tier that doesn't (cran /
    # bioconductor / a probe that couldn't fetch it) is left ALONE — no list is 'we don't
    # know', never 'the version is missing'. Runs only on a still-live pick, so an already-
    # refused ambiguous call is not second-guessed.
    if version and chosen in ("conda", "pip"):
        vs = availability.get(chosen, {}).get("versions") or []
        if vs and not _version_present(version, vs):
            if binary_ver and binary_ver.get("status") == "match":
                # conda/pip does NOT carry this version, but the user-named repo ships it as
                # a binary — route there rather than refuse a version that plainly EXISTS
                # (bioconda skips somalier 0.2.16 while brentp/somalier ships v0.2.16). NOT a
                # silent switch: the reroute is disclosed and rests on a 200 + a real asset.
                prior = chosen
                chosen = "binary"
                decision["chosen"] = "binary"
                decision["binary_version_reroute"] = {
                    "from": prior, "requested": version, "resolved_tag": binary_ver["tag"]}
                decision["rationale"] = (
                    f"{prior.upper()} LACKS {tool}=={version}, but {github_repo} ships it as a "
                    f"binary (release {binary_ver['tag']}) — routing to the binary tier to honor "
                    f"the pin. || " + decision.get("rationale", ""))
            else:
                nearest = _nearest_versions(version, vs)
                # If the named repo was ALSO checked and can't deliver the pin, say so — the
                # refusal then reflects the whole investigation, not just the registry tier.
                # But an error/incomplete release lookup is UNCHECKED, NOT a "checked": it must
                # not be rendered as a settled fact (binary_checked stays False), it gets its own
                # UNCHECKED disclosure, and it is recorded so _classify_refusal downgrades the
                # refusal to investigation_incomplete — a pin-honoring tier we could NOT reach
                # does not make the version known-absent (the UNREACHABLE-first doctrine).
                bstat = binary_ver.get("status") if binary_ver else None
                also = ""
                if bstat in ("absent", "no_asset"):
                    also = (f" The named repo {github_repo} was also checked: "
                            + ("no release matches this version."
                               if bstat == "absent"
                               else f"release {binary_ver.get('tag','')} exists but ships no "
                                    f"linux/amd64 binary."))
                elif bstat in ("error", "incomplete"):
                    why = binary_ver.get("error", "release list truncated at 100; page 2+ may hold it")
                    also = (f" The named repo {github_repo} could NOT be checked for this version "
                            f"({why}) — UNCHECKED, not absent; {version} may ship there as a binary.")
                    decision["binary_version_unchecked"] = binary_ver
                decision["version_absent"] = {
                    "tier": chosen, "requested": version,
                    "channel": availability.get(chosen, {}).get("channel", ""),
                    # TRUE only when the release lookup actually CONCLUDED (a 'match' would have
                    # rerouted above, so absent/no_asset are the only concluded states here).
                    "nearest": nearest, "binary_checked": bstat in ("absent", "no_asset")}
                tail = ("Emitting it would be a real-looking but UNSOLVABLE pin, byte-identical "
                        "to a valid one — refuse and pick an existing version."
                        if bstat not in ("error", "incomplete") else
                        f"conda/pip lacks it and the binary tier that could carry it was "
                        f"UNCHECKED — re-run (or fetch the {version} asset explicitly) rather "
                        f"than silently switching to a nearby version.")
                decision["rationale"] = (
                    f"VERSION NOT FOUND on {chosen}: no {tool}=={version} "
                    f"(nearest real: {', '.join(nearest) or '—'}).{also} {tail} || "
                    + decision.get("rationale", ""))
                chosen = None
                decision["chosen"] = None
    # FORK-ANCHOR outcomes (computed before rank_decision; applied to the ranked decision).
    if fork_divergence:
        decision["fork_divergence"] = fork_divergence
        decision["rationale"] = (
            f"FORK PIN: you named {fork_divergence['fork']}, which DIVERGES from its parent "
            f"{fork_divergence['parent']} — the registry tiers build the parent, so they were "
            f"disqualified; routing to the fork's own build. || " + decision.get("rationale", ""))
    if declared_repo_contradiction and chosen in ("conda", "pip", "cran", "bioconductor"):
        # A distinct same-name lineage the user did NOT name. Refuse and ASK rather than
        # silently build the other project (investigation_contradicted via _classify_refusal).
        decision["declared_repo_contradiction"] = declared_repo_contradiction
        decision["rationale"] = (
            f"DECLARED-REPO CONTRADICTION: you named "
            f"{declared_repo_contradiction['user_repo']}, but the "
            f"{declared_repo_contradiction['registry_tier']} package is built from "
            f"{declared_repo_contradiction['registry_repo']} — distinct same-name projects with "
            f"no fork edge between them. Refusing rather than building the other one: proceed via "
            f"{declared_repo_contradiction['registry_tier']} for the packaged line, or pass "
            f"prefer='synthesis'/language= to build the repo you named. || "
            + decision.get("rationale", ""))
        chosen = None
        decision["chosen"] = None
    if fork_disclosure:
        decision["rationale"] = decision.get("rationale", "") + "  NOTE: " + fork_disclosure
    # BINARY-VERSION disclosures. Two honest-non-refusal cases the version pin creates:
    if binary_ver:
        bstat = binary_ver.get("status")
        if chosen == "binary" and bstat in ("error", "incomplete"):
            # UNCHECKED: we kept the binary pick (no silent tier switch) but could NOT confirm
            # the pinned version's asset. The install_call carries a verify-manually placeholder,
            # not latest's bytes — say why, and that this is NOT a claim the version is absent.
            why = binary_ver.get("error", "release list truncated at 100; the version may be older")
            decision["binary_version_unverified"] = binary_ver
            decision["rationale"] = (
                f"BINARY VERSION UNVERIFIED: could not confirm {tool}=={version} in "
                f"{github_repo}'s releases ({why}) — NOT using latest's bytes; fetch the "
                f"{version} asset from https://github.com/{github_repo}/releases and pass it "
                f"explicitly. This is UNCHECKED, not 'version absent'. || "
                + decision.get("rationale", ""))
        elif bstat in ("error", "incomplete") and chosen in ("synthesis", "source"):
            # UNCHECKED and binary did NOT win: latest ships no linux asset (a CHECKED fact →
            # tier unavailable-from-latest) AND the pinned-release lookup did not complete. The
            # CHECKED-absent fallthrough below IS disclosed, so staying silent on this
            # strictly-weaker-knowledge case is a backwards asymmetry — and a prefer='binary'
            # here is reported merely 'not available', which is UNCHECKED for this pin, not
            # settled. Disclose + surface the fact machine-readably (mirroring the chosen==binary
            # path). No tier/bytes change — the unchecked lookup can only PROMOTE binary, never
            # demote it, so synthesis was the pick regardless; this only names what we couldn't see.
            why = binary_ver.get("error", "release list truncated at 100; the version may be older")
            decision["binary_version_unverified"] = binary_ver
            decision["rationale"] = (
                f"NOTE: the binary tier was UNCHECKED for {tool}=={version} ({why}) — "
                f"{github_repo}'s latest release ships no linux/amd64 binary AND the pinned "
                f"release could not be reached, so a {version} binary MAY exist there; {chosen} "
                f"builds from a repo ref instead. UNCHECKED, not 'no binary' — re-run to confirm. "
                f"|| " + decision.get("rationale", ""))
        elif bstat in ("absent", "no_asset") and chosen in ("synthesis", "source"):
            # The pin isn't deliverable as a binary, so the binary tier went unavailable and we
            # fell to synthesis/source — which build a REF, not this version's release asset.
            # Disclose so the fallthrough isn't silent (the reader may want a different version).
            decision["rationale"] = (
                f"NOTE: {github_repo} has no linux/amd64 binary for {tool}=={version} "
                + ("(no release matches this version)" if bstat == "absent"
                   else f"(release {binary_ver.get('tag','')} ships no such asset)")
                + f" — the binary tier is unavailable for this pin; {chosen} builds from a repo "
                f"ref, so confirm it targets {version}. || " + decision.get("rationale", ""))
    unchecked = unchecked_tiers(availability)
    # UNIFIED "is there a pullable image?" — a fact spanning the authors' own image and a
    # BioContainer. Pull-don't-build is the least-resistance path; surface it up front.
    pull = pullable_image(availability, tool, version, timeout, chosen=chosen)
    decision.update({
        "tool": tool,
        "version": version or None,
        "language": language or None,
        "github_repo": github_repo or None,
        "ambiguous": ambiguous,
        "probed": availability,
        "cross_namespace_collisions": cross_namespace_collisions,
        "unchecked_tiers": unchecked,
        "pullable_image": pull,
    })
    # When the pick is conda but a pre-built BioContainer exists, name the pull-by-digest
    # shortcut in the rationale: freeze ADOPTS it (no build), and an agent can go straight to
    # freeze_from_image and skip building a host env for a single-tool ask. (The author_image
    # case already IS the pick, so its adopt_call is the install_call — no duplicate note.)
    if chosen and pull.get("found") and pull.get("source") == "biocontainer" and chosen != "author_image":
        decision["rationale"] = (
            f"{decision.get('rationale', '')}  A pre-built BioContainer exists — adopt by "
            f"digest (PULL, no build): {pull['image_by_digest']}.").strip()
    # NAME-MAPPING HONESTY. When the pick came via the bioconductor→conda fold, the emitted
    # call names `bioconductor-{tool}`, not the `{tool}` the caller typed — say so up front so
    # a reader sees requested-vs-installed at a glance, never a silent substitution.
    # This clause used to read ONLY `bioc_spec`, so the two OTHER ways a name gets rewritten
    # went out silently: `r_spec` (`resolve('Seurat', language='r')` installed `r-Seurat` with
    # no mention) and CRAN's `resolved_name` (crandb is case-sensitive, so the query and the
    # package name differ). It now asks `registry_name` — the same leaf the install_call and
    # the biocontainer lookup use — so the disclosure cannot disagree with what gets installed.
    _chosen_detail = availability.get(chosen, {}) if chosen else {}
    _mapped_name = registry_name(_chosen_detail, tool)
    if _mapped_name != tool:
        if _chosen_detail.get("bioc_spec"):
            # The CRAN clause is CHECKED, not assumed: `limma` IS on CRAN (an archived mirror),
            # so hardcoding "not on CRAN" would contradict the resolver's own probe (and the
            # `cran` in this same rationale as a lower-priority alternative) — a report lie.
            _cran_clause = "" if availability.get("cran", {}).get("available") else ", and not on CRAN"
            decision["rationale"] = (
                f"BIOCONDUCTOR: '{tool}' is an R/Bioconductor package — bioconda ships it as "
                f"'{_mapped_name}' (not the bare name{_cran_clause}). "
                + decision.get("rationale", "")
            )
        else:
            decision["rationale"] = (
                f"NAME MAPPED: you asked for '{tool}'; the {chosen} registry knows this package "
                f"as '{_mapped_name}', and the emitted call installs THAT — confirm it is the "
                f"tool you meant. " + decision.get("rationale", "")
            )
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
    if ambiguous and chosen is None:
        # Only a still-unresolved collision gets the disambiguate-me note. A name that
        # RESOLVED to conda (the bioinformatics-channel package, incl. the bioconductor fold)
        # has already been disambiguated in context — telling the caller to pass a hint for a
        # pick we already made would be a lie of the kind the report-honesty work exists to kill.
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
    decision["refusal_reason"] = None
    # WHAT THE REPO SAYS IT IS. Stated on every decision, None included, for the reason the
    # paragraph above gives — and stated BEFORE _classify_refusal, which reads it.
    decision["workflow_repo"] = (authors_assessment.get("workflow_manifest")
                                 if authors_assessment.get("workflow_only") else None)
    if chosen:
        # FACTS, not a verdict. The resolver surfaces the entry's self-description +
        # repo provenance for the ride (the LLM) to judge identity; it no longer stamps
        # `confirmed` or poisons `install_call` with an identity warning (Phase 2 —
        # judgment moves to the ride). `install_call` stays a clean, runnable one-liner.
        decision["identity"] = identity_facts(tool, chosen, availability, github_repo, ev,
                                              repo_discovery)
        decision["install_call"] = _install_call(
            chosen, tool, version, availability.get(chosen, {}), github_repo
        )
    else:
        # chosen is None ⇒ a refusal (install_call is None IFF chosen is None). STATE the
        # machine-readable WHY, not just the absence — so a reader, and the intent corpus's
        # earned-vs-lazy check, gets the reason behind the ask. Read from the signals the
        # body already recorded above (unchecked_tiers @ update, discovery_error, collisions,
        # discovered_repos); the auto-adopt early-return threads its own via the recursion.
        decision["refusal_reason"] = _classify_refusal(decision)

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

    # THE VERSION YOU ARE BEING HANDED IS THE BEST OF THE CHANNELS WE COULD REACH.
    #
    # `probe_conda` queries bioconda AND conda-forge and returns the higher real version —
    # that comparison is the guard against an abandoned build on one channel shadowing the
    # maintained package on the other. When a channel does not answer, the guard silently
    # runs on one input and its output is indistinguishable from a real answer.
    #
    # Measured live: with conda-forge timing out, `resolve('seurat', language='r')` emitted
    # `r-seurat=3.0.2` — a 2019 build — while conda-forge carries 5.5.1. Two calls seconds
    # apart disagreed by two major versions of the standard single-cell toolkit and neither
    # said why. Same shape as `unchecked_tiers` one level up ("the best among the tiers we
    # could REACH is a different claim from the best there is"), one level down, and it was
    # the only level not saying it.
    if chosen == "conda" and decision.get("install_call"):
        _cerr = (availability.get("conda") or {}).get("channel_errors") or {}
        if _cerr:
            decision["conda_channel_unchecked"] = _cerr
            _won = availability["conda"].get("channel", "")
            _disclose(decision,
                      f"VERSION NOT PROVEN LATEST — {', '.join(f'{c} ({e})' for c, e in _cerr.items())}. "
                      f"'{tool}' resolved on {_won}, but the other channel never answered, so the "
                      f"higher-version comparison ran on ONE input. This is the newest build among "
                      f"the channels we could REACH, which is a different claim from the newest "
                      f"build. Re-run when the probe recovers before pinning this version.",
                      "a channel that could carry a newer build was NOT ruled out, only unreachable")

    # THE RIDE'S NORMALIZATION IS A CLAIM, AND CLAIMS GET CHECKED.
    #
    # `user_said='gatk'` with `tool='gatk4'` says: the user typed one thing, I am installing
    # another, on purpose. That is exactly what should happen — an LLM knows GATK4 is the
    # current line and the bare `gatk` package is GATK3 from 2017, and the system's job is to
    # USE that knowledge, not to re-derive it mechanically. But a substitution nobody checks
    # is a substitution nobody can catch, so it is verified here, where the family evidence
    # already is: the two names must belong to ONE package family.
    #
    # Same family (`gatk`/`gatk4`, `seurat`/`Seurat`, `macs2`/`macs3`) -> corroborated, and
    # the record carries both names so the ENV report can show "asked X, installed Y".
    # Different families -> REFUSE. A ride that answers 'bwa' with 'bowtie2' is not
    # normalizing, it is substituting a different tool, and no amount of good intent makes
    # that safe to do silently.
    _user_word = (user_said or "").strip().lower()
    # F1/F13. `user_said` is contracted as THE USER'S OWN WORD; a caller holding free
    # text passes the whole sentence, because that is what they have. Before this, a
    # sentence went straight into the family comparison, failed it (a sentence is not a
    # package family), and produced `investigation_contradicted` — a claim that the
    # investigation found a conflict — plus the remedy *"Resolve '<the whole sentence>'
    # as typed and read the family"*, which cannot be done: a sentence is not a package
    # name.
    #
    # F13 is what makes this more than a bad string: the guard INVERTED. Passing more
    # truth about the user's intent produced a refusal, and passing less — omitting
    # `user_said` entirely — produced a clean confident answer. A guard that punishes
    # disclosure teaches callers to withhold it, which is the opposite of what it is for.
    #
    # So: free text is classified as free text. If it CONTAINS the tool as a token, it
    # corroborates rather than contradicts — nobody substituted anything, the caller
    # just handed over the sentence — and the resolution proceeds with the mis-shaped
    # argument DISCLOSED rather than silently dropped. If it does not contain the tool,
    # the substitution question is live again and it refuses, with a remedy that names
    # the real front door instead of an impossible action.
    if _user_word and (len(_user_word.split()) > 1):
        _tok = tool.strip().lower()
        _mentions = bool(_tok) and re.search(rf"(?<![a-z0-9]){re.escape(_tok)}(?![a-z0-9])",
                                             _user_word) is not None
        decision["user_said_shape"] = "free_text"
        if _mentions:
            decision["rationale"] = (
                f"NOTE: `user_said` was free text ({user_said!r}), not the user's WORD for "
                f"the tool, which is what this argument is contracted to carry. It names "
                f"'{tool}', so it corroborates the call rather than contradicting it and the "
                f"resolution below stands. For the checked-substitution guard to actually "
                f"run, pass the bare name — `user_said='{tool}'` — or route free text "
                f"through `interpret_request`, which extracts the name and makes this call "
                f"for you. || " + decision.get("rationale", ""))
        else:
            decision["chosen"] = None
            chosen = None
            decision["install_call"] = None
            decision["refusal_reason"] = "needs_user_input"
            decision["normalization_rejected"] = {"user_said": user_said, "resolved_to": tool}
            decision["rationale"] = (
                f"CANNOT CHECK THE NORMALIZATION: `user_said` was free text ({user_said!r}) "
                f"rather than the user's WORD for the tool, and it does not mention '{tool}' "
                f"anywhere — so this call may be installing something the user never named, "
                f"and nothing here can tell. Two ways forward, both of them things you can "
                f"actually do: route the free text through `interpret_request`, which "
                f"extracts the tool name and makes this call correctly; or, if you already "
                f"know the user's word for it, re-call with `user_said='<that one word>'`. || "
                + decision.get("rationale", ""))
        _user_word = ""     # handled; do not also run the word-vs-word comparison below
    if _user_word and _user_word != tool.strip().lower():
        decision["normalized_from"] = user_said
        _base = re.match(r"^(.*?)\d*$", _user_word).group(1)
        _tbase = re.match(r"^(.*?)\d*$", tool.strip().lower()).group(1)
        # `r-{name}` / `bioconductor-{name}` are this module's OWN channel conventions, not
        # a rename by the ride — strip them before comparing, or every language='r' call
        # would read as a cross-family substitution.
        _tbase = re.sub(r"^(r-|bioconductor-)", "", _tbase)
        if _base != _tbase:
            decision["chosen"] = None
            chosen = None
            decision["install_call"] = None
            decision["refusal_reason"] = "investigation_contradicted"
            decision["normalization_rejected"] = {"user_said": user_said, "resolved_to": tool}
            decision["rationale"] = (
                f"NORMALIZATION REJECTED: the request named '{user_said}' and this call asks "
                f"for '{tool}', which is not the same package family ('{_base}' vs "
                f"'{_tbase}'). Renaming across a major version is expected here and is "
                f"supported; substituting a DIFFERENT tool is not, and nothing in the "
                f"registries can tell the user you did it. Resolve '{user_said}' as typed and "
                f"read the family, or ask the user to confirm they want '{tool}'. || "
                + decision.get("rationale", ""))

    # THE NAME IS A FAMILY, AND A REGISTRY HIT IS NOT AN ANSWER.
    #
    # `resolve('gatk')` emitted `gatk=3.8` — a 2017 release — because bioinformatics versions
    # its tools by RENAMING the package and GATK4 ships as `gatk4`. Every fact about that
    # pick was correct, which is what made it dangerous, and nothing anywhere said the thing
    # almost everyone means was one character away. The root cause is not the name: it is
    # that this module STOPPED INVESTIGATING the moment a registry answered. Discovery fires
    # only at a dead end, so a stale-but-present hit ends the search by succeeding.
    #
    # So we research on a hit too, and hand the ride the whole family in ONE call: every
    # member's version, its own words, and a pasteable line for each. The ride reads
    # "gatk 3.8 / gatk4 4.6.2.0, both the Genome Analysis Toolkit" and picks in the same
    # turn — no second round trip, which was the whole complaint.
    #
    # AND WE DO NOT RANK. "Newest wins" swaps bowtie1 (an ungapped aligner, its own repo,
    # its own users) for bowtie2. Refusing taxes every family for a question most callers
    # already answered — the over-correction the corpus's tophat row exists to guard.
    # What the pick loses is only its false confidence: with a family on the table the
    # install_call is comment-prefixed, so the older member is never pasted as though it
    # were the settled answer, and the runnable line survives for whoever did mean it.
    if chosen == "conda" and not version and decision.get("install_call"):
        _fam = probe_package_family(tool, timeout)
        if _fam.get("probe_error"):
            # ABSENT ≠ UNCHECKED. Staying silent here would assert "this tool has no
            # siblings" on a search that never ran — the same lie the rest of this file
            # spends its length refusing.
            decision["package_family_unchecked"] = _fam["probe_error"]
            _disclose(decision,
                      f"SIBLING PACKAGES NOT CHECKED: the channel search for the '{tool}' "
                      f"family failed ({_fam['probe_error']}). This field renames packages "
                      f"across major versions, so a newer lineage may exist under another "
                      f"name and we did NOT rule it out — this is unchecked, not absent.",
                      "we could not look for sibling packages")
        elif _fam.get("members"):
            decision["package_family"] = _fam
            _rows = "\n".join(
                f"#   {m['name']}={m['latest']} ({m['channel']})"
                + (f" — {m['summary']}" if m['summary'] else "")
                + (f" [{m['repo']}]" if m['repo'] else "")
                for m in _fam["members"])
            _disclose(decision,
                      f"THIS NAME IS A FAMILY — {len(_fam['members'])} packages on the "
                      f"channels carry it, and this field renames the package when a major "
                      f"version lands, so the newest '{tool}' need not be the newest of this "
                      f"tool. All of them, with their own descriptions:\n{_rows}\n"
                      f"# Pick the one the request means and call resolve_tool with THAT name "
                      f"(they are separate packages, not versions of one). The line below is "
                      f"the literal name you typed, nothing more.",
                      "the pick below is one member of a family — confirm it is the right one")

    # THE INVESTIGATION MUST NOT STOP THE MOMENT SOMETHING ANSWERS — the github half.
    #
    # The block above researches the CHANNEL on a hit. This researches GITHUB on a hit, and
    # it closes the inversion measured on 2026-08-06: the three tools with no registry entry
    # each got a five-repo investigation and an honest answer, while the two with a same-name
    # squatter got NO investigation and a clean, confident, wrong `install_call`. Discovery
    # fired only at a dead end, so a registry hit ended the search by succeeding — and the
    # two names it silenced (`cellranger`, `dorado`) are the two most contested in the set.
    # A squatter EXISTING is the strongest available signal that a name is contested, and it
    # was the one condition under which we stopped looking.
    #
    # Skipped when the caller named a repo: they anchored the identity themselves, the
    # cross-namespace guard above already reconciles the registry hit against it, and the
    # auto-adopt path re-enters here WITH `github_repo=` — so this costs one search per
    # resolve, never two.
    #
    # THE COST, stated because the old comment on `probe_github_search` promised the
    # opposite: unauthenticated github search is 10 req/min, so this DOES put the common
    # registry path within reach of that quota. What it may never do is let the quota become
    # a finding — a search that did not answer is `unobserved`, the pick is untouched, and
    # nothing claims the name is uncontested. (`_headers` sends GITHUB_TOKEN when there is
    # one, which raises the ceiling without changing any of that.)
    #
    # The key is STATED on every decision, `not_applicable` included — a reader cannot tell
    # "there was nothing to corroborate" from "this producer forgot the field", and the
    # difference is the whole content of the answer here.
    decision["identity_corroboration"] = {
        "status": "not_applicable", "entry_repo": github_repo or "",
        "same_name_repos": [], "divergent_repos": [], "matched": None,
        "detail": ("the caller named the repo, so identity is anchored and the "
                   "cross-namespace guard reconciled the registry hit against it"
                   if github_repo else
                   "no tier was chosen, so there is no entry to corroborate — the "
                   "dead-end path runs the same search as DISCOVERY")}
    if chosen and not github_repo:
        _corr = name_corroboration(
            tool, (decision.get("identity") or {}).get("repo") or "",
            probe_github_search(tool, timeout))
        decision["identity_corroboration"] = _corr
        if _corr["status"] == "diverges":
            # SHOW ALL OF THEM, AND NAME NONE OF THEM.
            #
            # Two defects lived in this block, both introduced by the fix for F15 —
            # the finding about name confusion — and both found by audit hours later:
            #
            # 1. `[:3]` printed three rows under a sentence that said how many repos
            #    own the name. Measured on `dorado`: "4 repo(s) own the name" above a
            #    list of three. One field, two readings, in the message whose whole job
            #    is to let a human compare candidates.
            # 2. The copy-pasteable remedy hard-named `divergent_repos[0]`. That list
            #    arrives ordered by STARS (github search, top 5 by stars), so the one
            #    repo we singled out was the star maximum — for `dorado`, a 1222-star
            #    PowerShell Scoop bucket, recommended over the 859-star
            #    `nanoporetech/dorado` printed directly beneath it. Star-dominance is
            #    the exact heuristic this codebase removed from `auto_adoptable`, and
            #    it came back inside the remedy string of the fix built to cure name
            #    confusion.
            #
            # The 2026-08-06 ruling is that judging WHICH project you meant is the
            # ride's call, because the ride is the reader with the world knowledge to
            # make it. A resolver that picks one is doing that judging with a star
            # count. So the remedy offers the FORM and the reader picks the repo.
            # Every row carries its OWN copy-pasteable next call, so the message stays
            # as actionable as it was while privileging nobody. Naming one and only one
            # was what smuggled the star ranking in.
            _div = _corr["divergent_repos"]
            _rows = "\n".join(
                f"#   {c['repo']} ({c['stars']}★)"
                + (f" — \"{c['description']}\"" if c.get("description") else
                   " — publishes NO description, so there is nothing to read it against")
                + f"\n#       -> github_repo='{c['repo']}'"
                for c in _div)
            _disclose(decision,
                      f"SAME NAME, DIFFERENT PROJECTS — we searched github for repos named "
                      f"exactly '{tool}' (the check a registry hit used to suppress). "
                      f"{_corr['detail']}. All {len(_div)} of them, in the order github "
                      f"ranked them BY STARS (which is popularity, not relevance — the "
                      f"one you want may be last):\n{_rows}\n"
                      f"# A registry carrying the name proves the NAME is taken, not that "
                      f"this entry is the tool you meant — and this is the one question no "
                      f"gate downstream asks: a wrong-but-working tool builds, validates, "
                      f"and ships green with a digest and an attestation. Read each "
                      f"description above against what you asked for, then either proceed "
                      f"(the line below is unchanged) or re-run with github_repo= set to "
                      f"the value printed beside whichever project you meant. "
                      f"We do not pick one for you: choosing between these is world "
                      f"knowledge, and a resolver that guessed would be guessing by "
                      f"star count.",
                      "another project owns this name — confirm which one you meant")

    # A WORKFLOW IS NOT A TOOL, AND SAYING SO IS THE WHOLE ANSWER.
    #
    # Disclosed whether or not we refused: a pipeline that ALSO has a registry entry gets a
    # real install_call, and the caller still needs to know the repo they named is a
    # pipeline. The note carries three things, and the third is the one that keeps the next
    # agent from "fixing" this by wiring up the obvious route:
    #   1. what it is, in its own manifest's words;
    #   2. that the ENGINE is the installable part, and resolves cleanly today;
    #   3. why we do not just freeze the engine and call the pipeline validated. `nextflow
    #      run` spawns a container PER PROCESS, none of which this system froze, pinned or
    #      validated. Sealing that would set `validated_in_shipped_image` on the launcher
    #      while STAR and salmon ran in images nobody here ever looked at — green about the
    #      thing that did no work, silent about everything that did. That is precisely the
    #      false green the two-layer contract exists to prevent, so the honest landing is to
    #      name the shape of the gap rather than paper it.
    if decision.get("workflow_repo"):
        _wf = decision["workflow_repo"]
        _dec = _wf.get("declares") or {}
        _eng = _wf.get("engine") or "workflow"
        _needs = _dec.get("nextflowVersion")
        # THE MANIFEST WAS READ AT HEAD, AND THE PIN IS THE CALLER'S.
        #
        # assess_tool_sources walks `ref="HEAD"` — deliberately, because a tag guessed wrong
        # ('3.14.0' vs 'v3.14.0') 404s and loses the DETECTION, which matters more than the
        # figure. But that makes the manifest's `version` an observation of the DEFAULT
        # BRANCH, and the first cut of this message spent it as the `-r` operand: a caller
        # who asked for 3.14.0 was handed `nextflow run nf-core/rnaseq -r 3.26.0`. That is
        # the somalier 0.2.15→v0.3.3 class — latest wearing the requested version's name —
        # reproduced in the message announcing that we caught a wrong answer. The revision
        # to run is what the CALLER pinned; HEAD's figure is labelled as HEAD's.
        _head_ver = _dec.get("version")
        _run_rev = version or _head_ver
        _self = " ".join(x for x in (_dec.get("name"), _head_ver) if x)
        if _self and _head_ver:
            _self += " on its default branch"
        # A CONCRETE COMMAND ONLY WHERE WE CAN GROUND ONE. `nextflow run <name> -r <rev>` is
        # the form nf-core's own README uses and the manifest hands us both operands. There
        # is no equally canonical one-liner for Snakemake (--snakefile / --deploy-sources /
        # a clone, depending on version and layout), so we say the SHAPE and stop. Emitting
        # `snakemake run <repo>` — which is not a real invocation — would be the confident-
        # wrong output the rest of this file exists to prevent, in the message announcing
        # that we caught a confident-wrong output.
        _howto = (f"`nextflow run {_dec.get('name') or github_repo or tool}"
                  + (f" -r {_run_rev}" if _run_rev else "") + "`"
                  if _eng == "nextflow" else
                  f"the {_eng} CLI against a pinned revision of {github_repo or tool} — "
                  f"see the repo's own README for the exact invocation")
        _disclose(decision,
                  f"THIS IS A {_eng.upper()} WORKFLOW, NOT AN INSTALLABLE TOOL — "
                  + (f"the repo's own manifest calls it {_self}" if _self
                     else f"the repo ships {', '.join(_wf.get('paths') or [])}")
                  + " and carries no Dockerfile, env-spec or build script. A pipeline is RUN "
                    f"by an engine, not installed: {_howto}. The INSTALLABLE part is the "
                    f"engine — resolve_tool('{_eng.split('/')[0]}')"
                  + (f", which this pipeline pins at '{_needs}'" if _needs else "")
                  + ". Note what that does NOT buy you: the engine runs each step in its OWN "
                    "environment, none of which this system freezes, pins or validates, so "
                    "freezing the engine and sealing the run would mark the launcher "
                    "validated-in-shipped-image while every tool that did the work ran "
                    "somewhere we never looked.",
                  "no install_call is emitted for a pipeline — the engine is the tool")

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
    # THE INVERSION, closed. Measured on the real registries: `cellranger` (a real CRAN
    # entry) got a loud "A repo is not the tool's just because it shares its name", while
    # `bcl2fastq` — no registry hit AT ALL, adopted purely because a github name search
    # returned a 57-star repo whose own description calls it a "wrapper" — got a clean,
    # uncommented install_call and the line "proceeding without a human." The case with
    # the LEAST corroboration produced the MOST confident output, because auto-adopt
    # re-entered resolve with `github_repo=` set and the inner call could not tell our
    # guess from the user's instruction.
    #
    # Stated as PROVENANCE, not judgement: no domain heuristics, no "is this a bio tool",
    # no star threshold reinterpreted as identity. Whether brwnj/bcl2fastq IS bcl2fastq
    # is the ride's call — this only refuses to imply the question was settled.
    if repo_discovery and github_repo:
        # The repo's OWN words, quoted rather than judged — the same signal
        # `identity_evidence` calls "the single most useful" one, which the discovery
        # path was collecting and discarding. For bcl2fastq it reads "NextSeq specific
        # bcl2fastq2 wrapper", which settles the question the star count cannot.
        says = (repo_discovery.get("description") or "").strip()
        _disclose(decision,
                  f"AUTO-DISCOVERED REPO, IDENTITY NOT CORROBORATED — '{tool}' hit NO "
                  f"registry, so {github_repo} was chosen by a github search on the NAME "
                  f"and adopted because it leads on stars "
                  f"({repo_discovery.get('stars', '?')}★). Stars measure popularity, not "
                  f"identity: the top exact-name hit is routinely a third-party wrapper, a "
                  f"fork, or an unrelated project. "
                  + (f"The repo describes ITSELF as: \"{says}\" — read that against what "
                     f"you asked for. " if says else
                     "The repo publishes NO description, so there is nothing to read it "
                     "against — which is less to go on, not more. ")
                  + f"Confirm it IS the tool before building; re-run with "
                    f"github_repo='{github_repo}' to state that deliberately.",
                  "NOTHING vouched for this repo — we matched a name")
    # A vendor-CDN asset is a real answer and a weaker anchor than a release asset, and
    # both halves have to reach the caller. `install_release_binary` will sha256-pin
    # whatever bytes it gets, but a README is not tag-pinned, so WHICH bytes is the part
    # nobody else can check for us.
    bin_detail = availability.get("binary") or {}
    if chosen == "binary" and bin_detail.get("asset_source") == "readme":
        reach = bin_detail.get("vendor_asset_reachable")
        _disclose(decision,
                  f"VENDOR-CDN ASSET, NOT A RELEASE ASSET — {github_repo} attaches no "
                  f"assets to its release, so this URL comes from the README "
                  f"({'HEAD 200 confirmed' if reach else 'reachability UNCHECKED'}). A "
                  f"README lives on the default branch, so it names whatever the authors "
                  f"last shipped"
                  + (f" (filename says {bin_detail['vendor_asset_version']})"
                     if bin_detail.get("vendor_asset_version") else "")
                  + ". Pin the sha256 you actually download; do not assume this URL is "
                    "stable across releases.",
                  "the download URL is tied to a branch, not a tag")
    if bin_detail.get("vendor_asset_pin_mismatch"):
        _disclose(decision,
                  f"VENDOR BUILD DOES NOT MATCH THE PIN — "
                  f"{bin_detail['vendor_asset_pin_mismatch']}",
                  "the binary tier was NOT offered for this version")
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

    # A RESTRICTED licence changes the CALL, so it is disclosed on the call. This is not
    # the identity poisoning Phase 2 removed: identity asks "is this the tool you meant",
    # and for novoalign the answer is an unqualified yes — right tool, right channel,
    # correct description, a BioContainer ready to adopt. The licence is a third axis, and
    # the only one whose consequence is a downstream REFUSAL: freeze will not stamp
    # `redistributable: true` on an image whose own conda-meta says Commercial. Naming that
    # here is the same forward-pointer discipline as THE GATE IS THE GUIDE — except the
    # gate fires at freeze and the decision is made here, so the pointer has to travel.
    if (decision.get("identity") or {}).get("license_disposition") == "restricted":
        _disclose(decision,
                  f"LICENSE-GATED — {decision['chosen']} publishes this as "
                  f"{(decision['identity'].get('license') or '')!r}. It is the right tool; "
                  f"the artifact is not freely redistributable",
                  "freeze REFUSES this unless you declare it: "
                  "freeze(…, gated=True, licenses=[…]) — which also keeps the image "
                  "out of any registry push (I13)")

    # THE TWO NOTES THAT STATE AN ABSENCE — appended, never a banner.
    #
    # Both say "nobody answered", and neither is a finding. `_disclose` prefixes the
    # rationale AND stamps the install_call, which is right for something we FOUND and
    # wrong for something we could not look at: github search is 10 req/min, and every
    # non-registry pick lacks a licence by construction, so routing these two through the
    # loud channel would put a banner on healthy picks all session — the false-accuse
    # failure the `ambiguous` calibration above is written against ("a warning that fires
    # on the healthy case is not a cautious warning, it is a broken one"). They are STATED,
    # in the rationale and machine-readably in `identity_corroboration` / `identity.
    # license_evidence`, which is what absence-is-not-a-verdict requires; what it does not
    # require is that every absence shout.
    _notes: list[str] = []
    if (decision.get("identity_corroboration") or {}).get("status") == "unobserved":
        _notes.append(
            f"NAME NOT CORROBORATED (unobserved, not clean): "
            f"{decision['identity_corroboration']['detail']}. A same-name project on "
            f"github would not have been seen — re-run, or set GITHUB_TOKEN, if this name "
            f"is one another project could plausibly own.")
    _lic_ev = (decision.get("identity") or {}).get("license_evidence")
    if _lic_ev and _lic_ev != "published":
        _why = {
            "unclassified_text": (f"{decision['identity'].get('license_source')} holds the "
                                  f"licence TEXT rather than an identifier, and we do not "
                                  f"keyword-match prose"),
            "not_published": f"the {chosen} entry publishes no licence",
            "not_probed": f"our {chosen} probe reads no licence field",
            "no_channel": (f"the {chosen} route is a repo or a release asset, and nothing "
                           f"on it publishes a licence for these bytes"),
        }[_lic_ev]
        _notes.append(
            f"LICENCE UNOBSERVED — {_why}, so this decision says NOTHING about whether "
            f"these bytes may be redistributed. Nor will anything downstream: I13 arms on "
            f"a licence it can OBSERVE in the shipped image's conda-meta, which a tool "
            f"built from a repo or fetched as a binary does not carry. This absence is "
            f"also what a vendor-gated tool looks like from here — bytes behind an account "
            f"or a click-through are unredistributable, which is exactly why no public "
            f"registry carries them — so if a download form stands between you and this "
            f"tool, that is yours to recognise: declare it at freeze "
            f"(gated=True, licenses=[…]) and install the bytes the user supplies.")
    if _notes:
        decision["rationale"] = (decision.get("rationale", "") + "  NOTE: "
                                 + "  NOTE: ".join(_notes)).strip()

    # NO identity poisoning of install_call. Identity is the ride's judgment now
    # (Phase 2); the facts it judges on ride in `decision["identity"]` (self-description
    # + repo provenance), and install_call stays a clean, runnable one-liner. The
    # disclosures that DO remain above (authors-path-not-assessed, gate-errored, unchecked
    # tiers, prefer-ignored) are about the ROUTING being incomplete — a different axis from
    # "is this the tool you meant", which no longer gets a resolver verdict.
    #
    # NO TOOL-NAME TABLE, EITHER. A `VENDOR_GATED` dict lived here briefly (2026-08-06):
    # seven proprietary names — cellranger, dragen, guppy, … — that turned a working
    # install_call into chosen=None because the vendor gates the real binary, so a
    # same-name registry hit is necessarily a different project. The fact is true; the
    # mechanism was wrong. It made this module the one place in `agent/` where a hardcoded
    # tool name changed behaviour, and it duplicated, in a table that can only rot, world
    # knowledge the ride already has. The seventh name being right does not make the
    # eighth's absence honest: a finite list silently promises completeness it cannot keep.
    #
    # The division of labour stands as written above. The resolver surfaces FACTS — the
    # chosen entry's own words, its repo provenance, its licence, which tiers went
    # unprobed. Judging whether those facts describe the tool the user MEANT is the ride's
    # call, and the ride is an LLM with the world knowledge to make it: to know that 10x
    # gates Cell Ranger, that CRAN's `cellranger` parses spreadsheet ranges, and that a
    # request naming single-cell RNA-seq cannot mean the latter. Where it genuinely cannot
    # tell, the answer is to ASK the user, not to consult a list.
    #
    # The backstop is downstream and needs no names: `tool_identity` reads the shipped
    # SBOM and prints the installed package's own words into the ENV report, so a wrong
    # tool that got past the ride is legible to the human reading the artifact.
    return decision


