"""data_tools — test data + reference data + the autonomous install brief.

What gets ACQUIRED (test_data, reference databases, phenopackets, raw pod5),
what's AVAILABLE (list/download_resource), and what to feed an autonomous
installer (install_pipeline_brief).

phenopacket_to_vcf lives here too — it materializes a VCF from registered
phenopacket metadata, the synthetic-test-data shortcut for VEP / Exomiser
pipelines.
"""
from __future__ import annotations

import time
from pathlib import Path

# IMPORT-BINDING: see workflow_tools.py — singletons go through `_ms.X`
# so test monkeypatching on mcp_server reaches us.
from agent import mcp_server as _ms
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched
from agent.skills.outcomes import refused
@mcp.tool()
def download_reference_database(
    name: str,
    url: str,
    local_path: str = "",
    version: str = "",
    description: str = "",
    pipeline_id: str = "",
    extract: bool = True,
    compute_env: str = "",
    remote_dir: str = "",
    slurm: dict = {},
) -> dict:
    """Download a reference database large enough to need watchdog-safe execution.

    DEFAULT (compute_env=""): download to the LOCAL agent machine at `local_path`.
    Uses run_in_background internally — agent doesn't have to remember to wrap
    the curl in async, doesn't have to worry about --silent / -q traps that
    killed the original Exomiser install. Auto-records a ReferenceDatabase
    entry in the draft when pipeline_id is supplied.

    DIRECTED TO CLUSTER (compute_env set): render a SIMPLE resumable SLURM script
    and `sbatch` it on that env, so the COMPUTE node pulls the bytes (the head
    node stays clean — never scp/curl a huge DB through it). Lands in the env's
    common_data zone by default (`<common_data>/<name>/`); `remote_dir` overrides
    (must stay under common_data). `slurm` is the per-job resource request
    ({time,mem,cpus,...}; defaults to a generous 1-day walltime). Submit-and-
    document: returns the job_id immediately (a gnomAD/VEP-cache download runs for
    hours and would blow the ~10-min watchdog) + writes a findable manifest. Poll
    with cluster_job_status; the DB is verified OVER SSH at seal (locus-aware I5).
    The recorded reference_databases entry carries locus="cluster" + compute_env +
    the cluster path, so I8 (composition) and I5 (availability) both work with data
    that never touches your laptop. `local_path` is ignored in this mode.

    extract=True: if `url` ends in .zip / .tar.gz / .tar, unpack in place.
    extract=False: just download the file.

    Returns immediately with a job_id — caller polls check_job(job_id) (local) or
    cluster_job_status (cluster) until done. The ReferenceDatabase entry's
    `available`, `sha256`, and `size_bytes` are re-derived at seal: locally from a
    `<local_path>.source.sha256` sidecar, or over ssh from the cluster sidecar —
    the reproducibility anchor pins the DB by content, not just name+URL.
    """
    if not (name or "").strip() or not (url or "").strip():
        return refused("data.download_db_missing_args", success=False,
                       error="required argument(s) empty: pass at least a name and "
                             "source url (local_path for a local download, or "
                             "compute_env for a cluster download)")

    # Directed to the cluster → the resumable-SLURM-download path.
    if (compute_env or "").strip():
        from agent.skills import acquire_data
        return acquire_data.acquire_to_cluster(
            name=name, url=url, compute_env=compute_env,
            remote_dir=remote_dir, slurm=(slurm or None),
            version=version, description=description,
            extract=extract, pipeline_id=pipeline_id)

    if not (local_path or "").strip():
        return refused("data.download_db_missing_args", success=False,
                       error="local_path is required for a local download "
                             "(or set compute_env to download onto a cluster)")

    target = Path(local_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # sha256 anchor: hash the ARTIFACT THE URL SERVED (the archive, or the file
    # itself when not extracting) into a sidecar next to local_path, BEFORE the
    # archive is removed. This is the reproducibility anchor — a bundle named
    # "vep_cache_111_hg38" is not self-verifying; the URL can serve different
    # bytes over time. `available`/`sha256`/`size_bytes` are folded back into
    # the ReferenceDatabase record from disk at seal (see workflow_tools). The
    # portable hasher works on both Linux (sha256sum) and macOS (shasum -a256).
    sidecar = f"{target}.source.sha256"
    def _hash_into_sidecar(artifact: str) -> str:
        return (f"( sha256sum {artifact} 2>/dev/null || shasum -a 256 {artifact} ) "
                f"| awk '{{print $1}}' > {sidecar}")

    if extract and url.endswith(".zip"):
        # Download to a sibling .zip, unpack into local_path, remove the zip.
        zip_path = target.parent / Path(url).name
        cmd = (
            f"curl -L --progress-bar -C - -o {zip_path} '{url}' "
            f"&& {_hash_into_sidecar(str(zip_path))} "
            f"&& mkdir -p {target} "
            f"&& unzip -o {zip_path} -d {target.parent} "
            f"&& rm {zip_path}"
        )
    elif extract and (url.endswith(".tar.gz") or url.endswith(".tgz")):
        tar_path = target.parent / Path(url).name
        cmd = (
            f"curl -L --progress-bar -C - -o {tar_path} '{url}' "
            f"&& {_hash_into_sidecar(str(tar_path))} "
            f"&& mkdir -p {target} "
            f"&& tar -xzf {tar_path} -C {target} "
            f"&& rm {tar_path}"
        )
    elif extract and url.endswith(".tar"):
        tar_path = target.parent / Path(url).name
        cmd = (
            f"curl -L --progress-bar -C - -o {tar_path} '{url}' "
            f"&& {_hash_into_sidecar(str(tar_path))} "
            f"&& mkdir -p {target} "
            f"&& tar -xf {tar_path} -C {target} "
            f"&& rm {tar_path}"
        )
    else:
        cmd = (
            f"curl -L --progress-bar -C - -o {target} '{url}' "
            f"&& {_hash_into_sidecar(str(target))}"
        )

    job = _ms._job_manager.start(cmd, job_id=f"refdb_{name}_{int(time.time())}")
    if pipeline_id:
        # Append the ReferenceDatabase entry now; `available` is re-derived at
        # finalize from filesystem state (so an in-progress download correctly
        # shows available=false until it completes).
        rdb = {
            "name":          name,
            "version":       version or "unknown",
            "source_url":    url,
            "local_path":    str(target),
            "available":     False,
            "description":   description or None,
        }
        draft = _ms._pipeline_state.get_draft(pipeline_id) or {}
        existing = draft.get("reference_databases") or []
        # Update-by-name if it exists.
        replaced = False
        for i, e in enumerate(existing):
            if isinstance(e, dict) and e.get("name") == name:
                existing[i] = {**e, **{k: v for k, v in rdb.items() if v is not None}}
                replaced = True
                break
        if not replaced:
            existing.append({k: v for k, v in rdb.items() if v is not None})
        _ms._pipeline_state.patch(pipeline_id, {"reference_databases": existing})
    return {
        "job_id":     job.get("job_id"),
        "status_path": job.get("status_path"),
        "log_path":   job.get("log_path"),
        "local_path": str(target),
        "name":       name,
        "url":        url,
        "command":    cmd,
        "note":       "Use check_job(job_id) to monitor; ReferenceDatabase entry recorded in draft.reference_databases (available=false until download finishes).",
    }


@mcp.tool()
def acquire_reference_via_recipe(
    name: str,
    recipe_path: str,
    compute_env: str,
    versions: list = [],
    bundle_version: str = "",
    description: str = "",
    pipeline_id: str = "",
    remote_dir: str = "",
    slurm: dict = {},
    mask_tools: list = ["tmux"],
) -> dict:
    """Run a TOOL'S OWN reference-data acquisition recipe on a compute node.

    When a tool ships its own data-gathering script (a `large_files/gather_files.sh`,
    a `download_reference_data.sh`, …) — USE IT, don't re-implement its URL list.
    A real gather script also does decompression, chr-renames, and format
    conversion (echtvar/tabix/faidx) that a transcribed list silently drops,
    drifting from the tool's recommended pipeline. This primitive uploads the
    tool's recipe verbatim, wraps it in a resumable SLURM runner that (a) runs it
    head-node-safe on a COMPUTE node into the env's common_data zone, (b) forces
    the recipe's NON-INTERACTIVE path by masking interactive-only tools
    (`mask_tools`, default tmux — a download-manager recipe re-execs into a
    detached tmux server SLURM would kill), and (c) hashes every produced file for
    provenance. Submit-and-document: returns the job_id immediately (a full bundle
    is tens of GB / hours) + a findable manifest. Poll cluster_job_status; seal
    verifies the bundle dir over ssh (locus-aware I5).

    `recipe_path` = the tool's script on the agent machine (e.g. from its cloned
    repo). `versions` = the agent-read per-file version manifest,
    [{filename, url, version, note?}] with the FINAL produced filenames — recorded
    on the reference_databases entry's files[] + the manifest (the reproducibility
    + citation record; every reference must carry its version). Lands in
    `<common_data>/<name>/` by default (`remote_dir` overrides, must stay under
    common_data). Records ONE cluster-locus reference_databases entry for the
    whole bundle dir.
    """
    if not (name or "").strip() or not (recipe_path or "").strip() \
            or not (compute_env or "").strip():
        return refused("data.recipe_missing_args", success=False,
                       error="name, recipe_path, and compute_env are all required")
    from agent.skills import acquire_data
    return acquire_data.acquire_via_recipe(
        name=name, recipe_local_path=recipe_path, compute_env=compute_env,
        versions=list(versions or []), remote_dir=remote_dir,
        slurm=(slurm or None), mask_tools=tuple(mask_tools or ()),
        bundle_version=bundle_version, description=description,
        pipeline_id=pipeline_id)


@mcp.tool()
def list_available_resources(resource_type: str = "both") -> dict:
    """List genomes and/or test datasets on disk.
    resource_type: 'genomes' | 'test_data' | 'both'"""
    return _ms._list_resources({"resource_type": resource_type}, _ms.config)


@mcp.tool()
def download_resource(resource_type: str, resource_id: str) -> dict:
    """Download a reference genome not yet on disk.
    resource_type: 'genome', resource_id: e.g. 'hg38_chr22'"""
    return _ms._test_runner.download_resource(resource_type, resource_id)


@mcp.tool()
def add_core_test_data(
    accession: str,
    assay_type: str,
    end_type: str = "paired_end",
    genome_build: str = "hg38",
    sample: str = "",
    subset: str = "10K",
    platform: str = "illumina",
    source_url: str = "",
    source_url_r2: str = "",
) -> dict:
    """Stream-download and register a new sequencing dataset.
    assay_type:   exome | wgs | rnaseq | chipseq | atacseq | hic | amplicon | wgbs | ont_wgs | pacbio_hifi | direct_rna | isoseq | fiberseq
    platform:     illumina (default) | ont | pacbio_hifi | pacbio_isoseq | pacbio_fiberseq
    subset:       500 | 1K | 10K (default) | 50K | 100K | 500K | 1M  — use 500 for long-read platforms
    source_url:   override EBI URL builder (e.g. NCBI FTP, S3). For paired-end also supply source_url_r2."""
    return _ms._add_core_test_data(
        _ms.config, accession, assay_type,
        end_type=end_type, genome_build=genome_build,
        sample=sample, subset=subset, platform=platform,
        source_url=source_url, source_url_r2=source_url_r2,
    )


@mcp.tool()
def add_core_pod5_data(
    accession: str,
    sample: str,
    source_url: str,
    assay_type: str = "ont_wgs",
    platform: str = "ont",
    chemistry: str = "",
    flowcell: str = "",
    kit: str = "",
    suggested_model: str = "",
    expected_sha256: str = "",
    expected_size: int = 0,
    genome_build: str = "hg38",
) -> dict:
    """Download a raw nanopore pod5 file into core_test_data and register it.

    Pod5 files are binary Apache Arrow (not subsettable like gzipped FASTQ) —
    the whole file is fetched, sha256-anchored against `expected_sha256`
    when supplied, and recorded with nanopore-specific metadata
    (chemistry, flowcell, kit, suggested_model) on the SampleMeta sidecar
    so downstream basecaller pipelines (dorado, bonito, remora) pick the
    right base + modbase models without re-probing.

    Idempotent: skips download if the file is present and (sha256 OR size)
    matches the declared anchor. Methylation/base-mod model selection
    (5mC / 6mA / etc.) is derived from `chemistry` + the basecaller's
    paired-model registry at pipeline-build time — no pre-declared list."""
    return _ms._add_core_pod5_data(
        _ms.config,
        accession=accession,
        sample=sample,
        source_url=source_url,
        assay_type=assay_type,
        platform=platform,
        chemistry=chemistry,
        flowcell=flowcell,
        kit=kit,
        suggested_model=suggested_model,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        genome_build=genome_build,
    )


@mcp.tool()
def add_phenopacket(
    source_url: str,
    genome_build: str = "hg38",
) -> dict:
    """Download and register a GA4GH phenopacket JSON into core_test_data.

    The phenopacket ID, subject, HPO terms, diseases, genes, and variants are
    all extracted from the JSON itself via PhenopacketMeta.from_phenopacket() —
    nothing is supplied manually.  Idempotent: re-running refreshes the sidecar.

    source_url:   direct URL to a phenopacket JSON file (GitHub raw, HTTP, etc.)
    genome_build: target core_test_data directory, e.g. hg38 (default)"""
    return _ms._add_phenopacket(_ms.config, source_url=source_url, genome_build=genome_build)


@mcp.tool()
def phenopacket_to_vcf(
    phenopacket_id: str,
    output_vcf: str,
    genome_build: str = "hg38",
) -> dict:
    """Materialise a single-sample VCF from a registered phenopacket's variant block.

    Use this in Exomiser / variant-annotator pipelines where the test VCF should
    contain the exact variant(s) recorded in the phenopacket — no hand-written
    VCF synthesis required. Reads {data_dir}/core_test_data_{genome_build}/
    phenopackets/{phenopacket_id}_meta.yaml; writes a VCFv4.2 file to output_vcf.

    Inputs:
      phenopacket_id: as registered by add_phenopacket (PMID_30315159_Patient_N, …)
      output_vcf:     absolute path to write the VCF
      genome_build:   defaults to hg38

    Output keys: success, phenopacket_id, sample_id, output_vcf, num_variants,
                 contigs, genome_assembly. On failure: {success: false, error}.
    Phenotype-only phenopackets (no variant block) cannot be materialised — this
    is surfaced as a clear error rather than silently producing an empty VCF.
    """
    return _ms._phenopacket_to_vcf(
        _ms.config, phenopacket_id=phenopacket_id,
        output_vcf=output_vcf, genome_build=genome_build,
    )


@mcp.tool()
def select_test_data(
    genome_build: str = "hg38",
    assay_type: str = "",
    end_type: str = "",
    sample: str = "",
    accession: str = "",
    subset: str = "",
    file_format: str = "",
    pipeline_id: str = "",
) -> dict:
    """Find a matching test dataset on disk and return a TestDataRef-shaped
    dict ready to drop into the spec. If pipeline_id is supplied, also sets
    draft.test_data.

    Match is best-effort: each criterion scores points; the highest-scoring
    AVAILABLE dataset wins. Returns {test_data, available, match_score}, or
    {error} if nothing on disk matches at all. Inspect the result and call
    again with different criteria if the match is wrong.

    `file_format` ("fastq" | "pod5" | "fast5" | …) lets a basecaller pipeline
    pick the raw-signal entry deterministically when both pod5 and the same
    project's basecalled FASTQ exist under the same assay_type. Defaults to
    "" (don't filter on format)."""
    all_data = _ms._list_resources({"resource_type": "test_data"}, _ms.config).get("test_data", [])
    sequencing = [d for d in all_data if d.get("type") not in ("phenopacket", "pipeline_output")]

    def _score(d: dict) -> int:
        s = 0
        if genome_build and d.get("genome_build") == genome_build: s += 32
        if assay_type   and d.get("assay_type")   == assay_type:   s += 16
        if file_format  and d.get("file_format")  == file_format:  s += 12
        if end_type     and d.get("end_type")     == end_type:     s += 8
        if sample       and d.get("sample")       == sample:       s += 4
        if accession    and d.get("accession")    == accession:    s += 2
        if subset       and d.get("subset")       == subset:       s += 1
        return s

    scored = [(d, _score(d), bool(d.get("available"))) for d in sequencing]
    if not scored:
        return refused("data.no_test_data_on_disk", error="no sequencing test data on disk")
    scored.sort(key=lambda x: (x[2], x[1]), reverse=True)
    best, score, available = scored[0]
    if score == 0:
        return refused(
            "data.no_test_data_match",
            error="no test data matches the requested criteria",
            criteria={
                "genome_build": genome_build, "assay_type": assay_type,
                "end_type": end_type, "sample": sample,
                "accession": accession, "subset": subset,
                "file_format": file_format,
            },
        )

    test_data_ref = {
        "genome_build":    best.get("genome_build", ""),
        "read_type":       best.get("read_type"),
        "end_type":        best.get("end_type"),
        "assay_type":      best.get("assay_type"),
        "sample":          best.get("sample"),
        "accession":       best.get("accession"),
        "subset":          best.get("subset"),
        "num_reads":       best.get("num_reads"),
        "r1":              best.get("r1"),
        "r2":              best.get("r2"),
        # Pod5 / nanopore extensions — None on conventional FASTQ entries; an
        # empty string on file_format would be the same. Filtered below.
        "file_format":     best.get("file_format") if best.get("file_format") != "fastq" else None,
        "chemistry":       best.get("chemistry"),
        "suggested_model": best.get("suggested_model"),
        "core_data_dir":   best.get("core_dir"),
    }
    test_data_ref = {k: v for k, v in test_data_ref.items() if v is not None}

    result = {"test_data": test_data_ref, "available": available, "match_score": score}
    if pipeline_id:
        ok = _ms._pipeline_state.set_test_data(pipeline_id, test_data_ref)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id} if ok
            else {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_pipeline_brief(name: str, version: str = "", hints: dict = {}) -> dict:
    """Return the install brief for `name` — the structured prompt a downstream
    autonomous agent should follow to install this tool end-to-end.

    This is the substrate for fully-autonomous installs. You (the interactive
    Claude) call this to get the canonical instructions; a subagent or batch
    runner can also pull this brief and execute it without human supervision.
    The brief enumerates the invariants the resulting spec must satisfy and
    the primitives the agent should compose to satisfy them. No per-tool prose
    — the brief is short and the same shape for every tool.

    Returns: {pipeline_name, version, hints, invariants, primitives, protocol}.
    """
    invariants = [
        # Layer 1 — the env image (env_honesty.check_build; install==ship is ONE event,
        # so the per-tier env-build invariants I1/I2/I5/I9/I10/I11/I12/I13/I14 collapse
        # into three structural guarantees enforced INSIDE the shipped image):
        "BUILT: the env image + image_digest resolve in the local Docker daemon",
        "VALIDATED_IN_IMAGE: every tool's evidence command re-runs green INSIDE the shipped image AND references the tool (echo/print/true cheats rejected) — because install==ship, the bytes validated are the bytes that run on HPC",
        "POLICY_CLEAN: I12 accelerator honesty (cuda/rocm need toolkit_version; runtime_verified needs a captured probe + min_driver_version; mps is dev_only) + I13 license firewall (gated => redistributable:false AND licenses[] recorded)",
        # Layer 2 — the workflow run (check_workflow_invariants, over the validated run):
        "I0: every top-level list-of-records holds only dicts (shape sanity)",
        "I3: every pipeline_step has validated detected_outputs AND no validation uses expected_type='any' — declare types via run_pipeline_step's output_types",
        "I4: usage.command_template executes against every declared trial AND each produced file passes type-aware validate_output (samtools/bcftools/json.loads/etc — touch-and-hope cheats fail)",
        "I6: every input/output path is absolute AND every {PLACEHOLDER} in usage.command_template is declared",
        "I7: every rc=0 pipeline_step has resource_usage (wall, peak RSS, peak CPU) — populated by run_pipeline_step / run_step_in_container",
        "I8: every step input traces to a prior step's output OR an external source (test_data, reference_databases, runtime_configs, authored_artifacts)",
    ]
    primitives = [
        "install_conda_packages: bioconda / conda-forge / defaults",
        "install_r_package(source=cran|bioconductor|github:owner/repo): handles library isolation + load-or-die",
        "install_pip_package: handles import verification",
        "install_jar_tool: Java tools — openjdk dep + JAR download + wrapper",
        "install_git_repo(repo_url, tool_name, ref=…): clone-and-run repos that aren't packages (academic script collections) — clones into {env}/share/{tool}, pins commit SHA, optional build + smoke verify; sets install_method.type=source. Pin ref to a tag/commit.",
        "download_reference_database: watchdog-safe via run_in_background; auto-records ReferenceDatabase",
        "run_pipeline_step: run + auto-validate every detected output in one call",
        "stage_authored_artifact: record an agent-written file (driver script, synthetic test data, staged BAM/VCF/FASTA) with verbatim content or genesis command + sha256 anchor — required if any step input is something the agent generated outside MCP",
        "start_service(pipeline_id=…): launch a companion service (Redis, Postgres, web server, Spark) and record the readiness probe — required for service-dependent tools; satisfies I10 on a healthy start",
        "verify_service_dependency: append an additional health probe to a declared service's log; satisfies I10 if start_service's initial probe wasn't recorded against this pipeline",
        "phenopacket_to_vcf: materialize a VCF from a registered phenopacket",
    ]
    protocol = [
        "1. start_pipeline(name) — get pipeline_id; thread it through everything",
        "2. install_*_* primitives — compose the env for this tool; primitives handle category details",
        "3. select_test_data + run_pipeline_step (auto-validates outputs; pass output_types to declare file types). If you write a driver script, generate synthetic test data, or stage a transformed file (BAM/VCF/FASTA) outside MCP, call stage_authored_artifact for each — otherwise the I8 walk treats the file as an orphan input.",
        "4. patch_pipeline with usage (command_template + inputs + outputs.files globs + trials[] for multi-shape I4 coverage; empty trials => single inferred trial). NOTE: patch_pipeline only accepts agent-authored keys (usage, notes, runtime_environment, runtime_configs, reference_databases, description, final_summary). Pipeline_steps / install_steps / packages / verifications / authored_artifacts / service_dependencies are runtime-captured and CANNOT be hand-patched — they flow through their dedicated primitives.",
        "5. freeze(env, tools, pipeline_id=…) — Layer 1: build (or adopt by digest) the content-addressed, HPC-shippable env image; non-conda installs are installed + validated INSIDE the ship image (validated==shipped). Returns a freeze_request_key. Docker daemon must be available.",
        "6. run_step_in_container(freeze_request_key, …) — re-run the workflow's steps INSIDE the frozen image so the recorded run is the one that ships (sets validated_in_shipped_image, captures in-container resource_usage).",
        "7. seal_workflow(pipeline_id, freeze_request_key) — Layer 2: validate the run-side invariants (I0/I3/I6/I7/I8), self-test usage.command_template (I4), pin the env BY DIGEST, and write the WorkflowSpec + user guide rendered from the validated run.",
        "8. write_pipeline_provenance with the right input shape",
    ]
    return {
        "pipeline_name": name,
        "version":       version or "latest",
        "hints":         hints,
        "invariants":    invariants,
        "primitives":    primitives,
        "protocol":      protocol,
        "note": (
            "This brief is the entire 'how to install a pipeline' protocol — no per-tool prose. "
            "Compose primitives until the invariants hold; freeze() machine-verifies the env IN "
            "the shipped image and seal_workflow() machine-verifies the run — neither writes a "
            "fake-able artifact."
        ),
    }
