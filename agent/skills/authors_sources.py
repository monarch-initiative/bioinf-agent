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

import functools
import re
import urllib.error
import urllib.request
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


#: Registry manifest media types — an image index (multi-arch) or a single manifest.
_MANIFEST_ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def _default_probe_ghcr_image(owner: str, repo: str, *, tag: str = "latest",
                              timeout: int = 12) -> Optional[dict]:
    """Does `ghcr.io/{owner}/{repo}:{tag}` exist and can WE pull it, anonymously?

    Returns {ref, source} | None, or {"error": ...} when the probe itself failed.

    ASK THE REGISTRY, NOT GITHUB'S INDEX. This used to hit
    `api.github.com/orgs/{owner}/packages`, which returns **401 for every org on
    earth** — public ones included — because that REST API requires authentication,
    period. No token was ever plumbed, and the 401 was swallowed two layers down in
    `_default_get_text`, so the gate reported "the authors ship no image" when the
    truth was "we were denied". The top-ranked tier of the reliability gate was 100%
    dead for every tool, and said so in the voice of a finding (audit 2026-07-16).

    It was also the wrong QUESTION. "Is a package listed under this org?" needs auth,
    excludes user-owned repos (`/orgs/` only), and doesn't answer what we need. "Can I
    pull this image?" is answered by the registry's own anonymous token flow, needs no
    credentials for public images — and a private image is one we could never adopt
    anyway, so a negative there is a true negative. Verified live: `astral-sh/uv` → 200
    with no token; `api.github.com/orgs/astral-sh/packages` → 401.
    """
    import json
    if not owner or not repo:
        return None
    ref = f"{owner}/{repo}".lower()

    def _fetch(url: str, headers: dict) -> tuple[Optional[str], Optional[str]]:
        """(body, error). A 403/404 is a real ANSWER (no public image), not an error —
        distinguishing those two is the whole point of this probe, so it cannot go
        through _default_get_text, which collapses every failure to None."""
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace"), None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                return None, None          # denied/absent → true negative
            return None, f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            return None, f"{type(e).__name__}: {e}"

    try:
        tok_url = (f"https://ghcr.io/token?scope=repository:{ref}:pull"
                   f"&service=ghcr.io")
        txt, err = _fetch(tok_url, {"User-Agent": "bioinf-agent-authors"})
        if err:
            return {"error": f"ghcr token endpoint: {err}"}
        if txt is None:
            # ghcr DENIED an anonymous pull scope → the package is private or absent.
            # A true negative: we could not adopt it even if it exists.
            return None
        token = (json.loads(txt) or {}).get("token") if txt.strip().startswith("{") else None
        if not token:
            return None
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{ref}/manifests/{tag}",
            headers={"Authorization": f"Bearer {token}", "Accept": _MANIFEST_ACCEPT,
                     "User-Agent": "bioinf-agent-authors"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return {"ref": f"ghcr.io/{ref}", "source": "ghcr", "tag": tag}
    except urllib.error.HTTPError as e:
        # 404/403 = the image genuinely isn't publicly pullable → a real answer, not an
        # error. Anything else means our probe failed and must NOT read as "no image".
        return None if e.code in (403, 404) else {"error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def discover_authors_sources(
    owner: str, repo: str, *, ref: str = "HEAD",
    get_text: Callable[..., Optional[str]] = _default_get_text,
    get_json: Callable[..., Optional[Any]] = _default_get_json,
    probe_image: Optional[Callable[..., Optional[dict]]] = None,
) -> dict[str, Any]:
    """Discover the authoring resources in a GitHub repo (best-effort, network-injected).
    Returns {container_recipes: [{path, text}], env_specs: [{path, text}],
    build_scripts: [{path}], author_image: {ref, source} | None,
    author_image_error?: str}. Fetches recipe TEXT (so the completeness parser can run)
    but only lists env-specs/scripts by presence.

    `probe_image` is a separate seam from get_text/get_json because the registry probe
    needs auth headers those two can't carry. It defaults to None and resolves from
    module globals at CALL time, so `_default_probe_ghcr_image` stays monkeypatchable —
    the same discipline that keeps the real resolve→assess path under test."""
    probe_image = probe_image or _default_probe_ghcr_image
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

    # author-published image: ask the REGISTRY whether we can pull it, anonymously.
    # A publicly pullable ghcr image under the repo's own owner, named for the repo, is a
    # strong "the authors ship an image" signal — and, unlike a package *listing*, it is
    # the exact fact we need (see _default_probe_ghcr_image for why the old GitHub-index
    # probe was both unauthorized and asking the wrong question). Docker Hub / quay are
    # checked by the caller when a ref is declared; we don't brute-force registries here.
    img = probe_image(owner, repo)
    if isinstance(img, dict) and img.get("error"):
        # R2: a failed probe must NOT read as "the authors ship no image". That silent
        # degradation is exactly what kept this tier dead and invisible.
        out["author_image_error"] = img["error"]
    elif img:
        out["author_image"] = img
    return out


# ---------------------------------------------------------------------------
# the router-facing verdict
# ---------------------------------------------------------------------------
def assess_tool_sources(
    tool: str, *, owner: str = "", repo: str = "", ref: str = "HEAD",
    sources: Optional[dict] = None,
    timeout: int = 12,
    get_text: Optional[Callable[..., Optional[str]]] = None,
    get_json: Optional[Callable[..., Optional[Any]]] = None,
    probe_image: Optional[Callable[..., Optional[dict]]] = None,
) -> dict[str, Any]:
    """Combine discovery + completeness into the reliability-gate verdict the router
    consumes. `sources` may be injected (already-discovered) to skip network.

    `timeout` bounds each default fetch: discovery is a serial walk of _CONTAINER_RECIPES
    + _ENV_SPECS + _BUILD_SCRIPTS, and resolve_tool is a query-only interactive call, so
    the caller owns the budget. It binds to the DEFAULT fetchers only — an injected
    get_text/get_json owns its own I/O (and its own timeout).

    get_text/get_json default to None (not to the functions themselves) so the fallback is
    resolved from module globals at CALL time: that keeps `_default_get_*` monkeypatchable,
    which is what lets a test drive the REAL resolve→assess call path with stubbed I/O
    instead of replacing this function with a double. Replacing the function is how the
    call-signature drift of audit 2026-07-16 stayed invisible behind 9 green tests.

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
    get_text = get_text or functools.partial(_default_get_text, timeout=timeout)
    get_json = get_json or functools.partial(_default_get_json, timeout=timeout)
    probe_image = probe_image or functools.partial(_default_probe_ghcr_image, timeout=timeout)
    src = sources if sources is not None else discover_authors_sources(
        owner, repo, ref=ref, get_text=get_text, get_json=get_json,
        probe_image=probe_image)
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
    image_error = src.get("author_image_error")
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
    if image_error:
        # R2 again, at the verdict layer: "we couldn't check" must never be delivered as
        # "there is nothing there". Every sentence above assumes author_image is a real
        # observation; when the probe broke, say so in the same breath.
        rec_txt += (f" [NB: the author-image probe FAILED ({image_error}) — 'no author "
                    f"image' here is UNCHECKED, not a negative finding]")

    out = {
        "author_image": author_image,
        "authors_recipe": authors_recipe,
        "env_specs": src.get("env_specs", []),
        "build_scripts": src.get("build_scripts", []),
        "reconstruction_incomplete": incomplete,
        "recommendation": rec_txt,
    }
    if image_error:
        out["author_image_error"] = image_error
    return out
