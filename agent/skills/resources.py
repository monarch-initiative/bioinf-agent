"""
Resource listing utilities — manifests and pipeline specs.

Read the on-disk genome / test-data manifests, and the two layers of built
artifacts (frozen envs from the EnvCache + sealed WorkflowSpecs), into
structured dicts for the MCP tools list_available_resources and
list_installed_pipelines.
"""

from pathlib import Path

import yaml



def list_resources(inputs: dict, config: dict) -> dict:
    """
    Read core_test_data manifests and return structured resource info.

    inputs["resource_type"]: "genomes" | "test_data" | "both"
    config["paths"]["data_dir"]: root data directory (relative or absolute)
    """
    resource_type = inputs["resource_type"]
    result: dict = {}
    data_dir = Path(config["paths"]["data_dir"])

    genomes = []
    test_data = []

    for core_dir in sorted(data_dir.glob("core_test_data_*")):
        manifest_path = core_dir / "manifest.yaml"
        if not manifest_path.exists():
            continue
        with open(manifest_path) as f:
            m = yaml.safe_load(f) or {}

        build = m.get("genome_build", core_dir.name.replace("core_test_data_", ""))
        chrom = m.get("chromosome_subset", "")

        if resource_type in ("genomes", "both"):
            ginfo = m.get("genome", {})
            if ginfo:
                fasta = core_dir / ginfo.get("fasta", "")
                genomes.append({
                    "id": f"{build}_{chrom}" if chrom else build,
                    "build": build,
                    "chromosome_subset": chrom,
                    "fasta": str(fasta),
                    "available": fasta.exists(),
                    "indexes": list(ginfo.get("indexes", {}).keys()),
                    "core_dir": str(core_dir),
                })

        if resource_type in ("test_data", "both"):
            # Sequencing data. The directory tree (short_read/{end_or_platform}/{assay_type}/)
            # is just for organization — the authoritative read_type/end_type/assay_type/
            # platform values live on each per-sample record.  Long-read entries use the
            # platform name (pacbio/ont) in the directory slot where short-read uses
            # end_type, so reading from the per-sample record is the only correct path.
            for _read_type_key, level2 in m.get("sequencing_data", {}).items():
                if not isinstance(level2, dict):
                    continue
                for _level2_key, level3 in level2.items():
                    if not isinstance(level3, dict):
                        continue
                    for _level3_key, samples in level3.items():
                        if not isinstance(samples, list):
                            continue
                        for smp in samples:
                            for subset, sinfo in smp.get("subsets", {}).items():
                                if not isinstance(sinfo, dict):
                                    continue
                                r1 = core_dir / sinfo["r1"] if sinfo.get("r1") else None
                                test_data.append({
                                    "id": f"{build}_{smp.get('assay_type','')}_{smp.get('accession', '')}_{subset}",
                                    "genome_build": build,
                                    "read_type": smp.get("read_type", ""),
                                    "end_type":  smp.get("end_type", ""),
                                    "assay_type": smp.get("assay_type", ""),
                                    "platform":  smp.get("platform", ""),
                                    "sample": smp.get("sample", ""),
                                    "accession": smp.get("accession", ""),
                                    "subset": subset,
                                    "num_reads": sinfo.get("num_reads"),
                                    "available": sinfo.get("available", False) and (r1.exists() if r1 else False),
                                    "r1": str(core_dir / sinfo["r1"]) if sinfo.get("r1") else None,
                                    "r2": str(core_dir / sinfo["r2"]) if sinfo.get("r2") else None,
                                    # Pod5 / nanopore raw-signal metadata (defaults to "fastq" /
                                    # None on conventional entries). Surfaced so select_test_data
                                    # + downstream basecaller pipelines can route by file_format
                                    # / chemistry / suggested_model.
                                    "file_format":     smp.get("file_format", "fastq"),
                                    "chemistry":       smp.get("chemistry"),
                                    "flowcell":        smp.get("flowcell"),
                                    "kit":             smp.get("kit"),
                                    "suggested_model": smp.get("suggested_model"),
                                    "core_dir": str(core_dir),
                                })

            # Phenopackets
            for pk in m.get("phenopackets", []):
                if not isinstance(pk, dict):
                    continue
                test_data.append({
                    "id":              f"{build}_phenopacket_{pk.get('phenopacket_id', '')}",
                    "genome_build":    build,
                    "type":            "phenopacket",
                    "phenopacket_id":  pk.get("phenopacket_id", ""),
                    "subject_id":      pk.get("subject_id", ""),
                    "sex":             pk.get("sex"),
                    "genes":           pk.get("genes", []),
                    "diseases":        pk.get("diseases", []),
                    "hpo_terms":       pk.get("hpo_terms", []),
                    "variants":        pk.get("variants", []),
                    "genome_assembly": pk.get("genome_assembly", ""),
                    "available":       pk.get("available", False),
                    "file":            str(core_dir / pk["file"]) if pk.get("file") else None,
                    "source_url":      pk.get("source_url", ""),
                    "core_dir":        str(core_dir),
                })

            # Pipeline outputs
            for pipeline_name, pout in m.get("pipeline_outputs", {}).items():
                if not isinstance(pout, dict):
                    continue
                for sample_key, sout in pout.get("samples", {}).items():
                    if not isinstance(sout, dict):
                        continue
                    files = [
                        {
                            "path": str(core_dir / f["path"]),
                            "type": f.get("type"),
                            "exists": (core_dir / f["path"]).exists(),
                        }
                        for f in sout.get("files", [])
                    ]
                    test_data.append({
                        "id": f"{build}_pipeline_output_{pipeline_name}_{sample_key}",
                        "genome_build": build,
                        "type": "pipeline_output",
                        "pipeline": pipeline_name,
                        "sample": sample_key,
                        "upstream_pipelines": pout.get("upstream_pipelines", []),
                        "available": pout.get("available", False),
                        "files": files,
                        "provenance": str(core_dir / sout["provenance"]) if sout.get("provenance") else None,
                        "core_dir": str(core_dir),
                    })

    if resource_type in ("genomes", "both"):
        result["genomes"] = genomes
    if resource_type in ("test_data", "both"):
        result["test_data"] = test_data

    return result


def _semantic_versions(record: dict) -> list[dict]:
    """The REQUESTED tools with human-readable semantic versions, read off the
    shipped image's SBOM. Never image/build hashes — this is the string a user
    cites in a paper."""
    resolved = {p.get("name"): p.get("version")
                for p in (record.get("resolved_packages") or [])
                if isinstance(p, dict)}
    out = []
    for spec in (record.get("requested_tools") or []):
        name = str(spec).split("=")[0].strip()
        if not name:
            continue
        pinned = str(spec).split("=", 1)[1].strip() if "=" in str(spec) else ""
        out.append({"tool": name, "version": resolved.get(name) or pinned or None})
    return out


def list_pipelines(config: dict, env_cache=None) -> dict:
    """Inventory of what has ALREADY been built, in both layers.

    Layer 1 — `envs`: the frozen, content-addressed envs in the EnvCache. These
    are the reusable "solved components": ask for one of these tools again and
    freeze serves it by digest instead of re-solving.
    Layer 2 — `workflows`: the sealed WorkflowSpecs (`*.workflow.yaml`), each
    pinning its env BY DIGEST.

    HONESTY: every env is re-anchored against the full contract before it is
    listed (`EnvCache.contract_violations`, the same check freeze/run/stage/seal
    ask at serve time), so `contract_ok: False` means "on disk but would NOT be
    served today". Listing a record as though it were usable when the serving
    paths would refuse it is exactly the false green tier 5 closed.

    Rewritten in tier 7. It previously parsed every `*.yaml` in env_reports/ as a
    `PipelineSpec` — a model whose producer (finalize_pipeline/save_pipeline_spec)
    was RETIRED in the re-spine. So it matched only WorkflowSpec/recipe yamls,
    every parse raised, a try/except turned each into an `{file, error}` dict, and
    the tool returned `count: 7` having understood exactly none of them. Broken
    for real users, and green in the suite: the disease in a user-facing tool.
    """
    pipelines_dir = Path(config["paths"]["pipelines_dir"])

    # --- Layer 1: frozen envs ------------------------------------------------
    envs: list[dict] = []
    if env_cache is None:
        from agent.skills.freeze import EnvCache
        env_cache = EnvCache(pipelines_dir / "_env_cache.json")
    for key, rec in sorted((env_cache.all() or {}).items()):
        if not isinstance(rec, dict):
            continue
        violations = env_cache.contract_violations(rec)
        envs.append({
            "request_key":      key,
            "name":             rec.get("name"),
            "tools":            _semantic_versions(rec),
            "image":            rec.get("image"),
            "image_digest":     rec.get("image_digest"),
            "content_digest":   rec.get("content_digest"),
            "build_method":     rec.get("mode"),
            "platform":         rec.get("platform"),
            "validation_locus": rec.get("validation_locus"),
            "created_at":       rec.get("created_at"),
            "license_gated":    bool(rec.get("license_gated")),
            # The green is EARNED here, not remembered from freeze time.
            "contract_ok":          not violations,
            "contract_violations":  violations,
        })

    # --- Layer 2: sealed workflows -------------------------------------------
    workflows: list[dict] = []
    for spec_file in sorted(pipelines_dir.glob("*.workflow.yaml")):
        try:
            d = yaml.safe_load(spec_file.read_text()) or {}
            steps = d.get("pipeline_steps") or []
            workflows.append({
                "workflow_name":  d.get("workflow_name"),
                "description":    d.get("description"),
                "created_at":     d.get("created_at"),
                "env_request_key":    d.get("env_request_key"),
                "env_image":          d.get("env_image"),
                "env_content_digest": d.get("env_content_digest"),
                "envs":               d.get("envs") or [],
                "validated_in_shipped_image": bool(d.get("validated_in_shipped_image")),
                "usage_verified":             bool(d.get("usage_verified")),
                "steps_total":     len(steps),
                "steps_validated": sum(
                    1 for s in steps
                    if isinstance(s, dict) and s.get("validation_status") == "passed"),
                "path": str(spec_file),
            })
        except Exception as e:      # a malformed artifact is reported, never swallowed
            workflows.append({"file": spec_file.name, "error": str(e)})

    return {
        "envs": envs,
        "workflows": workflows,
        "counts": {
            "envs": len(envs),
            "envs_contract_ok": sum(1 for e in envs if e.get("contract_ok")),
            "workflows": len(workflows),
        },
    }
