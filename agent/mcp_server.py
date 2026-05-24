"""
Bioinformatics Agent — MCP Server

Exposes all pipeline execution capabilities as MCP tools so Claude Code
can drive orchestration directly using your Claude subscription, with no
separate Anthropic API credits required.

Start with:
    python -m agent.mcp_server

Or register in .claude/settings.json (already done) so Claude Code
starts it automatically.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config + skill singletons (initialised once at server startup)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.resolve()


def _load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "agent_config.yaml") as f:
        return yaml.safe_load(f)


config = _load_config()

from agent.skills.package_search import PackageSearch
from agent.skills.env_manager import EnvManager
from agent.skills.test_runner import TestRunner
from agent.skills.docker_builder import DockerBuilder
from agent.skills import biocontainers as _biocontainers
from agent.skills import env_freeze as _env_freeze
from agent.skills import freeze as _freeze
from agent.skills import resolver as _resolver
from agent.skills import user_guide as _user_guide
from agent.skills.core_test_data import add_core_test_data as _add_core_test_data
from agent.skills.core_test_data import add_phenopacket as _add_phenopacket
from agent.skills.core_test_data import phenopacket_to_vcf as _phenopacket_to_vcf
from agent.validators.output_validator import OutputValidator
from agent.skills.spec_writer import write_provenance as _write_provenance
from agent.skills.resources import list_resources as _list_resources
from agent.skills.resources import list_pipelines as _list_pipelines
from agent.skills.pipeline_state import PipelineState
from agent.skills.job_manager import JobManager

_pkg_search     = PackageSearch(config)
_env_mgr        = EnvManager(config)
_test_runner    = TestRunner(config)
_docker         = DockerBuilder(config)
_validator      = OutputValidator(config)
_pipeline_state = PipelineState(config)
_job_manager    = JobManager(config)
_env_cache      = _freeze.EnvCache(_env_mgr.project_root / "env_reports" / "_env_cache.json")

# Reap stale PID files from prior agent sessions whose owning process has
# already exited. Living services owned by other processes are left alone.
_orphan_reap = EnvManager.cleanup_orphan_service_pids()
if _orphan_reap.get("removed"):
    print(f"[bioinf] reaped {len(_orphan_reap['removed'])} orphan service PID file(s): "
          f"{_orphan_reap['removed']}", file=sys.stderr)

mcp = FastMCP("bioinf-agent")

# freeze()'s `platform` is a conda subdir (linux-64); buildx/recipe builds want a
# docker platform (linux/amd64). Map the ones we ship to.
_CONDA_TO_DOCKER_PLATFORM = {
    "linux-64": "linux/amd64",
    "linux-aarch64": "linux/arm64",
    "linux-arm64": "linux/arm64",
}

# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

@mcp.tool()
def search_package(
    package_name: str,
    requested_version: str = "latest",
    pipeline_id: str = "",
) -> dict:
    """Search anaconda.org / bioconda / conda-forge / PyPI for a bioinformatics package.
    Returns channel, exact version, conda spec, install command, and brief description.

    `packages` in the final spec is rebuilt at finalize-time from the live
    conda env + install_steps' installed_packages (the source of truth),
    NOT from search_package. So this tool is query-only: use the returned
    versions/channels to choose what to actually install via
    `install_conda_packages` / `run_install_command`.

    If pipeline_id is supplied, description/homepage/input_types/output_types
    are cached on `draft.search_cache[package_name]` so the finalize pass can
    annotate the derived package record without re-querying."""
    result = _pkg_search.search(package_name, requested_version)
    if pipeline_id and result.get("found"):
        name = result.get("package_name", package_name)
        cache_entry = {
            "description":   result.get("description"),
            "homepage":      result.get("home"),
            "input_types":   result.get("input_types", []),
            "output_types":  result.get("output_types", []),
            "check_command": result.get("check_command"),
        }
        cache_entry = {k: v for k, v in cache_entry.items() if v}
        ok = _pipeline_state.cache_search_result(pipeline_id, name, cache_entry)
        result["pipeline_merge"] = (
            {"status": "cached", "pipeline_id": pipeline_id, "name": name}
            if ok else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result

# ---------------------------------------------------------------------------
# Environment management
# ---------------------------------------------------------------------------

@mcp.tool()
def resolve_tool(
    tool: str,
    version: str = "",
    github_repo: str = "",
    prefer: str = "",
    language: str = "",
) -> dict:
    """Decide WHICH install tier to use for `tool`, and record WHY (the
    ResolutionDecision). Probes availability independently per tier
    (conda/bioconda, PyPI, CRAN — plus release-binary/source when github_repo is
    given) and ranks by the preference order conda > pip/cran/bioconductor >
    binary > source > manual (reproducibility + clean containerization + least
    build fragility).

    Returns {chosen, install_call, rationale, alternatives, ambiguous, probed,
    …}: the concrete install primitive call to make, why it was chosen over the
    others, and the rejected-but-available alternatives.

    DISAMBIGUATION: bare tool names collide across registries (PyPI `ape` ≠
    CRAN's R `ape`). Pass `language` ('python'|'r') to restrict the search to one
    ecosystem; with no hint, a name found in both PyPI and CRAN comes back
    `ambiguous: true`. `prefer` forces a tier when available. `github_repo`
    ('owner/repo') unlocks the binary/source tiers.

    Query-only: it does NOT install. Use the returned install_call with the
    matching primitive, then freeze().
    """
    return _resolver.resolve(tool, version=version, github_repo=github_repo,
                             prefer=(prefer or None), language=language)


@mcp.tool()
def create_conda_env(
    env_name: str,
    python_version: str = "",
    pipeline_id: str = "",
    subdir: str = "",
) -> dict:
    """Create a new isolated conda environment.

    `subdir` pins the env's platform when a tool has no native build on the
    host arch — most commonly `subdir="osx-64"` on Apple Silicon for
    osx-64-only bioconda packages (plink, plink2, older C/C++ tools), which
    then run under Rosetta 2. The subdir is persisted to the env's .condarc so
    every subsequent install_conda_packages into this env honors it. Check
    search_package's `installable_on_current_platform` / `available_subdirs`
    first; if the host arch is missing but osx-64/linux-64 exists, create with
    that subdir.

    If pipeline_id is supplied, draft.conda_env and draft.python_version are
    set, and an entry is appended to draft.install_steps recording the env
    creation so the install journey is captured chronologically."""
    pv = python_version or config["conda"]["python_version"]
    result = _env_mgr.create(env_name, python_version=pv, subdir=subdir or None)
    if pipeline_id:
        _pipeline_state.set_conda_env(pipeline_id, env_name, python_version=pv)
        cmd = f"conda create --prefix envs/{env_name} python={pv}"
        if subdir:
            cmd = f"CONDA_SUBDIR={subdir} {cmd}  # + conda config --env --set subdir {subdir}"
        idx = _pipeline_state.add_install_step(pipeline_id, {
            "tool":               "conda",
            "subcommand":         "create",
            "purpose":            f"Create the {env_name} conda environment" + (f" (subdir={subdir})" if subdir else ""),
            "command":            cmd,
            "returncode":         0 if result.get("success") else 1,
            "installed_packages": [{"name": "python", "version": pv, "channel": "conda-forge"}],
        })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_conda_packages(
    env_name: str,
    packages: list[dict],
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install conda packages (bioconda / conda-forge / defaults) into a conda env.
    packages: list of {spec: str, channel: str}, e.g. [{spec: 'samtools=1.21', channel: 'bioconda'}]
    conda-pack is added automatically.

    If pipeline_id is supplied, an entry is appended to draft.install_steps with
    installed_packages parsed from each spec (spec='samtools=1.21' → name=samtools
    version=1.21). conda-pack is filtered out of installed_packages since it is
    infrastructure, not a user-facing package.

    Pass `step=N` to replace install_step N (for retries after a solver
    conflict, etc). Default is append — same semantics as run_install_command."""
    result = _env_mgr.install(env_name, packages)
    if pipeline_id:
        # Record the env this installed into. install() auto-creates the env when
        # absent (a supported path), so an agent that skips create_conda_env would
        # otherwise leave draft.conda_env unset — breaking finalize + the guide.
        _pipeline_state.set_conda_env(pipeline_id, env_name)
        from agent.skills.env_manager import parse_conda_spec
        installed: list[dict] = []
        for pkg in packages:
            parsed = parse_conda_spec(pkg.get("spec", ""))
            name = parsed["name"]
            if not name or name == "conda-pack":
                continue
            entry = {"name": name, "channel": pkg.get("channel", "")}
            if parsed["version"]:
                entry["version"] = parsed["version"]
            if parsed["constraint"] and parsed["constraint"] not in ("=", "=="):
                entry["version_constraint"] = parsed["constraint"]
            installed.append(entry)
        idx = _pipeline_state.add_install_step(pipeline_id, {
            "tool":               "conda",
            "subcommand":         "install",
            "purpose":            f"Install {len(installed)} package(s) into {env_name}",
            "command":            "conda install " + " ".join(p.get("spec", "") for p in packages),
            "returncode":         result.get("returncode"),
            "installed_packages": installed,
        }, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_git_repo(
    env_name: str,
    repo_url: str,
    tool_name: str,
    ref: str = "",
    build_command: str = "",
    verify_command: str = "",
    bin_path: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Vendor a git repository as a source-installed tool (the clone-and-run
    pattern that conda/pip/jar primitives don't cover — e.g. an academic repo
    you run as `python run_thing.py …`).

    Clones repo_url into {env}/share/{tool_name}, checks out `ref` (branch /
    tag / commit; default HEAD), resolves the commit SHA (the immutable content
    anchor), optionally runs `build_command` (e.g. `pip install -e .`, `make`)
    and a `verify_command` smoke test inside the env at the clone dir.

    If pipeline_id is supplied, records an install_step (tool=git,
    subcommand=clone) whose installed_packages entry carries
    install_method.type="source" with repo_url + commit_sha + local_path, and
    caches a verification (verify_command/verify_output) so the derived
    PackageRecord satisfies I2. The clone path + commit_sha are re-checked at
    finalize by I11 (source_install_present).

    `bin_path` (relative path to the built executable in the clone, e.g.
    "seqtk") makes a PATH wrapper at {env}/bin/{tool_name} and is recorded with
    build_command so freeze can REPLAY the build in the ship image. Omit for
    run-by-path script repos.

    Pin `ref` to a tag or commit for reproducibility — a bare default branch
    drifts. Returns: {success, clone_path, commit_sha, repo_url, ref,
    build_command, bin_path, wrapper_path, verify_command, verify_output, log}.
    """
    result = _env_mgr.install_git_repo(
        env_name       = env_name,
        repo_url       = repo_url,
        tool_name      = tool_name,
        ref            = ref,
        build_command  = build_command,
        verify_command = verify_command,
        bin_path       = bin_path,
    )
    if pipeline_id:
        from urllib.parse import urlparse
        host = urlparse(repo_url).netloc or ""
        channel = "github" if "github.com" in host else "git"
        install_method = {
            "type":       "source",
            "source":     repo_url,
            "commit_sha": result.get("commit_sha"),
            "ref":        result.get("ref") or (ref or "HEAD"),
            "local_path": result.get("clone_path"),
            # Recorded so freeze can REPLAY the build on the ship platform.
            "build_command": build_command,
            "bin_path":      bin_path,
        }
        ip_record = {
            "name":           tool_name,
            "channel":        channel,
            "source":         repo_url,
            "install_method": install_method,
        }
        if result.get("commit_sha"):
            ip_record["version"] = result["commit_sha"][:12]
        step_data = {
            "tool":        "git",
            "subcommand":  "clone",
            "purpose":     f"Vendor {tool_name} from {host or repo_url} (source install)",
            "command":     f"git clone {repo_url}" + (f" @ {ref}" if ref else ""),
            "returncode":  0 if result.get("success") else 1,
            "installed_packages": [ip_record],
        }
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # Cache the verify so the finalize-time package derivation attaches it
        # (mirrors verify_installation). Source tools don't go through the
        # registry-anchored verify() — their anchor is commit_sha + on-disk path.
        if result.get("success") and result.get("verify_output"):
            _pipeline_state.cache_verification(pipeline_id, tool_name, {
                "verify_command": result.get("verify_command"),
                "verify_output":  result.get("verify_output"),
            })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_release_binary(
    env_name: str,
    tool_name: str,
    url: str,
    sha256: str = "",
    binary_in_archive: str = "",
    wrapper_name: str = "",
    verify_command: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a precompiled release binary (Tier-3): the static-binary pattern
    conda/pip/jar/source don't cover — mosdepth, somalier, slivar, sylph,
    dorado, cellranger. Downloads the asset, anchors it by sha256 (a mismatch is
    a HARD FAIL), extracts if it's a tar/zip, chmods the executable, and writes a
    PATH launcher at {env}/bin/{wrapper_name or tool_name}.

    sha256: pass the published checksum of the ASSET (the .tar.gz/.zip the
    publisher ships) to guarantee you got the exact download — verified before
    extraction and recorded as install_method.asset_sha256. The EXTRACTED binary
    is separately hashed into install_method.sha256 — that's the anchor I14
    re-hashes on disk at finalize (for an archive, the runnable binary differs
    from the tarball; for a single-binary download they coincide).

    binary_in_archive: for archive assets, the executable's path inside it
    (basename accepted); omit for single-binary downloads.

    A smoke verify runs after install ({tool} --version/--help by default, or
    your verify_command) — this is what catches a wrong-ARCHITECTURE binary that
    is present on disk (and so passes I14) but cannot execute. With pipeline_id,
    records an install_step (install_method.type="binary" + binary_url + sha256 +
    local_path) and caches the verify so the derived PackageRecord satisfies I2.

    Returns: {success, binary_path, wrapper_path, url, sha256, verify_command,
    verify_output, install_method, log}.
    """
    result = _env_mgr.install_release_binary(
        env_name          = env_name,
        tool_name         = tool_name,
        url               = url,
        sha256            = sha256,
        binary_in_archive = binary_in_archive,
        wrapper_name      = wrapper_name,
    )
    if not result.get("success"):
        return result

    launcher = wrapper_name or tool_name
    vcmd = verify_command or (
        f"{launcher} --version 2>&1 || {launcher} --help 2>&1 || {launcher} -h 2>&1"
    )
    vres = _env_mgr.run_in_env(env_name, vcmd, timeout=120)
    verify_ok = vres.get("returncode") == 0
    verify_output = ((vres.get("stdout") or "") + (vres.get("stderr") or "")).strip()[:200]
    result["verify_command"] = vcmd
    result["verify_output"]  = verify_output
    if not verify_ok:
        result["success"] = False
        result["verify_failed"] = True
        result["stderr"] = (result.get("stderr") or "") + (
            f"\n[verify failed: `{vcmd}` rc={vres.get('returncode')} — the binary is on disk "
            f"but did not execute; likely the wrong architecture/libc for this platform]"
        )

    if pipeline_id:
        install_method = result.get("install_method") or {
            "type": "binary", "binary_url": url,
            "sha256": result.get("sha256"), "local_path": result.get("binary_path"),
        }
        ip_record = {
            "name":           tool_name,
            "channel":        "binary",
            "source":         url,
            "install_method": install_method,
        }
        step_data = {
            "tool":        "curl",
            "subcommand":  "download",
            "purpose":     f"Install release binary {tool_name} from {url}",
            "command":     f"curl -L -o {tool_name} {url}",
            "returncode":  0 if result.get("success") else 1,
            "installed_packages": [ip_record],
        }
        if verify_ok:
            step_data["verify_command"] = vcmd
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        if verify_ok and verify_output:
            _pipeline_state.cache_verification(pipeline_id, tool_name, {
                "verify_command": vcmd,
                "verify_output":  verify_output,
            })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


def _merge_simple_install(pipeline_id, step, result, name, channel, tool, command, purpose):
    """Shared draft-merge for the single-package install tiers (perl/cargo/go):
    record one install_step carrying the install_method, and cache the verify so
    the derived PackageRecord satisfies I2. Returns the pipeline_merge dict."""
    install_method = result.get("install_method") or {}
    ip_record = {
        "name": name, "channel": channel,
        "source": install_method.get("source", ""),
        "install_method": install_method,
    }
    step_data = {
        "tool": tool, "subcommand": "install", "purpose": purpose,
        "command": command,
        "returncode": 0 if result.get("success") else 1,
        "installed_packages": [ip_record],
    }
    if result.get("success") and result.get("verify_command"):
        step_data["verify_command"] = result["verify_command"]
    idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
    if result.get("success") and result.get("verify_output"):
        _pipeline_state.cache_verification(pipeline_id, name, {
            "verify_command": result.get("verify_command"),
            "verify_output":  result.get("verify_output"),
        })
    return (
        {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
        if idx is not None else
        {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
    )


@mcp.tool()
def install_perl_package(
    env_name: str,
    module: str,
    distribution: str = "",
    cpanm_flags: str = "",
    build_env: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Perl/CPAN module via cpanm (Tier: perl) — Ensembl VEP, BioPerl,
    and other Perl tools conda/pip don't cover. Requires perl + cpanm in the env
    (install conda packages `perl` and `perl-app-cpanminus` first).

    `module` is the Perl package name (e.g. Bio::DB::HTS) used for the
    `perl -M{module} -e1` load-or-die verify — the registry anchor for cpanm
    modules (tracked by neither conda nor pip). Set `distribution` when the CPAN
    distribution name differs from the module name. `build_env` = space-separated
    KEY=VAL exports for XS builds that link a conda C lib (e.g.
    "HTSLIB_DIR=$CONDA_PREFIX" for Bio::DB::HTS) — use $CONDA_PREFIX so freeze's
    recipe replay resolves it inside the SHIP image. With pipeline_id, records the
    install_step (install_method.type="perl", recorded for replay) + caches the
    verify (I2). freeze rebuilds the module from CPAN on the ship platform with a C
    toolchain + the conda layer's libs.

    Returns: {success, module, verify_command, verify_output, install_method, log}.
    """
    result = _env_mgr.install_perl_package(env_name, module, distribution, cpanm_flags, build_env)
    if pipeline_id:
        result["pipeline_merge"] = _merge_simple_install(
            pipeline_id, step, result, name=module, channel="cpan", tool="cpanm",
            command=f"cpanm {distribution or module}",
            purpose=f"Install Perl module {module}",
        )
    return result


@mcp.tool()
def install_cargo_tool(
    env_name: str,
    crate: str,
    version: str = "",
    binary_name: str = "",
    git_url: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Rust crate's binary via `cargo install` (Tier: cargo) — Rust
    tools not on bioconda. Requires rust+cargo in the env (conda: `rust`).
    Installs with --root {env} so the binary lands on the env PATH; `binary_name`
    (defaults to crate) is the cli_which anchor. `git_url` installs from a git
    repo instead of crates.io. Pin `version` for reproducibility.

    NOTE many Rust genomics tools (sylph, skani, sourmash) are ALSO on bioconda —
    prefer install_conda_packages when available; this is the fallback. With
    pipeline_id, records the install_step + caches the verify (I2).

    Returns: {success, crate, binary_name, verify_command, verify_output, install_method, log}.
    """
    result = _env_mgr.install_cargo_tool(env_name, crate, version, binary_name, git_url)
    if pipeline_id:
        name = binary_name or crate
        result["pipeline_merge"] = _merge_simple_install(
            pipeline_id, step, result, name=name, channel="cargo", tool="cargo",
            command=f"cargo install {git_url or crate}" + (f" --version {version}" if version else ""),
            purpose=f"Install Rust tool {name}",
        )
    return result


@mcp.tool()
def install_go_tool(
    env_name: str,
    package: str,
    version: str = "latest",
    binary_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Go tool via `go install pkg@version` (Tier: go) — Go tools not
    on bioconda. Requires go in the env (conda: `go`). Sets GOBIN={env}/bin so
    the binary lands on the env PATH; `binary_name` (defaults to the last path
    segment of `package`) is the cli_which anchor. Pin `version` (not "latest")
    for reproducibility. With pipeline_id, records the install_step + caches the
    verify (I2).

    Returns: {success, package, binary_name, verify_command, verify_output, install_method, log}.
    """
    result = _env_mgr.install_go_tool(env_name, package, version, binary_name)
    if pipeline_id:
        name = binary_name or package.rstrip("/").split("/")[-1]
        result["pipeline_merge"] = _merge_simple_install(
            pipeline_id, step, result, name=name, channel="go", tool="go",
            command=f"go install {package}@{version}",
            purpose=f"Install Go tool {name}",
        )
    return result


@mcp.tool()
def install_jar_tool(
    env_name: str,
    tool_name: str,
    jar_url: str,
    java_flags: list[str] = [],
    wrapper_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Java JAR-based tool end-to-end (Exomiser, Picard, GATK, snpEff, …).

    The env must already have `openjdk` (and `unzip` if `jar_url` is a .zip).
    Downloads the JAR (curl with a progress bar — watchdog-friendly), unpacks if
    it's a distribution zip, picks the primary jar (heuristic: name contains
    `tool_name`, shortest matches first), and writes a wrapper script at
    {env}/bin/{wrapper_name or tool_name} that runs `java {java_flags} -jar JAR "$@"`.

    java_flags default: ["-Xmx4g"]. Override for memory-hungry tools (Exomiser
    typically wants -Xmx6g or higher).

    If pipeline_id is supplied, records an install_step (tool=jar, subcommand=install)
    with installed_packages=[{name=tool_name, channel=github (if URL host is github.com),
    else external, source=jar_url}], so the finalize-time package derivation picks it
    up automatically and the spec ends up with install_method.type=jar.
    """
    flags = list(java_flags) if java_flags else ["-Xmx4g"]
    result = _env_mgr.install_jar_tool(
        env_name      = env_name,
        tool_name     = tool_name,
        jar_url       = jar_url,
        java_flags    = flags,
        wrapper_name  = wrapper_name,
    )
    if pipeline_id:
        from urllib.parse import urlparse
        from agent.skills.env_manager import parse_version_from_url
        host = urlparse(jar_url).netloc or ""
        channel = "github" if "github.com" in host else "external"
        version = parse_version_from_url(jar_url)
        ip_record = {
            "name":    tool_name,
            "channel": channel,
            "source":  jar_url,
            "install_method": {"type": "jar", "source": jar_url},
        }
        if version:
            ip_record["version"] = version
        step_data = {
            "tool":        "jar",
            "subcommand":  "install",
            "purpose":     f"Install {tool_name} JAR from {host}",
            "command":     f"install_jar_tool --jar-url {jar_url}",
            "returncode":  0 if result.get("success") else 1,
            "installed_packages": [ip_record],
        }
        if result.get("success"):
            step_data["installed_packages"][0]["install_method"]["jar_path"]        = result.get("jar_path")
            step_data["installed_packages"][0]["install_method"]["wrapper_script"]  = result.get("wrapper_path")
            step_data["installed_packages"][0]["install_method"]["java_flags"]      = flags
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_r_package(
    env_name: str,
    name: str,
    source: str,
    pipeline_id: str = "",
    step: int = 0,
    deps_first: list[str] = [],
) -> dict:
    """Install an R package end-to-end with category-correct discovery built in.

    `source` is one of:
      cran          — install.packages("name") from CRAN
      bioconductor  — BiocManager::install("name")
      github:owner/repo  — remotes::install_github("owner/repo", dependencies=FALSE)
                          (use deps_first to pre-install undeclared transitive deps)

    Encapsulates everything the CLAUDE.md "R tools" section used to require
    the agent to remember:
      - Library isolation via $CONDA_PREFIX/lib/R/library (R_LIBS_USER hazard)
      - Auto-installed BiocManager bootstrap if missing
      - Post-install requireNamespace() load-or-die check baked into the
        Rscript itself (catches the "install printed ERROR but rc=0" failure)
      - Auto-records install_method.type='r_install' with the source URL
    `pipeline_id`: lands an install_step automatically. Use `step=N` to replace
    a failed prior attempt (retry semantics).

    Returns the run_install_command shape; failure cases include both R
    install errors and load-failures with a clean signal.
    """
    # NOTE: the executed Rscript is wrapped in `Rscript -e "..."` (outer double
    # quotes), so every R string literal inside MUST use single quotes — nested
    # double quotes terminate the outer bash string and Rscript receives the
    # package name as an unquoted symbol ("object 'X' not found").
    if source.startswith("github:"):
        owner_repo = source[len("github:"):].strip()
        # Pre-install undeclared transitive deps if requested.
        pre_lines = []
        if deps_first:
            pre = ",".join(f"'{d}'" for d in deps_first)
            pre_lines.append(
                f'BiocManager::install(c({pre}), lib=lib, ask=FALSE, update=FALSE)'
            )
        install_expr = (
            f"remotes::install_github('{owner_repo}', lib=lib, dependencies=FALSE)"
        )
        check_name = name
        install_block = "; ".join(pre_lines + [install_expr])
        channel = "github"
        source_url = f"https://github.com/{owner_repo}"
        im_source = f"remotes::install_github('{owner_repo}')"
    elif source == "cran":
        install_block = (
            f"install.packages('{name}', lib=lib, repos='https://cloud.r-project.org')"
        )
        channel = "cran"
        source_url = f"https://CRAN.R-project.org/package={name}"
        im_source = f"install.packages('{name}')"
        check_name = name
    elif source == "bioconductor":
        install_block = (
            f"BiocManager::install('{name}', lib=lib, ask=FALSE, update=FALSE)"
        )
        channel = "bioconductor"
        source_url = f"https://bioconductor.org/packages/{name}/"
        im_source = f"BiocManager::install('{name}')"
        check_name = name
    else:
        return {"success": False, "error": f"unknown R source: {source!r} (use cran|bioconductor|github:owner/repo)"}

    # Wrap with library isolation + BiocManager bootstrap + load-or-die check.
    # The load-or-die is what makes this honest: rc != 0 if the install
    # silently failed but Rscript would otherwise exit 0.
    rscript = (
        "lib <- file.path(Sys.getenv('CONDA_PREFIX'),'lib','R','library'); "
        "if(!requireNamespace('BiocManager',quietly=TRUE)) "
        "install.packages('BiocManager',lib=lib,repos='https://cloud.r-project.org'); "
        f"{install_block}; "
        f"if(!requireNamespace('{check_name}',quietly=TRUE,lib.loc=lib)) "
        f"stop('install reported success but {check_name} is not loadable');"
    )
    command = f"Rscript -e \"{rscript}\""
    verify_command = (
        f"Rscript -e \"if(!requireNamespace('{check_name}',quietly=TRUE)) quit(status=1); "
        f"cat(as.character(packageVersion('{check_name}')))\""
    )

    # Delegate to run_install_command for the actual install_step plumbing.
    result = _env_mgr.run_in_env(env_name, command, timeout=1800)
    if result.get("returncode") == 0:
        vresult = _env_mgr.run_in_env(env_name, verify_command, timeout=60)
        if vresult.get("returncode") != 0:
            result["returncode"] = vresult.get("returncode") or 1
            result["success"]    = False
            result["stderr"]     = (result.get("stderr") or "") + (
                f"\n[verify failed: {vresult.get('stderr','')[-200:]}]"
            )
        else:
            r_version = (vresult.get("stdout") or "").strip()
            result["verify_command"]  = verify_command
            result["verify_output"]   = (vresult.get("stdout") or "")[:500]
            result["resolved_version"] = r_version

    if pipeline_id:
        ip_record = {
            "name":    check_name,
            "channel": channel,
            "source":  im_source,
            "install_method": {"type": "r_install", "source": im_source},
        }
        if result.get("resolved_version"):
            ip_record["version"] = result["resolved_version"]
        step_data = {
            "tool":        "Rscript",
            "subcommand":  source,
            "purpose":     f"Install R package {check_name} from {source}",
            "command":     command,
            "returncode":  result.get("returncode"),
            "runtime_seconds": result.get("runtime_seconds"),
            "installed_packages": [ip_record],
        }
        if result.get("verify_command"):
            step_data["verify_command"] = result["verify_command"]
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # Cache the load-or-die verify so the finalize package derivation
        # attaches it (mirrors verify_installation / install_git_repo). Without
        # this the derived PackageRecord has no verify_output and fails I2,
        # forcing a redundant verify_installation call for every R package.
        if result.get("success") and result.get("verify_output"):
            _pipeline_state.cache_verification(pipeline_id, check_name, {
                "verify_command": result.get("verify_command"),
                "verify_output":  result.get("verify_output"),
            })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_pip_package(
    env_name: str,
    name: str,
    version: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a pip package end-to-end with an auto-verify_command.

    Equivalent to running pip install + python -c "import name" inside the env.
    The import-check is the load-or-die: if pip says it installed but the
    package isn't importable, the step is recorded as failed.

    Notes:
      - pip's canonical name is preserved (e.g. open-cravat stays hyphenated)
      - resolved_version is filled from `pip list --format=json` at finalize
        when no explicit version is passed
    """
    spec = f"{name}=={version}" if version else name
    # Module name for the import check: pip's canonical hyphenated names use
    # underscores at import time (open-cravat → import cravat or open_cravat).
    # We default to the lowercased name; agents can override with verify_command
    # post-hoc if the import path differs.
    import_check_name = name.replace("-", "_").lower()
    command = f"pip install {spec}"
    verify_command = f"python -c 'import {import_check_name}' || pip show {name} > /dev/null"

    result = _env_mgr.run_in_env(env_name, command, timeout=600)
    if result.get("returncode") == 0:
        vresult = _env_mgr.run_in_env(env_name, verify_command, timeout=30)
        if vresult.get("returncode") != 0:
            result["returncode"] = vresult.get("returncode") or 1
            result["success"]    = False
            result["stderr"]     = (result.get("stderr") or "") + (
                f"\n[verify failed: cannot import {import_check_name} and pip show {name} did not find it]"
            )
        else:
            result["verify_command"] = verify_command
            result["verify_output"]  = (vresult.get("stdout") or "")[:200] or "(import succeeded)"

    if pipeline_id:
        ip_record = {
            "name":    name,
            "channel": "pip",
            "source":  f"pip install {spec}",
            "install_method": {"type": "pip", "source": f"pip install {spec}"},
        }
        if version:
            ip_record["version"] = version
        step_data = {
            "tool":               "pip",
            "subcommand":         "install",
            "purpose":            f"Install pip package {name}",
            "command":            command,
            "returncode":         result.get("returncode"),
            "runtime_seconds":    result.get("runtime_seconds"),
            "installed_packages": [ip_record],
        }
        if result.get("verify_command"):
            step_data["verify_command"] = result["verify_command"]
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # Cache the import-check verify so the finalize package derivation
        # attaches it — without this the derived PackageRecord has no
        # verify_output and fails I2 (same gap fixed for R packages).
        if result.get("success") and result.get("verify_output"):
            _pipeline_state.cache_verification(pipeline_id, name, {
                "verify_command": result.get("verify_command"),
                "verify_output":  result.get("verify_output"),
            })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def download_reference_database(
    name: str,
    url: str,
    local_path: str,
    version: str = "",
    description: str = "",
    pipeline_id: str = "",
    extract: bool = True,
) -> dict:
    """Download a reference database large enough to need watchdog-safe execution.

    Uses run_in_background internally — agent doesn't have to remember to wrap
    the curl in async, doesn't have to worry about --silent / -q traps that
    killed the original Exomiser install. Auto-records a ReferenceDatabase
    entry in the draft when pipeline_id is supplied.

    extract=True: if `url` ends in .zip / .tar.gz / .tar, unpack into local_path
                  and remove the archive. .zip uses unzip; .tar.gz uses tar.
    extract=False: just download to local_path.

    Returns immediately with a job_id — caller polls check_job(job_id) until
    state != "running", then sees success/failure via returncode. The
    ReferenceDatabase entry's `available` is auto-derived at finalize from
    whether local_path exists on disk.
    """
    from pathlib import Path as _Path
    target = _Path(local_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if extract and url.endswith(".zip"):
        # Download to a sibling .zip, unpack into local_path, remove the zip.
        zip_path = target.parent / _Path(url).name
        cmd = (
            f"curl -L --progress-bar -C - -o {zip_path} '{url}' "
            f"&& mkdir -p {target} "
            f"&& unzip -o {zip_path} -d {target.parent} "
            f"&& rm {zip_path}"
        )
    elif extract and (url.endswith(".tar.gz") or url.endswith(".tgz")):
        tar_path = target.parent / _Path(url).name
        cmd = (
            f"curl -L --progress-bar -C - -o {tar_path} '{url}' "
            f"&& mkdir -p {target} "
            f"&& tar -xzf {tar_path} -C {target} "
            f"&& rm {tar_path}"
        )
    elif extract and url.endswith(".tar"):
        tar_path = target.parent / _Path(url).name
        cmd = (
            f"curl -L --progress-bar -C - -o {tar_path} '{url}' "
            f"&& mkdir -p {target} "
            f"&& tar -xf {tar_path} -C {target} "
            f"&& rm {tar_path}"
        )
    else:
        cmd = f"curl -L --progress-bar -C - -o {target} '{url}'"

    job = _job_manager.start(cmd, job_id=f"refdb_{name}_{int(__import__('time').time())}")
    if pipeline_id:
        # Append the ReferenceDatabase entry now; `available` is re-derived at
        # finalize from filesystem state (so an in-progress download correctly
        # shows available=false until it completes).
        rdb = {
            "name":          name,
            "version":       version or "unknown",
            "source_url":    url,
            "local_path":    str(target),
            "available":     False,
            "description":   description or None,
        }
        draft = _pipeline_state.get_draft(pipeline_id) or {}
        existing = draft.get("reference_databases") or []
        # Update-by-name if it exists.
        replaced = False
        for i, e in enumerate(existing):
            if isinstance(e, dict) and e.get("name") == name:
                existing[i] = {**e, **{k: v for k, v in rdb.items() if v is not None}}
                replaced = True
                break
        if not replaced:
            existing.append({k: v for k, v in rdb.items() if v is not None})
        _pipeline_state.patch(pipeline_id, {"reference_databases": existing})
    return {
        "job_id":     job.get("job_id"),
        "status_path": job.get("status_path"),
        "log_path":   job.get("log_path"),
        "local_path": str(target),
        "name":       name,
        "url":        url,
        "command":    cmd,
        "note":       "Use check_job(job_id) to monitor; ReferenceDatabase entry recorded in draft.reference_databases (available=false until download finishes).",
    }


@mcp.tool()
def run_pipeline_step(
    env_name: str,
    command: str,
    pipeline_id: str,
    inputs: list = [],
    output_types: dict = {},
    watch_dir: str = "",
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
    step: int = 0,
    timeout_seconds: int = 1800,
) -> dict:
    """Run a pipeline step and auto-validate every produced output in one call.

    Replaces the three-call dance (run_in_env → inspect detected_outputs →
    validate_output(files=[...])) with one. Internally:
      1. Runs the command via run_in_env (with pipeline_id-merge into pipeline_steps)
      2. Validates every detected_output against an inferred or supplied type
      3. Validations are merged into the same pipeline_step automatically

    output_types: optional dict mapping basename or extension to expected_type
                  (e.g. {".vcf.gz": "vcf", ".bam": "bam", "report.html": "html"}).
                  Anything not matched falls back to extension inference.

    pipeline_id is required (this primitive's purpose is the merged flow).
    """
    if not pipeline_id:
        return {"error": "pipeline_id is required for run_pipeline_step"}

    result = _env_mgr.run_in_env(
        env_name, command, timeout=timeout_seconds, inputs=inputs,
        watch_dir=watch_dir or None,
    )
    step_data = {
        "tool":            tool or (command.split() or [""])[0],
        "subcommand":      subcommand or None,
        "purpose":         purpose or None,
        "command":         command,
        "returncode":      result.get("returncode"),
        "runtime_seconds": result.get("runtime_seconds"),
        "resource_usage":  result.get("resource_usage"),
        "inputs":          result.get("inputs", []),
        "detected_outputs": result.get("detected_outputs", []),
    }
    step_data = {k: v for k, v in step_data.items() if v is not None}
    idx = _pipeline_state.add_step(pipeline_id, step_data, replace_step=step)

    # Auto-validate every detected output if the run succeeded.
    validations: dict = {}
    if result.get("returncode") == 0 and idx is not None:
        for path in result.get("detected_outputs", []):
            basename = Path(path).name
            ext = "".join(Path(path).suffixes).lower()
            # Resolve expected type: explicit basename match > extension match > "any".
            etype = (output_types.get(basename)
                     or output_types.get(ext)
                     or output_types.get(ext.lstrip("."))
                     or _infer_validator_type(basename, ext))
            v = _validator.validate(path, etype, env_name=env_name)
            validations[basename] = v
            _pipeline_state.add_validation(pipeline_id, idx, basename, v)

    return {
        **result,
        "pipeline_merge":  {"status": "merged", "pipeline_id": pipeline_id, "step_index": idx},
        "validations":     validations,
        "validation_count": len(validations),
    }


def _infer_validator_type(basename: str, ext: str) -> str:
    """Pure-function extension → validator type. No memorization beyond
    obvious filetype names that the OutputValidator already handles."""
    ext = ext.lstrip(".").lower()
    # Strip .gz: validators handle compressed forms natively.
    if ext.endswith(".gz"):
        ext = ext[:-3]
    # Final extension is usually authoritative.
    last = ext.split(".")[-1] if ext else ""
    # Aliases — map common shorthands to canonical validator types.
    aliases = {"fq": "fastq", "fa": "fasta", "fna": "fasta", "faa": "fasta", "ndjson": "jsonl"}
    if last in aliases:
        return aliases[last]
    # Validator's internal dispatch already keys off these names; mirror it.
    for known in ("bam", "sam", "bai", "vcf", "bcf", "fasta", "fastq",
                  "bed", "bigwig", "gtf", "gff", "gfa", "tsv", "csv", "txt",
                  "html", "json", "jsonl"):
        if last == known:
            return known
    return "any"


@mcp.tool()
def run_step_in_container(
    freeze_request_key: str,
    command: str,
    pipeline_id: str,
    inputs: list = [],
    output_types: dict = {},
    data_dir: str = "",
    watch_dir: str = "",
    extra_mounts: list = [],
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
    step: int = 0,
    timeout_seconds: int = 1800,
    platform: str = "linux/amd64",
) -> dict:
    """Run a pipeline step INSIDE the frozen env image and auto-validate every
    produced output — the validation-locus pivot: **the artifact you ship is the
    artifact you validate.** Use this instead of run_pipeline_step once the env is
    frozen, so the recorded run is the one that actually executes on HPC.

    Resolves the image from the EnvCache by `freeze_request_key` (call freeze()
    first), pulling an adopted-by-digest image local if needed. The data dir is
    bind-mounted at its OWN host path inside the container, so the same absolute
    paths in `command`/`inputs` work unchanged and outputs land back on the host
    for validation. Resource usage (I7) is captured IN the container (GNU time →
    exact peak RSS, falling back to host docker-stats sampling). The step is
    stamped ran_in_container + container_image[_digest] so seal can assert
    validated==shipped.

    output_types: {basename|ext: validator_type}. inputs: paths (or {path,…}).
    extra_mounts: ["host:container", …] for data outside data_dir."""
    if not pipeline_id:
        return {"error": "pipeline_id is required for run_step_in_container"}
    rec = _env_cache.lookup(freeze_request_key)
    if not rec:
        return {"error": f"no frozen env for '{freeze_request_key}' — run freeze() first"}
    image = rec.get("image")
    if not image:
        return {"error": f"freeze record for '{freeze_request_key}' has no image handle"}
    # An adopted biocontainer is referenced by digest — pull it local so it can run.
    if _docker._run(["docker", "image", "inspect", image])["returncode"] != 0:
        pull = _docker._run(["docker", "pull", "--platform", platform, image], timeout=900)
        if pull["returncode"] != 0:
            return {"error": f"could not pull image {image}: {(pull['stderr'] or '')[-300:]}"}

    ddir = (Path(data_dir) if data_dir else (_env_mgr.project_root / "data")).resolve()
    mounts = [(str(ddir), str(ddir))]   # same-path mount → host abs paths work verbatim
    for m in extra_mounts:
        if isinstance(m, str) and ":" in m:
            h, c = m.split(":", 1)
            mounts.append((h, c))

    wdir = (Path(watch_dir).resolve() if watch_dir else ddir)

    def _snap() -> dict:
        snap = {}
        for p in wdir.rglob("*"):
            if p.is_file():
                try:
                    stt = p.stat()
                    snap[str(p)] = (stt.st_mtime_ns, stt.st_size)
                except OSError:
                    pass
        return snap

    before = _snap()
    res = _docker.run_in_container(image, command, mounts=mounts, workdir=str(ddir),
                                   platform=platform, timeout=timeout_seconds)
    after = _snap()
    detected = sorted(p for p, sig in after.items() if before.get(p) != sig)

    norm_inputs = [{"path": i, "references": []} if isinstance(i, str) else i for i in inputs]
    step_data = {
        "tool":            tool or (command.split() or [""])[0],
        "subcommand":      subcommand or None,
        "purpose":         purpose or None,
        "command":         command,
        "returncode":      res.get("returncode"),
        "resource_usage":  res.get("resource_usage"),
        "inputs":          norm_inputs,
        "detected_outputs": detected,
        "ran_in_container": True,
        "container_image":  image,
        "container_image_digest": rec.get("image_digest"),
    }
    step_data = {k: v for k, v in step_data.items() if v is not None}
    idx = _pipeline_state.add_step(pipeline_id, step_data, replace_step=step)

    validations: dict = {}
    if res.get("returncode") == 0 and idx is not None:
        for path in detected:
            basename = Path(path).name
            ext = "".join(Path(path).suffixes).lower()
            etype = (output_types.get(basename) or output_types.get(ext)
                     or output_types.get(ext.lstrip(".")) or _infer_validator_type(basename, ext))
            v = _validator.validate(path, etype)
            validations[basename] = v
            _pipeline_state.add_validation(pipeline_id, idx, basename, v)

    return {
        **res,
        "detected_outputs":  detected,
        "validated_in_image": image,
        "pipeline_merge":    {"status": "merged", "pipeline_id": pipeline_id, "step_index": idx},
        "validations":       validations,
        "validation_count":  len(validations),
    }


@mcp.tool()
def verify_installation(
    env_name: str,
    package_name: str,
    check_command: str,
    pipeline_id: str = "",
) -> dict:
    """Run a custom version/help command inside the env to confirm a package
    installed correctly.

    Advisory: env_status no longer requires every package to have a verify
    record — `conda list --json` plus successful install_steps are the
    structural truth. Use this when you want to capture a custom check
    (e.g. `samtools --version` for a CLI tool) so it appears in the report
    next to that package.

    If pipeline_id is supplied, the result is cached in
    draft.verifications[package_name] and stitched onto the derived package
    record at finalize-time."""
    result = _env_mgr.verify(env_name, package_name, check_command)
    if pipeline_id:
        ok = _pipeline_state.cache_verification(pipeline_id, package_name, {
            "verify_command": check_command,
            "verify_output":  result.get("output", ""),
        })
        result["pipeline_merge"] = (
            {"status": "cached", "pipeline_id": pipeline_id, "name": package_name}
            if ok else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def check_gpu() -> dict:
    """Check if an NVIDIA GPU is available for GPU-accelerated tools.

    Returns differ by outcome:
      Available:    {available: True, gpus: [{name, driver_version, memory_mb}, ...], cuda_version}
      Unavailable:  {available: False, reason: "<why>", fallback: "Use CPU mode for testing"}

    Use the `available` field to decide whether to install GPU deps and to set
    `runtime_environment.gpu_required` on the spec. If False, fall back to CPU
    mode (most tools expose `--device cpu`)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return {"available": False, "reason": "nvidia-smi not found", "fallback": "Use CPU mode for testing"}
    if r.returncode != 0:
        return {"available": False, "reason": r.stderr.strip()[:200], "fallback": "Use CPU mode for testing"}

    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"name": parts[0], "driver_version": parts[1], "memory_mb": parts[2]})

    # Extract CUDA version from nvidia-smi header line
    header = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10)
    cuda_ver = ""
    for ln in header.stdout.splitlines():
        if "CUDA Version:" in ln:
            cuda_ver = ln.split("CUDA Version:")[-1].strip().split()[0]
            break

    return {"available": bool(gpus), "gpus": gpus, "cuda_version": cuda_ver}


@mcp.tool()
def start_service(
    env_name: str,
    service_name: str,
    start_command: str,
    health_check_command: str,
    health_check_timeout_seconds: int = 30,
    working_dir: str = "",
    env_vars: dict[str, str] = {},
    pipeline_id: str = "",
    service_type: str = "custom",
    stop_command: str = "",
    port: int = 0,
    version: str = "",
) -> dict:
    """Start a background service (web server, database, Spark, cache, …) inside
    a conda env. Polls health_check_command until healthy or timeout.

    If pipeline_id is supplied, the service is registered into the draft as a
    ServiceDependency: the declaration (start_command, stop_command,
    health_check_command, port, version, type) is recorded, the initial
    readiness probe is appended to health_check_log, and status is set to
    `running` (or `failed` if the probe never succeeds).

    `service_dependencies` is BLOCKED from patch_pipeline; the only path to
    declare a service in the spec is through this primitive (or
    verify_service_dependency for follow-up probes). I10 requires every
    declared dependency to have ≥1 healthy probe in its log.

    Returns: {success, pid, log, health_probe?}.
    """
    from datetime import datetime, timezone

    result = _env_mgr.start_service(
        env_name, service_name, start_command, health_check_command,
        health_check_timeout_seconds=health_check_timeout_seconds,
        working_dir=working_dir or None,
        env_vars=env_vars or None,
    )

    if pipeline_id:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Always run one explicit probe + record it (success OR failure). When
        # start_service times out, the inner loop's last probe wasn't returned,
        # so we re-probe here to make the failure case provable too.
        probe_raw = _env_mgr.check_service_health(env_name, health_check_command, working_dir=working_dir or None)
        probe = {
            "timestamp":      now,
            "command":        health_check_command,
            "returncode":     int(probe_raw["returncode"]) if probe_raw.get("returncode") is not None else 1,
            "healthy":        bool(probe_raw.get("healthy")),
            "output_excerpt": ((probe_raw.get("stdout") or "") + (probe_raw.get("stderr") or ""))[-500:],
        }
        pid_int: Optional[int] = None
        if result.get("pid"):
            try:
                pid_int = int(result["pid"])
            except (TypeError, ValueError):
                pid_int = None
        fields = {
            "type":                 service_type or "custom",
            "version":              version or None,
            "start_command":        start_command,
            "stop_command":         stop_command or "",
            "health_check_command": health_check_command,
            "health_check_timeout_seconds": health_check_timeout_seconds,
            "port":                 port or None,
            "env_vars":             env_vars or {},
            "pid":                  pid_int,
            "started_at":           now,
            # Restarting a previously stopped service clears the prior
            # stopped_at — without this, the spec shows status=running with a
            # stale stopped_at timestamp, which is contradictory.
            "stopped_at":           None,
            "status":               "running" if (result.get("success") and probe["healthy"]) else "failed",
            "health_check_log":     [probe],
        }
        _pipeline_state.upsert_service_dependency(pipeline_id, service_name, fields)
        result["pipeline_merge"] = {
            "status":         "merged",
            "pipeline_id":    pipeline_id,
            "service_name":   service_name,
            "probe_healthy":  probe["healthy"],
        }
        result["health_probe"] = probe

    return result


@mcp.tool()
def stop_service(
    env_name: str,
    service_name: str,
    stop_command: str = "",
    pipeline_id: str = "",
) -> dict:
    """Stop a background service started with start_service.
    Prefers stop_command if provided; falls back to killing by PID file.

    If pipeline_id is supplied, the spec's service_dependency entry is updated
    with status=stopped and stopped_at timestamp."""
    from datetime import datetime, timezone

    result = _env_mgr.stop_service(env_name, service_name, stop_command=stop_command)
    if pipeline_id:
        _pipeline_state.upsert_service_dependency(
            pipeline_id, service_name,
            {
                "status":     "stopped" if result.get("success") else "failed",
                "stopped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        result["pipeline_merge"] = {
            "status":       "merged",
            "pipeline_id":  pipeline_id,
            "service_name": service_name,
        }
    return result


@mcp.tool()
def check_service_health(
    env_name: str,
    health_check_command: str,
    working_dir: str = "",
) -> dict:
    """Run a health-check command to verify a background service is responding.
    Returns: healthy (bool), returncode, stdout, stderr.

    For recording the probe into a pipeline spec (so I10 is satisfied), use
    verify_service_dependency instead — this primitive is unrecorded by design
    (debug / one-off probe)."""
    return _env_mgr.check_service_health(env_name, health_check_command, working_dir=working_dir or None)


@mcp.tool()
def verify_service_dependency(
    pipeline_id: str,
    service_name: str,
    env_name: str,
    health_check_command: str = "",
    working_dir: str = "",
) -> dict:
    """Probe a declared service and append the observation to its
    health_check_log. The honest companion to start_service.

    If `health_check_command` is empty, the command recorded on the existing
    service_dependency entry is reused — so repeat probes are easy.

    I10 — at finalize, every service_dependency must have ≥1 entry in its
    health_check_log with healthy=true. start_service records one such probe
    on successful start; use this primitive to record additional probes
    (mid-pipeline checkpoint, post-step verification, recovery after a flap).

    Returns: {success, healthy, returncode, output, probe_recorded}.
    """
    from datetime import datetime, timezone

    draft = _pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return {"error": f"unknown pipeline_id: {pipeline_id}"}

    existing_cmd = ""
    for d in draft.get("service_dependencies", []) or []:
        if isinstance(d, dict) and d.get("name") == service_name:
            existing_cmd = d.get("health_check_command") or ""
            break

    cmd = health_check_command or existing_cmd
    if not cmd:
        return {
            "error": (
                f"service '{service_name}' has no recorded health_check_command "
                f"and none was supplied. Call start_service first, or pass "
                f"health_check_command explicitly."
            ),
        }

    probe_raw = _env_mgr.check_service_health(env_name, cmd, working_dir=working_dir or None)
    probe = {
        "timestamp":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command":        cmd,
        "returncode":     int(probe_raw["returncode"]) if probe_raw.get("returncode") is not None else 1,
        "healthy":        bool(probe_raw.get("healthy")),
        "output_excerpt": ((probe_raw.get("stdout") or "") + (probe_raw.get("stderr") or ""))[-500:],
    }
    idx = _pipeline_state.upsert_service_dependency(
        pipeline_id, service_name,
        {"health_check_log": [probe]},
    )
    return {
        "success":         True,
        "healthy":         probe["healthy"],
        "returncode":      probe["returncode"],
        "output":          probe["output_excerpt"],
        "probe_recorded":  True,
        "pipeline_merge":  {
            "status":       "merged" if idx is not None else "no_op",
            "pipeline_id":  pipeline_id,
            "service_name": service_name,
        },
    }


@mcp.tool()
def run_in_env(
    env_name: str,
    command: str,
    working_dir: str = "",
    timeout_seconds: int = 1800,
    inputs: list = [],
    watch_dir: str = "",
    pipeline_id: str = "",
    step: int = 0,
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
) -> dict:
    """Run an arbitrary shell command inside a conda environment. Always use absolute paths.

    inputs: list of files this step consumes. Each entry is either:
            - a plain string path: "/abs/path/to/file"
            - a structured dict:   {path: "/abs/path/to/run.R",
                                    references: ["/abs/path/data1.tsv", ...]}
              Use the dict form when an input is a script / config / wrapper that
              opens other files at runtime — the references become an indented
              sublist under that input in the HTML report and are kept in the
              spec so the lineage is programmatic (not memory-bound).
    watch_dir: directory to snapshot before/after execution. New and modified files
               are returned as detected_outputs. Defaults to working_dir if omitted.

    If pipeline_id is supplied, a PipelineStep entry is appended to
    draft.pipeline_steps with the command, returncode, runtime, inputs, and
    detected outputs. Pass `step=N` to replace step N (for retries); default
    is append. The returned `pipeline_merge.step_index` is what you pass to
    validate_output(step=...) to attach validations to this step.

    Return keys: returncode, stdout, stderr, success, command, runtime_seconds,
                 inputs, detected_outputs, [pipeline_merge]."""
    result = _env_mgr.run_in_env(
        env_name, command,
        working_dir=working_dir or None,
        timeout=timeout_seconds,
        inputs=inputs,
        watch_dir=watch_dir or working_dir or None,
    )
    if pipeline_id:
        step_data = {
            "tool":            tool or (command.split() or [""])[0],
            "subcommand":      subcommand or None,
            "purpose":         purpose or None,
            "command":         command,
            "returncode":      result.get("returncode"),
            "runtime_seconds": result.get("runtime_seconds"),
            "resource_usage":  result.get("resource_usage"),
            "inputs":          result.get("inputs", []),
            "outputs":         result.get("detected_outputs", []),
        }
        step_data = {k: v for k, v in step_data.items() if v is not None}
        idx = _pipeline_state.add_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result

# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.tool()
def list_available_resources(resource_type: str = "both") -> dict:
    """List genomes and/or test datasets on disk.
    resource_type: 'genomes' | 'test_data' | 'both'"""
    return _list_resources({"resource_type": resource_type}, config)


@mcp.tool()
def download_resource(resource_type: str, resource_id: str) -> dict:
    """Download a reference genome not yet on disk.
    resource_type: 'genome', resource_id: e.g. 'hg38_chr22'"""
    return _test_runner.download_resource(resource_type, resource_id)


@mcp.tool()
def add_core_test_data(
    accession: str,
    assay_type: str,
    end_type: str = "paired_end",
    genome_build: str = "hg38",
    sample: str = "",
    subset: str = "10K",
    platform: str = "illumina",
    source_url: str = "",
    source_url_r2: str = "",
) -> dict:
    """Stream-download and register a new sequencing dataset.
    assay_type:   exome | wgs | rnaseq | chipseq | atacseq | hic | amplicon | wgbs | ont_wgs | pacbio_hifi | direct_rna | isoseq | fiberseq
    platform:     illumina (default) | ont | pacbio_hifi | pacbio_isoseq | pacbio_fiberseq
    subset:       500 | 1K | 10K (default) | 50K | 100K | 500K | 1M  — use 500 for long-read platforms
    source_url:   override EBI URL builder (e.g. NCBI FTP, S3). For paired-end also supply source_url_r2."""
    return _add_core_test_data(
        config, accession, assay_type,
        end_type=end_type, genome_build=genome_build,
        sample=sample, subset=subset, platform=platform,
        source_url=source_url, source_url_r2=source_url_r2,
    )

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@mcp.tool()
def add_phenopacket(
    source_url: str,
    genome_build: str = "hg38",
) -> dict:
    """Download and register a GA4GH phenopacket JSON into core_test_data.

    The phenopacket ID, subject, HPO terms, diseases, genes, and variants are
    all extracted from the JSON itself via PhenopacketMeta.from_phenopacket() —
    nothing is supplied manually.  Idempotent: re-running refreshes the sidecar.

    source_url:   direct URL to a phenopacket JSON file (GitHub raw, HTTP, etc.)
    genome_build: target core_test_data directory, e.g. hg38 (default)"""
    return _add_phenopacket(config, source_url=source_url, genome_build=genome_build)


@mcp.tool()
def phenopacket_to_vcf(
    phenopacket_id: str,
    output_vcf: str,
    genome_build: str = "hg38",
) -> dict:
    """Materialise a single-sample VCF from a registered phenopacket's variant block.

    Use this in Exomiser / variant-annotator pipelines where the test VCF should
    contain the exact variant(s) recorded in the phenopacket — no hand-written
    VCF synthesis required. Reads {data_dir}/core_test_data_{genome_build}/
    phenopackets/{phenopacket_id}_meta.yaml; writes a VCFv4.2 file to output_vcf.

    Inputs:
      phenopacket_id: as registered by add_phenopacket (PMID_30315159_Patient_N, …)
      output_vcf:     absolute path to write the VCF
      genome_build:   defaults to hg38

    Output keys: success, phenopacket_id, sample_id, output_vcf, num_variants,
                 contigs, genome_assembly. On failure: {success: false, error}.
    Phenotype-only phenopackets (no variant block) cannot be materialised — this
    is surfaced as a clear error rather than silently producing an empty VCF.
    """
    return _phenopacket_to_vcf(
        config, phenopacket_id=phenopacket_id,
        output_vcf=output_vcf, genome_build=genome_build,
    )


@mcp.tool()
def validate_output(
    file_path: str = "",
    expected_type: str = "",
    files: list[dict] = [],
    env_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
    allow_empty: bool = False,
) -> dict:
    """Validate one or many bioinformatics output files are non-empty and parseable.

    expected_type: sam | bam | fastq | fasta | vcf | bcf | bed | bigwig |
                   bim | fam | ld | frq | prune | tsv | csv | txt | log | any

    Two call shapes:
      Single:  validate_output(file_path=..., expected_type=...)
      Batch:   validate_output(files=[{path, expected_type, allow_empty?}, ...])

    In batch mode each file is validated independently — one failure never
    aborts the others. Returns per-file results in `validations` keyed by
    basename, plus aggregate `all_passed` / `passed_count` / `failed_count`.

    `allow_empty` (single-call) or `allow_empty: true` per-entry (batch):
    treat an empty file as passing instead of failing. Use for tools whose
    success signal is an empty `.err` / `.log` (OpenCRAVAT's stderr file is
    the canonical case).

    If pipeline_id+step are supplied, every result is merged into
    draft.pipeline_steps[step].validation[basename] — the step's
    validation_status (I3) is derived from the aggregate at seal time."""
    # Batch path
    if files:
        validations: dict = {}
        passed_count = failed_count = 0
        merge_status = "merged" if pipeline_id and step > 0 else None
        for entry in files:
            path = entry.get("path", "")
            etype = entry.get("expected_type", "any")
            entry_allow_empty = bool(entry.get("allow_empty", False))
            vr = _validator.validate(
                path, etype, env_name=env_name or None,
                allow_empty=entry_allow_empty,
            )
            filename = Path(path).name
            validations[filename] = vr
            if vr.get("passed") is True:
                passed_count += 1
            elif vr.get("passed") is False:
                failed_count += 1
            if merge_status == "merged":
                ok = _pipeline_state.add_validation(pipeline_id, step, filename, vr)
                if not ok:
                    merge_status = "step_not_found"
        out: dict = {
            "validations":   validations,
            "all_passed":    failed_count == 0 and passed_count == len(files),
            "passed_count":  passed_count,
            "failed_count":  failed_count,
            "total":         len(files),
        }
        if pipeline_id:
            if step <= 0:
                out["pipeline_merge"] = {"status": "step_required", "pipeline_id": pipeline_id}
            else:
                out["pipeline_merge"] = {
                    "status": merge_status, "pipeline_id": pipeline_id, "step": step,
                    "merged_files": passed_count + failed_count,
                }
        return out

    # Single-file path (unchanged behavior)
    result = _validator.validate(
        file_path, expected_type, env_name=env_name or None,
        allow_empty=allow_empty,
    )
    if pipeline_id:
        if step <= 0:
            result["pipeline_merge"] = {"status": "step_required", "pipeline_id": pipeline_id}
        else:
            filename = Path(file_path).name
            ok = _pipeline_state.add_validation(pipeline_id, step, filename, result)
            result["pipeline_merge"] = (
                {"status": "merged", "pipeline_id": pipeline_id,
                 "step": step, "filename": filename}
                if ok else
                {"status": "step_not_found", "pipeline_id": pipeline_id, "step": step}
            )
    return result

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

@mcp.tool()
def freeze(
    env_name: str,
    tools: list[str],
    pipeline_name: str = "",
    pipeline_description: str = "",
    version: str = "",
    platform: str = "linux-64",
    accel: str = "none",
    gated: bool = False,
    licenses: list[str] = [],
    push_target: str = "",
    gpu_required: bool = False,
    cuda_version: str = "",
    pipeline_id: str = "",
) -> dict:
    """Freeze an env into a content-addressed, HPC-shippable artifact (Slice 5).

    The re-spine's env-artifact primitive — adopt a pre-built BioContainer by
    digest when one exists, else build our own, and emit the Apptainer/HPC
    delivery contract. Steps:

      1. request_key (what was ASKED) → EnvCache lookup; a hit returns the
         proven artifact by hash with NO re-solve (the scale unlock).
      2. content_digest (what was GOT): lock + source/binary/artifact hashes +
         platform + accel (from the draft when pipeline_id is given).
      3. ADOPT-or-BUILD:
         - pure conda + a published biocontainer → ADOPT it BY DIGEST (no build).
         - everything else → CONTAINER-NATIVE BUILD via env_freeze: install +
           validate IN the ship image (one generic bake, validated==shipped). Covers
           hand-installed tools (binary/jar/source/cargo/go/perl), pure conda, and
           pip/R — cross-arch too (built in-container; no host-arch conda-pack and no
           cross-arch refusal). A biocontainer is NOT adopted when the env has non-
           conda installs it can't represent (that would ship a different artifact).
           Gated builds must pass `licenses` (I13).
      4. HPC delivery, registry-free by DEFAULT: adopted → `apptainer pull
         docker://…@digest`; built → `docker save` tarball + `apptainer build
         docker-archive://…`. A push_target pushes the built image instead
         (NEVER for gated — I13: gated images stay tarball-only).
      5. EnvCache.register(request_key → record).

    `tools` are the PRIMARY requested tools (e.g. ['samtools=1.21',
    'bwa=0.7.17']) — what biocontainer/mulled adoption matches on, NOT the full
    dependency closure. Returns {mode, image, image_digest, content_digest,
    request_key, hpc_delivery, cache_hit, …}.
    """
    parsed = _freeze.parse_tools(tools)
    rkey = _freeze.request_key([(n, v or "") for n, v in parsed], platform, accel)

    cached = _env_cache.lookup(rkey)
    if cached:
        return {"success": True, "cache_hit": True, "request_key": rkey, **cached}

    if pipeline_id and _pipeline_state.get_draft(pipeline_id) is not None:
        content_digest = _freeze.content_digest_from_spec(_pipeline_state.get_draft(pipeline_id))
    else:
        content_digest = _freeze.compute_content_digest({
            "tools": sorted(f"{n}={v or ''}" for n, v in parsed),
            "platform": platform, "accel": accel,
        })

    name = pipeline_name or env_name
    sif = f"{name}.sif"
    adopt = _biocontainers.resolve_biocontainer(parsed)
    conda_lock_path = None
    shipped_binaries: list[dict] = []
    build_method = None

    draft = _pipeline_state.get_draft(pipeline_id) if pipeline_id else None
    non_conda = _freeze.non_conda_installs(draft) if draft else []
    can_adopt = bool(adopt.get("found") and adopt.get("image_by_digest") and not gated)

    def _deliver_built(image):
        """docker-save tarball + optional push → registry-free Apptainer contract."""
        idg = _docker.image_digest(image)
        tar_path = _env_mgr.project_root / "docker_images" / name / f"{name}.tar"
        save = _docker.save_archive(image, tar_path)
        tball = save.get("tarball") if save.get("success") else None
        pushed = None
        if push_target and not gated:
            if _docker._run(["docker", "tag", image, push_target])["returncode"] == 0 and \
               _docker._run(["docker", "push", push_target], timeout=900)["returncode"] == 0:
                pushed = push_target
        h = _freeze.apptainer_delivery(mode="build", sif_name=sif, push_target=pushed,
                                       tarball=tball, gated=gated)
        return idg, tball, h

    if can_adopt and not non_conda:
        # ADOPT — pure-conda env with a published biocontainer. The biocontainer
        # IS the artifact (provenance = its digest), so no conda-lock of our env.
        mode, image, image_digest, tarball = "adopt", adopt["image_by_digest"], adopt["digest"], None
        hpc = _freeze.apptainer_delivery(mode="adopt", sif_name=sif,
                                         image_by_digest=adopt["image_by_digest"])

    else:
        # CONTAINER-NATIVE BUILD — the SINGLE build path (Phase E: freeze no longer
        # uses conda-pack at all). env_freeze installs + validates IN the ship image
        # (one generic bake, validated==shipped), covering hand-installed tools
        # (binary/jar/source/cargo/go/perl), pure conda, and pip/R — cross-arch too
        # (build in-container, no host-arch conda-pack and no cross-arch refusal).
        # A biocontainer is not adopted when the env has non-conda installs it can't
        # represent (adopting would ship a different, unvalidated artifact).
        if can_adopt:
            adopt = {**adopt, "skipped": "env has non-conda installs a biocontainer cannot represent"}
        docker_platform = _CONDA_TO_DOCKER_PLATFORM.get(platform, platform)
        conda_deps = _freeze.requested_conda_specs(draft) if draft else []
        if not conda_deps and not non_conda:
            # no draft (or no recorded conda installs): treat the requested tools as
            # conda specs (the declarative pure-conda case).
            conda_deps = [f"{n}={v}" if v else n for n, v in parsed]
        br = _env_freeze.build_env_image(
            draft or {}, name=name, version=version, conda_deps=conda_deps,
            primary_tools=[n for n, _ in parsed], platform=docker_platform,
            accelerator=draft.get("accelerator") if isinstance(draft, dict) else None,
            license_gated=gated, licenses=licenses, redistributable=not gated)
        if not br.get("success"):
            # build_env_image refuses pip/r_install with no generator, a non-replayable
            # source, or any honesty-contract violation (incl. I13: a gated build needs
            # licenses[]). Surface it verbatim.
            return {"success": False, "stage": "container_build", "request_key": rkey,
                    "adopt_attempt": adopt, "build": br}
        mode, build_method, image = "build", "container-native", br["image"]
        image_digest = br["image_digest"]
        _, tarball, hpc = _deliver_built(image)   # docker-save tarball + Apptainer contract
        # record what shipped: each baked long-tail step (the command IS the provenance).
        shipped_binaries = [{"name": s.get("purpose", ""), "command": s.get("command", "")}
                            for s in br.get("longtail_steps", [])]
        if conda_deps:  # portable lock for the conda layer (image digest is the real anchor)
            plats = [platform] if platform.startswith("linux") else ["linux-64", platform]
            cl = _env_mgr.generate_lock(env_name, platforms=plats)
            conda_lock_path = cl.get("lockfile") if cl.get("success") else None

    record = _freeze.freeze_record(
        request_key=rkey, content_digest=content_digest, mode=mode,
        image=image, image_digest=image_digest, platform=platform, gated=gated,
        conda_lock_path=conda_lock_path, tarball=tarball, hpc=hpc,
    )
    if build_method:
        record["build_method"] = build_method
    if shipped_binaries:
        record["shipped_binaries"] = shipped_binaries
    _env_cache.register(rkey, record)
    return {"success": True, "cache_hit": False, "adopt_attempt": adopt, **record}


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@mcp.tool()
def generate_user_guide(
    pipeline_id: str = "",
    spec: dict = {},
    freeze_request_key: str = "",
    write: bool = True,
) -> dict:
    """Render the Layer-2 user guide for a pipeline (Markdown) from its PASSING,
    validated run — every command shown was actually executed and its outputs
    checked (drawn only from validated pipeline_steps + a self-tested
    usage.command_template). A workflow consumes its env BY DIGEST: the
    "Get the environment" section is the freeze() Apptainer delivery and
    provenance pins the content/image digests.

    Pass `pipeline_id` (uses its draft) or a `spec` dict. `freeze_request_key`
    looks the frozen artifact up in the EnvCache (e.g. 'samtools=1.21|linux-64|
    none') to source the HPC delivery + digests. Writes env_reports/{name}.GUIDE.md
    when write=True.
    """
    s = spec or (_pipeline_state.get_draft(pipeline_id) if pipeline_id else None)
    if not s:
        return {"success": False, "error": "provide pipeline_id (with a draft) or a spec dict"}
    fr = _env_cache.lookup(freeze_request_key) if freeze_request_key else None
    md = _user_guide.render_user_guide(s, freeze_record=fr)
    result = {
        "success": True,
        "markdown": md,
        "commands_shown": len(_user_guide.executed_commands(s)),
        "env_pinned": bool(fr),
    }
    if write:
        out = _env_mgr.project_root / "env_reports" / f"{s.get('pipeline_name','pipeline')}.GUIDE.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        result["path"] = str(out)
    return result


@mcp.tool()
def seal_workflow(
    pipeline_id: str,
    freeze_request_key: str,
    workflow_name: str = "",
    description: str = "",
    write: bool = True,
) -> dict:
    """Seal the Layer-2 WORKFLOW: validate the run-side invariants, PIN the
    frozen environment by digest, render the user guide, write the WorkflowSpec.

    The two-layer split. Layer 1 (ENVIRONMENT) = finalize + freeze: a
    content-addressed, reusable artifact. Layer 2 (WORKFLOW) CONSUMES it. Call
    freeze() first to produce the env artifact, then seal_workflow with its
    request_key to pin the env by digest — the wall that lets the workflow layer
    ignore install concerns.

    Refuses to write if any workflow invariant fails (I0 shape · I3 validated
    outputs · I6 paths · I7 resources · I8 input provenance); the env-build
    invariants are Layer 1's concern. Writes {workflow_name}.workflow.yaml +
    {workflow_name}.GUIDE.md (the guide rendered from the passing run).
    """
    draft = _pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return {"success": False, "error": f"unknown pipeline_id: {pipeline_id}"}
    fr = _env_cache.lookup(freeze_request_key)
    if not fr:
        return {"success": False,
                "error": f"no frozen env for '{freeze_request_key}' — run freeze() first"}

    from agent.skills.spec_writer import (check_workflow_invariants, self_test_usage,
                                          write_workflow_spec)
    violations = check_workflow_invariants(draft)
    if violations:
        return {"success": False, "stage": "workflow_invariants",
                "violations": violations, "violation_count": len(violations)}

    # The usage.command_template IS the workflow's run contract — establish
    # usage_verified honestly by self-testing it (I4), since the draft doesn't
    # persist the field (it's derived only at validate/finalize). A verified
    # template is what the guide shows as the runnable form.
    usage_ok = False
    if draft.get("usage") and draft.get("conda_env"):
        try:
            usage_ok = bool(self_test_usage(draft, _env_mgr, validator=_validator).get("ok"))
        except Exception:
            usage_ok = False
    render_spec = {**draft, "usage_verified": usage_ok}
    guide_md = _user_guide.render_user_guide(render_spec, freeze_record=fr)
    key_packages = _user_guide.key_packages(draft)

    from datetime import datetime, timezone
    wname = workflow_name or f"{draft.get('pipeline_name', 'workflow')}_workflow"
    wf = {
        "workflow_name":      wname,
        "description":        description or draft.get("description", ""),
        "created_at":         datetime.now(timezone.utc).isoformat(),
        "env_request_key":    fr.get("request_key", freeze_request_key),
        "env_content_digest": fr.get("content_digest", ""),
        "env_image":          fr.get("image", ""),
        "env_hpc_delivery":   fr.get("hpc_delivery", {}),
        "pipeline_status":    draft.get("pipeline_status", "in_progress"),
        "usage_verified":     usage_ok,
        "validated_in_shipped_image": _user_guide.validated_in_shipped_image(draft, fr),
        "usage":              draft.get("usage"),
        "pipeline_steps":     draft.get("pipeline_steps", []),
        # External sources carried so the artifact self-verifies (I8 standalone).
        "test_data":            draft.get("test_data"),
        "reference_databases":  draft.get("reference_databases", []),
        "runtime_configs":      draft.get("runtime_configs", []),
        "authored_artifacts":   draft.get("authored_artifacts", []),
        "driver_env":         {"conda_env": draft.get("conda_env"),
                               "python_version": draft.get("python_version"),
                               "key_packages": key_packages},
        "user_guide":         guide_md,
    }
    # The artifact must pass its OWN run-side invariants — validate what we WRITE,
    # not just the draft we sealed from (the draft is richer; the artifact must
    # stand on its own).
    self_violations = check_workflow_invariants(wf)
    if self_violations:
        return {"success": False, "stage": "workflow_self_verify",
                "reason": "constructed WorkflowSpec failed its own run-side invariants — "
                          "it would not be re-verifiable standalone",
                "violations": self_violations, "violation_count": len(self_violations)}
    result = {
        "success": True,
        "workflow_name": wname,
        "env_pinned_digest": fr.get("content_digest"),
        "env_image": fr.get("image"),
        "commands_shown": len(_user_guide.executed_commands(draft)),
    }
    if write:
        out = write_workflow_spec(wf, config)
        if out.get("error"):
            return {"success": False, **out}
        result.update(out)
    else:
        result["workflow"] = wf
    return result


@mcp.tool()
def write_pipeline_provenance(
    pipeline: str,
    conda_env_path: str,
    pipeline_spec_path: str,
    output_files: list[dict],
    output_dir: str,
    sample_key: str,
    # genome reference — optional for tools that don't use a reference FASTA
    genome_build: str = "",
    chromosome: str = "",
    reference_path: str = "",
    # input types — at least one must be provided
    reads: Optional[dict] = None,
    bam_input: Optional[dict] = None,
    vcf_input: Optional[dict] = None,
    assembly_input: Optional[dict] = None,
    phenotype: Optional[dict] = None,
    pedigree: Optional[dict] = None,
    genotype_array: Optional[dict] = None,
    quantitative_traits: Optional[dict] = None,
    upstream_pipelines: Optional[list[str]] = None,
    parameters: Optional[dict] = None,
) -> dict:
    """Write a validated provenance YAML for a completed pipeline run.

    output_files: list of {file: str, type: str, indexed: bool}

    Input types (at least one required):
      reads:               {r1, r2?, sample, accession, subset, num_reads, assay_type, end_type, database}
      bam_input:           {bam: str, bai: str}
      vcf_input:           {vcf: str, tbi?: str, genome_build: str, upstream_pipeline?: str, sample_ids?: []}
      assembly_input:      {assembly: str, upstream_pipeline?: str}
      phenotype:           {ontology?: str, terms: [str], source?: str}
      pedigree:            {ped: str, proband?: str}
      genotype_array:      {file: str, format: hapmap|plink_bed|vcf|dosage|bgen,
                            bim?: str, fam?: str, n_samples?: int, n_snps?: int,
                            genome_build?: str, upstream_pipeline?: str}
      quantitative_traits: {traits: [str], file: str, n_samples?: int,
                            measurement_type?: continuous|binary|ordinal}

    genome_build / chromosome / reference_path are optional for tools that do not
    consume a reference FASTA (e.g. variant prioritizers, phenotype scorers, GWAS)."""
    inputs: dict[str, Any] = {
        "pipeline":           pipeline,
        "conda_env_path":     conda_env_path,
        "pipeline_spec_path": pipeline_spec_path,
        "genome_build":       genome_build,
        "chromosome":         chromosome,
        "reference_path":     reference_path,
        "output_files":       output_files,
        "output_dir":         output_dir,
        "sample_key":         sample_key,
    }
    if reads:                inputs["reads"]                = reads
    if bam_input:            inputs["bam_input"]            = bam_input
    if vcf_input:            inputs["vcf_input"]            = vcf_input
    if assembly_input:       inputs["assembly_input"]       = assembly_input
    if phenotype:            inputs["phenotype"]            = phenotype
    if pedigree:             inputs["pedigree"]             = pedigree
    if genotype_array:       inputs["genotype_array"]       = genotype_array
    if quantitative_traits:  inputs["quantitative_traits"]  = quantitative_traits
    if upstream_pipelines:   inputs["upstream_pipelines"]   = upstream_pipelines
    if parameters:           inputs["parameters"]           = parameters
    return _write_provenance(inputs, config)


@mcp.tool()
def list_installed_pipelines() -> dict:
    """List all pipelines installed and validated, with Docker tags and validation status."""
    return _list_pipelines(config)


# ---------------------------------------------------------------------------
# R package utilities
# ---------------------------------------------------------------------------

def _parse_dcf(text: str) -> dict[str, str]:
    """Parse a Debian Control File (R DESCRIPTION format) into a flat dict."""
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")):
            if current_key:
                fields[current_key] += " " + line.strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            current_key = key.strip()
            fields[current_key] = val.strip()
    return fields


_R_RUNTIME_INSTALL_RE = re.compile(
    r"""(?:BiocManager::install
        |install\.packages
        |requireNamespace            # if(!requireNamespace("X")) is the canonical lazy-install signal
        |library
        |require)
        \s*\(
    """,
    re.VERBOSE,
)

_R_RUNTIME_QUOTED_RE = re.compile(r"""(['"])([A-Za-z0-9._]+)\1""")


def _scan_r_runtime_installs(github_repo: str, ref: str) -> list[str]:
    """Look for `BiocManager::install("X")` / `install.packages("X")` calls in the
    R/ source directory of a GitHub R package. Returns a deduplicated list of
    package names. Best-effort — failures are silent (we still want the
    DESCRIPTION info to be returned)."""
    api_url = f"https://api.github.com/repos/{github_repo}/contents/R?ref={ref}"
    try:
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            listing = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    found: set[str] = set()
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if not name.endswith(".R") and not name.endswith(".r"):
            continue
        raw_url = entry.get("download_url")
        if not raw_url:
            continue
        try:
            with urllib.request.urlopen(raw_url, timeout=15) as f:
                source = f.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        # Strip comment lines so we don't grab examples from comments.
        cleaned = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        # Find each call site, then pluck every quoted name from the argument list.
        for m in _R_RUNTIME_INSTALL_RE.finditer(cleaned):
            # The regex captures only the first name; scan the substring containing
            # the call's arg list for all quoted strings (handles c('a','b','c')).
            start = m.start()
            depth = 0
            i = start
            while i < len(cleaned) and cleaned[i] != "(":
                i += 1
            arglist_start = i + 1
            depth = 1
            i = arglist_start
            while i < len(cleaned) and depth > 0:
                if cleaned[i] == "(":
                    depth += 1
                elif cleaned[i] == ")":
                    depth -= 1
                i += 1
            arglist = cleaned[arglist_start:i-1]
            for q in _R_RUNTIME_QUOTED_RE.finditer(arglist):
                pkg = q.group(2)
                # Filter out obvious non-package strings (version numbers, paths, options)
                if pkg and not pkg.startswith(("/", "./", "http", "https")) and "." not in pkg[:1]:
                    if re.match(r"^[A-Za-z][A-Za-z0-9._]*$", pkg):
                        found.add(pkg)
    return sorted(found)


def _parse_pkg_list(raw: str) -> list[str]:
    """Extract bare package names from a comma-separated dep field, stripping version specs."""
    names = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name = re.split(r"[\s(]", item)[0].strip()
        if name and name != "R":
            names.append(name)
    return names


@mcp.tool()
def fetch_r_package_deps(github_repo: str, ref: str = "HEAD") -> dict:
    """Fetch the DESCRIPTION file from a GitHub R package and parse all dependencies.

    Use this BEFORE installing any R package from GitHub so you can pre-install
    all dependencies, then call remotes::install_github(..., dependencies=FALSE).

    github_repo: owner/repo, e.g. "jiabowang/GAPIT3"
    ref:         branch, tag, or commit SHA (default HEAD → main/master)

    Returns:
      package_name, version, r_version_required
      imports, depends, suggests, linking_to — raw dep name lists
      all_required  — union of imports + depends + linking_to (what must be installed)
      install_strategy — ordered steps: conda first, then BiocManager for everything
                         else (BiocManager resolves both CRAN and Bioconductor),
                         GitHub last with dependencies=FALSE."""
    url = f"https://raw.githubusercontent.com/{github_repo}/{ref}/DESCRIPTION"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        return {"success": False, "error": str(e), "url": url}

    fields = _parse_dcf(content)

    imports    = _parse_pkg_list(fields.get("Imports", ""))
    depends    = _parse_pkg_list(fields.get("Depends", ""))
    suggests   = _parse_pkg_list(fields.get("Suggests", ""))
    linking_to = _parse_pkg_list(fields.get("LinkingTo", ""))

    r_ver_match = re.search(r"R\s*\(>=[^)]*\)", fields.get("Depends", ""))
    r_version_required = r_ver_match.group(0) if r_ver_match else ""

    all_required = sorted(set(imports + depends + linking_to))

    # Hunt for *undeclared* transitive deps — R packages whose .onLoad hooks or
    # zzz.R do `BiocManager::install("X")` / `install.packages("X")` for things
    # not listed in DESCRIPTION. GAPIT does this with snpStats; without this
    # discovery the agent learns about it only by the install failing.
    undeclared_runtime_installs = _scan_r_runtime_installs(github_repo, ref)
    undeclared_set = sorted({
        pkg for pkg in undeclared_runtime_installs
        if pkg not in set(all_required + suggests) and pkg != fields.get("Package", "")
    })

    return {
        "success": True,
        "github_repo": github_repo,
        "ref": ref,
        "url": url,
        "package_name": fields.get("Package", ""),
        "version": fields.get("Version", ""),
        "r_version_required": r_version_required,
        "imports": imports,
        "depends": depends,
        "suggests": suggests,
        "linking_to": linking_to,
        "all_required": all_required,
        "undeclared_runtime_installs": undeclared_set,
        "install_strategy": [
            "1. For each dep in all_required, call search_package to check conda-forge "
            "(r-{lowercase}) and bioconda (bioconductor-{lowercase}) availability.",
            "2. Install all conda-available deps in one install_conda_packages call.",
            "3. For deps not found on conda, install via BiocManager — it resolves both "
            "CRAN and Bioconductor packages without needing to know which is which: "
            "Rscript -e \"lib<-file.path(Sys.getenv('CONDA_PREFIX'),'lib','R','library'); "
            "if(!requireNamespace('BiocManager',quietly=TRUE)) "
            "install.packages('BiocManager',lib=lib); "
            "BiocManager::install(c('pkg1','pkg2'), lib=lib, ask=FALSE, update=FALSE)\"",
            f"4. Finally: remotes::install_github('{github_repo}', "
            "lib=file.path(Sys.getenv('CONDA_PREFIX'),'lib','R','library'), "
            "dependencies=FALSE)",
        ],
    }


# ---------------------------------------------------------------------------
# Pipeline state accumulator
# ---------------------------------------------------------------------------

@mcp.tool()
def start_pipeline(pipeline_name: str, description: str) -> dict:
    """Start a new pipeline draft (or silently resume an existing one).

    Returns pipeline_id (= pipeline_name). Pass pipeline_id to subsequent
    tools (search_package, create_conda_env, verify_installation, run_in_env,
    validate_output, run_pipeline_step, select_test_data) so their results
    are auto-merged into a server-side draft. You don't hand-assemble the
    final artifacts — freeze(pipeline_id) produces the Layer-1 env image and
    seal_workflow(pipeline_id, freeze_request_key) writes the Layer-2
    WorkflowSpec + guide from the validated run.

    Resume semantics: if a draft for this pipeline_name already exists (e.g.
    after an MCP restart mid-install), it is loaded and resumed silently;
    `resumed: true` is set in the return so you know. On resume, a `summary`
    block is included describing what's already in the draft so the agent can
    decide whether to continue from where the prior run left off or call
    discard_pipeline_draft and start fresh."""
    r = _pipeline_state.start(pipeline_name, description)
    if r.get("resumed"):
        draft = _pipeline_state.get_draft(pipeline_name) or {}
        install_steps = draft.get("install_steps", []) or []
        pipeline_steps = draft.get("pipeline_steps", []) or []
        last_install = install_steps[-1] if install_steps else None
        last_pipeline = pipeline_steps[-1] if pipeline_steps else None
        # Surface any pipeline step with no detected outputs and no validation —
        # the silent-empty-success pattern that bit the prior Exomiser run.
        suspect_steps = [
            s.get("step")
            for s in pipeline_steps
            if not (s.get("detected_outputs") or s.get("outputs") or s.get("validation"))
        ]
        r["summary"] = {
            "conda_env":              draft.get("conda_env"),
            "env_status":             draft.get("env_status"),
            "pipeline_status":        draft.get("pipeline_status"),
            "install_steps_count":    len(install_steps),
            "install_steps_failed":   sum(1 for s in install_steps if s.get("returncode") not in (None, 0)),
            "pipeline_steps_count":   len(pipeline_steps),
            "pipeline_steps_failed":  sum(1 for s in pipeline_steps if s.get("returncode") not in (None, 0)),
            "packages_recorded":      len(draft.get("packages", []) or []),
            "test_data":              draft.get("test_data") is not None,
            "docker":                 draft.get("docker") is not None,
            "usage":                  draft.get("usage") is not None,
            "last_install_tool":      last_install.get("tool") if last_install else None,
            "last_pipeline_tool":     last_pipeline.get("tool") if last_pipeline else None,
            "suspect_unvalidated_steps": suspect_steps,
        }
    return r


@mcp.tool()
def discard_pipeline_draft(pipeline_id: str) -> dict:
    """Delete a pipeline draft without finalizing. Use when you want to start
    fresh with the same pipeline_name and don't want resume semantics."""
    return _pipeline_state.discard(pipeline_id)


@mcp.tool()
def show_pipeline_draft(pipeline_id: str) -> dict:
    """Return the current accumulated draft for inspection. Does not modify
    or finalize anything."""
    draft = _pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return {"error": f"unknown pipeline_id: {pipeline_id}"}
    return {"pipeline_id": pipeline_id, "draft": draft}


@mcp.tool()
def patch_pipeline(pipeline_id: str, patches: dict) -> dict:
    """Deep-merge agent-authored patches into the draft. Accepts only the
    keys no primitive produces directly: description, notes, final_summary,
    conda_env, created_at, python_version, reference_free, runtime_environment,
    runtime_configs, reference_databases, service_dependencies, usage.

    Patches to runtime-captured or finalize-derived fields (pipeline_steps,
    install_steps, packages, verifications, test_data, authored_artifacts,
    docker, env_status, pipeline_status, docker_status, usage_verified,
    lock_sha256) are rejected — use the dedicated primitive instead so the
    spec stays anchored to observed reality.

    Lists are replaced wholesale (pipeline_steps and install_steps are blocked
    here anyway; they merge by step number through their own primitives).

    Deletion: pass the literal string "__DELETE__" as a value inside a
    patchable key's subtree to remove that nested key. e.g.
    {"runtime_environment": {"min_ram_gb": "__DELETE__"}} drops the
    min_ram_gb hint while leaving the rest of runtime_environment intact.
    Use this rather than setting to None — None can trigger downstream
    attribute errors."""
    return _pipeline_state.patch(pipeline_id, patches)


@mcp.tool()
def stage_authored_artifact(
    pipeline_id: str,
    path: str,
    role: str,
    description: str,
    content: str = "",
    generated_by: str = "",
    language: str = "",
    overwrite: bool = True,
) -> dict:
    """Stage an agent-authored artifact and record its provenance in the spec.

    Use this whenever you write a file or perform a transformation OUTSIDE the
    pipeline_step / install_step primitives — driver scripts, synthetic test
    inputs, hand-staged BAM/VCF/FASTA, sed-massaged configs, generated CSV/TSV
    fixtures. These artifacts are otherwise invisible to the spec; a step that
    consumes them looks like an orphan to the I8 composition-coherence walk
    and finalize fails.

    Two modes:

      content mode      — supply `content` (text). The runtime writes the file
                          at `path`, computes sha256, and stores the contents
                          (full if small, excerpt + full sha256 if large)
                          inside the spec. Reviewers can audit exactly what
                          the agent generated.

      generated_by mode — supply `generated_by` (the shell command you ran),
                          with the file already on disk at `path`. The runtime
                          records the command as the genesis and sha256s the
                          bytes. Use for binary outputs (BAM, FASTA, indexed
                          DB, pickled models).

    Honesty effect:
      - Path is added to the I8 universe of external sources, so downstream
        pipeline_steps can legally name it as an input.
      - sha256 is re-verified at finalize (I9). If the on-disk content drifts
        from the recorded sha256, the spec is refused — silent tampering is
        impossible.
      - `authored_artifacts` is blocked from patch_pipeline, so the only path
        to record an artifact is this primitive (the sha256 anchor is
        compulsory).

    Returns: {success, path, sha256, size_bytes, role, mode}.
    """
    import hashlib
    from datetime import datetime, timezone

    if bool(content) == bool(generated_by):
        return {
            "error": "exactly one of `content` (text content to write) "
                     "or `generated_by` (genesis command for an existing file) must be supplied",
        }
    if not Path(path).is_absolute():
        return {"error": f"path must be absolute, got: {path!r}"}

    p = Path(path)
    mode: str

    if content:
        if p.exists() and not overwrite:
            return {"error": f"path already exists and overwrite=False: {path}"}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        except Exception as e:
            return {"error": f"could not write artifact: {e!r}", "path": path}
        mode = "content"
    else:
        if not p.exists():
            return {
                "error": f"generated_by mode requires the file to already exist on disk: {path}",
            }
        mode = "generated_by"

    try:
        raw = p.read_bytes()
    except Exception as e:
        return {"error": f"could not read back artifact for sha256: {e!r}", "path": path}

    sha256 = hashlib.sha256(raw).hexdigest()
    size_bytes = len(raw)

    # For content mode, embed the full text up to ~64 KiB; otherwise an excerpt.
    # Binary-mode artifacts get a short hex preview so a reviewer can sanity-check.
    SPEC_FULL_LIMIT = 65536
    excerpt: Optional[str] = None
    content_full_in_spec = False
    if mode == "content":
        if size_bytes <= SPEC_FULL_LIMIT:
            excerpt = content
            content_full_in_spec = True
        else:
            excerpt = content[:4096] + f"\n... [truncated, total {size_bytes} bytes]"
    else:
        # Show a short hex preview for binary artifacts so the spec isn't blind.
        excerpt = f"<binary; first 64 bytes hex: {raw[:64].hex()}>"

    artifact = {
        "path":        str(p),
        "role":        role,
        "description": description,
        "sha256":      sha256,
        "size_bytes":  size_bytes,
        "created_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if language:
        artifact["language"] = language
    if excerpt is not None:
        artifact["content_excerpt"] = excerpt
    artifact["content_full_in_spec"] = content_full_in_spec
    if generated_by:
        artifact["generated_by"] = generated_by

    idx = _pipeline_state.add_authored_artifact(pipeline_id, artifact)
    if idx is None:
        return {"error": f"unknown pipeline_id: {pipeline_id}", "path": path}

    return {
        "success":   True,
        "path":      str(p),
        "sha256":    sha256,
        "size_bytes": size_bytes,
        "role":      role,
        "mode":      mode,
        "pipeline_merge": {
            "status":        "merged",
            "pipeline_id":   pipeline_id,
            "artifact_index": idx,
        },
    }


@mcp.tool()
def mark_step_validated(
    pipeline_id: str,
    step: int,
    validation_status: str = "passed",
) -> dict:
    """Set the validation_status of a pipeline_step. The pipeline-level
    pipeline_status derives from these at finalize time.

    Use this when a step's outputs are known good but validate_output wasn't
    called for them (e.g. the step's outputs were checked by the next step's
    success, or the LLM verified them by other means). Operates only on
    pipeline_steps — install_steps don't carry a validation_status field;
    their success is captured by status (returncode==0)."""
    if validation_status not in {"passed", "failed"}:
        return {"error": "validation_status must be 'passed' or 'failed'",
                "got": validation_status}
    # Guard against the silent-empty-success trap: a step that exited 0 but
    # produced no detected outputs and was never validated cannot be honestly
    # called "passed". The agent should use "failed" or retry the step.
    if validation_status == "passed":
        draft = _pipeline_state.get_draft(pipeline_id) or {}
        steps = draft.get("pipeline_steps", [])
        if 1 <= step <= len(steps):
            s = steps[step - 1]
            outs  = s.get("detected_outputs") or s.get("outputs") or []
            vlds  = s.get("validation") or {}
            if not outs and not vlds:
                return {
                    "error": "cannot mark step 'passed' with no detected outputs and no validation entries — "
                             "this is the 'ran without error but produced nothing' pattern. Investigate the step "
                             "or use validation_status='failed' if the run was actually broken.",
                    "pipeline_id": pipeline_id, "step": step,
                }
    ok = _pipeline_state.mark_pipeline_step_validated(pipeline_id, step, validation_status)
    return (
        {"status": "set", "pipeline_id": pipeline_id, "step": step,
         "validation_status": validation_status}
        if ok else
        {"error": "unknown pipeline_id or step out of range",
         "pipeline_id": pipeline_id, "step": step}
    )


@mcp.tool()
def run_install_command(
    env_name: str,
    command: str,
    installed_packages: list[dict] = [],
    working_dir: str = "",
    timeout_seconds: int = 1800,
    pipeline_id: str = "",
    step: int = 0,
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
    verify_command: str = "",
) -> dict:
    """Run an install command inside a conda environment (BiocManager::install,
    remotes::install_github, pip install, downloading reference DBs, etc.).

    This is the install-side mirror of run_in_env: same shape, same semantics,
    but the resulting step lands in draft.install_steps (not pipeline_steps)
    so the environment-build journey is recorded separately from the actual
    algorithm/analysis runs.

    installed_packages should list what this command installed:
        [{name: 'GAPIT', version: '4.1.0', channel: 'github', source: '...'}, ...]
    These power the side-by-side "command → packages" rendering in the report,
    AND each entry is appended to draft.packages as a PackageRecord (with an
    install_method derived from `channel`) — closing the loop with verify_installation,
    which can then patch verify_command / verify_output onto that record.

    Pass `step=N` to replace install_step N (for retries). Default is append.

    verify_command: an optional shell command run AFTER the install command in the
    same env. If supplied AND the install command returns 0, but the verify command
    returns non-zero, the step is recorded as failed (returncode=verify exit code,
    stderr includes the verify output). This catches silent-success cases like
    `R install.packages` printing 'ERROR: lazy loading failed' while the Rscript
    process still exits 0. Recommended for R installs:
        verify_command="Rscript -e 'if(!requireNamespace(\"GAPIT\")) quit(status=1)'"
    and for pip-source installs:
        verify_command="python -c 'import mypkg'"

    Return keys: returncode, stdout, stderr, success, command, runtime_seconds,
                 inputs, detected_outputs, [verify_returncode, verify_output],
                 [pipeline_merge]."""
    result = _env_mgr.run_in_env(
        env_name, command,
        working_dir=working_dir or None,
        timeout=timeout_seconds,
        inputs=[],
        watch_dir=None,
    )
    if verify_command and result.get("returncode") == 0:
        vresult = _env_mgr.run_in_env(
            env_name, verify_command,
            working_dir=working_dir or None,
            timeout=120,
        )
        verify_rc  = vresult.get("returncode", 1)
        verify_out = ((vresult.get("stdout") or "") + (vresult.get("stderr") or ""))[:500]
        result["verify_command"]   = verify_command
        result["verify_returncode"] = verify_rc
        result["verify_output"]    = verify_out
        if verify_rc != 0:
            result["returncode"] = verify_rc
            result["success"]    = False
            result["stderr"]     = (result.get("stderr") or "") + (
                f"\n\n[verify_command failed: {verify_command}]\n{verify_out}"
            )
    if pipeline_id:
        step_data = {
            "tool":               tool or (command.split() or [""])[0],
            "subcommand":         subcommand or None,
            "purpose":            purpose or None,
            "command":            command,
            "returncode":         result.get("returncode"),
            "runtime_seconds":    result.get("runtime_seconds"),
            "installed_packages": installed_packages or [],
        }
        if verify_command:
            step_data["verify_command"]   = verify_command
            step_data["verify_returncode"] = result.get("verify_returncode")
        step_data = {k: v for k, v in step_data.items() if v is not None}
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # No more dual-write to draft.packages. The finalize-time package
        # builder picks up every `installed_packages` entry from successful
        # install_steps automatically — single source of truth.
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def select_test_data(
    genome_build: str = "hg38",
    assay_type: str = "",
    end_type: str = "",
    sample: str = "",
    accession: str = "",
    subset: str = "",
    pipeline_id: str = "",
) -> dict:
    """Find a matching test dataset on disk and return a TestDataRef-shaped
    dict ready to drop into the spec. If pipeline_id is supplied, also sets
    draft.test_data.

    Match is best-effort: each criterion scores points; the highest-scoring
    AVAILABLE dataset wins. Returns {test_data, available, match_score}, or
    {error} if nothing on disk matches at all. Inspect the result and call
    again with different criteria if the match is wrong."""
    all_data = _list_resources({"resource_type": "test_data"}, config).get("test_data", [])
    sequencing = [d for d in all_data if d.get("type") not in ("phenopacket", "pipeline_output")]

    def _score(d: dict) -> int:
        s = 0
        if genome_build and d.get("genome_build") == genome_build: s += 32
        if assay_type   and d.get("assay_type")   == assay_type:   s += 16
        if end_type     and d.get("end_type")     == end_type:     s += 8
        if sample       and d.get("sample")       == sample:       s += 4
        if accession    and d.get("accession")    == accession:    s += 2
        if subset       and d.get("subset")       == subset:       s += 1
        return s

    scored = [(d, _score(d), bool(d.get("available"))) for d in sequencing]
    if not scored:
        return {"error": "no sequencing test data on disk"}
    scored.sort(key=lambda x: (x[2], x[1]), reverse=True)
    best, score, available = scored[0]
    if score == 0:
        return {
            "error": "no test data matches the requested criteria",
            "criteria": {
                "genome_build": genome_build, "assay_type": assay_type,
                "end_type": end_type, "sample": sample,
                "accession": accession, "subset": subset,
            },
        }

    test_data_ref = {
        "genome_build":  best.get("genome_build", ""),
        "read_type":     best.get("read_type"),
        "end_type":      best.get("end_type"),
        "assay_type":    best.get("assay_type"),
        "sample":        best.get("sample"),
        "accession":     best.get("accession"),
        "subset":        best.get("subset"),
        "num_reads":     best.get("num_reads"),
        "r1":            best.get("r1"),
        "r2":            best.get("r2"),
        "core_data_dir": best.get("core_dir"),
    }
    test_data_ref = {k: v for k, v in test_data_ref.items() if v is not None}

    result = {"test_data": test_data_ref, "available": available, "match_score": score}
    if pipeline_id:
        ok = _pipeline_state.set_test_data(pipeline_id, test_data_ref)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id} if ok
            else {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


# ---------------------------------------------------------------------------
# Async background job tools — watchdog-proof execution
#
# Use these for any operation that may run silently for >5 minutes:
#   - large downloads (Exomiser data bundles, BLAST nt, full genome FASTAs)
#   - long conda solves on big dependency graphs
#   - multi-hour assembler / aligner runs on real data
#   - any tool whose normal output stream is sparse
#
# Pattern:
#   r = run_in_background("curl -L --progress-bar -o big.zip 'URL' && unzip big.zip", env_name="bioinf_x")
#   # job_id returned immediately; agent does other work
#   while check_job(r['job_id'])['state'] == 'running':
#       sleep(30)   # or do unrelated tool calls
#   final = check_job(r['job_id'])
# ---------------------------------------------------------------------------


@mcp.tool()
def run_in_background(
    command: str,
    env_name: str = "",
    job_id: str = "",
    working_dir: str = "",
) -> dict:
    """Spawn `command` as a background process. Returns immediately.

    Use this for any operation that may run silently for more than 5 minutes —
    the agent stream-watchdog kills tool calls that go silent that long. Big
    downloads, conda solves, assembler runs, etc.

    Arguments:
      command:     full shell command (bash -c executes it; pipes / redirects OK)
      env_name:    if set, runs inside that conda env via `conda run --prefix`
      job_id:      caller-supplied (must be unique among running jobs).
                   If empty, a 12-char hex ID is auto-generated.
      working_dir: subprocess cwd (default: project root)

    Returns:
      {job_id, status_path, log_path, pid, state="running"}

    Use `check_job(job_id)` to poll. The status file is durable — survives
    server restarts and is queryable indefinitely after the job ends.
    """
    return _job_manager.start(
        command, env_name=env_name, job_id=job_id, working_dir=working_dir,
    )


@mcp.tool()
def check_job(job_id: str, log_tail_lines: int = 30) -> dict:
    """Return the current state of a background job. Non-blocking; ~50 ms.

    States: running | exited | cancelled
    Always includes a `log_tail` (last N lines of combined stdout+stderr) so you
    can monitor progress (e.g. curl's progress bar) without reading the full log
    file. `bytes_logged` is the size of the log so far — a download's progress
    can be inferred from this.

    For a job that has just exited, this is also where you learn it terminated —
    the state flips from "running" to "exited" on the first check after the
    subprocess died.
    """
    return _job_manager.check(job_id, log_tail_lines=log_tail_lines)


@mcp.tool()
def cancel_job(job_id: str, force: bool = False) -> dict:
    """Terminate a running job. SIGTERM by default; force=True sends SIGKILL.

    Always signals the whole process group so children (e.g. unzip launched
    after curl in a chained command) also die. A SIGTERM is followed by a 5 s
    grace window before promoting to SIGKILL.
    """
    return _job_manager.cancel(job_id, force=force)


@mcp.tool()
def list_jobs(include_terminated: bool = True) -> dict:
    """List all jobs ever started on this machine, newest first.

    include_terminated: when False, only currently-running jobs are returned.
    """
    return {"jobs": _job_manager.list_jobs(include_terminated=include_terminated)}


# ---------------------------------------------------------------------------
# Autonomous install entry point
# ---------------------------------------------------------------------------


@mcp.tool()
def install_pipeline_brief(name: str, version: str = "", hints: dict = {}) -> dict:
    """Return the install brief for `name` — the structured prompt a downstream
    autonomous agent should follow to install this tool end-to-end.

    This is the substrate for fully-autonomous installs. You (the interactive
    Claude) call this to get the canonical instructions; a subagent or batch
    runner can also pull this brief and execute it without human supervision.
    The brief enumerates the invariants the resulting spec must satisfy and
    the primitives the agent should compose to satisfy them. No per-tool prose
    — the brief is short and the same shape for every tool.

    Returns: {pipeline_name, version, hints, invariants, primitives, protocol}.
    """
    invariants = [
        # Layer 1 — the env image (env_honesty.check_build; install==ship is ONE event,
        # so the per-tier env-build invariants I1/I2/I5/I9/I10/I11/I12/I13/I14 collapse
        # into three structural guarantees enforced INSIDE the shipped image):
        "BUILT: the env image + image_digest resolve in the local Docker daemon",
        "VALIDATED_IN_IMAGE: every tool's evidence command re-runs green INSIDE the shipped image AND references the tool (echo/print/true cheats rejected) — because install==ship, the bytes validated are the bytes that run on HPC",
        "POLICY_CLEAN: I12 accelerator honesty (cuda/rocm need toolkit_version; runtime_verified needs a captured probe + min_driver_version; mps is dev_only) + I13 license firewall (gated => redistributable:false AND licenses[] recorded)",
        # Layer 2 — the workflow run (check_workflow_invariants, over the validated run):
        "I0: every top-level list-of-records holds only dicts (shape sanity)",
        "I3: every pipeline_step has validated detected_outputs AND no validation uses expected_type='any' — declare types via run_pipeline_step's output_types",
        "I4: usage.command_template executes against every declared trial AND each produced file passes type-aware validate_output (samtools/bcftools/json.loads/etc — touch-and-hope cheats fail)",
        "I6: every input/output path is absolute AND every {PLACEHOLDER} in usage.command_template is declared",
        "I7: every rc=0 pipeline_step has resource_usage (wall, peak RSS, peak CPU) — populated by run_pipeline_step / run_step_in_container",
        "I8: every step input traces to a prior step's output OR an external source (test_data, reference_databases, runtime_configs, authored_artifacts)",
    ]
    primitives = [
        "install_conda_packages: bioconda / conda-forge / defaults",
        "install_r_package(source=cran|bioconductor|github:owner/repo): handles library isolation + load-or-die",
        "install_pip_package: handles import verification",
        "install_jar_tool: Java tools — openjdk dep + JAR download + wrapper",
        "install_git_repo(repo_url, tool_name, ref=…): clone-and-run repos that aren't packages (academic script collections) — clones into {env}/share/{tool}, pins commit SHA, optional build + smoke verify; sets install_method.type=source. Pin ref to a tag/commit.",
        "download_reference_database: watchdog-safe via run_in_background; auto-records ReferenceDatabase",
        "run_pipeline_step: run + auto-validate every detected output in one call",
        "stage_authored_artifact: record an agent-written file (driver script, synthetic test data, staged BAM/VCF/FASTA) with verbatim content or genesis command + sha256 anchor — required if any step input is something the agent generated outside MCP",
        "start_service(pipeline_id=…): launch a companion service (Redis, Postgres, web server, Spark) and record the readiness probe — required for service-dependent tools; satisfies I10 on a healthy start",
        "verify_service_dependency: append an additional health probe to a declared service's log; satisfies I10 if start_service's initial probe wasn't recorded against this pipeline",
        "phenopacket_to_vcf: materialize a VCF from a registered phenopacket",
    ]
    protocol = [
        "1. start_pipeline(name) — get pipeline_id; thread it through everything",
        "2. install_*_* primitives — compose the env for this tool; primitives handle category details",
        "3. select_test_data + run_pipeline_step (auto-validates outputs; pass output_types to declare file types). If you write a driver script, generate synthetic test data, or stage a transformed file (BAM/VCF/FASTA) outside MCP, call stage_authored_artifact for each — otherwise the I8 walk treats the file as an orphan input.",
        "4. patch_pipeline with usage (command_template + inputs + outputs.files globs + trials[] for multi-shape I4 coverage; empty trials => single inferred trial). NOTE: patch_pipeline only accepts agent-authored keys (usage, notes, runtime_environment, runtime_configs, reference_databases, description, final_summary). Pipeline_steps / install_steps / packages / verifications / authored_artifacts / service_dependencies are runtime-captured and CANNOT be hand-patched — they flow through their dedicated primitives.",
        "5. freeze(env, tools, pipeline_id=…) — Layer 1: build (or adopt by digest) the content-addressed, HPC-shippable env image; non-conda installs are installed + validated INSIDE the ship image (validated==shipped). Returns a freeze_request_key. Docker daemon must be available.",
        "6. run_step_in_container(freeze_request_key, …) — re-run the workflow's steps INSIDE the frozen image so the recorded run is the one that ships (sets validated_in_shipped_image, captures in-container resource_usage).",
        "7. seal_workflow(pipeline_id, freeze_request_key) — Layer 2: validate the run-side invariants (I0/I3/I6/I7/I8), self-test usage.command_template (I4), pin the env BY DIGEST, and write the WorkflowSpec + user guide rendered from the validated run.",
        "8. write_pipeline_provenance with the right input shape",
    ]
    return {
        "pipeline_name": name,
        "version":       version or "latest",
        "hints":         hints,
        "invariants":    invariants,
        "primitives":    primitives,
        "protocol":      protocol,
        "note": (
            "This brief is the entire 'how to install a pipeline' protocol — no per-tool prose. "
            "Compose primitives until the invariants hold; freeze() machine-verifies the env IN "
            "the shipped image and seal_workflow() machine-verifies the run — neither writes a "
            "fake-able artifact."
        ),
    }


# ---------------------------------------------------------------------------
# Entry point — supports BIOINF_MCP_AUTO_RELOAD=1 for dev hot-reload.
# When set, a background thread watches agent/ for .py changes and exits
# the process on any mtime change. The MCP client reconnects on next call
# and gets the fresh code, eliminating the "I committed but the server is
# stale" foot-gun. Off by default; opt in via env var.
# ---------------------------------------------------------------------------


def _watch_and_exit_on_change():
    """Poll agent/ + config/ for .py / .yaml mtime changes. exit() on any."""
    import threading, time as _time
    project_root = Path(__file__).parent.parent.resolve()
    watch_dirs = [project_root / "agent", project_root / "config"]

    def snapshot() -> dict:
        out = {}
        for d in watch_dirs:
            if not d.exists():
                continue
            for p in d.rglob("*"):
                if p.suffix in (".py", ".yaml", ".yml") and p.is_file():
                    try:
                        out[str(p)] = p.stat().st_mtime
                    except FileNotFoundError:
                        continue
        return out

    def watcher():
        baseline = snapshot()
        while True:
            _time.sleep(2)
            current = snapshot()
            # New file, missing file, or mtime change → restart.
            if set(current) != set(baseline) or any(
                current.get(k) != baseline.get(k) for k in current
            ):
                changed = [
                    k for k in set(current) | set(baseline)
                    if current.get(k) != baseline.get(k)
                ]
                sys.stderr.write(
                    f"[bioinf-mcp] file change detected ({len(changed)} files), "
                    f"exiting for reload\n"
                )
                sys.stderr.flush()
                # Hard exit — the MCP client will reconnect and respawn us.
                os._exit(0)

    t = threading.Thread(target=watcher, daemon=True)
    t.start()


if __name__ == "__main__":
    if os.environ.get("BIOINF_MCP_AUTO_RELOAD") == "1":
        _watch_and_exit_on_change()
    mcp.run()
