"""
agent_status — the "where am I in this workflow?" snapshot.

A pure-read survey of every subsystem the agent maintains state in. No
mutation, no LLM-tier work, no subprocess except the optional git probe.
Stitches together pipeline drafts, frozen envs, sealed workflows, test
data, the compute-env bridge, background jobs, and the repo state into
ONE digestible record.

Designed to answer questions like:
  - "Did I leave any pipeline drafts hanging?"
  - "Which envs have been frozen and shipped?"
  - "Is my ssh tunnel to the cluster still alive?"
  - "Are there background jobs I forgot about?"
  - "Where is my work relative to origin/main?"

Each subsystem query is its own helper so:
  (a) testing can substitute fake state for any one slice
  (b) a single subsystem's failure (e.g. a corrupt manifest.yaml) degrades
      to a `{"error": "..."}` shape in that slice — never crashes the
      whole status call.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.skills.outcomes import broke


# ---------------------------------------------------------------------------
# Per-subsystem queries (private; each is fault-tolerant)
# ---------------------------------------------------------------------------

def _drafts_summary(pipeline_state) -> list[dict]:
    """In-progress pipeline drafts the agent has been building. Reads the
    `PipelineState._drafts` dict directly — every entry is a draft that
    hasn't been sealed (or discarded) yet."""
    try:
        out: list[dict] = []
        # Best-effort access to the internal map; defensive in case the
        # PipelineState API gains a public accessor later.
        drafts = getattr(pipeline_state, "_drafts", {}) or {}
        for pid, draft in drafts.items():
            out.append({
                "pipeline_id":     pid,
                "description":     (draft.get("description") or "")[:120],
                "env_status":      draft.get("env_status"),
                "pipeline_status": draft.get("pipeline_status"),
                "conda_env":       draft.get("conda_env"),
                "install_steps":   len(draft.get("install_steps") or []),
                "pipeline_steps":  len(draft.get("pipeline_steps") or []),
                "packages":        len(draft.get("packages") or []),
                "verifications":   len(draft.get("verifications") or {}),
            })
        return out
    except Exception as e:
        return [{"error": f"drafts query failed: {e}"}]


def _frozen_envs_summary(env_cache, env_reports_dir: Path) -> list[dict]:
    """Frozen envs registered in the EnvCache + their associated deliverables
    on disk (ENV.html report, attestation.json). One row per EnvCache entry."""
    try:
        out: list[dict] = []
        for req_key, rec in (env_cache.all() or {}).items():
            name = rec.get("name") or rec.get("env_name") or req_key.split("|")[0]
            html = env_reports_dir / f"{name}.ENV.html"
            attest = env_reports_dir / f"{name}.attestation.json"
            recipe = env_reports_dir / f"{name}.recipe.yaml"
            out.append({
                "request_key":     req_key,
                "name":            name,
                "image":           rec.get("image"),
                "image_digest":    rec.get("image_digest"),
                "content_digest":  rec.get("content_digest"),
                "platform":        rec.get("platform"),
                "build_method":    rec.get("build_method"),
                "image_size_bytes": rec.get("image_size_bytes"),
                "env_report":      str(html) if html.exists() else None,
                "attestation":     str(attest) if attest.exists() else None,
                "recipe":          str(recipe) if recipe.exists() else None,
            })
        return out
    except Exception as e:
        return [{"error": f"frozen envs query failed: {e}"}]


def _sealed_workflows_summary(env_reports_dir: Path) -> list[dict]:
    """Sealed Layer-2 workflow specs on disk. One row per
    env_reports/*.workflow.yaml — these are the user-facing deliverables
    from seal_workflow."""
    try:
        if not env_reports_dir.is_dir():
            return []
        out: list[dict] = []
        for spec_path in sorted(env_reports_dir.glob("*.workflow.yaml")):
            try:
                spec = yaml.safe_load(spec_path.read_text()) or {}
            except Exception:
                spec = {}
            name = spec.get("workflow_name") or spec_path.stem.replace(".workflow", "")
            run_report = env_reports_dir / f"{name}.RUN.html"
            # Envs the spec pins (multi-env workflows record several).
            env_blocks = spec.get("envs") or []
            env_digests = [b.get("image_digest") for b in env_blocks if b.get("image_digest")]
            out.append({
                "name":          name,
                "spec":          str(spec_path),
                "run_report":    str(run_report) if run_report.exists() else None,
                "description":   (spec.get("description") or "")[:120],
                "validated_in_shipped_image": spec.get("validated_in_shipped_image"),
                "pinned_env_digests": env_digests,
                "pipeline_steps": len(spec.get("pipeline_steps") or []),
            })
        return out
    except Exception as e:
        return [{"error": f"sealed workflows query failed: {e}"}]


def _core_test_data_summary(data_dir: Path) -> dict:
    """Counts (not contents) of every bootstrapped core_test_data tree —
    just enough to answer 'do I have data to work with?'. Counts per
    sequencing category and per genome build."""
    try:
        out: dict[str, Any] = {"genome_builds": [], "by_build": {}}
        for core_dir in sorted(data_dir.glob("core_test_data_*")):
            manifest_path = core_dir / "manifest.yaml"
            if not manifest_path.exists():
                continue
            try:
                m = yaml.safe_load(manifest_path.read_text()) or {}
            except Exception:
                continue
            build = m.get("genome_build") or core_dir.name.replace("core_test_data_", "")
            out["genome_builds"].append(build)

            # Walk the manifest and count samples per (read_type, file_format).
            counts = {"short_read_fastq": 0, "long_read_fastq": 0,
                      "long_read_pod5": 0, "phenopackets": 0}
            for read_type, level2 in (m.get("sequencing_data") or {}).items():
                if not isinstance(level2, dict):
                    continue
                for _lvl2, level3 in level2.items():
                    if not isinstance(level3, dict):
                        continue
                    for _lvl3, samples in level3.items():
                        if not isinstance(samples, list):
                            continue
                        for smp in samples:
                            ff = smp.get("file_format", "fastq")
                            key = f"{read_type}_{ff}"
                            if key in counts:
                                counts[key] += 1
                            elif read_type == "short_read":
                                counts["short_read_fastq"] += 1
                            elif read_type == "long_read":
                                counts["long_read_fastq"] += 1
            counts["phenopackets"] = len(m.get("phenopackets") or [])
            out["by_build"][build] = {
                "core_dir": str(core_dir),
                "manifest": str(manifest_path),
                **counts,
            }
        return out
    except Exception as e:
        return broke("status.core_test_data_query_failed", error=f"core_test_data query failed: {e}")


def _envs_on_disk_summary(envs_root: Path) -> list[dict]:
    """The host-side conda envs (Layer-0 install state). Distinct from
    frozen envs: these are what's in `envs/` on disk, with no claim of
    being validated-in-ship-image. Useful for 'I installed X locally — is
    it still here?' questions."""
    try:
        if not envs_root.is_dir():
            return []
        out: list[dict] = []
        for env_dir in sorted(envs_root.iterdir()):
            if not env_dir.is_dir():
                continue
            bin_dir = env_dir / "bin"
            # Tools — anything in bin/ that's a regular file, not a python dist.
            # Don't enumerate them all; just count + flag a few notable names.
            tool_count = 0
            if bin_dir.is_dir():
                try:
                    tool_count = sum(1 for p in bin_dir.iterdir()
                                     if p.is_file() and not p.name.endswith((".py", ".pyc")))
                except OSError:
                    pass
            out.append({
                "name":        env_dir.name,
                "path":        str(env_dir),
                "bin_count":   tool_count,
            })
        return out
    except Exception as e:
        return [{"error": f"envs query failed: {e}"}]


def _compute_env_bridge_summary(access_path: Optional[Path]) -> dict:
    """Compute-env bridge state: declared envs/projects from
    projects_access.yaml + active ControlMaster sockets on disk. Together
    these answer 'is my cluster connection alive?'."""
    try:
        out: dict[str, Any] = {}
        # 1. The declared config (if it exists).
        if access_path and access_path.exists():
            try:
                cfg = yaml.safe_load(access_path.read_text()) or {}
            except Exception as e:
                return broke("status.access_yaml_parse_failed", error=f"failed to parse {access_path}: {e}")
            out["config_path"] = str(access_path)
            out["envs"] = [e.get("name") for e in (cfg.get("compute_envs") or [])
                           if isinstance(e, dict) and e.get("name")]
            out["projects"] = [p.get("name") for p in (cfg.get("projects") or [])
                               if isinstance(p, dict) and p.get("name")]
        else:
            out["config_path"] = None
            out["envs"] = []
            out["projects"] = []

        # 2. Active ControlMaster sockets. These are Unix-socket files at
        # ~/.ssh/cm-* — their presence means an ssh master connection is
        # alive (or recently was; ssh cleans them on close).
        ssh_dir = Path.home() / ".ssh"
        sockets: list[str] = []
        if ssh_dir.is_dir():
            for p in ssh_dir.glob("cm-*"):
                try:
                    if p.is_socket():
                        sockets.append(str(p))
                except OSError:
                    continue
        out["active_ssh_sessions"] = sockets
        return out
    except Exception as e:
        return broke("status.compute_env_bridge_query_failed", error=f"compute_env bridge query failed: {e}")


def _background_jobs_summary(job_manager) -> list[dict]:
    """Running + recently-terminated background jobs. Wraps
    JobManager.list_jobs(); failure is non-fatal."""
    try:
        return job_manager.list_jobs(include_terminated=False) or []
    except Exception as e:
        return [{"error": f"jobs query failed: {e}"}]


def _repo_summary(project_root: Path) -> dict:
    """Lightweight git state: current branch, uncommitted-files count,
    ahead/behind origin. All cheap, all `git` shell-outs. Best-effort —
    a non-git working tree returns `{}`."""
    if not (project_root / ".git").exists():
        return {}
    try:
        def _git(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=project_root,
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()

        branch = _git("branch", "--show-current") or None
        status_short = _git("status", "--short")
        uncommitted = sum(1 for ln in status_short.splitlines() if ln.strip())
        ahead_behind_raw = _git("rev-list", "--left-right", "--count",
                                 "HEAD...@{upstream}") if branch else ""
        ahead = behind = None
        if ahead_behind_raw:
            parts = ahead_behind_raw.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
        head_sha = _git("rev-parse", "--short", "HEAD") or None
        return {
            "branch":         branch,
            "head":           head_sha,
            "uncommitted":    uncommitted,
            "ahead_origin":   ahead,
            "behind_origin":  behind,
        }
    except Exception as e:
        return broke("status.git_probe_failed", error=f"git probe failed: {e}")


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def agent_status(
    *,
    pipeline_state,
    env_cache,
    job_manager,
    config: dict,
    access_path: Optional[Path] = None,
    include_repo: bool = True,
) -> dict:
    """Return a snapshot of every subsystem the agent maintains state in.

    Pure-read. No mutation, no LLM-tier work. Each subsystem query is
    fault-tolerant: a corrupt manifest or missing directory degrades to a
    `{"error": "..."}` entry in that slice rather than crashing the whole
    call.

    Args:
      pipeline_state:  PipelineState singleton (in-process drafts)
      env_cache:       EnvCache instance (frozen-env registry on disk)
      job_manager:     JobManager instance (background jobs on disk)
      config:          agent config dict — needs config['paths']['data_dir']
      access_path:     Optional path to projects_access.yaml (compute-env
                       bridge config). None disables that subsystem query.
      include_repo:    If True, runs cheap git probes for branch/ahead/behind.

    Returns a dict with keys:
      drafts, envs_on_disk, frozen_envs, sealed_workflows,
      core_test_data, compute_env_bridge, background_jobs, repo (optional)
    """
    project_root = Path(__file__).parent.parent.parent.resolve()
    data_dir = project_root / config["paths"]["data_dir"]
    envs_root = project_root / "envs"
    env_reports_dir = project_root / "env_reports"

    out: dict[str, Any] = {
        "drafts":            _drafts_summary(pipeline_state),
        "envs_on_disk":      _envs_on_disk_summary(envs_root),
        "frozen_envs":       _frozen_envs_summary(env_cache, env_reports_dir),
        "sealed_workflows":  _sealed_workflows_summary(env_reports_dir),
        "core_test_data":    _core_test_data_summary(data_dir),
        "compute_env_bridge": _compute_env_bridge_summary(access_path),
        "background_jobs":   _background_jobs_summary(job_manager),
    }
    if include_repo:
        out["repo"] = _repo_summary(project_root)
    return out
