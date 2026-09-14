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
  3. transfer.upload         — push each rendered file into the
                               agent's scratch sandbox (the scratch
                               zone is routed by the workflow_dir path,
                               which is under agent_scratch_target)
  4. sbatch_via_ssh          — kick off the SLURM job, get job_id
  5. cluster_job_status      — poll until terminal (validation jobs
                               are short; polling is viable here in a
                               way it isn't for production)
  6. cluster_job_resources   — fetch wall_seconds + peak_rss_mb from
                               sacct's batch row (I7 evidence)
  7. transfer.download       — fetch outputs back local (sha256
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
from agent.skills.outcomes import proven, refused, broke
from agent.skills.pipeline_state import validation_key as _validation_key
from agent.validators.output_validator import infer_validator_type


# workflow_name becomes a path component under scratch — keep it safe.
# Same shape as the project_name token validator (alnum + _-, ≤64 chars).
_WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


_RENDERED_FILES = ("main.nf", "nextflow.config", "launcher.sh")

# Where the rendered workflow files are staged locally before upload. This
# MUST live under a Globus-accessible location for globus-transport envs:
# Globus Connect Personal only scans its Accessible Folders (default $HOME)
# and REFUSES a system temp dir like macOS's /var/folders or /tmp (surfaced
# by the live acceptance run). The repo sits under $HOME, so a repo-local
# staging dir works for BOTH transports (scp doesn't care where the source
# is) and keeps everything self-contained in the project tree per the user's
# rails. TemporaryDirectory still auto-cleans each run.
_RENDER_STAGE_DIR = Path(__file__).resolve().parents[2] / "data" / "cluster_render_staging"


# Terminal SLURM states — once we hit one of these the job is over.
#: Alias of the one definition, in `cluster_jobs` alongside the sacct parser
#: that produces the rows. It was a second literal copy here — and the copy in
#: the L14 surface test asserts set-equality against it, so the two had to be
#: edited in lockstep and the test could only prove the set had not changed,
#: never that it was right. Same shape as the invariant roster: the poller that
#: consumes the set is not its owner.
_TERMINAL_STATES = cluster_jobs.TERMINAL_STATES


def absolutize_download_dir(download_local_dir: str) -> Path:
    """Where downloaded outputs land, always as an absolute path.

    Two separate things break on a relative one, which is why this is not merely tidy:

      * `detected_outputs` must be absolute or I6 refuses the seal;
      * Globus resolves a local destination against its ENDPOINT ROOT, not the process
        CWD, so a relative path records an output somewhere other than where Globus
        actually delivered it — a recorded path that points at nothing.

    `resolve()` is lexical for a directory that does not exist yet, which is the normal
    case here: the caller mkdirs it immediately after.

    Public and named rather than inline because the test for it used to read this
    module's source and assert the literal string
    `Path(download_local_dir).expanduser().resolve()` appeared somewhere in it. That
    passes if the expression sits in a comment and fails if someone splits the line —
    it pins the spelling, not the behaviour. Callable, it can be asked the question
    that actually matters.
    """
    return Path(download_local_dir).expanduser().resolve()


def _parse_exit_code(s: str) -> int:
    """SLURM's ExitCode is `<rc>:<signal>`. Take the rc."""
    if not isinstance(s, str) or ":" not in s:
        return -1
    rc, _, _ = s.partition(":")
    try:
        return int(rc)
    except (ValueError, TypeError):
        return -1


# The SLURM placement keys worth sealing for reproducibility — a reader needs
# these to resubmit the job on the same queue with the same limits. We record a
# whitelist (not the whole dict) so a stray/injected key can't ride into the
# sealed WorkflowSpec.
_SLURM_CONTEXT_KEYS = (
    "account", "partition", "qos",
    "time", "mem", "mem_per_cpu", "cpus", "cpus_per_task",
    "nodes", "ntasks", "gpus", "gres", "constraint",
)


def _seal_slurm_context(slurm: Optional[Mapping]) -> Optional[dict]:
    """Filter the request's SLURM block to the reproducibility-relevant
    placement keys. Returns None when nothing relevant is present (so the
    field is omitted rather than sealing an empty dict)."""
    if not isinstance(slurm, Mapping):
        return None
    ctx = {k: slurm[k] for k in _SLURM_CONTEXT_KEYS
           if k in slurm and slurm[k] not in (None, "")}
    return ctx or None


def _record_failed_cluster_step(
        _pipeline_state, pipeline_id, *,
        tool_name, command, inputs, failure_code, error,
        job_id=None, workflow_dir=None, sif_path_remote=None,
        image_digest=None, cluster_sif_sha256=None, extra=None) -> Optional[int]:
    """Record a FAILED pipeline_step so a cluster attempt that dies mid-flight
    (sbatch rejected, poll query errored, or poll timed out) leaves a durable
    trace in the draft instead of VANISHING. This is the honesty fix for the
    three former `vanished` terminals: a failure the system cannot see is worse
    than one it records loudly.

    Two deliberate shape choices:
      - `returncode = -1` marks the step failed, so seal's rc=0-gated
        invariants (I3 outputs, I7 resource_usage) correctly SKIP it — a step
        that never ran can't be asked to have produced validated outputs.
      - the attempted inputs are recorded under `attempted_inputs`, NOT
        `inputs`. I8 (composition_coherence) walks `inputs` to build the
        data-flow graph regardless of returncode; a step that never consumed
        its inputs must NOT become a node in that graph (it would demand
        provenance for a consumption that didn't happen and could block a later
        seal of a retried run). `attempted_inputs` keeps the forensic record
        without asserting graph membership.

    Returns the step_index (or None if the draft is gone)."""
    step_data = {
        "tool":             tool_name or (command.split() or [""])[0],
        "purpose":          f"cluster run of {tool_name or 'tool'} — FAILED "
                            f"({failure_code})",
        "command":          command,
        "returncode":       -1,
        "attempted_inputs": [{"path": p} for p in inputs.values()],
        "detected_outputs": [],
        "validation_locus": "cluster",
        "failure_code":     failure_code,
        "failure_error":    error,
        "cluster_job_id":         job_id,
        "cluster_workflow_dir":   workflow_dir,
        "container_image":        sif_path_remote,
        "container_image_digest": image_digest,
        "cluster_sif_sha256":     cluster_sif_sha256,
    }
    if extra:
        step_data.update(extra)
    step_data = {k: v for k, v in step_data.items() if v is not None}
    return _pipeline_state.add_step(pipeline_id, step_data)


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
        return refused("run_cluster.pipeline_id_required",
                       error="pipeline_id is required for run_step_on_cluster")
    if not workflow_name or not _WORKFLOW_NAME_RE.match(workflow_name):
        return refused("run_cluster.bad_workflow_name",
            error=f"workflow_name must match {_WORKFLOW_NAME_RE.pattern!r} "
            f"(it becomes a path component under scratch); got "
            f"{workflow_name!r}")

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
        return refused("run_cluster.config_error",
                       error=f"{type(e).__name__}: {e}")

    env_type = env.get("type")
    if env_type != "ssh":
        return refused("run_cluster.not_ssh_env",
            error=f"run_step_on_cluster only supports ssh compute envs; "
            f"got type={env_type!r} on env {compute_env_name!r}")

    scratch_target = compute_access.get_agent_scratch_target(env)
    if scratch_target is None:
        return refused("run_cluster.no_scratch_target",
            error=f"compute_env {compute_env_name!r} has no "
            f"agent_scratch_target declared — run_step_on_cluster "
            f"can only run inside the agent's scratch sandbox. Add an "
            f"agent_scratch_target block on this env in "
            f"projects_access.yaml.")

    # Auth: project must have access to env; scratch target must advertise
    # `exec` (required for the SLURM job to write its outputs in-place).
    try:
        compute_access.check_env_target_capability(
            project, compute_env_name, scratch_target,
            "run_step_on_cluster", "agent_scratch_target")
    except compute_access.PermissionDenied as e:
        return refused("run_cluster.permission_denied",
                       error=f"PermissionDenied: {e}")

    scratch_root = scratch_target.get("path", "").rstrip("/")
    workflow_dir = f"{scratch_root}/{project_name}/{workflow_name}"

    # ─── 0b. Loud input precondition (Flow fix) ───────────────────────
    # run_step_on_cluster uploads ONLY the 3 rendered workflow files — it
    # does NOT stage input DATA (unlike run_step_in_container). A declared
    # input that isn't already on the cluster would fail deep inside the
    # Nextflow run after a wasted SLURM submission. Check existence up front
    # (one ssh hop) and fail with the offending path BEFORE any cluster
    # mutation. No auto-staging — the user's rails decide where data lives.
    pre = cluster_jobs.remote_paths_exist(env, [str(p) for p in inputs.values()])
    if "error" in pre:
        return refused("run_cluster.input_missing",
                error=pre["error"],
                **({"missing_paths": pre["missing_paths"]}
                   if "missing_paths" in pre else {}))

    # ─── 1. Stage the .sif ────────────────────────────────────────────
    stage = stage_apptainer.stage_apptainer_image(
        project_name=project_name,
        compute_env_name=compute_env_name,
        freeze_request_key=freeze_request_key,
        sif_subpath=sif_subpath or "",
        access_path=access_path)
    if "error" in stage:
        return broke("run_cluster.stage_failed",
                error=f"stage_apptainer_image failed: {stage['error']}",
                stage_result=stage)

    sif_path_remote = stage["sif_path"]
    image_digest = stage.get("image_digest", "")

    # ─── 1b. Fingerprint the .sif THAT WILL RUN (C2 round-trip) ───────
    # The EnvCache image_digest above is NOMINAL — copied from the freeze
    # record, never observed on the cluster. Look at the actual artifact:
    # sha256 of the .sif (the exact bytes apptainer will exec) + inspect
    # provenance. Two strengths of verification:
    #   STRONG  — the .sif's apptainer-inspect labels carry a source digest
    #             (`…deffile.from = repo@sha256:…`, as biocontainer/adopt .sifs
    #             do). We match it against the EnvCache's pinned image_digest:
    #             a cryptographic tie that the .sif on the cluster was built
    #             from the exact image we froze. A MISMATCH withholds the badge.
    #   OBSERVED— no comparable digest in the labels (build_archive from a
    #             local docker-archive). Fall back to: we got a real sha256 AND
    #             apptainer could inspect it as a valid image at the exec path.
    # Either way the badge rests on a real cluster-side observation, never on a
    # digest we merely echoed back to ourselves.
    sif_probe = stage_apptainer.inspect_staged_sif(env, sif_path_remote)
    cluster_sif_sha256 = None
    cluster_image_verified = False
    cluster_image_digest_match = None
    if "error" not in sif_probe:
        cluster_sif_sha256 = sif_probe.get("sif_sha256")
        src_digests = stage_apptainer._extract_source_digests(
            sif_probe.get("inspect"))
        if src_digests and image_digest:
            cluster_image_digest_match = image_digest in src_digests
            cluster_image_verified = bool(
                cluster_sif_sha256 and cluster_image_digest_match)
        else:
            cluster_image_verified = bool(
                cluster_sif_sha256 and sif_probe.get("apptainer_inspect_ok"))

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
            env=env,
        )
    except ValueError as e:
        return refused("run_cluster.render_failed",
                error=f"workflow_render failed: {e}",
                stage_result=stage)

    files_uploaded: list[str] = []
    _RENDER_STAGE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bioinf_cluster_step_",
                                     dir=str(_RENDER_STAGE_DIR)) as td:
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
                return broke("run_cluster.upload_failed",
                    error=f"upload of {fname} to scratch failed: {up['error']}",
                    stage_result=stage,
                    files_uploaded=files_uploaded)
            files_uploaded.append(up["remote_abs_path"])

    sb = submit_workflow.sbatch_via_ssh(env, workflow_dir, timeout=300)
    if "error" in sb:
        # RECORDED (was VANISHED): the SLURM submission failed. Record a failed
        # pipeline_step BEFORE returning so this attempt leaves a durable trace
        # in the draft — the files were uploaded but no job started.
        step_index = _record_failed_cluster_step(
            _pipeline_state, pipeline_id,
            tool_name=tool_name, command=command, inputs=inputs,
            failure_code="run_cluster.sbatch_failed",
            error=f"sbatch failed: {sb['error']}",
            workflow_dir=workflow_dir,
            sif_path_remote=sif_path_remote, image_digest=image_digest,
            cluster_sif_sha256=cluster_sif_sha256)
        return broke("run_cluster.sbatch_failed",
                error=f"sbatch failed: {sb['error']}",
                stage_result=stage,
                files_uploaded=files_uploaded,
                sbatch_result=sb,
                pipeline_merge={"status": "recorded_failed",
                                "pipeline_id": pipeline_id,
                                "step_index": step_index})

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
            # RECORDED (was VANISHED — the worst hole): the job was submitted
            # (job_id in hand) but the poll query errored. Record a failed step
            # carrying the job_id so the possibly-still-running SLURM job is
            # traceable (a reader can sacct it) instead of leaving zero trace.
            step_index = _record_failed_cluster_step(
                _pipeline_state, pipeline_id,
                tool_name=tool_name, command=command, inputs=inputs,
                failure_code="run_cluster.poll_status_failed",
                error=f"cluster_job_status failed during poll: {s['error']}",
                job_id=job_id, workflow_dir=workflow_dir,
                sif_path_remote=sif_path_remote, image_digest=image_digest,
                cluster_sif_sha256=cluster_sif_sha256)
            return broke("run_cluster.poll_status_failed",
                error=f"cluster_job_status failed during poll: {s['error']}",
                stage_result=stage, job_id=job_id,
                workflow_dir=workflow_dir, last_poll=s,
                pipeline_merge={"status": "recorded_failed",
                                "pipeline_id": pipeline_id,
                                "step_index": step_index})
        if s.get("jobs"):
            row = s["jobs"][0]
            # normalize_state, not a bare `in`: sacct writes `CANCELLED by 12345`,
            # which never matched the undecorated name — so the one death a user
            # causes on purpose polled until the timeout and was reported as a
            # poll_timeout rather than as the cancellation it was.
            if cluster_jobs.normalize_state(row.get("state")) in _TERMINAL_STATES:
                final_status = row
                break
        time.sleep(poll_interval)

    if final_status is None:
        # RECORDED (was VANISHED): the job was submitted but never reached a
        # terminal state within the poll cap. Record a failed step carrying the
        # job_id so the (possibly still-running) job is traceable — the caller
        # can sacct it or cancel it rather than losing the handle.
        step_index = _record_failed_cluster_step(
            _pipeline_state, pipeline_id,
            tool_name=tool_name, command=command, inputs=inputs,
            failure_code="run_cluster.poll_timeout",
            error=f"polling timed out after {max_polls * poll_interval}s — "
            f"job_id={job_id} did not reach a terminal state.",
            job_id=job_id, workflow_dir=workflow_dir,
            sif_path_remote=sif_path_remote, image_digest=image_digest,
            cluster_sif_sha256=cluster_sif_sha256)
        return broke("run_cluster.poll_timeout",
            error=f"polling timed out after "
            f"{max_polls * poll_interval}s — job_id={job_id} did "
            f"not reach a terminal state.",
            stage_result=stage, job_id=job_id,
            workflow_dir=workflow_dir,
            pipeline_merge={"status": "recorded_failed",
                            "pipeline_id": pipeline_id,
                            "step_index": step_index})

    rc = _parse_exit_code(final_status.get("exit_code", ""))

    # ─── 3b. Did the job actually SUCCEED? ────────────────────────────
    # The State decides, not the exit code — see cluster_jobs.classify_sacct_row
    # for the six death shapes that reached this line with rc=0 and proceeded to
    # download, validate and record a green step. A scheduler-killed job dies
    # mid-write, so the danger is not the missing output (the download loop
    # already fails loudly on that, and I3 refuses a step with none) but the
    # TRUNCATED one: a BAM cut off by a wall-clock kill exists, is non-empty,
    # and passes validation.
    verdict, why = cluster_jobs.classify_sacct_row(final_status)
    if verdict == cluster_jobs.DIED:
        step_index = _record_failed_cluster_step(
            _pipeline_state, pipeline_id,
            tool_name=tool_name, command=command, inputs=inputs,
            failure_code="run_cluster.job_died",
            error=f"the cluster job did not succeed: {why}",
            job_id=job_id, workflow_dir=workflow_dir,
            sif_path_remote=sif_path_remote, image_digest=image_digest,
            cluster_sif_sha256=cluster_sif_sha256,
            # Forensics for whoever reads the draft later. The .out/.err land in
            # workflow_dir because sbatch is run from there and --job-name is the
            # workflow name, so `%x-%j` resolves to exactly this pair.
            extra={"cluster_job_state": cluster_jobs.normalize_state(
                       final_status.get("state")),
                   "cluster_exit_code": final_status.get("exit_code"),
                   "cluster_sacct_reason": final_status.get("reason"),
                   "cluster_stdout_log": f"{workflow_dir}/{workflow_name}-{job_id}.out",
                   "cluster_stderr_log": f"{workflow_dir}/{workflow_name}-{job_id}.err"})
        return broke("run_cluster.job_died",
            error=f"the cluster job did not succeed: {why}",
            stage_result=stage, job_id=job_id, workflow_dir=workflow_dir,
            job_state=cluster_jobs.normalize_state(final_status.get("state")),
            exit_code=final_status.get("exit_code"),
            sacct_reason=final_status.get("reason"),
            diagnose={
                "stderr": f"{workflow_dir}/{workflow_name}-{job_id}.err",
                "stdout": f"{workflow_dir}/{workflow_name}-{job_id}.out",
                "sacct":  f"call cluster_job_status(job_id={job_id!r}) for the raw row",
            },
            pipeline_merge={"status": "recorded_failed",
                            "pipeline_id": pipeline_id,
                            "step_index": step_index})

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
            # sacct measured THIS job on the node that ran it — native by
            # construction. This was the one producer not stamping authority
            # (sea-trial F20), so the corpus's only genuinely budgetable
            # numbers rendered under "unknown authority" with a
            # recorded-before-capture explanation that was false for them.
            "i7_authoritative": True,
        }
        # A successful sacct QUERY can still carry no MaxRSS (cluster without cgroup
        # memory accounting). cluster_job_resources marks that with a sacct_error;
        # propagate it so I7 sees it — dropping it here would have let the placeholder
        # zero seal as an observation (audit 2026-07-16).
        if resources.get("sacct_error"):
            resource_usage["sacct_error"] = resources["sacct_error"]

    # ─── 5. Download outputs back local (from scratch) ────────────────
    download_dir = absolutize_download_dir(download_local_dir)
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
        # C2 — the REAL round-trip: the sha256 of the .sif that actually ran on
        # the cluster (observed, not copied) + whether its inspect-label source
        # digest matched the EnvCache's pinned image (cryptographic tie, when
        # available). cluster_image_verified is what the shipped-image badge
        # rests on. cluster_image_digest_match is None when the .sif carried no
        # comparable digest (build_archive) — then verification is observed-only.
        "cluster_sif_sha256":       cluster_sif_sha256,
        "cluster_image_verified":   cluster_image_verified,
        "cluster_image_digest_match": cluster_image_digest_match,
        # Layer-2 HPC-locus metadata (new fields, downstream-tolerant)
        # WHERE THE OUTPUTS LIVE AT THE LOCUS THAT MADE THEM. detected_outputs
        # above are the DOWNLOADED LOCAL copies — correct for validation, since
        # those are the bytes we hashed and type-checked — but a second cluster
        # step consumes the REMOTE originals, and no local path can ever match
        # one. Without this a multi-step cluster chain is unsealable: step 2's
        # input traces to nothing, and the honest options are to fake a lineage
        # or to give up on sealing. Recording the truth is neither.
        #
        # Declared from `outputs`, which is what the job was TOLD to write and
        # what the download loop just read back — not a guess about the remote
        # filesystem. Empty when the step declared no outputs, so a step that
        # produced nothing gains no phantom provenance.
        "remote_outputs":         [f"{workflow_dir}/{fn}"
                                   for fn in outputs.values()] or None,
        "validation_locus":       "cluster",
        "cluster_job_id":         job_id,
        "cluster_workflow_dir":   workflow_dir,
        "cluster_node":           final_status.get("nodelist"),
        "cluster_state":          final_status.get("state"),
        "cluster_exit_code":      final_status.get("exit_code"),
        # The VERDICT, stated — not left for a reader to re-derive from the two
        # fields above. Re-deriving it is exactly the defect this fix removes:
        # `TIMEOUT` + `0:0` reads as success to anyone who looks at the exit code
        # first, and every reader that needs this answer would have to get the
        # State-before-rc precedence right independently. The producer captures;
        # the reader must not scrape.
        "cluster_job_verdict":    verdict,
        # Repro (cluster context) — the run environment a reader needs to
        # reproduce the job: the modules loaded + the SLURM placement. Recorded
        # from the request (account/partition/qos) + the observed node above.
        "cluster_apptainer_module": apptainer_module or None,
        "cluster_nextflow_module":  nextflow_module or None,
        "cluster_slurm":            _seal_slurm_context(slurm),
        # The submission AS RENDERED — the literal launcher.sh / main.nf /
        # nextflow.config this step uploaded and sbatch'd. Captured at submit
        # time because a render-time reconstruction is a claim, not a record
        # (the I4-transcript rule): it would differ from the truth exactly
        # where that is easy to get wrong. The RUN dashboard shows these so
        # "review all the parameters before running on real data" includes the
        # #SBATCH header and the params block that actually ran — and so a
        # reader can edit them and resubmit variants by hand.
        "cluster_rendered_files":   {fn: rendered[fn] for fn in _RENDERED_FILES
                                     if isinstance(rendered.get(fn), str)},
    }
    step_data = {k: v for k, v in step_data.items() if v is not None}
    step_index = _pipeline_state.add_step(pipeline_id, step_data)

    # ─── 8. Validate outputs (type-aware, same as local steps) ────────
    validations: dict = {}
    output_types_used: set = set()
    if rc == 0 and step_index is not None:
        for path in downloaded:
            basename = Path(path).name
            ext = "".join(Path(path).suffixes).lower()
            etype = None
            for key in (basename, ext, ext.lstrip(".")):
                if key in output_types:
                    etype = output_types[key]
                    output_types_used.add(key)
                    break
            if etype is None:
                etype = infer_validator_type(basename)
            v = _validator.validate(path, etype)
            # keyed by resolved PATH (see pipeline_state.validation_key) — a per-sample
            # cluster fan-out is precisely where two outputs share a basename.
            validations[_validation_key(path)] = v
            _pipeline_state.add_validation(pipeline_id, step_index, path, v)
    # Same disclosure as run_pipeline_step's: a key that bound to nothing is a
    # typo, a wrong extension, or a file the job never produced — and silently
    # dropping the caller's stated intent is how a wrong fallback takes over
    # unannounced (sea-trial F18: a placeholder-keyed dict was ignored without
    # a word while the joined-suffix fallback failed a valid BAM as text).
    unmatched = sorted(set(output_types) - output_types_used)

    return proven(
        "run_cluster.step_recorded",
        success=(rc == 0 and not download_errors),
        output_types_unmatched=unmatched or None,
        returncode=rc,
        job_id=job_id,
        sif_path=sif_path_remote,
        cluster_sif_sha256=cluster_sif_sha256,
        cluster_image_verified=cluster_image_verified,
        cluster_image_digest_match=cluster_image_digest_match,
        workflow_dir=workflow_dir,
        resource_usage=resource_usage,
        detected_outputs=downloaded,
        output_sha256=output_sha256,
        validations=validations,
        validation_count=len(validations),
        download_errors=download_errors,
        pipeline_merge={
            "status":      "merged",
            "pipeline_id": pipeline_id,
            "step_index":  step_index,
        },
        final_status=final_status,
    )


# Filename → type inference is agent.validators.output_validator.infer_validator_type,
# imported at the top. A second copy lived here ("the same idea" as run_tools's, per its
# own docstring) and drifted from it on the joined-vs-last suffix reading — sea-trial
# F18, a valid `x.sorted.bam` failed as text on the cluster path only. One reading now.
