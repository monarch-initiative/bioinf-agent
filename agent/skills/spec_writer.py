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
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

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

def save_pipeline_spec(spec: dict, config: dict, env_manager: Optional[Any] = None) -> dict:
    """Validate and write a PipelineSpec dict as YAML + HTML report.

    Derives each pipeline_step's validation_status from its validation dict,
    then derives env_status (from install_steps + package verifications) and
    pipeline_status (from pipeline_steps) separately. Anything the caller
    passed for these is overwritten by the derived values.

    If env_manager is provided, runs a single reconciliation pass against the
    live conda env: re-queries actual installed versions, fills missing
    install_method types, and derives homepages from (channel, name, source).
    This is the source-of-truth pass — eliminates drift between what was
    requested at search-time and what the solver actually installed.

    Filename stem is `{pipeline_name}_{version}` where version is the
    resolved_version of the package whose name best matches pipeline_name
    (exact > substring), searching both spec.packages and
    install_steps[].installed_packages. Falls back to the first non-conda-pack
    package's version, then to "latest".
    """
    project_root = Path(__file__).parent.parent.parent.resolve()
    pipelines_dir = project_root / config["paths"]["pipelines_dir"]
    pipelines_dir.mkdir(parents=True, exist_ok=True)

    if env_manager is not None and spec.get("conda_env"):
        derive_packages_from_env(spec, env_manager, spec["conda_env"])
        reconcile_packages_with_env(spec, env_manager, spec["conda_env"])

    derive_step_dag(spec)
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

    # Reproducibility artifacts: environment.yml is the portable conda recipe
    # (anyone can `conda env create -f` it on any platform); .lock is the
    # URL-pinned explicit list for bit-exact recreation on the build platform.
    env_yml_path: Optional[Path] = None
    lock_path:    Optional[Path] = None
    if env_manager is not None and write_spec.get("conda_env"):
        env_name = write_spec["conda_env"]
        env_yml = env_manager.export_environment_yml(env_name, from_history=True)
        if env_yml:
            env_yml_path = pipelines_dir / f"{stem}.environment.yml"
            env_yml_path.write_text(env_yml)
        lock = env_manager.export_explicit_lock(env_name)
        if lock:
            lock_path = pipelines_dir / f"{stem}.lock"
            lock_path.write_text(lock)

    return {
        "saved_yaml":      str(yaml_path),
        "saved_html":      str(html_path),
        "saved_env_yml":   str(env_yml_path) if env_yml_path else None,
        "saved_lock":      str(lock_path)    if lock_path    else None,
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
    """Compute env_status from install_steps.

    The structural truth that the env was built correctly is:
      - every install_step exited 0
      - conda list (queried at finalize) found the packages
      - the derived packages list is non-empty

    Per-package verify_installation calls are advisory now — they enrich the
    report but don't gate the status. This lets the LLM skip the "verify each
    package with a custom command" step entirely for SRA-agent-driven flows
    where dozens of packages are installed in one go.

      failed             — any install_step.returncode != 0
      fully_validated    — all installs ran clean AND packages list is non-empty
      complete           — all installs ran clean but no packages derived (unusual)
    """
    install_steps = spec.get("install_steps", [])
    if any(s.get("returncode") not in (None, 0) for s in install_steps):
        return "failed"
    packages = [p for p in spec.get("packages", []) if p.get("name") != "conda-pack"]
    return "fully_validated" if packages else "complete"


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
# Environment reconciliation
#
# The draft accumulates what we *requested* during install — search_package
# returns the channel's "latest", but the solver may pin a different version
# based on co-installed packages' constraints (a downgrade for r-base 4.4,
# a newer build pulled in transitively, etc). At finalize-time we probe the
# live env and patch each PackageRecord so the saved spec reflects truth.
#
# Single probe per env (one conda list + one Rscript per r_install package).
# Pure templating for homepage and install_method defaults.
# ---------------------------------------------------------------------------

_R_INSTALL_GITHUB_REGEX = re.compile(r"""['"]([^'"\s]+/[^'"\s]+)['"]""")

# Packages that are infrastructure (not user-facing tools) and shouldn't be
# surfaced as primary spec entries. conda-pack is added to every env automatically.
_INFRASTRUCTURE_PACKAGES = frozenset({"conda-pack"})


def derive_packages_from_env(spec: dict, env_manager: Any, env_name: str) -> dict:
    """Rebuild spec['packages'] from sources of truth: `conda list` + every
    install_step's `installed_packages` array.

    This replaces the accumulator-based packages list (which drifted from the
    actual install) with one derived directly from what conda + run_install_command
    actually did. Annotations (description, homepage, channel) recorded earlier
    via search_package or run_install_command are preserved by name when possible.

    Returns a summary dict for diagnostics.
    """
    cache         = spec.get("search_cache",  {}) or {}
    verifications = spec.get("verifications", {}) or {}
    # Preserve annotations from any prior `packages` list (description, channel,
    # homepage, verify_*, install_method.source, etc.) keyed by name.
    prior_by_name: dict[str, dict] = {
        p.get("name"): p for p in spec.get("packages", []) or []
        if isinstance(p, dict) and p.get("name")
    }

    conda_versions = env_manager.list_conda_packages(env_name)
    # Only user-requested conda packages belong in spec.packages — not the full
    # transitive closure (samtools brings in 100 dynamic-link deps you didn't ask
    # for). The lock file captures the closure for reproducibility; this list
    # is the user-facing tool roster.
    explicit = env_manager.list_explicit_conda_packages(env_name)
    if explicit:
        conda_versions = {n: v for n, v in conda_versions.items() if n in explicit}

    # Collect non-conda packages from install_steps. install_steps records each
    # run_install_command's installed_packages, which captures JARs (Exomiser),
    # R packages from CRAN/Bioc/GitHub, pip wheels, anything outside conda.
    install_step_packages: dict[str, dict] = {}
    for step in spec.get("install_steps", []) or []:
        if not isinstance(step, dict):
            continue
        # Only trust successful install steps as a source of truth.
        if step.get("returncode") not in (None, 0):
            continue
        for ip in step.get("installed_packages", []) or []:
            if not isinstance(ip, dict):
                continue
            name = ip.get("name")
            if not name or name in _INFRASTRUCTURE_PACKAGES:
                continue
            # If the same name appears in conda list, conda owns it; skip.
            # Otherwise this is a non-conda install (R / pip / JAR / etc).
            if name in conda_versions:
                continue
            install_step_packages[name] = ip

    rebuilt: list[dict] = []

    # 1) Every conda-tracked package
    for name in sorted(conda_versions):
        if name in _INFRASTRUCTURE_PACKAGES:
            continue
        prior = prior_by_name.get(name, {})
        v = verifications.get(name, {})
        rec = {
            "name":              name,
            "requested_version": prior.get("requested_version") or "latest",
            "resolved_version":  conda_versions[name],
            "channel":           prior.get("channel"),
            "install_method":    prior.get("install_method") or {"type": "conda"},
            "description":       prior.get("description") or cache.get(name, {}).get("description"),
            "homepage":          prior.get("homepage") or cache.get(name, {}).get("homepage"),
            "verify_command":    v.get("verify_command") or prior.get("verify_command"),
            "verify_output":     v.get("verify_output")  or prior.get("verify_output"),
        }
        rebuilt.append({k: v for k, v in rec.items() if v is not None})

    # 2) Every non-conda install_step package
    for name, ip in install_step_packages.items():
        prior = prior_by_name.get(name, {})
        v = verifications.get(name, {})
        channel = ip.get("channel") or prior.get("channel", "")
        im_type = (
            (prior.get("install_method") or {}).get("type")
            or _CHANNEL_TO_INSTALL_METHOD.get((channel or "").lower(), "source")
        )
        install_method = {"type": im_type}
        if ip.get("source") or (prior.get("install_method") or {}).get("source"):
            install_method["source"] = ip.get("source") or prior["install_method"]["source"]
        rec = {
            "name":              name,
            "requested_version": prior.get("requested_version") or ip.get("requested_version") or ip.get("version") or "latest",
            "resolved_version":  ip.get("version") or prior.get("resolved_version"),
            "channel":           channel or None,
            "install_method":    install_method,
            "description":       prior.get("description") or cache.get(name, {}).get("description"),
            "homepage":          prior.get("homepage") or cache.get(name, {}).get("homepage"),
            "verify_command":    v.get("verify_command") or prior.get("verify_command"),
            "verify_output":     v.get("verify_output")  or prior.get("verify_output"),
        }
        rebuilt.append({k: v for k, v in rec.items() if v is not None})

    spec["packages"] = rebuilt
    return {
        "conda_count":         len(conda_versions),
        "install_step_count":  len(install_step_packages),
        "rebuilt_count":       len(rebuilt),
    }


# Mirror of _DERIVE_INSTALL_METHOD_TYPE in mcp_server; duplicated here so
# spec_writer can rebuild packages without importing from mcp_server (which
# would create a cycle via FastMCP).
_CHANNEL_TO_INSTALL_METHOD: dict[str, str] = {
    "github":        "r_install",
    "cran":          "r_install",
    "bioconductor":  "r_install",
    "pip":           "pip",
    "pypi":          "pip",
    "conda-forge":   "conda",
    "bioconda":      "conda",
    "noarch":        "conda",
    "docker":        "docker_pull",
}


def derive_step_dag(spec: dict) -> None:
    """For each pipeline_step missing `depends_on`, derive it from the
    input/output overlap with earlier steps.

    Specifically: if step N's inputs contain a path that appears in step M's
    outputs (M < N), then N depends_on M. The result is a partial order that
    a downstream tool (Nextflow generator, parallel scheduler) can consume.
    """
    steps = spec.get("pipeline_steps", []) or []
    if not steps:
        return

    # Build a map: output_path -> step_number that produced it.
    produced_by: dict[str, int] = {}
    for s in steps:
        if not isinstance(s, dict):
            continue
        step_num = s.get("step")
        if step_num is None:
            continue
        for out in s.get("outputs", []) or []:
            produced_by[out] = step_num

    for s in steps:
        if not isinstance(s, dict):
            continue
        if s.get("depends_on"):
            continue   # caller-provided; trust it
        step_num = s.get("step")
        if step_num is None:
            continue
        deps: set[int] = set()
        for entry in s.get("inputs", []) or []:
            # Inputs are StepInput dicts after model coercion; the YAML may
            # still see strings if hand-edited. Handle both.
            paths: list[str] = []
            if isinstance(entry, dict):
                if entry.get("path"):
                    paths.append(entry["path"])
                paths.extend(entry.get("references", []) or [])
            elif isinstance(entry, str):
                paths.append(entry)
            for p in paths:
                producer = produced_by.get(p)
                if producer is not None and producer < step_num:
                    deps.add(producer)
        if deps:
            s["depends_on"] = sorted(deps)


def reconcile_packages_with_env(spec: dict, env_manager: Any, env_name: str) -> dict:
    """Patch spec.packages in place against the live conda env.

    Returns a summary dict for diagnostics:
        {patched_versions:    [{name, from, to}, ...],
         filled_homepages:    [name, ...],
         filled_install_methods: N}
    """
    packages = spec.get("packages", []) or []
    if not packages or not env_name:
        return {"patched_versions": [], "filled_homepages": [], "filled_install_methods": 0}

    conda_versions = env_manager.list_conda_packages(env_name)
    patched_versions: list[dict] = []
    filled_homepages: list[str] = []
    filled_install_methods = 0

    for p in packages:
        name = p.get("name", "")
        if not name:
            continue

        # 1. Default install_method to conda when unset (most common case)
        im = p.get("install_method")
        if not isinstance(im, dict) or not im.get("type"):
            p["install_method"] = {"type": "conda"}
            im = p["install_method"]
            filled_install_methods += 1

        im_type = im.get("type", "")

        # 2. Reconcile resolved_version against the source of truth for this
        #    install path. conda list for conda packages; packageVersion() for
        #    r_install packages — that's the number users see at library() time.
        before = p.get("resolved_version")
        actual = ""
        if im_type == "conda":
            actual = conda_versions.get(name, "")
        elif im_type == "r_install":
            actual = env_manager.r_package_version(env_name, name)

        if actual and actual != before:
            p["resolved_version"] = actual
            patched_versions.append({"name": name, "from": before, "to": actual})

        # 2b. For R packages installed as conda (r-*, bioconductor-*), the
        #     recipe version and packageVersion() differ. Capture both:
        #     resolved_version stays as the conda truth (for reproducibility),
        #     runtime_version exposes the R-side number users see at library() time.
        is_r_conda = im_type == "conda" and (
            name.startswith("r-") or name.startswith("bioconductor-")
        )
        if is_r_conda and not p.get("runtime_version"):
            r_pkg = name.split("-", 1)[1] if "-" in name else name
            rt = env_manager.r_package_version(env_name, r_pkg)
            if rt and rt != p.get("resolved_version"):
                p["runtime_version"] = rt

        # 3. Fill missing homepage from (channel, name, source) — deterministic
        if not p.get("homepage"):
            hp = _derive_homepage(name, p.get("channel", ""), im)
            if hp:
                p["homepage"] = hp
                filled_homepages.append(name)

    return {
        "patched_versions":       patched_versions,
        "filled_homepages":       filled_homepages,
        "filled_install_methods": filled_install_methods,
    }


def _derive_homepage(name: str, channel: str, install_method: dict) -> str:
    """Return a canonical upstream URL given (name, channel, install_method).

    Order of preference:
      1. install_method.source contains owner/repo (github install_github)
      2. channel-direct mappings (cran / bioconductor / pypi)
      3. Name-prefix conventions for conda packages (r-* → CRAN,
         bioconductor-* → Bioconductor)
    """
    ch = (channel or "").lower()
    im_type = (install_method or {}).get("type", "")
    source  = (install_method or {}).get("source", "") or ""

    if im_type == "r_install" and ch == "github":
        m = _R_INSTALL_GITHUB_REGEX.search(source)
        if m:
            return f"https://github.com/{m.group(1)}"

    if ch == "cran":
        return f"https://CRAN.R-project.org/package={name}"
    if ch == "bioconductor":
        return f"https://bioconductor.org/packages/{name}/"
    if ch in ("pypi", "pip"):
        return f"https://pypi.org/project/{name}/"

    if name.startswith("r-"):
        return f"https://CRAN.R-project.org/package={name[2:]}"
    if name.startswith("bioconductor-"):
        return f"https://bioconductor.org/packages/{name[len('bioconductor-'):]}/"

    return ""


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
