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
from agent.skills.docker_builder import DockerBuilder
from agent.skills import biocontainers as _biocontainers
from agent.skills import env_freeze as _env_freeze
from agent.skills import env_honesty as _env_honesty
from agent.skills import freeze as _freeze
from agent.skills import resolver as _resolver
from agent.skills import intent as _intent
from agent.skills import plan as _plan
from agent.skills import user_guide as _user_guide
from agent.skills import env_report_html as _env_report_html
from agent.skills import attestation as _attestation
from agent.skills import locus as _locus
from agent.skills import synthesis as _synth
from agent.skills import provenance as _prov
from agent.skills import env_recipe as _env_recipe
from agent.skills import container_build as _container_build
from agent.skills.container_build import BASE_IMAGE as _BASE_IMAGE
from agent.skills.core_test_data import add_core_test_data as _add_core_test_data
from agent.skills.core_test_data import add_core_pod5_data as _add_core_pod5_data
from agent.skills.core_test_data import add_phenopacket as _add_phenopacket
from agent.skills.core_test_data import phenopacket_to_vcf as _phenopacket_to_vcf
from agent.validators.output_validator import OutputValidator
from agent.skills.resources import list_resources as _list_resources
from agent.skills.resources import list_pipelines as _list_pipelines
from agent.skills.pipeline_state import PipelineState
from agent.skills.job_manager import JobManager

_pkg_search     = PackageSearch(config)
_env_mgr        = EnvManager(config)
_docker         = DockerBuilder(config)
_validator      = OutputValidator(config)
_pipeline_state = PipelineState(config)
_job_manager    = JobManager(config)
_env_cache      = _freeze.EnvCache(_env_mgr.project_root / "env_reports" / "_env_cache.json")

# Reap stale PID files from prior agent sessions whose owning process has
# already exited. Living services owned by other processes are left alone.
#
# N5 fix (batch-3): the reaper used to run at MODULE-IMPORT time, which meant
# the W1 job_runner subprocess (which imports the tool it was asked to run)
# also ran it on startup — and `start_service` writes the PID of the
# nohup-backgrounded `bash` wrapper (via `echo $!`), NOT the daemon PID that
# the wrapper spawned. By the time the freeze subprocess imports this module,
# the wrapper bash has often exited (mongod --fork returned, bash unwound),
# so `os.kill(wrapper_pid, 0)` raises ProcessLookupError and the reaper
# deletes the PID file out from under the still-running daemon. Then the
# parent's stop_service() finds no PID file and orphans the real daemon.
#
# Fix: only reap in the actual MCP-server process (the __main__ entrypoint).
# Any other importer (a detached job_runner, tests, ad-hoc tooling) keeps its
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

# Tool definitions live in agent/mcp_tools/ — see the index at the end of
# this file. The helpers below stay here because (a) they're called from
# multiple submodules (`_shrink_stdio_for_response`, `_merge_simple_install`,
# `_summarize_sbom_in_response`), (b) freeze() needs them (
# `_check_disk_failsafe`, `_prune_buildkit_after_failure`, `_effective_push_target`,
# `_synth_accelerator_from_request`, `_resolve_versions_from_install_record`), and
# (c) tests reach in to test them directly via `from agent.mcp_server import ...`.


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


def _check_docker_available() -> Optional[dict]:
    """Returns a freeze()/run refusal dict if the Docker daemon can't be reached,
    else None. The single upfront guard for every container-native entrypoint —
    freeze(), freeze_from_image(), build_env_from_authors_recipe(),
    run_step_in_container().

    Each of those reaches docker through a DIFFERENT low-level wrapper with a
    different exception discipline, so a missing/stopped daemon surfaced
    inconsistently: a raw FileNotFoundError from one path (container_build._sh
    catches only TimeoutExpired; three direct subprocess.run sites have no
    try/except at all), a structured-but-MISLABELED broke from another
    ('image_pull_failed', 'authors_recipe.build_failed') — sometimes only after a
    git clone had already run. This converts all of that into ONE honest,
    correctly-named refusal BEFORE any clone / cache lookup / build spin-up, the
    same fast-refuse posture as _check_disk_failsafe.

    The probe hits the SERVER, not just the client: `docker version` (with a
    Server template) fails when the daemon is down, whereas `docker --version`
    (client only) succeeds even then. BIOINF_SKIP_DOCKER_PREFLIGHT=1 bypasses (a
    dev/ops escape hatch, mirroring BIOINF_FREEZE_MIN_DISK_GB=0); the test suite
    monkeypatches this function directly, matching how it stubs _check_disk_failsafe."""
    if os.environ.get("BIOINF_SKIP_DOCKER_PREFLIGHT") == "1":
        return None
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return {
            "success": False, "outcome": "refused",
            "stage": "docker_preflight", "code": "docker.not_installed",
            "message": (
                "refusing to start — the `docker` CLI was not found on PATH. This "
                "operation builds/adopts and validates a container image and needs a "
                "working Docker daemon. Install Docker (or Docker Desktop) and make "
                "sure `docker` is on PATH, then retry."),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False, "outcome": "refused",
            "stage": "docker_preflight", "code": "docker.daemon_unavailable",
            "message": (
                "refusing to start — `docker version` timed out probing the daemon "
                "(20s). The Docker daemon appears unresponsive; start or restart it "
                "and retry."),
        }
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        detail = detail[0][:300] if detail else "(no diagnostic)"
        return {
            "success": False, "outcome": "refused",
            "stage": "docker_preflight", "code": "docker.daemon_unavailable",
            "message": (
                "refusing to start — the `docker` CLI is installed but the daemon "
                "could not be reached (is Docker running?). This operation builds/"
                "adopts and validates a container image and needs a live daemon.\n"
                f"  docker said: {detail}"),
        }
    return None


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


# ---------------------------------------------------------------------------
# Tool surface — moved to agent/mcp_tools/
# ---------------------------------------------------------------------------
# data_tools           — add_core_*, phenopacket_*, select_test_data,
#                        list_available_resources,
#                        download_reference_database, install_pipeline_brief
# env_tools            — search_package, resolve_tool, create_conda_env,
#                        install_conda_packages, install_git_repo, synth_fetch,
#                        synth_build,
#                        install_release_binary, install_perl_package,
#                        install_cargo_tool, install_go_tool, install_jar_tool,
#                        install_r_package, install_pip_package,
#                        run_install_command
# freeze_tools         — freeze, verify_env_recipe, generate_user_guide
# jobs_tools           — run_in_background, check_job, cancel_job, list_jobs
# observability_tools  — agent_status, snapshot_project
# run_tools            — run_pipeline_step, run_step_in_container,
#                        verify_installation, run_in_env, validate_output
# service_tools        — check_gpu, start_service, stop_service,
#                        check_service_health, verify_service_dependency
# workflow_tools       — start_pipeline, discard_pipeline_draft,
#                        show_pipeline_draft, patch_pipeline,
#                        stage_authored_artifact, mark_step_validated,
#                        seal_workflow,
#                        list_installed_pipelines, fetch_r_package_deps


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


# ---------------------------------------------------------------------------
# Tool registration — submodule imports fire @mcp.tool() decorators as a
# side effect. ALL names a submodule may need (mcp, config, singletons,
# helpers, StrList/OptStrList) are defined ABOVE this line; Python's
# partial-module handling gives the submodules the bound names through the
# circular `from agent.mcp_server import …` they use. Adding a new tool
# submodule = one line in agent/mcp_tools/__init__.py + its file + a
# back-compat re-export below.
#
# RELOAD CORRECTNESS: importlib.reload(mcp_server) creates a fresh `mcp`
# above; without cascading the reload into mcp_tools, the cached submodules'
# decorators stay attached to the OLD mcp and the new mcp ships with zero
# tools. test_invariants.py::test_orphan_service_pid_reaper_not_at_import
# (N5) is the call site that surfaced this. Loop below: if the package is
# already loaded, reload each submodule so its @mcp.tool() decorators
# re-fire on the new `mcp`.
# ---------------------------------------------------------------------------
if "agent.mcp_tools" in sys.modules:  # noqa: E402 — reload path only
    import importlib as _importlib
    for _sub in ("bridge_tools", "data_tools", "env_tools", "freeze_tools",
                 "intent_tools", "plan_tools", "sealed_tools", "jobs_tools",
                 "observability_tools", "run_tools", "service_tools", "workflow_tools"):
        _full = f"agent.mcp_tools.{_sub}"
        if _full in sys.modules:
            _importlib.reload(sys.modules[_full])
from agent import mcp_tools  # noqa: E402,F401  (must be after all module-level state)

# Back-compat re-exports — `job_runner` (the detached subprocess), tests, and any
# external caller continues to `from agent.mcp_server import <tool>`. This
# list grows alongside the mcp_tools package; populated per phase as tools
# move out.
from agent.mcp_tools.bridge_tools import (  # noqa: E402,F401
    upload,
    download,
    cluster_module_avail,
    cluster_job_status,
    globus_task_status,
    submit_workflow_job,
    run_production_pipeline,
    stage_apptainer_image,
    run_step_on_cluster,
)
from agent.mcp_tools.data_tools import (  # noqa: E402,F401
    download_reference_database,
    acquire_reference_via_recipe,
    list_available_resources,
    add_core_test_data,
    add_core_pod5_data,
    add_phenopacket,
    phenopacket_to_vcf,
    select_test_data,
    install_pipeline_brief,
)
from agent.mcp_tools.env_tools import (  # noqa: E402,F401
    search_package,
    resolve_tool,
    create_conda_env,
    install_conda_packages,
    install_git_repo,
    synth_fetch,
    synth_build,
    install_release_binary,
    install_perl_package,
    install_cargo_tool,
    install_go_tool,
    install_jar_tool,
    install_r_package,
    install_pip_package,
    run_install_command,
)
from agent.mcp_tools.freeze_tools import (  # noqa: E402,F401
    freeze,
    freeze_from_image,
    build_env_from_authors_recipe,
    verify_env_recipe,
    generate_user_guide,
)
from agent.mcp_tools.intent_tools import (  # noqa: E402,F401
    interpret_request,
)
from agent.mcp_tools.plan_tools import (  # noqa: E402,F401
    plan_request,
)
from agent.mcp_tools.sealed_tools import (  # noqa: E402,F401
    describe_sealed_step,
)
from agent.mcp_tools.jobs_tools import (  # noqa: E402,F401
    run_in_background,
    check_job,
    cancel_job,
    list_jobs,
)
from agent.mcp_tools.observability_tools import (  # noqa: E402,F401
    agent_status,
    snapshot_project,
)
from agent.mcp_tools.run_tools import (  # noqa: E402,F401
    run_pipeline_step,
    run_step_in_container,
    verify_installation,
    run_in_env,
    validate_output,
    # Back-compat: tests/test_invariants.py probes these private helpers
    # via `m._stamp_i7_authority` / `m._infer_validator_type`. They were
    # extracted to run_tools alongside the only tools that use them; we
    # re-export so the existing tests don't change.
    _stamp_i7_authority,
    _infer_validator_type,
)
from agent.mcp_tools.service_tools import (  # noqa: E402,F401
    check_gpu,
    start_service,
    stop_service,
    check_service_health,
    verify_service_dependency,
)
from agent.mcp_tools.workflow_tools import (  # noqa: E402,F401
    seal_workflow,
    list_installed_pipelines,
    fetch_r_package_deps,
    start_pipeline,
    discard_pipeline_draft,
    show_pipeline_draft,
    patch_pipeline,
    stage_authored_artifact,
    mark_step_validated,
)


# Entry point lives in agent/__main__.py (`python -m agent`). Do NOT run this
# file directly as `python -m agent.mcp_server` — that loads it twice (once as
# __main__, once as agent.mcp_server via the submodule back-import) and the
# `mcp` that gets `.run()`'d is the empty first instance. See agent/__main__.py
# for the full explanation. The service-PID reaper + auto-reload watchdog are
# triggered from there, on the canonical module's `mcp`.
