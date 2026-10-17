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
EnvCache and `run_production_pipeline` to run jobs. This primitive is
the irreducible "get the bytes there" step.

How it delivers (local-build-and-ship)
--------------------------------------
The .sif is ALWAYS built on the AGENT machine and only the finished
artifact is shipped — image -> .sif conversion (unpack + mksquashfs) is
heavy and must never run on the shared cluster head node, and we never
`apptainer pull` a multi-GB image there either
([[feedback-no-head-node-image-builds]]). macOS/dev machines have no
apptainer binary, so the build runs apptainer INSIDE a pinned linux
container (`local_sif.build_sif_locally`).

Every EnvCache `mode` converges to the same flow:
  1. Obtain a LOCAL docker image:
       - adopt / push_target  -> `docker pull` the (public/pushed) ref
       - build                -> the locally-built image tag, or freeze's
                                 docker-save tarball
  2. `local_sif.build_sif_locally(...)` -> a .sif on the agent machine.
  3. `transfer.upload(...)` the finished .sif to container_upload_target.
     Wire protocol (scp_head_node vs globus) is picked by the env's
     `data_transfer.type` inside transfer.upload.
Idempotent: a light read-only ssh (`test -f`) skips the whole build+ship
when the .sif is already staged. No head-node build/pull, ever.

Where the .sif lands
--------------------
The finished .sif lands in the env's `container_upload_target`:
  <env.container_upload_target>/<env_name>_<digest>.sif
with the env's record (ENV report, attestation, both recipe forms) beside it in a
directory of the same name, so the cluster copy says what it holds and how it was
proven:
  <env.container_upload_target>/<env_name>_<digest>/
It is sent once, only when the record here names this image's digest, and a copy
already there counts only when its checksums match.

No fallback. If the env hasn't declared a `container_upload_target`
with `upload` permission, the primitive refuses — per
[[project-container-artifacts-routing]], container artifacts do NOT
spill into `agent_common_data_target` (that zone is for reference
data). The user defines the rails; we don't paper over a missing one.

env_name is the freeze record's env name (so multiple frozen envs
coexist); digest is the short IMAGE digest, so a .sif's name says which
image it holds: a rebuild is a new image and gets a new .sif, never the
old one found under the same name.

Caller may override the .sif subpath via `sif_subpath`; we still
resolve it against `container_upload_target.path` (so the file stays
in the designated zone).

Authorization
-------------
Same env-implicit grant as every other env-target primitive:
  - the project's `compute_envs` must list this env
  - env must declare `container_upload_target` with `upload` perm
Local docker (build/pull) + apptainer-in-docker run locally; the only
cluster interaction is a read-only `test -f` probe and the .sif upload.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from agent.skills import compute_access, transfer, workspace
from agent.models.core_data import frozen_image_ref, record_is_gated as _record_is_gated, sha256_file
from agent.skills.freeze import canon_platform as _canon_platform
from agent.skills.outcomes import proven, refused, broke, loop
from agent.skills.snapshot import _SEEN_FN, _ssh_argv, _ssh_failure_hint


def _short_digest(digest: str) -> str:
    """Take the first 12 hex chars of a sha256:abcd... digest so the
    filename stays readable while keeping enough entropy to dedupe."""
    if not isinstance(digest, str) or ":" not in digest:
        return "unknown"
    hexdigest = digest.split(":", 1)[1]
    return hexdigest[:12] if hexdigest else "unknown"




def sif_remote_path(env: dict, record: dict, freeze_request_key: str, sif_subpath: str = "") -> str:
    """Where a frozen env's .sif lands on `env`: the container zone itself (no
    extra prefix), as `<env_name>_<image digest12>.sif` unless a subpath is given.
    Named by the image, not the build inputs: a rebuild from the same inputs is
    a different image, and must not find the old .sif waiting under its name."""
    ct_path = ((env.get("container_upload_target") or {}).get("path") or "").rstrip("/")
    name = record.get("name") or freeze_request_key.split("|", 1)[0]
    digest = record.get("image_digest") or record.get("content_digest", "")
    return f"{ct_path}/{sif_subpath or f'{name}_{_short_digest(digest)}.sif'}"


def _remote_sif_exists(env: dict, sif_remote_abs: str, *, timeout: int = 120) -> Optional[bool]:
    """ONE light read-only ssh: does the .sif already exist on the cluster? True or
    False as the cluster answered; None when it could not be asked (falsy, so a
    stage builds and ships, and the ship then names the ssh failure). Used for
    idempotency (skip a re-stage) WITHOUT any build/pull on the head node."""
    body = _SEEN_FN + f"seen {shlex.quote(sif_remote_abs)} && echo EXISTS || echo MISSING"
    cmd = f"bash -lc {shlex.quote(body)}"
    try:
        res = subprocess.run(_ssh_argv(env, cmd), capture_output=True,
                             text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = (res.stdout or "").splitlines()
    return True if "EXISTS" in lines else (False if "MISSING" in lines else None)


def _stage_via_local_build(*, record: dict, env: dict, env_name: str, project_name: str,
                           compute_env_name: str, sif_remote_abs: str,
                           image_digest: str, freeze_request_key: str,
                           platform: str, access_path: Optional[str],
                           timeout: int) -> dict:
    """Build the .sif LOCALLY (apptainer-in-docker) and ship the finished
    artifact — NEVER build/pull on the shared cluster head node
    ([[feedback-no-head-node-image-builds]]). Unifies every delivery mode:
    obtain a local docker image (pull a public/pushed ref, or use the locally
    built image / freeze's docker-save tarball), build the .sif on the agent
    machine, upload it to the env's container_upload_target."""
    from agent.skills import local_sif

    mode = record.get("mode")
    record_kw = dict(env=env, env_name=env_name,
                     digest=record.get("content_digest") or image_digest,
                     project_name=project_name, compute_env_name=compute_env_name,
                     sif_remote_abs=sif_remote_abs, access_path=access_path, timeout=timeout)
    # 1. Idempotency — skip if the .sif is already staged on the cluster.
    if _remote_sif_exists(env, sif_remote_abs, timeout=min(timeout, 120)):
        return proven("stage.skipped", success=True,
            compute_env=compute_env_name, mode=mode,
            sif_path=sif_remote_abs, image_digest=image_digest,
            request_key=freeze_request_key, skipped=True,
            staged_at=datetime.now(timezone.utc).isoformat(),
            env_record=_stage_record(**record_kw))

    # An earlier upload of this .sif whose Globus task has not been seen to end may still
    # be reading the local .sif a rebuild would replace.
    inflight = transfer.pending_upload(env, sif_remote_abs)
    if inflight is not None:
        call = transfer.status_call(project_name, compute_env_name, inflight[1].get("globus_task_id"))
        return loop("stage.sif_upload_still_running", success=False, sif_path=sif_remote_abs,
            error=f"an earlier upload of {sif_remote_abs} has not been seen to end; rebuilding the "
                  f"local .sif under it would change what that upload reads",
            remedy=f"{call} until SUCCEEDED (it puts the .sif in place), then stage_apptainer_image "
                   f"again: it finds the .sif and sends the record")

    # 2. Resolve the LOCAL docker source (tarball or image tag). The archive's
    #    directory is the env's directory in the environments zone, read at call time.
    from agent.skills.freeze import tarball_path
    tarball = tarball_path(record)
    image_tag = None
    if mode == "adopt":
        image_tag = record.get("image")                 # public biocontainer ref
    elif record.get("push_target") and not _record_is_gated(record):
        image_tag = record.get("push_target")            # pushed registry ref
    elif tarball and Path(tarball).exists():
        pass                                             # freeze docker-save archive
    elif record.get("image"):
        # the locally built image, by the handle its record pins: a tag may have moved
        image_tag = frozen_image_ref(record["image"], record.get("image_digest") or "")

    if not (tarball and Path(str(tarball)).exists()) and not image_tag:
        return refused("stage.no_local_source",
            error=f"cannot stage: freeze record has no local docker-save tarball "
                  f"nor an image ref to build from (request_key="
                  f"{freeze_request_key!r})")

    # 3. If it's a registry ref not already on disk, pull it locally.
    #
    # Test the RESOLVED FILE, not the recorded string. A freeze record keeps its
    # `tarball` path forever, so once the tarball is deleted (they are large, and
    # pruning them is routine) `tarball` stays TRUTHY and this branch was skipped —
    # leaving the pull unreachable and `local_sif` to `docker save` a tag that may no
    # longer exist. The precondition three lines above already resolves the path this
    # way; one file, two readings of one fact, and this was the wrong half.
    if image_tag and not (tarball and Path(str(tarball)).exists()):
        have = subprocess.run(["docker", "image", "inspect", image_tag],
                              capture_output=True, text=True, timeout=60)
        if have.returncode != 0:
            pull = subprocess.run(
                ["docker", "pull", "--platform", platform, image_tag],
                capture_output=True, text=True, timeout=timeout)
            if pull.returncode != 0:
                return broke("stage.docker_pull_failed",
                    error=f"docker pull {image_tag!r} failed: "
                          f"{(pull.stderr or '').strip()[:400]}")

    # 4. Build the .sif LOCALLY (apptainer-in-docker; no cluster involvement).
    local_sif_path = transfer.staging_dir("apptainer_local_sif") / Path(sif_remote_abs).name
    built = local_sif.build_sif_locally(
        out_sif=str(local_sif_path),
        tarball=(str(tarball) if (tarball and not image_tag) else ""),
        image_tag=(image_tag or ""),
        platform=platform, timeout=timeout)
    if built.get("outcome") != "proven":
        return built  # broke/refused (docker_save/build failure) surfaces as-is

    # 5. Ship the FINISHED .sif to the container zone (globus/scp per env).
    up = transfer.upload(
        project_name=project_name, compute_env_name=compute_env_name,
        local_path=str(local_sif_path), remote_abs_path=sif_remote_abs,
        access_path=str(Path(access_path)) if access_path else None,
        timeout=timeout)
    if up.get("outcome") == "loop":
        return loop("stage.sif_upload_still_running", success=False,
            error=up["error"], task_id=up.get("task_id"), sif_path=sif_remote_abs,
            local_sif=str(local_sif_path),
            remedy=f"{transfer.status_call(project_name, compute_env_name, up.get('task_id'))} until SUCCEEDED (it puts "
                   f"the .sif in place), then stage_apptainer_image again: it finds the "
                   f".sif and sends the record")
    if up.get("code") == "transfer.remote_exists":
        # An upload lands under its final name only once verified, so a .sif there
        # is a whole one: the same answer the probe at the top gives.
        return proven("stage.skipped", success=True,
            compute_env=compute_env_name, mode=mode,
            sif_path=sif_remote_abs, image_digest=image_digest,
            request_key=freeze_request_key, skipped=True,
            staged_at=datetime.now(timezone.utc).isoformat(),
            env_record=_stage_record(**record_kw))
    if "error" in up:
        return broke("stage.sif_upload_failed",
            error=f"upload of the locally-built .sif to container_upload_target "
                  f"failed: {up['error']}",
            local_sif=str(local_sif_path))

    return proven("stage.built_local",
        success=True, compute_env=compute_env_name, mode=f"{mode}_local_sif",
        sif_path=sif_remote_abs, image_digest=image_digest,
        request_key=freeze_request_key, local_sif=str(local_sif_path),
        builder_image=built.get("builder_image"), skipped=False,
        staged_at=datetime.now(timezone.utc).isoformat(),
        env_record=_stage_record(**record_kw))


def _record_digests(files: list[Path]) -> set[str]:
    """The image digests the record files name: the recipe's content_digest and the
    attestation's subjects."""
    named: set[str] = set()
    for f in files:
        try:
            if f.name.endswith(".recipe.yaml"):
                named.add(str((yaml.safe_load(f.read_text()) or {}).get("content_digest") or ""))
            elif f.name.endswith(".attestation.json"):
                named |= {f"sha256:{s.get('digest', {}).get('sha256', '')}"
                          for s in json.loads(f.read_text()).get("subject") or []}
        except (OSError, ValueError, AttributeError):
            continue
    return named


def _remote_record(env: dict, remote: str, *, timeout: int) -> Optional[dict]:
    """{file name: sha256} of the record directory at `remote`, or None when there is
    none. One ssh hop, in bash whatever the login shell."""
    probe = (_SEEN_FN + f"if seen {shlex.quote(remote)}; then echo PRESENT; "
             f"cd {shlex.quote(remote)} && sha256sum -- *; fi")
    res = subprocess.run(_ssh_argv(env, f"bash -c {shlex.quote(probe)}"),
                         capture_output=True, text=True, timeout=timeout)
    lines = (res.stdout or "").splitlines()
    if "PRESENT" not in lines:
        return None
    return {m[2]: m[1] for m in map(_SHA_LINE_RE.fullmatch, lines) if m}


_SHA_LINE_RE = re.compile(r"([0-9a-f]{64})  (.+)")


def _stage_record(*, env: dict, env_name: str, digest: str, project_name: str,
                  compute_env_name: str, sif_remote_abs: str, access_path: Optional[str],
                  timeout: int) -> dict:
    """Put the env's record beside its .sif, in a directory of the .sif's name, as one
    transfer — only when the record here names this image (a later freeze under the
    same name rewrites it). A copy already there counts only when its checksums match
    this record's. A record that could not be sent is reported, and never undoes the
    .sif."""
    remote = (sif_remote_abs.removesuffix(".sif") if sif_remote_abs.endswith(".sif")
              else f"{sif_remote_abs}.record")
    try:
        files = workspace.env_record_files(env_name)
        if not files:
            return {"status": "none_here",
                    "note": f"no ENV report, attestation or recipe for {env_name!r} in the environments zone"}
        if digest not in _record_digests(files):
            return {"status": "not_this_image", "path": remote,
                    "reason": f"the record of {env_name!r} here describes another image than {digest} "
                              f"(a later freeze under the same name); this image's record is not here to send"}
        here = {f.name: sha256_file(f) for f in files}
        there = _remote_record(env, remote, timeout=min(timeout, 120))
        if there == here:
            return {"status": "already_there", "path": remote}
        if there is not None:
            return {"status": "differs", "path": remote,
                    "reason": "a record directory is there that does not match this one; it is left as it is"}
        staged = Path(tempfile.mkdtemp(prefix="record_", dir=transfer.staging_dir("env_record_staging")))
        tree = staged / Path(remote).name
        tree.mkdir()
        for f in files:
            shutil.copy2(f, tree / f.name)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"status": "not_sent", "path": remote, "reason": f"{type(e).__name__}: {e}"}
    up = transfer.upload(project_name=project_name, compute_env_name=compute_env_name,
                         local_path=str(tree), remote_abs_path=remote,
                         access_path=str(Path(access_path)) if access_path else None, timeout=timeout)
    if "error" in up:
        # The staged copy stays: a transfer that stopped waiting may still be reading it.
        return {"status": "not_sent", "path": remote, "reason": up["error"], "staged": str(tree)}
    shutil.rmtree(staged)
    return {"status": "sent", "path": remote, "files": [f.name for f in files]}


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
    # `sha256sum` prints `<hash>  <file>`; the first whitespace token is extracted
    # in Python (below) rather than by a nested awk/cut in the remote body.
    body = (
        "module load apptainer >/dev/null 2>&1 || true; " + _SEEN_FN +
        f"if ! seen {q}; then echo SIF_MISSING; exit 3; fi; "
        f"sha256sum {q} 2>/dev/null; "
        "echo ---INSPECT---; "
        f"apptainer inspect --json {q} 2>/dev/null || echo INSPECT_FAILED"
    )
    return f"bash -lc {shlex.quote(body)}"


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
    # the one sha256sum reader: a login shell's banner lines are not the hash
    sif_sha256 = transfer._parse_sha256sum_output(head)
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
    `compute_env_name`. The .sif is built on THIS machine (apptainer inside a
    pinned linux container, from the frozen image or its docker-save tarball)
    and uploaded to the env's container_upload_target; nothing is built or
    pulled on the cluster.

    Returns on success:
      {success: True, compute_env, mode, sif_path, image_digest,
       request_key, local_sif, builder_image, skipped: bool, staged_at,
       env_record: {status, path?, ...}}
    The `skipped: True` flag means the .sif already existed at
    sif_path — a re-stage is a no-op, not an error. `env_record.status` is
    sent / already_there / differs / not_this_image / not_sent / none_here.

    Returns {"error": ..., ...} on any refusal/failure, with `hint` when
    the ssh hop itself failed (e.g. "open ssh hpc-agent in a side terminal").
    """
    try:
        # EnvCache lookup — find the freeze record by request_key.
        # EnvCache.lookup(key) reads from disk on every call, so a freeze
        # in a sibling process is visible immediately.
        if env_cache is None:
            from agent import mcp_server as _ms  # late import; singleton
            env_cache = _ms._env_cache
        # The SERVING question — this is the last gate before the artifact leaves the
        # machine for a cluster we cannot re-check. An env whose Layer-1 contract no
        # longer holds must not become a .sif on someone's HPC.
        record, env_violations = env_cache.lookup_verified(freeze_request_key)
        if env_violations:
            return refused("stage.env_contract_violated",
                error=f"the frozen env {freeze_request_key!r} no longer satisfies the "
                f"Layer-1 honesty contract — re-run freeze() before staging it to a cluster",
                honesty_violations=env_violations,
                violation_count=len(env_violations))
        if not record:
            known = sorted(env_cache.all().keys())[:50]
            return refused("stage.not_in_cache",
                error=(f"freeze_request_key {freeze_request_key!r} not in EnvCache "
                       + (f"(which holds: {known}). " if known else "(which is empty). ")
                       + "Call freeze() first."),
                known_request_keys=known)

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
        # Refuse traversal in sif_subpath
        if sif_subpath and (".." in sif_subpath.split("/") or sif_subpath.startswith("/")):
            return refused("stage.bad_subpath",
                error=f"sif_subpath {sif_subpath!r} must be relative + no `..`")
        sif_remote_abs = sif_remote_path(env, record, freeze_request_key, sif_subpath)

        mode = record.get("mode")
        image_digest = record.get("image_digest") or ""
        # The record's `platform` may be stored in conda form ('linux-64', how a
        # container-native BUILD records it) or docker form ('linux/amd64', how an
        # adopt/authors record does). It feeds `docker run/pull --platform` below,
        # which ONLY accepts the docker form — an un-normalized 'linux-64' is
        # rejected ("unknown operating system or architecture"). canon_platform
        # collapses both spellings to the docker form (unknown values pass through).
        platform = _canon_platform(record.get("platform") or "linux/amd64")

        # Guard: an ADOPT record MUST carry the image ref to build from.
        # (Kept as an explicit pre-flight refusal — no docker/ssh before it.)
        if mode == "adopt" and not record.get("image"):
            return refused("stage.adopt_missing_image",
                error=f"ADOPT record missing `image`; can't stage "
                f"(request_key={freeze_request_key!r})")

        # ─── Unified local-build-and-ship ────────────────────────────
        # ALL modes converge here: build the .sif LOCALLY (apptainer-in-docker)
        # and upload the finished artifact. The heavy image -> .sif conversion
        # (unpack + mksquashfs) NEVER runs on the cluster head node, and we never
        # `apptainer pull` a multi-GB image there either
        # ([[feedback-no-head-node-image-builds]]).
        return _stage_via_local_build(
            record=record, env=env, env_name=env_name_for_sif, project_name=project_name,
            compute_env_name=compute_env_name, sif_remote_abs=sif_remote_abs,
            image_digest=image_digest, freeze_request_key=freeze_request_key,
            platform=platform, access_path=access_path, timeout=timeout)

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return refused("stage.exception", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("stage.timeout",
                     error=f"stage_apptainer_image timed out after {e.timeout}s")
