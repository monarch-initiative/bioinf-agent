"""freeze_tools — the Layer-1 deliverable surface.

Three tools:
  freeze              — build (or adopt) the content-addressed, HPC-shippable
                        env image; emit ENV.html + attestation.json + recipe.yaml.
  verify_env_recipe   — rebuild from a recipe alone, check the digest converges.
  generate_user_guide — render the Layer-2 user guide from a validated run.

freeze() is the biggest single tool in the codebase (~420 LOC) — it coordinates
the adopt-or-build decision tree, honesty contract checks, Apptainer delivery,
registry push, and three deliverable renderers. All its helpers live in
mcp_server.py (read via `_ms.X`) so this submodule stays purely a tool surface.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# IMPORT-BINDING: see workflow_tools.py — singletons go through `_ms.X`
# so test monkeypatching on mcp_server reaches us.
from agent import mcp_server as _ms
from agent.mcp_server import mcp, StrList, OptStrList  # never monkeypatched
from agent.models.core_data import ShippedBinary as _ShippedBinary
from agent.skills.outcomes import proven, refused, broke


def _validate_tools_in_image(image: str, platform: str, tools: list) -> list[dict]:
    """Run each tool's presence evidence INSIDE `image` and return the verification rows
    env_honesty.check_build consumes: [{label, tool, check, rc, passed, out}].

    Extracted for the ADOPT path, which previously validated nothing at all. It generates
    the SAME probe the build path uses (env_freeze._conda_presence_check) and runs it in
    the SAME way — so "adopted" and "built" are held to one contract rather than two, and
    an adopted image proves it carries the tool instead of being taken on the registry's
    word.

    Extracted rather than routed through freeze_from_image on purpose: that function
    computes its own request_key from tools[0] (dropping the policy facets this key folds
    in), writes no hpc_delivery, and records no validation_locus — reusing it would have
    quietly regressed the adopt record's shape to buy code-sharing.
    """
    rows: list[dict] = []
    for tool in tools:
        check = _ms._env_freeze._conda_presence_check(tool)
        try:
            r = _ms._docker.run_in_container(image, check, platform=platform, timeout=300)
            rc = r.get("returncode", 1)
            out = (r.get("stdout") or r.get("stderr") or "")[:400]
        except Exception as e:
            rc, out = 1, f"{type(e).__name__}: {e}"
        rows.append({"label": tool, "tool": tool, "check": check,
                     "rc": rc, "passed": rc == 0, "out": out})
    return rows


def _shipped_binary_entry(step: dict) -> dict:
    """One `shipped_binaries[]` record from a baked long-tail step, as the declared
    `ShippedBinary` shape. The baked command IS this tier's provenance; C5 surfaces
    the per-tool SHIP assurance (verified/assurance/tier) whenever the tier disclosed
    one — so the ENV report states HOW each shipped tool is anchored across ALL tiers,
    not just binary.

    Fields state absence explicitly rather than defaulting it (see `ShippedBinary`):
    `version` is None because this tier does not probe the built binary for one —
    "we did not capture it" is the honest record, and a reader must render it as
    absence, never scrape a number out of a banner to fill the hole.

    `tool` (the PATH command) and `install_command` (the literal baked shell line)
    used to share one `command` key with opposite meanings across the two producers,
    which is what rendered `bcftools` inside a <pre> shell block under the label
    "tool". `purpose` stays as the human label; `step["tool"]` is now carried from the
    generator that computed it (audit 2026-07-16)."""
    prov = step.get("provenance") or {}
    return _ShippedBinary(
        tool=step.get("tool") or "",
        version=None,
        provenance=step.get("purpose") or None,
        install_command=step.get("command") or None,
        tier=prov.get("tier") if prov.get("assurance") else None,
        verified=bool(prov.get("verified")) if prov.get("assurance") else None,
        assurance=prov.get("assurance") or None,
    ).model_dump()


@mcp.tool()
def freeze(
    env_name: str,
    tools: StrList,
    pipeline_name: str = "",
    pipeline_description: str = "",
    version: str = "",
    platform: str = "linux-64",
    accel: str = "none",
    gated: bool = False,
    licenses: OptStrList = None,
    push_target: str = "",
    gpu_required: bool = False,
    cuda_version: str = "",
    pipeline_id: str = "",
    background: bool = False,
) -> dict:
    """Freeze an env into a content-addressed, HPC-shippable artifact (Slice 5).

    The re-spine's env-artifact primitive — adopt a pre-built BioContainer by
    digest when one exists, else build our own, and emit the Apptainer/HPC
    delivery contract. Steps:

      1. request_key (what was ASKED) → EnvCache lookup; a hit returns the
         proven artifact by hash with NO re-solve (the scale unlock).
      2. content_digest (what was GOT): lock + source/binary/artifact hashes +
         platform + accel (from the draft when pipeline_id is given).
      3. ADOPT-or-BUILD:
         - pure conda + a published biocontainer → ADOPT it BY DIGEST (no build).
         - everything else → CONTAINER-NATIVE BUILD via env_freeze: install +
           validate IN the ship image (one generic bake, validated==shipped). Covers
           hand-installed tools (binary/jar/source/cargo/go/perl), pure conda, and
           pip/R — cross-arch too (built in-container; no host-arch conda-pack and no
           cross-arch refusal). A biocontainer is NOT adopted when the env has non-
           conda installs it can't represent (that would ship a different artifact).
           Gated builds must pass `licenses` (I13).
      4. HPC delivery: adopted → `apptainer pull docker://…@digest`. Built →
         registry-free by DEFAULT (`docker save` tarball + `apptainer build
         docker-archive://…`, zero _ms.config/auth). If a default registry is
         configured (_ms.config docker.registry) OR a push_target is passed, the built
         image is pushed and delivery becomes `apptainer pull docker://…`; a push
         failure falls back to the tarball and is reported in push_status. Gated
         images are NEVER pushed (I13 — tarball-only).
      5. EnvCache.register(request_key → record).

    `tools` are the PRIMARY requested tools (e.g. ['samtools=1.21',
    'bwa=0.7.17']) — what biocontainer/mulled adoption matches on, NOT the full
    dependency closure. Returns {mode, image, image_digest, content_digest,
    request_key, hpc_delivery, cache_hit, …}.

    `background` (W1 mitigation): when True, spawns freeze in a detached
    subprocess via JobManager and returns IMMEDIATELY with a job_id. Use this
    for large builds (CUDA tools, ML/AI models, big release binaries) where the
    synchronous in-call time would exceed the MCP stream-watchdog window (~10 min).
    The subprocess writes the full freeze result JSON to
    `data/jobs/{job_id}.result.json` when it finishes; poll `check_job(job_id)`
    until state=='exited', then read that file. The standard env_report/attestation/
    recipe artifacts are written by the subprocess too, on success.
    """
    if background:
        return _ms._freeze_in_background(
            env_name=env_name, tools=tools, pipeline_name=pipeline_name,
            pipeline_description=pipeline_description, version=version,
            platform=platform, accel=accel, gated=gated,
            licenses=list(licenses or []), push_target=push_target,
            gpu_required=gpu_required, cuda_version=cuda_version,
            pipeline_id=pipeline_id,
        )
    # A1 failsafe (Apollo3 stress, batch-2): disk pre-check. 4 parallel freezes
    # piled buildkit intermediates to 80 GB / 92% capacity → infinite retry loop.
    # Refuse here with a diagnostic that names the cleanup command, BEFORE any
    # cache lookup or docker work. The threshold IS the concurrency safety:
    # parallel freezes that race for disk now refuse cleanly instead of wedging.
    _disk_refusal = _ms._check_disk_failsafe()
    if _disk_refusal:
        return _disk_refusal
    parsed = _ms._freeze.parse_tools(tools)
    # F1 fix (Batch 2): the licenses surface has TWO entry points — the freeze()
    # `licenses=[...]` kwarg AND patch_pipeline({licenses, license_gated, …}) on
    # the draft. An agent that diligently called patch_pipeline but forgot to
    # re-pass licenses on freeze() would land here with licenses=None, the I13
    # gate would refuse the gated build, and the diligence would look like a
    # bug. Merge: caller's licenses wins if non-empty; else fall back to the
    # draft's. Same merge for `gated`: an explicit gated=True from the caller
    # wins; absent caller intent, the draft's license_gated promotes us into
    # gated mode (so I13 still fires, but with the licenses the agent recorded).
    _draft_for_merge = _ms._pipeline_state.get_draft(pipeline_id) if pipeline_id else None
    if isinstance(_draft_for_merge, dict):
        if not (licenses or []) and _draft_for_merge.get("licenses"):
            licenses = list(_draft_for_merge.get("licenses") or [])
        if not gated and bool(_draft_for_merge.get("license_gated")):
            gated = True
    # F2 fix (Batch 2): I13 EARLY GATE. The honesty contract already refuses a
    # gated build with empty licenses[] (`I13.gated_license_recorded` in
    # env_honesty), but only AFTER the docker build finishes — the user pays
    # 10-30 min of build time to learn the artifact will be refused. Refuse
    # here, before request_key/cache lookup/docker work, with the same shape
    # the contract uses so the error is structurally indistinguishable.
    if gated and not (licenses or []):
        return refused("freeze.gated_no_license", success=False, stage="i13_early_gate",
                honesty_violations=[{
                    "invariant": "I13.gated_license_recorded", "where": "licenses",
                    "message": "license_gated=true requires at least one entry in licenses[] "
                               "naming the license/terms the artifact is bound by. Pass "
                               "licenses=[…] on freeze() (or patch_pipeline before freeze)."}],
                violation_count=1)
    # Bind the MCP scalar accel/cuda_version to a proper Accelerator policy dict so
    # the honesty contract (I12) can actually check it. A draft-supplied accelerator
    # wins (richer record); when absent and the caller passed accel="cuda" but no
    # patch_pipeline(accelerator=…), we synthesize the minimum and let I12 catch any
    # missing required metadata (toolkit_version, runtime_probe, min_driver_version) —
    # the right failure mode is REFUSE-WITH-DIAGNOSTIC, not a vacuous pass. This is
    # the dorado-stress D3 fix: previously `accel` only fed the cache key and never
    # reached _check_accelerator, so `accel="cuda"` on a draft with accelerator=None
    # produced a "POLICY_CLEAN — I12 passed" badge without I12 ever running.
    # Fetched BEFORE rkey so the cache key can include the policy facets (D5 fix)
    # AND the install-record version fill (B7 fix — see parsed_filled).
    # Reuses _draft_for_merge (read above for F1/F2 licenses merge) — same draft.
    draft = _draft_for_merge
    _draft_accel = draft.get("accelerator") if isinstance(draft, dict) else None
    effective_accel = _ms._synth_accelerator_from_request(accel, cuda_version, _draft_accel)
    # B7 fix (verification-driven, 2026-05-27): fill version slots from the
    # install record BEFORE computing the cache key. Pre-fix, parsed=[(busco,None)]
    # fed request_key (yielding `busco|linux/amd64|none`) but the adopt lookup
    # used the install-record-filled version separately. Two distinct envs
    # (busco==6.0.0 vs busco==5.8.3) collided on ONE cache key, so the second
    # freeze returned the FIRST's image — a wrong-version trust violation
    # isomorphic to the original B1, surfaced at a higher layer. Filling here
    # makes the cache key reflect what we'll actually adopt/build.
    parsed_filled = _ms._resolve_versions_from_install_record(parsed, draft)
    # D5 + D6 fix: request_key now folds in gated, accelerator-policy hash, and
    # the licenses set hash so policy-distinct artifacts don't collide on the cache
    # key. Platform is canonicalized inside request_key so conda-form ('linux-64')
    # and Docker-form ('linux/amd64') callers share ONE slot (D6).
    rkey = _ms._freeze.request_key(
        [(n, v or "") for n, v in parsed_filled], platform, accel,
        gated=gated, accel_policy=effective_accel, licenses=list(licenses or []),
    )

    # Re-anchor the cache against reality: a hit is a CLAIM until we confirm the
    # image is still present in the docker daemon. An evicted image (or one a user
    # `docker rmi`'d) was treated as a hit before — handing back a stale record,
    # no rebuild, no report re-render. lookup_anchored turns that into a MISS so
    # the build path runs, materializes a fresh image, and re-renders every
    # deliverable. (The container-native analog of the I8/I11 anchoring the host
    # path used to do for binaries / source clones.)
    def _docker_image_present(ref: str) -> bool:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
                           capture_output=True, text=True)
        return r.returncode == 0
    # cache lookup: lookup_anchored confirms the image is still in the docker
    # daemon (an evicted image gets re-built rather than returning a dangling
    # record). On hit we summarize the SBOM in the response only; the cached
    # record on disk keeps the full lists for env_report/attestation rendering.
    cached = _ms._env_cache.lookup_anchored(rkey, _docker_image_present)
    if cached:
        # Orientation pointer (Phase-3 Piece B) on the REUSE-BY-HASH branch too —
        # a cache hit is still a freeze, so current_state must read ENV_FROZEN for
        # it. Best-effort; a pointer hiccup never fails a freeze.
        if pipeline_id:
            try:
                _ms._pipeline_state.set_frozen_pointer(pipeline_id, rkey)
            except Exception:
                pass
        return _ms._summarize_sbom_in_response(
            # merge (not kwargs) so a business key already in `cached` (e.g.
            # request_key) can't collide with an explicit kwarg → TypeError.
            proven("freeze.cache_hit",
                   **{**cached, "success": True, "cache_hit": True, "request_key": rkey})
        )

    # A request-based FALLBACK anchor only. The authoritative content_digest is the
    # 'what was GOT' digest set per-branch below (the EnvBuild lock+longtail digest
    # for a build, the biocontainer manifest digest for an adopt) via
    # _ms._freeze.record_content_digest. The superseded finalized-spec digest read
    # fields (packages[]/lock_sha256) a live draft lacks, collapsing to one constant
    # for every container-native build — see record_content_digest's docstring.
    content_digest = _ms._freeze.compute_content_digest({
        "tools": sorted(f"{n}={v or ''}" for n, v in parsed),
        "platform": platform, "accel": accel,
    })

    name = pipeline_name or env_name
    sif = f"{name}.sif"
    # B1 + B7 fix: adopt lookup consumes `parsed_filled` (versions resolved from
    # install_steps[*].installed_packages above) — the same value that fed the
    # cache key, so the adopt-decision and the cache slot agree on what env
    # we're building. The install record is the trust anchor.
    adopt = _ms._biocontainers.resolve_biocontainer(parsed_filled)
    conda_lock_path = None
    shipped_binaries: list[dict] = []
    build_method = None
    validation_locus = "unknown"   # native | emulated | adopted — where we validated
    locus_advisory = ""
    env_recipe_dict = None         # the self-contained rebuild recipe (build path only)
    build_cd = ""                  # the EnvBuild content_digest (the authoritative build anchor)

    non_conda = _ms._freeze.non_conda_installs(draft) if draft else []
    # P3 fix: pipeline_steps that ran `pip install …` via run_in_env mutate the
    # env outside install_steps' structured surface — non_conda_installs misses
    # them, so the adopt gate would otherwise see "pure conda" and ship a
    # BioContainer that omits the pip install. (pysam-stress: host-built
    # pysam==0.24.0 via run_in_env → freeze adopted pysam==0.23.3 biocontainer.)
    env_mutators = _ms._freeze.env_mutating_pipeline_steps(draft) if draft else []
    has_env_mutations = bool(non_conda or env_mutators)
    can_adopt = bool(adopt.get("found") and adopt.get("image_by_digest") and not gated)

    # Registry push as a first-class delivery: an explicit push_target wins, else a
    # configured default registry (_ms.config docker.registry) auto-derives the ref so a
    # push happens with no per-call argument. Tarball→Apptainer stays the genuine
    # zero-_ms.config default (no registry configured → no push, no auth needed). Gated
    # artifacts are NEVER pushed (I13).
    default_registry = (_ms.config.get("docker", {}).get("registry") or "").strip()
    effective_push = _ms._effective_push_target(push_target, default_registry, name, version, gated)
    push_status = "not-configured"   # surfaced honestly: pushed / push-failed / skipped

    def _deliver_built(image):
        """docker-save tarball + registry push (when a target is configured) →
        Apptainer contract. Push failure falls back to the tarball but is reported,
        never silently swallowed."""
        nonlocal push_status
        idg = _ms._docker.image_digest(image)
        tar_path = _ms._env_mgr.project_root / "docker_images" / name / f"{name}.tar"
        save = _ms._docker.save_archive(image, tar_path)
        tball = save.get("tarball") if save.get("success") else None
        pushed = None
        if effective_push:
            tagged = _ms._docker._run(["docker", "tag", image, effective_push])["returncode"] == 0
            if tagged and _ms._docker._run(["docker", "push", effective_push], timeout=900)["returncode"] == 0:
                pushed, push_status = effective_push, f"pushed: {effective_push}"
            else:
                push_status = f"push-failed: {effective_push} (tarball fallback)"
        elif gated and (push_target or default_registry):
            push_status = "skipped: gated artifacts are never pushed (I13)"
        h = _ms._freeze.apptainer_delivery(mode="build", sif_name=sif, push_target=pushed,
                                       tarball=tball, gated=gated)
        return idg, tball, h

    if can_adopt and not has_env_mutations:
        # ADOPT — pure-conda env with a published biocontainer. The biocontainer
        # IS the artifact (provenance = its digest), so no conda-lock of our env.
        mode, image, image_digest, tarball = "adopt", adopt["image_by_digest"], adopt["digest"], None
        # ONE CONTRACT, not two (audit 2026-07-16 Tier 2). This branch used to call
        # `check_adopt`, a mode-aware variant that asserted BUILT + POLICY_CLEAN and
        # deliberately skipped VALIDATED_IN_IMAGE — "the biocontainer's bytes are trusted
        # by their published digest". That answered the wrong question. The risk was never
        # that bioconda lies about its own bytes; it is that WE bound the wrong image, and
        # only running the tool catches that. Adopt is also the DEFAULT for pure-conda, i.e.
        # the commonest env this system produces, so the one unvalidated path was also the
        # busiest one: `samtools=1.21` shipped with `verifications: []` and two sealed
        # workflows rest on it.
        #
        # Evidence is generated + run against the adopted image further down (where the
        # image has already been pulled for the SBOM), and check_build — the SAME contract
        # the build path answers to — is applied to the completed record. check_adopt is
        # deleted; its I13 clause was dead anyway, since `can_adopt` requires `not gated`.
        hpc = _ms._freeze.apptainer_delivery(mode="adopt", sif_name=sif,
                                         image_by_digest=adopt["image_by_digest"])
        # stays "unknown" until the in-image evidence has actually run (below), at which
        # point it takes the real native/emulated locus the build path records.

    else:
        # CONTAINER-NATIVE BUILD — the SINGLE build path (Phase E: freeze no longer
        # uses conda-pack at all). env_freeze installs + validates IN the ship image
        # (one generic bake, validated==shipped), covering hand-installed tools
        # (binary/jar/source/cargo/go/perl), pure conda, and pip/R — cross-arch too
        # (build in-container, no host-arch conda-pack and no cross-arch refusal).
        # A biocontainer is not adopted when the env has non-conda installs it can't
        # represent (adopting would ship a different, unvalidated artifact).
        if can_adopt:
            if non_conda:
                _skipped = ("env has non-conda installs a biocontainer cannot represent "
                            f"({len(non_conda)} install_step(s))")
            else:
                _skipped = (f"env has {len(env_mutators)} pipeline_step(s) that mutated the env "
                            "(e.g. `pip install …` via run_in_env) — a biocontainer cannot "
                            "represent these")
            adopt = {**adopt, "skipped": _skipped}
        docker_platform = _ms._CONDA_TO_DOCKER_PLATFORM.get(platform, platform)
        conda_deps = _ms._freeze.requested_conda_specs(draft) if draft else []
        if not conda_deps and not non_conda:
            # no draft (or no recorded conda installs): treat the requested tools as
            # conda specs (the declarative pure-conda case).
            conda_deps = [f"{n}={v}" if v else n for n, v in parsed]
        br = _ms._env_freeze.build_env_image(
            draft or {}, name=name, version=version, conda_deps=conda_deps,
            primary_tools=[n for n, _ in parsed], platform=docker_platform,
            accelerator=effective_accel,
            license_gated=gated, licenses=licenses, redistributable=not gated)
        if not br.get("success"):
            # build_env_image refuses pip/r_install with no generator, a non-replayable
            # source, or any honesty-contract violation (incl. I13: a gated build needs
            # licenses[]). Surface it verbatim.
            #
            # A2 failsafe (Apollo3 stress, batch-2): when the failure happened INSIDE
            # docker (stages: start/declare/install/freeze — pre-docker refusals like
            # resolve/route/map_install leave no layers), it may have parked
            # intermediate buildkit layers that compound across freezes. Best-effort
            # prune ONLY when free disk is already approaching the failsafe threshold
            # (1.5x the hard floor) — on a healthy disk we keep buildkit cache for
            # fast iteration on the next retry. The prune outcome rides on the
            # response so the operator can see what was cleaned (or why nothing was).
            _DOCKER_STAGES = {"start", "declare", "declare_locked", "declare_pypi",
                              "install", "freeze"}
            extra = {}
            if br.get("stage") in _DOCKER_STAGES:
                try:
                    import shutil as _sh
                    free_gb = _sh.disk_usage(str(_ms.PROJECT_ROOT)).free / (1024 ** 3)
                except Exception:
                    free_gb = None
                soft_threshold = _ms._FREEZE_MIN_DISK_GB_DEFAULT * 1.5
                if free_gb is not None and free_gb < soft_threshold:
                    extra["buildkit_prune"] = _ms._prune_buildkit_after_failure()
                    extra["buildkit_prune"]["reason"] = (
                        f"free disk {free_gb:.1f} GB below soft threshold "
                        f"{soft_threshold:.0f} GB — pruning to avoid cascade")
            return broke("freeze.container_build_failed", success=False,
                    stage="container_build", request_key=rkey,
                    adopt_attempt=adopt, build=br, **extra)
        mode, build_method, image = "build", "container-native", br["image"]
        image_digest = br["image_digest"]
        build_cd = br.get("content_digest", "")   # the real, unique, reproducible anchor
        validation_locus = br.get("validation_locus", "unknown")
        locus_advisory = br.get("locus_advisory", "")
        _, tarball, hpc = _deliver_built(image)   # docker-save tarball + Apptainer contract
        # record what shipped: each baked long-tail step (the command IS the provenance).
        # Each baked long-tail step (the command IS the provenance). A binary-tier
        # step also carries its install→ship assurance (verified/assurance) from
        # the integrity chain — surfaced so the artifact discloses whether each
        # shipped binary was checksum-verified (F1/F2), never conflating them.
        shipped_binaries = [_shipped_binary_entry(s) for s in br.get("longtail_steps", [])]
        # the SELF-CONTAINED rebuild recipe — everything build_env_image needs to
        # reproduce this env with no draft/agent (the verify-by-rebuild / CI artifact).
        # content_digest is the EnvBuild one (what a rebuild via build_env_image yields).
        env_recipe_dict = _ms._env_recipe.extract_recipe(
            draft, name=name, version=version, conda_deps=conda_deps,
            primary_tools=[n for n, _ in parsed], platform=docker_platform,
            accelerator=effective_accel,
            license_gated=gated, licenses=licenses, redistributable=not gated,
            content_digest=br.get("content_digest", ""),
            # ship the engine lock with the recipe — replay materializes the env
            # from these exact bytes, no solve. Eliminates the conda drift hole.
            conda_lock=br.get("lock_files") or None,
            # snapshot.debian.org timestamp the BUILDER + RUNTIME stages pinned
            # apt to — replay points at the same snapshot, same apt bytes.
            apt_snapshot=br.get("apt_snapshot") or "")
        if conda_deps:  # portable lock for the conda layer (image digest is the real anchor)
            plats = [platform] if platform.startswith("linux") else ["linux-64", platform]
            cl = _ms._env_mgr.generate_lock(env_name, platforms=plats)
            conda_lock_path = cl.get("lockfile") if cl.get("success") else None

    # Authoritative 'what was GOT' anchor: EnvBuild digest for a build, biocontainer
    # manifest digest for an adopt (request hash only as a last-resort fallback).
    content_digest = _ms._freeze.record_content_digest(
        mode, build_digest=build_cd, adopt_digest=adopt.get("digest", ""),
        fallback=content_digest)
    record = _ms._freeze.freeze_record(
        request_key=rkey, content_digest=content_digest, mode=mode,
        image=image, image_digest=image_digest, platform=platform, gated=gated,
        conda_lock_path=conda_lock_path, tarball=tarball, hpc=hpc,
    )
    if build_method:
        record["build_method"] = build_method
    # A recipe ALWAYS exists, regardless of install path (an env is not a solved
    # component if no one can rebuild it). The BUILD path assembled env_recipe_dict
    # above; the ADOPT path's recipe is "pull this biocontainer by digest" — trivially
    # self-contained, so construct it here from the finalized content_digest.
    if env_recipe_dict is None and mode == "adopt":
        env_recipe_dict = _ms._env_recipe.extract_recipe(
            draft, name=name, version=version,
            conda_deps=[f"{n}={v}" if v else n for n, v in parsed],
            primary_tools=[n for n, _ in parsed], platform=platform,
            accelerator=effective_accel,
            license_gated=gated, licenses=licenses, redistributable=not gated,
            content_digest=content_digest,
            build_method="adopt", adopt_image=adopt.get("image_by_digest", image))
    if shipped_binaries:
        record["shipped_binaries"] = shipped_binaries
    record["validation_locus"] = validation_locus
    # report inputs — all runtime-captured (from the BuildResult), so the env
    # report rendered from them can't be faked. Absent on the adopt path.
    record["name"] = name
    record["requested_tools"] = [n for n, _ in parsed]
    # DECLARED POLICY (submitter-asserted, NOT runtime-verified — the contract only
    # checks them for consistency, I12/I13). Recorded so the report can show them in
    # a clearly-separated, honestly-labelled section.
    record["licenses"] = list(licenses or [])
    # The accelerator policy on the record is the SAME object the honesty contract
    # was just evaluated against (effective_accel = draft.accelerator OR synthesized
    # from the MCP scalar args). The record's accelerator MUST agree with the policy
    # that gated the build/adopt, else the artifact and its claim diverge.
    record["accelerator"] = effective_accel
    if mode == "build":
        record["engine"] = br.get("engine", "none")
        record["conda_specs"] = br.get("conda_specs", [])
        record["verifications"] = br.get("verifications", [])
        record["resolved_packages"] = br.get("resolved_packages", [])
        record["system_packages"] = br.get("system_packages", [])
        record["push_status"] = push_status
    if mode == "adopt":
        # adopt_source — the biocontainer-resolver's output, preserved on the
        # record so the report can show the actual tag (human version, e.g.
        # samtools `1.21--h50ea8bc_0`) instead of just the manifest digest, and
        # so the install-commands section can render the `apptainer pull` that
        # produced this artifact. The digest in `image` is the only trust
        # anchor; the tag is provenance display.
        record["adopt_source"] = {
            "repo":            adopt.get("repo"),
            "tag":             adopt.get("tag"),
            "image_by_tag":    adopt.get("image"),
            "image_by_digest": adopt.get("image_by_digest"),
            "digest":          adopt.get("digest"),
        }
        # SBOM from the ADOPTED image (read via a throwaway container, same
        # 'read-from-the-image, can't-be-faked' ground truth the build path
        # captures from its build container). Without this, resolved_packages is
        # empty and the env report has nothing to show per tool but the shared
        # mulled image tag (`2d1a988…-0` on EVERY tool) — useless for citing a
        # version in a publication. With it, each requested tool resolves to its
        # HUMAN-READABLE conda version (samtools 1.21, bwa 0.7.17), consistent
        # across tools, plus the biocontainer's transitive + OS layers. Best-effort:
        # a docker/read failure leaves it empty and the report falls back to the tag.
        from agent.skills.container_build import ContainerBuild as _CB
        _adopt_platform = _ms._CONDA_TO_DOCKER_PLATFORM.get(platform, platform)
        try:
            record["resolved_packages"] = _CB.conda_sbom_from_image(image, _adopt_platform)
            record["system_packages"] = _CB.apt_sbom_from_image(image, _adopt_platform)
        except Exception:
            record["resolved_packages"] = record.get("resolved_packages", [])
            record["system_packages"] = record.get("system_packages", [])

        # VALIDATED_IN_IMAGE for the ADOPT path (audit 2026-07-16 Tier 2). Adopt was the
        # ONE path that shipped an env nobody had ever run a tool in: check_adopt
        # deliberately skipped validation and `samtools=1.21` registered with
        # `verifications: []` — while the ENV report and the attestation carried it as a
        # solved component, and two sealed workflows depend on it. "The biocontainer's
        # bytes are trusted by their published digest" answers the wrong question: the risk
        # was never that bioconda lies, it is that WE adopted the wrong image (a mulled tag
        # resolving to a package set that doesn't contain the tool). Running the evidence
        # is what catches that, and it is the same probe the build path uses.
        #
        # Affordable: the pull is already paid for by the SBOM reads above, and the
        # marginal cost is ~0.25s per tool (measured). The fast path stays fast.
        record["verifications"] = _validate_tools_in_image(
            image, _adopt_platform, [n for n, _ in parsed])
        # A real locus now — the evidence genuinely ran somewhere. `validation_locus:
        # "adopted"` was the honest name for "we validated nowhere"; with evidence it
        # would be a lie by omission (native vs emulated decides whether I7 timings are
        # authoritative), so it takes the same value the build path records.
        record["validation_locus"] = _ms._locus.detect_locus(_adopt_platform)["locus"]

        # THE CONTRACT, on the completed record. It runs here (not at the adopt decision)
        # because BUILT/VALIDATED_IN_IMAGE/POLICY_CLEAN read image + verifications +
        # accelerator + licenses, and the last of those only land once the record is
        # assembled — gating earlier would have checked a record that didn't exist yet.
        adopt_violations = _ms._env_honesty.check_build(record)
        if adopt_violations:
            return refused("freeze.adopt_honesty", success=False, stage="adopt_honesty",
                    request_key=rkey, adopt_attempt=adopt,
                    honesty_violations=adopt_violations,
                    violation_count=len(adopt_violations),
                    verifications=record.get("verifications"))
    # IDENTITY DISCLOSURE (audit #8): what each requested tool says it IS, read from the
    # registry the shipped package actually came from (matched via the in-image SBOM).
    # Agent-asserted, best-effort — a probe miss yields self_description=None and NEVER
    # fails a freeze; the record's ToolIdentity shape is enforced at register.
    from agent.skills import tool_identity as _ti
    try:
        # A tool installed from a non-registry tier (binary/source/jar/cargo/go/perl) is not
        # its own provider in the SBOM closure — pass those so capture() gives them honest
        # silence instead of a same-named transitive dep's identity.
        _nonreg = [p.get("name", "") for p in
                   (_ms._freeze.non_conda_installs(draft) if draft else [])]
        record["tool_identities"] = _ti.capture(
            record.get("requested_tools") or [], record.get("resolved_packages") or [],
            nonregistry_tools=_nonreg)
    except Exception:
        record["tool_identities"] = []
    # Carry the disclosure into the machine recipe too, so a rebuild's context ("what
    # this is") travels with the reproduction bytes. Extra key; ignored by rebuild.
    if isinstance(env_recipe_dict, dict):
        env_recipe_dict["tool_identities"] = record["tool_identities"]
        # Carry the OBSERVED SBOM (what actually shipped) beside conda_deps (the rebuild
        # INPUTS) so the machine recipe is self-describing about its installed contents,
        # not only how to reconstruct them (audit 2026-07-19, W4). Descriptive; the
        # rebuild ignores these keys.
        env_recipe_dict["installed_packages"] = record.get("resolved_packages") or []
        env_recipe_dict["system_packages"] = record.get("system_packages") or []
    _ms._env_cache.register(rkey, record)
    # Orientation pointer (Phase-3 Piece B): tie this frozen env back to its draft
    # so current_state re-earns ENV_FROZEN. Best-effort; never fails a freeze.
    if pipeline_id:
        try:
            _ms._pipeline_state.set_frozen_pointer(pipeline_id, rkey)
        except Exception:
            pass

    # Layer-1 deliverables, rendered PURELY from the verified record (can't be faked):
    # the human env report (HTML headline + Markdown for diff/parse) + a standard
    # in-toto/SLSA provenance attestation. Views — never block a good freeze on a
    # render error.
    # The .md renderer was retired in batch-3 (the .html is the canonical Layer-1
    # deliverable; .md was a redundant view that only existed during the AUDIT#2
    # phase to ease grep-based diff). Two artifacts now: ENV.html + attestation.json.
    report_html_path = attestation_path = None
    reports_dir = _ms._env_mgr.project_root / "env_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    try:
        (reports_dir / f"{name}.ENV.html").write_text(_ms._env_report_html.render_env_report_html(record))
        report_html_path = str(reports_dir / f"{name}.ENV.html")
    except Exception as e:
        report_html_path = f"(html report render failed: {e!r})"
    try:
        import json as _json
        att = _ms._attestation.build_attestation(record, base_image=_ms._BASE_IMAGE if mode == "build" else "")
        (reports_dir / f"{name}.attestation.json").write_text(_json.dumps(att, indent=2))
        attestation_path = str(reports_dir / f"{name}.attestation.json")
    except Exception as e:
        attestation_path = f"(attestation render failed: {e!r})"
    # The build recipe — ALWAYS written, for EVERY install path (build / adopt), in
    # BOTH forms rendered PURELY from the verified record:
    #   {name}.recipe.yaml  — machine-readable, self-contained; verify_env_recipe rebuilds it
    #   {name}.recipe.md    — human-readable, the runnable command sequence for a hand rebuild
    # A frozen env is only a "solved component" if anyone can reproduce it; the recipe
    # is that guarantee, so it is never optional. Views — a render error never blocks a
    # good freeze.
    recipe_path = recipe_md_path = None
    if env_recipe_dict:
        try:
            import yaml as _yaml
            (reports_dir / f"{name}.recipe.yaml").write_text(
                _yaml.safe_dump(env_recipe_dict, sort_keys=False))
            recipe_path = str(reports_dir / f"{name}.recipe.yaml")
        except Exception as e:
            recipe_path = f"(recipe render failed: {e!r})"
        try:
            from agent.skills import env_recipe_render as _err
            (reports_dir / f"{name}.recipe.md").write_text(
                _err.render_recipe_markdown(env_recipe_dict, record))
            recipe_md_path = str(reports_dir / f"{name}.recipe.md")
        except Exception as e:
            recipe_md_path = f"(recipe.md render failed: {e!r})"

    # merge (not kwargs) — `record` is a build result that may already carry a
    # `success` key; a dict-literal merge makes the explicit values last-wins
    # instead of a kwarg collision.
    out = proven("freeze.built", **{**record, "success": True, "cache_hit": False,
                                    "adopt_attempt": adopt})
    out["env_report_html"] = report_html_path
    out["attestation"] = attestation_path
    out["env_recipe"] = recipe_path
    out["env_recipe_md"] = recipe_md_path
    if locus_advisory:
        out["locus_advisory"] = locus_advisory   # actionable, e.g. "enable Rosetta…"
    # Summarize the bulky SBOM in the live response only. The full SBOM lives
    # in the EnvCache record (registered above) and in env_reports/{name}.ENV.html
    # / .attestation.json — both rendered from the full record before this
    # summarization fires. ~10-15k tokens of SBOM rows eliminated per response
    # with zero loss of accessible information (the agent Reads the HTML when
    # it wants the full SBOM).
    return _ms._summarize_sbom_in_response(out)


@mcp.tool()
def verify_env_recipe(recipe_path: str) -> dict:
    """Rebuild an env FROM ITS RECIPE ALONE (env_reports/{name}.recipe.yaml, written by
    freeze) and check it reproduces the recorded content_digest. The recipe is self-
    contained — it carries the conda specs + every non-conda install_method (synthesized
    commands + provenance + commit, jar/binary/source/...), so this rebuild uses NO
    draft / agent / pipeline-state.

    WHAT A MATCH PROVES (precisely — no overclaiming):
      • COMPLETENESS — the recipe is self-contained (rebuild from it alone succeeds).
      • LOCAL DETERMINISM / CONVERGENCE — replaying it here yields the same content_digest;
        the conda layer is RE-SOLVED (not cheated from a cached lock), so a match means the
        solve converged — the strongest same-machine signal for 'two runs → same result'.
    It does NOT prove cross-machine reproducibility (different base cache / network / docker)
    or independent-party tamper-evidence — those are this SAME rebuild run ELSEWHERE (CI, a
    colleague) + signing. The recipe ENABLES them; this verifies the necessary local
    conditions. Requires Docker. Returns {success, content_digest_match, rebuilt/expected
    content_digest, proves, honesty_violations}."""
    import yaml as _yaml
    try:
        with open(recipe_path) as fh:
            recipe = _yaml.safe_load(fh)
    except Exception as e:
        return refused("freeze.recipe_load_failed", success=False,
                       error=f"could not load recipe {recipe_path!r}: {e}")
    if not isinstance(recipe, dict) or not recipe.get("name"):
        return refused("freeze.recipe_invalid", success=False,
                       error="not a valid env recipe (missing name)")

    # Verify the way the recipe was BUILT — a generic container-native rebuild only
    # reproduces a container-native env. For adopt / authors-dockerfile recipes it would
    # produce a DIFFERENT image and falsely report 'not reproduced'; branch instead.
    method = recipe.get("build_method") or "container-native-build"
    expected = recipe.get("content_digest") or ""
    if method == "adopt":
        # An adopt env's contract IS the published manifest digest — re-pull the image
        # by digest and confirm it still resolves to the recorded digest (no build).
        img = recipe.get("adopt_image") or ""
        if not img:
            return refused("freeze.recipe_adopt_no_image", success=False,
                           error="adopt recipe has no adopt_image to re-pull")
        pull = subprocess.run(["docker", "pull", img], capture_output=True, text=True, timeout=1800)
        # The REGISTRY MANIFEST digest — the thing `expected` actually is (an adopt's
        # content_digest is the biocontainer's published manifest digest, visible right
        # there in `adopt_image: repo@sha256:…`). This read `{{index .Id}}`, the daemon's
        # LOCAL content id, and matched only because this project's dev Mac runs the
        # containerd snapshotter, where the two coincide. On a classic overlay2 daemon
        # `.Id` is the config blob digest, so this branch reported "recipe not reproduced"
        # for EVERY adopt recipe, for every normal user — a verification tool that passed
        # by accident of one machine's docker config (audit 2026-07-16 Tier 6).
        got = _ms._container_build.registry_manifest_digest(img)
        match = bool(expected) and got == expected
        rf = dict(success=pull.returncode == 0, content_digest_match=match,
                  rebuilt_content_digest=got, expected_content_digest=expected,
                  proves="ADOPT: the biocontainer re-pulls to the recorded manifest digest "
                         "(content-addressed — identical bytes). Not a from-source rebuild.")
        return proven("freeze.recipe_verified", **rf) if match else broke("freeze.recipe_not_reproduced", **rf)
    if method in ("authors-dockerfile", "freeze-from-image"):
        # NOT VERIFIED — and it must not be tagged as though it were. This returned
        # `proven(success=True, content_digest_match=None)` for a check that runs NOTHING:
        # an agent branching on `success` concludes the recipe was verified, and the only
        # tell is a None buried in a sibling key. Declining to rebuild is right (a
        # Dockerfile build is not bit-reproducible — apt mirrors, timestamps and upstream
        # tags all move — so digest convergence would fail for a CORRECT recipe and emit a
        # false 'not reproduced'). Declining is not the same as passing, and `refused` is
        # the tag that exists for exactly this.
        ds = recipe.get("dockerfile_source") or {}
        pin = ds.get("commit") or ""
        missing = [n for n, v in (("repo", ds.get("repo")), ("commit", pin)) if not v]
        return refused(
            "freeze.recipe_verify_unavailable", success=False, content_digest_match=None,
            expected_content_digest=expected,
            verifiable=not missing,
            error=(f"this recipe cannot be verified by rebuild: its source is unpinned "
                   f"(missing {', '.join(missing)}), so there is nothing to rebuild FROM"
                   if missing else
                   "an authors-Dockerfile build is not bit-reproducible, so digest "
                   "convergence cannot prove it — no check was run"),
            proves="NOTHING WAS VERIFIED HERE. To reproduce by hand: re-run "
                   f"build_env_from_authors_recipe(repo={ds.get('repo') or '?'!r}, "
                   f"ref={pin or '?'!r}"
                   + (f", recipe={ds['recipe_path']!r}" if ds.get("recipe_path") else "")
                   + (f", build_args={ds['build_args']!r}" if ds.get("build_args") else "")
                   + ") and compare the tools' evidence output. A generic conda rebuild does "
                     "not apply to this env.")

    res = _ms._env_recipe.rebuild_from_recipe(recipe)
    # Outcome is runtime-conditional: only a rebuild that SUCCEEDED and converged
    # to the same content digest is `proven`. A failed build or a digest mismatch
    # (local drift — the recipe didn't reproduce) is `broke`, not a green tag.
    verified = bool(res.get("success") and res.get("content_digest_match"))
    _rf = dict(
        success=res["success"],
        content_digest_match=res["content_digest_match"],
        rebuilt_content_digest=res["rebuilt_content_digest"],
        expected_content_digest=res["expected_content_digest"],
        proves=res["proves"],
        build_stage=res["build"].get("stage"),
        honesty_violations=res["build"].get("honesty_violations"),
    )
    if verified:
        return proven("freeze.recipe_verified", **_rf)
    return broke("freeze.recipe_not_reproduced", **_rf)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@mcp.tool()
def generate_user_guide(
    pipeline_id: str = "",
    spec: dict = {},
    freeze_request_key: str = "",
    write: bool = True,
    overview: str = "",
    traps: list = [],
) -> dict:
    """Render the Layer-2 user guide for a pipeline (Markdown) as a hand-holding,
    copy-pasteable WALKTHROUGH of how to run the tool on the compute resource it was
    validated against. The SKELETON is DERIVED from the PASSING, validated run —
    every command shown was actually executed and its outputs checked (drawn only
    from validated pipeline_steps + a self-tested usage.command_template), the
    compute-node acquisition (`srun`) and `module load` lines from the recorded
    cluster placement, and the container from the freeze() Apptainer delivery. A
    workflow consumes its env BY DIGEST; provenance pins the content/image digests.

    `overview` + `traps` are the OPTIONAL agent-AUTHORED narrative — the "what the
    tool does" paragraph and the hard-won gotchas that no record can hold. They are
    rendered in clearly-labelled 'authored, not machine-verified' sections and are
    omitted entirely when not supplied (never fabricated). This is the reverse-theme-
    park split in the deliverable: a verified skeleton with a free authored middle.

    Pass `pipeline_id` (uses its draft) or a `spec` dict. `freeze_request_key`
    looks the frozen artifact up in the EnvCache (e.g. 'samtools=1.21|linux-64|
    none') to source the HPC delivery + digests. Writes env_reports/{name}.GUIDE.md
    when write=True.
    """
    s = spec or (_ms._pipeline_state.get_draft(pipeline_id) if pipeline_id else None)
    if not s:
        return refused("freeze.guide_no_source", success=False,
                       error="provide pipeline_id (with a draft) or a spec dict")
    fr = _ms._env_cache.lookup(freeze_request_key) if freeze_request_key else None
    narrative = {k: v for k, v in (("overview", overview), ("traps", traps)) if v}
    md = _ms._user_guide.render_user_guide(s, freeze_record=fr, narrative=narrative)
    result = proven(
        "freeze.guide_rendered",
        success=True,
        markdown=md,
        commands_shown=len(_ms._user_guide.executed_commands(s)),
        env_pinned=bool(fr),
    )
    if write:
        out = _ms._env_mgr.project_root / "env_reports" / f"{s.get('pipeline_name','pipeline')}.GUIDE.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        result["path"] = str(out)
    return result




@mcp.tool()
def freeze_from_image(
    image: str,
    tools: list,
    name: str,
    version: str = "",
    platform: str = "linux/amd64",
    build_method: str = "adopt-image",
    dockerfile_source: dict = {},
    gated: bool = False,
    licenses: OptStrList = None,
) -> dict:
    """Freeze an env from an EXISTING image — the authors' OWN published image, or one
    built from their Dockerfile — instead of reconstructing it from conda/pip. This is
    the executor for the authors-recipe-first path (route via resolve_tool's `author_image`
    tier): a human handed a tool that ships its own image would USE it, and so does the
    agent, as a first-class primitive.

    The honesty contract is UNCHANGED — the image earns its registration:
      BUILT (image+digest resolve) · VALIDATED_IN_IMAGE (each tool's evidence RUNS green
      in the image, no echo/print cheats) · POLICY_CLEAN (I12/I13). VALIDATED_IN_IMAGE is
      the guard the Talos reconstruction slipped past — so `tools[*].evidence` must RUN the
      tool (shell out on real inputs), not merely import it.

    `tools`: [{name, evidence}] — evidence is a command that exercises the tool in-image
    and exits 0. `build_method`: 'adopt-image' (author image, adopted by digest) or
    'authors-dockerfile' (an image built from their Dockerfile; pass `dockerfile_source`=
    {repo, commit, tag} so the recipe records the pinned source). Writes the four Layer-1
    deliverables (ENV.html, attestation.json, recipe.yaml, recipe.md) rendered PURELY from
    the verified record. Docker required."""
    from agent.skills import freeze_from_image as _ffi
    return _ffi.freeze_from_image(
        image=image, tools=[dict(t) for t in tools], name=name, version=version,
        platform=platform, build_method=build_method,
        dockerfile_source=dict(dockerfile_source) if dockerfile_source else None,
        gated=gated, licenses=list(licenses or []),
        env_cache=_ms._env_cache,
        reports_dir=_ms._env_mgr.project_root / "env_reports")


@mcp.tool()
def build_env_from_authors_recipe(
    repo: str,
    tools: list,
    name: str,
    recipe: str = "Dockerfile",
    ref: str = "",
    version: str = "",
    platform: str = "linux/amd64",
    build_args: dict = {},
    gated: bool = False,
    licenses: OptStrList = None,
) -> dict:
    """Build an env image from the tool's OWN container recipe (their Dockerfile), then
    freeze it. The declarative executor for resolve_tool's `authors_recipe` tier — the
    path taken when the authors' recipe installs pieces a conda/pip reconstruction would
    silently DROP (compiled/vendored/system/binary deps; the completeness gate in
    authors_sources). A human would follow the authors' guide rather than reconstruct;
    this does the same.

    Clones `repo` (a GitHub 'owner/repo', a full git URL, or a `file://` local mirror) at
    `ref` (PIN a tag/commit — a bare default branch drifts), `docker build`s its `recipe`
    (default 'Dockerfile') for `platform`, then hands the built image to freeze_from_image
    (honesty contract + deliverables). `tools`: [{name, evidence}] — evidence must RUN each
    tool in-image. The recipe records the pinned source so the build is reproducible.
    Docker + git (+ network for a remote repo) required."""
    from agent.skills import freeze_from_image as _ffi
    return _ffi.build_from_authors_recipe(
        repo=repo, tools=[dict(t) for t in tools], name=name, recipe=recipe, ref=ref,
        version=version, platform=platform, build_args=dict(build_args or {}),
        gated=gated, licenses=list(licenses or []),
        env_cache=_ms._env_cache,
        reports_dir=_ms._env_mgr.project_root / "env_reports")
