"""
pipeline_record — the typed PIPELINE record, derived from a sealed WorkflowSpec.

A pipeline is a RENDER of a sealed workflow, and it invents nothing: the stages are
the sealed how-to's commands, the images are the sealed steps' observed digests, the
samplesheet columns are the how-to's per-sample inputs, and every artifact a stage
writes into the row's directory is published, as the sealed run left it. This module
derives that record — the ONE thing the Nextflow renderer and the explain page read —
and refuses, naming the remedy, when the seal cannot support the render.

Vocabulary
----------
  template   one entry of `usage.command_template` (placeholders intact)
  row        one I4 trial: a substitution map = one samplesheet row
  stage      one or more consecutive templates that run as ONE job (default: one
             template per stage — the user iterates on one step at a time)
  artifact   a file the how-to names inside an output slot, `{OUTPUT_DIR}/<name>`;
             produced by the earliest template the sealed run OBSERVED writing it,
             consumed by every later template that names it
  samplesheet  always: `sample` (the row key) first, then one column per per-sample
             input, one row per trial the seal proved — the worked example a user
             replaces with their own samples. A one-trial seal is a one-row sheet
  thread slot  a how-to input declared `format: threads`: the count a tool's thread
             flag takes. It binds to the stage's CPU request (`task.cpus`), never to
             params.yaml, and the sealed run's count IS that request unless the
             caller sizes the stage — so the command and the request cannot disagree
  samplesheet slot  a how-to input declared `format: samplesheet`: the pipeline's own
             samples.csv, handed to the stage as a file. The seal's trial file shows
             which columns the stage reads; columns the per-sample inputs do not
             already give are added to the sheet, their example values taken from
             that file, matched on `sample`
  cohort stage  a stage that runs ONCE, over every sample, after the per-sample
             stage it collects from has finished for every row. It comes from a
             SECOND sealed workflow attached to the first (`cohort=`), and its
             fan-in is DECLARED at render — `collect={PLACEHOLDER: artifact}` names
             which per-sample artifact the cohort how-to's input receives, every
             row's copy staged into one directory — never inferred
  script     a shared path input whose sealed value is an authored artifact of the
             workflow (`stage_authored_artifact`): the record carries its text, the
             render puts it in `bin/` beside the files, and params.yaml points there
"""
from __future__ import annotations

import fnmatch
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

import yaml
from pydantic import BaseModel, ConfigDict

from agent.models import core_data
from agent.skills.spec_writer import _SIDECAR_SUFFIXES, _is_output_slot

#: The I6 reading of a placeholder — `{NAME}`, upper-case, as the seal scans it.
_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
#: Characters that can appear in a filename token named inside an output slot.
_TOKEN_CHARS = r"[^\s'\"|;&<>()]+"
#: Formats whose inputs are per-sample by nature when only one trial exists.
_READ_FORMATS = frozenset({"fastq", "fq", "fastq.gz", "fq.gz", "bam", "ubam", "cram",
                           "pod5", "fast5", "sam"})
_SAMPLE_NAMES = frozenset({"SAMPLE", "SAMPLE_ID", "SAMPLE_NAME", "ID"})
#: A how-to input declared with this format is a THREAD SLOT (see the vocabulary).
THREADS_FORMAT = "threads"
#: A how-to input declared with this format is a SAMPLESHEET SLOT (see the vocabulary).
SAMPLESHEET_FORMAT = "samplesheet"
#: Where an authored script lands in the rendered directory (Nextflow's own convention).
SCRIPT_DIR = "bin"
#: Interpreters whose first argument is the script that names the stage.
_INTERPRETERS = frozenset({"Rscript", "python", "python3", "bash", "sh", "perl", "julia"})
_COUNT_RE = re.compile(r"^[1-9][0-9]*$")

#: The request a stage gets when the caller sized nothing. A job script must carry
#: SOME request; this one is labelled as unsized wherever it is rendered, and the
#: record's `requested_by: default` is what the page reads to say so. ONE value, read
#: by both form renderers.
DEFAULT_STAGE_REQUEST: dict[str, Any] = {"time": "4:00:00", "mem": "8G", "cpus": 1}
#: The manager job (Nextflow form): tiny, long-lived, never does the work.
MANAGER_JOB_REQUEST: dict[str, Any] = {"time": "2-00:00:00", "mem": "4G", "cpus": 1}
#: Nextflow executor defaults the record's `defaults` table states.
NEXTFLOW_QUEUE_SIZE = 50

ScopeT = Literal["per_sample", "cohort"]


class PipelineDerivationError(ValueError):
    """A refusal: the seal cannot support this render. `code` is the outcome tag the
    MCP wrapper emits; `remedy` names what to change."""

    def __init__(self, code: str, error: str, remedy: str = ""):
        super().__init__(error)
        self.code, self.error, self.remedy = code, error, remedy


# ── the record ──────────────────────────────────────────────────────────────


class PipelineParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str                                  # placeholder, e.g. STRANDED
    kind: Literal["shared", "per_sample", "cpus", "samplesheet", "collected"]
    #   cpus: a thread slot, bound to the stage's CPU request · samplesheet: the pipeline's own
    #   samples.csv · collected: every row's copy of a per-sample artifact (a cohort stage's fan-in)
    value_kind: Literal["path", "prefix", "value"]   # prefix: names a FAMILY of files (an aligner index)
    default: Optional[str]                     # the sealed trial's value for a shared param; None for a per-sample one, whose values are the samplesheet's rows
    source: str                                # usage_input | literal | test_data:<key> | reference_database:<name> | sealed_step:<n> | authored_artifact:<name> | collect:<artifact>
    format: Optional[str]
    description: Optional[str]
    used_by: list[str]                         # stage names
    reason: str                                # why it was classified as it was


class StageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str                                  # a placeholder, or an artifact name
    origin: Literal["param", "column", "stage", "request", "samplesheet", "collect"]
    #   request: the stage's own cpus (a thread slot) · samplesheet: the pipeline's samples.csv ·
    #   collect: every row's copy of a per-sample artifact, from `from_stage`
    from_stage: Optional[str]
    artifact: Optional[str]                    # origin=stage|collect: the artifact (templated basename or glob)


class StageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: str                              # templated basename, or a glob when rows disagree
    observed: Optional[str]                    # basename observed in the sealed run (row 0), None if never observed
    consumed_by: list[str]
    declared_pattern: Optional[str]            # the usage.outputs glob that matched, if any


class StageResources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpus: Optional[int]
    mem: Optional[str]
    time: Optional[str]
    gpus: int
    requested_by: Literal["caller", "seal", "default"]   # seal: cpus is the how-to's thread count, the rest default
    threads_slot: Optional[str]                # the thread slot the stage's command binds to task.cpus, else None
    measured_wall_seconds: Optional[float]
    measured_peak_rss_mb: Optional[float]
    measured_max_cpu_percent: Optional[float]
    measured_authority: str                    # authoritative | not_authoritative | unrecorded | mixed | none
    measured_on: Optional[str]


class PipelineStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    index: int
    scope: ScopeT
    templates: list[int]                       # 0-based indices into the how-to command list
    commands: list[str]                        # the templates, placeholders intact
    tool: str
    sealed_steps: list[int]                    # sealed step numbers backing this stage, every row
    image: Optional[str]
    image_digest: Optional[str]
    request_key: Optional[str]
    env_name: Optional[str]                    # the frozen env's name (EnvCache), what the .sif is named after
    sif_sha256: Optional[str]
    sif_path: Optional[str]                    # where the .sif lives on the cluster the caller named, else None
    inputs: list[StageInput]
    outputs: list[StageOutput]
    consumes_workdir: bool                     # a template names an output slot bare — the whole row workdir
    stage_in_copy: bool                        # the stage rewrites an artifact it consumed
    resources: StageResources


class SamplesheetColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str                                  # column header (placeholder, lower-cased)
    placeholder: str                           # the per-sample input it binds, or the column name for a sheet-slot column
    value_kind: Literal["path", "prefix", "value"]
    format: Optional[str]
    description: Optional[str]
    read_by: list[str]                         # stages that read the column THROUGH the samplesheet slot; [] for a bound input


class Samplesheet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: list[SamplesheetColumn]           # `sample` first, always
    rows: list[dict[str, str]]                 # the seal's trials — the worked example


class PipelineDefault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: str
    source: Literal["default", "caller", "seal"]


class ProvenanceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int
    workflow: str                              # the sealed workflow the step belongs to
    tool: str
    command: str
    produces_param: str


class PipelineScript(BaseModel):
    """An authored script the how-to runs, carried verbatim: the render writes it to
    `bin/<name>` and the param that names it defaults to that path."""
    model_config = ConfigDict(extra="forbid")
    param: str                                 # the placeholder bound to it
    name: str                                  # its basename, the file under bin/
    sha256: str                                # as the seal anchored it
    content: str
    sealed_path: str                           # where the sealed run read it


class CollectedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    placeholder: str                           # the cohort how-to's input
    artifact: str                              # the per-sample artifact (templated basename), every row's copy
    from_stage: str                            # the per-sample stage that writes it
    stage: str                                 # the cohort stage that receives them


class CohortWorkflow(BaseModel):
    """A second sealed workflow attached as the pipeline's cohort stages."""
    model_config = ConfigDict(extra="forbid")
    sealed_workflow: str
    sealed_workflow_path: str
    sealed_workflow_sha256: Optional[str]
    stages: list[str]
    collect: list[CollectedInput]


class LocalRuntime(BaseModel):
    """How nextflow is made available on the machine that rendered the pipeline: the
    checkout's own runtime env, entered by sourcing one script."""
    model_config = ConfigDict(extra="forbid")
    activate: str                              # `source` this: the runtime env's nextflow and its Java on PATH
    nextflow: Optional[str]                    # the nextflow binary it puts on PATH; None when absent at render time


class PipelineRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    created_at: str
    sealed_workflow: str
    sealed_workflow_path: str
    sealed_workflow_sha256: Optional[str]
    env_digests: list[str]
    params: list[PipelineParam]
    samplesheet: Samplesheet                   # always; `sample` first, the seal's trials as the example rows
    output_slots: list[str]
    compute_env: Optional[str]                 # the compute env the cluster files were rendered for, else None
    modules: list[str]                         # Lmod modules that env loads before apptainer/nextflow run
    local_runtime: Optional[LocalRuntime]      # how this machine provides nextflow, else None
    stages: list[PipelineStage]
    provenance_steps: list[ProvenanceStep]
    unmatched_steps: list[int]
    scripts: list[PipelineScript]              # authored scripts the how-to runs, rendered into bin/
    cohort_workflows: list[CohortWorkflow]     # the sealed workflows attached as cohort stages, with their fan-in
    defaults: list[PipelineDefault]
    notes: list[str]

    def stage(self, name: str) -> PipelineStage:
        for s in self.stages:
            if s.name == name:
                return s
        raise KeyError(name)

    def param(self, name: str) -> PipelineParam:
        for p in self.params:
            if p.name == name:
                return p
        raise KeyError(name)


# ── small readers ───────────────────────────────────────────────────────────


def _ws(s: str) -> str:
    return " ".join(str(s).split())


def _collapse(piece: str) -> str:
    """Collapse runs of whitespace but KEEP a boundary space — the pieces of a
    template around a placeholder end or begin with the space that separates it."""
    return re.sub(r"\s+", " ", piece)


def placeholders(text: str) -> list[str]:
    """Every `{NAME}` in `text`, first-occurrence order, no duplicates."""
    seen: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def artifact_tokens(template: str, out_slots: set[str]) -> list[str]:
    """The `<name>` fragments the template names inside an output slot,
    `{OUTPUT_DIR}/<name>`, first-occurrence order."""
    toks: list[str] = []
    for slot in sorted(out_slots):
        for m in re.finditer(r"\{" + slot + r"\}/(" + _TOKEN_CHARS + ")", template):
            if m.group(1) not in toks:
                toks.append(m.group(1))
    return toks


def _names_bare_slot(template: str, out_slots: set[str]) -> bool:
    return any(re.search(r"\{" + slot + r"\}(?!/)", template) for slot in out_slots)


def _template_regex(template: str, subs: Mapping[str, str], out_slots: set[str]) -> re.Pattern:
    """A pattern that matches the literal command the how-to produces for one row:
    input placeholders bound to the row's values, output slots left as wildcards
    (the seal's steps and the I4 self-test wrote to different output dirs)."""
    parts: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(template):
        parts.append(re.escape(_collapse(template[pos:m.start()])))
        name = m.group(1)
        if name in out_slots:
            parts.append(r"\S+")
        else:
            if name not in subs:
                raise KeyError(name)
            parts.append(re.escape(_ws(subs[name])))
        pos = m.end()
    parts.append(re.escape(_collapse(template[pos:])))
    return re.compile("".join(parts))


def _step_dicts(spec: Any) -> list[dict]:
    steps = getattr(spec, "pipeline_steps", None) or []
    out = []
    for s in steps:
        d = s.model_dump(exclude_none=True) if hasattr(s, "model_dump") else dict(s)
        out.append(d)
    return out


def _spec_dict(spec: Any) -> dict:
    return spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)


def _sanitize_name(tool: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", tool).strip("_").upper()
    if not s or not re.match(r"[A-Z_]", s):
        s = f"STAGE_{s}" if s else "STAGE"
    return s


def _templatize(basename: str, row_subs: Mapping[str, str], value_params: list[str]) -> str:
    """Put per-sample VALUE placeholders back into an observed basename
    (`SRR1039508.counts.tsv` → `{SAMPLE}.counts.tsv`). Path-valued placeholders
    are never substituted — a read filename inside an output name is not
    recoverable without guessing."""
    out = basename
    for ph in sorted(value_params, key=lambda p: -len(row_subs.get(p, ""))):
        v = row_subs.get(ph)
        if v and v in out:
            out = out.replace(v, "{" + ph + "}")
    return out


def _common_suffix_glob(names: list[str]) -> str:
    if not names:
        return "*"
    suffix = names[0]
    for n in names[1:]:
        i = 0
        while i < min(len(suffix), len(n)) and suffix[-1 - i] == n[-1 - i]:
            i += 1
        suffix = suffix[len(suffix) - i:] if i else ""
    return "*" + suffix if suffix else "*"


def _sidecar_of(name: str, produced: Mapping[str, int]) -> Optional[str]:
    """`x.bam.bai` is a sidecar of `x.bam` when `x.bam` is a known artifact."""
    low = name.lower()
    for suf in _SIDECAR_SUFFIXES:
        if low.endswith(suf) and name[: -len(suf)] in produced:
            return name[: -len(suf)]
    return None


# ── derivation ──────────────────────────────────────────────────────────────


def _thread_slot(ph: str, values: list[str], declared: Mapping, caller_named: set[str]) -> PipelineParam:
    """A placeholder declared `format: threads`, as a `cpus` param: every trial must
    bind one and the same positive whole number — the stage's CPU request needs ONE
    count, and a count is what a thread flag takes. Refuses, naming the remedy, when
    the caller tried to make it a column or a params.yaml value instead."""
    if ph in caller_named:
        raise PipelineDerivationError(
            "pipeline.threads_kind",
            f"{ph} is declared `format: {THREADS_FORMAT}`, which binds it to the stage's CPU request; "
            f"it cannot be a samplesheet column or a params.yaml value",
            "drop it from per_sample= / shared=, or change the how-to input's format")
    bad = sorted({v for v in values if not _COUNT_RE.match(v)})
    if bad:
        raise PipelineDerivationError(
            "pipeline.threads_not_a_count",
            f"{ph} is declared `format: {THREADS_FORMAT}` but the trials bind {bad}, not a positive whole "
            f"number of threads",
            "bind a positive whole number in every trial, or change the how-to input's format")
    if len(set(values)) > 1:
        raise PipelineDerivationError(
            "pipeline.threads_vary",
            f"{ph} is declared `format: {THREADS_FORMAT}` but the trials bind different counts "
            f"{sorted(set(values))}; the stage's CPU request needs one",
            "bind the same thread count in every trial")
    return PipelineParam(
        name=ph, kind="cpus", value_kind="value", default=values[0], source="literal",
        format=declared.get("format"), description=declared.get("description"), used_by=[],
        reason=f"declared format {THREADS_FORMAT!r}: the stage's CPU request, {values[0]} in the sealed run")


def _sheet_slot(ph: str, values: list[str], declared: Mapping, caller_named: set[str]) -> PipelineParam:
    """A placeholder declared `format: samplesheet`: the pipeline's own samples.csv,
    handed to the stage as a file. Every trial binds the sheet it was proven with;
    the render binds `params.samplesheet` instead. Refuses a caller who tried to make
    it a column or a params.yaml value."""
    if ph in caller_named:
        raise PipelineDerivationError(
            "pipeline.samplesheet_kind",
            f"{ph} is declared `format: {SAMPLESHEET_FORMAT}`, which binds it to the pipeline's own "
            f"samplesheet; it cannot be a samplesheet column or a params.yaml value",
            "drop it from per_sample= / shared=, or change the how-to input's format")
    return PipelineParam(
        name=ph, kind="samplesheet", value_kind="path", default=values[0], source="usage_input",
        format=declared.get("format"), description=declared.get("description"), used_by=[],
        reason=f"declared format {SAMPLESHEET_FORMAT!r}: the pipeline's samples.csv; the sealed run read {values[0]}")


def _parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text))
    cols = [c.strip() for c in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]
    return cols, rows


def _extend_sheet(sheet: Samplesheet, ph: str, csv_text: str, stage_names: list[str],
                  notes: list[str]) -> None:
    """Add to the pipeline's samplesheet the columns a samplesheet-slot input reads
    that the per-sample inputs do not already give — their example values from the
    slot's trial file, matched on `sample`. The trial file must carry a `sample`
    column: it is the row key the stage joins on."""
    cols, rows = _parse_csv(csv_text)
    if "sample" not in cols:
        raise PipelineDerivationError(
            "pipeline.samplesheet_slot_no_key",
            f"the samplesheet the sealed run bound to {ph} has columns {cols} and no `sample` column, "
            f"the row key every stage joins on",
            "give the trial's samplesheet a `sample` column naming each row, and re-seal")
    by_sample = {r.get("sample", ""): r for r in rows}
    existing = {c.name for c in sheet.columns}
    for c in cols:
        if c == "sample":
            continue
        if c in existing:
            col = next(x for x in sheet.columns if x.name == c)
            col.read_by = sorted(set(col.read_by) | set(stage_names))
            continue
        sheet.columns.append(SamplesheetColumn(
            name=c, placeholder=c, value_kind="value", format=None,
            description=None, read_by=list(stage_names)))
        for row in sheet.rows:
            row[c] = by_sample.get(row.get("sample", ""), {}).get(c, "")
        notes.append(f"samples.csv column `{c}`: read by {', '.join(stage_names)} through {ph}; "
                     f"example values from the sealed run's own samplesheet")
    unmatched = [s for s in (r.get("sample") for r in sheet.rows) if s not in by_sample]
    if unmatched and len(cols) > 1:
        notes.append(f"the sealed run's samplesheet for {ph} has no row for {unmatched}; their example "
                     f"cells are empty")


class CohortRequest(BaseModel):
    """A second sealed workflow to attach as cohort stages: `spec` (a WorkflowSpec),
    where it was read from, and `collect` — which per-sample artifact each of its
    fan-in inputs receives, declared, never inferred."""
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    spec: Any
    spec_path: str = ""
    spec_sha256: Optional[str] = None
    collect: dict[str, str]
    stages: Optional[list[list[int]]] = None
    stage_names: Optional[list[str]] = None
    shared: Optional[list[str]] = None


def derive_pipeline_record(spec: Any, *, name: str, spec_path: str = "",
                           spec_sha256: Optional[str] = None,
                           stages: Optional[list[list[int]]] = None,
                           stage_names: Optional[list[str]] = None,
                           per_sample: Optional[list[str]] = None,
                           shared: Optional[list[str]] = None,
                           resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
                           env_names: Optional[Mapping[str, str]] = None,
                           sif_paths: Optional[Mapping[str, str]] = None,
                           compute_env: Optional[str] = None,
                           modules: Optional[list[str]] = None,
                           local_runtime: Optional[LocalRuntime] = None,
                           samplesheet_files: Optional[Mapping[str, str]] = None,
                           cohort: Optional[list[CohortRequest]] = None,
                           _collected: Optional[Mapping[str, str]] = None) -> PipelineRecord:
    """Derive the pipeline record from a sealed WorkflowSpec. Raises
    PipelineDerivationError (a refusal with a remedy) when the seal cannot support
    the render; every derivation the caller did not dictate is stated in `notes`.

    `samplesheet_files` is `{path: csv text}` for every samplesheet the sealed trials
    bound to a `format: samplesheet` input — the caller reads them, this stays pure.
    `cohort` attaches further sealed workflows as cohort stages (see the vocabulary).
    `_collected` is the attachment's own: the cohort how-to's fan-in placeholders."""
    notes: list[str] = []
    collected_set = dict(_collected or {})
    usage = getattr(spec, "usage", None)
    if usage is None:
        raise PipelineDerivationError(
            "pipeline.no_howto",
            f"sealed workflow {spec.workflow_name!r} carries no `usage` how-to",
            "re-seal the workflow with a usage.command_template (one command per step)")
    usage_d = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    templates = core_data.usage_commands(usage_d)
    if not templates:
        raise PipelineDerivationError(
            "pipeline.no_howto", "the sealed how-to has no commands",
            "re-seal with a non-empty usage.command_template")

    # ── rows: the I4 trials ─────────────────────────────────────────────────
    spec_d = _spec_dict(spec)
    rows = [t for t in core_data.usage_proven_trials(spec_d) if isinstance(t, dict)]
    row_source = "proven"
    if not rows:
        rows = [{"name": t.get("name", f"trial_{i + 1}"), "substitutions": dict(t.get("substitutions") or {})}
                for i, t in enumerate(usage_d.get("trials") or []) if isinstance(t, dict)]
        row_source = "declared"
    if not rows:
        raise PipelineDerivationError(
            "pipeline.no_trials",
            "the sealed how-to has no trials to derive rows from",
            "declare usage.trials (one per sample) and re-seal")
    notes.append(f"rows: {len(rows)} ({row_source} trial{'s' if len(rows) != 1 else ''})")

    all_placeholders: list[str] = []
    for t in templates:
        for p in placeholders(t):
            if p not in all_placeholders:
                all_placeholders.append(p)
    out_slots = {p for p in all_placeholders if _is_output_slot(p)}
    in_placeholders = [p for p in all_placeholders if p not in out_slots]
    declared_inputs = {i.get("name"): i for i in (usage_d.get("inputs") or []) if isinstance(i, dict)}
    declared_outputs = [o for o in (usage_d.get("outputs") or []) if isinstance(o, dict)]

    for r in rows:
        missing = [p for p in in_placeholders if p not in (r.get("substitutions") or {})]
        if missing:
            raise PipelineDerivationError(
                "pipeline.trial_missing_placeholder",
                f"trial {r.get('name')!r} binds no value for {missing}",
                "every trial must bind every non-output placeholder the how-to uses")

    # ── params: shared vs per-sample ───────────────────────────────────────
    per_sample_set = set(per_sample or [])
    shared_set = set(shared or [])
    unknown = (per_sample_set | shared_set) - set(in_placeholders)
    if unknown:
        raise PipelineDerivationError(
            "pipeline.unknown_placeholder",
            f"{sorted(unknown)} are not placeholders of the how-to ({in_placeholders})",
            "name a placeholder the how-to uses")
    test_paths = set(core_data.test_data_paths(spec_d.get("test_data") or {}).values())
    ref_dbs = [rd for rd in (spec_d.get("reference_databases") or []) if isinstance(rd, dict)]
    step_ds = _step_dicts(spec)
    ok_steps = [s for s in step_ds if s.get("returncode") == 0]

    def classify(ph: str) -> tuple[str, str]:
        values = [str((r.get("substitutions") or {})[ph]) for r in rows]
        if ph in per_sample_set:
            return "per_sample", "set by caller"
        if ph in shared_set:
            return "shared", "set by caller"
        if len(rows) > 1:
            if len(set(values)) > 1:
                return "per_sample", f"differs across the {len(rows)} trials"
            return "shared", f"identical across the {len(rows)} trials"
        v = values[0]
        fmt = str((declared_inputs.get(ph) or {}).get("format") or "").lower()
        if ph in _SAMPLE_NAMES:
            return "per_sample", "a sample identifier"
        if fmt in _READ_FORMATS:
            return "per_sample", f"declared format {fmt!r} is per-sample data"
        if v in test_paths:
            key = next(k for k, p in core_data.test_data_paths(spec_d.get("test_data") or {}).items() if p == v)
            if key in ("r1", "r2") or key.startswith("r1_") or key.startswith("r2_"):
                return "per_sample", f"bound to test_data.{key} (reads)"
        return "shared", "one trial, not a reads input"

    params: list[PipelineParam] = []
    for ph in in_placeholders:
        values = [str((r.get("substitutions") or {})[ph]) for r in rows]
        v0 = values[0]
        fmt_declared = str((declared_inputs.get(ph) or {}).get("format") or "").lower()
        if fmt_declared == THREADS_FORMAT:
            params.append(_thread_slot(ph, values, declared_inputs.get(ph) or {}, per_sample_set | shared_set))
            notes.append(f"{ph}: cpus ({params[-1].reason})")
            continue
        if fmt_declared == SAMPLESHEET_FORMAT:
            params.append(_sheet_slot(ph, values, declared_inputs.get(ph) or {}, per_sample_set | shared_set))
            notes.append(f"{ph}: samplesheet ({params[-1].reason})")
            continue
        if ph in collected_set:
            if ph in per_sample_set | shared_set:
                raise PipelineDerivationError(
                    "pipeline.collect_kind",
                    f"{ph} is a collected input (collect=), every sample's {collected_set[ph]}; it cannot "
                    f"be a samplesheet column or a params.yaml value",
                    "drop it from per_sample= / shared=, or from collect=")
            di = declared_inputs.get(ph) or {}
            params.append(PipelineParam(
                name=ph, kind="collected", value_kind="path", default=None, source=f"collect:{collected_set[ph]}",
                format=di.get("format"), description=di.get("description"), used_by=[],
                reason=f"declared collect=: every sample's {collected_set[ph]}, staged into one directory; "
                       f"the sealed run read {v0}"))
            notes.append(f"{ph}: collected ({params[-1].reason})")
            continue
        kind, why = classify(ph)
        value_kind = "path" if core_data.is_path_like(v0) else "value"
        source = "usage_input" if ph in declared_inputs else "literal"
        if value_kind == "value":
            source = "literal"
        if kind == "shared" and value_kind == "path":
            for rd in ref_dbs:
                lp = str(rd.get("local_path") or "")
                if lp and (v0 == lp or v0.startswith(lp.rstrip("/") + "/")):
                    source = f"reference_database:{rd.get('name')}"
                    break
            else:
                for key, p in core_data.test_data_paths(spec_d.get("test_data") or {}).items():
                    if v0 == p:
                        source = f"test_data:{key}"
                        break
        di = declared_inputs.get(ph) or {}
        params.append(PipelineParam(
            name=ph, kind=kind, value_kind=value_kind,
            default=v0 if kind == "shared" else None,     # a per-sample value is a samplesheet cell
            source=source, format=di.get("format"), description=di.get("description"),
            used_by=[], reason=why))
        notes.append(f"{ph}: {kind} ({why})")
    # a shared path that is an authored artifact of the workflow is a SCRIPT: carried
    # verbatim, rendered into bin/, its param defaulting to that relative path
    scripts: list[PipelineScript] = []
    authored = [a for a in (spec_d.get("authored_artifacts") or []) if isinstance(a, dict)]
    for p in params:
        if p.kind != "shared" or p.value_kind != "path" or not p.default:
            continue
        art = next((a for a in authored if str(a.get("path") or "") == p.default), None)
        if art is None:
            continue
        content = art.get("content_excerpt")
        if not isinstance(content, str) or int(art.get("size_bytes") or 0) != len(content.encode("utf-8")):
            raise PipelineDerivationError(
                "pipeline.script_not_carried",
                f"{p.name} names the authored artifact {p.default}, but the sealed record does not carry "
                f"its full text (size {art.get('size_bytes')} bytes)",
                "stage scripts under 64 KiB in content mode so the seal carries them verbatim")
        bn = Path(p.default).name
        if any(sc.name == bn for sc in scripts):
            raise PipelineDerivationError(
                "pipeline.script_name_clash",
                f"two authored scripts share the basename {bn!r}; bin/ holds one file per name",
                "rename one script and re-seal")
        scripts.append(PipelineScript(param=p.name, name=bn, sha256=str(art.get("sha256") or ""),
                                      content=content, sealed_path=p.default))
        p.source = f"authored_artifact:{bn}"
        p.default = f"{SCRIPT_DIR}/{bn}"
        p.reason += f"; an authored script, rendered into {SCRIPT_DIR}/"
        notes.append(f"{p.name}: an authored script ({bn}), carried into {SCRIPT_DIR}/{bn}")
    per_sample_names = [p.name for p in params if p.kind == "per_sample"]
    value_per_sample = [p.name for p in params if p.kind == "per_sample" and p.value_kind == "value"]
    cpus_of = {p.name: int(p.default or 0) for p in params if p.kind == "cpus"}

    # ── match sealed steps to (template, row) ──────────────────────────────
    matched: dict[int, list[tuple[int, dict]]] = {i: [] for i in range(len(templates))}
    claimed: set[int] = set()
    for ti, tmpl in enumerate(templates):
        for ri, r in enumerate(rows):
            pat = _template_regex(tmpl, r.get("substitutions") or {}, out_slots)
            for s in ok_steps:
                n = int(s.get("step", 0))
                if n in claimed:
                    continue
                if pat.fullmatch(_ws(s.get("command", ""))):
                    matched[ti].append((ri, s))
                    claimed.add(n)
                    break
    unmatched = [s for s in ok_steps if int(s.get("step", 0)) not in claimed]

    # provenance: unmatched steps that produced a shared param's value
    provenance: list[ProvenanceStep] = []
    prov_steps: set[int] = set()
    for s in unmatched:
        outs = [str(o) for o in (s.get("detected_outputs") or [])]
        for p in params:
            if p.kind != "shared" or p.value_kind != "path" or not p.default:
                continue
            v = p.default
            exact = any(o == v or str(Path(o).parent) == v.rstrip("/") for o in outs)
            prefix = (not exact) and any(
                Path(o).parent == Path(v).parent and Path(o).name.startswith(Path(v).name) and o != v
                for o in outs)
            hit = exact or prefix
            if hit:
                if prefix:
                    p.value_kind = "prefix"
                    notes.append(f"{p.name}: a prefix — sealed step {int(s['step'])} wrote a family "
                                 f"of files named after it")
                provenance.append(ProvenanceStep(step=int(s["step"]), workflow=str(spec.workflow_name),
                                                 tool=str(s.get("tool") or ""),
                                                 command=str(s.get("command") or ""),
                                                 produces_param=p.name))
                p.source = f"sealed_step:{int(s['step'])}"
                prov_steps.add(int(s["step"]))
                break
    unmatched_steps = sorted(int(s["step"]) for s in unmatched if int(s["step"]) not in prov_steps)
    if unmatched_steps:
        notes.append(f"sealed steps {unmatched_steps} match no how-to command and produce no "
                     f"how-to input; they are not part of the pipeline")

    # ── artifacts: producers, consumers, observed names ────────────────────
    env_map: dict[str, dict] = {}
    for e in (spec_d.get("envs") or []):
        if isinstance(e, dict) and e.get("image_digest"):
            env_map[str(e["image_digest"])] = e

    produced_by: dict[str, int] = {}           # artifact token -> template index
    observed: dict[tuple[int, str], list[Optional[str]]] = {}   # (template, token) -> per-row observed basename
    template_tokens: list[list[str]] = []
    template_bare: list[bool] = []
    consumers: dict[str, list[int]] = {}
    rewrites: dict[int, list[str]] = {i: [] for i in range(len(templates))}
    template_outputs: list[list[str]] = []   # named tokens + observed-but-unnamed artifacts
    for ti, tmpl in enumerate(templates):
        toks = artifact_tokens(tmpl, out_slots)
        template_tokens.append(toks)
        template_bare.append(_names_bare_slot(tmpl, out_slots))
        template_outputs.append(list(toks))
        steps_here = matched[ti]
        for tok in toks:
            # observed basenames for this token across rows
            per_row: list[Optional[str]] = []
            for ri, s in steps_here:
                subs = rows[ri].get("substitutions") or {}
                literal = tok
                for ph in placeholders(tok):
                    literal = literal.replace("{" + ph + "}", str(subs.get(ph, "{" + ph + "}")))
                outs = [Path(str(o)).name for o in (s.get("detected_outputs") or [])]
                per_row.append(literal if literal in outs else None)
            wrote_it = bool(per_row) and all(o is not None for o in per_row)
            if wrote_it:
                if tok in produced_by and produced_by[tok] < ti:
                    rewrites[ti].append(tok)          # consumed and re-written: stage_in_copy
                    consumers.setdefault(tok, []).append(ti)
                    produced_by[tok] = ti
                else:
                    produced_by.setdefault(tok, ti)
                observed[(ti, tok)] = per_row
            elif tok in produced_by and produced_by[tok] < ti:
                consumers.setdefault(tok, []).append(ti)
            elif not steps_here:
                # no sealed step to observe: a redirect / -o target is the producer, else a consumer
                if re.search(r"(>\s*|-o\s+|--out\S*[= ]\s*)\{[A-Z0-9_]+\}/" + re.escape(tok), tmpl):
                    produced_by.setdefault(tok, ti)
                    observed[(ti, tok)] = []
                    notes.append(f"{tok}: producer inferred from the command text of how-to "
                                 f"command {ti + 1} (no sealed step to confirm it)")
                elif tok in produced_by:
                    consumers.setdefault(tok, []).append(ti)
                else:
                    raise PipelineDerivationError(
                        "pipeline.orphan_artifact",
                        f"how-to command {ti + 1} names `{tok}` inside an output slot, but no "
                        f"earlier command produces it and no sealed step confirms it",
                        "name the artifact after the command that writes it, or declare it as an input")
            else:
                parent = _sidecar_of(tok, produced_by)
                if parent is not None:
                    consumers.setdefault(tok, []).append(ti)
                    continue
                raise PipelineDerivationError(
                    "pipeline.orphan_artifact",
                    f"how-to command {ti + 1} names `{tok}` inside an output slot, but the sealed "
                    f"run never observed it written and no earlier command produces it",
                    "write every intermediate file through the output slot in the command that "
                    "creates it, so the next command's input has a producer")

        # Files the sealed run OBSERVED this command writing that the command text never
        # names (`samtools index x.bam` writes x.bam.bai): outputs all the same, keyed by
        # their templated basename when every row agrees on it.
        if steps_here:
            named_literals: dict[int, set[str]] = {}
            for ri, s in steps_here:
                subs = rows[ri].get("substitutions") or {}
                lits = set()
                for tok in toks:
                    lit = tok
                    for ph in placeholders(tok):
                        lit = lit.replace("{" + ph + "}", str(subs.get(ph, "{" + ph + "}")))
                    lits.add(lit)
                named_literals[ri] = lits
            per_row_extra: dict[str, list[Optional[str]]] = {}
            for ri, s in steps_here:
                subs = rows[ri].get("substitutions") or {}
                for o in (s.get("detected_outputs") or []):
                    b = Path(str(o)).name
                    if b in named_literals.get(ri, set()):
                        continue
                    key = _templatize(b, subs, value_per_sample)
                    per_row_extra.setdefault(key, [None] * len(steps_here))
                    per_row_extra[key][[r for r, _ in steps_here].index(ri)] = b
            for key, per_row in per_row_extra.items():
                if not all(per_row):
                    continue                       # not every row wrote it: not an artifact
                if key in produced_by and produced_by[key] < ti:
                    rewrites[ti].append(key)
                    consumers.setdefault(key, []).append(ti)
                produced_by[key] = ti
                observed[(ti, key)] = per_row
                if key not in template_outputs[ti]:
                    template_outputs[ti].append(key)

    # ── stages ─────────────────────────────────────────────────────────────
    groups = stages if stages else [[i] for i in range(len(templates))]
    flat = [i for g in groups for i in g]
    if sorted(flat) != list(range(len(templates))) or any(g != sorted(g) for g in groups) \
            or flat != sorted(flat):
        raise PipelineDerivationError(
            "pipeline.bad_stage_groups",
            f"stages={groups} must partition the how-to commands 0..{len(templates) - 1} in order",
            "list consecutive command indices, every command exactly once")
    if stages:
        notes.append(f"stage cut set by caller: {groups}")
    else:
        notes.append("stage cut: one stage per how-to command (default)")

    # scope: the seal's self-test ran EVERY how-to command once per trial, so every
    # command of a workflow rendered per sample ran per sample — a command that binds no
    # per-sample value included. A cohort stage is never inferred: it comes from a
    # workflow attached with `cohort=`, whose seal ran once over the cohort's inputs.
    template_scope: list[ScopeT] = ["cohort" if _collected is not None else "per_sample"] * len(templates)

    stage_of_template: dict[int, int] = {}
    stage_recs: list[PipelineStage] = []
    used_names: set[str] = set()
    for gi, g in enumerate(groups):
        scopes = {template_scope[i] for i in g}
        steps_in = [s for i in g for _, s in matched[i]]
        digests = {str(s.get("container_image_digest")) for s in steps_in if s.get("container_image_digest")}
        if len(digests) > 1:
            raise PipelineDerivationError(
                "pipeline.stage_crosses_images",
                f"stage {gi + 1} groups commands that ran in different images {sorted(digests)}",
                "split the stage at the env boundary")
        digest = next(iter(digests)) if digests else None
        if digest is None and len(env_map) == 1:
            digest = next(iter(env_map))
            notes.append(f"stage {gi + 1}: image taken from the workflow's single env "
                         f"(no sealed step backs its command)")
        if digest is None and steps_in == [] and len(env_map) != 1:
            raise PipelineDerivationError(
                "pipeline.template_unmatched",
                f"how-to command(s) {[i + 1 for i in g]} match no sealed step, so no image can be "
                f"named for them (the workflow pins {len(env_map)} envs)",
                "make the how-to commands the literal commands the sealed steps ran, with "
                "placeholders where the trial values were")
        env = env_map.get(digest or "", {})
        image = next((str(s.get("container_image")) for s in steps_in if s.get("container_image")),
                     env.get("image"))
        sif = next((str(s.get("cluster_sif_sha256")) for s in steps_in if s.get("cluster_sif_sha256")), None)
        tool = str(core_data.default_step_tool(templates[g[0]]) or "stage")
        base_from = tool
        if tool in _INTERPRETERS:
            # `Rscript {SCRIPT} …`: the script, not the interpreter, is what the stage does
            m = re.search(re.escape(tool) + r"\s+\{([A-Z][A-Z0-9_]*)\}", templates[g[0]])
            sc = next((sc for sc in scripts if m and sc.param == m.group(1)), None)
            if sc is not None:
                tool = f"{tool} {sc.name}"
                base_from = Path(sc.name).stem
        base = (stage_names[gi] if stage_names and gi < len(stage_names) else _sanitize_name(base_from))
        nm, k = base, 2
        while nm in used_names:
            nm, k = f"{base}_{k}", k + 1
        used_names.add(nm)
        for i in g:
            stage_of_template[i] = gi
        # resources
        walls = [float((s.get("resource_usage") or {}).get("wall_seconds") or 0) for s in steps_in]
        rss = [float((s.get("resource_usage") or {}).get("peak_rss_mb") or 0) for s in steps_in]
        cpu = [float((s.get("resource_usage") or {}).get("max_cpu_percent") or 0) for s in steps_in]
        auths = {core_data.resource_usage_authority(s) for s in steps_in}
        authority = "none" if not steps_in else (next(iter(auths)) if len(auths) == 1 else "mixed")
        gpus = 0
        for s in steps_in:
            gp = s.get("gpu_placement") or {}
            cs = s.get("cluster_slurm") or {}
            gpus = max(gpus, int(gp.get("gpus") or 0), int(cs.get("gpus") or 0))
        req = dict((resources or {}).get(nm) or {})
        # a thread slot in any of the stage's commands: the caller's cpus if sized, else the
        # sealed count — either way the command reads task.cpus
        slot = next((ph for i in g for ph in placeholders(templates[i]) if ph in cpus_of), None)
        res = StageResources(
            cpus=int(req["cpus"]) if req.get("cpus") is not None else (cpus_of[slot] if slot else None),
            mem=str(req["mem"]) if req.get("mem") else None,
            time=str(req["time"]) if req.get("time") else None,
            gpus=int(req.get("gpus", gpus) or 0),
            requested_by="caller" if req else ("seal" if slot else "default"),
            threads_slot=slot,
            measured_wall_seconds=max(walls) if walls else None,
            measured_peak_rss_mb=max(rss) if rss else None,
            measured_max_cpu_percent=max(cpu) if cpu else None,
            measured_authority=authority,
            measured_on=(f"{len(steps_in)} sealed step{'s' if len(steps_in) != 1 else ''} on the "
                         f"workflow's test data") if steps_in else None)
        stage_recs.append(PipelineStage(
            name=nm, index=gi, scope=next(iter(scopes)), templates=list(g),
            commands=[templates[i] for i in g], tool=tool,
            sealed_steps=sorted(int(s["step"]) for s in steps_in),
            image=image, image_digest=digest, request_key=env.get("request_key"),
            env_name=(env_names or {}).get(str(env.get("request_key"))),
            sif_sha256=sif, sif_path=(sif_paths or {}).get(str(env.get("request_key"))),
            inputs=[], outputs=[], consumes_workdir=any(template_bare[i] for i in g),
            stage_in_copy=any(rewrites[i] for i in g), resources=res))

    # ── wire inputs / outputs at stage level ───────────────────────────────
    declared_globs = [(str(o.get("name")), str(f)) for o in declared_outputs for f in (o.get("files") or [])]
    stage_by_name = {s.name: s for s in stage_recs}
    for st in stage_recs:
        seen_in: set[str] = set()
        for i in st.templates:
            for ph in placeholders(templates[i]):
                if ph in out_slots or ph in seen_in:
                    continue
                seen_in.add(ph)
                p = next(pp for pp in params if pp.name == ph)
                p.used_by.append(st.name)
                origin = {"per_sample": "column", "cpus": "request", "samplesheet": "samplesheet",
                          "collected": "collect"}.get(p.kind, "param")
                st.inputs.append(StageInput(
                    name=ph, origin=origin, from_stage=None,
                    artifact=collected_set.get(ph) if p.kind == "collected" else None))
            for tok in template_tokens[i]:
                src = produced_by.get(tok)
                if src is None or stage_of_template.get(src) == st.index:
                    continue
                if src > i:
                    continue
                parent = _sidecar_of(tok, produced_by)
                from_stage = stage_recs[stage_of_template[src]].name
                key = tok
                if key in seen_in:
                    continue
                seen_in.add(key)
                st.inputs.append(StageInput(name=tok, origin="stage", from_stage=from_stage, artifact=tok))
                # a consumer of X also receives X's sidecars produced before it
                for other, osrc in produced_by.items():
                    if other != tok and _sidecar_of(other, {tok: src}) == tok and osrc < i \
                            and stage_of_template[osrc] != st.index and other not in seen_in:
                        seen_in.add(other)
                        st.inputs.append(StageInput(name=other, origin="stage",
                                                    from_stage=stage_recs[stage_of_template[osrc]].name,
                                                    artifact=other))
                _ = parent
        # outputs this stage produces (last producer wins across its templates)
        for i in st.templates:
            for tok in template_outputs[i]:
                if produced_by.get(tok) != i:
                    continue
                per_row = observed.get((i, tok), [])
                subs0 = rows[0].get("substitutions") or {}
                obs0 = per_row[0] if per_row else None
                templated = [_templatize(o, rows[ri].get("substitutions") or {}, value_per_sample)
                             for ri, o in enumerate(per_row) if o]
                if templated and len(set(templated)) == 1:
                    art = templated[0]
                elif templated:
                    art = _common_suffix_glob(templated)
                    notes.append(f"{st.name}: `{tok}` is named differently per row; declared as {art!r}")
                else:
                    art = tok
                _ = subs0
                pattern = None
                for _slot, glob in declared_globs:
                    cand = obs0 or art
                    if fnmatch.fnmatch(cand, glob) or fnmatch.fnmatch(art, glob):
                        pattern = glob
                        break
                consumed = sorted({stage_recs[stage_of_template[c]].name for c in consumers.get(tok, [])
                                   if stage_of_template[c] != st.index})
                st.outputs.append(StageOutput(
                    artifact=art, observed=obs0,
                    consumed_by=consumed, declared_pattern=pattern))
    _ = stage_by_name

    # ── samplesheet: always — `sample` (the row key) first, one row per trial ──
    # The key's placeholder is the how-to's own sample identifier when it has one, so
    # the renderer binds that placeholder to the `sample` column; a how-to with no
    # identifier still gets the column (the trial's name), because every task is
    # tagged by it and every result directory named after it.
    id_ph = next((p.name for p in params if p.kind == "per_sample" and p.name in _SAMPLE_NAMES), None)
    cols = [SamplesheetColumn(name="sample", placeholder=id_ph or "SAMPLE", value_kind="value",
                              format=None,
                              description="the row key: it tags every task and names results/<sample>/",
                              read_by=[])]
    for p in params:
        if p.kind != "per_sample" or p.name in _SAMPLE_NAMES:
            continue
        cols.append(SamplesheetColumn(name=p.name.lower(), placeholder=p.name,
                                      value_kind=p.value_kind, format=p.format,
                                      description=p.description, read_by=[]))
    srows: list[dict[str, str]] = []
    for r in rows:
        subs = r.get("substitutions") or {}
        row = {"sample": str(subs.get(id_ph)) if id_ph and subs.get(id_ph) else str(r.get("name"))}
        for c in cols[1:]:
            row[c.name] = str(subs.get(c.placeholder, ""))
        srows.append(row)
    sheet = Samplesheet(columns=cols, rows=srows)
    for p in params:
        if p.kind != "samplesheet" or _collected is not None:
            continue                     # an attached workflow extends the PIPELINE's sheet, in _attach_cohort
        text = (samplesheet_files or {}).get(str(p.default))
        if text is None:
            raise PipelineDerivationError(
                "pipeline.samplesheet_slot_unread",
                f"{p.name} is declared `format: {SAMPLESHEET_FORMAT}` and the sealed run bound {p.default}, "
                f"whose text was not handed to the derivation",
                "pass samplesheet_files={<that path>: <its text>} (the MCP tool reads it for you)")
        _extend_sheet(sheet, p.name, text, list(p.used_by), notes)

    # ── the defaults table ─────────────────────────────────────────────────
    defaults = [
        PipelineDefault(key="stage_cut", value="explicit groups" if stages else "one stage per how-to command",
                        source="caller" if stages else "default"),
        PipelineDefault(key="publish", value="every artifact a stage writes, into the row's directory", source="default"),
        PipelineDefault(key="resume", value="on: the launch line carries -resume; drop it for a fresh run", source="default"),
        PipelineDefault(key="errors", value="finish: a failure submits nothing new and in-flight tasks complete; "
                        "`-resume` re-runs what failed; no retries", source="default"),
        PipelineDefault(key="cache", value="lenient on the cluster, standard locally", source="default"),
        PipelineDefault(key="queue_size", value="50", source="default"),
        PipelineDefault(key="run_records", value="one directory per run, runs/<timestamp>/, never overwritten: "
                        "params.json (every param as resolved), samples.csv as read, trace.txt (every task), "
                        "report.html, timeline.html", source="default"),
        PipelineDefault(key="cleanup", value="never automatic; `nextflow clean -f` when you are done", source="default"),
        PipelineDefault(key="sheet_preflight", value="the samplesheet and every file column are checked as the run starts",
                        source="default"),
        PipelineDefault(key="resources", value="per-stage requests; measurements quoted, never used as the request",
                        source="caller" if resources else "default"),
    ]

    record = PipelineRecord(
        name=name, version="1",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        sealed_workflow=str(spec.workflow_name), sealed_workflow_path=spec_path,
        sealed_workflow_sha256=spec_sha256,
        env_digests=sorted(env_map) or [str(getattr(spec, "env_content_digest", ""))],
        params=params, samplesheet=sheet,
        output_slots=sorted(out_slots), compute_env=compute_env, modules=list(modules or []),
        local_runtime=local_runtime,
        stages=stage_recs, provenance_steps=provenance,
        unmatched_steps=unmatched_steps, scripts=scripts, cohort_workflows=[],
        defaults=defaults, notes=notes)
    for req in (cohort or []):
        _attach_cohort(record, req, resources=resources, env_names=env_names, sif_paths=sif_paths,
                       samplesheet_files=samplesheet_files)
    return record


# ── cohort stages: a second sealed workflow, attached ───────────────────────


def _attach_cohort(record: PipelineRecord, req: CohortRequest, *,
                   resources: Optional[Mapping[str, Mapping[str, Any]]],
                   env_names: Optional[Mapping[str, str]], sif_paths: Optional[Mapping[str, str]],
                   samplesheet_files: Optional[Mapping[str, str]]) -> None:
    """Attach `req.spec` as cohort stages of `record`, in place. The cohort how-to is
    derived like any other (its own sealed steps, images, measurements, scripts), then
    composed: every stage runs once over the cohort; each `collect` input is wired to
    the per-sample stage that writes the artifact, every row's copy; a samplesheet
    slot reads the pipeline's own sheet; its params join params.yaml. Refuses, naming
    the remedy, on an undeclared fan-in, an artifact no per-sample stage writes, one
    not named after the sample (the copies would collide), or a placeholder both
    how-tos use."""
    spec = req.spec
    wf = str(spec.workflow_name)
    per_sample_stages = [s for s in record.stages if s.scope == "per_sample"]
    produced: dict[str, str] = {}                       # templated artifact -> per-sample stage
    for s in per_sample_stages:
        for o in s.outputs:
            produced.setdefault(o.artifact, s.name)
    value_cols = {c.placeholder for c in record.samplesheet.columns if c.value_kind == "value"}
    for ph, art in req.collect.items():
        if art not in produced:
            raise PipelineDerivationError(
                "pipeline.collect_unknown_artifact",
                f"collect= names {art!r} for {ph}, which no per-sample stage of {record.sealed_workflow} "
                f"writes; the per-sample artifacts are {sorted(produced)}",
                "name one of those artifacts, as the record spells it")
        if not any(p in value_cols for p in placeholders(art)):
            raise PipelineDerivationError(
                "pipeline.collect_not_unique",
                f"collect= names {art!r} for {ph}, but that name is the same for every sample, so the "
                f"collected copies would overwrite each other in one directory",
                "write the per-sample output with the sample id in its name (e.g. {SAMPLE}.counts.tsv) "
                "and re-seal the per-sample workflow")
    sub = derive_pipeline_record(
        spec, name=record.name, spec_path=req.spec_path, spec_sha256=req.spec_sha256,
        stages=req.stages, stage_names=req.stage_names, shared=req.shared,
        resources=resources, env_names=env_names, sif_paths=sif_paths,
        compute_env=record.compute_env, modules=record.modules, local_runtime=None,
        samplesheet_files=samplesheet_files, _collected=req.collect)
    undeclared = [i.name for i in sub.params if i.name in req.collect and i.kind != "collected"]
    assert not undeclared
    missing = sorted(set(req.collect) - {p.name for p in sub.params})
    if missing:
        raise PipelineDerivationError(
            "pipeline.collect_unknown_placeholder",
            f"collect= names {missing}, which the how-to of {wf} does not use "
            f"({[p.name for p in sub.params]})",
            "name a placeholder of the cohort how-to")
    per_sample = [p.name for p in sub.params if p.kind == "per_sample"]
    if per_sample:
        raise PipelineDerivationError(
            "pipeline.cohort_binds_per_sample",
            f"the how-to of {wf} binds {per_sample} per sample (values differ across its trials, or a "
            f"reads / sample-id input), but a cohort stage runs once over every sample",
            "declare the fan-in with collect=, or pass shared= for a value that is one per run")
    # params: the two how-tos may not share a placeholder (each name is one params.yaml key)
    base_names = {p.name for p in record.params}
    for p in sub.params:
        if p.kind in ("collected",):
            continue
        if p.name in base_names:
            raise PipelineDerivationError(
                "pipeline.param_collision",
                f"placeholder {p.name} is used by both {record.sealed_workflow} and {wf}; one name is one "
                f"params.yaml key",
                f"rename the placeholder in the how-to of {wf} and re-seal it")
    for sc in sub.scripts:
        if any(x.name == sc.name for x in record.scripts):
            raise PipelineDerivationError(
                "pipeline.script_name_clash",
                f"both workflows carry a script named {sc.name!r}; {SCRIPT_DIR}/ holds one file per name",
                f"rename the script in {wf} and re-seal it")
    # stages: renamed past any clash, re-indexed after the per-sample stages, cohort-scoped
    offset = len(record.stages)
    used = {s.name for s in record.stages}
    rename: dict[str, str] = {}
    for s in sub.stages:
        nm, k = s.name, 2
        while nm in used:
            nm, k = f"{s.name}_{k}", k + 1
        used.add(nm)
        rename[s.name] = nm
    cohort_names = [rename[s.name] for s in sub.stages]
    collected: list[CollectedInput] = []
    for s in sub.stages:
        s.name = rename[s.name]
        s.index += offset
        s.scope = "cohort"
        for i in s.inputs:
            if i.origin == "stage" and i.from_stage:
                i.from_stage = rename.get(i.from_stage, i.from_stage)
            elif i.origin == "collect":
                art = req.collect[i.name]
                i.from_stage = produced[art]
                i.artifact = art
                collected.append(CollectedInput(placeholder=i.name, artifact=art,
                                                from_stage=produced[art], stage=s.name))
                record.stage(produced[art]).outputs[
                    [o.artifact for o in record.stage(produced[art]).outputs].index(art)].consumed_by.append(s.name)
        for o in s.outputs:
            o.consumed_by = [rename.get(c, c) for c in o.consumed_by]
    for p in sub.params:
        p.used_by = [rename.get(u, u) for u in p.used_by]
        if p.source.startswith("sealed_step:"):
            p.source = f"{p.source}@{wf}"
    for c in sub.samplesheet.columns:
        c.read_by = [rename.get(r, r) for r in c.read_by]
    # the sheet: the cohort how-to's samplesheet slot adds the columns it reads
    for p in sub.params:
        if p.kind == "samplesheet":
            text = (samplesheet_files or {}).get(str(p.default))
            if text is None:
                raise PipelineDerivationError(
                    "pipeline.samplesheet_slot_unread",
                    f"{p.name} is declared `format: {SAMPLESHEET_FORMAT}` and the sealed run of {wf} bound "
                    f"{p.default}, whose text was not handed to the derivation",
                    "pass samplesheet_files={<that path>: <its text>} (the MCP tool reads it for you)")
            _extend_sheet(record.samplesheet, p.name, text, list(p.used_by), record.notes)
    record.params.extend(p for p in sub.params if p.kind != "collected")
    record.stages.extend(sub.stages)
    record.scripts.extend(sub.scripts)
    record.provenance_steps.extend(sub.provenance_steps)
    record.env_digests = sorted(set(record.env_digests) | set(sub.env_digests))
    record.output_slots = sorted(set(record.output_slots) | set(sub.output_slots))
    record.cohort_workflows.append(CohortWorkflow(
        sealed_workflow=wf, sealed_workflow_path=req.spec_path, sealed_workflow_sha256=req.spec_sha256,
        stages=cohort_names, collect=collected))
    if not any(d.key == "cohort" for d in record.defaults):
        record.defaults.append(PipelineDefault(
            key="cohort", value="a cohort stage runs once, after every sample has passed the stage it collects "
            "from; its results are published flat under results/", source="default"))
    record.notes.append(f"cohort stages {cohort_names} from sealed workflow {wf}: "
                        + "; ".join(f"{c.placeholder} = every sample's {c.artifact} from {c.from_stage}"
                                    for c in collected))
    record.notes.extend(f"{wf}: {n}" for n in sub.notes)


# ── the samplesheet, ONE rendering ──────────────────────────────────────────


def render_samplesheet(record: PipelineRecord) -> str:
    """`samples.csv`: the record's columns as the header, the seal's trials as the
    worked-example rows. The Nextflow renderer ships this text and the lint compares
    against it — ONE rendering."""
    cols = [c.name for c in record.samplesheet.columns]
    lines = [",".join(cols)]
    for row in record.samplesheet.rows:
        lines.append(",".join(str(row.get(c, "")) for c in cols))
    return "\n".join(lines) + "\n"


# ── disk ────────────────────────────────────────────────────────────────────

RECORD_FILENAME = "pipeline.yaml"


def record_yaml(record: PipelineRecord) -> str:
    """THE text of a pipeline record on disk — explicit nulls kept so it round-trips.
    One rendering, so the directory writer's manifest and this module's writer
    cannot disagree about the bytes."""
    return yaml.safe_dump(record.model_dump(), sort_keys=False, default_flow_style=False)


def write_pipeline_record(record: PipelineRecord, out_dir: Path) -> Path:
    """`<out_dir>/pipeline.yaml`. The renderers and the page read only this file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / RECORD_FILENAME
    path.write_text(record_yaml(record))
    return path


def load_pipeline_record(path: Path) -> PipelineRecord:
    """THE typed reader for a pipeline record — a malformed file fails here, loudly."""
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"pipeline record at {path} is not a mapping")
    return PipelineRecord.model_validate(raw)


def sha256_of(path: Path) -> Optional[str]:
    try:
        return core_data.sha256_file(str(path))
    except Exception:
        return None


__all__ = [
    "PipelineDerivationError", "PipelineRecord", "PipelineStage", "PipelineParam",
    "StageInput", "StageOutput", "StageResources", "Samplesheet", "SamplesheetColumn", "LocalRuntime",
    "PipelineDefault", "ProvenanceStep", "PipelineScript", "CollectedInput", "CohortWorkflow", "CohortRequest",
    "derive_pipeline_record", "write_pipeline_record",
    "record_yaml", "load_pipeline_record", "placeholders", "artifact_tokens", "RECORD_FILENAME",
    "DEFAULT_STAGE_REQUEST", "MANAGER_JOB_REQUEST", "NEXTFLOW_QUEUE_SIZE", "THREADS_FORMAT", "SAMPLESHEET_FORMAT",
    "SCRIPT_DIR", "render_samplesheet",
]
