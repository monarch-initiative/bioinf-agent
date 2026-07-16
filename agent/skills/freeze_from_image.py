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
        verifications.append({"label": tname, "tool": tname, "check": ev,
                              "passed": res["rc"] == 0, "rc": res["rc"],
                              "out": res["out"]})

    # -- SBOM captured FROM the shipped image (can't be faked) --
    resolved_packages: list[dict] = []
    system_packages: list[dict] = []
    try:
        from agent.skills.container_build import ContainerBuild as _CB
        resolved_packages = _CB.conda_sbom_from_image(image, platform) or []
        system_packages = _CB.apt_sbom_from_image(image, platform) or []
    except Exception:
        pass
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

    return proven("freeze_from_image.frozen", success=True, cache_hit=False,
                  request_key=rkey, image=image, image_digest=digest,
                  content_digest=digest, build_method=build_method, platform=platform,
                  verifications=verifications, **out_paths)
