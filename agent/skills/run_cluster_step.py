"""
run_step_on_cluster — Layer-2 cluster-locus pipeline step.

The Path-4 keystone. Runs a workflow step on a compute env, records
the cluster-side evidence as a pipeline_step in the local draft, and
validates each fetched output. The result is a pipeline_step that
seal_workflow() can consume to produce a WorkflowSpec — the
HPC analog of run_step_in_container.

What this composes (no new logic, just orchestration)
-----------------------------------------------------
  1. stage_apptainer_image   — get the .sif onto the env (idempotent)
  2. submit_workflow_job     — render + upload + sbatch, get job_id
  3. cluster_job_status      — poll until terminal (PENDING -> ... ->
                               COMPLETED / FAILED / CANCELLED / ...)
  4. cluster_job_resources   — fetch wall_seconds + peak_rss_mb from
                               sacct's batch row (I7 evidence)
  5. download_from_project_path — fetch outputs back local (sha256
                                  round-trip)
  6. _validator.validate     — type-aware validation per output
                               (same code path local steps use)
  7. _pipeline_state.add_step+add_validation — record everything

Composition discipline
----------------------
Not itself a composite primitive in the bad sense — it doesn't expose
a flatter API than the underlying primitives. It exposes the SAME
API a local step would (compatible with the pipeline draft model) so
seal_workflow can treat cluster and local steps the same way.

The end state the caller chooses
--------------------------------
After this returns successfully, three legitimate next moves:
  (a) seal_workflow(pipeline_id, freeze_request_key) — produce a
      sealed WorkflowSpec with validation_locus="cluster"
  (b) Continue adding more steps (call again with a different
      command — multi-step workflows)
  (c) discard_pipeline_draft(pipeline_id) — run-and-go, no seal

The pipeline_step records `validation_locus: cluster` in resource_usage
so a future code reader (or attestation) can see this step's evidence
came from sacct, not host psutil.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Mapping, Optional

from agent.skills import (
    cluster_jobs,
    project_path,
    stage_apptainer,
    submit_workflow,
)


# Terminal SLURM states — once we hit one of these the job is over.
_TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
    "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL",
    "DEADLINE", "REVOKED",
}


def _parse_exit_code(s: str) -> int:
    """SLURM's ExitCode is `<rc>:<signal>`. Take the rc."""
    if not isinstance(s, str) or ":" not in s:
        return -1
    rc, _, _ = s.partition(":")
    try:
        return int(rc)
    except (ValueError, TypeError):
        return -1


def run_step_on_cluster(
        *,
        pipeline_id: str,
        freeze_request_key: str,
        project_name: str,
        compute_env_name: str,
        workflow_dir: str = "",
        workflow_name: str = "",
        tool_name: str,
        command: str,
        inputs: Mapping[str, str],
        outputs: Mapping[str, str],
        download_local_dir: str,
        apptainer_module: str,
        nextflow_module: str,
        slurm: Mapping,
        sif_subpath: str = "",
        access_path: Optional[str] = None,
        poll_interval: int = 15,
        max_polls: int = 240,           # 240 × 15s = 60 min cap
        output_types: Optional[Mapping[str, str]] = None,
        _pipeline_state=None,           # injectable for tests
        _validator=None,                # injectable for tests
        _env_mgr=None,                  # injectable for tests (hash_outputs)
        ) -> dict:
    """Run `command` inside the cluster-staged frozen env, record
    evidence as a pipeline_step.

    Returns on success:
      {success: True, returncode, job_id, sif_path, workflow_dir,
       resource_usage: {wall_seconds, peak_rss_mb, max_cpu_percent,
                        locus="cluster", sacct_job_id, sacct_rows},
       detected_outputs: [<local abs paths>, ...],
       output_sha256: {<basename>: <hex>, ...},
       validations: {<basename>: <validator dict>},
       pipeline_merge: {status, pipeline_id, step_index}}

    Returns {"error": ..., ...} on any refusal/failure. When the
    submit / poll / fetch phases each fail, the prior phases'
    results are kept on the dict (`stage_result`, `submit_result`,
    `final_status`, `downloaded`) so the caller can diagnose
    without re-running.
    """
    if not pipeline_id:
        return {"error": "pipeline_id is required for run_step_on_cluster"}

    # Late-bind the singletons (preserves [[feedback-mcp-tools-conventions]]
    # monkeypatchability — tests inject overrides via the _* kwargs).
    if _pipeline_state is None or _validator is None or _env_mgr is None:
        from agent import mcp_server as _ms
        _pipeline_state = _pipeline_state or _ms._pipeline_state
        _validator = _validator or _ms._validator
        _env_mgr = _env_mgr or _ms._env_mgr

    output_types = output_types or {}

    # ─── 1. Stage the .sif ────────────────────────────────────────────
    stage = stage_apptainer.stage_apptainer_image(
        project_name=project_name,
        compute_env_name=compute_env_name,
        freeze_request_key=freeze_request_key,
        sif_subpath=sif_subpath or "",
        access_path=access_path)
    if "error" in stage:
        return {"error": f"stage_apptainer_image failed: {stage['error']}",
                "stage_result": stage}

    sif_path_remote = stage["sif_path"]
    image_digest = stage.get("image_digest", "")

    # ─── 2. Submit the workflow job ───────────────────────────────────
    sub = submit_workflow.submit_workflow_job(
        project_name=project_name,
        compute_env_name=compute_env_name,
        workflow_dir=workflow_dir,
        workflow_name=workflow_name,
        tool_name=tool_name,
        command=command,
        inputs=inputs,
        outputs=outputs,
        apptainer_sif=sif_path_remote,
        apptainer_module=apptainer_module,
        nextflow_module=nextflow_module,
        slurm=slurm,
        access_path=access_path)
    if "error" in sub:
        return {"error": f"submit_workflow_job failed: {sub['error']}",
                "stage_result": stage, "submit_result": sub}

    job_id = sub["job_id"]
    # If workflow_dir was empty on entry, submit_workflow_job auto-derived
    # it under the env's scratch zone. Use the resolved path from here
    # for the download step + pipeline_step record.
    workflow_dir = sub["workflow_dir"]

    # ─── 3. Poll cluster_job_status until terminal ────────────────────
    final_status = None
    for _i in range(max_polls):
        s = cluster_jobs.cluster_job_status(
            project_name=project_name,
            compute_env_name=compute_env_name,
            job_id=job_id,
            access_path=access_path)
        if "error" in s:
            return {"error":
                f"cluster_job_status failed during poll: {s['error']}",
                "stage_result": stage, "submit_result": sub,
                "last_poll": s}
        if s.get("jobs"):
            row = s["jobs"][0]
            if row.get("state") in _TERMINAL_STATES:
                final_status = row
                break
        time.sleep(poll_interval)

    if final_status is None:
        return {"error":
            f"polling timed out after "
            f"{max_polls * poll_interval}s — job_id={job_id} did "
            f"not reach a terminal state.",
            "stage_result": stage, "submit_result": sub}

    rc = _parse_exit_code(final_status.get("exit_code", ""))

    # ─── 4. Fetch I7-shaped resource_usage from sacct ─────────────────
    resources = cluster_jobs.cluster_job_resources(
        project_name=project_name,
        compute_env_name=compute_env_name,
        job_id=job_id,
        access_path=access_path)
    # resources may have {error} — tolerate; the pipeline_step still
    # records what we have, but I7 will fail seal if rc=0 and no
    # resource_usage. Surface a clear flag for the caller.
    if "error" in resources:
        resource_usage = {
            "wall_seconds":    0.0,
            "peak_rss_mb":     0.0,
            "max_cpu_percent": 0.0,
            "locus":           "cluster",
            "sacct_job_id":    job_id,
            "sacct_error":     resources["error"],
        }
    else:
        resource_usage = {
            "wall_seconds":    resources["wall_seconds"],
            "peak_rss_mb":     resources["peak_rss_mb"],
            "max_cpu_percent": resources["max_cpu_percent"],
            "locus":           "cluster",
            "sacct_job_id":    job_id,
            "sacct_rows":      resources.get("sacct_rows", []),
        }

    # ─── 5. Download outputs back local ──────────────────────────────
    download_dir = Path(download_local_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    download_errors: list[dict] = []
    for placeholder, filename in outputs.items():
        local = str(download_dir / filename)
        dl = project_path.download_from_project_path(
            project_name=project_name,
            compute_env_name=compute_env_name,
            abs_path=f"{workflow_dir}/{filename}",
            local_path=local,
            access_path=access_path)
        if "error" in dl:
            # Tolerate the local-already-exists case as a re-download skip;
            # caller may have downloaded outputs in a prior partial run.
            if "already exists" in (dl.get("error") or ""):
                downloaded.append(local)
            else:
                download_errors.append({
                    "placeholder": placeholder, "filename": filename,
                    "error": dl["error"]})
        else:
            downloaded.append(local)

    # ─── 6. Compute output sha256s (L11 lineage) ──────────────────────
    output_sha256 = _env_mgr.hash_outputs(downloaded) or {}

    # ─── 7. Build the pipeline_step record + add to draft ─────────────
    step_data = {
        "tool":                   tool_name or (command.split() or [""])[0],
        "purpose":                f"cluster run of {tool_name or 'tool'}",
        "command":                command,
        "returncode":             rc,
        "resource_usage":         resource_usage,
        "inputs":                 [{"path": p, "references": []}
                                   for p in inputs.values()],
        "detected_outputs":       downloaded,
        "output_sha256":          output_sha256 or None,
        "ran_in_container":       True,
        "container_image":        sif_path_remote,
        "container_image_digest": image_digest or None,
        # Layer-2 HPC-locus metadata (new fields, downstream-tolerant)
        "validation_locus":       "cluster",
        "cluster_job_id":         job_id,
        "cluster_workflow_dir":   workflow_dir,
        "cluster_node":           final_status.get("nodelist"),
        "cluster_state":          final_status.get("state"),
        "cluster_exit_code":      final_status.get("exit_code"),
    }
    step_data = {k: v for k, v in step_data.items() if v is not None}
    step_index = _pipeline_state.add_step(pipeline_id, step_data)

    # ─── 8. Validate outputs (type-aware, same as local steps) ────────
    validations: dict = {}
    if rc == 0 and step_index is not None:
        for path in downloaded:
            basename = Path(path).name
            ext = "".join(Path(path).suffixes).lower()
            etype = (output_types.get(basename)
                     or output_types.get(ext)
                     or output_types.get(ext.lstrip("."))
                     or _infer_etype(basename, ext))
            v = _validator.validate(path, etype)
            validations[basename] = v
            _pipeline_state.add_validation(
                pipeline_id, step_index, basename, v)

    return {
        "success":            (rc == 0 and not download_errors),
        "returncode":         rc,
        "job_id":             job_id,
        "sif_path":           sif_path_remote,
        "workflow_dir":       workflow_dir,
        "resource_usage":     resource_usage,
        "detected_outputs":   downloaded,
        "output_sha256":      output_sha256,
        "validations":        validations,
        "validation_count":   len(validations),
        "download_errors":    download_errors,
        "pipeline_merge":     {
            "status":      "merged",
            "pipeline_id": pipeline_id,
            "step_index":  step_index,
        },
        "final_status":       final_status,
    }


def _infer_etype(basename: str, ext: str) -> str:
    """Coarse fallback when output_types didn't specify. Kept here
    rather than imported from run_tools to avoid the mcp_tools↔skills
    crossing — run_tools._infer_validator_type is the same idea."""
    by_ext = {
        ".bam": "bam", ".sam": "sam", ".cram": "cram",
        ".vcf": "vcf", ".vcf.gz": "vcf",
        ".bed": "bed", ".gff": "gff", ".gtf": "gtf",
        ".fasta": "fasta", ".fa": "fasta", ".fna": "fasta",
        ".fastq": "fastq", ".fq": "fastq", ".fastq.gz": "fastq",
        ".json": "json", ".jsonl": "jsonl",
        ".tsv": "tsv", ".csv": "csv", ".txt": "txt",
        ".html": "html",
    }
    return by_ext.get(ext) or by_ext.get("." + ext.lstrip(".")) or "txt"
