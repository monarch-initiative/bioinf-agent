"""
authors_sources — detect and assess a tool's OWN install resources, so routing can
follow the authors' path when a package-manager reconstruction would be incomplete.

The governing idea (see [[feedback-prioritize-authors-own-env-recipe]]): a human asked
to install a tool that ships its own image / Dockerfile / env-spec would FOLLOW that
guide — even if rough — rather than reconstruct the environment from scratch and risk
silently missing the compiled/system pieces the authors bundle. bioconda/PyPI are
reconstructions; excellent when complete, silently wrong when not (Talos shipped a
private bcftools fork + htslib + echtvar that a pip reconstruction dropped, yet unit
tests passed because they don't shell out).

This module gives the router the INPUTS it lacks: which authoring resources exist, and
— the load-bearing signal — whether the authors' recipe installs things a conda/pip
reconstruction could NOT represent. That is the "reliability gate": it does NOT declare
the authors' path always-best (a cleanly, completely bioconda-packaged tool is better
served by conda — solver-managed, pinned, small). It fires the authors' path ONLY when
reconstruction is demonstrably incomplete.

Two layers, kept separate so the judgement is unit-testable without network:
  • analyze_recipe_completeness(text, ...)   — PURE. Parse a Dockerfile / .def / build
    script for signals a registry recipe wouldn't capture (system apt, compiled-from-
    source, a vendored dependency fork, a fetched binary, a foreign-toolchain build).
  • discover_authors_sources(owner, repo, ...) — network (injected) discovery of the
    repo's Dockerfile/.def/env-spec + a best-effort author-published-image probe.
assess_tool_sources() combines them into the router-facing verdict + recommendation.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

# Files that carry the authors' own build/environment definition, by kind.
_CONTAINER_RECIPES = ("Dockerfile", "dockerfile", "Containerfile",
                      "Singularity", "Singularity.def", "apptainer.def", "container.def")
_ENV_SPECS = ("environment.yml", "environment.yaml", "conda-lock.yml", "spack.yaml",
              "renv.lock")
_BUILD_SCRIPTS = ("install.sh", "setup.sh", "build.sh", "Makefile", "makefile")

# apt/yum/apk packages that are benign runtime plumbing — installing ONLY these is not
# a "system dependency the reconstruction misses" signal.
_BENIGN_SYSTEM_PKGS = {
    "ca-certificates", "tzdata", "locales", "gnupg", "curl", "wget", "procps",
    "bash", "coreutils", "less", "vim", "nano", "git",
}


# ---------------------------------------------------------------------------
# PURE completeness analysis
# ---------------------------------------------------------------------------
def _apt_packages(text: str) -> list[str]:
    """Package names from `apt-get install` / `apt install` / `apk add` / `yum install`
    lines (line-continuations joined). Best-effort token scrape, flags stripped."""
    joined = re.sub(r"\\\s*\n", " ", text)  # join backslash-continued lines
    pkgs: list[str] = []
    pat = re.compile(r"(?:apt-get|apt|apk|yum|dnf|zypper)\s+(?:-\S+\s+)*"
                     r"(?:install|add)\s+(.+)", re.IGNORECASE)
    for m in pat.finditer(joined):
        tail = m.group(1)
        # stop at shell operators / the next command in a chained RUN
        tail = re.split(r"&&|\|\||;|>|<", tail)[0]
        for tok in tail.split():
            if tok.startswith("-") or "=" in tok or tok in ("y", "yes"):
                continue
            if tok in ("apt-get", "apt", "&&", "rm", "clean", "update"):
                continue
            # variable refs / obvious non-packages
            if tok.startswith("$") or "/" in tok:
                continue
            pkgs.append(tok)
    return pkgs


def analyze_recipe_completeness(text: str, *, kind: str = "dockerfile",
                                self_repo: str = "") -> dict[str, Any]:
    """PURE. Given a recipe's text, return whether a conda/pip RECONSTRUCTION of the
    tool would be complete, and the concrete signals that say otherwise.

    `self_repo` ('owner/repo' or 'repo') lets us tell a VENDORED dependency clone (a
    real signal — e.g. Talos cloning populationgenomics/bcftools) from the repo cloning
    ITSELF (not a signal). Returns:
        {reconstruction_safe: bool, signals: [{kind, evidence}], summary: str}
    reconstruction_safe is True iff NO strong signal is present — i.e. the recipe is
    essentially 'install the language package (+ pip/conda deps)', which conda/pip DOES
    capture. Any strong signal (system pkgs / compiled / vendored dep / fetched binary /
    foreign toolchain) flips it False: follow the authors' recipe instead."""
    signals: list[dict[str, str]] = []
    low = text
    self_name = (self_repo.split("/")[-1] or "").lower()

    # --- system packages (beyond benign plumbing) ---
    apt = [p for p in _apt_packages(low)]
    real_sys = [p for p in apt if p.lower() not in _BENIGN_SYSTEM_PKGS]
    if real_sys:
        signals.append({"kind": "system_packages",
                        "evidence": "installs OS packages a registry recipe omits: "
                                    + ", ".join(sorted(set(real_sys))[:12])})

    # --- compiled from source ---
    if re.search(r"\b(make|cmake|\./configure|autoreconf|autoconf|autoheader|meson|ninja)\b", low):
        m = re.search(r".*\b(make|cmake|\./configure|autoreconf|meson)\b.*", low)
        signals.append({"kind": "compiled_from_source",
                        "evidence": (m.group(0).strip()[:140] if m else "make/configure present")})

    # --- vendored dependency (git clone of a DIFFERENT repo) ---
    for gm in re.finditer(r"git\s+clone\s+(?:--\S+\s+)*(\S+)", low):
        url = gm.group(1)
        repo_name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1]).lower()
        if repo_name and repo_name != self_name:
            signals.append({"kind": "vendored_dependency",
                            "evidence": f"clones a dependency the reconstruction can't see: {url}"})

    # --- fetched binary (wget/curl of a release asset, not a source tarball) ---
    for fm in re.finditer(r"(?:wget|curl)\s+[^\n]*?(https?://\S+)", low):
        url = fm.group(1).rstrip("\"'")
        tail = url.lower()
        looks_binary = ("/releases/download/" in tail
                        or re.search(r"\.(gz|bz2|xz|zip|tar|tgz)(\?|$)", tail) is None
                        and not tail.endswith((".txt", ".json", ".cfg", ".yml", ".yaml")))
        if "/releases/download/" in tail or looks_binary:
            signals.append({"kind": "fetched_binary",
                            "evidence": f"downloads a prebuilt asset: {url[:110]}"})

    # --- foreign language toolchain build (rust/go/node compiled, not python/R pkg) ---
    if re.search(r"\bcargo\s+(install|build)\b", low):
        signals.append({"kind": "foreign_toolchain", "evidence": "builds a Rust crate (cargo)"})
    if re.search(r"\bgo\s+(install|build)\b", low):
        signals.append({"kind": "foreign_toolchain", "evidence": "builds a Go binary (go install/build)"})

    # de-dup by (kind, evidence)
    seen = set()
    uniq = []
    for s in signals:
        key = (s["kind"], s["evidence"])
        if key not in seen:
            seen.add(key)
            uniq.append(s)

    safe = len(uniq) == 0
    if safe:
        summary = (f"the authors' {kind} installs nothing a conda/pip reconstruction "
                   "would miss — the registry route is complete and preferred")
    else:
        kinds = sorted({s["kind"] for s in uniq})
        summary = (f"the authors' {kind} installs pieces a registry reconstruction would "
                   f"DROP ({', '.join(kinds)}) — follow the authors' recipe, don't reconstruct")
    return {"reconstruction_safe": safe, "signals": uniq, "summary": summary}


# ---------------------------------------------------------------------------
# network discovery (injected http so it's testable)
# ---------------------------------------------------------------------------
def _default_get_text(url: str, timeout: int = 12) -> Optional[str]:
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bioinf-agent-authors"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None


def _default_get_json(url: str, timeout: int = 12) -> Optional[Any]:
    import json
    txt = _default_get_text(url, timeout)
    if txt is None:
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None


def discover_authors_sources(
    owner: str, repo: str, *, ref: str = "HEAD",
    get_text: Callable[..., Optional[str]] = _default_get_text,
    get_json: Callable[..., Optional[Any]] = _default_get_json,
) -> dict[str, Any]:
    """Discover the authoring resources in a GitHub repo (best-effort, network-injected).
    Returns {container_recipes: [{path, text}], env_specs: [{path, text}],
    build_scripts: [{path}], author_image: {ref, source} | None}. Fetches recipe TEXT
    (so the completeness parser can run) but only lists env-specs/scripts by presence."""
    out: dict[str, Any] = {"container_recipes": [], "env_specs": [],
                           "build_scripts": [], "author_image": None}
    if not owner or not repo:
        return out

    def raw(path: str) -> Optional[str]:
        return get_text(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}")

    # container recipes — fetch text (the completeness signal lives here)
    for fn in _CONTAINER_RECIPES:
        txt = raw(fn)
        if txt:
            out["container_recipes"].append({"path": fn, "text": txt})
    # env specs + build scripts — presence is enough to surface as an authoring resource
    for fn in _ENV_SPECS:
        if raw(fn) is not None:
            out["env_specs"].append({"path": fn})
    for fn in _BUILD_SCRIPTS:
        if raw(fn) is not None:
            out["build_scripts"].append({"path": fn})

    # author-published image (best-effort): GitHub Container Registry package listing.
    # A published ghcr package under the repo owner named for the repo is a strong "the
    # authors ship an image" signal. Docker Hub / quay are checked by the caller when a
    # ref is declared; we don't brute-force registries here.
    pkgs = get_json(f"https://api.github.com/orgs/{owner}/packages?package_type=container")
    if isinstance(pkgs, list):
        for p in pkgs:
            if isinstance(p, dict) and (p.get("name") or "").lower() == repo.lower():
                out["author_image"] = {"ref": f"ghcr.io/{owner}/{repo}", "source": "ghcr"}
                break
    return out


# ---------------------------------------------------------------------------
# the router-facing verdict
# ---------------------------------------------------------------------------
def assess_tool_sources(
    tool: str, *, owner: str = "", repo: str = "", ref: str = "HEAD",
    sources: Optional[dict] = None,
    get_text: Callable[..., Optional[str]] = _default_get_text,
    get_json: Callable[..., Optional[Any]] = _default_get_json,
) -> dict[str, Any]:
    """Combine discovery + completeness into the reliability-gate verdict the router
    consumes. `sources` may be injected (already-discovered) to skip network.

    Returns:
      {
        author_image: {ref, source} | None,     # tier A — adopt it
        authors_recipe: {                        # tier B — build the authors' recipe
            present: bool, path: str, reconstruction_safe: bool,
            signals: [...], summary: str } | None,
        env_specs: [...], build_scripts: [...],
        reconstruction_incomplete: bool,         # THE gate: True => prefer authors' path
        recommendation: str,
      }
    reconstruction_incomplete is True iff a container recipe carries a strong signal.
    When False, the authors ship nothing conda/pip would miss → the registry route
    stays preferred (conda keeps winning for cleanly-packaged tools)."""
    src = sources if sources is not None else discover_authors_sources(
        owner, repo, ref=ref, get_text=get_text, get_json=get_json)
    self_repo = f"{owner}/{repo}" if owner and repo else (repo or tool)

    authors_recipe = None
    worst = None
    for rec in src.get("container_recipes", []):
        analysis = analyze_recipe_completeness(rec.get("text", ""), kind=rec.get("path", "dockerfile"),
                                               self_repo=self_repo)
        cand = {"present": True, "path": rec.get("path", ""),
                "reconstruction_safe": analysis["reconstruction_safe"],
                "signals": analysis["signals"], "summary": analysis["summary"]}
        # keep the recipe with the STRONGEST (least safe) signal — that's the one that
        # decides whether reconstruction is safe.
        if worst is None or (not cand["reconstruction_safe"] and worst["reconstruction_safe"]):
            worst = cand
    authors_recipe = worst

    author_image = src.get("author_image")
    incomplete = bool(authors_recipe and not authors_recipe["reconstruction_safe"])

    if author_image:
        rec_txt = (f"the authors publish an image ({author_image['ref']}) — adopt it by "
                   "digest (highest fidelity, lowest cost)")
    elif incomplete:
        rec_txt = (f"build from the authors' {authors_recipe['path']}: "
                   + authors_recipe["summary"])
    elif authors_recipe:
        rec_txt = ("the authors ship a recipe but it installs nothing a registry route "
                   "would miss — a clean conda/pip install is the reliable least-resistance path")
    else:
        rec_txt = ("no authoring image/recipe found — route by the registry tiers "
                   "(conda/pip/...) as usual")

    return {
        "author_image": author_image,
        "authors_recipe": authors_recipe,
        "env_specs": src.get("env_specs", []),
        "build_scripts": src.get("build_scripts", []),
        "reconstruction_incomplete": incomplete,
        "recommendation": rec_txt,
    }
