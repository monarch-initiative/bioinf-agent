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
        if not im.get("build_command") or not im.get("bin_path"):
            return {"error": f"source tool '{name}' is not replayable: install_method needs "
                             f"build_command + bin_path (re-run install_git_repo with bin_path)."}
        return {"spec": ic.source(name, im.get("source") or "",
                                  ref=im.get("commit_sha") or im.get("ref") or "",
                                  build_command=im.get("build_command"),
                                  bin_path=im.get("bin_path"), wrapper=name)}

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

    all_conda = plan_conda(conda_deps, non_conda)
    if all_conda:
        non_conda_names = {x.get("name", "") for x in non_conda}
        verify = [(t, f"command -v {t}") for t in primary_tools if t not in non_conda_names]
        eb.add_conda(all_conda, verify=verify)
    if pip_installs:
        versions = {p.get("name"): p.get("version") for p in _freeze.installed_packages(spec)
                    if isinstance(p, dict)}
        pip_specs, pip_verify = [], []
        for x in pip_installs:
            nm = x.get("name", "")
            v = versions.get(nm)
            pip_specs.append(f"{nm}=={v}" if v else nm)
            # presence via python's dist metadata — NOT `pip show` (pixi --pypi uses
            # uv, so the env has no pip binary), and dist-name based (robust to an
            # import-name mismatch like pyyaml->yaml).
            pip_verify.append((nm, f"python -c \"import importlib.metadata as _m; _m.version('{nm}')\""))
        eb.add_pip(pip_specs, verify=pip_verify)
    for s in tool_specs:
        eb.add_tool(s)

    result = eb.run()
    result["request_key"] = eb.request_key()
    return result
