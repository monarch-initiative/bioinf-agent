"""
Environment freezing — content-addressing + the solve-once cache.

Two notions of identity (this is the "scale" unlock):

  request_key    — what was ASKED for: (tools+versions, platform, accel). Used
                   for cache LOOKUP: "have we already solved samtools=1.21 on
                   linux-64?" Channels drift, so the same request can resolve
                   differently over time — hence it is only a lookup handle.

  content_digest — what was actually GOT: a sha256 over the resolved lock +
                   source commit_shas + binary/artifact sha256s + platform +
                   accel. Identical bytes → identical digest. This is the proof
                   of identity; an adopted/built image digest is the shipping
                   handle on top of it.

The EnvCache maps request_key → {content_digest, image, image_digest, …} in a
JSON file so freeze() can hand back a proven artifact by hash instead of
re-solving — the wall between install-hell and the biology layer that consumes
the env. Everything here is pure / filesystem-only (no network), so it is fully
deterministic and unit-testable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional


def request_key(tools: list[tuple[str, str]], platform: str, accel: str = "none") -> str:
    """Canonical lookup handle for 'what was asked for'. Order-independent."""
    spec = ",".join(f"{n}={v}" if v else n for n, v in sorted(tools))
    return f"{spec}|{platform}|{accel or 'none'}"


def compute_content_digest(parts: dict) -> str:
    """sha256 over a canonicalized identity dict → 'sha256:…'. Stable across
    key order and process runs (json sort_keys)."""
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


def content_digest_parts(spec: dict) -> dict:
    """Extract the identity-determining parts of a spec/draft. Kept separate
    from the hash so tests (and debugging) can see exactly what feeds the
    digest. Every component is something the runtime captured, not an
    agent assertion: lock_sha256 (conda list --explicit), source commit_shas
    (I11), binary sha256s (I14), authored-artifact sha256s (I9)."""
    pkgs = [p for p in (spec.get("packages") or []) if isinstance(p, dict)]

    def _im(p):
        im = p.get("install_method")
        return im if isinstance(im, dict) else {}

    sources = sorted(
        [[p.get("name", ""), _im(p).get("commit_sha", "")]
         for p in pkgs if _im(p).get("type") == "source"]
    )
    binaries = sorted(
        [[p.get("name", ""), _im(p).get("sha256", "")]
         for p in pkgs if _im(p).get("type") == "binary"]
    )
    artifacts = sorted(
        a.get("sha256", "") for a in (spec.get("authored_artifacts") or [])
        if isinstance(a, dict)
    )
    docker = spec.get("docker") if isinstance(spec.get("docker"), dict) else {}
    accel = spec.get("accelerator") if isinstance(spec.get("accelerator"), dict) else {}
    return {
        "lock":      spec.get("lock_sha256") or "",
        "sources":   sources,
        "binaries":  binaries,
        "artifacts": artifacts,
        "platform":  (docker or {}).get("platform") or "",
        "accel":     (accel or {}).get("type") or "none",
    }


def content_digest_from_spec(spec: dict) -> str:
    """Content digest of an env from its (draft or finalized) spec dict."""
    return compute_content_digest(content_digest_parts(spec))


def parse_tools(specs: list[str]) -> list[tuple[str, Optional[str]]]:
    """['samtools=1.21', 'bwa==0.7.17', 'fastqc'] → [('samtools','1.21'), …].
    Accepts '=' or '==' (conda/pip), and a bare name (no version)."""
    out: list[tuple[str, Optional[str]]] = []
    for s in specs:
        s = (s or "").strip()
        if not s:
            continue
        if "==" in s:
            n, v = s.split("==", 1)
        elif "=" in s:
            n, v = s.split("=", 1)
        else:
            n, v = s, None
        out.append((n.strip(), (v.strip() or None) if v else None))
    return out


def apptainer_delivery(
    *,
    mode: str,
    sif_name: str,
    image_by_digest: Optional[str] = None,
    push_target: Optional[str] = None,
    tarball: Optional[str] = None,
    gated: bool = False,
) -> dict[str, Any]:
    """The HPC delivery + run contract, matching the validated monarch-phenologs
    Apptainer pattern. Picks the get-it-onto-the-cluster command by case:

      adopt (public biocontainer)  → `apptainer pull` the immutable digest (no
                                     push, no transfer — it's already public)
      build + push_target (and not gated) → `apptainer pull` the pushed ref
      build (default / gated)      → registry-free: scp the docker-save tarball,
                                     `apptainer build … docker-archive://…`

    Returns the pull/build command, a run example, and a SLURM sbatch template
    (module load apptainer, APPTAINER_TMPDIR→scratch, --bind /scratch:/data).
    This block is recorded on the artifact and doubles as Layer-2 user-guide
    content (every command here is the real, runnable delivery path).
    """
    if mode == "adopt" and image_by_digest:
        get_cmd = f"apptainer pull {sif_name} docker://{image_by_digest}"
        source_note = "adopted public BioContainer — pulled by immutable digest, no push/transfer"
    elif mode == "build" and push_target and not gated:
        get_cmd = f"apptainer pull {sif_name} docker://{push_target}"
        source_note = f"built image pushed to {push_target}"
    else:
        tar = Path(tarball).name if tarball else f"{sif_name.removesuffix('.sif')}.tar"
        get_cmd = (
            f"# transfer {tar} to the cluster (scp/rsync), then on the HPC:\n"
            f"apptainer build {sif_name} docker-archive://{tar}"
        )
        source_note = ("registry-free transfer (docker save → docker-archive)"
                       + (" — required: image is license-gated" if gated else ""))

    run_example = f"apptainer exec --bind /scratch/$USER/data:/data {sif_name} <command>"
    sbatch = (
        "#!/bin/bash\n"
        "#SBATCH --job-name=bioinf\n"
        "#SBATCH --time=24:00:00\n"
        "#SBATCH --mem=32G\n"
        "#SBATCH --cpus-per-task=4\n\n"
        "module load apptainer\n"
        "export APPTAINER_TMPDIR=/scratch/$USER/tmp && mkdir -p \"$APPTAINER_TMPDIR\"\n\n"
        f"apptainer exec --bind /scratch/$USER/data:/data {sif_name} <command>\n"
    )
    return {
        "mode": mode,
        "source_note": source_note,
        "get_image": get_cmd,
        "run_example": run_example,
        "sbatch_template": sbatch,
    }


def freeze_record(
    *,
    request_key: str,
    content_digest: str,
    mode: str,
    image: str,
    image_digest: str,
    platform: str,
    gated: bool,
    lock_path: Optional[str] = None,
    conda_lock_path: Optional[str] = None,
    tarball: Optional[str] = None,
    hpc: Optional[dict] = None,
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the artifact record stored in the EnvCache and returned by
    freeze(). `image` is the shipping handle (adopted digest ref or built tag);
    `image_digest` its immutable content id; redistributable is derived from
    `gated` (the I13 firewall)."""
    from datetime import datetime, timezone
    return {
        "request_key":     request_key,
        "content_digest":  content_digest,
        "mode":            mode,                 # "adopt" | "build"
        "image":           image,
        "image_digest":    image_digest,
        "platform":        platform,
        "gated":           gated,
        "redistributable": not gated,
        "lock":            lock_path,
        "conda_lock":      conda_lock_path,
        "tarball":         tarball,
        "hpc_delivery":    hpc or {},
        "created_at":      created_at or datetime.now(timezone.utc).isoformat(),
    }


class EnvCache:
    """Persisted request_key → artifact-record map. The store that makes
    'solve once, pull by digest' real: a cache hit returns the content_digest +
    image without re-solving the env."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def lookup(self, key: str) -> Optional[dict]:
        return self._load().get(key)

    def register(self, key: str, record: dict) -> dict:
        data = self._load()
        data[key] = record
        self._save(data)
        return record

    def all(self) -> dict:
        return self._load()
