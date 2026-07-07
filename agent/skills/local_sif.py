"""local_sif — build an Apptainer .sif LOCALLY from a docker image.

The rule ([[feedback-no-head-node-image-builds]]): image -> .sif conversion
(unpack + mksquashfs) is heavy and must NEVER run on the shared cluster head
node. The clean model is build the .sif on the agent machine and ship only the
finished artifact.

macOS / dev machines have no `apptainer` binary, so we run apptainer INSIDE a
pinned linux container (apptainer-in-docker) to convert a local docker image ->
.sif. `--privileged` gives it the loop/mksquashfs access the build needs; on
Docker Desktop that stays inside the local Linux VM.

Pure helper: docker in, local .sif path out. Uploading the .sif to the cluster is
the caller's job (via transfer.upload -> container_upload_target).
"""
from __future__ import annotations

import shlex
import subprocess
import tempfile
from pathlib import Path

from agent.skills.outcomes import proven, refused, broke

# The apptainer-in-docker builder image. Pinned to a tag (not :latest) so the
# .sif packing is reproducible; kaczmarj/apptainer ships the apptainer CLI as its
# entrypoint. Overridable for a different builder/registry.
APPTAINER_BUILDER_IMAGE = "kaczmarj/apptainer:1.4.4"


def _run(argv: list, timeout: int) -> dict:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"rc": r.returncode, "out": r.stdout or "", "err": r.stderr or ""}
    except subprocess.TimeoutExpired as e:
        return {"rc": 124, "out": "", "err": f"timed out after {e.timeout}s"}
    except FileNotFoundError as e:
        return {"rc": 127, "out": "", "err": str(e)}


def build_sif_locally(*, out_sif: str, tarball: str = "", image_tag: str = "",
                      platform: str = "linux/amd64",
                      builder_image: str = APPTAINER_BUILDER_IMAGE,
                      timeout: int = 1800) -> dict:
    """Build `out_sif` locally from EITHER a docker-save `tarball` OR a local
    `image_tag` (which we `docker save` to a temp tar first). Runs apptainer in a
    privileged linux container so no apptainer binary is needed on the host and no
    conversion touches the cluster.

    Returns {outcome: proven, sif_path, size_bytes, builder_image} on success, or a
    refused/broke dict. The .sif lands at `out_sif` (its parent dir is created)."""
    if bool(tarball) == bool(image_tag):
        return refused("local_sif.bad_args",
            error="pass exactly one of `tarball` or `image_tag`")
    out = Path(out_sif).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # Everything the builder container sees must sit under ONE bind dir (the .sif
    # output dir): the docker-archive tar and the produced .sif both live there.
    work = out.parent
    with tempfile.TemporaryDirectory(prefix="localsif_") as _td:
        tar_path = Path(tarball).expanduser().resolve() if tarball else None

        # image_tag -> docker save into the work dir so it's inside the bind.
        if image_tag:
            tar_path = work / f".{out.stem}.docker-archive.tar"
            sv = _run(["docker", "save", "-o", str(tar_path), image_tag], timeout)
            if sv["rc"] != 0:
                return broke("local_sif.docker_save_failed",
                    error=f"docker save {image_tag!r} failed: {sv['err'][:400]}")

        # The tar must be inside the bind dir (work). If a caller-supplied tarball
        # lives elsewhere, copy it in (small vs the build cost).
        if tar_path.parent != work:
            staged = work / f".{out.stem}.docker-archive.tar"
            cp = _run(["cp", str(tar_path), str(staged)], 120)
            if cp["rc"] != 0:
                return broke("local_sif.stage_tar_failed",
                    error=f"could not stage tar into build dir: {cp['err'][:300]}")
            tar_path = staged

        tar_name = tar_path.name
        sif_name = out.name
        # apptainer-in-docker: entrypoint IS apptainer, so args start at `build`.
        argv = [
            "docker", "run", "--rm", "--privileged", "--platform", platform,
            "-v", f"{work}:/work", "-w", "/work", builder_image,
            "build", "--force", sif_name, f"docker-archive:/work/{tar_name}",
        ]
        res = _run(argv, timeout)
        # clean up the docker-save tar we created (leave a caller-supplied one)
        try:
            if image_tag or (tarball and Path(tarball).resolve() != tar_path):
                tar_path.unlink(missing_ok=True)
        except OSError:
            pass

        if res["rc"] != 0:
            return broke("local_sif.build_failed",
                error=f"apptainer build failed (rc={res['rc']}): "
                      f"{(res['err'] or res['out'])[:600]}",
                builder_image=builder_image)
        if not out.is_file():
            return broke("local_sif.no_output",
                error=f"apptainer build reported success but {out} is missing")
        return proven("local_sif.built",
            success=True,
            sif_path=str(out),
            size_bytes=out.stat().st_size,
            builder_image=builder_image,
            source=("image:" + image_tag) if image_tag else ("tarball:" + str(tarball)))
