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
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Annotated, Any, Optional

import yaml
from fastmcp import FastMCP
from pydantic import BeforeValidator


# ---------------------------------------------------------------------------
# MCP wire coercion — some MCP clients wire-encode array arguments as JSON
# strings (e.g. pip_flags arrives as '["--no-binary", ":all:"]' instead of
# ["--no-binary", ":all:"]). FastMCP's Pydantic validator refuses
# string-when-list-expected, dropping the call. The Batch-2 stress campaign
# hit this on `pip_flags` and `licenses`. We coerce at the parameter boundary
# so the primitive's contract stays list[str] regardless of transport quirks.
# (Symmetric with how Pydantic ships BeforeValidator for boundary coercion.)
# ---------------------------------------------------------------------------

def _coerce_str_list(v):
    """list[str] coercer: list → list, JSON-string-of-list → list, None → None
    (used with Optional). A bare non-empty string becomes [s] (single-item)."""
    if v is None or isinstance(v, list):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s[0] == "[" and s[-1] == "]":
            try:
                import json as _json
                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        return [s]
    return v


# Annotated alias: `StrList` and `OptStrList` behave like list[str] / Optional[list[str]]
# but quietly accept a JSON-encoded list when the wire transport hands us one.
StrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]
OptStrList = Annotated[Optional[list[str]], BeforeValidator(_coerce_str_list)]

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
from agent.skills import env_honesty as _env_honesty
from agent.skills import freeze as _freeze
from agent.skills import resolver as _resolver
from agent.skills import user_guide as _user_guide
from agent.skills import env_report_html as _env_report_html
from agent.skills import attestation as _attestation
from agent.skills import locus as _locus
from agent.skills import synthesis as _synth
from agent.skills import provenance as _prov
from agent.skills import env_recipe as _env_recipe
from agent.skills.container_build import BASE_IMAGE as _BASE_IMAGE
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
#
# N5 fix (batch-3): the reaper used to run at MODULE-IMPORT time, which meant
# the W1 freeze_runner subprocess (which `from agent.mcp_server import freeze`)
# also ran it on startup — and `start_service` writes the PID of the
# nohup-backgrounded `bash` wrapper (via `echo $!`), NOT the daemon PID that
# the wrapper spawned. By the time the freeze subprocess imports this module,
# the wrapper bash has often exited (mongod --fork returned, bash unwound),
# so `os.kill(wrapper_pid, 0)` raises ProcessLookupError and the reaper
# deletes the PID file out from under the still-running daemon. Then the
# parent's stop_service() finds no PID file and orphans the real daemon.
#
# Fix: only reap in the actual MCP-server process (the __main__ entrypoint).
# Any other importer (W1 freeze_runner, tests, ad-hoc tooling) keeps its
# hands off the parent's service registry. The reaper still runs once at
# server startup — just not in every subprocess that imports this module.
def _reap_orphan_service_pids() -> None:
    r = EnvManager.cleanup_orphan_service_pids()
    if r.get("removed"):
        print(f"[bioinf] reaped {len(r['removed'])} orphan service PID file(s): "
              f"{r['removed']}", file=sys.stderr)

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
    return _shrink_stdio_for_response(result, label=f"conda.create.{env_name}")


# ---------------------------------------------------------------------------
# Response-shape helpers — truncation/summarization ONLY at the LLM-facing
# response surface. The truth surface (install_step record, EnvCache record,
# env_reports/{name}.ENV.html, attestation, recipe) is NEVER touched by
# these. The contract is: disk is the source of truth; the response is just
# what fits comfortably in the agent's context. On failure or when more detail
# is needed, the response carries a `log_path` (install) or
# `sbom_full_in_report` (freeze) and the agent Reads from disk.
# ---------------------------------------------------------------------------

_STDIO_SHRINK_OVER = 5000   # combined stdout+stderr chars; below this no shrink
_STDIO_HEAD_KEEP  = 1500    # leading chars in the shrunk response
_STDIO_TAIL_KEEP  = 2500    # trailing chars (errors typically live at the tail)


def _shrink_stdio_for_response(result: dict, *, label: str) -> dict:
    """Cap stdout/stderr in the LIVE response to head+tail, preserving full
    bytes on disk under env_reports/install_logs/{label}.{ts}.log.

    Truth surface unchanged — install_step records `command` + `returncode` +
    `installed_packages` (NEVER `stdout`/`stderr` from this dict, verified by
    audit), so the pipeline draft, recipe, attestation, and env reports are
    byte-identical whether the response was shrunk or not. This affects only
    what the MCP caller (the agent) reads inline; on failure `log_path` is
    in the response and the agent Reads it for the full diagnostic.

    Truncation is symmetric (head + tail) so both install setup AND the final
    error survive when a long source-compile log fails — R packages with C
    sources can dump 60K+ chars of `clang -c file.c -o file.o` invocations
    and the actual error message is in the LAST few hundred chars."""
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if len(stdout) + len(stderr) <= _STDIO_SHRINK_OVER:
        return result

    import time
    log_dir = _env_mgr.project_root / "env_reports" / "install_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in label)[:60]
    log_path = log_dir / f"{safe}.{int(time.time() * 1000)}.log"
    full = (f"=== COMMAND ===\n{result.get('command','')}\n"
            f"=== RETURNCODE === {result.get('returncode')}\n"
            f"=== STDOUT ({len(stdout)} chars) ===\n{stdout}\n"
            f"=== STDERR ({len(stderr)} chars) ===\n{stderr}\n")
    log_path.write_text(full)

    def _trunc(s: str) -> str:
        if len(s) <= _STDIO_HEAD_KEEP + _STDIO_TAIL_KEEP + 100:
            return s
        omitted = len(s) - _STDIO_HEAD_KEEP - _STDIO_TAIL_KEEP
        return (s[:_STDIO_HEAD_KEEP]
                + f"\n\n... [TRUNCATED — {omitted} chars omitted; full log: {log_path}] ...\n\n"
                + s[-_STDIO_TAIL_KEEP:])

    result["stdout"] = _trunc(stdout)
    result["stderr"] = _trunc(stderr)
    result["log_path"] = str(log_path)
    result["log_truncated"] = True
    result["original_log_chars"] = len(stdout) + len(stderr)
    return result


def _summarize_sbom_in_response(out: dict) -> dict:
    """Replace resolved_packages + system_packages in the freeze response with
    counts + a primary-tools-resolved subset.

    Truth surface unchanged — full SBOM is preserved in the EnvCache record
    (stored on disk in env_reports/_env_cache.json BEFORE this is called) and
    in env_reports/{name}.ENV.html + .attestation.json on disk. env_report_html
    and attestation continue to render from the record (which contains full
    lists), untouched. This affects ONLY the live MCP response shape — ~10-15k
    tokens of SBOM rows eliminated per freeze response. If the agent wants the
    full SBOM it Reads `sbom_full_in_report` (the HTML — Layer-1 canonical)."""
    resolved = out.get("resolved_packages") or []
    system = out.get("system_packages") or []
    requested = set(out.get("requested_tools") or [])
    by_name = {p.get("name"): p for p in resolved if isinstance(p, dict)}
    primary = {n: by_name[n].get("version", "?") for n in requested if n in by_name}
    out["resolved_packages_summary"] = {
        "count": len(resolved),
        "primary_tools_resolved": primary,
    }
    out["system_packages_summary"] = {"count": len(system)}
    out["sbom_full_in_report"] = out.get("env_report_html") or "(env report path not yet set)"
    out.pop("resolved_packages", None)
    out.pop("system_packages", None)
    return out


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
    return _shrink_stdio_for_response(result, label=f"conda.install.{env_name}")


@mcp.tool()
def install_git_repo(
    env_name: str,
    repo_url: str,
    tool_name: str,
    ref: str = "",
    build_command: str = "",
    verify_command: str = "",
    bin_path: str = "",
    entrypoint: str = "",
    interpreter: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Vendor a git repository as a source-installed tool (the clone-and-run
    pattern that conda/pip/jar primitives don't cover — e.g. an academic repo
    you run as `python run_thing.py …`).

    Two shapes: a COMPILED tool (pass `build_command` + `bin_path`, the relative
    path to the built executable) or a RUN-BY-PATH script collection (pass
    `entrypoint`, the repo-relative entry script, and `interpreter` e.g. `python`
    — the half-baked academic norm: no compiled binary). Either writes a PATH
    wrapper at {env}/bin/{tool_name}; freeze REPLAYS a compiled tool via the
    source generator and a run-by-path repo via the script_repo generator.

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

    `entrypoint` + `interpreter` is the SIBLING shape for run-by-path academic
    repos with no compiled binary (e.g. `HIC_ASSEMBLER/run_hicAssembler.py` run
    as `python …`). The wrapper at {env}/bin/{tool_name} execs the entry script
    via the named interpreter (`python`, `bash`, …); freeze's script_repo
    generator clones the same SHA in the ship image and writes the identical
    wrapper. Pass entrypoint AND interpreter together — omit both for compiled
    tools that use bin_path + build_command instead. The two shapes are
    mutually exclusive; install fails if both or neither are supplied.

    Pin `ref` to a tag or commit for reproducibility — a bare default branch
    drifts. Returns: {success, clone_path, commit_sha, repo_url, ref,
    build_command, bin_path, entrypoint, interpreter, wrapper_path,
    verify_command, verify_output, log}.
    """
    result = _env_mgr.install_git_repo(
        env_name       = env_name,
        repo_url       = repo_url,
        tool_name      = tool_name,
        ref            = ref,
        build_command  = build_command,
        verify_command = verify_command,
        bin_path       = bin_path,
        entrypoint     = entrypoint,
        interpreter    = interpreter,
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
            # Run-by-path (script collection) replay fields — freeze routes these
            # to the script_repo generator instead of a compiled-source build.
            "entrypoint":    entrypoint,
            "interpreter":   interpreter,
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
    return _shrink_stdio_for_response(result, label=f"git.{env_name}.{tool_name}")


@mcp.tool()
def synth_fetch(repo_url: str, ref: str = "", mode: str = "auto") -> dict:
    """Synthesis tier, call 1 of 2 — PROGRAMMATIC ground-truth fetch for a long-tail
    tool that has no conda/pip/cran/cargo/go/perl/binary/jar home. Acquires the
    source two ways (auto-detected, or force via `mode`='git'|'archive'):
      - GIT repo (any host — GitHub/GitLab/Bitbucket/self-hosted): clone, check out
        `ref` (tag/branch/commit; default HEAD), resolve the CONCRETE commit
        (`git rev-parse` — you never supply it).
      - non-git ARCHIVE (a raw release/vendor tarball or zip over http/ftp — the 'no
        GitHub' case): download, sha256-anchor, extract path-traversal-safely.
    Either way it returns the tool's OWN build files so you read them and decide how
    it installs — the universal residual path: no per-tool generator, you read the
    repo's real build instructions; provenance + the honesty contract make it safe.

    Returns {success, source_kind, files:[{path,sha256,text}], ranked_sources, ...}
    where the immutable anchor is `commit` (git) or `archive_sha256` (archive).
    `ranked_sources` orders the build files by how authoritative a recipe each is
    (Dockerfile › install.sh › CI workflow › Makefile/CMakeLists › setup.py/
    pyproject › README). READ them, then call synth_build with:
      - commands tagged source='extracted' (lifted VERBATIM from a file — pass its
        path as origin_file; PREFER this), or source='agent_authored' (composed
        from the repo's prose — every URL/remote you use MUST appear in the repo).
    synth_build re-fetches at the SAME anchor and re-verifies every command against
    these exact bytes, so an extraction you claim must really be in the file and an
    authored command must be grounded. Nothing you state from memory can ship."""
    fetch = _env_mgr.fetch_build_source(
        repo_url, ref, mode=mode, is_relevant=_synth.is_build_relevant)
    if not fetch.get("success"):
        return fetch
    paths = [f["path"] for f in fetch["files"]]
    fetch["ranked_sources"] = _synth.rank_build_sources(paths)
    fetch["corpus_chars"] = sum(len(f.get("text", "")) for f in fetch["files"])
    return fetch


@mcp.tool()
def synth_build(
    env_name: str,
    repo_url: str,
    tool_name: str,
    commit: str = "",
    commands: list = [],
    evidence: str = "",
    ref: str = "",
    mode: str = "auto",
    archive_sha256: str = "",
    engine_coupled: bool = False,
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Synthesis tier, call 2 of 2 — record a VALIDATED, provenance-tagged install
    recipe for a long-tail tool. The UNIVERSAL residual installer: it replaces a
    per-tool generator with the agent reading the tool's own build files, gated so
    nothing is taken on faith. Works for a git repo OR a non-git archive (the anchor
    is `commit` for git, `archive_sha256` for an archive — pass whichever synth_fetch
    returned; `mode` mirrors synth_fetch).

    `commands` = the ordered install sequence, each:
      {command, source, origin_file?, engine_coupled?}
    where source is 'extracted' (the command occurs VERBATIM in origin_file — the
    file you name from synth_fetch; preferred) or 'agent_authored' (you composed it
    from the repo's prose — its URLs/remotes must appear in the repo).

    This RE-FETCHES at the SAME anchor (deterministic ground truth) and refuses if
    the anchor no longer matches (the source moved/changed), then validate_submission
    re-verifies EVERY command against the runtime's own bytes: an 'extracted' command
    must really occur in origin_file (sha256 stamped by the runtime, not you); an
    'agent_authored' command must be grounded. The source URL — and for an archive
    its sha256 — are ground truth by construction (we fetched exactly them), so a
    `git clone {url}` / `curl {url}` + `sha256sum -c` ground; every OTHER external
    reference must trace to the repo's files. Any violation → refused, no record. On
    success it records ONE install_method (type='synthesized') into the draft; freeze()
    replays it verbatim and the honesty contract proves the tool RUNS (`evidence`).

    NOTE — runtime SERVICES (MongoDB/Postgres/Redis/Spark) are NOT installed here:
    synthesis bakes the TOOL only (install-time); a service is provisioned per-run via
    start_service (Layer 2, I10). Keep synthesized commands install-time, not a
    long-running daemon.

    `evidence`: the in-image check that proves the tool works (must reference the
    tool as a real token — echo/true cheats are rejected by the contract). Default
    `command -v {tool_name}`. Returns {success, anchor, records, install_method,
    violations?, pipeline_merge?}."""
    fetch = _env_mgr.fetch_build_source(
        repo_url, commit or ref, mode=mode, is_relevant=_synth.is_build_relevant)
    if not fetch.get("success"):
        return {"success": False, "stage": "refetch", **fetch}
    kind = fetch.get("source_kind", "git")
    # Anchor verification — the re-fetch must resolve the SAME immutable bytes.
    if kind == "git":
        if commit and fetch.get("commit") != commit:
            return {"success": False, "stage": "refetch",
                    "error": f"re-fetch resolved {fetch.get('commit')!r}, expected {commit!r} "
                             f"— ref is not pinned to an immutable commit"}
        anchor = {"commit_sha": fetch.get("commit"), "ref": ref or commit or "HEAD"}
        ground_extra = repo_url
        anchor_val = fetch.get("commit")
    else:  # archive
        if archive_sha256 and fetch.get("archive_sha256") != archive_sha256:
            return {"success": False, "stage": "refetch",
                    "error": f"re-download sha256 {fetch.get('archive_sha256')!r} != expected "
                             f"{archive_sha256!r} — the archive at that URL changed; re-run synth_fetch"}
        anchor = {"archive_sha256": fetch.get("archive_sha256")}
        # the archive URL AND its sha256 are ground truth (we downloaded exactly it),
        # so a `curl {url}` + `sha256sum -c {sha}` grounds.
        ground_extra = repo_url + "\n" + (fetch.get("archive_sha256") or "")
        anchor_val = fetch.get("archive_sha256")
    fetch["corpus"] = _synth.build_corpus(fetch["files"]) + "\n" + ground_extra
    val = _synth.validate_submission(fetch, list(commands))
    if not val["ok"]:
        return {"success": False, "stage": "validate_submission",
                "violations": val["violations"],
                "hint": "an 'extracted' command must occur verbatim in its origin_file; "
                        "an 'agent_authored' command's URLs/remotes must appear in the repo "
                        "(the source URL and an archive's sha256 are auto-grounded)"}
    records = val["records"]
    install_method = {
        "type":         "synthesized",
        "source":       repo_url,
        "tool":         tool_name,
        "evidence":     evidence or f"command -v {tool_name}",
        "engine_coupled": engine_coupled,
        "commands":     records,                       # validated, provenance-tagged
        "file_hashes":  {f["path"]: f["sha256"] for f in fetch["files"]},
        **anchor,
    }
    result: dict = {"success": True, "source_kind": kind, "anchor": anchor_val,
                    "records": records, "install_method": install_method}
    if pipeline_id:
        ip_record = {"name": tool_name, "channel": kind, "source": repo_url,
                     "install_method": install_method,
                     "version": (anchor_val or "")[:12]}
        step_data = {
            "tool": "git", "subcommand": "synthesize",
            "purpose": f"Synthesize {tool_name} from {repo_url} (agent-authored, gated)",
            "command": f"synth_build {tool_name} @ {(anchor_val or '')[:12]}",
            "returncode": 0, "installed_packages": [ip_record],
        }
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id})
    return _shrink_stdio_for_response(result, label=f"synth.{env_name}.{tool_name}")


@mcp.tool()
def install_spack_package(
    env_name: str,
    tool_name: str,
    package: str = "",
    spack_ref: str = "v0.22.1",
    evidence: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Declare a Spack package (the HPC from-source registry — thousands of curated
    community recipes) for a tool not on conda/pip/cran. A DECLARE primitive: it
    records the install_method (type='spack') into the draft; the actual build +
    validation happen at freeze() INSIDE the ship image (Spack on the host is
    impractical, and container-native is where 'validated == shipped' holds anyway).

    freeze() bootstraps Spack with its store under /opt/tools (so the dep-closure
    RPATHs resolve in the slim runtime), builds `package` (default = tool_name) from
    source with the build container's gcc, trims build-only deps, and symlinks the
    tool onto PATH — then the honesty contract proves the tool RUNS in the shipped
    image via `evidence`. NOTE: from-source builds are slow; this tier is practical
    on a NATIVE amd64 host. Pass an `evidence` that RUNS the tool (e.g.
    'samtools --version') — `command -v` alone can't catch a mis-relocated binary.
    `spack_ref` pins Spack (default v0.22.1; v1.0 split builtin packages out).

    Returns {success, install_method, pipeline_merge?}."""
    install_method = {
        "type":      "spack",
        "package":   package or tool_name,
        "spack_ref": spack_ref,
        "evidence":  evidence or f"command -v {tool_name}",
        "source":    f"spack:{package or tool_name}@{spack_ref}",
    }
    result: dict = {"success": True, "tool_name": tool_name, "install_method": install_method,
                    "note": "declared — Spack builds + validates at freeze() in the ship image "
                            "(best on a native amd64 host; from-source is slow under emulation)"}
    if pipeline_id:
        ip_record = {"name": tool_name, "channel": "spack",
                     "source": install_method["source"], "install_method": install_method}
        step_data = {
            "tool": "spack", "subcommand": "install",
            "purpose": f"Declare {tool_name} via Spack ({package or tool_name}@{spack_ref})",
            "command": f"spack install {package or tool_name}",
            "returncode": 0, "installed_packages": [ip_record],
        }
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id})
    return _shrink_stdio_for_response(result, label=f"spack.{env_name}.{tool_name}")


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
    return _shrink_stdio_for_response(result, label=f"binary.{env_name}.{tool_name}")


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
    return _shrink_stdio_for_response(result, label=f"perl.{env_name}.{module}")


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
    return _shrink_stdio_for_response(result, label=f"cargo.{env_name}.{crate}")


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
    return _shrink_stdio_for_response(result, label=f"go.{env_name}.{package}")


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
    return _shrink_stdio_for_response(result, label=f"jar.{env_name}.{tool_name}")


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
    a failed prior attempt (retry semantics) — note that when the previous
    attempt at slot N FAILED (returncode != 0) and this one succeeds, the new
    step is APPENDED at the end of install_steps rather than overwriting slot N
    (see pipeline_state._add_to_step_list). This keeps the replay order = the
    actual successful install order even when a missing dep was discovered
    mid-install.

    Returns the run_install_command shape; failure cases include both R
    install errors and load-failures with a clean signal. On failure, the
    return ALSO includes `missing_packages: [name, ...]` parsed from stderr —
    a structured surface for the autonomy loop. R's package-not-available
    errors are wordy ("Error : Package 'snpStats' not available after install
    attempt"; "there is no package called 'snpStats'") — parsing them here so
    the caller doesn't have to. The caller installs each missing package then
    retries this install, no string-handling round trip.
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
    # On failure, surface every package R complained was missing as a structured
    # field. R logs these in TWO distinct shapes — both load-bearing in the wild:
    #   • `Error : Package 'X' not available after install attempt` — BiocManager
    #     trying to install a dep that doesn't exist on the resolved mirror
    #     (often: foreign Bioc mirror unreachable; or a CRAN/Bioc-only dep
    #     opportunistically pulled by remotes during install_github)
    #   • `there is no package called 'X'` — load failure of a transitive dep
    #     (e.g. R CMD INSTALL byte-compiling lazy bindings that touch X)
    # Both shapes mean the same thing for the caller: install X then retry me.
    if result.get("returncode") != 0:
        import re as _re
        stderr_blob = (result.get("stderr") or "") + "\n" + (result.get("stdout") or "")
        missing: list[str] = []
        seen: set[str] = set()
        for pat in (r"Package '([A-Za-z][A-Za-z0-9._]*)' not available",
                    r"there is no package called '([A-Za-z][A-Za-z0-9._]*)'"):
            for m in _re.finditer(pat, stderr_blob):
                pkg = m.group(1)
                if pkg != check_name and pkg not in seen:
                    # don't include the package WE were trying to install — its
                    # own load-or-die failure already telegraphs that; missing_packages
                    # is specifically the OTHER packages this install depends on.
                    seen.add(pkg)
                    missing.append(pkg)
        if missing:
            result["missing_packages"] = missing
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
    return _shrink_stdio_for_response(result, label=f"r.{source}.{env_name}.{name}")


@mcp.tool()
def install_pip_package(
    env_name: str,
    name: str,
    version: str = "",
    pip_flags: OptStrList = None,
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a pip package end-to-end with an auto-verify_command.

    Equivalent to running pip install + python -c "import name" inside the env.
    The import-check is the load-or-die: if pip says it installed but the
    package isn't importable, the step is recorded as failed.

    `pip_flags` (NEW): extra flags passed verbatim to pip install. Use for
    `--no-binary :all:` (force source compile), `--no-build-isolation`,
    `--index-url`, etc. PERSISTED on `install_method.pip_flags` so freeze's
    replay path emits the SAME flags inside the shipped image — otherwise
    pip's default wheel substitution would silently downgrade the validated
    source compile (the pysam-stress P2 trust violation). Pass as a list of
    tokens (`['--no-binary', ':all:']`); we shlex-quote each. Flag-bearing
    pip installs land in the freeze build as engine-coupled long-tail steps,
    not via `pixi add --pypi` (uv doesn't honor pip flags).

    Notes:
      - pip's canonical name is preserved (e.g. open-cravat stays hyphenated)
      - resolved_version is filled from `pip list --format=json` at finalize
        when no explicit version is passed
    """
    pip_flags = list(pip_flags or [])
    spec = f"{name}=={version}" if version else name
    # Module name for the import check: pip's canonical hyphenated names use
    # underscores at import time (open-cravat → import cravat or open_cravat).
    # We default to the lowercased name; agents can override with verify_command
    # post-hoc if the import path differs.
    import_check_name = name.replace("-", "_").lower()
    _flag_str = " ".join(shlex.quote(f) for f in pip_flags)
    command = " ".join(part for part in (f"pip install", _flag_str, spec) if part)
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
        # `pip_flags` persists on install_method so freeze's replay re-emits the
        # SAME flags inside the shipped image. Without this, env_freeze would
        # reconstruct the pip spec from {name, version} alone and silently drop
        # the flags — the P2 trust violation (validated source compile →
        # shipped manylinux wheel).
        im: dict = {"type": "pip", "source": command}
        if pip_flags:
            im["pip_flags"] = list(pip_flags)
        ip_record = {
            "name":    name,
            "channel": "pip",
            "source":  command,
            "install_method": im,
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
    return _shrink_stdio_for_response(result, label=f"pip.{env_name}.{name}")


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

    output_types: optional dict mapping {basename | extension | absolute path}
                  to expected_type (e.g. `{".vcf.gz": "vcf", ".bam": "bam",
                  "/tmp/report.html": "html"}`). Lookup order on each detected
                  output: absolute resolved path → as-detected path → basename
                  → ".ext" → "ext" → extension inference. Pre-N8 only the
                  basename/extension keys worked — passing a full path
                  silently fell through to inference (which yields
                  expected_type='any', an I3 violation at seal time). Unmatched
                  output_types keys are reported back in the response as
                  `output_types_unmatched` so a typo doesn't go silent.

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
    # N8 (batch-3): track which output_types keys were consumed so we can
    # surface unmatched keys back to the caller — a typo or a path that
    # didn't actually get produced should not silently fall through to
    # _infer_validator_type (which returns "any" → I3 violation at seal).
    output_types_used: set[str] = set()
    validations: dict = {}
    if result.get("returncode") == 0 and idx is not None:
        for path in result.get("detected_outputs", []):
            basename = Path(path).name
            ext = "".join(Path(path).suffixes).lower()
            # Resolve expected type, in order of specificity:
            #   1. absolute resolved path  (N8: full-path keys now work)
            #   2. as-detected path        (path-as-it-came, in case agent
            #                               keyed off a non-canonical form)
            #   3. basename
            #   4. ".ext"  (with leading dot)
            #   5. "ext"   (without leading dot)
            #   6. extension inference     (fallback — yields "any" if no
            #                               recognized extension)
            try:
                resolved_path = str(Path(path).resolve())
            except Exception:
                resolved_path = path
            etype = None
            for key in (resolved_path, path, basename, ext, ext.lstrip(".")):
                if key and key in output_types:
                    etype = output_types[key]
                    output_types_used.add(key)
                    break
            if etype is None:
                etype = _infer_validator_type(basename, ext)
            v = _validator.validate(path, etype, env_name=env_name)
            validations[basename] = v
            _pipeline_state.add_validation(pipeline_id, idx, basename, v)

    unmatched = sorted(set(output_types) - output_types_used)
    out = {
        **result,
        "pipeline_merge":  {"status": "merged", "pipeline_id": pipeline_id, "step_index": idx},
        "validations":     validations,
        "validation_count": len(validations),
    }
    if unmatched:
        # Surface but don't fail the call — the validation outcome is still
        # produced; the caller just needs to know their output_types key
        # didn't bind to anything (typo, wrong extension, file not produced).
        out["output_types_unmatched"] = unmatched
    return out


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


def _stamp_i7_authority(resource_usage, platform: str):
    """Mark whether captured I7 resource numbers (wall, peak RSS, peak CPU) are
    authoritative. They are real only when the step ran on a NATIVE locus; under
    emulation (qemu/Rosetta) they are emulator artefacts. This does NOT change the
    step's pass/fail or the I7 invariant (the measurement still happened) — it stops
    the spec/guide from presenting emulated timings as ship-architecture truth."""
    if isinstance(resource_usage, dict):
        loc = _locus.detect_locus(platform)
        resource_usage["i7_authoritative"] = loc["i7_authoritative"]
        resource_usage["locus"] = loc["locus"]
    return resource_usage


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
    if _locus.daemon_is_remote():
        # This step bind-mounts LOCAL test data; a remote daemon (DOCKER_HOST) can't
        # see local paths, so outputs would never land back here for validation.
        # (Layer-1 freeze() build+validation IS daemon-agnostic and runs natively on
        # a remote amd64 host — only these Layer-2 DATA steps need the daemon local.)
        return {"error": "run_step_in_container bind-mounts local test data, but the active "
                "Docker daemon is REMOTE (DOCKER_HOST). It cannot see local paths. Use a local "
                "daemon for Layer-2 data steps, or stage the data on the remote host and pass "
                "data_dir as its path there. Note: Layer-1 freeze() validation runs natively on "
                "a remote amd64 host with no change."}
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
    # I7 numbers measured under emulation are emulator artefacts, not real — stamp
    # whether they're authoritative (native locus) so the spec/guide never presents
    # emulated timings as if they were measured on the ship architecture.
    res["resource_usage"] = _stamp_i7_authority(res.get("resource_usage"), platform)
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

def _resolve_versions_from_install_record(
    parsed: list[tuple[str, Optional[str]]],
    draft: Optional[dict],
) -> list[tuple[str, Optional[str]]]:
    """B1 fix: fill a version slot from `install_steps[*].installed_packages`
    when the caller passed a bare tool name. The install record is the
    authoritative answer to "what version did we actually install and validate"
    — the biocontainer adopt-decision must consult it, else it picks whatever
    tag ranks highest (the BUSCO-stress 3.0.2 vs 6.0.0 wrong-version trust
    violation, where ranking-by-build-number elevated an older major version).

    An EXPLICIT caller pin (busco=5.4) is honored verbatim — the install record
    only fills the None slot. Same trust-anchor pattern: when the user/install
    record speaks, registry-side guessing yields. Pure / no network."""
    if not isinstance(draft, dict):
        return parsed
    installed = {p.get("name"): p.get("version")
                 for p in _freeze.installed_packages(draft)
                 if isinstance(p, dict) and p.get("name")}
    return [(n, v if v is not None else installed.get(n)) for n, v in parsed]


def _synth_accelerator_from_request(accel: str, cuda_version: str,
                                    draft_accel: Optional[dict]) -> Optional[dict]:
    """Bind the MCP `freeze(accel=…, cuda_version=…)` scalars to an Accelerator
    policy dict so env_honesty.{check_build, check_adopt} can actually evaluate
    I12. A DRAFT-supplied accelerator (via patch_pipeline) wins — it's the richer,
    explicit record. Otherwise we materialize a minimal policy from the scalars:

      accel='none' / unset → None  (no policy, no I12 check)
      accel='mps'          → {type: mps, dev_only: True}  (the only honest claim
                             for Metal/MPS — it does not survive containerization)
      accel='cuda'|'rocm'  → {type: ..., runtime: 'build_only',
                              toolkit_version: cuda_version if given}

    A missing toolkit_version is INTENTIONALLY left blank — I12 fires
    `accel_toolkit_version_required` and freeze refuses, surfacing the caller's
    half-formed claim instead of silently substituting a default.

    Pure / unit-testable (no draft-state needed when draft_accel is supplied)."""
    if isinstance(draft_accel, dict):
        return draft_accel
    a = (accel or "none").strip().lower()
    if a in ("", "none"):
        return None
    if a == "mps":
        return {"type": "mps", "dev_only": True}
    out: dict[str, Any] = {"type": a, "runtime": "build_only"}
    if (cuda_version or "").strip():
        out["toolkit_version"] = cuda_version.strip()
    return out


# ---------------------------------------------------------------------------
# Disk failsafe — Apollo3 stress (2026-05-27) cascade mitigation.
#
# Apollo3 stress: 4 parallel subagent freezes → docker buildkit's intermediate
# layers piled up to 80 GB → host disk at 92% → builds entered an infinite
# retry loop on 'no space left on device', wedging the orchestrator. Two
# defensive layers below:
#
#   A1 disk pre-check (_check_disk_failsafe): refuse fast at freeze() entry
#       when free disk is below a configurable threshold. Names the cleanup
#       command in the diagnostic.
#
#   A2 post-failure buildkit prune (_prune_buildkit_after_failure): when a
#       container-native build returns success=False, attempt to reclaim the
#       dangling layers it left behind so the NEXT freeze doesn't compound.
#       Best-effort + reported in the result (never fails the call).
#
# The disk threshold IS the concurrency safety: if N parallel freezes race
# for disk, the early ones succeed and the late ones hit the failsafe and
# refuse cleanly — no cascade, no wedge, no lock file needed.
# ---------------------------------------------------------------------------

# Minimum free disk a freeze() needs to safely embark on a container-native build.
# A typical CUDA/bio build's buildkit intermediates hit 5-15 GB; 10 GB is the
# floor we want to keep the OS and other docker layers healthy. Overridable via
# BIOINF_FREEZE_MIN_DISK_GB for test/dev (set to 0 to bypass, e.g. in CI mocks).
_FREEZE_MIN_DISK_GB_DEFAULT = 10


def _check_disk_failsafe(min_gb: Optional[int] = None) -> Optional[dict]:
    """Returns a freeze() refusal dict if free disk is below threshold, else
    None. We probe the partition holding the project root (Docker Desktop on
    macOS stores its data under the same user-home filesystem; on Linux it's
    usually the same root partition). A diagnostic in 'message' names the
    exact cleanup commands so the agent can recover deterministically.

    The env var BIOINF_FREEZE_MIN_DISK_GB overrides; 0 disables (used by
    the test suite — see test_freeze_disk_failsafe_*). When shutil.disk_usage
    fails (an exotic FS or sandbox), we DO NOT refuse: a failsafe that
    blocks legitimate work because IT couldn't read disk is worse than no
    failsafe."""
    import shutil
    if min_gb is None:
        try:
            min_gb = int(os.environ.get("BIOINF_FREEZE_MIN_DISK_GB",
                                        str(_FREEZE_MIN_DISK_GB_DEFAULT)))
        except (TypeError, ValueError):
            min_gb = _FREEZE_MIN_DISK_GB_DEFAULT
    if min_gb <= 0:
        return None
    try:
        usage = shutil.disk_usage(str(PROJECT_ROOT))
    except Exception:
        return None
    free_gb = usage.free / (1024 ** 3)
    if free_gb >= min_gb:
        return None
    return {
        "success": False, "stage": "disk_failsafe",
        "free_gb": round(free_gb, 2), "min_gb": min_gb,
        "message": (
            f"refusing to start freeze() — only {free_gb:.1f} GB free on disk "
            f"(threshold {min_gb} GB). A container-native build's buildkit "
            f"intermediates can consume 10-30 GB per concurrent build; parallel "
            f"freezes can cascade into a disk wedge that no individual build "
            f"can escape. Recover with:\n"
            f"  docker builder prune -af               # reclaim buildkit cache\n"
            f"  docker system prune -af --volumes      # full reclaim (heavier)\n"
            f"Override the threshold with BIOINF_FREEZE_MIN_DISK_GB=<gb> (0 to "
            f"disable; not recommended in shared workspaces)."),
    }


def _prune_buildkit_after_failure() -> dict:
    """Best-effort `docker builder prune -af` after a failed container-native
    build. The build that failed leaves dangling intermediate layers; the next
    freeze's buildkit cache compounds them. Run-and-report — never raise.
    Returns a small dict the caller folds into the build result so the
    operator sees what was cleaned (or why the prune itself failed)."""
    try:
        r = subprocess.run(["docker", "builder", "prune", "-af"],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        return {"attempted": True, "ok": False, "reason": repr(e)}
    out = (r.stdout or "") + (r.stderr or "")
    # `docker builder prune` prints e.g. "Total reclaimed space: 14.2GB" on success
    reclaimed = ""
    for line in out.splitlines():
        if "reclaimed" in line.lower():
            reclaimed = line.strip()
            break
    return {"attempted": True, "ok": r.returncode == 0,
            "reclaimed": reclaimed, "rc": r.returncode}


def _freeze_in_background(**args) -> dict:
    """Spawn freeze() in a detached subprocess via JobManager. Returns
    immediately with {job_id, result_path, log_path, state="running"}.

    W1 mitigation: the synchronous freeze() can run for 10-60 minutes on a
    CUDA-tier build (download a multi-GB tarball, build a multi-stage image,
    pull deps under emulation). The MCP stream-watchdog kills any tool call
    that produces no stdout for ~600s, so the in-call freeze drops the
    transport mid-build — the build succeeds inside the container but freeze
    can never return its final JSON (no EnvCache record, no env report).

    The background mode spawns a Python subprocess that calls freeze() in
    SYNCHRONOUS mode (with `background=False`), writes the result JSON to
    data/jobs/{job_id}.result.json, and exits. The parent agent polls
    check_job(job_id) at its own cadence; when state=='exited', it reads the
    result file for the full freeze record. Standard env_report/attestation/
    recipe artifacts are written by the subprocess too — pure pass-through.

    Args are passed via a JSON file (not argv) to avoid shell-quoting hell
    with lists/dicts. The subprocess's stdout/stderr stream into the
    JobManager log, so `check_job(job_id)`'s log_tail surfaces high-level
    progress markers (start, mode, result-path) and any exception traceback.

    Defaults: license-gated/redistributable carry verbatim from the caller;
    push_target/registry behave the same in-subprocess as in-process; the
    EnvCache write happens in the subprocess so the parent's cache is updated
    on disk (and the next call sees it).
    """
    import json as _json
    import uuid as _uuid
    name = (args.get("pipeline_name") or args.get("env_name") or "freeze").strip()
    # `freeze.{name}.{8-char-hex}` — readable in `list_jobs` AND unique across
    # repeated background freezes of the same env.
    job_id = f"freeze.{name}.{_uuid.uuid4().hex[:8]}"
    # W1 ephemera (args + result JSON) live in data/jobs/ next to the
    # JobManager log/status files for the same job — env_reports/ stays
    # clean as the Layer-1 deliverables dir (HTML / attestation / recipe).
    # Configurable via paths.jobs_dir; back-compat falls back to env_reports.
    jobs_rel = config.get("paths", {}).get("jobs_dir") or "data/jobs"
    jobs_dir = _env_mgr.project_root / jobs_rel
    jobs_dir.mkdir(parents=True, exist_ok=True)
    args_path = jobs_dir / f"{job_id}.args.json"
    result_path = jobs_dir / f"{job_id}.result.json"
    # erase any prior result so a stale file can't fool a polling caller
    if result_path.exists():
        result_path.unlink()
    args_path.write_text(_json.dumps(args, default=str))
    # Drive the synchronous freeze from a small runner script that imports the
    # MCP module and calls freeze() with background=False. The runner captures
    # exceptions into the result JSON so the agent gets a structured failure
    # instead of a crashed subprocess.
    cmd = (
        f"python -m agent.skills.freeze_runner "
        f"{shlex.quote(str(args_path))} {shlex.quote(str(result_path))}"
    )
    job = _job_manager.start(cmd, job_id=job_id)
    if "error" in job:
        return {"success": False, "stage": "background_spawn", **job}
    return {
        "success":     True,
        "background":  True,
        "job_id":      job_id,
        "result_path": str(result_path),
        "log_path":    job.get("log_path", ""),
        "state":       "running",
        "note": ("freeze running in background; poll check_job(job_id) until "
                 "state='exited', then read the JSON at result_path for the full "
                 "freeze record. Shell loops can also `until [ -f {job_id}.done ]; "
                 "do sleep N; done` against data/jobs/ — the .done sentinel is "
                 "written atomically on exit (status.json exists from t=0, so "
                 "file-existence checks on it fire immediately and misfire). "
                 "The standard ENV.html / attestation.json / recipe.yaml are "
                 "also written by the subprocess on success."),
        "done_marker": str((_env_mgr.project_root / jobs_rel /
                            f"{job_id}.done")),
    }


def _effective_push_target(push_target: str, registry: str, name: str,
                           version: str, gated: bool) -> str:
    """The registry ref to push a built image to. An explicit push_target wins;
    else a configured default registry derives `{registry}/{name}:{version}` so a
    push happens with no per-call argument. Returns "" (no push, tarball delivery)
    when nothing is configured OR the artifact is gated (I13 — gated images stay
    tarball-only, never pushed)."""
    if gated:
        return ""
    if (push_target or "").strip():
        return push_target.strip()
    reg = (registry or "").strip().rstrip("/")
    return f"{reg}/{name}:{version or 'latest'}" if reg else ""


@mcp.tool()
def freeze(
    env_name: str,
    tools: StrList,
    pipeline_name: str = "",
    pipeline_description: str = "",
    version: str = "",
    platform: str = "linux-64",
    accel: str = "none",
    gated: bool = False,
    licenses: OptStrList = None,
    push_target: str = "",
    gpu_required: bool = False,
    cuda_version: str = "",
    pipeline_id: str = "",
    background: bool = False,
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
      4. HPC delivery: adopted → `apptainer pull docker://…@digest`. Built →
         registry-free by DEFAULT (`docker save` tarball + `apptainer build
         docker-archive://…`, zero config/auth). If a default registry is
         configured (config docker.registry) OR a push_target is passed, the built
         image is pushed and delivery becomes `apptainer pull docker://…`; a push
         failure falls back to the tarball and is reported in push_status. Gated
         images are NEVER pushed (I13 — tarball-only).
      5. EnvCache.register(request_key → record).

    `tools` are the PRIMARY requested tools (e.g. ['samtools=1.21',
    'bwa=0.7.17']) — what biocontainer/mulled adoption matches on, NOT the full
    dependency closure. Returns {mode, image, image_digest, content_digest,
    request_key, hpc_delivery, cache_hit, …}.

    `background` (W1 mitigation): when True, spawns freeze in a detached
    subprocess via JobManager and returns IMMEDIATELY with a job_id. Use this
    for large builds (CUDA tools, ML/AI models, big release binaries) where the
    synchronous in-call time would exceed the MCP stream-watchdog window (~10 min).
    The subprocess writes the full freeze result JSON to
    `data/jobs/{job_id}.result.json` when it finishes; poll `check_job(job_id)`
    until state=='exited', then read that file. The standard env_report/attestation/
    recipe artifacts are written by the subprocess too, on success.
    """
    if background:
        return _freeze_in_background(
            env_name=env_name, tools=tools, pipeline_name=pipeline_name,
            pipeline_description=pipeline_description, version=version,
            platform=platform, accel=accel, gated=gated,
            licenses=list(licenses or []), push_target=push_target,
            gpu_required=gpu_required, cuda_version=cuda_version,
            pipeline_id=pipeline_id,
        )
    # A1 failsafe (Apollo3 stress, batch-2): disk pre-check. 4 parallel freezes
    # piled buildkit intermediates to 80 GB / 92% capacity → infinite retry loop.
    # Refuse here with a diagnostic that names the cleanup command, BEFORE any
    # cache lookup or docker work. The threshold IS the concurrency safety:
    # parallel freezes that race for disk now refuse cleanly instead of wedging.
    _disk_refusal = _check_disk_failsafe()
    if _disk_refusal:
        return _disk_refusal
    parsed = _freeze.parse_tools(tools)
    # F1 fix (Batch 2): the licenses surface has TWO entry points — the freeze()
    # `licenses=[...]` kwarg AND patch_pipeline({licenses, license_gated, …}) on
    # the draft. An agent that diligently called patch_pipeline but forgot to
    # re-pass licenses on freeze() would land here with licenses=None, the I13
    # gate would refuse the gated build, and the diligence would look like a
    # bug. Merge: caller's licenses wins if non-empty; else fall back to the
    # draft's. Same merge for `gated`: an explicit gated=True from the caller
    # wins; absent caller intent, the draft's license_gated promotes us into
    # gated mode (so I13 still fires, but with the licenses the agent recorded).
    _draft_for_merge = _pipeline_state.get_draft(pipeline_id) if pipeline_id else None
    if isinstance(_draft_for_merge, dict):
        if not (licenses or []) and _draft_for_merge.get("licenses"):
            licenses = list(_draft_for_merge.get("licenses") or [])
        if not gated and bool(_draft_for_merge.get("license_gated")):
            gated = True
    # F2 fix (Batch 2): I13 EARLY GATE. The honesty contract already refuses a
    # gated build with empty licenses[] (`I13.gated_license_recorded` in
    # env_honesty), but only AFTER the docker build finishes — the user pays
    # 10-30 min of build time to learn the artifact will be refused. Refuse
    # here, before request_key/cache lookup/docker work, with the same shape
    # the contract uses so the error is structurally indistinguishable.
    if gated and not (licenses or []):
        return {"success": False, "stage": "i13_early_gate",
                "honesty_violations": [{
                    "invariant": "I13.gated_license_recorded", "where": "licenses",
                    "message": "license_gated=true requires at least one entry in licenses[] "
                               "naming the license/terms the artifact is bound by. Pass "
                               "licenses=[…] on freeze() (or patch_pipeline before freeze)."}],
                "violation_count": 1}
    # Bind the MCP scalar accel/cuda_version to a proper Accelerator policy dict so
    # the honesty contract (I12) can actually check it. A draft-supplied accelerator
    # wins (richer record); when absent and the caller passed accel="cuda" but no
    # patch_pipeline(accelerator=…), we synthesize the minimum and let I12 catch any
    # missing required metadata (toolkit_version, runtime_probe, min_driver_version) —
    # the right failure mode is REFUSE-WITH-DIAGNOSTIC, not a vacuous pass. This is
    # the dorado-stress D3 fix: previously `accel` only fed the cache key and never
    # reached _check_accelerator, so `accel="cuda"` on a draft with accelerator=None
    # produced a "POLICY_CLEAN — I12 passed" badge without I12 ever running.
    # Fetched BEFORE rkey so the cache key can include the policy facets (D5 fix)
    # AND the install-record version fill (B7 fix — see parsed_filled).
    # Reuses _draft_for_merge (read above for F1/F2 licenses merge) — same draft.
    draft = _draft_for_merge
    _draft_accel = draft.get("accelerator") if isinstance(draft, dict) else None
    effective_accel = _synth_accelerator_from_request(accel, cuda_version, _draft_accel)
    # B7 fix (verification-driven, 2026-05-27): fill version slots from the
    # install record BEFORE computing the cache key. Pre-fix, parsed=[(busco,None)]
    # fed request_key (yielding `busco|linux/amd64|none`) but the adopt lookup
    # used the install-record-filled version separately. Two distinct envs
    # (busco==6.0.0 vs busco==5.8.3) collided on ONE cache key, so the second
    # freeze returned the FIRST's image — a wrong-version trust violation
    # isomorphic to the original B1, surfaced at a higher layer. Filling here
    # makes the cache key reflect what we'll actually adopt/build.
    parsed_filled = _resolve_versions_from_install_record(parsed, draft)
    # D5 + D6 fix: request_key now folds in gated, accelerator-policy hash, and
    # the licenses set hash so policy-distinct artifacts don't collide on the cache
    # key. Platform is canonicalized inside request_key so conda-form ('linux-64')
    # and Docker-form ('linux/amd64') callers share ONE slot (D6).
    rkey = _freeze.request_key(
        [(n, v or "") for n, v in parsed_filled], platform, accel,
        gated=gated, accel_policy=effective_accel, licenses=list(licenses or []),
    )

    # Re-anchor the cache against reality: a hit is a CLAIM until we confirm the
    # image is still present in the docker daemon. An evicted image (or one a user
    # `docker rmi`'d) was treated as a hit before — handing back a stale record,
    # no rebuild, no report re-render. lookup_anchored turns that into a MISS so
    # the build path runs, materializes a fresh image, and re-renders every
    # deliverable. (The container-native analog of the I8/I11 anchoring the host
    # path used to do for binaries / source clones.)
    def _docker_image_present(ref: str) -> bool:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
                           capture_output=True, text=True)
        return r.returncode == 0
    # cache lookup: lookup_anchored confirms the image is still in the docker
    # daemon (an evicted image gets re-built rather than returning a dangling
    # record). On hit we summarize the SBOM in the response only; the cached
    # record on disk keeps the full lists for env_report/attestation rendering.
    cached = _env_cache.lookup_anchored(rkey, _docker_image_present)
    if cached:
        return _summarize_sbom_in_response(
            {"success": True, "cache_hit": True, "request_key": rkey, **cached}
        )

    # A request-based FALLBACK anchor only. The authoritative content_digest is the
    # 'what was GOT' digest set per-branch below (the EnvBuild lock+longtail digest
    # for a build, the biocontainer manifest digest for an adopt) via
    # _freeze.record_content_digest. The old content_digest_from_spec(draft) read
    # finalized-only fields (packages[]/lock_sha256) a live draft lacks, collapsing
    # to one constant for every container-native build.
    content_digest = _freeze.compute_content_digest({
        "tools": sorted(f"{n}={v or ''}" for n, v in parsed),
        "platform": platform, "accel": accel,
    })

    name = pipeline_name or env_name
    sif = f"{name}.sif"
    # B1 + B7 fix: adopt lookup consumes `parsed_filled` (versions resolved from
    # install_steps[*].installed_packages above) — the same value that fed the
    # cache key, so the adopt-decision and the cache slot agree on what env
    # we're building. The install record is the trust anchor.
    adopt = _biocontainers.resolve_biocontainer(parsed_filled)
    conda_lock_path = None
    shipped_binaries: list[dict] = []
    build_method = None
    validation_locus = "unknown"   # native | emulated | adopted — where we validated
    locus_advisory = ""
    env_recipe_dict = None         # the self-contained rebuild recipe (build path only)
    build_cd = ""                  # the EnvBuild content_digest (the authoritative build anchor)

    non_conda = _freeze.non_conda_installs(draft) if draft else []
    # P3 fix: pipeline_steps that ran `pip install …` via run_in_env mutate the
    # env outside install_steps' structured surface — non_conda_installs misses
    # them, so the adopt gate would otherwise see "pure conda" and ship a
    # BioContainer that omits the pip install. (pysam-stress: host-built
    # pysam==0.24.0 via run_in_env → freeze adopted pysam==0.23.3 biocontainer.)
    env_mutators = _freeze.env_mutating_pipeline_steps(draft) if draft else []
    has_env_mutations = bool(non_conda or env_mutators)
    can_adopt = bool(adopt.get("found") and adopt.get("image_by_digest") and not gated)

    # Registry push as a first-class delivery: an explicit push_target wins, else a
    # configured default registry (config docker.registry) auto-derives the ref so a
    # push happens with no per-call argument. Tarball→Apptainer stays the genuine
    # zero-config default (no registry configured → no push, no auth needed). Gated
    # artifacts are NEVER pushed (I13).
    default_registry = (config.get("docker", {}).get("registry") or "").strip()
    effective_push = _effective_push_target(push_target, default_registry, name, version, gated)
    push_status = "not-configured"   # surfaced honestly: pushed / push-failed / skipped

    def _deliver_built(image):
        """docker-save tarball + registry push (when a target is configured) →
        Apptainer contract. Push failure falls back to the tarball but is reported,
        never silently swallowed."""
        nonlocal push_status
        idg = _docker.image_digest(image)
        tar_path = _env_mgr.project_root / "docker_images" / name / f"{name}.tar"
        save = _docker.save_archive(image, tar_path)
        tball = save.get("tarball") if save.get("success") else None
        pushed = None
        if effective_push:
            tagged = _docker._run(["docker", "tag", image, effective_push])["returncode"] == 0
            if tagged and _docker._run(["docker", "push", effective_push], timeout=900)["returncode"] == 0:
                pushed, push_status = effective_push, f"pushed: {effective_push}"
            else:
                push_status = f"push-failed: {effective_push} (tarball fallback)"
        elif gated and (push_target or default_registry):
            push_status = "skipped: gated artifacts are never pushed (I13)"
        h = _freeze.apptainer_delivery(mode="build", sif_name=sif, push_target=pushed,
                                       tarball=tball, gated=gated)
        return idg, tball, h

    if can_adopt and not has_env_mutations:
        # ADOPT — pure-conda env with a published biocontainer. The biocontainer
        # IS the artifact (provenance = its digest), so no conda-lock of our env.
        mode, image, image_digest, tarball = "adopt", adopt["image_by_digest"], adopt["digest"], None
        # MODE-AWARE HONESTY (D2 fix): adopt skips VALIDATED_IN_IMAGE (BioContainers'
        # bytes are trusted by their published digest, not validated in-locus) but
        # POLICY_CLEAN STILL MUST PASS — accelerator honesty (I12) and the license
        # firewall (I13) describe what WE declare on this artifact, not who built it.
        # The previous freeze code path adopted+returned without ever calling the
        # contract, so the report rendered "POLICY_CLEAN — I12 passed" without I12
        # ever running. dorado-stress demonstrated this with a CPU-only samtools
        # biocontainer happily emitted under `accel=cuda`. We now refuse here.
        adopt_check_input = {
            "image": image, "image_digest": image_digest,
            "accelerator": effective_accel,
            "license_gated": gated, "licenses": list(licenses or []),
            "redistributable": not gated,
        }
        adopt_violations = _env_honesty.check_adopt(adopt_check_input)
        if adopt_violations:
            return {"success": False, "stage": "adopt_honesty", "request_key": rkey,
                    "adopt_attempt": adopt, "honesty_violations": adopt_violations,
                    "violation_count": len(adopt_violations)}
        hpc = _freeze.apptainer_delivery(mode="adopt", sif_name=sif,
                                         image_by_digest=adopt["image_by_digest"])
        validation_locus = "adopted"   # we trust the published digest, not an in-locus run

    else:
        # CONTAINER-NATIVE BUILD — the SINGLE build path (Phase E: freeze no longer
        # uses conda-pack at all). env_freeze installs + validates IN the ship image
        # (one generic bake, validated==shipped), covering hand-installed tools
        # (binary/jar/source/cargo/go/perl), pure conda, and pip/R — cross-arch too
        # (build in-container, no host-arch conda-pack and no cross-arch refusal).
        # A biocontainer is not adopted when the env has non-conda installs it can't
        # represent (adopting would ship a different, unvalidated artifact).
        if can_adopt:
            if non_conda:
                _skipped = ("env has non-conda installs a biocontainer cannot represent "
                            f"({len(non_conda)} install_step(s))")
            else:
                _skipped = (f"env has {len(env_mutators)} pipeline_step(s) that mutated the env "
                            "(e.g. `pip install …` via run_in_env) — a biocontainer cannot "
                            "represent these")
            adopt = {**adopt, "skipped": _skipped}
        docker_platform = _CONDA_TO_DOCKER_PLATFORM.get(platform, platform)
        conda_deps = _freeze.requested_conda_specs(draft) if draft else []
        if not conda_deps and not non_conda:
            # no draft (or no recorded conda installs): treat the requested tools as
            # conda specs (the declarative pure-conda case).
            conda_deps = [f"{n}={v}" if v else n for n, v in parsed]
        br = _env_freeze.build_env_image(
            draft or {}, name=name, version=version, conda_deps=conda_deps,
            primary_tools=[n for n, _ in parsed], platform=docker_platform,
            accelerator=effective_accel,
            license_gated=gated, licenses=licenses, redistributable=not gated)
        if not br.get("success"):
            # build_env_image refuses pip/r_install with no generator, a non-replayable
            # source, or any honesty-contract violation (incl. I13: a gated build needs
            # licenses[]). Surface it verbatim.
            #
            # A2 failsafe (Apollo3 stress, batch-2): when the failure happened INSIDE
            # docker (stages: start/declare/install/freeze — pre-docker refusals like
            # resolve/route/map_install leave no layers), it may have parked
            # intermediate buildkit layers that compound across freezes. Best-effort
            # prune ONLY when free disk is already approaching the failsafe threshold
            # (1.5x the hard floor) — on a healthy disk we keep buildkit cache for
            # fast iteration on the next retry. The prune outcome rides on the
            # response so the operator can see what was cleaned (or why nothing was).
            _DOCKER_STAGES = {"start", "declare", "declare_locked", "declare_pypi",
                              "install", "freeze"}
            extra = {}
            if br.get("stage") in _DOCKER_STAGES:
                try:
                    import shutil as _sh
                    free_gb = _sh.disk_usage(str(PROJECT_ROOT)).free / (1024 ** 3)
                except Exception:
                    free_gb = None
                soft_threshold = _FREEZE_MIN_DISK_GB_DEFAULT * 1.5
                if free_gb is not None and free_gb < soft_threshold:
                    extra["buildkit_prune"] = _prune_buildkit_after_failure()
                    extra["buildkit_prune"]["reason"] = (
                        f"free disk {free_gb:.1f} GB below soft threshold "
                        f"{soft_threshold:.0f} GB — pruning to avoid cascade")
            return {"success": False, "stage": "container_build", "request_key": rkey,
                    "adopt_attempt": adopt, "build": br, **extra}
        mode, build_method, image = "build", "container-native", br["image"]
        image_digest = br["image_digest"]
        build_cd = br.get("content_digest", "")   # the real, unique, reproducible anchor
        validation_locus = br.get("validation_locus", "unknown")
        locus_advisory = br.get("locus_advisory", "")
        _, tarball, hpc = _deliver_built(image)   # docker-save tarball + Apptainer contract
        # record what shipped: each baked long-tail step (the command IS the provenance).
        shipped_binaries = [{"name": s.get("purpose", ""), "command": s.get("command", "")}
                            for s in br.get("longtail_steps", [])]
        # the SELF-CONTAINED rebuild recipe — everything build_env_image needs to
        # reproduce this env with no draft/agent (the verify-by-rebuild / CI artifact).
        # content_digest is the EnvBuild one (what a rebuild via build_env_image yields).
        env_recipe_dict = _env_recipe.extract_recipe(
            draft, name=name, version=version, conda_deps=conda_deps,
            primary_tools=[n for n, _ in parsed], platform=docker_platform,
            accelerator=effective_accel,
            license_gated=gated, licenses=licenses, redistributable=not gated,
            content_digest=br.get("content_digest", ""),
            # ship the engine lock with the recipe — replay materializes the env
            # from these exact bytes, no solve. Eliminates the conda drift hole.
            conda_lock=br.get("lock_files") or None,
            # snapshot.debian.org timestamp the BUILDER + RUNTIME stages pinned
            # apt to — replay points at the same snapshot, same apt bytes.
            apt_snapshot=br.get("apt_snapshot") or "")
        if conda_deps:  # portable lock for the conda layer (image digest is the real anchor)
            plats = [platform] if platform.startswith("linux") else ["linux-64", platform]
            cl = _env_mgr.generate_lock(env_name, platforms=plats)
            conda_lock_path = cl.get("lockfile") if cl.get("success") else None

    # Authoritative 'what was GOT' anchor: EnvBuild digest for a build, biocontainer
    # manifest digest for an adopt (request hash only as a last-resort fallback).
    content_digest = _freeze.record_content_digest(
        mode, build_digest=build_cd, adopt_digest=adopt.get("digest", ""),
        fallback=content_digest)
    record = _freeze.freeze_record(
        request_key=rkey, content_digest=content_digest, mode=mode,
        image=image, image_digest=image_digest, platform=platform, gated=gated,
        conda_lock_path=conda_lock_path, tarball=tarball, hpc=hpc,
    )
    if build_method:
        record["build_method"] = build_method
    if shipped_binaries:
        record["shipped_binaries"] = shipped_binaries
    record["validation_locus"] = validation_locus
    # report inputs — all runtime-captured (from the BuildResult), so the env
    # report rendered from them can't be faked. Absent on the adopt path.
    record["name"] = name
    record["requested_tools"] = [n for n, _ in parsed]
    # DECLARED POLICY (submitter-asserted, NOT runtime-verified — the contract only
    # checks them for consistency, I12/I13). Recorded so the report can show them in
    # a clearly-separated, honestly-labelled section.
    record["licenses"] = list(licenses or [])
    # The accelerator policy on the record is the SAME object the honesty contract
    # was just evaluated against (effective_accel = draft.accelerator OR synthesized
    # from the MCP scalar args). The record's accelerator MUST agree with the policy
    # that gated the build/adopt, else the artifact and its claim diverge.
    record["accelerator"] = effective_accel
    if mode == "build":
        record["engine"] = br.get("engine", "none")
        record["conda_specs"] = br.get("conda_specs", [])
        record["verifications"] = br.get("verifications", [])
        record["resolved_packages"] = br.get("resolved_packages", [])
        record["system_packages"] = br.get("system_packages", [])
        record["push_status"] = push_status
    _env_cache.register(rkey, record)

    # Layer-1 deliverables, rendered PURELY from the verified record (can't be faked):
    # the human env report (HTML headline + Markdown for diff/parse) + a standard
    # in-toto/SLSA provenance attestation. Views — never block a good freeze on a
    # render error.
    # The .md renderer was retired in batch-3 (the .html is the canonical Layer-1
    # deliverable; .md was a redundant view that only existed during the AUDIT#2
    # phase to ease grep-based diff). Two artifacts now: ENV.html + attestation.json.
    report_html_path = attestation_path = None
    reports_dir = _env_mgr.project_root / "env_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    try:
        (reports_dir / f"{name}.ENV.html").write_text(_env_report_html.render_env_report_html(record))
        report_html_path = str(reports_dir / f"{name}.ENV.html")
    except Exception as e:
        report_html_path = f"(html report render failed: {e!r})"
    try:
        import json as _json
        att = _attestation.build_attestation(record, base_image=_BASE_IMAGE if mode == "build" else "")
        (reports_dir / f"{name}.attestation.json").write_text(_json.dumps(att, indent=2))
        attestation_path = str(reports_dir / f"{name}.attestation.json")
    except Exception as e:
        attestation_path = f"(attestation render failed: {e!r})"
    # the SELF-CONTAINED rebuild recipe (build path only) — verify with verify_env_recipe.
    recipe_path = None
    if env_recipe_dict:
        try:
            import yaml as _yaml
            (reports_dir / f"{name}.recipe.yaml").write_text(
                _yaml.safe_dump(env_recipe_dict, sort_keys=False))
            recipe_path = str(reports_dir / f"{name}.recipe.yaml")
        except Exception as e:
            recipe_path = f"(recipe render failed: {e!r})"

    out = {"success": True, "cache_hit": False, "adopt_attempt": adopt, **record}
    out["env_report_html"] = report_html_path
    out["attestation"] = attestation_path
    out["env_recipe"] = recipe_path
    if locus_advisory:
        out["locus_advisory"] = locus_advisory   # actionable, e.g. "enable Rosetta…"
    # Summarize the bulky SBOM in the live response only. The full SBOM lives
    # in the EnvCache record (registered above) and in env_reports/{name}.ENV.html
    # / .attestation.json — both rendered from the full record before this
    # summarization fires. ~10-15k tokens of SBOM rows eliminated per response
    # with zero loss of accessible information (the agent Reads the HTML when
    # it wants the full SBOM).
    return _summarize_sbom_in_response(out)


@mcp.tool()
def verify_env_recipe(recipe_path: str) -> dict:
    """Rebuild an env FROM ITS RECIPE ALONE (env_reports/{name}.recipe.yaml, written by
    freeze) and check it reproduces the recorded content_digest. The recipe is self-
    contained — it carries the conda specs + every non-conda install_method (synthesized
    commands + provenance + commit, jar/binary/source/...), so this rebuild uses NO
    draft / agent / pipeline-state.

    WHAT A MATCH PROVES (precisely — no overclaiming):
      • COMPLETENESS — the recipe is self-contained (rebuild from it alone succeeds).
      • LOCAL DETERMINISM / CONVERGENCE — replaying it here yields the same content_digest;
        the conda layer is RE-SOLVED (not cheated from a cached lock), so a match means the
        solve converged — the strongest same-machine signal for 'two runs → same result'.
    It does NOT prove cross-machine reproducibility (different base cache / network / docker)
    or independent-party tamper-evidence — those are this SAME rebuild run ELSEWHERE (CI, a
    colleague) + signing. The recipe ENABLES them; this verifies the necessary local
    conditions. Requires Docker. Returns {success, content_digest_match, rebuilt/expected
    content_digest, proves, honesty_violations}."""
    import yaml as _yaml
    try:
        with open(recipe_path) as fh:
            recipe = _yaml.safe_load(fh)
    except Exception as e:
        return {"success": False, "error": f"could not load recipe {recipe_path!r}: {e}"}
    if not isinstance(recipe, dict) or not recipe.get("name"):
        return {"success": False, "error": "not a valid env recipe (missing name)"}
    res = _env_recipe.rebuild_from_recipe(recipe)
    return {
        "success": res["success"],
        "content_digest_match": res["content_digest_match"],
        "rebuilt_content_digest": res["rebuilt_content_digest"],
        "expected_content_digest": res["expected_content_digest"],
        "proves": res["proves"],
        "build_stage": res["build"].get("stage"),
        "honesty_violations": res["build"].get("honesty_violations"),
    }


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

    # MULTI-ENV CHAINING: a workflow may chain steps that each ran in their OWN
    # frozen env (their own freeze). Validate every step's container digest against
    # the set of ALL frozen env digests, not just the one we sealed from — so a
    # multi-env workflow is still "validated == shipped" when every step ran in some
    # shipped env. `fr` remains the PRIMARY env (the guide's get-the-image section).
    all_envs = _env_cache.all()
    valid_digests = {r.get("image_digest") for r in all_envs.values() if r.get("image_digest")}
    by_digest = {r.get("image_digest"): (k, r) for k, r in all_envs.items() if r.get("image_digest")}
    envs_used: list[dict] = []
    seen_dig: set = set()
    for s in draft.get("pipeline_steps", []):
        if not isinstance(s, dict):
            continue
        d = s.get("container_image_digest")
        is_validated = s.get("validation") or s.get("validation_status") == "passed"
        if d and is_validated and d not in seen_dig:
            seen_dig.add(d)
            rk, rr = by_digest.get(d, ("", {}))
            envs_used.append({"request_key": rk, "image": rr.get("image", s.get("container_image", "")),
                              "image_digest": d})

    guide_md = _user_guide.render_user_guide(render_spec, freeze_record=fr,
                                             valid_digests=valid_digests)
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
        # every frozen env this workflow chains (multi-env); the primary env above
        # is for the guide's get-the-image step. Each step also carries its own
        # container_image_digest, so a reader can map step → env.
        "envs":               envs_used,
        "pipeline_status":    draft.get("pipeline_status", "in_progress"),
        "usage_verified":     usage_ok,
        "validated_in_shipped_image": _user_guide.validated_in_shipped_image(
            draft, fr, valid_digests=valid_digests),
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
    _label_suffix = tool or subcommand or "cmd"
    return _shrink_stdio_for_response(result, label=f"install.{env_name}.{_label_suffix}")


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
    # The MCP-server process is the ONLY caller authorized to reap the service
    # registry — see _reap_orphan_service_pids comment above (N5). Subprocesses
    # that import this module (the W1 freeze_runner, tests) never reach here.
    _reap_orphan_service_pids()
    if os.environ.get("BIOINF_MCP_AUTO_RELOAD") == "1":
        _watch_and_exit_on_change()
    mcp.run()
