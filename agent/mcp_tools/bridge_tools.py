"""bridge_tools — HPC bridge actuator surface (Phase 2+).

Sibling to observability_tools.py (Phase 1's read-only `snapshot_project`).
While observability is pure-read, this module's primitives push bytes,
submit jobs, monitor them, and fetch results back — all under the same
permission gate (`compute_access.check_permission`) and the same
ControlMaster ssh pattern.

Today the surface is:
  upload / download              — unified transfer (zone auto-routed
                                   by where the absolute remote path
                                   falls); replaces the six retired
                                   zone-specific primitives
  stage_apptainer_image          — get a frozen env's .sif onto a env
  submit_workflow_job            — production submit-and-document
  run_step_on_cluster            — validation/seal run in scratch
  cluster_job_status             — sacct job-state poll
  cluster_module_avail           — Lmod discovery
  globus_task_status             — async-Globus poll

Four transfer auth zones coexist (intentional), routed by where the
absolute remote path falls on the env:
  scratch          — env-implicit + auto-prefix by project (sandbox)
  common_data      — env-implicit + shared namespace (reference data)
  container_upload — env-implicit + content-addressed (.sif / .tar)
  project_path     — Phase-1 explicit grant via directories[] (workspace)

Authorization shape (env-implicit grant, Phase 2):
  - `project_name` + `compute_env_name` resolve to a project's
    compute_env_access entry (the project must have ACCESS to the env)
  - The env declares `agent_scratch_target` / `agent_common_data_target`
    / `container_upload_target` blocks at the env level; their
    `permissions:` lists the supported capabilities — these are the
    GRANT (no per-project re-declaration)
  - Multi-project isolation: in the scratch zone the path must fall
    under `<scratch_root>/<project_name>/...`
  - The agent-supplied absolute remote path is pure-string validated
    BEFORE any I/O

All cheat-guards live under
`tests/integration/honesty/L14_compute_env_safety/`.
"""
from __future__ import annotations

# IMPORT-BINDING (see feedback-mcp-tools-conventions): singletons go through
# `_ms.X` so test monkeypatching on mcp_server attribute names reaches us.
# `mcp` is the FastMCP app and is never monkeypatched, so a bare import is
# safe. Same shape as every other agent/mcp_tools/ submodule.
from pathlib import Path

from agent import mcp_server as _ms
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched


def _resolve_access_path() -> str | None:
    """Return the path to projects_access.yaml as a string, preferring the
    repo-root convention used in this codebase (the user's live file lives
    alongside the source tree), falling back to ~/.bioinf/. Returns None if
    neither exists — the primitive then surfaces a clean FileNotFoundError.

    Same resolution that `agent_status` uses; kept inline here so this
    submodule's MCP wrappers don't grow a cross-submodule import."""
    from agent.skills import compute_access as _ca
    repo_root = _ms._env_mgr.project_root
    candidate = repo_root / "projects_access.yaml"
    if candidate.exists():
        return str(candidate)
    default = _ca.default_access_path()
    return str(default) if default.exists() else None


@mcp.tool()
def upload(project_name: str,
           compute_env_name: str,
           local_path: str,
           remote_abs_path: str,
           async_globus: bool = False) -> dict:
    """Push a local file to an ABSOLUTE path on a compute env. The auth
    family is auto-routed by where the path falls — agent scratch
    sandbox, env's shared common_data zone, or one of the project's
    explicit `directories[]` workspaces.

    For ONE-OFF transfers (debug downloads, ad-hoc data inspection,
    quick uploads) pass `project_name="_ad_hoc"`. The _ad_hoc project
    is synthesized on the fly with access to scratch + common_data on
    every env. No YAML edit needed.

    Authorization routing:
      - path under env.agent_scratch_target.path     → scratch zone
        (env-implicit grant; path must be under
        `<scratch>/<project_name>/...` for multi-project isolation)
      - path under env.agent_common_data_target.path → common_data zone
        (env-implicit grant; shared across projects)
      - anywhere else                                 → project_path zone
        (longest-prefix match in project.directories[]; the matched
        entry MUST include `upload` in its permissions)

    Wire protocol: dispatched by the env's `data_transfer.type` —
    `scp_head_node` (default) or `globus` (when configured). Pass
    `async_globus=True` to return immediately with a task_id instead of
    waiting for SUCCEEDED; poll completion via `globus_task_status`.

    Manifest: every call (success OR failure) writes a JSON record under
    `transfer_history/<project>/<YYYY-MM-DD>/<stamp>_upload_<hash>.json`
    so the operation is replayable programmatically — no memory needed.

    Path safety: `remote_abs_path` must be absolute, ≤4096 chars, no
    '..' traversal, no shell metacharacters, no whitespace.

    `local_path`: must exist, be a regular file (symlinks refused), and
    be under the 5 GiB head-node cap (no cap when data_transfer=globus).

    Returns {success, project, compute_env, zone, direction:"upload",
    local_path, remote_abs_path, provider, task_id?, bytes, duration_s,
    local_sha256, remote_sha256?, verified_method, manifest} on success;
    {"error": "...", "manifest": "..."} on any failure."""
    from agent.skills import transfer
    return transfer.upload(
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_abs_path=remote_abs_path,
        async_globus=async_globus,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def download(project_name: str,
             compute_env_name: str,
             remote_abs_path: str,
             local_path: str,
             async_globus: bool = False) -> dict:
    """Pull a file from an ABSOLUTE path on a compute env to this laptop.
    Symmetric to `upload`: same zone routing (auto-classified by where
    remote_abs_path falls), same auth (the matched zone must declare
    `download` in its permissions; `upload` alone does NOT satisfy
    `download` — discrete capabilities), same manifest.

    For one-off downloads use `project_name="_ad_hoc"`.

    `local_path`: must NOT exist (no silent overwrite); parent dir must
    exist + be writable.

    Returns {success, project, compute_env, zone, direction:"download",
    local_path, remote_abs_path, provider, task_id?, bytes, duration_s,
    local_sha256, remote_sha256?, verified_method, manifest} on success;
    {"error": "...", "manifest": "..."} on any failure."""
    from agent.skills import transfer
    return transfer.download(
        project_name=project_name,
        compute_env_name=compute_env_name,
        remote_abs_path=remote_abs_path,
        local_path=local_path,
        async_globus=async_globus,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def cluster_module_avail(project_name: str,
                        compute_env_name: str,
                        pattern: str = "") -> dict:
    """Discover what HPC modules are loadable on a compute env so the
    agent can pick the right `module load X/Y.Z` line for a launcher.

    Pure-read: runs ONE ssh invocation of `bash -lc 'module avail …'`,
    parses the output, returns a flat list of `<name>/<version>`
    strings. Does not load any module; does not submit any job; never
    writes anything.

    Authorization: project must have a `compute_env_access` entry for
    `compute_env_name`. No per-directory permission needed — we're not
    touching the filesystem.

    `pattern` (optional): forwarded to `module avail <pattern>` AND
    filtered client-side. Useful: `pattern='nextflow'` returns just
    the nextflow versions. Must be a safe token (alnum + `_+.-/`).

    Returns {compute_env, pattern, modules, module_count, captured_at}
    on success; {"error": "...", "hint": ...} on failure (e.g. no
    ControlMaster session)."""
    from agent.skills import cluster_modules
    return cluster_modules.cluster_module_avail(
        project_name=project_name,
        compute_env_name=compute_env_name,
        pattern=pattern or None,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def globus_task_status(project_name: str,
                       compute_env_name: str,
                       task_id: str) -> dict:
    """Query the current state of a Globus transfer task.

    Pairs with `async_globus=True` on the upload/download primitives:
    those return immediately with a `task_id`; this primitive polls
    that task's status. Pure-read — runs ONE `globus task show <id>
    --format json` invocation; no submission.

    Authorization: project must have a `compute_env_access` entry for
    the env, AND the env must declare `data_transfer.type: globus`.
    Task IDs are scoped to the user's Globus tokens — the agent never
    sees a task belonging to anyone else.

    Accepts `project_name="_ad_hoc"` — the same synthesized one-off
    project the upload/download primitives accept — so a task submitted
    via an `_ad_hoc` async upload can be polled with the same project
    name (it has access to every env, so it can poll any env's task).

    `task_id` must be a canonical UUID; refused before any shell-out
    so a smuggled metacharacter can't reach the `globus` CLI.

    Returns {success, task_id, status, nice_status, bytes_transferred,
    files_transferred, files_skipped, fatal_error, type, captured_at}
    on success. `status` is one of ACTIVE, INACTIVE, SUCCEEDED, FAILED.
    {"error": "..."} on failure (cli missing, auth, network)."""
    from agent.skills import compute_access, transfer, transfer_providers
    access = compute_access.load_access(None)
    # _ad_hoc-aware resolve so the poll companion accepts the same
    # project name the async upload/download accepted.
    project = transfer._get_project_or_ad_hoc(project_name, access)  # auth check
    _ = project
    env = compute_access.get_compute_env(compute_env_name, access)
    return transfer_providers.globus_task_status(env, task_id)


@mcp.tool()
def cluster_job_status(project_name: str,
                       compute_env_name: str,
                       job_id: str) -> dict:
    """Look up SLURM state for `job_id` on `compute_env_name` so the
    agent can poll a submitted job to completion.

    Pure-read: runs ONE ssh invocation of
    `bash -lc 'sacct -j <id> -P --noheader -X -o <fields>'`, parses
    the pipe-delimited output, returns a list of row-dicts. Does not
    submit, cancel, or modify anything.

    Authorization: project must have a `compute_env_access` entry for
    `compute_env_name`. No per-directory permission needed. SLURM's
    own ACL keeps the visibility scoped to the user's own jobs.

    `job_id` must be digits with an optional `_<task>` suffix for array
    tasks (e.g. `12345`, `12345_3`). Anything else is refused BEFORE
    any ssh — a smuggled `12345; rm -rf /` never reaches the cluster.

    Returns {compute_env, job_id, jobs: [{job_id, state, elapsed,
    exit_code, nodelist, reason, start, end}, ...], captured_at} on
    success; {"error": "...", "hint": ...} on failure. Empty `jobs`
    means sacct doesn't recognize the id — caller distinguishes "not
    yet in slurmdbd" from "never existed" with a short retry."""
    from agent.skills import cluster_jobs
    return cluster_jobs.cluster_job_status(
        project_name=project_name,
        compute_env_name=compute_env_name,
        job_id=job_id,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def submit_workflow_job(project_name: str,
                        compute_env_name: str,
                        workflow_dir: str,
                        workflow_name: str,
                        tool_name: str,
                        command: str,
                        inputs: dict,
                        outputs: dict,
                        apptainer_sif: str,
                        apptainer_module: str,
                        nextflow_module: str,
                        slurm: dict) -> dict:
    """Production cluster submission — render → upload → sbatch → emit
    a local manifest, return job_id. NO POLLING.

    For runs that land in user-declared project workspace paths. The
    agent may be cut off long before a real production job finishes,
    so this primitive returns immediately after sbatch and writes a
    local manifest the user (or a future agent invocation) can use to
    find the job. For validation/seal runs (short, in the agent's
    scratch sandbox), use `run_step_on_cluster` instead.

    Composition discipline: NOT a composite — the caller still calls
    freeze() to build the env, stage_apptainer_image to push the .sif,
    cluster_job_status to poll AT THEIR OWN PACE, and `download` to
    fetch outputs.

    Authorization (Phase-1 explicit, dir-by-dir):
      - project must have a `compute_env_access` entry for the env
      - the project's `directories[]` under that env must contain
        `workflow_dir` (longest-prefix match) with `permissions:`
        including BOTH `upload` (for the file pushes) AND `exec` (so
        the SLURM job may write outputs in-place during execution)

    Inputs:
      workflow_dir       absolute remote path under a `directories[]`
                         entry; the per-run dir on the compute env.
                         No-overwrite contract means a second submit
                         to the same dir fails — pick a fresh per-run
                         subdir per submission.
      workflow_name      safe-token, ≤64 chars. Used for `sbatch
                         --job-name`, render_workflow's tag, AND the
                         local manifest filename.
      tool_name          safe-token; identifies the tool in
                         process_name + comments.
      command            single-line shell command with `${name}`
                         placeholders bound to inputs/outputs.
      inputs             {placeholder_name: remote_abs_path} — what
                         the running process will read.
      outputs            {placeholder_name: bare_filename} — what the
                         process writes (to the working dir).
      apptainer_sif      absolute remote path to the frozen .sif.
                         Caller stages via stage_apptainer_image
                         first; pass the resolved sif_path here.
      apptainer_module   Lmod token, e.g. "apptainer/1.4.1".
      nextflow_module    Lmod token, e.g. "nextflow/25.04.7".
      slurm              {queue, time, mem, cpus, account?} —
                         closed-key block (typos refused).

    Returns on success:
      {success: True, compute_env, job_id, workflow_dir,
       files_uploaded: [...], submitted_at, upload_started,
       manifest_path:
         "job_submissions/<project>/<workflow_name>_<job_id>.submission.json"}
    Returns {"error": "...", ...} on any refusal/failure. If sbatch
    fails after files have been uploaded, files_uploaded is
    included so the caller can clean up."""
    from agent.skills import submit_workflow
    return submit_workflow.submit_workflow_job(
        project_name=project_name,
        compute_env_name=compute_env_name,
        workflow_dir=workflow_dir,
        workflow_name=workflow_name,
        tool_name=tool_name,
        command=command,
        inputs=inputs,
        outputs=outputs,
        apptainer_sif=apptainer_sif,
        apptainer_module=apptainer_module,
        nextflow_module=nextflow_module,
        slurm=slurm,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def stage_apptainer_image(project_name: str,
                          compute_env_name: str,
                          freeze_request_key: str,
                          sif_subpath: str = "") -> dict:
    """Get the apptainer .sif for a frozen env onto a compute env.

    Mode-aware (auto-determined from the EnvCache record):
      ADOPT (pure-conda + public BioContainer):
        ONE ssh hop: `apptainer pull <sif> docker://<image_by_digest>`.
        No bytes move through us. Idempotent — re-stages are a no-op
        when the .sif already exists.
      BUILD with push_target:
        Same shape as adopt — pull from the configured registry.
      BUILD registry-free (default for non-conda):
        Two steps: (1) upload .tar via `transfer.upload` (wire protocol
        scp_head_node vs globus is picked by the env's
        `data_transfer.type`); (2) ssh `apptainer build <sif>
        docker-archive://<tar>` on the cluster.

    Where container artifacts land:
      .sif: `<env.container_upload_target>/<env_name>_<digest>.sif`
      .tar: `<env.container_upload_target>/apptainer_sources/<basename>.tar`
      Override the .sif subpath via `sif_subpath` (relative, no `..`).

    Authorization: project must have a `compute_env_access` entry for
    the env, AND the env must declare a `container_upload_target` with
    `upload` perm. NO FALLBACK to agent_common_data_target — that zone
    is for reference data; an undeclared container target is a clean
    refusal, not a silent reroute (per
    [[project-container-artifacts-routing]]).

    Returns {success, mode, sif_path, image_digest, request_key,
    skipped, staged_at} on success; {"error": ..., hint?, ...} on
    failure. `skipped: true` means the .sif already existed —
    re-stages are intentionally idempotent."""
    from agent.skills import stage_apptainer
    return stage_apptainer.stage_apptainer_image(
        project_name=project_name,
        compute_env_name=compute_env_name,
        freeze_request_key=freeze_request_key,
        sif_subpath=sif_subpath or "",
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def run_step_on_cluster(pipeline_id: str,
                        freeze_request_key: str,
                        project_name: str,
                        compute_env_name: str,
                        workflow_name: str,
                        tool_name: str,
                        command: str,
                        inputs: dict,
                        outputs: dict,
                        download_local_dir: str,
                        apptainer_module: str,
                        nextflow_module: str,
                        slurm: dict,
                        sif_subpath: str = "",
                        poll_interval: int = 15,
                        max_polls: int = 240,
                        output_types: dict = {}) -> dict:
    """Path-4 keystone — run a workflow step ON CLUSTER IN SCRATCH and
    record cluster-locus evidence as a pipeline_step in the draft.

    The wall: scratch-only
    ----------------------
    workflow_dir is computed internally as
      `<env.agent_scratch_target.path>/<project_name>/<workflow_name>/`
    There is NO knob to point it elsewhere. The scratch sandbox is
    what keeps the agent inside its own walls: it can mess around
    freely in scratch to prove a build works on cluster. Production
    runs (against user project workspaces) go through
    `submit_workflow_job` with directories[] auth.

    Auth: env must declare `agent_scratch_target` with `exec` perm
    (the schema validator enforces this on every scratch target).
    Hard-fails if no scratch target — no graceful fallback.

    Composes: `stage_apptainer_image` (idempotent) → render +
    `upload` × 3 (scratch zone) → `sbatch --parsable` →
    `cluster_job_status` (poll to terminal — viable here because
    validation jobs are bounded) → `cluster_job_resources` (sacct
    MaxRSS for I7) → `download` × N (sha256 round-trip) → type-aware
    validation. The pipeline_step records `validation_locus: "cluster"`
    so a future reader sees the evidence came from sacct.

    After this returns successfully, three legitimate next moves:
      (a) `seal_workflow(pipeline_id, freeze_request_key)` — produce
          a sealed WorkflowSpec with cluster-locus evidence
      (b) Call again with a different command — multi-step workflows
      (c) `discard_pipeline_draft(pipeline_id)` — run-and-go

    `outputs`: `{placeholder_name: bare_filename}` — the file the
    process writes (Nextflow's publishDir lands it in the scratch
    workflow_dir).
    `download_local_dir`: where to materialize the fetched outputs
    locally (created if absent).
    `output_types`: optional `{basename|ext: validator_type}` overrides
    for type-aware validation (same shape as run_step_in_container).

    Returns {success, returncode, job_id, sif_path, workflow_dir,
    resource_usage, detected_outputs, output_sha256, validations,
    validation_count, download_errors, pipeline_merge, final_status}
    on success; {"error": ..., stage_result/last_poll?}
    on any phase's refusal/failure. The prior phases' results are
    preserved on the error dict for diagnosis."""
    from agent.skills import run_cluster_step
    return run_cluster_step.run_step_on_cluster(
        pipeline_id=pipeline_id,
        freeze_request_key=freeze_request_key,
        project_name=project_name,
        compute_env_name=compute_env_name,
        workflow_name=workflow_name,
        tool_name=tool_name,
        command=command,
        inputs=inputs,
        outputs=outputs,
        download_local_dir=download_local_dir,
        apptainer_module=apptainer_module,
        nextflow_module=nextflow_module,
        slurm=slurm,
        sif_subpath=sif_subpath or "",
        poll_interval=poll_interval,
        max_polls=max_polls,
        output_types=output_types or {},
        access_path=_resolve_access_path(),
    )
