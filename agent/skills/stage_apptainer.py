"""
stage_apptainer_image — get an apptainer .sif onto a compute env.

What this is
------------
A small, MODE-AWARE primitive that takes a frozen env (by
request_key into the EnvCache) and puts the runnable .sif somewhere
on a compute env so a SLURM job can `apptainer exec` it.

What it ISN'T
-------------
Not a composite. The caller still calls `freeze()` to populate the
EnvCache and `submit_workflow_job` to run jobs. This primitive is
the irreducible "get the bytes there" step.

How it picks the right method
-----------------------------
The EnvCache record's `mode` decides:

  ADOPT
    Public BioContainer. ONE ssh hop:
      apptainer pull <sif_path> docker://<image_by_digest>
    No bytes move through us — apptainer fetches direct from quay.io
    (or wherever). Idempotent: skip the pull if the .sif already
    exists at sif_path.

  BUILD (registry-free)
    Local docker-save tarball. TWO steps:
      1. Upload the .tar to the cluster via `transfer.upload`.
         Wire protocol (scp_head_node vs globus) is picked by the
         env's `data_transfer.type` inside transfer.upload — this
         primitive doesn't second-guess it.
      2. ssh apptainer build <sif_path> docker-archive://<tar_path>
    Idempotent on the .sif side; the .tar lands at its own
    deterministic path under the container zone.

  BUILD (with push_target)
    Pushed to a registry. ONE ssh hop:
      apptainer pull <sif_path> docker://<push_target>
    Same shape as adopt.

Where the .sif (and .tar) land
------------------------------
Both container artifacts land in the env's `container_upload_target`:
  .sif:  <env.container_upload_target>/<env_name>_<digest>.sif
  .tar:  <env.container_upload_target>/apptainer_sources/<basename>.tar

No fallback. If the env hasn't declared a `container_upload_target`
with `upload` permission, the primitive refuses — per
[[project-container-artifacts-routing]], container artifacts do NOT
spill into `agent_common_data_target` (that zone is for reference
data). The user defines the rails; we don't paper over a missing one.

env_name is the freeze record's env name (so multiple frozen envs
coexist); digest is the short content_digest so a re-freeze with a
different result doesn't clobber the old .sif.

Caller may override the .sif subpath via `sif_subpath`; we still
resolve it against `container_upload_target.path` (so the file stays
in the designated zone).

Authorization
-------------
Same env-implicit grant as every other env-target primitive:
  - project must have a `compute_env_access` entry for this env
  - env must declare `container_upload_target` with `upload` perm
For ADOPT mode we ALSO need apptainer + network egress on the head
node — that's a cluster property, not something this primitive can
guarantee; failures surface in `apptainer_stderr`.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import compute_access, transfer
from agent.skills.outcomes import proven, refused, broke
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


def _short_digest(content_digest: str) -> str:
    """Take the first 12 hex chars of a sha256:abcd... digest so the
    filename stays readable while keeping enough entropy to dedupe."""
    if not isinstance(content_digest, str) or ":" not in content_digest:
        return "unknown"
    hexdigest = content_digest.split(":", 1)[1]
    return hexdigest[:12] if hexdigest else "unknown"


def _build_apptainer_pull_cmd(sif_remote_abs: str,
                              docker_uri: str) -> str:
    """The remote shell command that fetches an image via apptainer.
    Login shell so `module load apptainer` works on systems where
    apptainer lives behind Lmod. Skips the pull if the .sif already
    exists (idempotent re-stage)."""
    # `module load apptainer` is intentionally outside the test for
    # the file existing — even when we skip the pull, we want failures
    # to be diagnosed via `apptainer --version` rather than a missing
    # `apptainer` command.
    return (
        f"bash -lc 'module load apptainer >/dev/null 2>&1 || true; "
        f"if [ -f {shlex.quote(sif_remote_abs)} ]; then "
        f"  echo SKIP_ALREADY_STAGED; "
        f"else "
        f"  mkdir -p {shlex.quote(str(Path(sif_remote_abs).parent))} && "
        f"  apptainer pull {shlex.quote(sif_remote_abs)} "
        f"{shlex.quote(docker_uri)}; "
        f"fi'"
    )


_SHA256_TOKEN_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _extract_source_digests(inspect: Optional[dict]) -> set:
    """Pull every `sha256:<64hex>` token out of the .sif's apptainer-inspect
    labels. A biocontainer .sif built from a by-digest pull records its source
    in `org.label-schema.usage.singularity.deffile.from` (e.g.
    `quay.io/biocontainers/samtools@sha256:23cd…`) — confirmed against real
    HPC-cluster output. When present, matching this against the EnvCache's pinned
    image_digest is a CRYPTOGRAPHIC tie: the .sif on the cluster was built from
    the exact image we froze — not merely 'a .sif exists here'. build_archive
    .sifs (from a local docker-archive) carry no such digest, so the set is
    empty and the caller falls back to the observed-fingerprint check."""
    if not isinstance(inspect, dict):
        return set()
    labels = (((inspect.get("data") or {}).get("attributes") or {})
              .get("labels") or {})
    found: set = set()
    for v in labels.values():
        if isinstance(v, str):
            found.update(_SHA256_TOKEN_RE.findall(v))
    return found


def _build_inspect_cmd(sif_remote_abs: str) -> str:
    """ONE ssh hop that fingerprints the staged .sif: its on-disk sha256
    (the bytes that will actually `apptainer exec`) plus `apptainer inspect`
    provenance. A `---INSPECT---` marker separates the two so the parser
    never confuses a hash line with the JSON body."""
    q = shlex.quote(sif_remote_abs)
    # `sha256sum` prints `<hash>  <file>`; we extract the first whitespace
    # token in Python (below) rather than relying on a nested awk/cut inside
    # the single-quoted `bash -lc` body — one less quoting hazard on the wire.
    return (
        f"bash -lc 'module load apptainer >/dev/null 2>&1 || true; "
        f"if [ ! -f {q} ]; then echo SIF_MISSING; exit 3; fi; "
        f"sha256sum {q} 2>/dev/null; "
        f"echo ---INSPECT---; "
        f"apptainer inspect --json {q} 2>/dev/null || echo INSPECT_FAILED'"
    )


def inspect_staged_sif(env: dict, sif_remote_abs: str,
                       *, timeout: int = 300) -> dict:
    """Fingerprint the .sif THAT WILL RUN, on the cluster — the C2 round-trip.

    Before this, run_step_on_cluster recorded the EnvCache's *nominal* image
    digest as the step's container_image_digest and the seal matched it back
    against the EnvCache — circular: nothing was ever observed on the cluster.
    This looks at the actual artifact: `sha256sum` of the .sif (the exact
    bytes apptainer will exec) + `apptainer inspect --json` provenance.

    Returns {sif_sha256, inspect (dict|None), apptainer_inspect_ok, raw} on
    success, or {error} if the .sif is missing / ssh fails. The sha256 is the
    durable anchor a reviewer can independently re-check; its presence is what
    lets the shipped-image badge mean 'we observed the real image', not 'we
    copied a digest'."""
    argv = _ssh_argv(env, _build_inspect_cmd(sif_remote_abs))
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return broke("stage.inspect_timeout",
                     error=f"apptainer inspect timed out after {e.timeout}s")
    out = res.stdout or ""
    if "SIF_MISSING" in out:
        return refused("stage.inspect_sif_missing",
                error=f"staged .sif not found on cluster at {sif_remote_abs!r} "
                      f"— stage_apptainer_image must run first")
    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        return broke("stage.inspect_ssh_failed",
                error=f"apptainer inspect ssh failed (rc={res.returncode}): "
                      f"{(res.stderr or '').strip()[:300]}",
                **({"hint": hint} if hint else {}))

    head, _, tail = out.partition("---INSPECT---")
    # First whitespace token of the first non-empty line = the hash (drops the
    # trailing `  <filename>` sha256sum appends).
    first_line = next((ln for ln in head.splitlines() if ln.strip()), "")
    sif_sha256 = (first_line.split() or [""])[0]
    inspect_body = tail.strip()
    inspect: Optional[dict] = None
    apptainer_inspect_ok = False
    if inspect_body and "INSPECT_FAILED" not in inspect_body:
        try:
            import json as _json
            inspect = _json.loads(inspect_body)
            apptainer_inspect_ok = True
        except Exception:
            inspect = None
    return {
        "sif_sha256":           sif_sha256 or None,
        "inspect":              inspect,
        "apptainer_inspect_ok": apptainer_inspect_ok,
        "raw":                  out[:2000],
    }


def stage_apptainer_image(
        project_name: str,
        compute_env_name: str,
        freeze_request_key: str,
        *,
        sif_subpath: str = "",
        env_cache=None,
        access_path: Optional[str] = None,
        timeout: int = 1800) -> dict:
    """Get the apptainer .sif for `freeze_request_key` onto
    `compute_env_name`. Mode-aware: ADOPT → pull on cluster; BUILD →
    upload .tar + build on cluster.

    Returns on success:
      {success: True, compute_env, mode, sif_path, image_digest,
       request_key, skipped: bool, staged_at}
    The `skipped: True` flag means the .sif already existed at
    sif_path — a re-stage is a no-op, not an error.

    Returns {"error": ..., ...} on any refusal/failure. Useful
    diagnostic fields when present:
      - `apptainer_stderr` (ADOPT path)
      - `tar_upload_error` / `apptainer_build_error` (BUILD path)
      - `hint` (e.g. "open ssh hpc-agent in a side terminal")
    """
    try:
        # EnvCache lookup — find the freeze record by request_key.
        # EnvCache.lookup(key) reads from disk on every call, so a freeze
        # in a sibling process is visible immediately.
        if env_cache is None:
            from agent import mcp_server as _ms  # late import; singleton
            env_cache = _ms._env_cache
        record = env_cache.lookup(freeze_request_key)
        if not record:
            return refused("stage.not_in_cache",
                error=f"freeze_request_key {freeze_request_key!r} not in "
                f"EnvCache. Call freeze() first.")

        # Resolve env + project + auth (env-implicit container_upload perm)
        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        env_type = env.get("type")
        if env_type != "ssh":
            return refused("stage.non_ssh_env",
                error=f"stage_apptainer_image only supports ssh envs; "
                f"got type={env_type!r} on env {compute_env_name!r}")

        # Project must have access to env (same shape as the other
        # env-implicit primitives — no per-directory perm needed for
        # ADOPT; for BUILD upload, transfer.upload re-checks
        # container_upload_target capability at upload time).
        has_access = any(
            isinstance(b, dict) and b.get("compute_env") == compute_env_name
            for b in (project.get("compute_env_access") or []))
        if not has_access:
            return refused("stage.no_env_access",
                error=f"PermissionDenied: project {project_name!r} has no "
                f"compute_env_access entry for compute_env "
                f"{compute_env_name!r}")

        # Container artifacts (.sif + .tar) land ONLY in the env's
        # container_upload_target — no fallback to agent_common_data_target
        # (that zone is for reference data, per
        # [[project-container-artifacts-routing]]). If the env doesn't
        # declare a container target, this fails loud — the user defines
        # the rails; we don't paper over an undeclared one.
        ct = env.get("container_upload_target") or {}
        ct_path = (ct.get("path") or "").rstrip("/")
        ct_perms = ct.get("permissions") or []
        if not (ct_path and "upload" in ct_perms):
            return refused("stage.no_upload_target",
                error=f"env {compute_env_name!r} has no `container_upload_target` "
                f"with `upload` permission. Declare one in projects_access.yaml "
                f"under compute_envs[name={compute_env_name!r}]; container "
                f"artifacts (.sif + .tar) land there exclusively — they do NOT "
                f"fall back to agent_common_data_target.")

        env_name_for_sif = (record.get("name")
                            or freeze_request_key.split("|", 1)[0])
        content_digest = record.get("content_digest") or record.get(
            "image_digest", "")
        # The whole target IS the container zone — default subpath is
        # just `<env_name>_<digest>.sif`, no extra `apptainer/` prefix.
        subpath = (sif_subpath or
                   f"{env_name_for_sif}_{_short_digest(content_digest)}.sif")
        # Refuse traversal in sif_subpath
        if ".." in subpath.split("/") or subpath.startswith("/"):
            return refused("stage.bad_subpath",
                error=f"sif_subpath {subpath!r} must be relative + no `..`")
        sif_remote_abs = f"{ct_path}/{subpath}"

        mode = record.get("mode")
        image_digest = record.get("image_digest") or ""

        # ─── ADOPT path ──────────────────────────────────────────────
        if mode == "adopt":
            image = record.get("image")
            if not image:
                return refused("stage.adopt_missing_image",
                    error=f"ADOPT record missing `image`; can't pull "
                    f"(request_key={freeze_request_key!r})")
            docker_uri = f"docker://{image}"
            remote_cmd = _build_apptainer_pull_cmd(sif_remote_abs, docker_uri)
            argv = _ssh_argv(env, remote_cmd)
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
            if res.returncode != 0:
                hint = _ssh_failure_hint(res.stderr or "",
                                          env.get("host", "?"))
                return broke("stage.pull_failed",
                    error=
                        f"apptainer pull failed (rc={res.returncode}): "
                        f"{(res.stderr or '').strip()[:500]}",
                    apptainer_stderr=(res.stderr or "").strip()[:1000],
                    remote_cmd=remote_cmd[:200],
                    **({"hint": hint} if hint else {}),
                )
            skipped = "SKIP_ALREADY_STAGED" in (res.stdout or "")
            return proven("stage.adopted",
                success=True,
                compute_env=compute_env_name,
                mode="adopt",
                sif_path=sif_remote_abs,
                image_digest=image_digest,
                request_key=freeze_request_key,
                skipped=skipped,
                staged_at=datetime.now(timezone.utc).isoformat(),
            )

        # ─── BUILD-with-push_target path ─────────────────────────────
        push_target = record.get("push_target")
        if push_target and not record.get("gated"):
            docker_uri = f"docker://{push_target}"
            remote_cmd = _build_apptainer_pull_cmd(sif_remote_abs, docker_uri)
            argv = _ssh_argv(env, remote_cmd)
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
            if res.returncode != 0:
                return broke("stage.push_pull_failed",
                    error=
                        f"apptainer pull (push_target) failed (rc="
                        f"{res.returncode}): {(res.stderr or '').strip()[:500]}",
                    apptainer_stderr=(res.stderr or "").strip()[:1000],
                )
            return proven("stage.pushed",
                success=True,
                compute_env=compute_env_name,
                mode="build_push",
                sif_path=sif_remote_abs,
                image_digest=image_digest,
                request_key=freeze_request_key,
                skipped="SKIP_ALREADY_STAGED" in (res.stdout or ""),
                staged_at=datetime.now(timezone.utc).isoformat(),
            )

        # ─── BUILD-registry-free path (.tar transfer + build) ────────
        # Wire-protocol routing (scp_head_node vs globus) is decided
        # inside transfer.upload() by the env's `data_transfer.type` —
        # this primitive doesn't second-guess it.
        tarball = record.get("tarball")
        if not tarball:
            return refused("stage.no_tarball",
                error=f"freeze record has mode={mode!r} but no `tarball` or "
                f"`push_target` — can't stage. (request_key="
                f"{freeze_request_key!r})")
        tarball_path = Path(tarball)
        if not tarball_path.exists():
            return refused("stage.tarball_missing",
                error=f"freeze record tarball missing on disk: {tarball!r}. "
                f"Re-run freeze() to regenerate.")

        # The .tar is a container artifact — lands in the same
        # container_upload_target as the .sif (resolved above). The
        # `apptainer_sources/` subdir keeps build-input tarballs
        # separate from the built .sif files inside the same zone.
        tar_subpath = f"apptainer_sources/{tarball_path.name}"
        tar_remote_abs = f"{ct_path}/{tar_subpath}"
        up = transfer.upload(
            project_name=project_name,
            compute_env_name=compute_env_name,
            local_path=str(tarball_path),
            remote_abs_path=tar_remote_abs,
            access_path=str(Path(access_path)) if access_path else None,
            timeout=timeout,
        )
        if "error" in up:
            # Tolerate the upload-already-exists case as a no-op.
            if "already exists" not in (up.get("error") or ""):
                return broke("stage.tar_upload_failed",
                    error=
                        f"tar upload to container_upload_target failed: "
                        f"{up['error']}",
                    tar_upload_error=up.get("error"),
                )

        # Step 2: apptainer build .sif docker-archive://<tar>
        build_cmd = (
            f"bash -lc 'module load apptainer >/dev/null 2>&1 || true; "
            f"if [ -f {shlex.quote(sif_remote_abs)} ]; then "
            f"  echo SKIP_ALREADY_STAGED; "
            f"else "
            f"  mkdir -p {shlex.quote(str(Path(sif_remote_abs).parent))} && "
            f"  apptainer build {shlex.quote(sif_remote_abs)} "
            f"docker-archive://{shlex.quote(tar_remote_abs)}; "
            f"fi'"
        )
        argv = _ssh_argv(env, build_cmd)
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            return broke("stage.apptainer_build_failed",
                error=
                    f"apptainer build failed (rc={res.returncode}): "
                    f"{(res.stderr or '').strip()[:500]}",
                apptainer_build_error=(res.stderr or "").strip()[:1000],
                tar_remote_path=tar_remote_abs,
            )
        return proven("stage.built_archive",
            success=True,
            compute_env=compute_env_name,
            mode="build_archive",
            sif_path=sif_remote_abs,
            tar_remote_path=tar_remote_abs,
            image_digest=image_digest,
            request_key=freeze_request_key,
            skipped="SKIP_ALREADY_STAGED" in (res.stdout or ""),
            staged_at=datetime.now(timezone.utc).isoformat(),
        )

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return refused("stage.exception", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("stage.timeout",
                     error=f"stage_apptainer_image timed out after {e.timeout}s")
