"""acquire_data — reference-data acquisition, local-default or cluster-on-direction.

The generalized data layer that hooks into the rest of the system: "get this
reference data to where it needs to live." Two loci:

  LOCAL  (default) — download to the agent machine. Handled by the existing
                     download_reference_database path (curl + sha256 sidecar).

  CLUSTER (directed) — render a SIMPLE resumable SLURM shell script, push it to
                     the env's common_data zone, and `sbatch` it so the COMPUTE
                     node does the pulling (the head node never sees the bytes).
                     Submit-and-document: a multi-hour gnomAD/VEP-cache download
                     would blow the agent's ~10-min stream watchdog, so we return
                     the job_id + a manifest immediately; the caller polls with
                     cluster_job_status and seals once it's done.

Why a plain shell/SLURM script (NOT the nextflow+apptainer workflow renderer):
a download needs `wget`/`curl`, not the tool's container (which may not even
have curl). So this is deliberately the simpler renderer — a #SBATCH header
(the same controlled convention as workflow_render) + a resumable fetch + a
sha256 sidecar. Resumability is two-layered: `wget -c` / `curl -C -` inside the
script (survives a walltime kill) AND the script is idempotent, so a re-submit
just picks up where it left off.

The seal side (locus-aware):
  - `_refresh_reference_databases` (workflow_tools) ssh-derives available /
    size_bytes / sha256 for cluster entries from the cluster, at seal.
  - I5 (`spec_writer._check_reference_database_availability`) verifies a cluster
    entry over ssh (existence + non-empty) instead of a local re-hash — the same
    "trust an on-cluster observation, not a local one" move the C2 .sif
    round-trip uses. A cluster DB that isn't actually there fails the seal.

The reference_databases[] record stores the CLUSTER path in `local_path` (so the
I8 composition walk's path/basename match works unchanged) plus `locus:
"cluster"` + `compute_env` so the seal side knows to verify over ssh.
"""
from __future__ import annotations

import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from agent.skills import compute_access, transfer, submit_workflow, workflow_render
from agent.skills.outcomes import proven, refused, broke


# Where the rendered SLURM download script is staged locally before upload. MUST
# live under a Globus-accessible location ($HOME): Globus Connect Personal only
# scans its Accessible Folders and refuses a system temp dir like /var/folders.
# The repo sits under $HOME. Mirrors submit_workflow._RENDER_STAGE_DIR and
# run_cluster_step._RENDER_STAGE_DIR (both surfaced by real cluster runs).
_DL_STAGE_DIR = Path(__file__).resolve().parents[2] / "data" / "acquire_render_staging"

# The SLURM download script's fixed filename in the acquisition dir (sbatch_via_ssh
# expects `launcher.sh` in the workflow_dir).
_LAUNCHER_NAME = "launcher.sh"

# Default per-job resources for a download: generous walltime (big DBs take
# hours; a walltime kill just resumes on re-submit), minimal cpu/mem (I/O-bound).
_DEFAULT_DL_SLURM = {"time": "1-00:00:00", "mem": "4g"}

# ssh probe timeout for the seal-side cluster verification.
_PROBE_TIMEOUT = 120


# ---------------------------------------------------------------------------
# The simple SLURM download-script renderer
# ---------------------------------------------------------------------------

def render_download_script(*, name: str, url: str, dest_filename: str,
                           extract: bool, slurm_v: dict, email: str) -> str:
    """Render the resumable SLURM download script as a string. Pure: strings in,
    string out (filesystem writes + sbatch live in acquire_to_cluster).

    The script cd's into $SLURM_SUBMIT_DIR (the acquisition dir the launcher was
    uploaded into — see the SLURM $0-staging trap handled the same way in
    workflow_render), fetches `url` resumably, writes a `<dest>.source.sha256`
    sidecar (the reproducibility anchor — the bytes the URL served), and, when
    `extract`, unpacks a .zip/.tar.gz/.tar in place. Every value is validated by
    the caller before it reaches here."""
    q_url = shlex.quote(url)
    q_dest = shlex.quote(dest_filename)
    sidecar = f"{dest_filename}.source.sha256"
    q_sidecar = shlex.quote(sidecar)

    # Resumable fetch: prefer wget -c, fall back to curl -C -. Both continue a
    # partial file, so a walltime-killed job resumes cleanly on re-submit.
    fetch = (
        f"if command -v wget >/dev/null 2>&1; then\n"
        f"  wget -c -O {q_dest} {q_url}\n"
        f"else\n"
        f"  curl -L --fail -C - -o {q_dest} {q_url}\n"
        f"fi\n"
    )
    # sha256 of the artifact the URL served (portable: sha256sum | shasum).
    hasher = (
        f"( sha256sum {q_dest} 2>/dev/null || shasum -a 256 {q_dest} ) "
        f"| awk '{{print $1}}' > {q_sidecar}\n"
    )
    # Optional extract in place. The archive is kept (the sidecar hashes it and
    # it is the resumable anchor); disk is cheap on scratch/common_data.
    extract_block = ""
    if extract:
        low = dest_filename.lower()
        if low.endswith(".zip"):
            extract_block = f"unzip -o {q_dest}\n"
        elif low.endswith((".tar.gz", ".tgz")):
            extract_block = f"tar -xzf {q_dest}\n"
        elif low.endswith(".tar"):
            extract_block = f"tar -xf {q_dest}\n"

    return (
        f"#!/usr/bin/env bash\n"
        + workflow_render._render_sbatch_header(f"acquire_{name}", slurm_v, email)
        + f"\n"
        f"set -euo pipefail\n"
        f"\n"
        f"# Generated by acquire_data.render_download_script — DO NOT hand-edit.\n"
        f"# Resumable reference-data download. Runs on a COMPUTE node (sbatch) so\n"
        f"# the head node never moves the bytes. Re-submit to resume a partial\n"
        f"# download (wget -c / curl -C - continue in place).\n"
        f"\n"
        f"# SLURM stages the script into /var/spool/slurmd/...; $SLURM_SUBMIT_DIR\n"
        f"# is the dir we `sbatch`ed from (the acquisition dir the files live in).\n"
        f'cd "${{SLURM_SUBMIT_DIR:-$(dirname "$(readlink -f "$0")")}}"\n'
        f"\n"
        f"{fetch}"
        f"{hasher}"
        f"{extract_block}"
        f'echo "acquire_{name}: DONE"\n'
    )


def render_recipe_runner_script(*, name: str, recipe_filename: str,
                                mask_tools: tuple, slurm_v: dict,
                                email: str) -> str:
    """Render a SLURM wrapper that RUNS THE TOOL'S OWN data-acquisition recipe
    (a `gather_files.sh` / `download_*.sh` the tool ships) on a compute node, then
    captures per-file sha256 provenance.

    We do NOT re-implement the recipe (that silently drifts from the tool's
    pipeline — a tool's script also does decompression / renames / format
    conversion that a transcribed URL list misses). Our value-add is the wrapper:
    run it head-node-safe on a compute node, force its NON-INTERACTIVE path, and
    layer on the sha256 + version provenance the recipe doesn't record itself.

    `mask_tools` are interactive-only tools hidden from the recipe's PATH so it
    takes its batch path — e.g. tmux: a download-manager recipe re-execs into a
    DETACHED tmux server that SLURM kills at job end; masking tmux makes the
    recipe's own `[ -z $TMX ]` branch run the downloads inline (+ `wait`)."""
    q_recipe = shlex.quote(recipe_filename)
    mask = " ".join(mask_tools or ())
    q_mask = shlex.quote(mask)

    # Force the recipe's non-interactive path: mirror the full PATH into a shim
    # dir MINUS the masked tools (recipe-agnostic — nothing legitimate is lost,
    # only the interactive orchestrators the batch context can't support).
    mask_block = ""
    if mask:
        mask_block = (
            f'RECIPE_PATH="$PATH"\n'
            f'SHIM="$(mktemp -d)"\n'
            f'for d in $(printf "%s" "$PATH" | tr ":" " "); do\n'
            f'  [ -d "$d" ] || continue\n'
            f'  for f in "$d"/*; do\n'
            f'    b="$(basename "$f")"\n'
            f'    case " {mask} " in *" $b "*) continue;; esac\n'
            f'    [ -e "$SHIM/$b" ] || ln -s "$f" "$SHIM/$b" 2>/dev/null\n'
            f'  done\n'
            f'done\n'
            f'RECIPE_PATH="$SHIM"\n'
            f'echo "[acquire] masked from recipe PATH: {mask}"\n'
        )
    run_line = (
        f'PATH="$RECIPE_PATH" bash {q_recipe}\n' if mask
        else f'bash {q_recipe}\n'
    )

    return (
        f"#!/usr/bin/env bash\n"
        + workflow_render._render_sbatch_header(f"acquire_{name}", slurm_v, email)
        + f"\n"
        f"set -uo pipefail   # NOT -e: the recipe manages its own per-file resume\n"
        f"\n"
        f"# Generated by acquire_data.render_recipe_runner_script — DO NOT hand-edit.\n"
        f"# Runs the TOOL'S OWN data-acquisition recipe ({recipe_filename}) on a\n"
        f"# COMPUTE node (head node never moves the bytes), then hashes every\n"
        f"# produced file for provenance. Re-submit to resume (the recipe skips\n"
        f"# files already present).\n"
        f"\n"
        f'cd "${{SLURM_SUBMIT_DIR:-$(dirname "$(readlink -f "$0")")}}"\n'
        f"\n"
        f"{mask_block}"
        f'echo "[acquire] running recipe: {recipe_filename}"\n'
        f"{run_line}"
        f'recipe_rc=$?\n'
        f'echo "[acquire] recipe exited rc=$recipe_rc"\n'
        f"\n"
        f"# Provenance sweep (recipe-agnostic): sha256 every FINAL produced file.\n"
        f"# The recipe doesn't write checksums; this is our reproducibility anchor.\n"
        f'echo "[acquire] hashing produced files for provenance..."\n'
        f'for f in *; do\n'
        f'  [ -f "$f" ] || continue\n'
        f'  case "$f" in *.source.sha256|*.part|launcher.sh|{q_recipe}) continue;; esac\n'
        f'  ( sha256sum "$f" 2>/dev/null || shasum -a 256 "$f" ) '
        f"| awk '{{print $1}}' > \"$f.source.sha256\"\n"
        f'done\n'
        f"\n"
        f'echo "acquire_{name}: recipe DONE (rc=$recipe_rc)"\n'
        f'exit $recipe_rc\n'
    )


# ---------------------------------------------------------------------------
# The cluster acquisition orchestrator
# ---------------------------------------------------------------------------

def acquire_to_cluster(*, name: str, url: str, compute_env: str,
                       remote_dir: str = "", slurm: Optional[Mapping] = None,
                       version: str = "", description: str = "",
                       extract: bool = True, pipeline_id: str = "",
                       access_path: Optional[str] = None,
                       _pipeline_state=None) -> dict:
    """Render + upload + sbatch a resumable download job on `compute_env`; record
    a cluster-locus reference_databases entry. Submit-and-document — returns the
    job_id immediately (the download runs for as long as it takes; poll with
    cluster_job_status, seal once COMPLETED).

    Lands in the env's common_data zone by default (`<common_data>/<name>/`), the
    designated shared-reference zone. `remote_dir` overrides but MUST stay under
    common_data (the acquisition primitive is scoped to that zone; project-path
    downloads are a later addition if needed)."""
    if not name or not compute_access._is_safe_token(name):
        return refused("acquire.bad_name",
            error=f"name {name!r} must be a safe token (alnum + '_-', <=64 chars) "
                  f"— it becomes a path component under common_data")
    if not (url or "").strip():
        return refused("acquire.no_url", error="url is required")

    if _pipeline_state is None:
        from agent import mcp_server as _ms
        _pipeline_state = _ms._pipeline_state

    # ─── Resolve env + common_data zone ────────────────────────────────
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        env = compute_access.get_compute_env(compute_env, access)
    except (compute_access.ConfigError, FileNotFoundError, KeyError, ValueError) as e:
        return refused("acquire.config_error", error=f"{type(e).__name__}: {e}")

    if env.get("type") != "ssh":
        return refused("acquire.not_ssh_env",
            error=f"cluster acquisition needs an ssh compute env; {compute_env!r} "
                  f"is type={env.get('type')!r}. For a local download omit compute_env.")

    common = compute_access.get_agent_common_data_target(env)
    if common is None:
        return refused("acquire.no_common_data",
            error=f"compute_env {compute_env!r} declares no agent_common_data_target "
                  f"— cluster reference data lands in the common_data zone. Add that "
                  f"block (with exec) to this env in projects_access.yaml.")
    common_root = (common.get("path") or "").rstrip("/")

    # Default acquisition dir under common_data; an explicit remote_dir must stay
    # inside common_data (transfer.upload re-checks the zone + exec at upload).
    acq_dir = (remote_dir or f"{common_root}/{name}").rstrip("/")
    if not transfer._under(common_root, acq_dir):
        return refused("acquire.remote_dir_outside_common_data",
            error=f"remote_dir {acq_dir!r} is not under the env's common_data zone "
                  f"{common_root!r}. V1 cluster acquisition targets common_data only.")

    dest_filename = Path(url.split("?")[0]).name or f"{name}.download"
    # The recorded DB path: the extracted dir when unpacking an archive, else the
    # downloaded file itself.
    low = dest_filename.lower()
    is_archive = low.endswith((".zip", ".tar.gz", ".tgz", ".tar"))
    db_path = acq_dir if (extract and is_archive) else f"{acq_dir}/{dest_filename}"

    # ─── SLURM header (merge env policy + email, then validate + default) ──
    # _resolve_slurm_and_email merges the HPC policy (account/partition) but does
    # NOT apply the controlled-vocab defaults; _check_slurm does that (and raises
    # on a bad request) so _render_sbatch_header sees ntasks/cpus/gpus filled in.
    try:
        merged, email = submit_workflow._resolve_slurm_and_email(
            dict(slurm) if slurm else dict(_DEFAULT_DL_SLURM), env)
        slurm_v = workflow_render._check_slurm(merged)
    except ValueError as e:
        return refused("acquire.bad_slurm", error=f"slurm request invalid: {e}")

    # ─── Render locally into the Globus-accessible staging dir ─────────
    try:
        script = render_download_script(
            name=name, url=url, dest_filename=dest_filename,
            extract=extract, slurm_v=slurm_v, email=email)
    except ValueError as e:
        return refused("acquire.render_failed", error=f"script render failed: {e}")

    _DL_STAGE_DIR.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="bioinf_acquire_",
                                     dir=str(_DL_STAGE_DIR)) as td:
        local_launcher = Path(td) / _LAUNCHER_NAME
        local_launcher.write_text(script)

        # Upload the launcher into the acquisition dir (common_data zone). Globus
        # auto-creates the acq_dir leaf; scp mkdir -p's it — either way under the
        # agent's own common_data zone (env-implicit grant), not user directories[].
        up = transfer.upload(
            project_name=transfer.AD_HOC_PROJECT_NAME,
            compute_env_name=compute_env,
            local_path=str(local_launcher),
            remote_abs_path=f"{acq_dir}/{_LAUNCHER_NAME}",
            access_path=str(Path(access_path)) if access_path else None,
            timeout=600)
        if "error" in up:
            return broke("acquire.upload_failed",
                error=f"upload of the download script to {acq_dir} failed: {up['error']}",
                acq_dir=acq_dir)

    # ─── sbatch the download job (compute node does the pull) ──────────
    sb = submit_workflow.sbatch_via_ssh(env, acq_dir, timeout=300)
    if "error" in sb:
        return {**sb, "acq_dir": acq_dir}
    job_id = sb["job_id"]

    # ─── Record the cluster-locus reference_databases entry ────────────
    rdb = {
        "name":        name,
        "version":     version or "unknown",
        "source_url":  url,
        "local_path":  db_path,       # the CLUSTER path (I8 walks this field)
        "locus":       "cluster",
        "compute_env": compute_env,
        "available":   False,          # ssh-derived at seal (_refresh)
        "description": description or None,
    }
    merged_into = None
    if pipeline_id:
        draft = _pipeline_state.get_draft(pipeline_id) or {}
        existing = list(draft.get("reference_databases") or [])
        replaced = False
        for i, e in enumerate(existing):
            if isinstance(e, dict) and e.get("name") == name:
                existing[i] = {**e, **{k: v for k, v in rdb.items() if v is not None}}
                replaced = True
                break
        if not replaced:
            existing.append({k: v for k, v in rdb.items() if v is not None})
        _pipeline_state.patch(pipeline_id, {"reference_databases": existing})
        merged_into = pipeline_id

    # ─── Submit-and-document: a findable manifest for the download job ──
    manifest = {
        "kind":          "reference_data_acquisition",
        "name":          name,
        "compute_env":   compute_env,
        "job_id":        job_id,
        "acq_dir":       acq_dir,
        "db_path":       db_path,
        "source_url":    url,
        "sidecar":       f"{acq_dir}/{dest_filename}.source.sha256",
        "extract":       extract,
        "slurm":         slurm_v,
        "submitted_at":  datetime.now(timezone.utc).isoformat(),
        "host":          env.get("host"),
        "follow_up": {
            "poll":  f"cluster_job_status(project, {compute_env!r}, job_id={job_id!r})",
            "then":  "once COMPLETED, seal_workflow verifies the DB over ssh (I5)",
        },
    }
    manifest_path = submit_workflow._write_submission_manifest(
        project_name=compute_env, workflow_name=f"acquire_{name}",
        job_id=job_id, manifest=manifest)

    return proven("acquire.submitted",
        success=True,
        locus="cluster",
        compute_env=compute_env,
        job_id=job_id,
        acq_dir=acq_dir,
        db_path=db_path,
        source_url=url,
        reference_database_recorded=(merged_into is not None),
        pipeline_id=merged_into,
        manifest_path=manifest_path,
        note=(f"Download job {job_id} submitted on {compute_env}. It runs on a "
              f"compute node (head node stays clean) and resumes on re-submit. "
              f"Poll cluster_job_status; the reference DB will be verified over "
              f"ssh at seal (I5) — seal only once the job COMPLETES."))


def acquire_via_recipe(*, name: str, recipe_local_path: str, compute_env: str,
                       versions: Optional[list] = None, remote_dir: str = "",
                       slurm: Optional[Mapping] = None,
                       mask_tools: tuple = ("tmux",), bundle_version: str = "",
                       description: str = "", pipeline_id: str = "",
                       access_path: Optional[str] = None,
                       _pipeline_state=None) -> dict:
    """Run a TOOL'S OWN data-acquisition recipe on `compute_env` (head-node-safe,
    on a compute node) into the env's common_data zone, capturing sha256 + version
    provenance. The generalization of "the tool ships a gather_files.sh — use it,
    don't re-invent it": we upload the tool's recipe verbatim, wrap it in a SLURM
    runner that forces its non-interactive path + hashes every produced file, and
    record ONE cluster-locus reference_databases entry for the whole bundle dir
    plus a per-file `files[]` version manifest (agent-read from the recipe/docs —
    the reproducibility + citation record, per the reference-data-versioning rule).

    Submit-and-document: returns the job_id immediately (a full reference bundle is
    tens of GB / hours). Poll cluster_job_status; seal verifies the dir over ssh.

    `versions` = [{filename, url, version, note?}] — the FINAL produced filenames
    (post-decompression/rename) mapped to their versions. `mask_tools` hides
    interactive-only tools from the recipe's PATH (default tmux)."""
    if not name or not compute_access._is_safe_token(name):
        return refused("acquire.bad_name",
            error=f"name {name!r} must be a safe token (alnum + '_-', <=64 chars) "
                  f"— it becomes a path component under common_data")
    recipe_p = Path(recipe_local_path).expanduser()
    if not recipe_p.is_file():
        return refused("acquire.recipe_missing",
            error=f"recipe_local_path {recipe_local_path!r} is not a file — pass the "
                  f"tool's own acquisition script (e.g. its large_files/gather_files.sh)")

    if _pipeline_state is None:
        from agent import mcp_server as _ms
        _pipeline_state = _ms._pipeline_state

    # ─── Resolve env + common_data zone ────────────────────────────────
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        env = compute_access.get_compute_env(compute_env, access)
    except (compute_access.ConfigError, FileNotFoundError, KeyError, ValueError) as e:
        return refused("acquire.config_error", error=f"{type(e).__name__}: {e}")
    if env.get("type") != "ssh":
        return refused("acquire.not_ssh_env",
            error=f"cluster acquisition needs an ssh compute env; {compute_env!r} "
                  f"is type={env.get('type')!r}.")
    common = compute_access.get_agent_common_data_target(env)
    if common is None:
        return refused("acquire.no_common_data",
            error=f"compute_env {compute_env!r} declares no agent_common_data_target.")
    common_root = (common.get("path") or "").rstrip("/")

    acq_dir = (remote_dir or f"{common_root}/{name}").rstrip("/")
    if not transfer._under(common_root, acq_dir):
        return refused("acquire.remote_dir_outside_common_data",
            error=f"remote_dir {acq_dir!r} is not under the env's common_data zone "
                  f"{common_root!r}.")

    recipe_filename = recipe_p.name
    # sha256 of the recipe we actually ran (provenance: which recipe produced this).
    import hashlib
    recipe_sha256 = hashlib.sha256(recipe_p.read_bytes()).hexdigest()

    # ─── SLURM header (generous walltime — a full bundle runs for hours) ──
    try:
        merged, email = submit_workflow._resolve_slurm_and_email(
            dict(slurm) if slurm else dict(_DEFAULT_DL_SLURM), env)
        slurm_v = workflow_render._check_slurm(merged)
    except ValueError as e:
        return refused("acquire.bad_slurm", error=f"slurm request invalid: {e}")

    # ─── Render the runner + stage both files under the Globus-accessible dir ─
    try:
        launcher = render_recipe_runner_script(
            name=name, recipe_filename=recipe_filename,
            mask_tools=tuple(mask_tools or ()), slurm_v=slurm_v, email=email)
    except ValueError as e:
        return refused("acquire.render_failed", error=f"runner render failed: {e}")

    _DL_STAGE_DIR.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="bioinf_recipe_",
                                     dir=str(_DL_STAGE_DIR)) as td:
        (Path(td) / _LAUNCHER_NAME).write_text(launcher)
        (Path(td) / recipe_filename).write_text(recipe_p.read_text())
        # Upload the tool's recipe verbatim, then our launcher, into acq_dir
        # (common_data zone — env-implicit grant, auto-created leaf).
        for fn in (recipe_filename, _LAUNCHER_NAME):
            up = transfer.upload(
                project_name=transfer.AD_HOC_PROJECT_NAME,
                compute_env_name=compute_env,
                local_path=str(Path(td) / fn),
                remote_abs_path=f"{acq_dir}/{fn}",
                access_path=str(Path(access_path)) if access_path else None,
                timeout=600)
            if "error" in up:
                return broke("acquire.upload_failed",
                    error=f"upload of {fn} to {acq_dir} failed: {up['error']}",
                    acq_dir=acq_dir)

    # ─── sbatch the recipe-runner job ──────────────────────────────────
    sb = submit_workflow.sbatch_via_ssh(env, acq_dir, timeout=300)
    if "error" in sb:
        return {**sb, "acq_dir": acq_dir}
    job_id = sb["job_id"]

    # ─── Record ONE cluster-locus reference_databases entry for the bundle ──
    files_manifest = [
        {k: v for k, v in (f or {}).items() if v is not None}
        for f in (versions or []) if isinstance(f, dict)
    ]
    rdb = {
        "name":         name,
        # A bundle spans many source versions; the truthful per-file versions live
        # in files[]. The entry-level version is the recipe identity + date floor.
        "version":      bundle_version or f"recipe:{recipe_filename}@{datetime.now(timezone.utc).date().isoformat()}",
        "source_url":   None,           # multi-source; per-file urls live in files[]
        "local_path":   acq_dir,        # the dir the tool consumes (I8 walks this)
        "locus":        "cluster",
        "compute_env":  compute_env,
        "available":    False,          # ssh-derived at seal (_refresh)
        "description":  description or f"Reference bundle produced by {recipe_filename}",
        "recipe":       recipe_filename,
        "recipe_sha256": recipe_sha256,
        "files":        files_manifest,  # per-file version manifest (extra=allow)
    }
    merged_into = None
    if pipeline_id:
        draft = _pipeline_state.get_draft(pipeline_id) or {}
        existing = list(draft.get("reference_databases") or [])
        replaced = False
        for i, e in enumerate(existing):
            if isinstance(e, dict) and e.get("name") == name:
                existing[i] = {**e, **{k: v for k, v in rdb.items() if v is not None}}
                replaced = True
                break
        if not replaced:
            existing.append({k: v for k, v in rdb.items() if v is not None})
        _pipeline_state.patch(pipeline_id, {"reference_databases": existing})
        merged_into = pipeline_id

    # ─── Submit-and-document manifest (findable, carries the version record) ──
    manifest = {
        "kind":          "reference_data_acquisition_via_recipe",
        "name":          name,
        "compute_env":   compute_env,
        "job_id":        job_id,
        "acq_dir":       acq_dir,
        "recipe":        recipe_filename,
        "recipe_sha256": recipe_sha256,
        "masked_tools":  list(mask_tools or ()),
        "files":         files_manifest,
        "slurm":         slurm_v,
        "submitted_at":  datetime.now(timezone.utc).isoformat(),
        "host":          env.get("host"),
        "follow_up": {
            "poll":  f"cluster_job_status(project, {compute_env!r}, job_id={job_id!r})",
            "then":  "once COMPLETED, seal verifies the bundle dir over ssh (I5); "
                     "per-file sha256 sidecars land next to each produced file",
        },
    }
    manifest_path = submit_workflow._write_submission_manifest(
        project_name=compute_env, workflow_name=f"acquire_{name}",
        job_id=job_id, manifest=manifest)

    return proven("acquire.recipe_submitted",
        success=True,
        locus="cluster",
        compute_env=compute_env,
        job_id=job_id,
        acq_dir=acq_dir,
        recipe=recipe_filename,
        recipe_sha256=recipe_sha256,
        files=files_manifest,
        reference_database_recorded=(merged_into is not None),
        pipeline_id=merged_into,
        manifest_path=manifest_path,
        note=(f"Recipe {recipe_filename} submitted as job {job_id} on {compute_env}. "
              f"It runs the tool's OWN acquisition script on a compute node into "
              f"{acq_dir}, then hashes every produced file. Poll cluster_job_status; "
              f"seal once COMPLETE (the bundle dir is ssh-verified at seal, I5)."))


# ---------------------------------------------------------------------------
# Seal-side: probe + verify a cluster-resident reference DB over ssh
# ---------------------------------------------------------------------------

def _probe_cluster_path(env: dict, path: str, *,
                        timeout: int = _PROBE_TIMEOUT) -> dict:
    """ONE ssh hop that reports {exists, size_bytes, sha256} for a cluster path
    (file OR directory). size via `du -sb`; sha256 read from a `<path>.source.sha256`
    sidecar if present (the reproducibility anchor written by the download job).
    Returns {"error": ...} if the ssh itself fails (session down / unreachable)."""
    from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint
    q = shlex.quote(path)
    q_side = shlex.quote(f"{path}.source.sha256")
    # Emit three tagged lines so a MOTD banner can't confuse the parse.
    probe = (
        f"bash -lc '"
        f"if [ -e {q} ]; then echo EXISTS=1; else echo EXISTS=0; fi; "
        f"if [ -e {q} ]; then du -sb {q} 2>/dev/null | cut -f1 | "
        f"sed \"s/^/SIZE=/\"; fi; "
        f"if [ -f {q_side} ]; then head -n1 {q_side} | sed \"s/^/SHA=/\"; fi"
        f"'"
    )
    argv = _ssh_argv(env, probe)
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return {"error": f"cluster probe timed out after {e.timeout}s"}
    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        out = {"error": f"cluster probe ssh failed (rc={res.returncode}): "
                        f"{(res.stderr or '').strip()[:300]}"}
        if hint:
            out["hint"] = hint
        return out
    exists = False
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("EXISTS="):
            exists = line.endswith("1")
        elif line.startswith("SIZE="):
            try:
                size_bytes = int(line[len("SIZE="):])
            except ValueError:
                pass
        elif line.startswith("SHA="):
            tok = line[len("SHA="):].strip()
            if len(tok) == 64 and all(c in "0123456789abcdef" for c in tok.lower()):
                sha256 = tok.lower()
    return {"exists": exists, "size_bytes": size_bytes, "sha256": sha256}


def _resolve_env_for_rdb(rdb: dict, access_path: Optional[str]):
    """Load the compute env named on a cluster-locus reference DB entry. Returns
    (env, None) or (None, error_str)."""
    env_name = rdb.get("compute_env")
    if not env_name:
        return None, "cluster reference DB entry has no compute_env recorded"
    try:
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        return compute_access.get_compute_env(env_name, access), None
    except (compute_access.ConfigError, FileNotFoundError, KeyError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def refresh_cluster_reference_db(rdb: dict, *,
                                 access_path: Optional[str] = None) -> dict:
    """Seal-time provenance refresh for a cluster-locus DB: ssh-derive available /
    size_bytes / sha256 from the cluster (mirrors the local sidecar refresh, over
    ssh). Best-effort — on an ssh failure the recorded values are left as-is (I5
    is the hard gate; this only enriches provenance). Returns a NEW dict."""
    out = dict(rdb)
    env, err = _resolve_env_for_rdb(rdb, access_path)
    if err or env is None:
        return out
    probe = _probe_cluster_path(env, rdb.get("local_path") or "")
    if "error" in probe:
        return out
    out["available"] = bool(probe.get("exists"))
    if probe.get("size_bytes") is not None:
        out["size_bytes"] = probe["size_bytes"]
    if probe.get("sha256"):
        out["sha256"] = probe["sha256"]
    return out


def check_cluster_reference_db(rdb: dict, *,
                               access_path: Optional[str] = None) -> list[dict]:
    """I5 for a cluster-locus reference DB: verify it exists + is non-empty ON THE
    CLUSTER (ssh), the same 'observe at the locus' posture as the C2 .sif
    round-trip. A cluster DB that isn't there — or can't be verified because the
    env is unreachable — fails the seal. Returns a list of violation dicts."""
    name = rdb.get("name") or "reference_database"
    path = rdb.get("local_path")
    where = f"reference_databases[name={rdb.get('name')}]"
    if not path:
        return []   # nothing to verify (unused declaration)

    env, err = _resolve_env_for_rdb(rdb, access_path)
    if err or env is None:
        return [{
            "invariant": "I5.reference_database_unverifiable",
            "message":   f"cluster reference_database '{name}' cannot be verified: {err}",
            "where":     where, "path": path,
        }]
    probe = _probe_cluster_path(env, path)
    if "error" in probe:
        return [{
            "invariant": "I5.reference_database_unverifiable",
            "message":   f"cluster reference_database '{name}' could not be verified on "
                         f"{rdb.get('compute_env')!r}: {probe['error']} — a seal cannot "
                         f"claim a cluster dependency it cannot observe (is `ssh` open?)",
            "where":     where, "path": path,
            **({"hint": probe["hint"]} if "hint" in probe else {}),
        }]
    if not probe.get("exists"):
        return [{
            "invariant": "I5.reference_database_missing",
            "message":   f"cluster reference_database '{name}' does not exist on "
                         f"{rdb.get('compute_env')!r} at {path} — the download job may "
                         f"not have completed yet (poll cluster_job_status before sealing)",
            "where":     where, "path": path,
        }]
    if probe.get("size_bytes") == 0:
        return [{
            "invariant": "I5.reference_database_empty",
            "message":   f"cluster reference_database '{name}' exists on "
                         f"{rdb.get('compute_env')!r} but is empty at {path} "
                         f"(partial/failed download?)",
            "where":     where, "path": path,
        }]
    return []
