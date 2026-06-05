"""bridge_tools — HPC bridge actuator surface (Phase 2+).

Sibling to observability_tools.py (Phase 1's read-only `snapshot_project`).
While observability is pure-read, this module's primitives push bytes,
submit jobs, monitor them, and fetch results back — all under the same
permission gate (`compute_access.check_permission`) and the same
ControlMaster ssh pattern.

Today the surface is:
  upload_to_scratch / download_from_scratch              — Step 2
  upload_to_common_data / download_from_common_data      — Step 3
  upload_to_project_path / download_from_project_path    — Step 4
  cluster_module_avail                                   — Step 4

Three transfer auth families coexist (intentional):
  scratch        — env-implicit + auto-prefix by project (sandbox)
  common_data    — env-implicit + shared namespace (reference data)
  project_path   — Phase-1 explicit grant via directories[] (workspace)

Coming as each step lands:
  cluster_job_status                                     — Step 5
  submit_data_acquisition_job                            — Step 7
  submit_workflow_job                                    — Step 9

Authorization shape (env-implicit grant, Phase 2):
  - `project_name` + `compute_env_name` resolve to a project's
    compute_env_access entry (the project must have ACCESS to the env)
  - The env declares `agent_scratch_target` / `agent_common_data_target`
    blocks at the env level; their `permissions:` lists the supported
    capabilities — these are the GRANT (no per-project re-declaration)
  - Multi-project isolation: the resolved path is auto-prefixed with
    `project_name` (`<target>/<project>/<remote_subpath>`)
  - The agent-supplied path component (`remote_subpath`) is pure-string
    validated BEFORE any I/O

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
def upload_to_scratch(project_name: str,
                     compute_env_name: str,
                     local_path: str,
                     remote_subpath: str) -> dict:
    """Push a local file into the agent's scratch sandbox on a compute env,
    under THIS project's auto-prefixed namespace. sha256 round-trip is
    verified; mismatch refuses.

    Authorization (env-implicit): the project must have a `compute_env_access`
    entry naming `compute_env_name`, AND the env must declare an
    `agent_scratch_target` block whose `permissions` include `upload`.
    The project's `directories[]` is NOT consulted — that list is for
    project-specific paths only.

    Multi-project isolation: the resolved path is auto-prefixed with
    project_name. A call to upload_to_scratch('proj_a', ..., 'x.txt')
    lands at `<scratch.path>/proj_a/x.txt`; 'proj_b' lands elsewhere.

    `remote_subpath` rules: non-empty, ≤255 chars, no leading '/', no
    '..' segments, no shell metacharacters (newline / `;` / `|` / `$` /
    backticks / etc.), no whitespace.

    `project_name` rules: safe token (alnum + '_-', ≤64 chars) — used
    as the auto-prefix path component.

    `local_path`: must exist, be a REGULAR file (symlinks refused —
    defense against user's home symlink redirecting to /etc/shadow),
    and be under the 5 GiB head-node cap. Anything larger should go
    through `submit_cluster_job(job_type='data_acquisition')` (Step 6)
    which curl-resumes inside a SLURM job, or eventually Globus.

    Returns {success, compute_env, remote_path, sha256, bytes,
    duration_s, transferred_at} on success; {"error": "..."} on any
    refusal or transfer failure (no exception escapes — the MCP surface
    is dict-in-dict-out)."""
    from agent.skills import scratch
    return scratch.upload_to_scratch(
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_subpath=remote_subpath,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def upload_to_common_data(project_name: str,
                         compute_env_name: str,
                         local_path: str,
                         remote_subpath: str) -> dict:
    """Push a local file into the env's SHARED common-data zone.

    Authorization (env-implicit): project has compute_env_access for the
    env; env declares an `agent_common_data_target` block whose
    `permissions` include `upload`. The project's directories[] is NOT
    consulted.

    No project auto-prefix — common_data is intentionally SHARED across
    projects so reference data can be mixed and matched. The resolved
    path is `<common_data.path>/<remote_subpath>` directly.

    Overwrite refusal: if the resolved path already exists, the primitive
    refuses to upload. Reference data is versioned (e.g.
    `exomiser/v3.2.0/data.zip`), not silently replaced. Delete remotely
    before uploading a fresh version.

    Same path-safety + sha256-round-trip + 5 GiB head-node cap rules as
    upload_to_scratch.

    Returns {success, compute_env, remote_path, sha256, bytes, duration_s,
    transferred_at} on success; {"error": "..."} on refusal/failure."""
    from agent.skills import common_data
    return common_data.upload_to_common_data(
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_subpath=remote_subpath,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def download_from_common_data(project_name: str,
                             compute_env_name: str,
                             remote_subpath: str,
                             local_path: str) -> dict:
    """Pull a file from the env's SHARED common-data zone back to local.

    Symmetric to `upload_to_common_data` — same env-implicit auth with
    `download` (instead of `upload`) as the required capability on the
    env's `agent_common_data_target.permissions`. Discrete capabilities,
    not a lattice — `upload` alone does NOT satisfy `download`.

    No project auto-prefix; any project with env access can read any
    file in common_data.

    `local_path`: must NOT exist (no overwrite); parent must be writable.

    Returns {success, compute_env, remote_path, local_path, sha256,
    bytes, duration_s, fetched_at} on success; {"error": "..."} on
    refusal/failure."""
    from agent.skills import common_data
    return common_data.download_from_common_data(
        project_name=project_name,
        compute_env_name=compute_env_name,
        remote_subpath=remote_subpath,
        local_path=local_path,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def upload_to_project_path(project_name: str,
                          compute_env_name: str,
                          abs_path: str,
                          local_path: str) -> dict:
    """Push a local file to an authorized project-workspace path.

    Authorization (Phase-1 explicit): the project's `compute_env_access[]
    .directories[]` MUST include an entry whose path contains the
    requested abs_path (longest-prefix match), and that entry's
    `permissions:` MUST include `upload`. The Phase-2 env-implicit grant
    does NOT apply here — project_path is for the user's OWN data, which
    the user authorizes path-by-path.

    The abs_path is supplied LITERALLY (not as a relative subpath that
    gets auto-prefixed). This is so the agent can write to the project's
    real directory layout (e.g. `/work/.../PLANT_PROJECT/runs/...`)
    without disturbing it.

    Same upload contract as the other primitives: 5 GiB head-node cap,
    sha256 round-trip, refuses overwrites.

    Returns {success, compute_env, remote_path, sha256, bytes, duration_s,
    transferred_at} on success; {"error": "..."} on refusal/failure."""
    from agent.skills import project_path
    return project_path.upload_to_project_path(
        project_name=project_name,
        compute_env_name=compute_env_name,
        abs_path=abs_path,
        local_path=local_path,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def download_from_project_path(project_name: str,
                              compute_env_name: str,
                              abs_path: str,
                              local_path: str) -> dict:
    """Pull a file from an authorized project-workspace path back to local.

    Symmetric to upload_to_project_path; required permission is
    `download` on the matching directories[] entry. Discrete capability
    — `upload` alone does NOT satisfy `download`.

    `local_path`: must NOT exist; parent must be writable.

    Returns {success, compute_env, remote_path, local_path, sha256,
    bytes, duration_s, fetched_at} on success; {"error": "..."} on
    refusal/failure."""
    from agent.skills import project_path
    return project_path.download_from_project_path(
        project_name=project_name,
        compute_env_name=compute_env_name,
        abs_path=abs_path,
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
def download_from_scratch(project_name: str,
                         compute_env_name: str,
                         remote_subpath: str,
                         local_path: str) -> dict:
    """Pull a file from THIS project's scratch namespace back to a local
    path. sha256 round-trip is verified BEFORE declaring success; on
    mismatch the partial local file is removed and an error is returned.

    Symmetric to `upload_to_scratch` — same env-implicit authorization
    with `download` (instead of `upload`) as the required capability on
    the env's `agent_scratch_target.permissions`. Discrete capabilities,
    not a lattice — `upload` alone does NOT satisfy `download`.

    Multi-project isolation: the resolved path is auto-prefixed with
    project_name. This project sees only its OWN namespace; cross-project
    visibility requires another project.

    `local_path` rules: must NOT exist yet (no silent overwrites; the
    same never-overwrite contract upload uses on the remote side). Its
    parent directory must exist and be writable.

    Returns {success, compute_env, remote_path, local_path, sha256,
    bytes, duration_s, fetched_at} on success; {"error": "..."} on
    refusal/failure."""
    from agent.skills import scratch
    return scratch.download_from_scratch(
        project_name=project_name,
        compute_env_name=compute_env_name,
        remote_subpath=remote_subpath,
        local_path=local_path,
        access_path=_resolve_access_path(),
    )


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
    """Render → upload → sbatch a one-process Nextflow workflow.

    End-to-end submission primitive: takes a single-tool workflow spec,
    renders main.nf/nextflow.config/launcher.sh via workflow_render,
    uploads them to `workflow_dir` via upload_to_project_path, runs
    `sbatch --parsable launcher.sh` over ssh, returns the SLURM job_id
    the agent can poll with cluster_job_status.

    Composition discipline: NOT a composite — the caller still calls
    freeze() to build the env, upload_to_common_data to push the .sif,
    cluster_job_status to poll, download_from_project_path to fetch
    outputs. This primitive is the irreducible *submission* step.

    Authorization (Phase-1 explicit, dir-by-dir):
      - project must have a `compute_env_access` entry for the env
      - the project's `directories[]` under that env must contain
        `workflow_dir` (longest-prefix match) with `permissions:`
        including BOTH `upload` (for the file pushes) AND `exec` (so
        the SLURM job may write outputs in-place during execution)

    Inputs:
      workflow_dir       absolute remote path; the per-run dir on the
                         compute env. No-overwrite contract means a
                         second submit to the same dir fails — pick a
                         fresh per-run subdir per submission.
      workflow_name      safe-token, ≤64 chars. Used for `sbatch
                         --job-name` AND in render_workflow's tag.
      tool_name          safe-token; identifies the tool in
                         process_name + comments.
      command            single-line shell command with `${name}`
                         placeholders bound to inputs/outputs.
      inputs             {placeholder_name: remote_abs_path} — what
                         the running process will read.
      outputs            {placeholder_name: bare_filename} — what the
                         process writes (to the working dir).
      apptainer_sif      absolute remote path to the frozen .sif.
                         Caller uploads via upload_to_common_data
                         first; pass the remote path here.
      apptainer_module   Lmod token, e.g. "apptainer/1.4.1".
      nextflow_module    Lmod token, e.g. "nextflow/25.04.7".
      slurm              {queue, time, mem, cpus, account?} —
                         closed-key block (typos refused).

    Returns on success:
      {success: True, compute_env, job_id, workflow_dir,
       files_uploaded: [...], submitted_at, upload_started}
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
        Two steps: (1) transfer .tar via the env's configured
        `bulk_transfer.type` — today scp head node via
        upload_to_common_data, future datamover/globus by ONE
        internal branch; (2) ssh `apptainer build <sif>
        docker-archive://<tar>` on the cluster.

    Where the .sif lands:
      `<env.agent_common_data_target>/apptainer/<env_name>_<digest>.sif`
      by default. Override via `sif_subpath` (relative, under the
      common_data zone).

    Authorization: project must have a `compute_env_access` entry for
    the env, AND the env must declare an `agent_common_data_target`
    with `upload` perm. For BUILD-archive path,
    upload_to_common_data's auth is re-checked at upload time.

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
                        workflow_dir: str,
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
    """Path-4 keystone — run a workflow step on cluster + record the
    cluster-locus evidence as a pipeline_step in the draft.

    Composes: `stage_apptainer_image` (idempotent) → `submit_workflow_job`
    (render+upload+sbatch) → `cluster_job_status` (poll to terminal) →
    `cluster_job_resources` (sacct's MaxRSS for I7) →
    `download_from_project_path` (sha256 round-trip per output) →
    type-aware validation. The pipeline_step records `validation_locus:
    "cluster"` so a future reader sees the evidence came from sacct,
    not host psutil.

    After this returns successfully, three legitimate next moves:
      (a) `seal_workflow(pipeline_id, freeze_request_key)` — produce
          a sealed WorkflowSpec with cluster-locus evidence
      (b) Call again with a different command — multi-step workflows
      (c) `discard_pipeline_draft(pipeline_id)` — run-and-go

    `outputs`: `{placeholder_name: bare_filename}` — the file the
    process writes (Nextflow's publishDir lands it in workflow_dir).
    `download_local_dir`: where to materialize the fetched outputs
    locally (created if absent).
    `output_types`: optional `{basename|ext: validator_type}` overrides
    for type-aware validation (same shape as run_step_in_container).

    Returns {success, returncode, job_id, sif_path, workflow_dir,
    resource_usage, detected_outputs, output_sha256, validations,
    validation_count, download_errors, pipeline_merge, final_status}
    on success; {"error": ..., stage_result/submit_result/last_poll?}
    on any phase's refusal/failure. The prior phases' results are
    preserved on the error dict for diagnosis."""
    from agent.skills import run_cluster_step
    return run_cluster_step.run_step_on_cluster(
        pipeline_id=pipeline_id,
        freeze_request_key=freeze_request_key,
        project_name=project_name,
        compute_env_name=compute_env_name,
        workflow_dir=workflow_dir,
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
