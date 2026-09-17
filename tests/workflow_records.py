"""Shared workflow-draft record fixtures — shapes constructed THROUGH the models.

Imported as `from workflow_records import ...` (not `tests.workflow_records`): `tests/`
has no `__init__.py`, so pytest puts it on sys.path directly, and an unrelated `tests`
package in site-packages would shadow the dotted form.

WHY THIS FILE EXISTS — the same story as `env_records.py`, one layer up. An untyped
dict makes the fixture the schema, and every test file writes its own: that is how four
mutually exclusive `shipped_binaries` dialects stayed green while the shape the
producer actually emitted was exercised by no test at all. These builders construct
through the `agent.models.core_data` models, so a fixture cannot encode a shape the
model refuses — and when a model is hardened (a seam flips its noun to ENFORCED), every
fixture built here hardens with it instead of silently pinning the old dialect.

Build VIOLATING records deliberately — a raw dict, written inline, with a comment
saying which invariant it exists to trip. Never build one by accident.
"""
from __future__ import annotations


def resource_usage(**overrides) -> dict:
    """An I7-satisfying resource_usage block — a real observation shape."""
    from agent.models.core_data import ResourceUsage
    fields = {"wall_seconds": 1.4, "peak_rss_mb": 64.0,
              "max_cpu_percent": 87.0, "sample_count": 4}
    fields.update(overrides)
    return ResourceUsage.model_validate(fields).model_dump()


def step_input(path: str = "/data/in/sample.fastq", **overrides) -> dict:
    from agent.models.core_data import StepInput
    fields = {"path": path, "references": []}
    fields.update(overrides)
    return StepInput.model_validate(fields).model_dump()


def pipeline_step(**overrides) -> dict:
    """One `pipeline_steps[]` entry that seals clean: rc=0, an absolute detected
    output with a passing validation record keyed the way `add_validation` keys it,
    and real resource_usage. Override any field to build the shape under test."""
    from agent.models.core_data import PipelineStep
    out = "/data/out/result.bam"
    fields = {
        "step": 1,
        "tool": "samtools",
        "command": f"samtools view -b /data/in/sample.sam -o {out}",
        "returncode": 0,
        "inputs": [step_input("/data/in/sample.sam")],
        "detected_outputs": [out],
        "validation": {out: {"passed": True, "validation_method": "tool"}},
        "resource_usage": resource_usage(),
    }
    fields.update(overrides)
    return PipelineStep.model_validate(fields).model_dump()


def install_step(**overrides) -> dict:
    """One `install_steps[]` entry — the conda tier's ordinary shape."""
    from agent.models.core_data import InstallStep
    fields = {
        "step": 1,
        "tool": "conda",
        "subcommand": "install",
        "command": "conda install -n bioinf_demo -c bioconda samtools=1.21",
        "installed_packages": [{"name": "samtools", "version": "1.21"}],
        "returncode": 0,
    }
    fields.update(overrides)
    return InstallStep.model_validate(fields).model_dump()


def package_record(name: str = "samtools", **overrides) -> dict:
    from agent.models.core_data import PackageRecord
    fields = {"name": name, "requested_version": "1.21", "resolved_version": "1.21"}
    fields.update(overrides)
    return PackageRecord.model_validate(fields).model_dump()
