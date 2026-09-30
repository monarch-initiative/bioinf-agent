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
    kind: Literal["shared", "per_sample"]
    value_kind: Literal["path", "prefix", "value"]   # prefix: names a FAMILY of files (an aligner index)
    default: Optional[str]                     # the sealed trial's value for a shared param; None for a per-sample one, whose values are the samplesheet's rows
    source: str                                # usage_input | literal | test_data:<key> | reference_database:<name> | sealed_step:<n>
    format: Optional[str]
    description: Optional[str]
    used_by: list[str]                         # stage names
    reason: str                                # why it was classified as it was


class StageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str                                  # a placeholder, or an artifact name
    origin: Literal["param", "column", "stage"]
    from_stage: Optional[str]
    artifact: Optional[str]                    # origin=stage: the artifact (templated basename or glob)


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
    requested_by: Literal["caller", "default"]
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
    placeholder: str
    value_kind: Literal["path", "prefix", "value"]
    format: Optional[str]
    description: Optional[str]


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
    tool: str
    command: str
    produces_param: str


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
                           local_runtime: Optional[LocalRuntime] = None) -> PipelineRecord:
    """Derive the pipeline record from a sealed WorkflowSpec. Raises
    PipelineDerivationError (a refusal with a remedy) when the seal cannot support
    the render; every derivation the caller did not dictate is stated in `notes`."""
    notes: list[str] = []
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
        kind, why = classify(ph)
        values = [str((r.get("substitutions") or {})[ph]) for r in rows]
        v0 = values[0]
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
    per_sample_names = [p.name for p in params if p.kind == "per_sample"]
    value_per_sample = [p.name for p in params if p.kind == "per_sample" and p.value_kind == "value"]

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
                provenance.append(ProvenanceStep(step=int(s["step"]), tool=str(s.get("tool") or ""),
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

    # scope per template: per_sample if it binds a per-sample placeholder or consumes a
    # per-sample artifact; else cohort (it runs once, over every row's work)
    template_scope: list[ScopeT] = []
    for ti, tmpl in enumerate(templates):
        phs = placeholders(tmpl)
        is_ps = any(p in per_sample_names for p in phs)
        if not is_ps:
            for tok in template_tokens[ti]:
                src = produced_by.get(tok)
                if src is not None and src < ti and template_scope[src] == "per_sample":
                    is_ps = True
                    break
        template_scope.append("per_sample" if is_ps else "cohort")

    stage_of_template: dict[int, int] = {}
    stage_recs: list[PipelineStage] = []
    used_names: set[str] = set()
    for gi, g in enumerate(groups):
        scopes = {template_scope[i] for i in g}
        if len(scopes) > 1:
            raise PipelineDerivationError(
                "pipeline.stage_mixes_scopes",
                f"stage {gi + 1} groups commands {g} of which some run per sample and some over "
                f"the whole cohort",
                "cut the stage where the data scope changes")
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
        base = (stage_names[gi] if stage_names and gi < len(stage_names) else _sanitize_name(tool))
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
        res = StageResources(
            cpus=int(req["cpus"]) if req.get("cpus") is not None else None,
            mem=str(req["mem"]) if req.get("mem") else None,
            time=str(req["time"]) if req.get("time") else None,
            gpus=int(req.get("gpus", gpus) or 0),
            requested_by="caller" if req else "default",
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
                st.inputs.append(StageInput(
                    name=ph, origin="column" if p.kind == "per_sample" else "param",
                    from_stage=None, artifact=None))
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
                              description="the row key: it tags every task and names results/<sample>/")]
    for p in params:
        if p.kind != "per_sample" or p.name in _SAMPLE_NAMES:
            continue
        cols.append(SamplesheetColumn(name=p.name.lower(), placeholder=p.name,
                                      value_kind=p.value_kind, format=p.format,
                                      description=p.description))
    srows: list[dict[str, str]] = []
    for r in rows:
        subs = r.get("substitutions") or {}
        row = {"sample": str(subs.get(id_ph)) if id_ph and subs.get(id_ph) else str(r.get("name"))}
        for c in cols[1:]:
            row[c.name] = str(subs.get(c.placeholder, ""))
        srows.append(row)
    sheet = Samplesheet(columns=cols, rows=srows)

    # ── the defaults table ─────────────────────────────────────────────────
    defaults = [
        PipelineDefault(key="stage_cut", value="explicit groups" if stages else "one stage per how-to command",
                        source="caller" if stages else "default"),
        PipelineDefault(key="publish", value="every artifact a stage writes, into the row's directory", source="default"),
        PipelineDefault(key="resume", value="on: the launch line carries -resume; drop it for a fresh run", source="default"),
        PipelineDefault(key="errors", value="terminate on first failure, no retries", source="default"),
        PipelineDefault(key="cache", value="lenient on the cluster, standard locally", source="default"),
        PipelineDefault(key="queue_size", value="50", source="default"),
        PipelineDefault(key="run_records", value="trace (each task's command) + report under runs/<timestamp>/; the launch line in `nextflow log`", source="default"),
        PipelineDefault(key="cleanup", value="never automatic; `nextflow clean -f` when you are done", source="default"),
        PipelineDefault(key="sheet_preflight", value="the samplesheet and every file column are checked as the run starts",
                        source="default"),
        PipelineDefault(key="resources", value="per-stage requests; measurements quoted, never used as the request",
                        source="caller" if resources else "default"),
    ]

    return PipelineRecord(
        name=name, version="1",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        sealed_workflow=str(spec.workflow_name), sealed_workflow_path=spec_path,
        sealed_workflow_sha256=spec_sha256,
        env_digests=sorted(env_map) or [str(getattr(spec, "env_content_digest", ""))],
        params=params, samplesheet=sheet,
        output_slots=sorted(out_slots), compute_env=compute_env, modules=list(modules or []),
        local_runtime=local_runtime,
        stages=stage_recs, provenance_steps=provenance,
        unmatched_steps=unmatched_steps, defaults=defaults, notes=notes)


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
    "PipelineDefault", "ProvenanceStep", "derive_pipeline_record", "write_pipeline_record",
    "record_yaml", "load_pipeline_record", "placeholders", "artifact_tokens", "RECORD_FILENAME",
    "DEFAULT_STAGE_REQUEST", "MANAGER_JOB_REQUEST", "NEXTFLOW_QUEUE_SIZE", "render_samplesheet",
]
