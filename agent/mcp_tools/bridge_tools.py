"""bridge_tools — HPC bridge actuator surface (Phase 2+).

Sibling to observability_tools.py (Phase 1's read-only `snapshot_project`).
While observability is pure-read, this module's primitives push bytes,
submit jobs, monitor them, and fetch results back — all under the same
permission gate (`compute_access.check_permission`) and the same
ControlMaster ssh pattern.

Today the surface is:
  upload / download              — unified transfer (zone auto-routed
                                   by where the absolute remote path
                                   falls)
  stage_apptainer_image          — get a frozen env's .sif onto a env
  run_production_pipeline        — run a RENDERED pipeline in production (submit-and-document)
  run_step_on_cluster            — validation/seal run in scratch
  cluster_job_status             — sacct job-state poll
  cluster_module_avail           — Lmod discovery
  cluster_partitions             — SLURM partition / GPU discovery
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
    """Return the path to projects_access.yaml as a string, or None when the
    file does not exist — the primitive then surfaces a clean FileNotFoundError.

    ONE resolution, in `compute_access.default_access_path` — deriving another
    candidate here would be a second answer to where the file lives."""
    from agent.skills import compute_access as _ca
    p = _ca.default_access_path()
    return str(p) if p.exists() else None


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
def cluster_partitions(project_name: str,
                       compute_env_name: str,
                       pattern: str = "") -> dict:
    """Discover the SLURM partitions on a compute env — so a GPU convention
    can be read off the cluster instead of typed by hand.

    Pure-read: runs ONE ssh invocation of `bash -lc 'sinfo -h -o …'`, parses
    the output, returns one record per partition. Submits nothing, loads
    nothing, writes nothing.

    Authorization: project must have a `compute_env_access` entry for
    `compute_env_name`. No per-directory permission needed — the filesystem
    is not touched, and SLURM's own ACLs scope what sinfo reports.

    `pattern` (optional): client-side substring filter on the partition name
    (`sinfo` has no substring filter of its own, and `-p` demands an exact
    name). Must be a safe token (alnum + `_+.-/`).

    Each partition carries {name, is_default, avail, time_limit,
    cpus_per_node, memory_mb, gres, gpus[], has_gpu, states[], node_count};
    `gpus[]` holds {type, count_per_node} parsed out of gres, so "has GPUs"
    and "has A100s" are distinguishable when the site records the type.

    **Both halves of the GPU convention.** A GPU header wants a `{partition,
    qos}` pair. `sinfo` answers the hardware half; a second probe (`scontrol
    show partition -o`, same ssh round trip) reads `AllowQos=` for the other.
    `gpu_convention_candidates` holds the pairs the cluster would accept, each
    tagged with its `gpu_types` and limits.

    They are CANDIDATES, not a pick: choosing between an A100 partition and
    a consumer-card partition is a sizing judgement about the workload, which
    is yours. A partition whose QoS was NOT observed contributes no candidate
    — half a pair is not a convention — but stays visible in `partitions[]`
    with `qos_observed: False`, so the gap is legible rather than silently
    dropped. `AllowQos=ALL` (constrains nothing) is kept distinct from
    "never looked".

    **Nothing here is required to submit.** Pass a chosen pair straight into a
    job's `slurm={"gpus": N, "partition": ..., "qos": ...}` and it wins over
    whatever the env declares; pass neither and the job renders `--gres` alone
    with `gpu_placement: undeclared`, which is the RIGHT submission on a
    cluster whose scheduler places gres requests itself. Use this probe when
    you want to name a partition, not because you must.

    Returns {compute_env, pattern, partitions, partition_count,
    gpu_partitions, default_partition, qos_observable,
    gpu_convention_candidates, captured_at} on success; {"error": "...",
    hint: ...} on failure (e.g. no ControlMaster session, or no SLURM
    here)."""
    from agent.skills import cluster_partitions as _cp
    return _cp.cluster_partitions(
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
                       job_id: str,
                       run_dir: str = "") -> dict:
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
        run_dir=run_dir or "",
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def run_production_pipeline(project_name: str,
                            compute_env_name: str,
                            pipeline: str,
                            run_dir: str,
                            samplesheet: str = "",
                            params: dict = {},
                            walltime: str = "") -> dict:
    """Run a RENDERED pipeline in production on whichever compute env you name — ONE verb,
    swap `compute_env_name`. The pipeline directory `render_pipeline` wrote is copied into
    `run_dir` on that env, your samplesheet becomes its samples.csv, and the run starts the
    one way a rendered pipeline runs: `sbatch launcher.sh` on an ssh env, a background
    `nextflow run main.nf -profile local …` on a local env. Submit-and-document: a manifest
    is written and the call returns; follow up with `cluster_job_status(…, run_dir=)` or
    `check_job`.

    Before anything is copied or launched it checks AT THE LOCUS: every image the stages
    run in is there (the staged `.sif` — refused with the `stage_apptainer_image` call to
    make otherwise; the docker image locally); every path the run binds — the shared path
    parameters and every path cell of the samplesheet — exists; and the shared references
    still hash to what the workflow was sealed against where an anchor exists. A reference
    that diverged launches as `degraded`, never a bare success.

    Inputs:
      pipeline     a name under <workspace>/pipelines/ (see list_installed_pipelines) or a
                   rendered directory's absolute path.
      run_dir      absolute path on the env: the launch directory, created if it does not
                   exist yet. It may be under the env's pipelines zone, the agent's scratch,
                   or a project `directories[]` grant with `upload` and `exec`. A run_dir that already
                   holds this pipeline is re-launched with `-resume`; one holding another
                   render is refused.
      samplesheet  local CSV, one row per sample, with the columns the pipeline reads
                   (`sample` first; paths absolute AT THE LOCUS). Required on the first
                   launch into a run_dir; omit on a re-launch.
      params       {params.yaml key: value} for this run only — on the launch line, so it
                   wins over params.yaml. A path, a word or a number.
      walltime     the manager job's SLURM time for this submission (`sbatch --time=…`).

    Returns `{success, locus, pipeline, run_dir, job_id, samplesheet, params, launch,
    manifest_path, reference_check, follow_up, …}`; refusals name their remedy.
    """
    from agent.skills import run_production
    return run_production.run_production_pipeline(
        project_name=project_name, compute_env_name=compute_env_name,
        pipeline=pipeline, run_dir=run_dir, samplesheet=samplesheet or "",
        params=dict(params or {}), walltime=walltime or "",
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
    """Run a workflow step ON THE CLUSTER, in the agent's scratch sandbox, and record
    cluster-locus evidence as a pipeline_step in the draft.

    Scratch only: workflow_dir is `<env.agent_scratch_target.path>/<project>/<workflow_name>/`
    and there is no knob to point elsewhere; production runs against project workspaces go
    through `run_production_pipeline`. The env must declare `agent_scratch_target` with `exec`.

    Composes: `stage_apptainer_image` (idempotent) → render + upload the three workflow
    files (main.nf, nextflow.config, launcher.sh) → `sbatch --parsable` → poll
    `cluster_job_status` to a terminal state (validation jobs are bounded) →
    `cluster_job_resources` (sacct MaxRSS, I7) → `download` each output (sha256
    round-trip) → type-aware validation. The step records `validation_locus: "cluster"`.
    Then: `seal_workflow`, another step with a FRESH workflow_name, or
    `discard_pipeline_draft`.

    `command`: ONE line with `${name}` placeholders bound to inputs/outputs; every `$` must
    open a declared placeholder, and single quotes, backslashes and triple double quotes
    are refused before any ssh (main.nf would rewrite them). Put awk/sed programs in a
    script baked into the image.

    `inputs` are REMOTE absolute paths that must already exist on the cluster (they become
    `${params.x}` verbatim). This primitive uploads no input data: `upload(...)` it into
    scratch first, or point at data already there. `outputs`: `{placeholder: bare
    filename}`. `download_local_dir`: where fetched outputs land (created if absent).
    `output_types`: optional `{basename|ext: validator_type}`, as in run_step_in_container.

    Returns `{success, returncode, job_id, sif_path, workflow_dir, resource_usage,
    detected_outputs, output_sha256, validations, validation_count, download_errors,
    pipeline_merge, final_status}`; on a phase's refusal or failure `{"error": …}` with the
    prior phases' results kept for diagnosis.
    """
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
