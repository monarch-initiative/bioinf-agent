"""
PackageSearch — find the correct install info for a bioinformatics package.

Strategy (in order):
  1. Query anaconda.org REST API for the package in bioconda, then conda-forge
  2. Fall back to `conda search` subprocess if the API is unreachable
  3. Query PyPI if not found in conda
  4. Return structured info: channel, version, install spec, what the tool does
"""

import json
import subprocess
from typing import Any

import requests


CHANNEL_PRIORITY = ["bioconda", "conda-forge", "defaults"]
ANACONDA_API = "https://api.anaconda.org/package/{channel}/{package}"
PYPI_API = "https://pypi.org/pypi/{package}/json"


class PackageSearch:
    def __init__(self, config: dict):
        self.config = config

    def search(self, package_name: str, requested_version: str = "latest") -> dict[str, Any]:
        """
        Return install info for package_name.

        Result keys:
          found (bool), package_name, resolved_name, channel, version,
          conda_spec, install_command, description, input_types, output_types,
          check_command, notes
        """
        canonical = package_name.strip()

        # Try conda channels
        for channel in CHANNEL_PRIORITY:
            result = self._query_anaconda_api(canonical, channel, requested_version)
            if result["found"]:
                return result

        # Try conda search subprocess as fallback
        result = self._conda_search(canonical, requested_version)
        if result["found"]:
            return result

        # Try PyPI
        result = self._query_pypi(canonical, requested_version)
        if result["found"]:
            return result

        return {
            "found": False,
            "package_name": canonical,
            "error": (
                f"Could not find '{canonical}' in bioconda, conda-forge, defaults, or PyPI. "
                "Try an alternate spelling or check the tool's documentation for install instructions."
            ),
        }

    # -----------------------------------------------------------------------
    # Anaconda.org REST API
    # -----------------------------------------------------------------------

    def _query_anaconda_api(
        self, package_name: str, channel: str, requested_version: str
    ) -> dict:
        url = ANACONDA_API.format(channel=channel, package=package_name.lower())
        try:
            resp = requests.get(url, timeout=10)
        except requests.RequestException as e:
            return {"found": False, "error": str(e)}

        if resp.status_code != 200:
            return {"found": False}

        data = resp.json()
        versions = sorted(
            data.get("versions", []),
            key=lambda v: self._version_sort_key(v),
            reverse=True,
        )
        if not versions:
            return {"found": False}

        if requested_version == "latest":
            version = versions[0]
        else:
            # Find closest match
            version = next(
                (v for v in versions if v == requested_version or v.startswith(requested_version)),
                versions[0],
            )

        conda_spec = f"{package_name}={version}"
        install_cmd = f"conda install -c {channel} {conda_spec}"

        return {
            "found": True,
            "package_name": package_name,
            "resolved_name": data.get("name", package_name),
            "channel": channel,
            "version": version,
            "all_versions": versions[:10],
            "conda_spec": conda_spec,
            "install_command": install_cmd,
            "description": data.get("summary", ""),
            "home": data.get("home", ""),
            "license": data.get("license", ""),
            "input_types": self._infer_input_types(data.get("summary", ""), package_name),
            "output_types": self._infer_output_types(data.get("summary", ""), package_name),
            "check_command": self._infer_check_command(package_name),
            "install_via": "conda",
            "notes": "",
        }

    # -----------------------------------------------------------------------
    # conda search subprocess fallback
    # -----------------------------------------------------------------------

    def _conda_search(self, package_name: str, requested_version: str) -> dict:
        channels_args = " ".join(f"-c {c}" for c in CHANNEL_PRIORITY)
        version_suffix = f"={requested_version}" if requested_version != "latest" else ""
        cmd = f"conda search {channels_args} {package_name}{version_suffix} --json"

        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            data = json.loads(proc.stdout)
        except Exception:
            return {"found": False}

        if not data or package_name.lower() not in {k.lower() for k in data}:
            return {"found": False}

        key = next(k for k in data if k.lower() == package_name.lower())
        entries = sorted(data[key], key=lambda e: e.get("version", ""), reverse=True)
        if not entries:
            return {"found": False}

        best = entries[0]
        version = best.get("version", "unknown")
        channel = best.get("channel", "conda-forge").split("/")[-1]

        return {
            "found": True,
            "package_name": package_name,
            "resolved_name": key,
            "channel": channel,
            "version": version,
            "conda_spec": f"{package_name}={version}",
            "install_command": f"conda install -c {channel} {package_name}={version}",
            "description": "",
            "input_types": self._infer_input_types("", package_name),
            "output_types": self._infer_output_types("", package_name),
            "check_command": self._infer_check_command(package_name),
            "install_via": "conda",
            "notes": "Found via conda search fallback",
        }

    # -----------------------------------------------------------------------
    # PyPI fallback
    # -----------------------------------------------------------------------

    def _query_pypi(self, package_name: str, requested_version: str) -> dict:
        url = PYPI_API.format(package=package_name.lower())
        try:
            resp = requests.get(url, timeout=10)
        except requests.RequestException:
            return {"found": False}

        if resp.status_code != 200:
            return {"found": False}

        data = resp.json()
        info = data.get("info", {})

        if requested_version == "latest":
            version = info.get("version", "unknown")
        else:
            releases = list(data.get("releases", {}).keys())
            version = next(
                (v for v in releases if v == requested_version or v.startswith(requested_version)),
                info.get("version", "unknown"),
            )

        return {
            "found": True,
            "package_name": package_name,
            "resolved_name": info.get("name", package_name),
            "channel": "pypi",
            "version": version,
            "conda_spec": None,
            "install_command": f"pip install {package_name}=={version}",
            "description": info.get("summary", ""),
            "home": info.get("home_page", ""),
            "license": info.get("license", ""),
            "input_types": self._infer_input_types(info.get("summary", ""), package_name),
            "output_types": self._infer_output_types(info.get("summary", ""), package_name),
            "check_command": self._infer_check_command(package_name),
            "install_via": "pip",
            "notes": "Not found in conda — using pip. Consider wrapping in conda env.",
        }

    # -----------------------------------------------------------------------
    # Heuristics for input/output types and check commands
    # (Claude uses these as hints; it will reason further from the description)
    # -----------------------------------------------------------------------

    _INPUT_HINTS = {
        "fastq": ["bwa", "star", "hisat2", "bowtie", "minimap2", "salmon", "kallisto",
                  "trim_galore", "trimmomatic", "fastp", "cutadapt", "fastqc"],
        "bam_sam": ["samtools", "picard", "gatk", "featurecounts", "htseq", "deeptools",
                    "macs2", "macs3", "homer", "bamtools", "subread"],
        "vcf": ["bcftools", "vep", "snpeff", "annovar", "plink", "beagle", "shapeit"],
        "fasta": ["spades", "velvet", "prokka", "augustus", "repeatmasker", "blast",
                  "diamond", "makeblastdb", "bwa", "minimap2"],
        "bed": ["bedtools", "homer", "macs2", "deeptools", "ucsc"],
        "counts_matrix": ["deseq2", "edger", "limma", "seurat"],
    }

    _OUTPUT_HINTS = {
        "bam": ["bwa", "star", "hisat2", "bowtie", "minimap2", "samtools sort"],
        "vcf": ["gatk", "bcftools", "deepvariant", "strelka", "mutect2", "freebayes", "clair3", "medaka"],
        "counts_matrix": ["featurecounts", "htseq", "salmon", "kallisto", "stringtie"],
        "bed": ["macs2", "macs3", "homer", "bedtools"],
        "fasta": ["spades", "velvet", "trinity", "flye"],
        "bigwig": ["deeptools", "bamcoverage", "bamcompare"],
    }

    _CHECK_COMMANDS = {
        "bwa": "bwa 2>&1 | head -3",
        "star": "STAR --version",
        "hisat2": "hisat2 --version | head -1",
        "samtools": "samtools --version | head -1",
        "gatk": "gatk --version",
        "bcftools": "bcftools --version | head -1",
        "minimap2": "minimap2 --version",
        "salmon": "salmon --version",
        "kallisto": "kallisto version",
        "featurecounts": "featureCounts -v 2>&1 | head -2",
        "deeptools": "deeptools --version",
        "macs2": "macs2 --version",
        "macs3": "macs3 --version",
        "spades": "spades.py --version",
        "fastp": "fastp --version 2>&1 | head -1",
        "trimmomatic": "trimmomatic -version",
        "fastqc": "fastqc --version",
        "picard": "picard --version 2>&1 | head -1",
    }

    def _infer_input_types(self, description: str, package_name: str) -> list[str]:
        name_lower = package_name.lower()
        found = []
        for itype, tools in self._INPUT_HINTS.items():
            if any(t in name_lower for t in tools):
                found.append(itype)
        return found or ["unknown"]

    def _infer_output_types(self, description: str, package_name: str) -> list[str]:
        name_lower = package_name.lower()
        found = []
        for otype, tools in self._OUTPUT_HINTS.items():
            if any(t in name_lower for t in tools):
                found.append(otype)
        return found or ["unknown"]

    def _infer_check_command(self, package_name: str) -> str:
        return self._CHECK_COMMANDS.get(
            package_name.lower(), f"{package_name.lower()} --version 2>&1 | head -3"
        )

    @staticmethod
    def _version_sort_key(version_str: str):
        parts = []
        for part in version_str.replace("-", ".").split("."):
            try:
                parts.append((0, int(part)))
            except ValueError:
                parts.append((1, part))
        return parts
