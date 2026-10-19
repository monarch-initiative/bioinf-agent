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
  - `project_name` + `compute_env_name` resolve through the project's
    `compute_envs` list (the project must have ACCESS to the env)
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

# `mcp` is the FastMCP app and is never monkeypatched, so a bare import is safe.
# The bridge primitives reach no mcp_server singleton; a tool here that comes to
# need one goes through `_ms.X` like every other agent/mcp_tools/ submodule (see
# feedback-mcp-tools-conventions).
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched
from agent.skills.backgroundable import backgroundable


def _resolve_access_path() -> str | None:
    """Return the path to projects_access.yaml as a string, or None when the
    file does not exist — the primitive then surfaces a clean FileNotFoundError.

    ONE resolution, in `compute_access.default_access_path` — deriving another
    candidate here would be a second answer to where the file lives."""
    from agent.skills import compute_access as _ca
    p = _ca.default_access_path()
    return str(p) if p.exists() else None


@mcp.tool()
@backgroundable("project_name")
def upload(project_name: str,
           compute_env_name: str,
           local_path: str,
           remote_abs_path: str) -> dict:
    """Push a local file, or a whole directory, to an ABSOLUTE path on a
    compute env. The auth family is auto-routed by where the path falls — agent scratch
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
    sync-wait cap returns `globus_sync_wait_exceeded` WITH its task_id, and
    one whose end could not be seen (a status query that failed, a cancel
    that did not take) `globus_task_unobserved` — the transfer may still be
    running server-side; resolve it with `globus_task_status`, do not
    assume it failed. Until then, an upload to the same path sends nothing
    and returns `in_flight` with the call that resolves it.

    Manifest: every call (success OR failure) writes a JSON record under
    `transfer_history/<project>/<YYYY-MM-DD>/<stamp>_upload_<hash>.json`
    so the operation is replayable programmatically — no memory needed.

    Path safety: `remote_abs_path` must be absolute, ≤4096 chars, no
    '..' traversal, no shell metacharacters, no whitespace.

    `local_path`: must exist, be a regular file or a directory of them
    (symlinks refused anywhere in it), and be under the 5 GiB cap, which
    applies on every wire. Larger data is pulled by a compute node instead
    (`download_reference_database` with `compute_env`, or
    `acquire_reference_via_recipe`). A directory moves as one transfer, every
    file checked, and the result adds `files` and `checksums` (a
    `sha256sum -c` list beside the receipt).

    Returns {success, project, compute_env, zone, direction:"upload",
    local_path, remote_abs_path, provider, task_id?, bytes, duration_s,
    local_sha256, remote_sha256?, verified_method, manifest, files?,
    checksums?} on success; {"error": "...", "manifest": "..."} on any failure."""
    from agent.skills import transfer
    return transfer.upload(
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_abs_path=remote_abs_path,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
@backgroundable("project_name")
def download(project_name: str,
             compute_env_name: str,
             remote_abs_path: str,
             local_path: str) -> dict:
    """Pull a file, or a whole directory, from an ABSOLUTE path on a compute
    env to this laptop. A directory moves as one transfer: every file is
    checked against the source, symlinks are not followed (a tree holding one
    is refused over scp, and reported incomplete over Globus), and the result
    adds `files` and `checksums` (a `sha256sum -c` list beside the receipt).
    A transfer that may outlast the ~9-minute wait (a large directory) is one
    to start with `background=True`. The bytes land under a hidden partial
    name beside `local_path` and take its name only once verified, so a file
    under that name is always whole. A globus task past the wait cap
    (`globus_sync_wait_exceeded`), or one whose end could not be seen
    (`globus_task_unobserved`), keeps landing there; `globus_task_status`
    puts it in place when the task has succeeded. Until then a download to
    the same `local_path`, from any env, sends nothing and returns
    `in_flight` with the call that resolves it. A file that appears at
    `local_path` while the download runs is never replaced (`local_exists`).
    Symmetric to `upload`: same zone routing (auto-classified by where
    remote_abs_path falls), same auth (the matched zone must declare
    `download` in its permissions; `upload` alone does NOT satisfy
    `download` — discrete capabilities), same manifest.

    For one-off downloads use `project_name="_ad_hoc"`.

    `local_path`: must NOT exist (no silent overwrite); parent dir must
    exist + be writable.

    Returns {success, project, compute_env, zone, direction:"download",
    local_path, remote_abs_path, provider, task_id?, bytes, duration_s,
    local_sha256, remote_sha256?, verified_method, manifest, files?,
    checksums?} on success; {"error": "...", "manifest": "..."} on any failure."""
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

    Pure-read: runs `module avail` in a login shell over ONE ssh invocation,
    parses the output, returns a flat list of `<name>/<version>`
    strings. Does not load any module; does not submit any job; never
    writes anything.

    Authorization: the project's `compute_envs` must list
    `compute_env_name`. No per-directory permission needed — we're not
    touching the filesystem.

    `pattern` (optional): forwarded to `module avail <pattern>` AND
    filtered client-side. Useful: `pattern='nextflow'` returns just
    the nextflow versions. Must be a safe token (alnum + `_+.-/`).

    Returns {compute_env, pattern, modules, module_count, captured_at}
    on success; {"error": "...", "hint": ...} on failure (e.g. no
    ControlMaster session). Only what `module avail` printed is read, never
    the login profile's own output; a listing the login shell never printed
    is `listing_unobserved`, a login shell with no `module` command is
    `module_command_missing`, and `module avail` exiting other than 0 or 1
    (1: nothing matched) is `module_avail_failed`, never an empty list."""
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

    Pure-read: runs `sinfo -h -o …` in a login shell over ONE ssh invocation, parses
    the output, returns one record per partition. Submits nothing, loads
    nothing, writes nothing.

    Authorization: the project's `compute_envs` must list
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
    Runs `globus task show <id> --format json`; no submission.

    Reach for this when `upload`/`download` returned
    `globus_sync_wait_exceeded` or `globus_task_unobserved`: we stopped
    watching, but Globus may not have stopped transferring. This answers how the task actually ended. The
    outcome reflects the TASK: SUCCEEDED -> proven, FAILED -> broke,
    ACTIVE/INACTIVE -> loop (poll again). A directory task that SUCCEEDED
    but skipped files or left symlinks behind -> broke: not a whole copy.
    A transfer lands under a hidden partial name until it is verified; when
    the task has SUCCEEDED, this call renames it into place (`renamed_to`)
    and the transfer's receipt says it finished; when it ended otherwise,
    the receipt says it failed and a transfer to that path may go again.
    A task is resolved only on the env it ran on (another env is refused,
    `task_other_env`, naming the call), and only once the call that started
    it has returned: while that call still waits (a `background=True`
    transfer), or another globus_task_status is putting it in place, this
    call changes nothing and says so (`task_call_waiting`); a hold on the
    transfer's receipt that cannot be taken at all is `receipt_hold_failed`.
    Once the transfer has ended, `transfer` and `manifest` say how, from its
    receipt; a task that SUCCEEDED whose bytes were not put in place is
    `task_transfer_unplaced`.
    An upload's rename is made on the cluster. When it does not answer as
    made, what is at each name decides: the partial still there and the
    destination free, or either name unreadable, keeps the hold (call
    again); the destination written meanwhile is `task_rename_blocked`
    (nothing replaced, the bytes stay at the partial); the partial gone and
    the destination there is `task_rename_unconfirmed`; both gone clears the
    way to upload again.
    A download's rename is made here, once what landed has been read (a
    directory must hold every file the task moved): its `local_sha256`, or
    `files` and `checksums`, come back as a sync download's do. A download
    whose task did not succeed has what arrived removed. One that cannot be
    put in place: the destination written meanwhile is `task_rename_blocked`
    (nothing replaced, the copy removed); a copy short of the task, or
    nothing left under the partial name, is `task_rename_failed` (download
    it again), but `task_rename_unconfirmed` when the destination holds a
    file. A rename this machine's filesystem refused is `task_rename_failed`
    with `partial_path`: the bytes stay there and a later call retries it.

    Authorization: the project's `compute_envs` must list the env, AND
    the env must declare `data_transfer.type: globus`.
    Task IDs are scoped to the user's Globus tokens — the agent never
    sees a task belonging to anyone else.

    Accepts `project_name="_ad_hoc"` — the same synthesized one-off
    project the upload/download primitives accept — so a task submitted
    via an `_ad_hoc` async upload can be polled with the same project
    name (it has access to every env, so it can poll any env's task).

    `task_id` must be a canonical UUID; refused before any shell-out
    so a smuggled metacharacter can't reach the `globus` CLI.

    Returns {success, task_id, status, nice_status, bytes_transferred,
    files_transferred, files_skipped, symlinks, fatal_error, type, captured_at}
    on success. `status` is one of ACTIVE, INACTIVE, SUCCEEDED, FAILED.
    {"error": "..."} on failure (cli missing, auth, network)."""
    from agent.skills import compute_access, transfer
    from agent.skills.outcomes import refused
    try:
        access = compute_access.load_access(None)
        # _ad_hoc-aware resolve so the poll companion accepts the same
        # project name the async upload/download accepted.
        project = transfer._get_project_or_ad_hoc(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)
    except KeyError as e:
        # unknown project / compute_env → a clean auth failure, not a crash (C4).
        return refused("globus_task_status.project_or_env_not_found",
                       error=str(e).strip("'\""), success=False)
    except (compute_access.PermissionDenied, compute_access.ConfigError,
            FileNotFoundError) as e:
        return refused("globus_task_status.access_denied",
                       error=f"{type(e).__name__}: {e}", success=False)
    if not any(isinstance(b, dict) and b.get("compute_env") == compute_env_name
               for b in (project.get("compute_env_access") or [])):
        return refused("globus_task_status.access_denied", success=False,
                       error=f"PermissionDenied: project {project_name!r} does not "
                             f"list compute_env {compute_env_name!r}")
    return transfer.resolve_globus_task(env, task_id)


@mcp.tool()
def cluster_job_status(project_name: str,
                       compute_env_name: str,
                       job_id: str,
                       run_dir: str = "",
                       wait_s: int = 0) -> dict:
    """Look up SLURM state for `job_id` on `compute_env_name` so the
    agent can poll a submitted job to completion.

    Pure-read: runs, in a login shell, `sacct -j <id> -P --noheader -X -o <fields>`
    over ssh, parses the pipe-delimited output, returns a list of row-dicts.
    Does not submit, cancel, or modify anything.

    `run_dir`: a pipeline's launch directory (what `run_production_pipeline`
    returned), in a zone or directory the project may `download` from — its log
    and trace are read back. The job is then its manager, and `pipeline` reports the work:
    every finished task from the run's trace, every task submitted and not
    finished with the scheduler's state for it (PENDING and why, RUNNING where
    and for how long), and a verdict over the tasks: succeeded, failed, running,
    not_started (the manager is queued) or unobserved (the manager ended and left no
    task record in run_dir — check that run_dir is where it was launched).

    `wait_s`: block up to that many seconds (capped at 540) until something
    changes — the job's state, or a task starting or finishing — so ONE call
    replaces a poll loop. Prefer `wait_s=300` over calling this repeatedly.

    Authorization: the project's `compute_envs` must list
    `compute_env_name`; SLURM's own ACL keeps the visibility scoped to the
    user's own jobs. A `run_dir` must be a path the project may `download`.

    `job_id` must be digits with an optional `_<task>` suffix for array
    tasks (e.g. `12345`, `12345_3`). Anything else is refused BEFORE
    any ssh — a smuggled `12345; rm -rf /` never reaches the cluster.

    Returns {compute_env, job_id, jobs: [{job_id, state, elapsed,
    exit_code, nodelist, reason, start, end, verdict}, ...], captured_at,
    pipeline?, waited_s?} on success; {"error": "...", "hint": ...} on
    failure. Empty `jobs`
    means sacct doesn't recognize the id — caller distinguishes "not
    yet in slurmdbd" from "never existed" with a short retry."""
    from agent.skills import cluster_jobs
    return cluster_jobs.cluster_job_status(
        project_name=project_name,
        compute_env_name=compute_env_name,
        job_id=job_id,
        run_dir=run_dir or "",
        wait_s=wait_s,
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
    that diverged launches as `degraded`, never a bare success; so does a value (a
    params.yaml value or a samplesheet value column) the seal's self-test never ran,
    named in `value_check`.

    Inputs:
      pipeline     the name of a directory `render_pipeline` wrote under <workspace>/pipelines/,
                   or a rendered directory's absolute path.
      run_dir      absolute path on the env: the launch directory, created by the first
                   launch (on the cluster, as one transfer). It may be under the env's pipelines
                   zone, the agent's scratch, or a project `directories[]` grant with `upload`
                   and `exec`. A run_dir that already holds this pipeline is re-launched with
                   `-resume`; any other existing directory is refused.
      samplesheet  local CSV, one row per sample, with the columns the pipeline reads
                   (`sample` first; paths absolute AT THE LOCUS). Required on the first
                   launch into a run_dir; omit on a re-launch.
      params       {params.yaml key: value}, written into the run directory's params.yaml
                   before the launch, so the directory says what runs there and a later
                   launch, by this tool or by hand, runs the same. A path, a word or a number.
      walltime     the manager job's SLURM time, written into the run directory's launcher.sh.
                   ssh envs only.

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
      Beside it, `<env_name>_<digest>/` holds the env's record (ENV report,
      attestation, both recipe forms), so the cluster copy says what it holds
      and how it was proven; sent once, also for a .sif staged before.
      `env_record.status` says sent / already_there (its checksums match) /
      differs / not_this_image (the record here is of a later freeze under the
      same name) / not_sent (never fatal to the .sif) / none_here.

    Authorization: the project's `compute_envs` must list the env, AND
    the env must declare a `container_upload_target` with
    `upload` perm. NO FALLBACK to agent_common_data_target — that zone
    is for reference data; an undeclared container target is a clean
    refusal, not a silent reroute (per
    [[project-container-artifacts-routing]]).

    Returns {success, mode, sif_path, image_digest, request_key,
    skipped, staged_at, env_record, local_sif?, builder_image?} on success;
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
    open a declared placeholder, and a backslash or a `\"\"\"` is refused before any ssh
    (main.nf's Groovy script block would rewrite them; a single quote is carried
    faithfully). Put awk/sed programs in a script baked into the image.

    `inputs` are REMOTE absolute paths that must already exist on the cluster (they become
    `${params.x}` verbatim). This primitive uploads no input data: `upload(...)` it into
    scratch first (outside the workflow_dir, which the step creates), or point at data
    already there. `outputs`: `{placeholder: bare
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
