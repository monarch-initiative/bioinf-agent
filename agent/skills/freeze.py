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
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from agent.skills import store_lock as _store_lock


# Platform-spelling canonicalization (D6 fix). The cache contained BOTH
# 'linux-64' (conda form, freeze()'s public default) and 'linux/amd64' (docker
# form, EnvBuild's internal default) as DISTINCT keys for the same logical
# artifact — two writers, two slots, no shared cache. Canonicalize to ONE form
# at request_key time so both writers land in the same slot.
_PLATFORM_CANON = {
    "linux-64":      "linux/amd64",
    "linux/amd64":   "linux/amd64",
    "linux-aarch64": "linux/arm64",
    "linux-arm64":   "linux/arm64",
    "linux/arm64":   "linux/arm64",
    "osx-64":        "darwin/amd64",
    "darwin/amd64":  "darwin/amd64",
    "osx-arm64":     "darwin/arm64",
    "darwin/arm64":  "darwin/arm64",
}


def canon_platform(platform: str) -> str:
    """Canonicalize a platform spelling. Conda form ('linux-64') and Docker form
    ('linux/amd64') resolve to the same canonical token (the Docker form, since
    that's what container_build actually receives). Pre-canonicalization the
    cache had both as distinct keys — visible in dorado-stress as a duplicate
    samtools=1.21|linux-64 / samtools=1.21|linux/amd64 entry. Unknown platforms
    pass through unchanged so we never silently rewrite a caller's literal."""
    p = (platform or "").strip()
    return _PLATFORM_CANON.get(p, p)


def request_key(tools: list[tuple[str, str]], platform: str, accel: str = "none",
                *, gated: bool = False, accel_policy: Optional[dict] = None,
                licenses: Optional[list[str]] = None) -> str:
    """Canonical lookup handle for 'what was asked for'. Order-independent in
    tools; canonical in platform; sensitive to every POLICY facet that produces
    a materially different artifact.

    The minimal 3-part form `{tools}|{platform}|{accel}` was POLICY-BLIND
    (D5 stress finding): two callers asking for the same tools+platform+accel
    but different gated/license/accelerator-toolkit policy collided on one
    key, so the SECOND caller got the FIRST's artifact back — across a policy
    boundary. Adding the policy facets to the key:

      gated=True              → adds 'gated' segment (license firewall side)
      accel_policy dict       → adds 'accel:<12-char-hash>' over
                                {toolkit_version, runtime, min_driver_version,
                                 compute_capability} — the metadata that
                                actually differentiates cuda 12.1 vs 12.8 or
                                runtime_verified vs build_only.
      licenses[]              → adds 'lic:<12-char-hash>' over sorted(licenses)

    Default (no policy supplied) keeps the bare 3-part form (back-compat with
    every existing caller that never used policy gates).
    """
    spec = ",".join(f"{n}={v}" if v else n for n, v in sorted(tools))
    canon_p = canon_platform(platform)
    parts = [spec, canon_p, accel or "none"]
    if gated:
        parts.append("gated")
    if accel_policy and isinstance(accel_policy, dict):
        meta = {k: accel_policy.get(k, "")
                for k in ("toolkit_version", "runtime", "min_driver_version",
                          "compute_capability")}
        if any(meta.values()):
            parts.append("accel:" + hashlib.sha256(
                json.dumps(meta, sort_keys=True).encode()).hexdigest()[:12])
    if licenses:
        canon_lic = "\n".join(sorted(s.strip() for s in licenses if (s or "").strip()))
        if canon_lic:
            parts.append("lic:" + hashlib.sha256(canon_lic.encode()).hexdigest()[:12])
    return "|".join(parts)


def compute_content_digest(parts: dict) -> str:
    """sha256 over a canonicalized identity dict → 'sha256:…'. Stable across
    key order and process runs (json sort_keys)."""
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


def record_content_digest(mode: str, *, build_digest: str = "", adopt_digest: str = "",
                          fallback: str = "") -> str:
    """The AUTHORITATIVE 'what was GOT' content anchor for a frozen env, by mode:

      build → the EnvBuild lock+longtail+platform+engine digest (unique per env,
              reproducible by rebuild; the SAME digest recorded in the recipe and
              checked by verify-by-rebuild).
      adopt → the adopted biocontainer's manifest digest (content-addressed by the
              registry, reproducible by re-pull).

    `fallback` (a request-based hash) is used only if the mode-specific digest is
    missing.

    WHY THIS SHAPE (a real regression, kept as a warning): the superseded
    finalized-spec digest read packages[]/lock_sha256 — fields a live draft does
    not have — so it hashed the same empty view for every container-native build
    and returned ONE constant digest for four distinct envs. A content anchor that
    collides is worse than none: it silently declares different envs identical.
    Anchor on what was actually GOT (the build/adopt digest), never on fields the
    caller may not have populated yet."""
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
        if t != "conda":
            out.append({"name": p.get("name", ""), "type": t, "install_method": im})
    return out


def requested_conda_specs(spec: dict) -> list[str]:
    """The conda specs the agent EXPLICITLY asked for — install_conda_packages
    steps (tool==conda, subcommand==install) as 'name=version' / 'name'. EXCLUDES
    the bootstrap python from create_conda_env (a 'create' step: scaffolding, not a
    requested tool). This is the TOP-LEVEL
    request the container-native build re-solves from via the engine — not the full
    dependency closure (the engine resolves that; the in-image lock content-addresses
    what was actually got).

    R6 fix (batch-2 stress, 2026-05-27): filter rc!=0 install_steps AND apply
    move-to-end dedup by package name — mirroring installed_packages's semantics
    (the canonical inventory view used everywhere else). Pre-fix a failed-then-
    retried conda install surfaced both entries (potentially with different
    versions), so the engine got asked to install both — at best a name conflict,
    at worst the engine picking the failed version. The retry pattern (smart-
    replace step after a missing-dep fix) is the load-bearing case the R/GAPIT
    stress hit. install_steps with rc=None pass through (a 'create' step has no
    rc field but also no installed_packages of interest, so this is a no-op for
    them; the conditional explicitly only iterates `tool==conda subcommand==install`)."""
    out: dict[str, str] = {}   # name → 'name=version' / 'name' (move-to-end by name)
    for st in (spec.get("install_steps") or []):
        if not isinstance(st, dict):
            continue
        if st.get("tool") != "conda" or st.get("subcommand") != "install":
            continue
        # rc==0 success; rc==None means no rc was recorded (treat as pass-through
        # for backward compat with older drafts that didn't set it); rc != 0
        # means the install failed — its installed_packages claim is unsafe to
        # feed back to the engine as a request. The retry's rc==0 entry will
        # supersede via move-to-end.
        rc = st.get("returncode")
        if rc is not None and rc != 0:
            continue
        for ip in (st.get("installed_packages") or []):
            if isinstance(ip, dict) and ip.get("name"):
                v = ip.get("version")
                # Move-to-end: del the prior entry so the new one lands at the
                # END of the dict's iteration order (Python dicts preserve
                # insertion order). A successful retry sits AFTER any newly-
                # added deps in the replay order — same invariant as
                # installed_packages.
                if ip["name"] in out:
                    del out[ip["name"]]
                out[ip["name"]] = f"{ip['name']}={v}" if v else ip["name"]
    return list(out.values())


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
    image_arch: Optional[str] = None,
    lock_path: Optional[str] = None,
    conda_lock_path: Optional[str] = None,
    tarball: Optional[str] = None,
    hpc: Optional[dict] = None,
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the artifact record stored in the EnvCache and returned by
    freeze(). `image` is the shipping handle (adopted digest ref or built tag);
    `image_digest` its immutable content id; redistributable is derived from
    `gated` (the I13 firewall).

    `image_arch` is the architecture READ OFF the shipped image (locus.image_arch),
    as distinct from `platform`, which is what the caller ASKED FOR. Keeping both is
    the whole point: BUILT.platform compares them, and it can only do that because
    they have separate provenance. Pass None when nothing looked — the contract reads
    an absent key as UNOBSERVED and refuses to treat it as agreement. An empty STRING
    is a real observation with its own meaning (the digest is a manifest index and
    names no architecture), so the two are not interchangeable."""
    from datetime import datetime, timezone
    rec = {
        "request_key":     request_key,
        "content_digest":  content_digest,
        "mode":            mode,                 # "adopt" | "build"
        "image":           image,
        "image_digest":    image_digest,
        "platform":        platform,
        # CANONICAL NAME: `license_gated` — the same key env_honesty._check_license reads
        # and the same field name on the pydantic model (core_data.PipelineSpec). This used
        # to be emitted as `gated`, which silently disabled I13 on the ONE path that hands
        # the cache record straight to check_build (freeze_from_image / the authors' path):
        # the contract read `license_gated`, the record only had `gated`, so a gated
        # artifact with no licenses[] registered clean (audit 2026-07-16). One concept,
        # one name. Records written before this carry `gated` — read via record_is_gated().
        "license_gated":   gated,
        "redistributable": not gated,
        "lock":            lock_path,
        "conda_lock":      conda_lock_path,
        "tarball":         tarball,
        "hpc_delivery":    hpc or {},
        "created_at":      created_at or datetime.now(timezone.utc).isoformat(),
    }
    if image_arch is not None:
        rec["image_arch"] = image_arch
    return rec


class EnvCache:
    """Persisted request_key → artifact-record map. The store that makes
    'solve once, pull by digest' real: a cache hit returns the content_digest +
    image without re-solving the env."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict:
        """Read the cache, FAILING LOUD on a corrupt file rather than swallowing it
        to {}. A missing file is a legitimate empty cache and returns {}; a file that
        EXISTS but doesn't parse is corruption, and must stop the world.

        Silently returning {} here was the second half of a silent-data-loss bug: a
        corrupt read → {} → the next register() writes {only_the_new_key} over the
        (now-empty) in-memory dict and every previously frozen env record is gone,
        with no exception ever raised. `solve once, pull by digest` cannot rest on a
        store that erases itself on the first bad byte. Refusing here (paired with the
        atomic _save below, which prevents our own writes from ever producing the bad
        byte) means a corrupt cache is surfaced for a human to inspect — never
        rewritten out from under the records it still holds."""
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text()
        except OSError as e:
            raise RuntimeError(
                f"EnvCache: cannot read the frozen-env cache at {self.path}: {e}. "
                f"Refusing to proceed rather than treat it as empty (which would "
                f"overwrite it on the next freeze and lose every recorded env)."
            ) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"EnvCache: the frozen-env cache at {self.path} is corrupt / not "
                f"valid JSON ({e}). Inspect or remove it before continuing — "
                f"proceeding would rewrite it with only the next env and silently "
                f"lose the rest of the recorded envs."
            ) from e
        if not isinstance(data, dict):
            raise RuntimeError(
                f"EnvCache: the frozen-env cache at {self.path} parsed to a "
                f"{type(data).__name__}, not a JSON object. Inspect or remove it "
                f"before continuing."
            )
        return data

    def _save(self, data: dict) -> None:
        """Write the cache ATOMICALLY: a crash mid-write must never truncate the live
        file. We write a sibling temp file, fsync it, then os.replace() into place —
        an atomic rename on POSIX, so any reader (including a concurrent freeze) sees
        either the whole old file or the whole new file, never a half-written one.
        The plain write_text() this replaced could leave a truncated/corrupt file if
        the process died between truncate and full write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def lookup(self, key: str) -> Optional[dict]:
        return self._load().get(key)

    def contract_report(self, record: dict):
        """The Layer-1 honesty contract, re-run against a STORED record — in FULL
        (violations + per-clause coverage). Returns an `env_honesty.BuildContract`.

        The contract is a pure function of the record, so the question "would this
        artifact be accepted if we froze it today?" is answerable at any time — and
        must be, because a cache record is a claim that outlives the event that
        made it. Kept here (not inlined at each call site) so there is exactly ONE
        answer to "is this cached artifact trustworthy?"; the audit's whole finding
        was one concept re-derived in N places and drifting in N-1 of them."""
        from agent.skills import env_honesty   # re + typing + models (leaf); no import cycle
        return env_honesty.evaluate_build(record)

    def contract_violations(self, record: dict) -> list[dict]:
        """Just the objections — the GATE half of `contract_report`. Serving paths that
        decide pass/fail use this; anything that REPORTS the contract to a human should
        take `contract_report` instead, so it can distinguish a clause that checked three
        things from a clause that had nothing to check."""
        return self.contract_report(record).violations

    def lookup_anchored(self, key: str, image_present) -> Optional[dict]:
        """A cache hit RE-ANCHORED against reality: the record is returned only if
        it still satisfies the SAME contract a fresh freeze must satisfy — BUILT
        (its image is still present per `image_present(ref) -> bool`) AND
        VALIDATED_IN_IMAGE AND POLICY_CLEAN. Anything else is a MISS (None), so the
        caller rebuilds rather than serving a claim it can no longer support.

        The cache spans events (unlike EnvBuild.run(), which verifies on live calls
        in one pass), so a hit is a claim until re-checked. This used to re-check
        only image PRESENCE, which made the contract retroactively unenforceable:
        `samtools=1.21` was registered pre-Tier-2 with `verifications: []`, fails
        `check_build` today, and was still served as `proven` on every hit — the
        adopt-path validation gate was live in the code and absent in effect for
        every env that already existed (audit 2026-07-16).

        Re-anchoring the FULL contract is what makes a strengthened gate apply to
        artifacts frozen before it existed: a record that can no longer earn its
        green is simply re-earned by a rebuild. `image_present` is injected to keep
        this module network-free (a face supplies the docker-backed check)."""
        rec = self.lookup(key)
        if not rec:
            return None
        ref = rec.get("image") or rec.get("image_digest") or ""
        if not (ref and image_present(ref)):
            return None
        return None if self.contract_violations(rec) else rec

    def lookup_verified(self, key: str) -> tuple[Optional[dict], list[dict]]:
        """The SERVING question — "is there a record here I can honestly hand out?"

        Returns (record, violations). `lookup()` answers a different question ("is
        there a record?") and stays raw for diagnostics/listing, which must show
        what is really on disk. Consumers that RUN, SHIP, or PIN an env ask this
        one instead, and report `violations` rather than pretending the record was
        missing — a refusal that misnames its own reason is the same disease.
        Presence of the image is not re-checked here: these callers touch the image
        directly and fail loudly on their own when it is gone."""
        rec = self.lookup(key)
        if not rec:
            return None, []
        violations = self.contract_violations(rec)
        return (None, violations) if violations else (rec, [])

    def register(self, key: str, record: dict) -> dict:
        """Write a freeze record to the cache — and the ONE place Layer 1 asserts the
        record's SHAPE, the analog of `spec_writer.py`'s `model_validate` for Layer 2.

        This used to be a pure passthrough (`data[key] = record; self._save(data)`),
        and that is precisely how three producers came to write three key-dialects of
        `shipped_binaries` that four readers each mis-read differently. Every write
        path — `freeze`, `freeze_from_image`, `build_env_from_authors_recipe` —
        converges here, so a declaration here binds all of them at once.

        RAISES on a malformed record rather than returning a violation: a producer
        emitting a shape it never declared is OUR bug, not a user's env failing a
        policy gate. It must be loud at the seam and impossible to serve. Records that
        FAIL policy still register (that is what `contract_violations` is for) — this
        gate is strictly about "can this record be read at all".

        LOCKED read-modify-write. `_save` being atomic prevents a TORN file; it does
        nothing about a LOST UPDATE, where two writers read the same snapshot and the
        second `os.replace` discards the first's record. Having atomicity is what made
        the absence of exclusion hard to see: the file always parses. Measured — 3
        processes x 60 registers left 84 of 180 records, so 96 frozen envs (each a real
        image built at real cost) vanished with no error anywhere.

        The window is open in ordinary single-agent use: `freeze(background=True)`
        spawns a detached subprocess that registers into this same file while its parent
        keeps working. Two background freezes is a documented way to drive this system.

        The shape check stays OUTSIDE the lock — it touches only the argument, and a
        malformed record must raise without ever contending for the file.
        """
        self._validate_shape(record)
        with _store_lock.locked(self.path):
            return self._register_locked(key, record)

    def _register_locked(self, key: str, record: dict) -> dict:
        data = self._load()
        # Keyed by request_key (the REQUEST's content address): re-freezing an
        # identical request refreshes its own cache entry with an equivalent record
        # — a solve-once refresh, not provenance loss. What a sealed WorkflowSpec
        # actually depends on is the env IMAGE, pinned BY DIGEST (immutable in the
        # Docker daemon), independent of this cache. So Layer 1 needs no analog of
        # the seal write-guard here; Layer-2 provenance is protected at the seal
        # WRITE (workflow_tools._guard_spec_overwrite), where re-sealing over a
        # DIFFERENT-digest env is the real silent-replacement risk (Phase-3 Piece A).
        data[key] = record
        self._save(data)
        return record

    @staticmethod
    def _validate_shape(record: dict) -> None:
        """Sub-records with a declared model must conform BEFORE they reach disk.
        Kept separate from `contract_violations` (policy, returns violations) because
        this is a producer bug and must raise. Patch `_save` — the I/O — in tests, not
        `register`; patching `register` patches out the contract itself."""
        from agent.models.core_data import shipped_binaries, tool_identities
        shipped_binaries(record)   # raises ValidationError on an undeclared dialect
        tool_identities(record)    # identity disclosure (audit #8) — same forbid-extras seam

    def all(self) -> dict:
        return self._load()
