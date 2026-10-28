"""
pipeline_record — the typed PIPELINE record, derived from a sealed WorkflowSpec.

A pipeline is a RENDER of a sealed workflow, and it invents nothing: the stages are
the sealed how-to's commands, the images are the sealed steps' observed digests, the
samplesheet columns are the how-to's per-sample inputs, and what the how-to declares
as its result (`usage.outputs`), with its sidecars, is published into the row's
directory, as the sealed run left it — every artifact, when it declares none the run
wrote. This module
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
             (a local run caps it at the machine's cpus)
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

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

import yaml
from pydantic import BaseModel, ConfigDict

from agent.models import core_data
from agent.models.core_data import SLOT_RE, is_output_slot, output_matches, output_pattern, slots_in as placeholders
from agent.skills.spec_writer import _SIDECAR_SUFFIXES

#: Characters that can appear in a filename token named inside an output slot.
_TOKEN_CHARS = r"[^\s'\"|;&<>()]+"
#: Formats whose inputs are per-sample by nature when only one trial exists.
_READ_FORMATS = frozenset({"fastq", "fq", "fastq.gz", "fq.gz", "bam", "ubam", "cram",
                           "pod5", "fast5", "sam"})
_SAMPLE_NAMES = frozenset({"SAMPLE", "SAMPLE_ID", "SAMPLE_NAME", "ID"})
#: A how-to input declared with this format is a THREAD SLOT (see the vocabulary).
THREADS_FORMAT = "threads"
#: A how-to input declared with this format is a SAMPLESHEET SLOT (see the vocabulary).
#: A how-to input declared with this format is a VALUE however it is spelled: a URL or a
#: path inside the image is passed as text, never staged as a file.
VALUE_FORMAT = "value"
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
#: Nextflow's slurm throughput, stated in the record's `defaults` table: at most this
#: many jobs queued or running at once, submitted at no more than this rate. One
#: pipeline then never floods the scheduler or pins a user's whole job allowance, and a
#: second pipeline can run beside it. Both are per-pipeline knobs in nextflow.config.
NEXTFLOW_QUEUE_SIZE = 500
NEXTFLOW_SUBMIT_RATE = "100/1min"

ScopeT = Literal["per_sample", "cohort"]


class PipelineDerivationError(ValueError):
    """A refusal: the seal cannot support this render. `code` is the outcome tag the
    MCP wrapper emits; `remedy` names what to change."""

    def __init__(self, code: str, error: str, remedy: str = ""):
        super().__init__(error)
        self.code, self.error, self.remedy = code, error, remedy


# ── the record ──────────────────────────────────────────────────────────────


class IndexFile(BaseModel):
    """A file a tool finds beside its input by name (an index): the input's path with
    the extension `strip` removed (or nothing removed) and `add` appended."""
    model_config = ConfigDict(extra="forbid")
    strip: str                                 # "" for x.bam.bai; ".bam" for x.bai
    add: str

    def beside(self, path: str) -> Optional[str]:
        """This index's path beside `path`; None when `path` lacks the extension it replaces."""
        if not self.strip:
            return path + self.add
        return path[:-len(self.strip)] + self.add if path.endswith(self.strip) else None


class PipelineParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str                                  # placeholder, e.g. STRANDED
    kind: Literal["shared", "per_sample", "cpus", "samplesheet", "collected", "resolved"]
    #   cpus: a thread slot, bound to the stage's CPU request · samplesheet: the pipeline's own
    #   samples.csv · collected: every row's copy of a per-sample artifact (a cohort stage's fan-in) ·
    #   resolved: looked up at run time, the first line of a file a stage writes (resolve=)
    value_kind: Literal["path", "prefix", "value"]   # prefix: names a FAMILY of files (an aligner index)
    default: Optional[str]                     # the sealed trial's value for a shared param; None for a per-sample one, whose values are the samplesheet's rows
    source: str                                # usage_input | literal | test_data:<key> | reference_database:<name> | sealed_step:<n> | authored_artifact:<name> | collect:<artifact>
    format: Optional[str]
    description: Optional[str]
    used_by: list[str]                         # stage names
    reason: str                                # why it was classified as it was
    index_files: list[IndexFile]               # staged beside a path input: found beside every value the sealed run read


class StageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str                                  # a placeholder, or an artifact name
    origin: Literal["param", "column", "stage", "request", "samplesheet", "collect", "resolve"]
    #   request: the stage's own cpus (a thread slot) · samplesheet: the pipeline's samples.csv ·
    #   collect: every row's copy of a per-sample artifact, from `from_stage` ·
    #   resolve: a value `from_stage` looked up at run time, the first line of `artifact`
    from_stage: Optional[str]
    artifact: Optional[str]                    # origin=stage|collect|resolve: the artifact (templated basename or glob)


class StageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: str                              # templated basename, or a glob over the input's stem
    observed: Optional[str]                    # basename observed in the sealed run (row 0), None if never observed
    directory: bool                            # the artifact is a directory the stage writes a tree into
    consumed_by: list[str]
    declared_pattern: Optional[str]            # the usage.outputs glob that matched, if any
    published: bool                            # into results/: declared, a declared file's sidecar, or all when nothing is


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
    index_files: list[IndexFile]               # staged beside every row's file (a path column)


class Samplesheet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: list[SamplesheetColumn]           # `sample` first, always
    rows: list[dict[str, str]]                 # the seal's trials — the worked example
    rows_from: Literal["proven", "declared"]   # proven: the I4 self-test ran them; declared: it did not (a multi-env how-to)


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
    from_stage: str                            # the last per-sample stage that writes it
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


class ResolvedValue(BaseModel):
    """A placeholder whose value the pipeline looks up at run time: the first line of
    the file `stage` writes fills it for every later stage and names the sample."""
    model_config = ConfigDict(extra="forbid")
    placeholder: str
    artifact: str                              # the file, as the how-to spells it under the output slot
    stage: str                                 # the stage that writes it; it runs on every launch


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
    resolved: list[ResolvedValue]              # values looked up at run time (resolve=), at most one
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


def artifact_tokens(template: str, out_slots: set[str]) -> list[str]:
    """The `<name>` fragments the template names inside an output slot,
    `{OUTPUT_DIR}/<name>`, first-occurrence order. A deeper path names its top-level
    entry: `{OUTPUT_DIR}/kg/nodes.tsv` names the directory artifact `kg`."""
    toks: list[str] = []
    for slot in sorted(out_slots):
        for m in re.finditer(r"\{" + slot + r"\}/(" + _TOKEN_CHARS + ")", template):
            tok = m.group(1).split("/", 1)[0]
            if tok and tok not in toks:
                toks.append(tok)
    return toks


def _names_bare_slot(template: str, out_slots: set[str]) -> bool:
    return any(re.search(r"\{" + slot + r"\}(?!/)", template) for slot in out_slots)


def _template_regex(template: str, subs: Mapping[str, str], out_slots: set[str]) -> re.Pattern:
    """A pattern that matches the literal command the how-to produces for one row:
    input placeholders bound to the row's values, output slots left as wildcards
    (the seal's steps and the I4 self-test wrote to different output dirs). Each
    output slot's value is captured once (`groupdict()` is the directories the
    matched command wrote into); a later occurrence of the same slot must repeat it."""
    parts: list[str] = []
    pos = 0
    groups: dict[str, str] = {}
    text = ""            # literal text and input values, collapsed together (an empty value leaves one space)
    for m in SLOT_RE.finditer(template):
        text += template[pos:m.start()]
        name = m.group(1)
        if name in out_slots:
            # the command is matched trimmed (`_ws`), so the template's leading edge is too
            parts.append(re.escape(_collapse(text).lstrip() if not parts else _collapse(text)))
            text = ""
            if name in groups:
                parts.append(f"(?P={groups[name]})")
            else:
                groups[name] = f"out{len(groups)}"
                parts.append(rf"(?P<{groups[name]}>\S+)")
        else:
            if name not in subs:
                raise KeyError(name)
            text += _ws(subs[name])
        pos = m.end()
    tail = _collapse(text + template[pos:])
    parts.append(re.escape((tail.lstrip() if not parts else tail).rstrip()))
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


def _input_stem_glob(name: str, row_subs: Mapping[str, str], path_params: list[str],
                     value_params: list[str]) -> Optional[str]:
    """A file a tool names after its per-sample input (`s1_R1.fastq.gz` →
    `s1_R1_fastqc.html`) is one artifact across rows: the longest stem of the input
    (its name cut at a dot, `HG002.novaseq.R1` before `HG002`) that starts the file's
    name becomes `*` (`*_fastqc.html`). None when no stem does, or when the stem is a
    per-sample value itself: `{SAMPLE}` then names the file exactly."""
    stems = set()
    for ph in path_params:
        parts = Path(str(row_subs.get(ph) or "")).name.split(".")
        stems |= {".".join(parts[:k]) for k in range(1, len(parts))}
    values = {str(row_subs.get(ph) or "") for ph in value_params}
    for stem in sorted(stems, key=len, reverse=True):
        if len(stem) >= 2 and name.startswith(stem) and name != stem:
            return None if stem in values else "*" + name[len(stem):]
    return None


#: An index that replaces its file's extension rather than adding to it (GATK, Picard).
_REPLACING_SIDECARS = {".bai": (".bam",), ".crai": (".cram",), ".dict": (".fa", ".fasta", ".fna")}


def _sidecar_of(name: str, produced: Mapping[str, object]) -> Optional[str]:
    """`x.bam.bai` — or `x.bai` — is a sidecar of `x.bam` when `x.bam` is a known artifact."""
    low = name.lower()
    for suf in _SIDECAR_SUFFIXES:
        if low.endswith(suf) and name[: -len(suf)] in produced:
            return name[: -len(suf)]
    for suf, exts in _REPLACING_SIDECARS.items():
        if low.endswith(suf):
            return next((name[: -len(suf)] + e for e in exts if name[: -len(suf)] + e in produced), None)
    return None


#: Index files a tool looks for beside its input, by the name it derives from it.
_INDEX_SUFFIXES = (".bai", ".crai", ".csi", ".tbi", ".fai", ".gzi", ".idx")


def index_candidates(path: str) -> list[tuple[IndexFile, str]]:
    """Every index file a tool could look for beside `path`, with the path it would be at."""
    out = [(IndexFile(strip="", add=suf), path + suf) for suf in _INDEX_SUFFIXES]
    for suf, exts in _REPLACING_SIDECARS.items():
        out += [(IndexFile(strip=e, add=suf), path[: -len(e)] + suf) for e in exts if path.endswith(e)]
    return out


def _index_files_for(values: list[str], found: Mapping[str, list[str]]) -> list[IndexFile]:
    """The index files that sat beside EVERY value the sealed run read."""
    if not values:
        return []
    keep = []
    for ix, _ in index_candidates(values[0]):
        if all(any(c == ix and sp in (found.get(v) or []) for c, sp in index_candidates(v)) for v in values):
            keep.append(ix)
    return keep


def _observed_files(step: Mapping, out_dirs: list[str], name: str) -> list[str]:
    """The files a sealed step was observed writing as artifact `name` (the file itself,
    or every file of the tree), each by its path under the directory it wrote into."""
    rels = []
    for o in (step.get("detected_outputs") or []):
        p = str(o)
        if _artifact_key(p, out_dirs)[0] == name:
            root = next((d.rstrip("/") + "/" for d in out_dirs if p.startswith(d.rstrip("/") + "/")), "")
            rels.append(p[len(root):] if root else Path(p).name)
    return rels


def _artifact_key(path: str, out_dirs: list[str]) -> tuple[str, bool]:
    """The artifact an observed output belongs to: its basename when it lies directly in
    a directory the step wrote into, else the top-level directory it lies under there (a
    tool that writes a tree is observed writing the tree). Returns (name, is_directory);
    a path under none of the directories is its basename, a file."""
    for d in out_dirs:
        root = d.rstrip("/") + "/"
        if path.startswith(root):
            head, sep, _ = path[len(root):].partition("/")
            if head:
                return head, bool(sep)
    return Path(path).name, False


def _observed_keys(step: Mapping, out_dirs: list[str]) -> dict[str, bool]:
    """Every artifact a sealed step was observed writing, `{name: is_directory}`."""
    keys: dict[str, bool] = {}
    for o in (step.get("detected_outputs") or []):
        name, is_dir = _artifact_key(str(o), out_dirs)
        keys[name] = keys.get(name, False) or is_dir
    return keys


# ── derivation ──────────────────────────────────────────────────────────────


def _thread_slot(ph: str, values: list[str], declared: Mapping, caller_named: set[str]) -> PipelineParam:
    """A placeholder declared `format: threads`, as a `cpus` param: every trial must
    bind one and the same positive whole number — the stage's CPU request needs ONE
    count, and a count is what a thread flag takes. Refuses, naming the remedy, when
    the caller tried to make it a column, a params.yaml value or a collected input."""
    if ph in caller_named:
        raise PipelineDerivationError(
            "pipeline.threads_kind",
            f"{ph} is declared `format: {THREADS_FORMAT}`, which binds it to the stage's CPU request; "
            f"it cannot be a samplesheet column, a params.yaml value or a collected input",
            "drop it from per_sample= / shared= / collect=, or change the how-to input's format")
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
        reason=f"declared format {THREADS_FORMAT!r}: the stage's CPU request, {values[0]} in the sealed run",
        index_files=[])


def _sheet_slot(ph: str, values: list[str], declared: Mapping, caller_named: set[str]) -> PipelineParam:
    """A placeholder declared `format: samplesheet`: the pipeline's own samples.csv,
    handed to the stage as a file. Every trial binds the sheet it was proven with;
    the render binds `params.samplesheet` instead. Refuses a caller who tried to make
    it a column, a params.yaml value or a collected input."""
    if ph in caller_named:
        raise PipelineDerivationError(
            "pipeline.samplesheet_kind",
            f"{ph} is declared `format: {SAMPLESHEET_FORMAT}`, which binds it to the pipeline's own "
            f"samplesheet; it cannot be a samplesheet column, a params.yaml value or a collected input",
            "drop it from per_sample= / shared= / collect=, or change the how-to input's format")
    return PipelineParam(
        name=ph, kind="samplesheet", value_kind="path", default=values[0], source="usage_input",
        format=declared.get("format"), description=declared.get("description"), used_by=[],
        reason=f"declared format {SAMPLESHEET_FORMAT!r}: the pipeline's samples.csv; the sealed run read {values[0]}",
        index_files=[])


def _parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text.removeprefix("\ufeff")))   # a byte-order mark is not a column name
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
            description=None, read_by=list(stage_names), index_files=[]))
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
                           index_files: Optional[Mapping[str, list[str]]] = None,
                           cohort: Optional[list[CohortRequest]] = None,
                           resolve: Optional[Mapping[str, str]] = None,
                           _collected: Optional[Mapping[str, str]] = None) -> PipelineRecord:
    """Derive the pipeline record from a sealed WorkflowSpec. Raises
    PipelineDerivationError (a refusal with a remedy) when the seal cannot support
    the render; every derivation the caller did not dictate is stated in `notes`.

    `samplesheet_files` is `{path: csv text}` for every samplesheet the sealed trials
    bound to a `format: samplesheet` input, and `index_files` `{path: [index files
    found beside it]}` for the paths they bound that are on the caller's machine (see
    `index_candidates`) — the caller reads the disk, this stays pure.
    `cohort` attaches further sealed workflows as cohort stages (see the vocabulary).
    `resolve` is `{PLACEHOLDER: file}`: the placeholder's value is looked up at run time,
    the first line of that file as the stage that writes it wrote it; it fills the
    placeholder for every later stage and names the sample (see the vocabulary).
    `_collected` is the attachment's own: the cohort how-to's fan-in placeholders."""
    notes: list[str] = []
    collected_set = dict(_collected or {})
    resolved_set = {str(k): str(v) for k, v in (resolve or {}).items()}
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
    out_slots = {p for p in all_placeholders if is_output_slot(p)}
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
    caller_named = per_sample_set | shared_set | set(collected_set)
    unknown = (per_sample_set | shared_set) - set(in_placeholders)
    if unknown:
        raise PipelineDerivationError(
            "pipeline.unknown_placeholder",
            f"{sorted(unknown)} are not placeholders of the how-to ({in_placeholders})",
            "name a placeholder the how-to uses")
    if len(resolved_set) > 1:
        raise PipelineDerivationError(
            "pipeline.resolve_one",
            f"resolve= names {sorted(resolved_set)}; one value is looked up per pipeline",
            "declare one placeholder; the stage that looks it up may write what the rest need as files")
    for ph, art in resolved_set.items():
        if ph not in in_placeholders:
            raise PipelineDerivationError(
                "pipeline.resolve_unknown_placeholder",
                f"resolve= names {ph!r}, not a placeholder of the how-to ({in_placeholders})",
                "name a placeholder a later command uses")
        if ph in per_sample_set | shared_set | set(collected_set):
            raise PipelineDerivationError(
                "pipeline.resolve_kind",
                f"{ph} is looked up at run time (resolve=); it cannot also be a samplesheet column, "
                f"a params.yaml value or a collected input",
                "drop it from per_sample= / shared= / collect=, or from resolve=")
        if not art or "/" in art or placeholders(art) or any(c in art for c in "*?"):
            raise PipelineDerivationError(
                "pipeline.resolve_artifact_name",
                f"resolve= reads {ph} from {art!r}, which is not one fixed file name under the output slot",
                "name the file as the how-to writes it, `{OUTPUT_DIR}/<name>`: no directory, glob or placeholder")
    caller_named |= set(resolved_set)
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
            slot = key.rsplit(".", 1)[-1]                 # `samples.2.r1` → `r1`
            if slot in ("r1", "r2") or slot.startswith(("r1_", "r2_")):
                return "per_sample", f"bound to test_data.{key} (reads)"
        return "shared", "one trial, not a reads input"

    # an index file the how-to binds as an input of its own is staged through that input
    bound_values = {str(v) for r in rows for v in (r.get("substitutions") or {}).values()}
    beside = {p: [f for f in fs if f not in bound_values] for p, fs in (index_files or {}).items()}
    params: list[PipelineParam] = []
    for ph in in_placeholders:
        values = [str((r.get("substitutions") or {})[ph]) for r in rows]
        v0 = values[0]
        fmt_declared = str((declared_inputs.get(ph) or {}).get("format") or "").lower()
        if fmt_declared == THREADS_FORMAT:
            params.append(_thread_slot(ph, values, declared_inputs.get(ph) or {}, caller_named))
            notes.append(f"{ph}: cpus ({params[-1].reason})")
            continue
        if fmt_declared == SAMPLESHEET_FORMAT:
            params.append(_sheet_slot(ph, values, declared_inputs.get(ph) or {}, caller_named))
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
                       f"the sealed run read {v0}", index_files=[]))
            notes.append(f"{ph}: collected ({params[-1].reason})")
            continue
        if ph in resolved_set:
            di = declared_inputs.get(ph) or {}
            params.append(PipelineParam(
                name=ph, kind="resolved", value_kind="value", default=v0, source=f"resolve:{resolved_set[ph]}",
                format=di.get("format"), description=di.get("description"), used_by=[],
                reason=f"declared resolve=: the first line of {resolved_set[ph]}, written at run time, fills it "
                       f"and names the sample; the sealed run bound {v0}", index_files=[]))
            notes.append(f"{ph}: resolved ({params[-1].reason})")
            continue
        kind, why = classify(ph)
        given = [v for v in values if v != ""]
        # a declared `format: value` is a value whatever it looks like (a URL, a bucket
        # address, a path inside the image); otherwise a slash makes it a path to stage
        value_kind = ("value" if fmt_declared == VALUE_FORMAT
                      else "path" if any(core_data.is_path_like(v) for v in given) else "value")
        if value_kind == "path" and len(given) < len(values):
            raise PipelineDerivationError(
                "pipeline.optional_path",
                f"{ph} is a path in one trial and empty in another: an optional file input, which "
                f"the render cannot stage",
                "bind a file in every trial, or make the option a value (a flag the command reads)")
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
        indexes = _index_files_for(given, beside) if value_kind == "path" else []
        if value_kind == "path" and index_files is not None and any(v not in index_files for v in given):
            notes.append(f"{ph}: the sealed run's files are not on this machine, so no index file beside "
                         f"them was looked for; a tool that finds one by name will not see it in the task")
        params.append(PipelineParam(
            name=ph, kind=kind, value_kind=value_kind,
            default=v0 if kind == "shared" else None,     # a per-sample value is a samplesheet cell
            source=source, format=di.get("format"), description=di.get("description"),
            used_by=[], reason=why, index_files=indexes))
        if value_kind == "path" and not indexes and any(beside.get(v) for v in given):
            notes.append(f"{ph}: index files sit beside its files, but not under one name for every "
                         f"row, so none is staged; name them alike (e.g. <file>.bai) to have them staged")
        if indexes:
            notes.append(f"{ph}: staged with its index file(s) "
                         f"{', '.join(f'{ix.add} in place of {ix.strip}' if ix.strip else ix.add for ix in indexes)}, "
                         f"found beside every file the sealed run read")
        proven = (f"; the self-test ran it with {sorted(set(values))} only"
                  if row_source == "proven" and value_kind == "value" and ph not in _SAMPLE_NAMES else "")
        notes.append(f"{ph}: {kind} ({why}){proven}")
    # a shared path that is an authored artifact of the workflow is either a SCRIPT —
    # its full text in the record, carried verbatim into bin/, the param defaulting to
    # that relative path — or an authored DATA file (or a script too large to carry):
    # that stays a path input, anchored by the record's sha256. A
    # script-suffixed path the seal only excerpted is the one shape refused: the render
    # cannot carry a script it does not have.
    scripts: list[PipelineScript] = []
    authored = [a for a in (spec_d.get("authored_artifacts") or []) if isinstance(a, dict)]
    for p in params:
        if p.kind != "shared" or p.value_kind != "path" or not p.default:
            continue
        if p.source.startswith(("reference_database:", "test_data:")):
            continue   # declared data; an authored record at the same path changes nothing
        art = next((a for a in authored if str(a.get("path") or "") == p.default), None)
        if art is None:
            continue
        content = art.get("content_excerpt")
        carried = isinstance(content, str) and int(art.get("size_bytes") or 0) == len(content.encode("utf-8"))
        if not carried:
            if core_data.is_script_path(p.default):
                raise PipelineDerivationError(
                    "pipeline.script_not_carried",
                    f"{p.name} names the authored script {p.default}, but the sealed record does not carry "
                    f"its full text (size {art.get('size_bytes')} bytes)",
                    "stage scripts under 64 KiB (content mode, or generated_by for a script "
                    "already on disk) so the seal carries them verbatim")
            bn = Path(p.default).name
            p.source = f"authored_artifact:{bn}"
            p.reason += "; an authored data file, kept as a path input and pinned by its sha256"
            notes.append(f"{p.name}: an authored data file ({bn}), a path input pinned by sha256")
            continue
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
    value_per_sample = [p.name for p in params if p.kind == "per_sample" and p.value_kind == "value"]
    path_per_sample = [p.name for p in params if p.kind == "per_sample" and p.value_kind != "value"]
    cpus_of = {p.name: int(p.default or 0) for p in params if p.kind == "cpus"}

    # ── match sealed steps to (template, row) ──────────────────────────────
    matched: dict[int, list[tuple[int, dict]]] = {i: [] for i in range(len(templates))}
    wrote_into: dict[int, list[str]] = {}      # step number -> the output directories its command named
    claimed: set[int] = set()
    for ti, tmpl in enumerate(templates):
        for ri, r in enumerate(rows):
            pat = _template_regex(tmpl, r.get("substitutions") or {}, out_slots)
            for s in ok_steps:
                n = int(s.get("step", 0))
                if n in claimed:
                    continue
                hit = pat.fullmatch(_ws(s.get("command", "")))
                if hit:
                    matched[ti].append((ri, s))
                    wrote_into[n] = [v for v in hit.groupdict().values() if v]
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

    # ── artifacts: who wrote each, and the names it was observed under ─────
    env_map: dict[str, dict] = {}
    for e in (spec_d.get("envs") or []):
        if isinstance(e, dict) and e.get("image_digest"):
            env_map[str(e["image_digest"])] = e
    if not env_map:
        # Every stage runs in an image, and the seal pins only the images its steps
        # ran in. A workflow whose steps all ran on the host pins none, and saying so
        # by way of "command matches no step" sent a reader to compare command text for
        # three rounds when the command was fine and the locus was the fault.
        raise PipelineDerivationError(
            "pipeline.no_container_steps",
            "no sealed step ran inside a frozen image, so no stage can name the image it "
            "runs in (the workflow pins 0 envs)",
            "run each how-to command with run_step_in_container (not run_pipeline_step), "
            "then re-seal: the pipeline executes inside the images the seal pins")

    history: dict[str, list[int]] = {}         # artifact -> every template that wrote it so far, in order
    observed: dict[tuple[int, str], list[Optional[str]]] = {}   # (template, artifact) -> per-row observed name
    directories: set[str] = set()              # artifacts observed as directories (a tree written under them)
    template_tokens: list[list[str]] = []
    template_bare: list[bool] = []
    rewrites: dict[int, list[str]] = {i: [] for i in range(len(templates))}
    template_outputs: list[list[str]] = []   # named tokens + observed-but-unnamed artifacts

    def _written_before(tok: str, ti: int) -> bool:
        return bool(history.get(tok)) and history[tok][-1] < ti

    def _produce(tok: str, ti: int) -> None:
        if _written_before(tok, ti):
            rewrites[ti].append(tok)          # consumed and re-written: stage_in_copy
        if not history.get(tok) or history[tok][-1] != ti:
            history.setdefault(tok, []).append(ti)

    for ti, tmpl in enumerate(templates):
        toks = artifact_tokens(tmpl, out_slots)
        template_tokens.append(toks)
        template_bare.append(_names_bare_slot(tmpl, out_slots))
        template_outputs.append(list(toks))
        steps_here = matched[ti]
        # what each row's step was observed writing, as artifacts: a file directly in the
        # output directory by its basename, a tree by its top-level directory
        keys_here = {ri: _observed_keys(s, wrote_into.get(int(s.get("step", 0)), [])) for ri, s in steps_here}
        for tok in toks:
            # observed names for this token across rows
            per_row: list[Optional[str]] = []
            for ri, s in steps_here:
                subs = rows[ri].get("substitutions") or {}
                literal = tok
                for ph in placeholders(tok):
                    literal = literal.replace("{" + ph + "}", str(subs.get(ph, "{" + ph + "}")))
                per_row.append(literal if literal in keys_here[ri] else None)
                if keys_here[ri].get(literal):
                    directories.add(tok)
            wrote_it = bool(per_row) and all(o is not None for o in per_row)
            if wrote_it:
                _produce(tok, ti)
                observed[(ti, tok)] = per_row
            elif _written_before(tok, ti):
                pass                          # consumed from an earlier command
            elif not steps_here:
                # no sealed step to observe: a redirect / -o target is the producer, else a consumer
                if re.search(r"(>\s*|-o\s+|--out\S*[= ]\s*)\{[A-Z0-9_]+\}/" + re.escape(tok), tmpl):
                    _produce(tok, ti)
                    observed[(ti, tok)] = []
                    notes.append(f"{tok}: producer inferred from the command text of how-to "
                                 f"command {ti + 1} (no sealed step to confirm it)")
                else:
                    head = tmpl if len(tmpl) <= 90 else tmpl[:87] + "..."
                    raise PipelineDerivationError(
                        "pipeline.orphan_artifact",
                        f"no sealed step matched how-to command {ti + 1} (`{head}`), so what it writes "
                        f"was never observed; it names `{tok}` inside an output slot, no earlier command "
                        f"produces `{tok}`, and the command text does not show it as an output target "
                        f"(`> {{SLOT}}/{tok}`, `-o {{SLOT}}/{tok}` or `--out…={{SLOT}}/{tok}`)",
                        "make the how-to command the literal command a sealed step ran, with placeholders "
                        "where the trial values were, so the seal's record of that step's outputs confirms "
                        "the artifact; or write the output through `>`, `-o` or `--out…` so the producer "
                        "is read off the command text; or, if an earlier command writes it, name it after "
                        "that command")
            else:
                if _sidecar_of(tok, history) is not None:
                    continue                  # an index beside an artifact an earlier command wrote
                raise PipelineDerivationError(
                    "pipeline.orphan_artifact",
                    f"how-to command {ti + 1} names `{tok}` inside an output slot, but the sealed "
                    f"run never observed it written and no earlier command produces it",
                    "write every intermediate file through the output slot in the command that "
                    "creates it, so the next command's input has a producer")

        # Files the sealed run OBSERVED this command writing that the command text never
        # names (`samtools index x.bam` writes x.bam.bai, `fastqc` writes <reads>_fastqc.html):
        # outputs all the same, keyed by their templated basename when every row agrees on it.
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
                for obs_name, is_dir in keys_here[ri].items():
                    if obs_name in named_literals.get(ri, set()):
                        continue
                    key = (_input_stem_glob(obs_name, subs, path_per_sample, value_per_sample)
                           or _templatize(obs_name, subs, value_per_sample))
                    per_row_extra.setdefault(key, [None] * len(steps_here))
                    per_row_extra[key][[r for r, _ in steps_here].index(ri)] = obs_name
                    if is_dir:
                        directories.add(key)
            partial = sorted(n for per_row in per_row_extra.values() if not all(per_row) for n in per_row if n)
            if partial:
                notes.append(f"command {ti + 1} wrote {partial[:4]}{' …' if len(partial) > 4 else ''}, which "
                             f"not every row wrote under one name: not carried to a later stage, not published")
            for key, per_row in per_row_extra.items():
                if not all(per_row):
                    continue                       # not every row wrote it: not an artifact
                _produce(key, ti)
                observed[(ti, key)] = per_row
                if key not in template_outputs[ti]:
                    template_outputs[ti].append(key)

    def _producer_before(tok: str, i: int) -> Optional[int]:
        """The template that last wrote `tok` before command i, else None."""
        return max((p for p in history.get(tok, []) if p < i), default=None)

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
            m = re.search(re.escape(tool) + r"\s+" + SLOT_RE.pattern, templates[g[0]])
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
            # a copy only when the version rewritten came from ANOTHER stage: a stage that
            # rewrites what its own earlier command wrote consumed nothing to protect
            stage_in_copy=any(_producer_before(tok, i) not in g for i in g for tok in rewrites[i]),
            resources=res))

    # the stage that looks a resolved value up: the last writer of its file
    resolver_of: dict[str, int] = {}                 # placeholder -> index of the stage that writes its file
    for ph, art in resolved_set.items():
        if art not in history:
            raise PipelineDerivationError(
                "pipeline.resolve_unknown_artifact",
                f"resolve= reads {ph} from {art!r}, which no command writes under the output slot "
                f"(written: {sorted(history)})",
                "write the value with a command of the how-to, `> {OUTPUT_DIR}/<name>`, and re-seal")
        resolver_of[ph] = stage_of_template[history[art][-1]]
    resolver_stages = set(resolver_of.values())

    def _from_resolver(tok: str, ti: int) -> bool:
        src = _producer_before(tok, ti)
        return src is not None and stage_of_template[src] in resolver_stages

    # ── wire inputs / outputs at stage level ───────────────────────────────
    # each declared pattern, and its reading with the slots kept (for an artifact no step observed)
    declared_forms = [(str(f), output_pattern(str(f), {})) for o in declared_outputs for f in (o.get("files") or [])]

    # inputs: an artifact comes from the stage that last wrote it before the consuming
    # command — the latest version, when a stage in between rewrote it
    edges: list[tuple[str, int, int]] = []         # (artifact, consuming template, producing template)
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
                          "collected": "collect", "resolved": "resolve"}.get(p.kind, "param")
                if p.kind == "resolved" and st.index <= resolver_of[ph]:
                    raise PipelineDerivationError(
                        "pipeline.resolve_used_before",
                        f"{ph} is looked up by {stage_recs[resolver_of[ph]].name} (it writes {resolved_set[ph]}), "
                        f"but {st.name} uses it no later than that",
                        "look the value up in a command before every command that uses it")
                st.inputs.append(StageInput(
                    name=ph, origin=origin,
                    from_stage=stage_recs[resolver_of[ph]].name if p.kind == "resolved" else None,
                    artifact=(collected_set.get(ph) if p.kind == "collected"
                              else resolved_set.get(ph) if p.kind == "resolved" else None)))
            wanted = list(template_tokens[i])
            if template_bare[i]:
                # the command names the output directory itself: in the self-test it ran in
                # the one directory every earlier command wrote into, so everything written
                # before it is staged in — except what a stage that looks a value up wrote:
                # that stage hands on the value (in the row's meta), never files
                wanted += [tok for tok in history if tok not in wanted and not _from_resolver(tok, i)]
            for tok in wanted:
                src = _producer_before(tok, i)
                if src is None or stage_of_template[src] == st.index:
                    continue
                edges.append((tok, i, src))
                if tok in seen_in:
                    continue
                seen_in.add(tok)
                st.inputs.append(StageInput(name=tok, origin="stage",
                                            from_stage=stage_recs[stage_of_template[src]].name, artifact=tok))
                # a consumer of X also receives X's sidecars produced before it
                for other in history:
                    if other == tok or other in seen_in or _sidecar_of(other, {tok: src}) != tok:
                        continue
                    osrc = _producer_before(other, i)
                    if osrc is None or stage_of_template[osrc] == st.index:
                        continue
                    seen_in.add(other)
                    edges.append((other, i, osrc))
                    st.inputs.append(StageInput(name=other, origin="stage",
                                                from_stage=stage_recs[stage_of_template[osrc]].name,
                                                artifact=other))

    # outputs: every artifact a stage's commands wrote, declared once, as its last command
    # in the stage left it
    pending: list[tuple[PipelineStage, dict]] = []
    for st in stage_recs:
        out_toks: list[str] = []
        for i in st.templates:
            for tok in template_outputs[i]:
                if i in history.get(tok, []) and tok not in out_toks:
                    out_toks.append(tok)
        for tok in out_toks:
            # The artifact is the token: as the command names it, or as every row's
            # observed file templated to one name.
            last_i = max(i for i in st.templates if i in history[tok])
            per_row = observed.get((last_i, tok), [])
            obs0 = per_row[0] if per_row else None
            art = tok
            is_dir = tok in directories
            # declared when a pattern, read as the self-test reads it, names one of the files
            # the first row's step wrote as this artifact — or, unobserved, the artifact itself
            ri0, step0 = (matched.get(last_i) or [(0, None)])[0]
            files = (_observed_files(step0, wrote_into.get(int(step0.get("step", 0)), []), obs0)
                     if step0 is not None and obs0 else [])
            subs0 = rows[ri0].get("substitutions") or {}
            pattern = next((glob for glob, kept in declared_forms
                            if (any(output_matches(f, output_pattern(glob, subs0)) for f in files) if files
                                else output_matches(art, kept) or kept.startswith(art + "/"))), None)
            consumed = sorted({stage_recs[stage_of_template[c]].name for a, c, p in edges
                               if a == tok and p in st.templates and stage_of_template[c] != st.index})
            pending.append((st, dict(artifact=art, observed=obs0, directory=is_dir,
                                     consumed_by=consumed, declared_pattern=pattern)))
    # results/ gets the declared outputs and their sidecars (`x.bam.bai`, `x.bai` with
    # `x.bam`); a how-to that declares no output the run wrote names no result, so all
    declared = dict.fromkeys(kw["artifact"] for _st, kw in pending if kw["declared_pattern"])
    for st, kw in pending:
        st.outputs.append(StageOutput(**kw, published=not declared or kw["declared_pattern"] is not None
                                      or _sidecar_of(kw["artifact"], declared) is not None))
    for glob in sorted({g for g, _ in declared_forms} - {kw["declared_pattern"] for _st, kw in pending}):
        notes.append(f"usage.outputs pattern {glob!r} names no output a stage writes; "
                     + ("every output is published" if pending and not declared
                        else "nothing is published for it"))

    # ── a value looked up at run time: the stage that writes it names the sample ──
    resolved_recs: list[ResolvedValue] = []
    for ph, art in resolved_set.items():
        producer = stage_recs[resolver_of[ph]]
        if producer.scope != "per_sample":
            raise PipelineDerivationError(
                "pipeline.resolve_cohort_stage",
                f"{art} is written by {producer.name}, a cohort stage; a sample's name is looked up per row",
                "look the value up in a per-sample stage")
        mine = next(o for o in producer.outputs if o.artifact == art)
        if mine.directory:
            raise PipelineDerivationError(
                "pipeline.resolve_directory",
                f"{art} is a directory {producer.name} writes a tree into; the value is a file's first line",
                "write the value to one file under the output slot")
        consumed = sorted(o.artifact for o in producer.outputs if o.consumed_by)
        if consumed:
            raise PipelineDerivationError(
                "pipeline.resolve_stage_consumed",
                f"{producer.name} looks {ph} up, and a later stage consumes what it writes ({consumed}); the "
                f"stage that names the sample hands on the value, not files",
                "move what later commands read into the command after it, or look the value up in a command of its own")
        index_of = {s.name: s.index for s in stage_recs}
        for st in stage_recs:
            before = sorted({i.from_stage for i in st.inputs if i.origin == "stage" and i.from_stage
                             and index_of[i.from_stage] < producer.index})
            if st.index > producer.index and before:
                raise PipelineDerivationError(
                    "pipeline.resolve_join",
                    f"{st.name} runs under the name {producer.name} looks up, but consumes files from {before}, "
                    f"which ran before it under the row's own name; the two cannot be joined",
                    "look the value up in the first command, or move the consumed command after it")
        for o in producer.outputs:
            o.published = False
        notes.append(f"{producer.name} publishes nothing: {art} names the sample (resolve=); it runs on every launch")
        resolved_recs.append(ResolvedValue(placeholder=ph, artifact=art, stage=producer.name))

    # ── samplesheet: always — `sample` (the row key) first, one row per trial ──
    # The key's placeholder is the how-to's own sample identifier when it has one, so
    # the renderer binds that placeholder to the `sample` column; a how-to with no
    # identifier still gets the column (the trial's name), because every task is
    # tagged by it and every result directory named after it.
    # One identifier is the row key (SAMPLE when the how-to has it, so no other column
    # can be named `sample`); any other per-sample identifier is a column of its own.
    ids = [p.name for p in params if p.kind == "per_sample" and p.name in _SAMPLE_NAMES]
    id_ph = "SAMPLE" if "SAMPLE" in ids else (ids[0] if ids else None)
    cols = [SamplesheetColumn(name="sample", placeholder=id_ph or "SAMPLE", value_kind="value",
                              format=None,
                              description="the row key: it tags every task and names results/<sample>/",
                              read_by=[], index_files=[])]
    for p in params:
        if p.kind != "per_sample" or p.name == id_ph:
            continue
        cols.append(SamplesheetColumn(name=p.name.lower(), placeholder=p.name,
                                      value_kind=p.value_kind, format=p.format,
                                      description=p.description, read_by=[], index_files=p.index_files))
    srows: list[dict[str, str]] = []
    for r in rows:
        subs = r.get("substitutions") or {}
        row = {"sample": str(subs.get(id_ph)) if id_ph and subs.get(id_ph) else str(r.get("name"))}
        for c in cols[1:]:
            row[c.name] = str(subs.get(c.placeholder, ""))
        srows.append(row)
    sheet = Samplesheet(columns=cols, rows=srows, rows_from=row_source)
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
        PipelineDefault(key="publish", value="what usage.outputs declares and its sidecars (every artifact when it "
                        "declares none), into the row's directory, by copy (publish_dir_mode)", source="default"),
        PipelineDefault(key="resume", value="on: the launch line carries -resume; drop it for a fresh run", source="default"),
        PipelineDefault(key="errors", value="finish: a failure submits nothing new and in-flight tasks complete; "
                        "`-resume` re-runs what failed; no retries", source="default"),
        PipelineDefault(key="cache", value="lenient on the cluster, standard locally", source="default"),
        PipelineDefault(key="queue_size", value=f"{NEXTFLOW_QUEUE_SIZE} jobs queued or running at once on the cluster",
                        source="default"),
        PipelineDefault(key="submit_rate", value=f"{NEXTFLOW_SUBMIT_RATE}: at most 100 SLURM submissions a minute",
                        source="default"),
        PipelineDefault(key="run_records", value="one directory per run, runs/<timestamp>/, never overwritten: "
                        "params.json (every param as resolved), samples.csv as read, trace.txt (every task), "
                        "report.html, timeline.html", source="default"),
        PipelineDefault(key="cleanup", value="never automatic; `nextflow clean -f` when you are done", source="default"),
        PipelineDefault(key="sheet_preflight", value="the samplesheet and every file column are checked as the run starts",
                        source="default"),
        PipelineDefault(key="resources", value="per-stage requests; measurements quoted, never used as the request",
                        source="caller" if resources else "default"),
    ]
    if resolved_recs:
        defaults.append(PipelineDefault(
            key="resolve", source="caller",
            value="; ".join(f"{r.placeholder}: the first line of {r.artifact}, written by {r.stage} on every launch, "
                            f"names the sample and fills the placeholder" for r in resolved_recs)))

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
        unmatched_steps=unmatched_steps, scripts=scripts, cohort_workflows=[], resolved=resolved_recs,
        defaults=defaults, notes=notes)
    for req in (cohort or []):
        _attach_cohort(record, req, resources=resources, env_names=env_names, sif_paths=sif_paths,
                       samplesheet_files=samplesheet_files, index_files=index_files)
    if _collected is None:
        _check_sheet_cells(record.samplesheet)
        unknown = sorted(set(resources or {}) - {s.name for s in record.stages})
        if unknown:
            raise PipelineDerivationError(
                "pipeline.resources_unknown_stage",
                f"resources= names {unknown}, which no stage of this pipeline is called",
                f"size the stages by their names: {[s.name for s in record.stages]}")
    return record


#: What Nextflow's `splitCsv(header: true)` reads as structure, or keeps as part of a
#: cell, in samples.csv: it does not unquote and does not trim.
SHEET_CELL_BREAKERS = re.compile(r'[,"\r\n]|^\s|\s$')


def _check_sheet_cells(sheet: Samplesheet) -> None:
    """Refuse a column name or worked-example cell samples.csv cannot carry as written."""
    for row in [{c.name: c.name for c in sheet.columns}, *sheet.rows]:
        for col, cell in row.items():
            if SHEET_CELL_BREAKERS.search(cell or ""):
                raise PipelineDerivationError(
                    "pipeline.sheet_cell_unwritable",
                    f"samples.csv column {col!r} would hold {cell!r}; a comma, quote, line break or "
                    f"leading/trailing space shifts or alters what Nextflow reads (it reads the "
                    f"sheet unquoted and untrimmed)",
                    "bind a value without them in the trials, or make the input a params.yaml "
                    "value (shared=) when every sample uses the same one")


# ── cohort stages: a second sealed workflow, attached ───────────────────────


def _attach_cohort(record: PipelineRecord, req: CohortRequest, *,
                   resources: Optional[Mapping[str, Mapping[str, Any]]],
                   env_names: Optional[Mapping[str, str]], sif_paths: Optional[Mapping[str, str]],
                   samplesheet_files: Optional[Mapping[str, str]],
                   index_files: Optional[Mapping[str, list[str]]]) -> None:
    """Attach `req.spec` as cohort stages of `record`, in place. The cohort how-to is
    derived like any other (its own sealed steps, images, measurements, scripts), then
    composed: every stage runs once over the cohort; each `collect` input is wired to
    the last per-sample stage that writes the artifact, every row's copy; a samplesheet
    slot reads the pipeline's own sheet; its params join params.yaml. Refuses, naming
    the remedy, on an undeclared fan-in, an artifact no per-sample stage writes, one
    not named after the sample (the copies would collide), or a placeholder both
    how-tos use."""
    spec = req.spec
    wf = str(spec.workflow_name)
    per_sample_stages = [s for s in record.stages if s.scope == "per_sample"]
    produced: dict[str, str] = {}                       # templated artifact -> its LAST per-sample writer
    for s in per_sample_stages:                         # in execution order: a later stage's copy wins
        for o in s.outputs:
            produced[o.artifact] = s.name
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
        samplesheet_files=samplesheet_files, index_files=index_files, _collected=req.collect)
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
        if nm != s.name and ({s.name, nm} & set(resources or {})):
            raise PipelineDerivationError(
                "pipeline.resources_ambiguous",
                f"stage {s.name} names a stage of both {record.sealed_workflow} and {wf}, and the "
                f"cohort's is renamed {nm}, so resources= cannot size either by those names",
                f"name the cohort stages apart with its stage_names=, then size each by its name")
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


def record_identity(text: str) -> Optional[tuple[str, str]]:
    """The (name, sealed_workflow) a pipeline record's text names, read without
    validating the whole record, so a run directory rendered by an earlier version of
    this system still says which pipeline it holds; None when the text is not a record
    naming both."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    name, sealed = raw.get("name"), raw.get("sealed_workflow")
    return (name, sealed) if isinstance(name, str) and isinstance(sealed, str) else None


def load_pipeline_record(path: Path) -> PipelineRecord:
    """THE typed reader for a pipeline record — a malformed file fails here, loudly."""
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"pipeline record at {path} is not a mapping")
    return PipelineRecord.model_validate(raw)


__all__ = [
    "PipelineDerivationError", "PipelineRecord", "PipelineStage", "PipelineParam",
    "StageInput", "StageOutput", "StageResources", "Samplesheet", "SamplesheetColumn", "LocalRuntime",
    "PipelineDefault", "ProvenanceStep", "PipelineScript", "CollectedInput", "CohortWorkflow", "CohortRequest",
    "derive_pipeline_record", "write_pipeline_record",
    "record_yaml", "load_pipeline_record", "placeholders", "artifact_tokens", "RECORD_FILENAME",
    "DEFAULT_STAGE_REQUEST", "MANAGER_JOB_REQUEST", "NEXTFLOW_QUEUE_SIZE", "NEXTFLOW_SUBMIT_RATE", "THREADS_FORMAT",
    "SAMPLESHEET_FORMAT",
    "SCRIPT_DIR", "render_samplesheet",
]
