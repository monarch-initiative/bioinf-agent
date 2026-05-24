"""
env_freeze — the container-native freeze: a spec/draft → a shipped env IMAGE.

This is the C3 teardown bridge that retires the host recipe zoo. The OLD path
(docker_builder.build_recipe + _recipe_dockerfile + nine `_emit_*`) translated a
spec's typed install_method records back into Dockerfile RUN steps for the host-
REPLAY locus (conda at /opt/conda, rustup/go-tarball, conda-activate). This module
instead routes each install_method to the SINGLE source of per-tier knowledge —
the install_commands generators — and builds on the container-native locus
(EnvBuild: install + validate IN the ship image, one generic bake, validated==
shipped). One emitter, not nine; the per-tier knowledge lives once (install_commands).

Toolchain-coupled tiers (cargo/go/perl) build with the ENGINE's toolchain, so the
required toolchain conda specs are injected automatically (rust / go / perl +
perl-app-cpanminus + c-/cxx-compiler for XS) — the container-native replacement
for the host path's rustup / go-tarball / conda-builddeps emitters.

`_map_install` (per-tier record → generator spec) and `plan_conda` (toolchain
injection) are pure given injected network fns, so they unit-test without docker or
the network. `build_env_image` drives a real ContainerBuild and is live-proven.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Callable, Optional

from agent.skills import freeze as _freeze
from agent.skills import install_commands as ic
from agent.skills import resolver as _resolver
from agent.skills.env_build import EnvBuild

# coupled tiers → the engine toolchain conda specs they need to BUILD in-container.
# (pip is NOT here — it's declared through the engine directly via add_pip, not a
# long-tail generator; it needs no extra toolchain.)
_TOOLCHAIN_SPECS = {
    "cargo":     ["rust"],
    "go":        ["go"],
    "perl":      ["perl", "perl-app-cpanminus", "c-compiler", "cxx-compiler"],
    "r_install": ["r-base", "c-compiler", "cxx-compiler", "fortran-compiler"],
}


def _pip_presence_check(name: str) -> str:
    """In-image presence for a pip dist via python's metadata — NOT `pip show`
    (pixi --pypi uses uv, so the env ships no pip binary), and dist-name based
    (robust to an import-name mismatch like pyyaml->yaml)."""
    return f"python -c \"import importlib.metadata as _m; _m.version('{name}')\""


def _conda_presence_check(name: str) -> str:
    """In-image presence for a conda tool: a CLI on PATH, ELSE the package's
    installed python dist-metadata. The second clause covers library-only packages
    (numpy, scipy, networkx, python-louvain) that have no CLI — and is dist-NAME
    based, so an import name that differs from the package name (python-louvain ->
    `import community`) doesn't matter. BOTH clauses name the package, so
    env_honesty.evidence_shape still anchors on the tool token (a `command -v X`
    / metadata probe on a clean image can only pass if X is genuinely installed —
    no anti-cheat weakening). CLI tools short-circuit on the first clause, so a
    pure-CLI env with no python never reaches the fallback."""
    return (f"command -v {name} || "
            f"python -c \"import importlib.metadata as _m; _m.distribution('{name}')\"")


def _r_source_from_method(im: dict) -> str:
    """Reconstruct install_r_package's source arg (cran|bioconductor|github:owner/
    repo) from the recorded install_method.source R expression."""
    expr = im.get("source", "") or ""
    m = re.search(r"install_github\(['\"]([^'\"]+)['\"]", expr)
    if m:
        return f"github:{m.group(1)}"
    if "BiocManager" in expr:
        return "bioconductor"
    return "cran"


def _map_install(
    x: dict,
    platform: str = "linux/amd64",
    *,
    resolve_linux_asset: Callable[..., dict] = _resolver.resolve_linux_asset,
    sha256_of_url: Callable[..., dict] = _resolver.sha256_of_url,
) -> dict[str, Any]:
    """Map ONE non-conda install_method record to an install_commands generator
    spec (the thing EnvBuild.add_tool consumes). Returns {"spec": <gen>} or
    {"error": <reason>} for a non-replayable record. Network (binary asset
    resolution) is injected so the mapping is unit-testable."""
    name = x.get("name", "")
    t = x.get("type", "")
    im = x.get("install_method") if isinstance(x.get("install_method"), dict) else {}

    if t == "jar":
        jar_url = im.get("source") or im.get("jar_url")
        if not jar_url:
            return {"error": f"jar tool '{name}' has no jar_url to replay"}
        hh = sha256_of_url(jar_url)              # jars rarely publish a checksum; best-effort
        return {"spec": ic.jar(name, jar_url, sha256=hh.get("sha256", "") if hh.get("ok") else "",
                               java_flags=im.get("java_flags"), wrapper=name)}

    if t == "source":
        # Run-by-path script collection (the half-baked academic norm: a repo of
        # scripts, no compiled binary) → script_repo: clone + a wrapper that execs
        # `{interpreter} {entry}`. No build_command/bin_path needed.
        if im.get("entrypoint"):
            return {"spec": ic.script_repo(name, im.get("source") or "",
                                           ref=im.get("commit_sha") or im.get("ref") or "",
                                           script_rel=im.get("entrypoint"),
                                           interpreter=im.get("interpreter") or "",
                                           wrapper=name)}
        if not im.get("build_command") or not im.get("bin_path"):
            return {"error": f"source tool '{name}' is not replayable: install_method needs "
                             f"build_command + bin_path (compiled tool) OR entrypoint "
                             f"(run-by-path script repo) — re-run install_git_repo with one."}
        return {"spec": ic.source(name, im.get("source") or "",
                                  ref=im.get("commit_sha") or im.get("ref") or "",
                                  build_command=im.get("build_command"),
                                  bin_path=im.get("bin_path"), wrapper=name)}

    if t == "synthesized":
        # The UNIVERSAL tail: a validated, provenance-tagged command sequence the
        # agent synthesized from the tool's OWN build files (gated at authoring by
        # synthesis.validate_submission). Replayed verbatim; no per-tool generator.
        cmds = im.get("commands") or []
        if not cmds:
            return {"error": f"synthesized tool '{name}' has no commands to replay"}
        return {"spec": ic.synthesized(name, cmds, tool=im.get("tool") or name,
                                       evidence=im.get("evidence") or "",
                                       engine_coupled=im.get("engine_coupled", False),
                                       repo=im.get("source") or "",
                                       commit=im.get("commit_sha") or "")}

    if t == "cargo":
        return {"spec": ic.cargo(name, im.get("crate") or name, version=im.get("version") or "",
                                 git_url=im.get("git_url") or "",
                                 binary_name=im.get("binary_name") or name)}

    if t == "go":
        return {"spec": ic.go(name, im.get("package") or name, version=im.get("version") or "latest",
                              binary_name=im.get("binary_name") or name)}

    if t == "perl":
        module = im.get("module") or name
        dist = im.get("distribution") or (im.get("source") or "").replace("cpanm ", "").strip() or module
        return {"spec": ic.perl_cpanm(module, distribution=dist,
                                      cpanm_flags=im.get("cpanm_flags") or "--notest",
                                      build_env=im.get("build_env") or "")}

    if t == "r_install":
        return {"spec": ic.r_package(name, source=_r_source_from_method(im))}

    if t == "binary":
        la = resolve_linux_asset(im.get("binary_url") or "")
        if not la.get("found"):
            return {"error": f"could not resolve a {platform} asset for binary '{name}': "
                             f"{la.get('reason')}", "available": la.get("available")}
        hh = sha256_of_url(la["url"])
        if not hh.get("ok"):
            return {"error": f"could not hash {platform} asset for '{name}': {hh.get('reason')}"}
        inner = PurePosixPath(im.get("local_path") or name).name
        return {"spec": ic.release_binary(name, la["url"], sha256=hh["sha256"],
                                          binary_in_archive=inner, wrapper=name)}

    return {"error": f"install_method.type {t!r} for '{name}' has no container-native generator"}


def plan_conda(conda_deps: list[str], non_conda: list[dict]) -> list[str]:
    """Conda specs for the env = the declared deps PLUS the engine toolchains the
    coupled tiers (cargo/go/perl) need to BUILD in-container. Pure. Order-stable,
    deduped (a toolchain already declared isn't doubled)."""
    have = set(conda_deps)
    extra: list[str] = []
    for x in non_conda:
        for spec in _TOOLCHAIN_SPECS.get(x.get("type", ""), []):
            if spec not in have:
                have.add(spec)
                extra.append(spec)
    return list(conda_deps) + extra


def ensure_python_for_pip(conda_specs: list[str], has_pip: bool) -> list[str]:
    """pip is installed via the engine's PyPI path (`pixi add --pypi` → uv), which
    needs a python interpreter IN the env. If pip specs are present but no conda
    package provides python, inject `python` — the pip analog of plan_conda's
    toolchain injection. (A bare `python` lets the solver choose; a transitively-
    provided python makes this a no-op via the explicit-match guard.)"""
    if not has_pip:
        return conda_specs
    if any(re.match(r"python($|[=<>!~ ])", s.strip()) for s in conda_specs):
        return conda_specs
    return list(conda_specs) + ["python"]


def build_env_image(
    spec: dict,
    *,
    name: str,
    version: str = "",
    conda_deps: Optional[list[str]] = None,
    channels: Optional[list[str]] = None,
    primary_tools: Optional[list[str]] = None,
    platform: str = "linux/amd64",
    accelerator: Optional[dict] = None,
    license_gated: bool = False,
    licenses: Optional[list[str]] = None,
    redistributable: bool = True,
    engine=None,
) -> dict[str, Any]:
    """Build a ship-platform env IMAGE from a spec/draft, container-native. Routes
    each non-conda install to its generator, injects coupled toolchains, and runs
    EnvBuild (which is gated by env_honesty.check_build → BUILT/VALIDATED_IN_IMAGE/
    POLICY_CLEAN). `platform` is a DOCKER platform ('linux/amd64'). Returns the
    EnvBuild BuildResult (+ 'request_key'); refuses early on a non-replayable record."""
    conda_deps = conda_deps or []
    primary_tools = primary_tools or []
    non_conda = _freeze.non_conda_installs(spec)

    # pip is declared THROUGH the engine (engine --pypi → into the lock), not as a
    # long-tail generator — partition it out. Everything else maps to a generator.
    pip_installs = [x for x in non_conda if x.get("type") == "pip"]
    tool_installs = [x for x in non_conda if x.get("type") != "pip"]

    # map every generator install up front so a non-replayable one fails BEFORE
    # we spin a build container.
    tool_specs = []
    for x in tool_installs:
        m = _map_install(x, platform)
        if "error" in m:
            return {"success": False, "stage": "map_install", "reason": m["error"],
                    "available": m.get("available")}
        tool_specs.append(m["spec"])

    eb = EnvBuild(name, version, platform=platform, engine=engine, channels=channels,
                  accelerator=accelerator, license_gated=license_gated,
                  licenses=licenses, redistributable=redistributable)

    all_conda = ensure_python_for_pip(plan_conda(conda_deps, non_conda), bool(pip_installs))
    if all_conda:
        non_conda_names = {x.get("name", "") for x in non_conda}
        verify = [(t, _conda_presence_check(t)) for t in primary_tools if t not in non_conda_names]
        eb.add_conda(all_conda, verify=verify)
    if pip_installs:
        versions = {p.get("name"): p.get("version") for p in _freeze.installed_packages(spec)
                    if isinstance(p, dict)}
        pip_specs, pip_verify = [], []
        for x in pip_installs:
            nm = x.get("name", "")
            v = versions.get(nm)
            pip_specs.append(f"{nm}=={v}" if v else nm)
            pip_verify.append((nm, _pip_presence_check(nm)))
        eb.add_pip(pip_specs, verify=pip_verify)
    for s in tool_specs:
        eb.add_tool(s)

    result = eb.run()
    result["request_key"] = eb.request_key()
    return result


# R needs its toolchain (compile C/C++/Fortran source pkgs) — the resolver-tier
# names for R, mapped to the engine specs (build_env_image uses the install_method
# 'r_install' key; this is the resolve→route path's equivalent).
_R_TIERS = {"cran", "bioconductor"}


def build_env_from_tools(
    name: str,
    tools: list[str],
    *,
    github_repos: Optional[dict] = None,
    languages: Optional[dict] = None,
    prefers: Optional[dict] = None,
    version: str = "",
    channels: Optional[list[str]] = None,
    platform: str = "linux/amd64",
    accelerator: Optional[dict] = None,
    license_gated: bool = False,
    licenses: Optional[list[str]] = None,
    redistributable: bool = True,
    engine=None,
    resolve_fn: Callable[..., dict] = _resolver.resolve,
) -> dict[str, Any]:
    """Declarative container-native build straight from TOOL NAMES — no host install.

    For each requested tool: resolve() the best tier, route() it to an EnvBuild
    action, and build the image (gated by env_honesty.check_build). This is the
    'call once per tool, get a trustworthy artifact' entry point — the container-
    native alternative to the host install→draft→freeze flow (and the Phase-E
    enabler). Covers the resolvable tiers (conda/pip/cran/bioconductor/binary/
    source); cargo/go/perl come via their install primitives → build_env_image.

    `github_repos`/`languages`/`prefers` are per-tool {tool: value} hints passed to
    resolve(). `resolve_fn` is injectable for testing. Refuses (success=False) on an
    ambiguous resolve or a tier with no container-native route, BEFORE building."""
    github_repos, languages, prefers = github_repos or {}, languages or {}, prefers or {}
    conda_specs: list[str] = []
    conda_verify: list[tuple[str, str]] = []
    pip_specs: list[str] = []
    pip_verify: list[tuple[str, str]] = []
    tool_actions: list[dict] = []
    needs_r = False

    for ts in tools:
        tool, _, ver = ts.replace("==", "=").partition("=")
        tool, ver = tool.strip(), ver.strip()
        d = resolve_fn(tool, version=ver, github_repo=github_repos.get(tool, ""),
                       language=languages.get(tool, ""), prefer=prefers.get(tool, ""))
        if d.get("ambiguous"):
            return {"success": False, "stage": "resolve", "tool": tool, "reason": d.get("rationale")}
        action = _resolver.route(d, platform)
        kind = action.get("kind")
        if kind == "conda":
            # Honor an EXPLICIT user pin (samtools=1.21); otherwise add the BARE
            # name and let the solver co-resolve compatible versions. Pinning every
            # auto-resolved package to its independent latest over-constrains the
            # co-solve (e.g. numpy=latest vs numba's older-numpy requirement →
            # python_abi conflict). The lock still records exact versions, so
            # reproducibility is preserved; bare names just hand the SAT problem to
            # the solver instead of pre-deciding it wrong.
            base = action["spec"].split("=")[0]
            conda_specs.append(f"{base}={ver}" if ver else base)
            conda_verify.append((tool, _conda_presence_check(tool)))
        elif kind == "pip":
            pip_specs.append(action["spec"])
            pip_verify.append((tool, _pip_presence_check(tool)))
        elif kind == "tool":
            tool_actions.append(action)
            if action.get("tier") in _R_TIERS:
                needs_r = True
        else:  # defer / no automatable tier
            return {"success": False, "stage": "route", "tool": tool,
                    "reason": action.get("reason"), "decision": d}

    if needs_r:  # R toolchain for the engine (compiles source CRAN/Bioc pkgs)
        for s in ("r-base", "c-compiler", "cxx-compiler", "fortran-compiler"):
            if s not in conda_specs:
                conda_specs.append(s)

    conda_specs = ensure_python_for_pip(conda_specs, bool(pip_specs))
    eb = EnvBuild(name, version, platform=platform, engine=engine, channels=channels,
                  accelerator=accelerator, license_gated=license_gated,
                  licenses=licenses, redistributable=redistributable)
    if conda_specs:
        eb.add_conda(conda_specs, verify=conda_verify)
    if pip_specs:
        eb.add_pip(pip_specs, verify=pip_verify)
    for action in tool_actions:
        eb.add_tool(action["spec"])

    result = eb.run()
    result["request_key"] = eb.request_key()
    return result
