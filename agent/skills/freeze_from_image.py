"""
freeze_from_image — freeze an env from an EXISTING image (the authors' own, or one we
built from their Dockerfile), instead of reconstructing it from conda/pip.

This is the executor for the authors-recipe-first path (see [[feedback-prioritize-authors-
own-env-recipe]] + [[project-env-recipe-always]]). A human handed a tool that ships its
own image or Dockerfile would USE it; this makes the agent do the same as a first-class
primitive rather than a hand-driver (it generalizes the Talos ENV-report hand-driver).

The honesty contract is UNCHANGED — the image still has to earn its registration:
  BUILT               the image + digest resolve in the local daemon
  VALIDATED_IN_IMAGE  every requested tool's evidence command RUNS green IN the image
                      (and references the tool as a real token — echo/print cheats
                      rejected by env_honesty.evidence_shape_violation). The caller
                      supplies evidence that EXERCISES the tool, not merely imports it —
                      the exact gap that let a reconstruction pass unit tests yet not run.
  POLICY_CLEAN        accelerator + license firewall (I12/I13)

Two entry modes, one code path:
  • adopt-image        — an author-published image, adopted by digest (no build).
  • authors-dockerfile — an image we built from the tool's own Dockerfile at a pinned
                         commit (build_env_from_authors_recipe builds it, then calls here).

Deliverables are rendered PURELY from the verified record, same as freeze(): ENV.html +
attestation.json + recipe.yaml + recipe.md (the recipe records the authors' image/source,
so anyone can reproduce it). Docker required.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional

_shq = shlex.quote

from agent.skills import env_recipe, env_recipe_render
from agent.skills.outcomes import proven, refused, broke


def _sh(argv: list[str], timeout: int = 300) -> dict:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"rc": r.returncode, "out": r.stdout or "", "err": r.stderr or ""}
    except subprocess.TimeoutExpired as e:
        return {"rc": 124, "out": "", "err": f"timed out after {e.timeout}s"}
    except FileNotFoundError as e:
        return {"rc": 127, "out": "", "err": str(e)}


def _image_present(image: str) -> bool:
    return _sh(["docker", "image", "inspect", image], timeout=60)["rc"] == 0


def _image_digest(image: str) -> str:
    r = _sh(["docker", "image", "inspect", "--format", "{{index .Id}}", image], timeout=60)
    return r["out"].strip() if r["rc"] == 0 else ""


def _run_in_image(image: str, platform: str, command: str, timeout: int = 300,
                  maxlen: int = 400) -> dict:
    """Run a command in the image, return rc + captured output. Uses `bash -c`, NOT
    `bash -lc`: a login shell sources /etc/profile which can CLOBBER the image's own
    `ENV PATH` (e.g. a uv/conda venv baked at the front of PATH) — so `-lc` would fail to
    find the very tools we're validating. `-c` respects the image's environment.

    `maxlen` caps the returned output (evidence snippets stay short); pass a large value
    when the CALLER must parse the whole output (e.g. an SBOM JSON list) — truncating that
    mid-list would break the parse."""
    r = _sh(["docker", "run", "--rm", "--platform", platform, image, "bash", "-c", command],
            timeout=timeout)
    return {"rc": r["rc"], "out": (r["out"] or r["err"] or "").strip()[:maxlen]}


def freeze_from_image(
    *,
    image: str,
    tools: list[dict],
    name: str,
    env_cache,
    reports_dir: str | Path,
    version: str = "",
    platform: str = "linux/amd64",
    build_method: str = "adopt-image",
    dockerfile_source: Optional[dict] = None,
    request_key: str = "",
    accelerator: Optional[dict] = None,
    gated: bool = False,
    licenses: Optional[list[str]] = None,
    pull_if_absent: bool = True,
) -> dict[str, Any]:
    """Register an EnvCache entry from an existing `image`, gated by the honesty contract.

    `tools`: [{name, evidence}] — each evidence command must RUN the tool in-image and
    exit 0 (this is VALIDATED_IN_IMAGE; the caller owns making it exercise, not import).
    `build_method`: 'adopt-image' | 'authors-dockerfile'. `dockerfile_source`: the pinned
    source {repo, commit, tag, dockerfile?} when built from the authors' Dockerfile — it
    is embedded in the recipe so the build is reproducible.

    Returns proven(...) with the record + deliverable paths, or refused/broke on a missing
    image / honesty violation. Docker required."""
    if not tools:
        return refused("freeze_from_image.no_tools",
                       error="declare at least one tool with an evidence command that RUNS it in-image")
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # -- BUILT: ensure the image resolves locally (pull if allowed) --
    if not _image_present(image):
        if not pull_if_absent:
            return broke("freeze_from_image.image_absent",
                         error=f"image {image!r} is not in the local daemon and pull is disabled")
        pl = _sh(["docker", "pull", "--platform", platform, image], timeout=1800)
        if pl["rc"] != 0 or not _image_present(image):
            return broke("freeze_from_image.pull_failed",
                         error=f"could not pull {image!r}: {pl['err'][:300]}")
    digest = _image_digest(image)

    # -- VALIDATED_IN_IMAGE: run each tool's evidence IN the image --
    verifications: list[dict] = []
    for t in tools:
        tname = (t.get("name") or "").strip()
        ev = (t.get("evidence") or "").strip()
        if not tname or not ev:
            return refused("freeze_from_image.bad_tool",
                           error=f"each tool needs a name + evidence command (got {t!r})")
        res = _run_in_image(image, platform, ev)
        from agent.skills import env_honesty as _eh
        verifications.append({"label": tname, "tool": tname, "check": ev,
                              "passed": res["rc"] == 0, "rc": res["rc"],
                              "out": res["out"],
                              # DISCLOSURE (not a gate): how deeply this evidence exercises
                              # the tool. 'version'/'import'/'help' prove presence; only
                              # 'smoke'/'functional' prove it RUNS. Surfaced so a shallow
                              # proof can't masquerade as a functional one.
                              "depth": _eh.evidence_depth(ev, tname)})

    # -- SBOM captured FROM the shipped image (can't be faked) --
    # A capture FAILURE must not look like an image with no dependencies: an empty
    # resolved_packages renders as "0 along for the ride" in the ENV report and as an
    # empty package list in the attestation — absence of data reading as data. So each
    # probe records WHY it came back empty, the same way the authors gate records
    # authors_gate_error and cluster accounting records sacct_error. Not a refusal: some
    # images legitimately carry no conda prefix and no apt layer.
    resolved_packages: list[dict] = []
    system_packages: list[dict] = []
    sbom_errors: list[str] = []
    from agent.skills.container_build import ContainerBuild as _CB
    try:
        resolved_packages = _CB.conda_sbom_from_image(image, platform) or []
    except Exception as e:
        sbom_errors.append(f"conda: {type(e).__name__}: {e}")
    try:
        # separate try — an apt failure must not also discard a good conda closure
        system_packages = _CB.apt_sbom_from_image(image, platform) or []
    except Exception as e:
        sbom_errors.append(f"apt: {type(e).__name__}: {e}")
    # pip-only / venv images (no conda prefix) — best-effort importlib.metadata SBOM,
    # as a single-line `python -c` (a heredoc through `docker run bash -c` is fragile).
    if not resolved_packages:
        probe = ("import importlib.metadata as m,json;"
                 "print(json.dumps(sorted(set(d.metadata['Name']+'=='+d.version "
                 "for d in m.distributions() if d.metadata['Name']))))")
        pj = _run_in_image(image, platform, f"python -c {_shq(probe)}", timeout=120, maxlen=200_000)
        if pj["rc"] == 0 and "[" in pj["out"]:
            try:
                lst = json.loads(pj["out"][pj["out"].index("["):])
                resolved_packages = [{"name": s, "manager": "pip"} for s in lst]
            except (ValueError, IndexError):
                pass

    # -- assemble the record + run the honesty contract --
    primary = tools[0]["name"]
    version = version or ""
    rkey = request_key or f"{primary}={version or (digest.split(':')[-1][:12])}|{platform}|none"
    mode = "adopt" if build_method == "adopt-image" else "build"
    from agent.skills import freeze as _freeze
    record = _freeze.freeze_record(
        request_key=rkey, content_digest=digest, mode=mode,
        image=image, image_digest=digest, platform=platform, gated=gated)
    record["name"] = name
    record["version"] = version
    record["build_method"] = build_method
    record["requested_tools"] = [t["name"] for t in tools]
    record["verifications"] = verifications
    record["resolved_packages"] = resolved_packages
    record["system_packages"] = system_packages
    if sbom_errors and not resolved_packages:
        # only surfaced when the SBOM is ACTUALLY empty — a conda probe that failed on an
        # image the importlib fallback then read successfully is not a gap worth flagging.
        record["sbom_error"] = "; ".join(sbom_errors)
    record["accelerator"] = accelerator
    record["licenses"] = list(licenses or [])
    record["redistributable"] = not gated
    if dockerfile_source:
        record["dockerfile_source"] = dict(dockerfile_source)
    record["shipped_binaries"] = [
        {"command": t["name"],
         "provenance": f"validated in the {build_method} image (evidence: {t['evidence'][:60]})"}
        for t in tools]

    from agent.skills import env_honesty
    violations = env_honesty.check_build(record)
    if violations:
        return refused("freeze_from_image.honesty_violation",
                       error=f"the image failed the honesty contract ({len(violations)} violation(s)) — "
                             "not registered",
                       honesty_violations=violations, verifications=verifications)

    # -- register + deliverables (rendered purely from the record) --
    env_cache.register(rkey, record)
    recipe = env_recipe.extract_recipe(
        None, name=name, version=version, conda_deps=[],
        primary_tools=[t["name"] for t in tools], platform=platform,
        accelerator=accelerator, license_gated=gated, licenses=licenses,
        redistributable=not gated, content_digest=digest,
        build_method=("authors-dockerfile" if build_method == "authors-dockerfile" else "adopt"),
        adopt_image=(image if build_method == "adopt-image" else ""),
        dockerfile_source=dockerfile_source or {})
    recipe["shipped_binaries"] = record["shipped_binaries"]

    out_paths: dict[str, str] = {}
    for label, fname, render in (
        ("env_report", f"{name}.ENV.html",
         lambda: __import__("agent.skills.env_report_html", fromlist=["render_env_report_html"])
                 .render_env_report_html(record)),
        ("attestation", f"{name}.attestation.json",
         lambda: json.dumps(__import__("agent.skills.attestation", fromlist=["build_attestation"])
                            .build_attestation(record, base_image=""), indent=2)),
        ("recipe", f"{name}.recipe.yaml",
         lambda: __import__("yaml").safe_dump(recipe, sort_keys=False)),
        ("recipe_md", f"{name}.recipe.md",
         lambda: env_recipe_render.render_recipe_markdown(recipe, record)),
    ):
        try:
            (reports_dir / fname).write_text(render())
            out_paths[label] = str(reports_dir / fname)
        except Exception as e:
            out_paths[label] = f"({label} render failed: {e!r})"

    # SOFT advisory (never a refusal): requested tools whose evidence only proved
    # presence/loads, not that the tool RUNS. Surfaced so the agent can strengthen the
    # evidence — the honest nudge against a shallow proof reading as functional.
    shallow = [v["tool"] for v in verifications if v.get("depth") in ("version", "import", "help")]
    advisory = ""
    if shallow:
        advisory = ("shallow evidence (proves presence, not function) for: "
                    + ", ".join(shallow) + " — consider evidence that RUNS the tool on an input")
    return proven("freeze_from_image.frozen", success=True, cache_hit=False,
                  request_key=rkey, image=image, image_digest=digest,
                  content_digest=digest, build_method=build_method, platform=platform,
                  verifications=verifications, shallow_evidence=shallow,
                  evidence_advisory=advisory, **out_paths)


def _clone_url(repo: str) -> str:
    """Normalize a repo argument to a git-cloneable URL. An explicit scheme
    (http/https/git@/file://) is used verbatim; a bare 'owner/repo' is GitHub."""
    return repo if repo.startswith(("http://", "https://", "git@", "file://")) else f"https://github.com/{repo}"


def build_from_authors_recipe(
    *,
    repo: str,
    tools: list[dict],
    name: str,
    env_cache,
    reports_dir: str | Path,
    recipe: str = "Dockerfile",
    ref: str = "",
    version: str = "",
    platform: str = "linux/amd64",
    build_args: Optional[dict] = None,
    gated: bool = False,
    licenses: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Clone the tool's OWN repo at a pinned `ref`, `docker build` its `recipe` (their
    Dockerfile), then hand the built image to freeze_from_image (same honesty contract +
    deliverables). The declarative executor for resolve_tool's `authors_recipe` tier — the
    path taken when the authors' recipe installs pieces a conda/pip reconstruction would
    silently DROP. The pinned source (repo + resolved commit + tag + Dockerfile verbatim)
    is recorded so the build is reproducible. Docker + git (+ network for a remote repo).

    Kept a thin, injectable executor (env_cache + reports_dir params) so it mirrors
    freeze_from_image and is testable on real bytes without the MCP singletons."""
    if not tools:
        return refused("authors_recipe.no_tools",
                       error="declare at least one tool with an evidence command that RUNS it in-image")
    if not (repo or "").strip():
        return refused("authors_recipe.no_repo", error="repo required ('owner/repo' or a git URL)")

    import tempfile as _tf
    url = _clone_url(repo)
    with _tf.TemporaryDirectory(prefix="authors_recipe_") as td:
        cl = _sh(["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else []) + [url, td], timeout=600)
        if cl["rc"] != 0:
            # --branch fails on a raw commit SHA; retry a full clone + checkout
            cl2 = _sh(["git", "clone", url, td], timeout=600)
            if cl2["rc"] != 0:
                return broke("authors_recipe.clone_failed",
                             error=f"could not clone {url}: {(cl2['err'] or cl['err'])[:300]}")
            if ref:
                co = _sh(["git", "-C", td, "checkout", ref], timeout=120)
                if co["rc"] != 0:
                    return broke("authors_recipe.checkout_failed",
                                 error=f"could not checkout {ref!r}: {co['err'][:300]}")
        commit = _sh(["git", "-C", td, "rev-parse", "HEAD"], timeout=60)["out"].strip()
        tag = f"{name}:{version}" if version else f"{name}:latest"
        buildx = ["docker", "buildx", "build", "--platform", platform, "--load", "-f", f"{td}/{recipe}", "-t", tag]
        for k, v in (build_args or {}).items():
            buildx += ["--build-arg", f"{k}={v}"]
        buildx.append(td)
        bd = _sh(buildx, timeout=3600)
        if bd["rc"] != 0:
            return broke("authors_recipe.build_failed",
                         error=f"docker build of {recipe} failed: {(bd['err'] or '')[-800:]}",
                         dockerfile=recipe)
        try:
            dockerfile_text = (Path(td) / recipe).read_text()
        except OSError:
            dockerfile_text = ""

    return freeze_from_image(
        image=tag, tools=[dict(t) for t in tools], name=name, version=version,
        platform=platform, build_method="authors-dockerfile",
        dockerfile_source={"repo": url, "commit": commit, "tag": ref or "", "dockerfile": dockerfile_text},
        gated=gated, licenses=list(licenses or []), pull_if_absent=False,
        env_cache=env_cache, reports_dir=reports_dir)
