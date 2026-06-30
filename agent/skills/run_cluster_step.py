"""
run_step_on_cluster — Layer-2 cluster-locus pipeline step.

The Path-4 keystone. Runs a workflow step on a compute env IN THE
AGENT'S SCRATCH SANDBOX, records the cluster-side evidence as a
pipeline_step in the local draft, and validates each fetched output.
The result is a pipeline_step that seal_workflow() can consume to
produce a WorkflowSpec — the HPC analog of run_step_in_container.

The wall: scratch-only
----------------------
This primitive runs validation/seal jobs — short, bounded, the
agent's own work. It ALWAYS lands in:
    <env.agent_scratch_target.path>/<project_name>/<workflow_name>/

There is NO knob to point it elsewhere. The scratch sandbox is what
keeps the agent inside its own walls: it can mess around freely
inside scratch to prove a build works on the cluster; production
runs (against the user's project workspace) go through the separate
submit_workflow_job primitive with project.directories[] auth.

Auth via the env-level agent_scratch_target (check_env_target_capability
+ exec permission). If the env has no scratch target, this primitive
hard-fails — there's no graceful fallback because there's nowhere
else the agent is allowed to do this work.

Composition discipline
----------------------
This IS a thin composite, justified by the new state it produces: the
pipeline_step ledger entry that bridges Layer 1 (the frozen env) to
Layer 2 (the workflow seal). Without it, the agent would have to
manually thread the pipeline_id through ~6 calls just to seal a
single cluster-run step.

It composes:
  1. stage_apptainer_image   — get the .sif onto the env (idempotent)
  2. render_workflow_files   — produce main.nf / nextflow.config /
                               launcher.sh into a local tempdir
  3. upload_to_scratch       — push each rendered file into the
                               agent's scratch sandbox (auto-prefixed
                               by project)
  4. sbatch_via_ssh          — kick off the SLURM job, get job_id
  5. cluster_job_status      — poll until terminal (validation jobs
                               are short; polling is viable here in a
                               way it isn't for production)
  6. cluster_job_resources   — fetch wall_seconds + peak_rss_mb from
                               sacct's batch row (I7 evidence)
  7. download_from_scratch   — fetch outputs back local (sha256
                               round-trip)
  8. _validator.validate     — type-aware validation per output
                               (same code path local steps use)
  9. _pipeline_state.add_step+add_validation — record everything

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

import re
import tempfile
import time
from pathlib import Path
from typing import Mapping, Optional

from agent.skills import (
    cluster_jobs,
    compute_access,
    stage_apptainer,
    submit_workflow,
    transfer,
)


# workflow_name becomes a path component under scratch — keep it safe.
# Same shape as the project_name token validator (alnum + _-, ≤64 chars).
_WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


_RENDERED_FILES = ("main.nf", "nextflow.config", "launcher.sh")


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
        workflow_name: str,
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
    """Run `command` inside the cluster-staged frozen env IN SCRATCH,
    record evidence as a pipeline_step.

    The workflow_dir is computed internally as
        <env.agent_scratch_target.path>/<project_name>/<workflow_name>/
    No caller knob — validation/seal runs always land in the agent's
    scratch sandbox. The schema validator enforces that the env's
    scratch target has [upload, download, exec]; this primitive
    hard-fails if no scratch target is declared.

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
    results are kept on the dict (`stage_result`, `final_status`,
    `downloaded`) so the caller can diagnose without re-running.
    """
    if not pipeline_id:
        return {"error": "pipeline_id is required for run_step_on_cluster"}
    if not workflow_name or not _WORKFLOW_NAME_RE.match(workflow_name):
        return {"error":
            f"workflow_name must match {_WORKFLOW_NAME_RE.pattern!r} "
            f"(it becomes a path component under scratch); got "
            f"{workflow_name!r}"}

    # Late-bind the singletons (preserves [[feedback-mcp-tools-conventions]]
    # monkeypatchability — tests inject overrides via the _* kwargs).
    if _pipeline_state is None or _validator is None or _env_mgr is None:
        from agent import mcp_server as _ms
        _pipeline_state = _pipeline_state or _ms._pipeline_state
        _validator = _validator or _ms._validator
        _env_mgr = _env_mgr or _ms._env_mgr

    output_types = output_types or {}

    # ─── 0. Resolve project + env; compute the scratch workflow_dir ────
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)
    except (compute_access.ConfigError, FileNotFoundError, KeyError,
            ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"}

    env_type = env.get("type")
    if env_type != "ssh":
        return {"error":
            f"run_step_on_cluster only supports ssh compute envs; "
            f"got type={env_type!r} on env {compute_env_name!r}"}

    scratch_target = compute_access.get_agent_scratch_target(env)
    if scratch_target is None:
        return {"error":
            f"compute_env {compute_env_name!r} has no "
            f"agent_scratch_target declared — run_step_on_cluster "
            f"can only run inside the agent's scratch sandbox. Add an "
            f"agent_scratch_target block on this env in "
            f"projects_access.yaml."}

    # Auth: project must have access to env; scratch target must advertise
    # `exec` (required for the SLURM job to write its outputs in-place).
    try:
        compute_access.check_env_target_capability(
            project, compute_env_name, scratch_target,
            "run_step_on_cluster", "agent_scratch_target")
    except compute_access.PermissionDenied as e:
        return {"error": f"PermissionDenied: {e}"}

    scratch_root = scratch_target.get("path", "").rstrip("/")
    workflow_dir = f"{scratch_root}/{project_name}/{workflow_name}"

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

    # ─── 2. Render + upload to scratch + sbatch ───────────────────────
    try:
        rendered = submit_workflow.render_workflow_files(
            tool_name=tool_name,
            command=command,
            inputs=inputs,
            outputs=outputs,
            apptainer_sif=sif_path_remote,
            apptainer_module=apptainer_module,
            nextflow_module=nextflow_module,
            slurm=slurm,
            workflow_name=workflow_name,
        )
    except ValueError as e:
        return {"error": f"workflow_render failed: {e}",
                "stage_result": stage}

    files_uploaded: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bioinf_cluster_step_") as td:
        tdp = Path(td)
        for fname in _RENDERED_FILES:
            (tdp / fname).write_text(rendered[fname])

        for fname in _RENDERED_FILES:
            local = str(tdp / fname)
            # workflow_dir already includes project_name; pass the full
            # absolute path. transfer.upload routes via scratch zone
            # because the path is under env.agent_scratch_target.
            up = transfer.upload(
                project_name=project_name,
                compute_env_name=compute_env_name,
                local_path=local,
                remote_abs_path=f"{workflow_dir}/{fname}",
                access_path=str(Path(access_path)) if access_path else None,
                timeout=300)
            if "error" in up:
                return {"error":
                    f"upload of {fname} to scratch failed: {up['error']}",
                    "stage_result": stage,
                    "files_uploaded": files_uploaded}
            files_uploaded.append(up["remote_abs_path"])

    sb = submit_workflow.sbatch_via_ssh(env, workflow_dir, timeout=300)
    if "error" in sb:
        return {"error": f"sbatch failed: {sb['error']}",
                "stage_result": stage,
                "files_uploaded": files_uploaded,
                "sbatch_result": sb}

    job_id = sb["job_id"]

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
                "stage_result": stage, "job_id": job_id,
                "workflow_dir": workflow_dir, "last_poll": s}
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
            "stage_result": stage, "job_id": job_id,
            "workflow_dir": workflow_dir}

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

    # ─── 5. Download outputs back local (from scratch) ────────────────
    download_dir = Path(download_local_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    download_errors: list[dict] = []
    for placeholder, filename in outputs.items():
        local = str(download_dir / filename)
        # workflow_dir is the scratch sandbox for this job; outputs
        # live there with their declared filenames.
        dl = transfer.download(
            project_name=project_name,
            compute_env_name=compute_env_name,
            remote_abs_path=f"{workflow_dir}/{filename}",
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
