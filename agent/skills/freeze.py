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
    """Content digest of an env from its FINALIZED spec dict (packages[] + lock_sha256).

    NOTE: this reads derived/finalized fields (packages[], lock_sha256) that a LIVE
    DRAFT does not carry — a draft holds install_steps[].installed_packages and has
    no lock yet. On a draft it therefore collapses to a constant (empty parts → one
    digest for every env), so it must NOT be used as the freeze record's anchor on
    the container-native path. Use record_content_digest() with the EnvBuild digest
    instead — see that function."""
    return compute_content_digest(content_digest_parts(spec))


def record_content_digest(mode: str, *, build_digest: str = "", adopt_digest: str = "",
                          fallback: str = "") -> str:
    """The AUTHORITATIVE 'what was GOT' content anchor for a frozen env, by mode:

      build → the EnvBuild lock+longtail+platform+engine digest (unique per env,
              reproducible by rebuild; the SAME digest recorded in the recipe and
              checked by verify-by-rebuild).
      adopt → the adopted biocontainer's manifest digest (content-addressed by the
              registry, reproducible by re-pull).

    `fallback` (a request-based hash) is used only if the mode-specific digest is
    missing. This replaces content_digest_from_spec(draft) on the freeze path, which
    read finalized-only fields a live draft lacks and so produced ONE constant digest
    for every container-native build (a false content collision across distinct
    envs)."""
    if mode == "build" and build_digest:
        return build_digest
    if mode == "adopt" and adopt_digest:
        return adopt_digest
    return fallback


def _packages(spec: dict) -> list[dict]:
    return [p for p in (spec.get("packages") or []) if isinstance(p, dict)]


def installed_packages(spec: dict, *, successful_only: bool = False) -> list[dict]:
    """Every package record in a draft OR finalized spec. A finalized spec carries
    a derived packages[]; a LIVE DRAFT does not (packages[] is derived only at
    finalize) — it only has install_steps[].installed_packages. Union both and
    dedup by name with MOVE-TO-END semantics: when a later install_step provides
    a package already seen, the prior entry is REMOVED and the new one inserted
    at the new position. This is what makes a smart-replaced retry land AFTER
    its newly-added dependency in the iteration order (the GAPIT/snpStats
    scenario: agent fails GAPIT, installs snpStats, retries GAPIT — the
    successful retry's entry takes a position AFTER snpStats, not before).

    `successful_only`: skip install_steps with returncode != 0. Used selectively
    — NOT by non_conda_installs (the adopt-vs-build decision needs to see ALL
    declared non-conda installs regardless of host-verify outcome; a wrong-arch
    linux binary on a Mac host fails host verify by design but IS still a
    non-conda install that forbids biocontainer adopt). Move-to-end dedup means
    a successful retry naturally supersedes the failed attempt for the same
    package, with no need to filter the failed entry out — that's the simpler
    invariant. Default False keeps the inventory view that includes failed
    attempts (useful for reporting + diagnostics)."""
    out: dict[str, dict] = {}
    for st in (spec.get("install_steps") or []):
        if not isinstance(st, dict):
            continue
        if successful_only and st.get("returncode") not in (0, None):
            # rc==0 is success; rc==None occurs for steps without a returncode
            # field (e.g. create_conda_env records no rc) — those are skipped
            # neither way: they don't carry installed_packages of interest to
            # the replay (conda env CREATE, not INSTALL).
            continue
        for ip in (st.get("installed_packages") or []):
            if isinstance(ip, dict) and ip.get("name"):
                # Move-to-end: if we've seen this name, remove it first so the
                # new entry lands at the END of the dict's iteration order.
                # Python dicts preserve insertion order; del+set effectively
                # repositions. This is what makes a successful retry sit AFTER
                # any intervening successful installs in the replay order.
                if ip["name"] in out:
                    del out[ip["name"]]
                out[ip["name"]] = ip
    for p in _packages(spec):
        if p.get("name"):
            if p["name"] in out:
                del out[p["name"]]
            out[p["name"]] = p
    return list(out.values())


_ENV_MUTATING_RE = __import__("re").compile(
    r"(?:^|[\s;&|`(])(?:python\s+-m\s+)?pip3?\s+install\b"
    r"|(?:^|[\s;&|`(])(?:mamba|micromamba|conda)\s+install\b"
    r"|(?:^|[\s;&|`(])(?:mamba|micromamba|conda)\s+env\s+update\b",
    __import__("re").IGNORECASE,
)


def env_mutating_pipeline_steps(spec: dict) -> list[dict]:
    """Pipeline_steps whose command MUTATED the env outside the structured
    install primitives — typically `pip install …` via run_in_env, which lands
    in `pipeline_steps` (not `install_steps`).

    The freeze adopt-vs-build decision queries this alongside non_conda_installs.
    Without it the decision goes blind on a class of env mutations: a pip-install
    via run_in_env doesn't surface as an install_step, so non_conda_installs
    returns empty, the gate fires "pure conda → adopt biocontainer", and freeze
    silently ships a BioContainer that omits whatever pip just installed.
    (pysam-stress demonstrated this end-to-end: a host-source-built pysam==0.24.0
    via run_in_env got freeze-adopted as a pre-built pysam==0.23.3 biocontainer.)

    Matches the common shapes: `pip install foo`, `python -m pip install foo`,
    `conda install …`, `mamba install …`, `micromamba install …`, conda env
    updates. NOT a deep parse — a substring match is enough for "this command
    mutated the env". A false-positive forces a container-native build instead
    of adopt, which is the SAFE direction (we never wrongly adopt; we may
    occasionally rebuild when adopt was actually fine).

    Pure / no network; the same module's adopt-vs-build inputs live here so
    the policy is one-stop."""
    out: list[dict] = []
    for st in (spec.get("pipeline_steps") or []):
        if not isinstance(st, dict):
            continue
        cmd = st.get("command", "") or ""
        if _ENV_MUTATING_RE.search(cmd):
            out.append(st)
    return out


def non_conda_installs(spec: dict) -> list[dict]:
    """Packages installed by anything OTHER than conda (binary/source/jar/perl/
    cargo/go). These cannot be represented by adopting a bioconda biocontainer
    (it only knows conda packages), and cannot be conda-packed cross-arch — so
    their presence forces a recipe build that replays them on the ship platform.
    Returns [{name, type, install_method}].

    Does NOT filter by returncode — and this matters: the freeze adopt-or-build
    decision queries this function. A linux binary installed on a Mac host
    fails the post-install host verify (wrong-arch "cannot execute binary
    file"), and the install_step records rc=1 — but the install_method itself
    is valid (sha256-anchored URL) and the container-native build will execute
    it correctly. If we filtered failed steps out here, the adopt decision
    would see an empty non_conda list and silently adopt a biocontainer (which
    may have a DIFFERENT version of the tool than what the user pinned). That
    is a trust violation: shipping a different artifact than what was requested.

    Move-to-end dedup in installed_packages handles the retry-after-dep-fix
    scenario without needing a filter: a successful retry takes the LAST
    position and is the entry seen here, with the failed earlier entry
    superseded."""
    out = []
    for p in installed_packages(spec):
        im = p.get("install_method") if isinstance(p.get("install_method"), dict) else {}
        t = im.get("type") or "conda"
        if t not in ("conda", "docker_pull"):
            out.append({"name": p.get("name", ""), "type": t, "install_method": im})
    return out


def has_conda_packages(spec: dict) -> bool:
    """True if any TOOL was installed via conda (so the recipe build needs a
    conda layer). The bootstrap python from create_conda_env is scaffolding, not
    a tool — it lands in the 'conda create' step and must NOT trigger a conda
    layer. So we look for a real 'conda install' step (install_conda_packages),
    or, in a finalized spec, a package with install_method.type == conda."""
    for st in (spec.get("install_steps") or []):
        if (isinstance(st, dict) and st.get("tool") == "conda"
                and st.get("subcommand") == "install" and st.get("installed_packages")):
            return True
    for p in _packages(spec):
        im = p.get("install_method") if isinstance(p.get("install_method"), dict) else {}
        if im.get("type") == "conda":
            return True
    return False


def requested_conda_specs(spec: dict) -> list[str]:
    """The conda specs the agent EXPLICITLY asked for — install_conda_packages
    steps (tool==conda, subcommand==install) as 'name=version' / 'name'. EXCLUDES
    the bootstrap python from create_conda_env (a 'create' step: scaffolding, not a
    requested tool), matching has_conda_packages's view. This is the TOP-LEVEL
    request the container-native build re-solves from via the engine — not the full
    dependency closure (the engine resolves that; the in-image lock content-addresses
    what was actually got)."""
    out: list[str] = []
    for st in (spec.get("install_steps") or []):
        if not isinstance(st, dict):
            continue
        if st.get("tool") == "conda" and st.get("subcommand") == "install":
            for ip in (st.get("installed_packages") or []):
                if isinstance(ip, dict) and ip.get("name"):
                    v = ip.get("version")
                    out.append(f"{ip['name']}={v}" if v else ip["name"])
    return out


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

    def lookup_anchored(self, key: str, image_present) -> Optional[dict]:
        """A cache hit RE-ANCHORED against reality: returns the record only if its
        image is still present per `image_present(ref) -> bool`. The cache spans
        events (unlike EnvBuild.run(), which verifies on live calls in one pass), so
        a hit is a claim until re-checked — the container-native analog of anchoring
        docker_status to `docker image inspect` at finalize. An evicted image is
        treated as a MISS (None) so the caller rebuilds rather than shipping a
        dangling reference. `image_present` is injected to keep this module network-
        free (a face supplies the docker-backed check)."""
        rec = self.lookup(key)
        if not rec:
            return None
        ref = rec.get("image") or rec.get("image_digest") or ""
        return rec if (ref and image_present(ref)) else None

    def register(self, key: str, record: dict) -> dict:
        data = self._load()
        data[key] = record
        self._save(data)
        return record

    def all(self) -> dict:
        return self._load()
