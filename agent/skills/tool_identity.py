"""Identity disclosure (audit finding #8): what a REQUESTED tool says it IS.

`resolve()` surfaces a tool's self-description at resolve time and it dies there — no
frozen artifact carries it, so a human reading a frozen env's ENV.html cannot tell that
`cellranger` resolved to CRAN's "Translate Spreadsheet Cell Ranges" parser. The Layer-1
contract proves a tool WORKS (VALIDATED_IN_IMAGE); it never proved it is the tool you
MEANT. This module is the PRODUCER that puts identity on disk: freeze calls `capture()`
with the requested tools + the in-image SBOM, and it reads each tool's own words.

Three rules make the disclosure honest rather than a liability:

  1. TIED TO THE INSTALL, never a bare-name guess. A tool present in the SBOM as a
     conda/pip package gets THAT package's registry summary. A tool installed from a
     non-registry tier (binary / source / jar) gets `self_description=None` — honest
     silence, not a borrowed identity from a same-named package that was never installed.
     This is the trap the naive version falls into: a correctly-installed ONT `dorado`
     binary must NOT be labelled with astronomy-PyPI `dorado`'s summary.

  2. IDENTITY BY NAME IS NOT IDENTITY. When the record KNOWS a tool's repository (the
     authors' Dockerfile was cloned from it; the image carries a source label), a registry
     hit is adopted only if its own metadata — homepage, project URLs, source repo —
     points at that repository. A hit that points elsewhere is a NAME COLLISION: the
     description is withheld and the collision is recorded, so the reader is told "a
     same-named package exists elsewhere", never handed the other project's blurb. PyPI's
     `talos` ("Reproducible parameter sweeps for Keras…") must not describe an env built
     from populationgenomics/talos.

  3. AGENT-ASSERTED, best-effort, gates NOTHING. Any probe failure leaves that tool's
     `self_description` None and NEVER fails a freeze. Identity DISCLOSES; it does not
     validate. The `ToolIdentity` model this feeds is labelled OUTSIDE the verified
     surface everywhere it renders.

The dicts returned here are validated against `core_data.ToolIdentity` at
`EnvCache.register` — this module states facts, the seam enforces the shape.
"""
from __future__ import annotations

import re
from typing import Optional

# R / Bioconductor / Perl conda packages carry a channel prefix the requested tool name
# omits (`seurat` -> `r-seurat`, `limma` -> `bioconductor-limma`). Match these forms so a
# conda-installed R tool still resolves to its package + description; a miss still stays a
# miss — we never fall through to a bare-name registry guess, which is how a same-name
# squatter's blurb gets misattributed to the tool that actually shipped.
_SBOM_PREFIXES = ("", "r-", "bioconductor-", "perl-")

#: `github.com/owner/repo` in any spelling a URL or ssh remote uses.
_GH_REPO_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


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


def _registry_probe(kind: str, name: str) -> tuple[Optional[str], Optional[str], dict]:
    """(self_description, source, probe) from the registry the SBOM says the package came
    from. `probe` is the resolver's own response, kept so the anchor check below reads the
    same URLs `resolve()` reads.

    Reuses the resolver's OWN registry probes so "how we read a package summary" has a
    single home — the same `about.summary` / PyPI `Summary` that `resolve()` surfaces.
    Best-effort: a network/parse failure returns (None, None, {}), never raises."""
    from agent.skills import resolver as _R
    try:
        if kind == "conda":
            d = _R.probe_conda(name)
            if d.get("available"):
                return (((d.get("summary") or "").strip() or None), "conda", d)
        elif kind == "pypi":
            d = _R.probe_pypi(name)
            if d.get("available"):
                return (((d.get("summary") or "").strip() or None), "pypi", d)
    except Exception:
        pass
    return None, None, {}


def _repo_needles(known_repo: str) -> list[str]:
    """The lowercase substrings a registry URL must contain to count as pointing at
    `known_repo`. A GitHub repo (URL, ssh remote, or bare `owner/repo`) yields its
    `github.com/owner/repo` form and the `owner.github.io/repo` docs-site form; any other
    git URL yields its host+path with scheme, `.git` and trailing slashes stripped."""
    s = (known_repo or "").strip()
    if not s:
        return []
    m = _GH_REPO_RE.search(s)
    if m:
        owner, repo = m.group(1).lower(), re.sub(r"[.]git$", "", m.group(2)).lower()
        return [f"github.com/{owner}/{repo}", f"{owner}.github.io/{repo}"]
    bare = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", s)
    if bare:
        owner, repo = bare.group(1).lower(), re.sub(r"[.]git$", "", bare.group(2)).lower()
        return [f"github.com/{owner}/{repo}", f"{owner}.github.io/{repo}"]
    body = re.sub(r"^[a-z+]+://", "", s.lower())              # https://host/path -> host/path
    ssh = re.match(r"^[^@/]+@([^:/]+):(.+)$", body)             # user@host:path  -> host/path
    if ssh:
        body = f"{ssh.group(1)}/{ssh.group(2)}"
    body = re.sub(r"[.]git$", "", body.rstrip("/"))
    return [body] if body else []


def _registry_urls(source: str, probe: dict) -> list[str]:
    """Every URL the registry entry publishes about itself, in the order a reader would
    trust them: the source repository first, then the homepage, then the project page.
    conda's probe exposes the recipe's GitHub repo (`repo`, from dev_url/home); PyPI's
    exposes `home_page`, `project_urls` and `package_url`."""
    urls: list[str] = []
    if source == "conda":
        if probe.get("repo"):
            urls.append(f"https://github.com/{probe['repo']}")
    elif source == "pypi":
        pu = probe.get("project_urls") or {}
        preferred = [v for k, v in pu.items()
                     if isinstance(v, str) and re.search(r"source|repo|code|github", k, re.I)]
        rest = [v for k, v in pu.items() if isinstance(v, str) and v not in preferred]
        urls += preferred
        if probe.get("home_page"):
            urls.append(probe["home_page"])
        urls += rest
        if probe.get("package_url"):
            urls.append(probe["package_url"])
    return [u.strip() for u in urls if isinstance(u, str) and u.strip()]


def _anchor(source: str, probe: dict, known_repo: str) -> tuple[str, str]:
    """Does the registry entry point at `known_repo`? Returns (verdict, points_at):
      ("anchored", <the url that matched>)    — the hit IS the known project
      ("collision", <where it points instead>) — the hit names a different project
      ("unanchored", "")                       — the hit publishes no URL at all, so it
                                                 cannot be tied to the known repo either way
    """
    needles = _repo_needles(known_repo)
    urls = _registry_urls(source, probe)
    if not urls:
        return ("unanchored", "")
    for u in urls:
        lu = u.lower()
        if any(n in lu for n in needles):
            return ("anchored", u)
    return ("collision", urls[0])


def capture(requested_tools: list[str], resolved_packages: list[dict],
            nonregistry_tools: Optional[list[str]] = None,
            known_sources: Optional[dict[str, str]] = None) -> list[dict]:
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

    `known_sources` maps a requested tool name to the repository the RECORD knows it comes
    from (the authors' Dockerfile source; an image's source label). For such a tool a
    registry hit is adopted only when the registry's own metadata points at that
    repository. Otherwise the description is withheld and the record says why:
      • the entry points elsewhere → `collision={registry, points_at, known_repo}` + a note
        that a same-named package exists elsewhere;
      • the entry publishes no URL at all → no collision is claimed (nothing points
        anywhere), but the description is still withheld with a note, because a blurb that
        cannot be tied to the known project is a guess.
    Tools with no known source are matched as before — the SBOM tie is their anchor.

    KNOWN LIMITATION (channel axis): `_registry_probe` probes conda by name across both
    bioconda and conda-forge and takes the higher-version channel's summary — the SBOM does
    not record which channel shipped. For a name that hosts DIFFERENT projects across
    channels with an inverted version, this could surface the wrong channel's blurb. Rare
    (the two channels keep a largely collision-free namespace) and it never fabricates a
    capability — the field stays labelled unverified — so it is documented, not gated."""
    nonregistry = {(t or "").lower() for t in (nonregistry_tools or [])}
    known = {(t or "").lower(): (r or "").strip()
             for t, r in (known_sources or {}).items() if (t or "").strip() and (r or "").strip()}
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
        collision: Optional[dict] = None
        note: Optional[str] = None
        # A non-registry-tier tool gets honest silence — never a same-named closure entry's
        # identity. Only registry-tier tools (conda/pip) are matched against the SBOM.
        if (tool or "").lower() not in nonregistry:
            entry = _match_sbom(tool, sbom)
            if entry:
                pkg = entry.get("name") or None
                ver = entry.get("version") or None
                desc, source, probe = _registry_probe(entry.get("kind") or "", pkg or tool)
                repo = known.get((tool or "").lower(), "")
                if source and repo:
                    verdict, points_at = _anchor(source, probe, repo)
                    if verdict == "collision":
                        collision = {"registry": source, "points_at": points_at,
                                     "known_repo": repo}
                        note = (f"a same-named {source} package `{pkg or tool}` exists "
                                f"elsewhere ({points_at}); it is not {repo}, so its "
                                f"description is withheld")
                        desc = None
                    elif verdict == "unanchored":
                        note = (f"the {source} package `{pkg or tool}` publishes no "
                                f"homepage or repository, so it cannot be tied to {repo}; "
                                f"its description is withheld")
                        desc = None
        out.append({
            "tool": tool,
            "self_description": desc,
            "source": source,
            "package": pkg,
            "version": ver,
            "collision": collision,
            "note": note,
        })
    return out
