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
  globus_task_status             — poll a Globus task to its real end state

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
           remote_abs_path: str) -> dict:
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
    `scp_head_node` (default) or `globus` (when configured). The call
    BLOCKS until the bytes are verified. A globus transfer past the
    sync-wait cap returns `globus_sync_wait_exceeded` WITH its task_id —
    the transfer keeps running server-side; resolve it with
    `globus_task_status`, do not assume it failed.

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
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def download(project_name: str,
             compute_env_name: str,
             remote_abs_path: str,
             local_path: str) -> dict:
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

    Every successful globus upload/download records its `globus_task_id`
    in the transfer manifest, so any transfer record is a valid input.
    Pure-read — runs ONE `globus task show <id> --format json`
    invocation; no submission.

    Reach for this when `upload`/`download` returned
    `globus_sync_wait_exceeded`: we stopped waiting, but Globus did not
    stop transferring. This answers how the task actually ended. The
    outcome reflects the TASK: SUCCEEDED -> proven, FAILED -> broke,
    ACTIVE/INACTIVE -> loop (poll again).

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
    from agent.skills.outcomes import refused
    try:
        access = compute_access.load_access(None)
        # _ad_hoc-aware resolve so the poll companion accepts the same
        # project name the async upload/download accepted.
        project = transfer._get_project_or_ad_hoc(project_name, access)  # auth check
        _ = project
        env = compute_access.get_compute_env(compute_env_name, access)
    except KeyError as e:
        # unknown project / compute_env → a clean auth failure, not a crash (C4).
        return refused("globus_task_status.project_or_env_not_found",
                       error=str(e).strip("'\""), success=False)
    except (compute_access.PermissionDenied, compute_access.ConfigError,
            FileNotFoundError) as e:
        return refused("globus_task_status.access_denied",
                       error=f"{type(e).__name__}: {e}", success=False)
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
                         placeholders bound to inputs/outputs. Every `$`
                         must open a declared placeholder, and single
                         quotes / backslashes / triple-double-quotes are
                         REFUSED: main.nf would rewrite them in transit
                         and the cluster would run a different command.
                         Put awk/sed programs in a script baked into the
                         image (see workflow_render
                         `_check_command_renders_faithfully`).
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
def run_production_pipeline(project_name: str,
                           compute_env_name: str,
                           workflow_name: str,
                           tool_name: str,
                           command: str,
                           inputs: dict,
                           outputs: dict,
                           freeze_request_key: str,
                           workflow_dir: str,
                           resources: dict = {},
                           sealed_workflow: str = "",
                           platform: str = "linux/amd64") -> dict:
    """Run a frozen env's workflow in PRODUCTION on WHICHEVER compute env you
    name — local laptop OR ssh cluster. The SAME call, swap `compute_env_name`.
    Plug-and-play: what differs between loci (scheduler, container runtime,
    module names) is an ENV PROPERTY in projects_access.yaml, never an argument.

    Dispatches on the env's type:
      • local → renders a re-runnable `run.sh` that `docker run`s the frozen
        image against `workflow_dir` (same-path bind mount), launches it in the
        BACKGROUND, writes a submission manifest. Poll with check_job.
      • ssh   → delegates to the proven nextflow+slurm+apptainer submission,
        sourcing apptainer/nextflow modules + slurm policy from the env config
        and deriving the staged .sif path from freeze_request_key.

    NOT a composite. The caller still freezes the env; for the cluster locus the
    caller also stages the .sif (stage_apptainer_image) first — this verb REFUSES
    if the .sif isn't already staged. This is the PRODUCTION verb; it documents
    the run (a manifest, findable later) but does NOT seal a WorkflowSpec — the
    validation verbs (run_step_in_container / run_step_on_cluster) do that.

    Authorization: `workflow_dir` must be a `directories[]` path on the project
    with BOTH `upload` and `exec` (same wall as submit_workflow_job).

    Inputs:
      command            single-line shell command with ${PLACEHOLDER} slots —
                         the SAME syntax on both loci (one command, swap the env).
                         The CLUSTER branch is stricter about the command's
                         CONTENT: every `$` must open a declared placeholder, and
                         single quotes / backslashes / triple-double-quotes are
                         refused because main.nf's Groovy script block would
                         rewrite them. The local branch accepts them (it shell-
                         quotes straight into run.sh, no Groovy in between), so a
                         command that runs locally may still be refused on ssh —
                         keep it inside the cluster subset to stay portable.
      inputs             {PLACEHOLDER: absolute_path} — a real path on the
                         compute env. Every ${placeholder} must be declared.
      outputs            {PLACEHOLDER: bare_filename} — written into workflow_dir
                         (same convention as submit_workflow_job).
      freeze_request_key the frozen env handle (from freeze()); the uniform
                         env reference for BOTH loci.
      workflow_dir       absolute path under a `directories[]` grant.
      resources          {mem_gb, cpus, time, gpus?} — the uniform per-run
                         sizing knob. Optional locally (docker --memory/--cpus);
                         REQUIRED on the cluster (SLURM needs mem + time).
      sealed_workflow    OPTIONAL name of a sealed `{name}.workflow.yaml` to
                         check this run's DATA against. The env is pinned by
                         digest; without this NOTHING pins the references, so a
                         pipeline validated on gencode.v44 can be production-run
                         on v39 with no disclosure. Name it EXPLICITLY — it is
                         deliberately not inferred from workflow_name, because
                         the two genuinely differ in practice (a run named
                         `samtools_flagstat_prod007` against sealed workflow
                         `samtools_cluster_rung3`). Omitted ⇒ the result carries
                         `reference_check.status = "not_attempted"`, a stated
                         third state rather than a silent pass; a divergence
                         comes back `degraded`, never a bare success.

    Returns {success, locus, compute_env, job_id, workflow_dir, manifest_path,
    ...} on success; {"error": ..., ...} on any refusal/failure."""
    from agent.skills import run_production
    return run_production.run_production_pipeline(
        project_name=project_name,
        compute_env_name=compute_env_name,
        workflow_name=workflow_name,
        tool_name=tool_name,
        command=command,
        inputs=inputs,
        outputs=outputs,
        freeze_request_key=freeze_request_key,
        workflow_dir=workflow_dir,
        sealed_workflow=sealed_workflow or None,
        resources=resources or {},
        platform=platform or "linux/amd64",
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def stage_apptainer_image(project_name: str,
                          compute_env_name: str,
                          freeze_request_key: str,
                          sif_subpath: str = "") -> dict:
    """Get the apptainer .sif for a frozen env onto a compute env.

    LOCAL-BUILD-AND-SHIP — every EnvCache mode converges on one flow, and
    NOTHING heavy runs on the cluster:
      1. Obtain a local docker image — `docker pull` for an adopt record or a
         pushed `push_target` ref, otherwise the locally-built image tag or
         freeze's docker-save tarball.
      2. Build the .sif ON THE AGENT MACHINE (`local_sif.build_sif_locally`,
         which runs apptainer inside a pinned linux container, so a macOS host
         with no apptainer binary works).
      3. `transfer.upload` the FINISHED .sif; wire protocol (scp_head_node vs
         globus) comes from the env's `data_transfer.type`.
    The only cluster interaction is a read-only `test -f` probe and the upload.
    The image -> .sif conversion (unpack + mksquashfs) is heavy and never runs
    on the shared head node, and we never `apptainer pull` a multi-GB image
    there either ([[feedback-no-head-node-image-builds]]).

    Budget for it accordingly: the .sif is built and transferred by US, so a
    multi-GB env costs local disk, local CPU and a real transfer — this is not
    a cheap one-hop cluster-side pull. Idempotent: re-stages are a no-op when
    the .sif already exists, so the cost is paid once per (env, digest).

    Where the container artifact lands:
      .sif: `<env.container_upload_target>/<env_name>_<digest>.sif`
      Override the subpath via `sif_subpath` (relative, no `..`); it still
      resolves against `container_upload_target`.

    Authorization: project must have a `compute_env_access` entry for
    the env, AND the env must declare a `container_upload_target` with
    `upload` perm. NO FALLBACK to agent_common_data_target — that zone
    is for reference data; an undeclared container target is a clean
    refusal, not a silent reroute (per
    [[project-container-artifacts-routing]]).

    Returns {success, mode, sif_path, image_digest, request_key,
    skipped, staged_at, local_sif?, builder_image?} on success;
    {"error": ..., hint?, ...} on failure. `skipped: true` means the .sif
    already existed — re-stages are intentionally idempotent. `mode` is the
    EnvCache record's mode with a `_local_sif` suffix when a build actually
    ran (e.g. `adopt_local_sif`, `build_local_sif`), and the bare record mode
    on a skip — so the suffix tells you whether this call paid the build cost
    or just found the artifact already there."""
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

    `command`: single-line, with `${name}` placeholders bound to
    inputs/outputs. Every `$` must open a declared placeholder, and single
    quotes / backslashes / triple-double-quotes are REFUSED before any ssh —
    main.nf's Groovy script block would rewrite them and the compute node
    would run a different command (see workflow_render
    `_check_command_renders_faithfully`). Put awk/sed programs in a script
    baked into the image, where they become part of the frozen artifact.

    INPUT SEMANTICS — read this (differs from run_step_in_container):
    `inputs` values are REMOTE absolute paths that must ALREADY EXIST on
    the cluster (they become `${params.x}` in main.nf verbatim). This
    primitive uploads ONLY the 3 rendered workflow files (main.nf /
    nextflow.config / launcher.sh) to scratch — it does NOT stage input
    DATA. Its local analog `run_step_in_container` bind-mounts LOCAL
    paths; this one does not. So `upload(...)` your input data into
    scratch (or point at data already on the cluster) BEFORE calling —
    an input path that isn't present on the cluster makes the Nextflow
    process fail at runtime, not here. (Auto-staging local inputs is a
    planned Wave-2 improvement; today the precondition is on the caller.)

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
