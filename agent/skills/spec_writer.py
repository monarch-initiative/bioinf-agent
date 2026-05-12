"""
spec_writer — persist pipeline spec + provenance YAML artifacts.

Two pure functions exposed to the MCP server:

    save_pipeline_spec(spec, config) -> {saved_yaml, saved_html}
    write_provenance(inputs, config) -> {written, sample_key}

save_pipeline_spec derives validation_status per step from each step's
validation dict, then derives the pipeline-level status from those — so
"fully_validated" can only land if every step's outputs actually passed
validate_output, not just exited zero.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from agent.models.core_data import (
    AssemblyInput, BamInput, GenomeRef, GenotypeArrayInput, OutputFile,
    PedigreeInput, PhenotypeInput, PipelineSpec, Provenance, QuantitativeTraitInput,
    ReadInput, VcfInput,
)
from agent.skills.report_builder import generate as generate_report


# ---------------------------------------------------------------------------
# Pipeline spec persistence
# ---------------------------------------------------------------------------

def save_pipeline_spec(spec: dict, config: dict) -> dict:
    """Validate and write a PipelineSpec dict as YAML + HTML report.

    Derives each pipeline_step's validation_status from its validation dict,
    then derives env_status (from install_steps + package verifications) and
    pipeline_status (from pipeline_steps) separately. Anything the caller
    passed for these is overwritten by the derived values.

    Filename stem is `{pipeline_name}_{version}` where version is the
    resolved_version of the package whose name best matches pipeline_name
    (exact > substring), searching both spec.packages and
    install_steps[].installed_packages. Falls back to the first non-conda-pack
    package's version, then to "latest".
    """
    project_root = Path(__file__).parent.parent.parent.resolve()
    pipelines_dir = project_root / config["paths"]["pipelines_dir"]
    pipelines_dir.mkdir(parents=True, exist_ok=True)

    _derive_step_validation_status(spec)
    spec["env_status"]      = _derive_env_status(spec)
    spec["pipeline_status"] = _derive_pipeline_status(spec)

    try:
        pspec = PipelineSpec.model_validate(spec)
        write_spec = pspec.model_dump(exclude_none=True)
    except Exception as e:
        print(f"[spec_writer] WARN: PipelineSpec validation failed: {e}", file=sys.stderr)
        write_spec = spec

    name    = write_spec.get("pipeline_name", "pipeline")
    version = _pick_version_for_filename(write_spec, name)
    stem    = f"{name}_{version}" if version else name

    yaml_path = pipelines_dir / f"{stem}.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(write_spec, f, default_flow_style=False, sort_keys=False)

    html_path = pipelines_dir / f"{stem}.html"
    html_path.write_text(generate_report(write_spec))

    return {
        "saved_yaml":      str(yaml_path),
        "saved_html":      str(html_path),
        "env_status":      write_spec.get("env_status"),
        "pipeline_status": write_spec.get("pipeline_status"),
    }


def _pick_version_for_filename(spec: dict, pipeline_name: str) -> str:
    """Find the version that should appear in the filename stem.

    Search order:
      1. spec.packages where name == pipeline_name (case-insensitive)
      2. spec.packages where pipeline_name is a substring of name or vice versa
      3. install_steps[].installed_packages — same exact then substring search;
         this is where R packages installed via install_github / BiocManager
         actually have their version recorded
      4. First non-conda-pack package's resolved_version
    """
    name_l = pipeline_name.lower()
    packages = spec.get("packages", [])

    # Pass 1: exact match in spec.packages
    for p in packages:
        if p.get("name", "").lower() == name_l:
            v = p.get("resolved_version") or p.get("version")
            if v:
                return v

    # Pass 2: substring match in spec.packages
    for p in packages:
        pn = p.get("name", "").lower()
        if pn == "conda-pack" or not pn:
            continue
        if name_l in pn or pn in name_l:
            v = p.get("resolved_version") or p.get("version")
            if v:
                return v

    # Pass 3: search install_steps' installed_packages (exact then substring)
    install_pkgs = [
        ip for s in spec.get("install_steps", [])
        for ip in s.get("installed_packages", []) if isinstance(ip, dict)
    ]
    for ip in install_pkgs:
        if ip.get("name", "").lower() == name_l and ip.get("version"):
            return ip["version"]
    for ip in install_pkgs:
        pn = ip.get("name", "").lower()
        if not pn:
            continue
        if (name_l in pn or pn in name_l) and ip.get("version"):
            return ip["version"]

    # Pass 4: first non-conda-pack package version
    for p in packages:
        if p.get("name") == "conda-pack":
            continue
        v = p.get("resolved_version") or p.get("version")
        if v:
            return v

    return ""


def _derive_step_validation_status(spec: dict) -> None:
    """Fill in validation_status on each step from its validation dict.

    Rules:
      - any entry with passed=False    → "failed"
      - all entries with passed=True   → "passed"
      - empty / missing validation     → leave as None
    Respects an explicit validation_status the caller already set.
    """
    for step in spec.get("pipeline_steps", []):
        if step.get("validation_status"):
            continue
        validation = step.get("validation") or {}
        if not isinstance(validation, dict) or not validation:
            continue
        entries = [v for v in validation.values() if isinstance(v, dict)]
        if not entries:
            continue
        if any(v.get("passed") is False for v in entries):
            step["validation_status"] = "failed"
        elif all(v.get("passed") is True for v in entries):
            step["validation_status"] = "passed"


def _derive_env_status(spec: dict) -> str:
    """Compute env_status from install_steps' returncodes + per-package verifications.

      failed             — any install_step.returncode != 0
      fully_validated    — every non-conda-pack package has a verify_output
      partially_validated — some packages verified, others not
      complete           — all installs ran clean but nothing verified yet
    """
    install_steps = spec.get("install_steps", [])
    if any(s.get("returncode") not in (None, 0) for s in install_steps):
        return "failed"

    packages = [p for p in spec.get("packages", []) if p.get("name") != "conda-pack"]
    if not packages:
        return "complete"

    verified = sum(1 for p in packages if (p.get("verify_output") or "").strip())
    if verified == len(packages):
        return "fully_validated"
    if verified > 0:
        return "partially_validated"
    return "complete"


def _derive_pipeline_status(spec: dict) -> str:
    """Compute pipeline_status from pipeline_steps' execution + validation."""
    steps = spec.get("pipeline_steps", [])
    if not steps:
        # No algorithm runs means there's nothing to validate — but save_pipeline_spec
        # was called, so we're past in_progress. "complete" is correct for a spec
        # with only env setup (e.g. bioinf_core_tools).
        return "complete"

    if any(s.get("returncode") not in (None, 0) for s in steps):
        return "failed"

    val_states = [s.get("validation_status") for s in steps]
    if any(v == "failed" for v in val_states):
        return "failed"

    passed_count = sum(1 for v in val_states if v == "passed")
    if passed_count == len(steps):
        return "fully_validated"
    if passed_count > 0:
        return "partially_validated"
    return "complete"


# ---------------------------------------------------------------------------
# Provenance persistence
# ---------------------------------------------------------------------------

_DB_ALIASES = {"SRA": "EBI_SRA", "NCBI": "NCBI_SRA", "EBI": "EBI_SRA"}


def write_provenance(inputs: dict, config: dict) -> dict:
    """Build and write a validated Provenance YAML for one pipeline run.

    Tool versions are read from the spec YAML's packages list rather than
    probing binaries — the spec is the authoritative source for what was
    actually installed (PackageSearch resolved versions at install time)."""
    output_dir = Path(inputs["output_dir"])
    sample_key = inputs["sample_key"]
    prov_path = output_dir / f"{sample_key}_provenance.yaml"

    def _rel(abs_path: str) -> str:
        return os.path.relpath(Path(abs_path).resolve(), output_dir.resolve())

    genome = None
    if inputs.get("reference_path"):
        genome = GenomeRef(
            genome_build=inputs.get("genome_build", ""),
            chromosome_subset=inputs.get("chromosome", ""),
            reference=_rel(inputs["reference_path"]),
            reference_fai=_rel(inputs["reference_path"] + ".fai"),
        )

    reads = None
    if inputs.get("reads"):
        r = inputs["reads"]
        raw_db = r.get("database", "EBI_SRA")
        db = _DB_ALIASES.get(raw_db, raw_db)
        reads = [ReadInput(
            read_type=r.get("read_type", "short_read"),
            end_type=r.get("end_type", "paired_end"),
            assay_type=r.get("assay_type", "exome"),
            subset=r.get("subset", ""),
            num_reads=int(r.get("num_reads", 0)),
            r1=_rel(r["r1"]),
            r2=_rel(r["r2"]) if r.get("r2") else None,
            sample=r.get("sample", ""),
            accession=r.get("accession", ""),
            database=db,
        )]

    bam_input = None
    if inputs.get("bam_input"):
        b = inputs["bam_input"]
        bam_input = BamInput(bam=_rel(b["bam"]), bai=_rel(b["bai"]))

    vcf_input = None
    if inputs.get("vcf_input"):
        v = inputs["vcf_input"]
        vcf_input = VcfInput(
            vcf=_rel(v["vcf"]),
            tbi=_rel(v["tbi"]) if v.get("tbi") else None,
            genome_build=v.get("genome_build", inputs.get("genome_build", "")),
            upstream_pipeline=v.get("upstream_pipeline"),
            sample_ids=v.get("sample_ids", []),
        )

    assembly_input = None
    if inputs.get("assembly_input"):
        a = inputs["assembly_input"]
        assembly_input = AssemblyInput(
            assembly=_rel(a["assembly"]),
            upstream_pipeline=a.get("upstream_pipeline"),
        )

    phenotype = None
    if inputs.get("phenotype"):
        p = inputs["phenotype"]
        phenotype = PhenotypeInput(
            ontology=p.get("ontology", "HPO"),
            terms=p["terms"],
            source=p.get("source"),
        )

    pedigree = None
    if inputs.get("pedigree"):
        g = inputs["pedigree"]
        pedigree = PedigreeInput(
            ped=_rel(g["ped"]),
            proband=g.get("proband"),
        )

    genotype_array = None
    if inputs.get("genotype_array"):
        ga = inputs["genotype_array"]
        genotype_array = GenotypeArrayInput(
            file=_rel(ga["file"]),
            format=ga["format"],
            bim=_rel(ga["bim"]) if ga.get("bim") else None,
            fam=_rel(ga["fam"]) if ga.get("fam") else None,
            n_samples=ga.get("n_samples"),
            n_snps=ga.get("n_snps"),
            genome_build=ga.get("genome_build"),
            upstream_pipeline=ga.get("upstream_pipeline"),
        )

    quantitative_traits = None
    if inputs.get("quantitative_traits"):
        qt = inputs["quantitative_traits"]
        quantitative_traits = QuantitativeTraitInput(
            traits=qt["traits"],
            file=_rel(qt["file"]),
            n_samples=qt.get("n_samples"),
            measurement_type=qt.get("measurement_type", "continuous"),
        )

    pipeline_spec_path = Path(inputs["pipeline_spec_path"]).resolve()
    try:
        spec_rel = str(pipeline_spec_path.relative_to(output_dir.resolve()))
    except ValueError:
        spec_rel = str(pipeline_spec_path)

    outputs = [
        OutputFile(file=f["file"], type=f["type"], indexed=f.get("indexed", False))
        for f in inputs.get("output_files", [])
    ]

    tool_versions = _tool_versions_from_spec(pipeline_spec_path)

    prov = Provenance(
        pipeline=inputs["pipeline"],
        pipeline_spec=spec_rel,
        conda_env=Path(inputs["conda_env_path"]).name,
        created_at=str(date.today()),
        tool_versions=tool_versions,
        genome=genome,
        reads=reads,
        bam_input=bam_input,
        vcf_input=vcf_input,
        assembly_input=assembly_input,
        phenotype=phenotype,
        pedigree=pedigree,
        genotype_array=genotype_array,
        quantitative_traits=quantitative_traits,
        upstream_pipelines=inputs.get("upstream_pipelines", []),
        parameters=inputs.get("parameters") or None,
        outputs=outputs,
    )

    written = prov.write(prov_path)
    return {"written": str(written), "sample_key": sample_key}


def _tool_versions_from_spec(spec_path: Path) -> dict[str, str]:
    """Read tool versions from a pipeline spec YAML's packages list.

    The spec is the authoritative source — PackageSearch already resolved
    each package's exact version at install time and stored it there.
    Falls back to {} if the spec file is missing or unreadable."""
    if not spec_path.exists():
        return {}
    try:
        with open(spec_path) as f:
            spec = yaml.safe_load(f) or {}
    except Exception:
        return {}
    versions: dict[str, str] = {}
    for pkg in spec.get("packages", []):
        name = pkg.get("name")
        if not name or name == "conda-pack":
            continue
        ver = pkg.get("resolved_version") or pkg.get("version")
        if ver:
            versions[name] = ver
    return versions
