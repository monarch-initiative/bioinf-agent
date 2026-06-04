"""
Resource listing utilities — manifests and pipeline specs.

Read the on-disk genome / test-data manifests and the env_reports/ pipeline
specs into structured dicts for the MCP tools list_available_resources and
list_installed_pipelines.
"""

from pathlib import Path

import yaml

from agent.models.core_data import PipelineSpec


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


def list_pipelines(config: dict) -> dict:
    """Read all finalized pipeline spec YAMLs from env_reports/ and return
    summary dicts. *.draft.yaml files (in-progress drafts from the pipeline
    state accumulator) are skipped."""
    pipelines_dir = Path(config["paths"]["pipelines_dir"])
    pipelines = []

    for spec_file in sorted(pipelines_dir.glob("*.yaml")):
        if spec_file.name.endswith(".draft.yaml"):
            continue
        try:
            pspec = PipelineSpec.from_yaml(spec_file)
            docker = pspec.docker
            pipelines.append({
                "name": pspec.pipeline_name,
                "description": pspec.description,
                "conda_env": pspec.conda_env,
                "env_status": pspec.env_status,
                "pipeline_status": pspec.pipeline_status,
                "created_at": pspec.created_at,
                "docker_image": docker.image_tag if docker else None,
                "docker_built": docker.build_success if docker else False,
                "packages": [
                    {"name": p.name, "version": p.resolved_version or p.requested_version}
                    for p in pspec.packages if p.name != "conda-pack"
                ],
                "install_steps_total": len(pspec.install_steps),
                "install_steps_failed": sum(
                    1 for s in pspec.install_steps if s.returncode not in (None, 0)
                ),
                "pipeline_steps_validated": sum(
                    1 for s in pspec.pipeline_steps if s.validation_status == "passed"
                ),
                "pipeline_steps_ran_clean": sum(
                    1 for s in pspec.pipeline_steps if s.returncode == 0
                ),
                "pipeline_steps_total": len(pspec.pipeline_steps),
            })
        except Exception as e:
            pipelines.append({"file": spec_file.name, "error": str(e)})

    return {"pipelines": pipelines, "count": len(pipelines)}
