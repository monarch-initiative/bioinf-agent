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

# A repo can declare itself a WORKFLOW rather than a tool: an orchestrated graph of OTHER
# tools, RUN by an engine, not installed. These are the files that say so — and the walk
# above checked sixteen filenames without one of them, so nf-core/rnaseq (which ships no
# Dockerfile, no environment.yml and no Makefile) came back "no recipe in the repo" and
# fell through to the synthesis tier, whose only test is that the repo exists. The corpus
# synthesis then handed the agent contained the pipeline's release-announcement TWEET
# workflow and Black's line-length, and did NOT contain main.nf. Every signal saying "I am
# a pipeline" was on the wire; nothing read one.
_WORKFLOW_MANIFESTS = (
    ("nextflow",  "nextflow.config"),
    ("nextflow",  "main.nf"),
    ("snakemake", "Snakefile"),
    ("snakemake", "workflow/Snakefile"),
)
#: Of those, the only one carrying a DECLARATION worth parsing. The rest are presence.
_WORKFLOW_MANIFEST_TEXT = {"nextflow.config"}
#: The manifest keys we read. Four, named — not "whatever looks like a key".
_NF_MANIFEST_KEYS = ("name", "version", "nextflowVersion", "description")

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


def parse_nextflow_manifest(text: str) -> dict[str, str]:
    """The `manifest { … }` block of a nextflow.config, flattened to the keys we name.

    Deliberately NARROW, for the `_first_line` reason: a nextflow.config is Groovy, and a
    regex clever enough to cover the general case is a regex confident enough to return
    some OTHER block's `version` under the pipeline's name — the bcftools-reporting-
    htslib's-version defect, re-run on a config file. So: only inside the manifest block,
    only the four keys in `_NF_MANIFEST_KEYS`, only `key = 'quoted'`.

    Unparseable ⇒ `{}`. That is not a loss: the load-bearing fact is the file's PRESENCE,
    which the caller already has, and an absent declaration is honest where a guessed one
    would not be.
    """
    m = re.search(r"\bmanifest\s*\{", text or "")
    if not m:
        return {}
    block = (text or "")[m.end():]
    end = block.find("}")            # nf-core manifest blocks do not nest
    if end != -1:
        block = block[:end]
    out: dict[str, str] = {}
    for key in _NF_MANIFEST_KEYS:
        km = re.search(rf"^\s*{key}\s*=\s*['\"]([^'\"]*)['\"]", block, re.M)
        if km and km.group(1).strip():
            out[key] = km.group(1).strip()
    return out


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

#: Where the authors' own image is looked for under the repository's `owner/repo` name
#: (lower-cased, as registries require). Each is asked anonymously through its own token
#: flow, so a public image is found without credentials and a private one — which could
#: never be adopted — reads as absent. Bioinformatics publishes on all three.
_AUTHOR_IMAGE_REGISTRIES = ("ghcr", "dockerhub", "quay")
_REGISTRY_HOST = {"ghcr": "ghcr.io", "dockerhub": "registry-1.docker.io", "quay": "quay.io"}
_REGISTRY_LABEL = {"ghcr": "ghcr.io", "dockerhub": "Docker Hub", "quay": "quay.io"}
#: How many of an image's tags a found image carries back, so a caller can pin a release
#: without guessing at names.
_TAGS_LISTED = 40


def _registry_token_url(registry: str, ref: str) -> str:
    """The anonymous pull-scope token endpoint of each registry."""
    if registry == "ghcr":
        return f"https://ghcr.io/token?scope=repository:{ref}:pull&service=ghcr.io"
    if registry == "dockerhub":
        return f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{ref}:pull"
    return f"https://quay.io/v2/auth?service=quay.io&scope=repository:{ref}:pull"


def image_ref(registry: str, ref: str) -> str:
    """The pullable spelling: a Docker Hub image is bare `owner/name`; the others carry
    their host."""
    return ref if registry == "dockerhub" else f"{_REGISTRY_HOST[registry]}/{ref}"


def _registry_fetch(url: str, headers: dict, timeout: int, method: str = "GET") -> tuple[Optional[str], Optional[str]]:
    """(body, error). A 401/403/404 is a real ANSWER (no public image), not an error —
    distinguishing those two is the whole point of the probe, so it does not go through
    _default_get_text, which collapses every failure to None. A HEAD that succeeds returns
    an empty body, never None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bioinf-agent-authors", **headers},
                                     method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None, None
            return (r.read().decode("utf-8", "replace") if method == "GET" else ""), None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return None, None          # denied/absent → true negative
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"{type(e).__name__}: {e}"


def _probe_registry_image(registry: str, ref: str, *, tag: str = "latest",
                          timeout: int = 12) -> Optional[dict]:
    """Does `{registry}/{ref}` exist and can WE pull it, anonymously?

    Returns {ref, source, tag, tags, tags_total} | None, or {"error": ...} when the probe
    itself failed. `tag` is "" when the asked-for tag is absent but the image exists: the
    tags list is the existence proof then (quay images often carry no `latest`).

    ASK THE REGISTRY, NOT GITHUB'S INDEX. `api.github.com/orgs/{owner}/packages` returns
    401 for every org on earth — public ones included — because that REST API requires
    authentication, and it is the wrong question besides: "is a package listed?" needs
    auth and excludes user-owned repos, while "can I pull this?" is answered by the
    registry's own anonymous token flow, and a private image is one we could never adopt
    anyway, so a negative there is a true negative.
    """
    import json
    if not ref or "/" not in ref or registry not in _REGISTRY_HOST:
        return None
    host, label = _REGISTRY_HOST[registry], _REGISTRY_LABEL[registry]
    txt, err = _registry_fetch(_registry_token_url(registry, ref), {}, timeout)
    if err:
        return {"error": f"{label} token endpoint: {err}"}
    if txt is None:
        return None
    try:
        token = (json.loads(txt) or {}).get("token") if txt.strip().startswith("{") else None
    except ValueError:
        token = None
    if not token:
        return None
    auth = {"Authorization": f"Bearer {token}", "Accept": _MANIFEST_ACCEPT}
    body, err = _registry_fetch(f"https://{host}/v2/{ref}/manifests/{tag}", auth, timeout, method="HEAD")
    if err:
        return {"error": f"{label} manifest: {err}"}
    has_tag = body is not None
    tags_txt, terr = _registry_fetch(f"https://{host}/v2/{ref}/tags/list?n=1000", auth, timeout)
    tags: list[str] = []
    if tags_txt:
        try:
            tags = [t for t in ((json.loads(tags_txt) or {}).get("tags") or []) if isinstance(t, str)]
        except ValueError:
            tags = []
    if not has_tag and not tags:
        return {"error": f"{label} tags: {terr}"} if terr else None
    return {"ref": image_ref(registry, ref), "source": registry, "tag": tag if has_tag else "",
            "tags": tags[-_TAGS_LISTED:], "tags_total": len(tags)}


def _default_probe_ghcr_image(owner: str, repo: str, *, tag: str = "latest",
                              timeout: int = 12) -> Optional[dict]:
    """ghcr.io alone, by the owner/repo convention — one of the three registries
    `_default_probe_author_image` asks."""
    if not owner or not repo:
        return None
    return _probe_registry_image("ghcr", f"{owner}/{repo}".lower(), tag=tag, timeout=timeout)


#: An image reference as a README, a CI file or a compose file spells it. Only spellings
#: that can ONLY be an image are matched — a `docker pull`, a `docker://` URI, a
#: registry-prefixed path, a Docker Hub page URL, an `image:` key — so a repository URL
#: or a docs path is never taken for one.
_REF = r"(?P<ref>[A-Za-z0-9][\w.-]*(?:/[A-Za-z0-9][\w.-]*){1,3}(?::[\w][\w.-]{0,127})?(?:@sha256:[0-9a-f]{64})?)"
_DECLARED_IMAGE_PATTERNS = (
    re.compile(r"\bdocker\s+(?:pull|run|create)\s+(?:-{1,2}[\w-]+(?:[= ]\S+)?\s+)*" + _REF),
    re.compile(r"docker://" + _REF),
    re.compile(r"(?<![\w/.-])(?P<ref>(?:ghcr\.io|quay\.io|docker\.io)/[\w.-]+(?:/[\w.-]+){1,3}(?::[\w][\w.-]{0,127})?)"),
    re.compile(r"hub\.docker\.com/(?:r|repository/docker)/(?P<ref>[\w.-]+/[\w.-]+)"),
    re.compile(r"^\s*(?:image|container)\s*[:=]\s*['\"]?" + _REF, re.M),
)


def _split_image_ref(ref: str) -> tuple[str, str, str]:
    """`(registry, path, tag)` for an image reference; registry "" when the host is one
    this probe does not speak to. A bare `owner/name` is Docker Hub."""
    ref = ref.strip().strip("`'\"").split("@", 1)[0]
    registry = "dockerhub"
    for key, host in _REGISTRY_HOST.items():
        if ref.lower().startswith(host + "/"):
            registry, ref = key, ref[len(host) + 1:]
            break
    else:
        if ref.lower().startswith("docker.io/"):
            ref = ref[len("docker.io/"):]
        elif ref.count("/") >= 1 and "." in ref.split("/", 1)[0]:
            return "", "", ""           # some other registry host
    path, _, tag = ref.partition(":")
    if "/" in tag:                      # a port, not a tag — not a shape this probe handles
        return "", "", ""
    return registry, path, tag


def declared_images(text: str, owner: str = "") -> list[str]:
    """Image references `text` declares, de-duplicated in order. With `owner`, only images
    under that owner — the authors' own — are kept, so a base image a README mentions
    (`continuumio/miniconda3`) is never taken for theirs."""
    seen: list[str] = []
    for pat in _DECLARED_IMAGE_PATTERNS:
        for m in pat.finditer(text or ""):
            ref = m.group("ref").rstrip(".,;:)")
            registry, path, _ = _split_image_ref(ref)
            if not registry or not path:
                continue
            if owner and path.split("/", 1)[0].lower() != owner.lower():
                continue
            if ref not in seen:
                seen.append(ref)
    return seen


def _default_probe_author_image(owner: str, repo: str, *, tag: str = "latest", timeout: int = 12,
                                declared: tuple[str, ...] | list[str] = ()) -> Optional[dict]:
    """The authors' own published image, wherever they publish it: the references the
    repository itself declares (`declared`, from its README) are asked first, then
    `owner/repo` on each of `_AUTHOR_IMAGE_REGISTRIES`. The first image that answers wins
    and carries `declared_as` when the README named it. Returns {ref, source, tag, tags,
    tags_total, declared_as?} | None, or {"error": ...} when nothing was found AND at
    least one probe failed — so "no image" is never said about a registry that was not
    heard from."""
    attempts: list[tuple[str, str, str, str]] = []
    for d in declared or ():
        registry, path, dtag = _split_image_ref(d)
        if registry and path:
            attempts.append((registry, path.lower(), dtag or tag, d))
    if owner and repo:
        conv = f"{owner}/{repo}".lower()
        attempts.extend((r, conv, tag, "") for r in _AUTHOR_IMAGE_REGISTRIES)
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for registry, path, t, declared_as in attempts:
        if (registry, path) in seen:
            continue
        seen.add((registry, path))
        found = _probe_registry_image(registry, path, tag=t, timeout=timeout)
        if found and found.get("error"):
            errors.append(found["error"])
            continue
        if found:
            if declared_as:
                found["declared_as"] = declared_as
            return found
    return {"error": "; ".join(errors)} if errors else None


#: A container recipe by its file name, anywhere in a repository tree.
_RECIPE_BASENAME_RE = re.compile(r"(?:^|[._-])dockerfile(?:$|[._-])|\.dockerfile$|^containerfile$|\.def$", re.I)
#: Directories whose recipes are CI or documentation fixtures before they are the tool's.
_RECIPE_LOW_PRIORITY_DIRS = {".github", ".gitlab", "ci", "test", "tests", "testing", "docs", "doc", "examples", "example"}
#: How many below-root recipes are fetched for the completeness analysis.
_TREE_RECIPES_FETCHED = 3
#: Files a README is looked for under.
_READMES = ("README.md", "README.rst", "README", "readme.md")


def recipe_paths_in(paths: list[str]) -> list[str]:
    """Container recipes in a path listing, the most likely first: shallowest, with a
    plain `Dockerfile` ahead of a variant and CI/docs directories last."""
    def _is_recipe(p: str) -> bool:
        base = p.rsplit("/", 1)[-1]
        return base in _CONTAINER_RECIPES or bool(_RECIPE_BASENAME_RE.search(base))
    hits = [p for p in paths if _is_recipe(p)]
    return sorted(hits, key=lambda p: (p.split("/", 1)[0].lower() in _RECIPE_LOW_PRIORITY_DIRS and "/" in p,
                                       p.count("/"), p.rsplit("/", 1)[-1] not in _CONTAINER_RECIPES, p))


def _tree_paths(owner: str, repo: str, ref: str, get_json) -> tuple[Optional[list[str]], str]:
    """Every file path in the repository at `ref`, from ONE GitHub tree request;
    (None, why) when the listing is unavailable — a rate limit, a network failure, a
    host that is not GitHub — so a caller can say "not listed" rather than "nothing there"."""
    d = get_json(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1")
    if not isinstance(d, dict) or not isinstance(d.get("tree"), list):
        return None, "the repository tree could not be listed"
    paths = [t.get("path", "") for t in d["tree"] if isinstance(t, dict) and t.get("type") == "blob"]
    return paths, ("GitHub truncated the tree listing" if d.get("truncated") else "")


def discover_authors_sources(
    owner: str, repo: str, *, ref: str = "HEAD",
    get_text: Callable[..., Optional[str]] = _default_get_text,
    get_json: Callable[..., Optional[Any]] = _default_get_json,
    probe_image: Optional[Callable[..., Optional[dict]]] = None,
) -> dict[str, Any]:
    """Discover the authoring resources in a GitHub repo (best-effort, network-injected).
    Returns {container_recipes: [{path, text}], env_specs: [{path}], build_scripts: [{path}],
    workflow_files: [...], author_image: {ref, source, tag, tags, ...} | None,
    author_image_error?: str, readme?: str, declared_images: [...],
    recipe_search: "root" | "tree" | "root_only", recipe_search_note?: str,
    recipe_candidates?: [...]}. Fetches recipe TEXT (so the completeness parser can run)
    but only lists env-specs/scripts by presence.

    Recipes are looked for at the repository root first; when none is there, ONE tree
    listing finds them anywhere below it (`docker/Dockerfile`), and the first few are
    fetched. A tree that could not be listed is reported as `root_only`, never as "no
    recipe". The README is read for the image references the authors declare, which the
    image probe asks before the owner/repo convention.

    `probe_image` is a separate seam from get_text/get_json because the registry probe
    needs auth headers those two can't carry. It defaults to None and resolves from
    module globals at CALL time, so `_default_probe_author_image` stays monkeypatchable —
    the same discipline that keeps the real resolve→assess path under test."""
    probe_image = probe_image or _default_probe_author_image
    out: dict[str, Any] = {"container_recipes": [], "env_specs": [],
                           "build_scripts": [], "workflow_files": [], "author_image": None,
                           "declared_images": [], "recipe_search": "root"}
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
    # workflow manifests — WHAT this repo is, which none of the three walks above ask.
    for engine, fn in _WORKFLOW_MANIFESTS:
        txt = raw(fn)
        if txt is None:
            continue
        entry: dict[str, Any] = {"engine": engine, "path": fn}
        if fn in _WORKFLOW_MANIFEST_TEXT:
            entry["declares"] = parse_nextflow_manifest(txt)
        out["workflow_files"].append(entry)

    # Below the root: one tree listing, only when the root carried no recipe. The root
    # stays authoritative when it has one; a `docker/Dockerfile` is found when it has not.
    if not out["container_recipes"]:
        paths, note = _tree_paths(owner, repo, ref, get_json)
        if paths is None:
            out["recipe_search"] = "root_only"
            out["recipe_search_note"] = note
        else:
            out["recipe_search"] = "tree"
            if note:
                out["recipe_search_note"] = note
            cands = recipe_paths_in(paths)
            out["recipe_candidates"] = cands
            for p in cands[:_TREE_RECIPES_FETCHED]:
                txt = raw(p)
                if txt:
                    out["container_recipes"].append({"path": p, "text": txt})
            known = {e["path"] for e in out["env_specs"]} | {e["path"] for e in out["build_scripts"]}
            for p in paths:
                base = p.rsplit("/", 1)[-1]
                if "/" in p and p not in known and p.split("/", 1)[0].lower() not in _RECIPE_LOW_PRIORITY_DIRS:
                    if base in _ENV_SPECS:
                        out["env_specs"].append({"path": p})
                    elif base in _BUILD_SCRIPTS:
                        out["build_scripts"].append({"path": p})

    # The README: the image references the authors themselves declare.
    for fn in _READMES:
        txt = raw(fn)
        if txt:
            out["readme"] = fn
            out["declared_images"] = declared_images(txt, owner)
            break

    # author-published image: ask the REGISTRIES whether we can pull it, anonymously —
    # what the README declares first, then owner/repo on each registry the probe knows.
    img = probe_image(owner, repo, declared=tuple(out["declared_images"]))
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
    instead of replacing this function with a double. Replacing the function is how a
    call-signature drift stays invisible behind green tests.

    Returns:
      {
        author_image: {ref, source} | None,     # tier A — adopt it
        authors_recipe: {                        # tier B — build the authors' recipe
            present: bool, path: str, reconstruction_safe: bool,
            signals: [...], summary: str } | None,
        env_specs: [...], build_scripts: [...],
        reconstruction_incomplete: bool,         # THE gate: True => prefer authors' path
        workflow_manifest: {engine, paths, declares} | None,
        workflow_only: bool,                     # THE OTHER gate: True => not installable
        recommendation: str,
      }
    reconstruction_incomplete is True iff a container recipe carries a strong signal.
    When False, the authors ship nothing conda/pip would miss → the registry route
    stays preferred (conda keeps winning for cleanly-packaged tools).

    workflow_only answers a DIFFERENT question — not "how do we install this" but "is
    this an installable thing at all". True means the repo declares itself a Nextflow /
    Snakemake workflow and carries nothing installable, so the install tiers have nothing
    to work with and the honest answer names the engine instead."""
    get_text = get_text or functools.partial(_default_get_text, timeout=timeout)
    get_json = get_json or functools.partial(_default_get_json, timeout=timeout)
    probe_image = probe_image or functools.partial(_default_probe_author_image, timeout=timeout)
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
    if authors_recipe:
        authors_recipe["alternatives"] = [r.get("path", "") for r in src.get("container_recipes", [])
                                          if r.get("path", "") != authors_recipe["path"]]

    author_image = src.get("author_image")
    image_error = src.get("author_image_error")
    incomplete = bool(authors_recipe and not authors_recipe["reconstruction_safe"])

    # IS THIS A TOOL AT ALL? Every check above asks "how would we install this"; none
    # asks "is this an installable thing". A workflow repo is a graph of OTHER tools, run
    # by an engine — `nextflow run nf-core/rnaseq -r 3.14.0`, not `install`. The two
    # verdicts are computed HERE, together, so the router never re-derives either: a
    # second reading of "is this workflow-only" is precisely the drift
    # tests/test_one_reading_per_field.py exists to stop.
    wf_files = src.get("workflow_files") or []
    workflow_manifest = None
    if wf_files:
        engines: list[str] = []
        declares: dict[str, str] = {}
        for f in wf_files:
            if f.get("engine") and f["engine"] not in engines:
                engines.append(f["engine"])
            declares.update(f.get("declares") or {})
        workflow_manifest = {
            # ONE field, joined, rather than a singular plus a list — a repo carrying both
            # a Snakefile and a nextflow.config reads "nextflow/snakemake" and no consumer
            # has to decide which of two fields is authoritative.
            "engine": "/".join(engines),
            "paths": [f.get("path", "") for f in wf_files],
            "declares": declares,
        }
    # WORKFLOW-ONLY, not merely workflow-ISH. A repo may legitimately be both (a tool that
    # ships a Snakefile for its own CI, say), and for that one the install tiers are right.
    # The gate is: the repo declares itself a workflow AND carries nothing installable.
    workflow_only = bool(workflow_manifest and not (
        src.get("container_recipes") or src.get("env_specs") or src.get("build_scripts")))

    if workflow_only:
        _d = workflow_manifest["declares"]
        _named = " ".join(x for x in (_d.get("name"), _d.get("version")) if x)
        rec_txt = (f"THIS REPO IS A {workflow_manifest['engine'].upper()} WORKFLOW, NOT AN "
                   f"INSTALLABLE TOOL"
                   + (f" — it declares itself {_named}" if _named else "")
                   + f" ({', '.join(workflow_manifest['paths'])}, and no Dockerfile, "
                     f"env-spec or build script). It is RUN by an engine against a pinned "
                     f"revision; there is nothing here to install")
    elif author_image:
        tags = author_image.get("tags") or []
        where = (f"named in {src.get('readme') or 'the README'}" if author_image.get("declared_as")
                 else "under the authors' own name")
        rec_txt = (f"the authors publish an image ({author_image['ref']}, {where}"
                   + (f"; {author_image.get('tags_total', len(tags))} tags, among them "
                      f"{', '.join(tags[-5:])}" if tags else "")
                   + ") — adopt it by digest (highest fidelity, lowest cost); pin a release tag, not latest")
    elif incomplete:
        rec_txt = (f"build from the authors' {authors_recipe['path']}: "
                   + authors_recipe["summary"])
    elif authors_recipe:
        rec_txt = ("the authors ship a recipe but it installs nothing a registry route "
                   "would miss — a clean conda/pip install is the reliable least-resistance path")
    else:
        searched = {"tree": "root and tree", "root_only": "root only; the tree could not be listed",
                    "root": "root"}.get(src.get("recipe_search", "root"), "root")
        rec_txt = (f"no container recipe in the repository ({searched}), and no image under "
                   "the authors' name on ghcr.io, Docker Hub or quay.io — route by the "
                   "registry tiers (conda/pip/...) as usual")
    if not author_image and not image_error:
        # SCOPE THE NEGATIVE TO WHAT WAS ASKED. Three registries were asked for
        # `owner/repo`, plus whatever the README declares; an image published under some
        # other name was not asked for, and this verdict must not claim it does not exist.
        readme = src.get("readme")
        rec_txt += (" [NB: ghcr.io, Docker Hub and quay.io were asked for "
                    + (f"{owner}/{repo}" if owner and repo else "the repository's own name")
                    + (f" and for the image references in {readme}" if readme
                       else "; no README was read")
                    + " — an image published under another name is UNCHECKED, not ruled out]")
    if image_error:
        # The same rule at the verdict layer: "we couldn't check" must never be
        # delivered as "there is nothing there". Every sentence above assumes
        # author_image is a real observation; when the probe broke, say so in the
        # same breath.
        rec_txt += (f" [NB: the author-image probe FAILED ({image_error}) — 'no author "
                    f"image' here is UNCHECKED, not a negative finding]")

    out = {
        "author_image": author_image,
        "authors_recipe": authors_recipe,
        "env_specs": src.get("env_specs", []),
        "build_scripts": src.get("build_scripts", []),
        "reconstruction_incomplete": incomplete,
        "workflow_manifest": workflow_manifest,
        "workflow_only": workflow_only,
        "recipe_search": src.get("recipe_search", "root"),
        "recipe_candidates": list(src.get("recipe_candidates") or []),
        "declared_images": list(src.get("declared_images") or []),
        "recommendation": rec_txt,
    }
    if src.get("recipe_search_note"):
        out["recipe_search_note"] = src["recipe_search_note"]
    if image_error:
        out["author_image_error"] = image_error
    return out


# ---------------------------------------------------------------------------------------
# The gate, packaged for callers that hold a URL rather than an owner/repo pair.
# ---------------------------------------------------------------------------------------

_GIT_HOST_RE = re.compile(
    r"^(?:https?://|git@|ssh://git@)"
    r"(?P<host>[^/:]+)[/:]"
    r"(?P<owner>[^/]+)/"
    r"(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def parse_owner_repo(repo_url: str) -> Optional[tuple[str, str]]:
    """`(owner, repo)` for a GitHub URL, else None.

    None is a real answer, not a failure: `discover_authors_sources` reads exclusively from
    raw.githubusercontent.com, so for a GitLab, Bitbucket, self-hosted or archive URL there
    is no verdict to be had — and the caller must say UNCHECKED rather than assume clean.
    """
    m = _GIT_HOST_RE.match((repo_url or "").strip())
    if not m or m.group("host").lower() not in ("github.com", "www.github.com"):
        return None
    return m.group("owner"), m.group("repo")


def workflow_gate(repo_url: str, *, ref: str = "", tool: str = "",
                  assess: Optional[Callable[..., dict]] = None) -> dict:
    """Is the artifact at `repo_url` a WORKFLOW rather than an installable tool?

    Returns `{state, manifest?, recommendation?, reason?}` where state is one of:

      * ``"workflow_only"`` — the repo declares itself a Nextflow/Snakemake pipeline and
        carries nothing installable. The caller must refuse.
      * ``"installable"``   — checked, and it is not workflow-only.
      * ``"unchecked"``     — NOBODY LOOKED, with `reason` saying why (not a GitHub URL,
        or the probe raised). Distinct from "installable" on purpose: the discovery walk
        is GitHub-only, and reporting an unrun check as a clean bill of health is the
        absence-rounded-up-into-a-verdict shape this codebase refuses everywhere else.

    WHY THIS FUNCTION EXISTS. The router (`resolver.resolve`) reads `workflow_only`,
    withholds the `synthesis` and `source` tiers entirely and refuses with
    `artifact_is_a_workflow` — but the two MCP tools that IMPLEMENT the synthesis
    tier, `synth_fetch` / `synth_build`, sit directly behind that router and an agent
    can call them without passing through it. Without this check at the tools
    themselves, `synth_fetch` on a live pipeline repo (nf-core/rnaseq) returns proven
    — tens of files, ~66 KB — with `.devcontainer/setup.sh` top-ranked as the "most
    authoritative recipe" and neither main.nf nor nextflow.config in the corpus,
    while `assess_tool_sources` on the SAME repo returns `workflow_only: True` with a
    manifest naming the engine, both paths, and the pipeline's own name and version:
    a verdict that exists with nothing reading it.

    The corpus makes it quiet rather than loud. `synthesis.BUILD_SOURCES` has no pattern
    for `main.nf` / `nextflow.config` / `Snakefile`, so `is_build_relevant` filters the
    pipeline out of its own build corpus: the agent is handed 66 KB about the repository —
    CI lint jobs, the release-announcement tweet workflow, two READMEs — and not one byte
    of the pipeline. Every signal that would let a careful reader notice "this is a
    workflow" is precisely what was excluded.

    And the honesty layer cannot catch it, because it is answering a different question.
    Feeding `synth_build` the first real line of that top-ranked recipe —
    `echo "export PROMPT_DIRTRIM=2" >> $HOME/.bashrc` — returns `proven` with no
    violations on the `extracted` path, the one the docstring marks PREFERRED. The
    provenance contract is working exactly as designed: the command really does occur
    verbatim in the named file. **Provenance answers "did you make this up", never "is
    this the right thing".** So a dev-container shell-prompt tweak gets recorded as the
    install method for an RNA-seq pipeline, signed.

    The verdict is one HTTP walk away. This takes it.
    """
    parsed = parse_owner_repo(repo_url)
    if parsed is None:
        return {"state": "unchecked",
                "reason": (f"{repo_url!r} is not a github.com URL, and the workflow-manifest "
                           "walk reads raw.githubusercontent.com only. Whether this artifact "
                           "is a pipeline rather than an installable tool was NOT checked")}
    owner, repo = parsed
    fn = assess or assess_tool_sources
    try:
        a = fn(tool or repo, owner=owner, repo=repo, **({"ref": ref} if ref else {}))
    except Exception as e:
        return {"state": "unchecked",
                "reason": (f"the workflow-manifest walk for {owner}/{repo} raised "
                           f"{type(e).__name__}: {e} — whether this artifact is a pipeline "
                           "was NOT checked")}
    if not (a or {}).get("workflow_only"):
        return {"state": "installable"}
    return {"state": "workflow_only",
            "manifest": a.get("workflow_manifest") or {},
            "recommendation": a.get("recommendation") or ""}
