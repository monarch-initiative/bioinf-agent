"""
Resource listing utilities — manifests and pipeline specs.

Extracted here so both agent/tools.py (outer dispatcher) and
agent/skills/install_pipeline.py (sub-agent) can import without a
circular dependency (tools.py imports InstallPipelineSkill; if
install_pipeline.py imported from tools.py that would be circular).
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
            # Sequencing data
            for read_type, end_types in m.get("sequencing_data", {}).items():
                if not isinstance(end_types, dict):
                    continue
                for end_type, assay_types in end_types.items():
                    if not isinstance(assay_types, dict):
                        continue
                    for assay_type, samples in assay_types.items():
                        if not isinstance(samples, list):
                            continue
                        for smp in samples:
                            for subset, sinfo in smp.get("subsets", {}).items():
                                if not isinstance(sinfo, dict):
                                    continue
                                r1 = core_dir / sinfo["r1"] if sinfo.get("r1") else None
                                test_data.append({
                                    "id": f"{build}_{assay_type}_{smp.get('accession', '')}_{subset}",
                                    "genome_build": build,
                                    "read_type": read_type,
                                    "end_type": end_type,
                                    "assay_type": assay_type,
                                    "sample": smp.get("sample", ""),
                                    "accession": smp.get("accession", ""),
                                    "subset": subset,
                                    "num_reads": sinfo.get("num_reads"),
                                    "available": sinfo.get("available", False) and (r1.exists() if r1 else False),
                                    "r1": str(core_dir / sinfo["r1"]) if sinfo.get("r1") else None,
                                    "r2": str(core_dir / sinfo["r2"]) if sinfo.get("r2") else None,
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
    """Read all pipeline spec YAMLs from env_reports/ and return summary dicts."""
    pipelines_dir = Path(config["paths"]["pipelines_dir"])
    pipelines = []

    for spec_file in sorted(pipelines_dir.glob("*.yaml")):
        try:
            pspec = PipelineSpec.from_yaml(spec_file)
            docker = pspec.docker
            pipelines.append({
                "name": pspec.pipeline_name,
                "description": pspec.description,
                "conda_env": pspec.conda_env,
                "status": pspec.status,
                "created_at": pspec.created_at,
                "docker_image": docker.image_tag if docker else None,
                "docker_built": docker.build_success if docker else False,
                "packages": [
                    {"name": p.name, "version": p.resolved_version or p.requested_version}
                    for p in pspec.packages if p.name != "conda-pack"
                ],
                "steps_validated": sum(
                    1 for s in pspec.pipeline_steps if s.status == "validated"
                ),
                "steps_total": len(pspec.pipeline_steps),
            })
        except Exception as e:
            pipelines.append({"file": spec_file.name, "error": str(e)})

    return {"pipelines": pipelines, "count": len(pipelines)}
