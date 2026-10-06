"""
pipeline_render_nextflow — the NEXTFLOW form of a pipeline record.

One pure function, `render_nextflow(record, env=None)`, turns a `PipelineRecord`
(derived from a sealed workflow by `pipeline_record`) into the files of a Nextflow
DSL2 pipeline: strings in, `{relative path: content}` out, nothing read from disk and
nothing run. A refusal is a `ValueError` that names the remedy.

The file set
------------
  main.nf            one INLINE process per stage, in stage order, wired in `workflow {}`,
                     which opens by writing runs/<stamp>/params.json (every param as
                     resolved) and copying the samplesheet beside it, before any task runs
  nextflow.config    `local` (docker, local executor) and `slurm` (apptainer, SLURM
                     executor) profiles, each naming the image it runs — the docker tag
                     locally, the .sif on the cluster; per-stage resources; trace, report
                     and timeline under runs/<stamp>/, one directory per run
  params.yaml        the shared parameters with the sealed run's defaults, the
                     samplesheet and the output directory — nothing about WHERE the
                     pipeline runs; that is nextflow.config's
  samples.csv        `pipeline_record.render_samplesheet`, the ONE rendering; a CSV
                     cannot carry the leading comment the other files do (splitCsv
                     would read it as the header)
  launcher.sh        the manager job: `sbatch launcher.sh` on the cluster. On a laptop the
                     run is one line, `RUN_LOCAL`, which pipeline.html shows

The binding table — how a how-to placeholder reaches a process script
---------------------------------------------------------------------
  shared value param         {STRANDED}      ->  ${params.stranded}
  shared path param          {GTF}           ->  ${gtf}          staged: `path gtf` <- file(params.gtf)
  shared prefix param        {HISAT2_INDEX}  ->  ${file(params.hisat2_index).name}
                                                 staged: `path hisat2_index_files`
                                                         <- files("${params.hisat2_index}*")
  per-sample path column     {READS}         ->  ${reads}        `path(reads)` inside the meta tuple
  per-sample value column    {SAMPLE}        ->  ${meta.sample}
  thread slot                {THREADS}       ->  ${task.cpus}    the stage's cpus request
                                                 (format: threads; the sealed count unless sized)
  artifact in an output slot {OUT}/x.bam     ->  x.bam           (placeholders inside bind as above)
  bare output slot           {OUT}           ->  .
  `bound_commands` is that table applied to a stage — the page's command column, so
  the page shows the line main.nf runs and never a paraphrase of it.

Channels
--------
`rows` emits one `tuple(meta, <path columns>...)` per samples.csv row (bare `meta`
when the sheet has no path column); `meta` holds `sample` plus every value column. A
stage consuming artifacts from prior stages receives their `tuple(meta, path)` output
channels `.join()`ed on meta; one that also consumes a path column joins `rows` last
(projected to the columns it uses); one consuming only columns takes `rows`; one
consuming only the row identity takes `val(meta)`.

What is refused: a `$`, a backslash or a `\"\"\"` in a sealed command (the script
block is a Groovy triple-quoted string, which rewrites all three in transit); a cohort
stage; a stage, param or artifact name that is not a safe token; a stage that names no
image.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

import yaml

from agent.skills import compute_access
from agent.skills.pipeline_record import (DEFAULT_STAGE_REQUEST, MANAGER_JOB_REQUEST,
                                          NEXTFLOW_QUEUE_SIZE, PipelineParam,
                                          PipelineRecord, PipelineStage, placeholders,
                                          render_samplesheet)
from agent.skills.submit_workflow import _resolve_slurm_and_email
from agent.skills.workflow_render import (_GPU_PLACEMENT_HEADER_NOTE, _MEM_RE, _TIME_RE,
                                          _check_email, _check_module, _check_safe_token,
                                          _check_slurm, _nf_quote, _render_sbatch_header)

#: A Groovy identifier — a process name, a params key, an emit name, a staged-input name.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: An artifact as the record names it: a bare filename, a glob, or a templated name.
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9_.\-*?{}]+$")
#: A default that renders as a Groovy/YAML number and still reads back as the same
#: text. Integers only: a leading zero is an octal literal in Groovy and a decimal
#: loses its trailing zeros through YAML.
_INT_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_NOT_EXPRESSIBLE = ("which a Nextflow script block rewrites in transit; re-seal the how-to with "
                    "a command free of it")
_INDENT = "    "

STRICT_MODE_LINE = ("set -euo pipefail   # bash strict mode: stop at the first failing command, "
                    "an unset variable, or a failure inside a pipe")
_RUN_LINE = "nextflow run main.nf -profile {profile} -params-file params.yaml -resume"
#: How a run starts at each locus — the ONE spelling the launcher and the page share.
RUN_LOCAL = _RUN_LINE.format(profile="local")
RUN_HPC = "sbatch launcher.sh"
#: What a run leaves under runs/<stamp>/: each file, what it holds, and which rendered
#: file DEFINES it — params.yaml for the two the workflow writes as it opens (every param
#: as resolved, the samplesheet as read), nextflow.config for the three observers. The
#: workflow block, the config, the launcher and the page name these five from here.
RUN_RECORDS = (
    ("params.json", "every param as resolved; re-runs as `-params-file`", "params.yaml"),
    ("samples.csv", "a copy of the samplesheet as read", "params.yaml"),
    ("trace.txt", "every task: status, when, how long, cpu and memory, work dir, command", "nextflow.config"),
    ("report.html", "Nextflow's run report", "nextflow.config"),
    ("timeline.html", "Nextflow's timeline", "nextflow.config"),
)
RUN_RECORD_FILES = tuple(name for name, _, _ in RUN_RECORDS)
#: The trace's columns: which task, which SLURM job, how it ended, when, how long, what it
#: cost, where it ran, and the command it ran.
TRACE_FIELDS = ("task_id,native_id,name,status,exit,submit,start,complete,realtime,%cpu,peak_rss,"
                "container,workdir,script")
#: THE RUN RECORDS — the standard of every rendered pipeline: two blocks, verbatim. The
#: config names the run (one stamp, a params entry because the strict config parser allows
#: no variables) and points Nextflow's three observers into runs/<stamp>/; the workflow
#: opens by writing the two files only the script can write at start — every param as
#: resolved and the samplesheet as read. One name, params.run_stamp, joins them, and
#: nothing else about a run's records lives anywhere else.
RUN_RECORD_CONFIG = f"""\
// One directory per run, runs/<stamp>/, named by its launch time and never overwritten:
// trace.txt (every task: status, when, how long, resources, work dir, command), then
// report.html and timeline.html at the end; main.nf adds params.json and the samplesheet
// as read, before any task runs.
// (A params entry rather than a variable: the strict config parser allows no
// declarations beside config statements.)
params.run_stamp = new java.util.Date().format('yyyyMMdd_HHmmss')
trace {{
    enabled = true
    file = "runs/${{params.run_stamp}}/trace.txt"
    fields = '{TRACE_FIELDS}'
}}
report {{
    enabled = true
    file = "runs/${{params.run_stamp}}/report.html"
}}
timeline {{
    enabled = true
    file = "runs/${{params.run_stamp}}/timeline.html"
}}"""
#: The block's log line ends in a newline so Nextflow's own first banner line stays off it.
RUN_RECORD_BLOCK = """\
    // runs/<stamp>/: every param as resolved and the samplesheet as read, written before
    // any task runs; nextflow.config writes trace.txt, report.html and timeline.html there.
    // The stamp is left out of params.json so the file re-runs as -params-file.
    run_dir = file("runs/${params.run_stamp}")
    run_dir.mkdirs()
    file(params.samplesheet, checkIfExists: true).copyTo(run_dir.resolve('samples.csv'))
    run_dir.resolve('params.json').text = groovy.json.JsonOutput.prettyPrint(groovy.json.JsonOutput.toJson(params.findAll { k, v -> k != 'run_stamp' }))
    log.info "run records: runs/${params.run_stamp}/\\n"
"""
#: The two params every pipeline carries beside its own: (key, value) as params.yaml
#: and main.nf spell them. The page reads the same pair.
SAMPLESHEET_PARAM = ("samplesheet", "samples.csv")
OUTDIR_PARAM = ("outdir", "results")


# ── refusals ────────────────────────────────────────────────────────────────


def _check_command_expressible(stage: PipelineStage, command: str) -> None:
    """The script block is a Groovy triple-double-quoted string: `$` interpolates,
    a backslash starts an escape, `\"\"\"` ends the block. Each would reach the
    compute node as a different command from the sealed one, so all three refuse."""
    for needle, what in (('"""', 'a `"""`'), ("\\", "a backslash"), ("$", "a `$`")):
        if needle in command:
            raise ValueError(
                f"stage {stage.name}: the sealed command {command!r} contains {what}, "
                f"{_NOT_EXPRESSIBLE}")


def _check_identifier(label: str, value: str, remedy: str) -> None:
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise ValueError(f"{label} {value!r} is not a Groovy identifier (letters, digits, "
                         f"`_`, not starting with a digit); {remedy}")


def _check_artifact(stage: PipelineStage, artifact: str, known: set[str]) -> None:
    if not isinstance(artifact, str) or not artifact or not _ARTIFACT_RE.match(artifact) \
            or ".." in artifact.split("/"):
        raise ValueError(
            f"stage {stage.name}: artifact {artifact!r} is not a bare filename or glob "
            f"(letters, digits, `_.-*?` and placeholders); write the how-to's output as "
            f"<output slot>/<filename> and re-derive the record")
    unknown = [p for p in placeholders(artifact) if p not in known]
    if unknown:
        raise ValueError(
            f"stage {stage.name}: artifact {artifact!r} names placeholder(s) {unknown} that "
            f"are not parameters of the record; re-derive the record from the sealed workflow")


def _check_record(record: PipelineRecord) -> None:
    _check_safe_token("record.name", record.name)
    if len(record.name) > 64:
        raise ValueError(f"record.name length {len(record.name)} > 64 (it is the SLURM "
                         f"job name); derive the record with a shorter name=")
    if not record.stages:
        raise ValueError("the record has no stages; re-derive it from a sealed workflow "
                         "whose how-to has at least one command")
    known = {p.name for p in record.params}
    for p in record.params:
        _check_identifier(f"param {p.name}: params key", p.name.lower(),
                          "rename the placeholder in the how-to and re-seal")
        if p.default is not None:
            try:
                _nf_quote(p.default)
            except ValueError:
                raise ValueError(
                    f"param {p.name}: default {p.default!r} contains a single quote or a "
                    f"newline and cannot be written into main.nf; re-seal with a value "
                    f"free of both") from None
    for s in record.stages:
        _check_identifier(f"stage {s.name}: process name", s.name,
                          "pass stage_names= to derive_pipeline_record")
        if s.scope == "cohort":
            raise ValueError(
                f"stage {s.name}: cohort stages are not supported yet; cut the how-to so every "
                f"command runs per sample")
        if not (s.image or s.image_digest):
            raise ValueError(
                f"stage {s.name} names no image; re-derive the record from a sealed workflow "
                f"whose steps ran in a container")
        for c in s.commands:
            _check_command_expressible(s, c)
        for i in s.inputs:
            if i.origin == "stage":
                _check_artifact(s, i.artifact or i.name, known)
        for o in s.outputs:
            _check_artifact(s, o.artifact, known)


# ── the render context ─────────────────────────────────────────────────────


def _slug(artifact: str) -> str:
    """An emit name: the artifact's literal parts as an identifier —
    `{SAMPLE}.counts.tsv` -> counts_tsv, `aligned.bam.bai` -> aligned_bam_bai."""
    s = _PLACEHOLDER_RE.sub("", artifact)
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or s[0].isdigit():
        s = "out_" + s
    return s


def _nf_memory(mem: str) -> str:
    """SLURM `32G` / `12000M` -> Nextflow `'32 GB'` / `'12000 MB'`."""
    if not isinstance(mem, str) or not _MEM_RE.match(mem):
        raise ValueError(f"mem={mem!r} must look like 6G / 45g / 12000M; size the stage "
                         f"with resources={{<stage>: {{mem: '<N>G'}}}}")
    return f"'{mem[:-1]} {mem[-1].upper()}B'"


def _nf_time(t: str) -> str:
    """SLURM `HH:MM:SS` / `D-HH:MM:SS` / `D-` -> Nextflow's unit form (`'4h'`,
    `'1d 12h 30m'`). The colon form is SLURM's, not Nextflow's, so it is converted
    rather than quoted through."""
    if not isinstance(t, str) or not _TIME_RE.match(t):
        raise ValueError(f"time={t!r} must look like HH:MM:SS, D-HH:MM:SS, or D-; size the "
                         f"stage with resources={{<stage>: {{time: 'HH:MM:SS'}}}}")
    days, hms = (t.split("-", 1) + [""])[:2] if "-" in t else ("", t)
    h, m, s = (int(x) for x in hms.split(":")) if hms else (0, 0, 0)
    parts = [f"{int(days)}d" if days and int(days) else "", f"{h}h" if h else "",
             f"{m}m" if m else "", f"{s}s" if s else ""]
    return "'" + (" ".join(p for p in parts if p) or "0s") + "'"


class _Context:
    """Everything the five files share: the images (docker tag and .sif, per stage),
    the env's SLURM policy merged for the manager job, the module names, the emit
    slugs."""

    def __init__(self, record: PipelineRecord, env: Mapping):
        self.record = record
        self.env = dict(env)
        self.header = f"rendered from sealed workflow {record.sealed_workflow} " \
                      f"(pipeline {record.name}) — edit params.yaml / samples.csv, not this file"
        # images, first-appearance order, keyed by digest (falling back to the ref)
        self.images: list[dict] = []
        self.image_of: dict[str, dict] = {}        # stage name -> its image
        for s in record.stages:
            key = s.image_digest or s.image
            hit = next((im for im in self.images if im["key"] == key), None)
            if hit is None:
                hit = {"key": key, "ref": s.image or s.image_digest, "digest": s.image_digest,
                       "sif_path": s.sif_path}
                self.images.append(hit)
            self.image_of[s.name] = hit
        # the env's SLURM policy, merged the way every job header is
        merged, email, _ = _resolve_slurm_and_email(dict(MANAGER_JOB_REQUEST), self.env)
        self.manager_slurm = _check_slurm(merged)
        self.email = _check_email(email or "")
        self.account = self.manager_slurm.get("account")
        self.cpu_partition = self.manager_slurm.get("partition")
        mods = compute_access.get_container_modules(self.env) if env else {}
        for k, v in mods.items():
            _check_module(k, v)
        self.apptainer_module = mods.get("apptainer_module")
        self.nextflow_module = mods.get("nextflow_module")
        # emit slugs, unique within a stage
        self.slugs: dict[tuple[str, str], str] = {}
        for s in record.stages:
            seen: set[str] = set()
            for o in s.outputs:
                base = _slug(o.artifact)
                slug, k = base, 2
                while slug in seen:
                    slug, k = f"{base}_{k}", k + 1
                seen.add(slug)
                self.slugs[(s.name, o.artifact)] = slug
        # the samplesheet's columns by placeholder
        cols = record.samplesheet.columns
        self.col_of = {c.placeholder: c.name for c in cols}
        self.path_cols = [c for c in cols if c.value_kind in ("path", "prefix")]
        self.value_cols = [c for c in cols if c.value_kind == "value" and c.name != "sample"]

    def param(self, name: str) -> PipelineParam:
        return self.record.param(name)

    # ── the binding table ──────────────────────────────────────────────────

    def binding(self, ph: str) -> str:
        """The Groovy expression a placeholder becomes inside a script block."""
        p = self.param(ph)
        low = p.name.lower()
        if p.kind == "cpus":
            return "${task.cpus}"
        if p.kind == "per_sample":
            if p.value_kind == "value":
                return "${meta." + self.col_of.get(ph, "sample") + "}"
            return "${" + self.col_of.get(ph, low) + "}"
        if p.value_kind == "value":
            return "${params." + low + "}"
        if p.value_kind == "prefix":
            return "${file(params." + low + ").name}"
        return "${" + low + "}"

    def bind(self, stage: PipelineStage, text: str) -> str:
        """Bind every placeholder in a sealed command: output slots first (`{OUT}/x`
        -> `x`, bare `{OUT}` -> `.`), then the parameters."""
        out = text
        for slot in self.record.output_slots:
            out = out.replace("{" + slot + "}/", "").replace("{" + slot + "}", ".")

        def sub(m: re.Match) -> str:
            try:
                return self.binding(m.group(1))
            except KeyError:
                raise ValueError(
                    f"stage {stage.name}: placeholder {{{m.group(1)}}} is not a parameter "
                    f"of the record; re-derive the record from the sealed workflow") from None
        return _PLACEHOLDER_RE.sub(sub, out)

    def artifact_expr(self, stage: PipelineStage, artifact: str) -> str:
        """A `path(...)` argument for an artifact: single-quoted when literal,
        double-quoted when a per-sample placeholder is interpolated."""
        bound = self.bind(stage, artifact)
        return f"'{bound}'" if "$" not in bound else f'"{bound}"'


# ── main.nf ────────────────────────────────────────────────────────────────


def _describe_source(record: PipelineRecord, p: PipelineParam) -> str:
    src = p.source
    if src.startswith("sealed_step:"):
        n = src.split(":", 1)[1]
        prov = next((ps for ps in record.provenance_steps if str(ps.step) == n), None)
        return f"produced by sealed step {n}" + (f" ({prov.tool})" if prov and prov.tool else "")
    if src.startswith("reference_database:"):
        return f"reference database {src.split(':', 1)[1]}"
    if src.startswith("test_data:"):
        return f"the seal's test data ({src.split(':', 1)[1]})"
    return {"usage_input": "a how-to input", "literal": "a literal"}.get(src, src)


def _param_comment(record: PipelineRecord, p: PipelineParam) -> str:
    kind = {"path": "a path", "prefix": "a prefix naming a family of files",
            "value": "a value"}[p.value_kind]
    bits = [f"{p.name} — {kind}", _describe_source(record, p)]
    if p.format:
        bits.append(f"format {p.format}")
    if p.description:
        bits.append(p.description)
    if p.used_by:
        bits.append("used by " + ", ".join(p.used_by))
    return "; ".join(bits)


def _param_default_expr(p: PipelineParam) -> str:
    if p.default is None:
        return "null"
    if _INT_RE.match(p.default):
        return p.default
    return _nf_quote(p.default)


def _render_params_block(record: PipelineRecord, ctx: _Context) -> str:
    lines: list[str] = []
    for p in record.params:
        if p.kind != "shared":
            continue                                  # a samplesheet column or a thread slot, not a param
        lines.append(f"// {_param_comment(record, p)}.")
        lines.append("// The default is what the sealed run was validated with.")
        lines.append(f"params.{p.name.lower()} = {_param_default_expr(p)}")
    cols = ", ".join(c.name for c in record.samplesheet.columns)
    lines.append(f"// The samplesheet: one row per sample, columns {cols}.")
    lines.append(f"params.{SAMPLESHEET_PARAM[0]} = {_nf_quote(SAMPLESHEET_PARAM[1])}")
    lines.append("// Where published outputs land, one directory per sample, shared by every stage.")
    lines.append(f"params.{OUTDIR_PARAM[0]} = {_nf_quote(OUTDIR_PARAM[1])}")
    return "\n".join(lines)


def _measured_comment(stage: PipelineStage) -> Optional[str]:
    r = stage.resources
    if r.measured_wall_seconds is None and r.measured_peak_rss_mb is None:
        return None
    bits = []
    if r.measured_wall_seconds is not None:
        bits.append(f"wall {r.measured_wall_seconds:g} s")
    if r.measured_peak_rss_mb is not None:
        bits.append(f"peak RSS {r.measured_peak_rss_mb:g} MB")
    if r.measured_max_cpu_percent is not None:
        bits.append(f"max CPU {r.measured_max_cpu_percent:g}%")
    where = f" on {r.measured_on}" if r.measured_on else ""
    return (f"measured{where}: " + " · ".join(bits)
            + f" ({r.measured_authority}) — a measurement, never the request")


def _stage_inputs(stage: PipelineStage, ctx: _Context) -> dict[str, list]:
    """The stage's inputs sorted into the four plumbing classes, record order kept."""
    arts = [i for i in stage.inputs if i.origin == "stage"]
    names = {i.name for i in stage.inputs if i.origin in ("param", "column")}
    params = [ctx.param(i.name) for i in stage.inputs if i.origin in ("param", "column")]
    shared_paths = [p for p in params if p.kind == "shared" and p.value_kind == "path"]
    prefixes = [p for p in params if p.kind == "shared" and p.value_kind == "prefix"]
    path_cols = [c for c in ctx.path_cols if c.placeholder in names]
    return {"artifacts": arts, "shared_paths": shared_paths, "prefixes": prefixes,
            "path_cols": path_cols}


def _producer_slug(stage: PipelineStage, inp, ctx: _Context) -> str:
    art = inp.artifact or inp.name
    slug = ctx.slugs.get((inp.from_stage or "", art))
    if slug is None:
        raise ValueError(
            f"stage {stage.name} consumes {art!r} from stage {inp.from_stage!r}, which does "
            f"not produce it; re-derive the record from the sealed workflow")
    return slug


def _rows_expr(path_cols: list, ctx: _Context) -> str:
    """The rows channel as a stage consumes it: as-is when the stage uses every path
    column, projected to the ones it uses otherwise."""
    if [c.name for c in path_cols] == [c.name for c in ctx.path_cols]:
        return "rows"
    all_names = ", ".join(["meta"] + [c.name for c in ctx.path_cols])
    keep = ", ".join(["meta"] + [c.name for c in path_cols])
    return f"rows.map {{ {all_names} -> tuple({keep}) }}"


def _stage_call(stage: PipelineStage, ctx: _Context) -> tuple[str, Optional[str]]:
    """`STAGE(<args>)` and, when channels are joined, the comment explaining it."""
    io = _stage_inputs(stage, ctx)
    args: list[str] = []
    comment = None
    chans = [f"{i.from_stage}.out.{_producer_slug(stage, i, ctx)}" for i in io["artifacts"]]
    items = [[i.artifact or i.name] for i in io["artifacts"]]
    if io["path_cols"]:
        chans.append(_rows_expr(io["path_cols"], ctx))
        items.append([c.name for c in io["path_cols"]])
    if chans:
        primary = chans[0] + "".join(f".join({c})" for c in chans[1:])
        if len(chans) > 1:
            shapes = ["(meta, " + ", ".join(it) + ")" for it in items]
            joined = ", ".join(x for it in items for x in it)
            comment = ".join() on meta: " + " + ".join(shapes) + f" -> (meta, {joined})"
    elif ctx.path_cols:
        primary = "rows.map { it[0] }"
    else:
        primary = "rows"
    args.append(primary)
    args += [f"file(params.{p.name.lower()})" for p in io["shared_paths"]]
    args += ['files("${params.' + p.name.lower() + '}*")' for p in io["prefixes"]]
    return f"{stage.name}({', '.join(args)})", comment


def _render_process(record: PipelineRecord, stage: PipelineStage, ctx: _Context) -> str:
    io = _stage_inputs(stage, ctx)
    n = len(record.stages)
    steps = ", ".join(str(s) for s in stage.sealed_steps) if stage.sealed_steps else "none"
    head = [f"// stage {stage.index + 1} of {n} · {stage.tool} · image "
            f"{stage.image_digest or stage.image} · derived from sealed step(s) [{steps}]"]
    measured = _measured_comment(stage)
    if measured:
        head.append(f"// {measured}")
    if stage.consumes_workdir:
        head.append("// This stage names the output directory bare (`.` below); only the "
                    "artifacts declared as its inputs are staged into its work dir.")

    body: list[str] = []
    # Closures, not strings: a directive that names a task input must be evaluated
    # per task, and the strict parser refuses the string form outright.
    body.append("tag { meta.sample }")
    if stage.outputs:
        # One results directory per row, shared by every stage — the layout the sealed
        # how-to ran in. Every artifact a stage writes is published; names are unique
        # within a row by construction (the seal wrote them all into one working
        # directory). `overwrite: true` because Nextflow's default is false on -resume:
        # a stage re-executed after a parameter change must replace its stale
        # published copy.
        base = '{ "${params.outdir}/${meta.sample}" }'
        body.append(f"publishDir {base}, mode: 'copy', overwrite: true")
    if stage.stage_in_copy:
        body.append("stageInMode 'copy'                       // this stage rewrites an artifact it consumed")

    # inputs
    ins: list[str] = []
    parts = ["val(meta)"]
    parts += [f"path({ctx.artifact_expr(stage, i.artifact or i.name)})" for i in io["artifacts"]]
    parts += [f"path({c.name})" for c in io["path_cols"]]
    ins.append("tuple " + ", ".join(parts))
    ins += [f"path {p.name.lower()}" for p in io["shared_paths"]]
    ins += [f"path {p.name.lower()}_files" for p in io["prefixes"]]

    # outputs
    outs: list[str] = []
    for o in stage.outputs:
        slug = ctx.slugs[(stage.name, o.artifact)]
        expr = ctx.artifact_expr(stage, o.artifact)
        outs.append(f"tuple val(meta), path({expr}), emit: {slug}")

    script = [ctx.bind(stage, c) for c in stage.commands]

    lines = head + [f"process {stage.name} {{"]
    lines += [_INDENT + b for b in body]
    if body:
        lines.append("")
    if ins:
        lines += [_INDENT + "input:"] + [_INDENT + i for i in ins] + [""]
    if outs:
        lines += [_INDENT + "output:"] + [_INDENT + o for o in outs] + [""]
    lines += [_INDENT + "script:", _INDENT + '"""'] + [_INDENT + s for s in script] \
             + [_INDENT + '"""', "}"]
    return "\n".join(lines)


def _render_workflow_block(record: PipelineRecord, ctx: _Context) -> str:
    lines = ["workflow {"]
    # Refuse before any job is submitted when the slurm profile names no .sif: an empty
    # process.container would run every tool on the bare compute node instead, failing
    # one job at a time. Nextflow exposes the resolved container as workflow.container —
    # a string for one image, empty (falsy) when unset; with several images a map that
    # holds ONLY the processes whose container is set, so every stage is looked up by
    # name rather than the map's values scanned.
    digests = ", ".join(im["digest"] or im["ref"] for im in ctx.images)
    if len(ctx.images) == 1:
        empty = "!workflow.container"
    else:
        names = ", ".join(f"'{s.name}'" for s in record.stages)
        empty = (f"!(workflow.container instanceof Map ? [{names}].every {{ workflow.container[it] }} "
                 f": workflow.container)")
    lines.append(
        f"{_INDENT}if (workflow.profile.tokenize(',').contains('slurm') && {empty})"
        f"\n{_INDENT * 2}error \"nextflow.config, profile slurm: process.container is empty; set it "
        f"to the .sif built from image {digests}\"")
    # The launch record: what the run was given, written before any task runs so a run
    # killed hard still has it (RUN_RECORD_BLOCK, the standard of every rendered pipeline).
    lines.append(RUN_RECORD_BLOCK)
    meta = ", ".join(["sample: r.sample"] + [f"{c.name}: r.{c.name}" for c in ctx.value_cols])
    files = [f"file(r.{c.name}, checkIfExists: true)" for c in ctx.path_cols]
    item = f"tuple([{meta}], " + ", ".join(files) + ")" if files else f"[{meta}]"
    shape = "(meta, " + ", ".join(c.name for c in ctx.path_cols) + ")" if files else "meta"
    lines += [
        f"{_INDENT}rows = channel.fromPath(params.samplesheet, checkIfExists: true)",
        f"{_INDENT * 2}.splitCsv(header: true)",
        f"{_INDENT * 2}.map {{ r -> {item} }}   // one {shape} per samples.csv row",
        "",
    ]
    for s in record.stages:
        call, comment = _stage_call(s, ctx)
        lines.append(_INDENT + call + (f"   // {comment}" if comment else ""))
    lines.append("}")
    return "\n".join(lines)


def _render_main(record: PipelineRecord, ctx: _Context) -> str:
    parts = [
        f"// {ctx.header}",
        f"// {len(record.stages)} stage(s) over {sum(len(s.commands) for s in record.stages)} how-to "
        f"command(s), each stage run once per samples.csv row; every process runs its sealed "
        f"command(s) with the placeholders bound.",
        "",
        "nextflow.enable.dsl = 2",
        "",
        _render_params_block(record, ctx),
        "",
    ]
    for s in record.stages:
        parts += [_render_process(record, s, ctx), ""]
    parts.append(_render_workflow_block(record, ctx))
    return "\n".join(parts) + "\n"


# ── nextflow.config ────────────────────────────────────────────────────────


def _fully_sized(stage: PipelineStage) -> bool:
    r = stage.resources
    return r.cpus is not None and r.mem is not None and r.time is not None


def _sizing_lines(stage: PipelineStage) -> list[str]:
    r = stage.resources
    out = []
    if r.cpus is not None:
        line = f"cpus = {int(r.cpus)}"
        if r.threads_slot:
            line += ("   // the thread count the command runs with, through task.cpus"
                     + (f"; the sealed run used {int(r.cpus)}" if r.requested_by == "seal" else ""))
        out.append(line)
    if r.mem is not None:
        out.append(f"memory = {_nf_memory(r.mem)}")
    if r.time is not None:
        out.append(f"time = {_nf_time(r.time)}")
    return out


def _gpu_lines(stage: PipelineStage, ctx: _Context) -> list[str]:
    """A GPU stage's SLURM placement: the same job-then-env merge every header gets,
    `--gres` + `--qos` + `--account` through clusterOptions (withName replaces the
    profile-wide clusterOptions, so the account rides along), `--nv` for apptainer."""
    r = stage.resources
    req: dict[str, Any] = {"gpus": int(r.gpus)}
    merged, _, placement = _resolve_slurm_and_email(req, ctx.env)
    opts = [f"--gres=gpu:{int(r.gpus)}"]
    if merged.get("qos"):
        _check_safe_token("slurm.qos", merged["qos"])
        opts.append(f"--qos={merged['qos']}")
    if merged.get("account"):
        opts.append(f"--account={merged['account']}")
    out = []
    note = _GPU_PLACEMENT_HEADER_NOTE.get(placement["state"])
    if note:
        out.append("// " + note[2:])
    if merged.get("partition"):
        out.append(f"queue = '{merged['partition']}'")
    out.append(f"clusterOptions = '{' '.join(opts)}'")
    out.append("containerOptions = '--nv'")
    return out


def _sif_lines(im: dict, ctx: _Context) -> list[str]:
    """The slurm profile's container for one image: the .sif `stage_apptainer_image`
    put in the named cluster's container zone, or an empty value that says so — the
    workflow refuses to start on it rather than running the tools on the bare node."""
    digest = im["digest"] or im["ref"]
    if im.get("sif_path"):
        where = f" on {ctx.record.compute_env}" if ctx.record.compute_env else ""
        return [f"// the .sif built from image {digest}, where stage_apptainer_image put it{where}",
                f"container = {_nf_quote(im['sif_path'])}"]
    return [f"// SET ME: the .sif built from image {digest}. Render with env= naming the cluster,",
            "// or paste the path stage_apptainer_image reports.",
            "container = ''"]


def _render_config(record: PipelineRecord, ctx: _Context) -> str:
    L: list[str] = [f"// {ctx.header}", ""]
    # Each profile names the image it runs, so params.yaml carries nothing about WHERE.
    L += ["profiles {", f"{_INDENT}local {{", f"{_INDENT * 2}docker.enabled = true",
          f"{_INDENT * 2}process {{", f"{_INDENT * 3}executor = 'local'"]
    if len(ctx.images) == 1:
        im = ctx.images[0]
        L.append(f"{_INDENT * 3}// the frozen image {im['digest'] or im['ref']}, as docker names it here")
        L.append(f"{_INDENT * 3}container = {_nf_quote(im['ref'])}")
    else:
        for s in record.stages:
            L.append(f"{_INDENT * 3}withName: '{s.name}' {{ container = "
                     f"{_nf_quote(ctx.image_of[s.name]['ref'])} }}")
    L += [f"{_INDENT * 2}}}", f"{_INDENT}}}", f"{_INDENT}slurm {{",
          f"{_INDENT * 2}apptainer.enabled = true",
          f"{_INDENT * 2}apptainer.autoMounts = true",
          f"{_INDENT * 2}apptainer.runOptions = '--cleanenv'       // the image's own environment, "
          f"never the login node's",
          f"{_INDENT * 2}executor.queueSize = {NEXTFLOW_QUEUE_SIZE}",
          f"{_INDENT * 2}process {{", f"{_INDENT * 3}executor = 'slurm'"]
    if len(ctx.images) == 1:
        L += [f"{_INDENT * 3}{c}" for c in _sif_lines(ctx.images[0], ctx)]
    L.append(f"{_INDENT * 3}cache = 'lenient'")
    if ctx.apptainer_module:
        L.append(f"{_INDENT * 3}beforeScript = 'module load {ctx.apptainer_module}'")
    if ctx.cpu_partition:
        L.append(f"{_INDENT * 3}queue = '{ctx.cpu_partition}'")
    if ctx.account:
        L.append(f"{_INDENT * 3}clusterOptions = '--account={ctx.account}'")
    for s in record.stages:
        block: list[str] = []
        if len(ctx.images) > 1:
            block += _sif_lines(ctx.image_of[s.name], ctx)
        if s.resources.gpus > 0:
            block += _gpu_lines(s, ctx)
        if block:
            L.append(f"{_INDENT * 3}withName: '{s.name}' {{")
            L += [f"{_INDENT * 4}{b}" for b in block]
            L.append(f"{_INDENT * 3}}}")
    L += [f"{_INDENT * 2}}}", f"{_INDENT}}}", "}", ""]

    # resources
    L += ["process {",
          f"{_INDENT}// One failing sample stops nothing already running: in-flight tasks complete, nothing",
          f"{_INDENT}// new is submitted, and -resume re-runs the failed tasks and what follows them.",
          f"{_INDENT}errorStrategy = 'finish'"]
    if not all(_fully_sized(s) for s in record.stages):
        d = DEFAULT_STAGE_REQUEST
        L += [f"{_INDENT}// DEFAULT request — not sized for your data",
              f"{_INDENT}cpus = {int(d['cpus'])}",
              f"{_INDENT}memory = {_nf_memory(str(d['mem']))}",
              f"{_INDENT}time = {_nf_time(str(d['time']))}"]
    for s in record.stages:
        sizing = _sizing_lines(s)
        if not sizing:
            continue
        measured = _measured_comment(s)
        L.append(f"{_INDENT}withName: '{s.name}' {{" + (f"   // {measured}" if measured else ""))
        L += [f"{_INDENT * 2}{x}" for x in sizing]
        L.append(f"{_INDENT}}}")
    L += ["}", ""]

    L.append(RUN_RECORD_CONFIG)
    return "\n".join(L) + "\n"


# ── params.yaml ────────────────────────────────────────────────────────────


def _yaml_line(key: str, value: Any) -> str:
    return yaml.safe_dump({key: value}, default_flow_style=False, width=10 ** 6,
                          allow_unicode=True).rstrip("\n")


def _render_params_yaml(record: PipelineRecord, ctx: _Context) -> str:
    L = [f"# rendered from sealed workflow {record.sealed_workflow} (pipeline {record.name}) — "
         f"edit this file and samples.csv, not main.nf",
         "# Every value is what the sealed run was validated with. WHERE the pipeline runs — which",
         "# image, on which machine — is nextflow.config's business, never this file's."]
    for p in record.params:
        if p.kind != "shared":
            continue                                  # a samplesheet column or a thread slot
        L.append(f"# {_param_comment(record, p)}")
        if p.default is not None and _INT_RE.match(p.default):
            L.append(_yaml_line(p.name.lower(), int(p.default)))
        else:
            L.append(_yaml_line(p.name.lower(), p.default or ""))
    cols = ", ".join(c.name for c in record.samplesheet.columns)
    L.append(f"# The samplesheet: one row per sample, columns {cols}.")
    L.append(_yaml_line(*SAMPLESHEET_PARAM))
    L.append("# Where published outputs land.")
    L.append(_yaml_line(*OUTDIR_PARAM))
    return "\n".join(L) + "\n"


# ── the launchers ──────────────────────────────────────────────────────────


def _render_launcher(record: PipelineRecord, ctx: _Context) -> str:
    """The cluster launcher: `cd` into the pipeline directory, then `sbatch launcher.sh`.
    SLURM starts the job in the directory sbatch was run from, so there is no `cd`."""
    L = ["#!/usr/bin/env bash",
         f"# {ctx.header}",
         "# The manager job: it submits one job per stage and sample, and does none of the work.",
         _render_sbatch_header(record.name, ctx.manager_slurm, ctx.email).rstrip("\n"),
         "",
         STRICT_MODE_LINE,
         ""]
    if ctx.apptainer_module and ctx.nextflow_module:
        L += [f"module load {ctx.apptainer_module} {ctx.nextflow_module}"]
    elif ctx.env:
        L += ["# The env declares no apptainer_module / nextflow_module: make apptainer and",
              "# nextflow available before `sbatch launcher.sh`."]
    else:
        L += ["# Rendered without a compute env: make apptainer and nextflow available before",
              "# `sbatch launcher.sh`."]
    L += ["",
          "# Nextflow keeps its own files under NXF_HOME, which defaults to $HOME; compute nodes",
          "# may not be able to write there, so it lives inside this directory.",
          'export NXF_HOME="$PWD/.nextflow_home"',
          "",
          "# -resume re-runs only the stages whose inputs or parameters changed; drop it for a",
          "# fresh run. Each run leaves its own runs/<timestamp>/ (params.json, samples.csv, trace.txt,",
          "# report.html, timeline.html), never overwritten; this job's .out file is the manager's log.",
          _RUN_LINE.format(profile="slurm") + ' "$@"',
          "",
          "# Work directories are never cleaned for you. Once the published outputs are where",
          "# you want them:  nextflow clean -f"]
    return "\n".join(L) + "\n"


# ── the one entry point ────────────────────────────────────────────────────


def render_nextflow(record: PipelineRecord, *, env: Optional[dict] = None) -> dict[str, str]:
    """Render the Nextflow files of `record`: `{relative path: content}` for main.nf,
    nextflow.config, params.yaml, samples.csv and launcher.sh. `env` is a compute-env
    block from projects_access.yaml; it supplies the SLURM policy (account, partitions,
    qos), the notification email and the Lmod module names. Without it the files
    render for a cluster with no policy. Raises ValueError, naming the remedy, on
    anything the files cannot carry."""
    _check_record(record)
    ctx = _Context(record, env or {})
    return {
        "main.nf": _render_main(record, ctx),
        "nextflow.config": _render_config(record, ctx),
        "params.yaml": _render_params_yaml(record, ctx),
        "samples.csv": render_samplesheet(record),
        "launcher.sh": _render_launcher(record, ctx),
    }


def bound_commands(record: PipelineRecord, stage: PipelineStage) -> list[str]:
    """The script lines main.nf runs for `stage`: the sealed commands through the
    binding table. What the page's command column shows, so it can never paraphrase."""
    ctx = _Context(record, {})
    return [ctx.bind(stage, c) for c in stage.commands]


def run_lines(record: PipelineRecord, locus: str) -> list[str]:
    """How a run starts at a locus: `RUN_LOCAL` on a laptop, `RUN_HPC` on the cluster."""
    if locus == "local":
        return [RUN_LOCAL]
    if locus == "hpc":
        return [RUN_HPC]
    raise ValueError(f"locus must be 'local' or 'hpc', got {locus!r}")


__all__ = ["render_nextflow", "bound_commands", "run_lines", "RUN_LOCAL", "RUN_HPC",
           "STRICT_MODE_LINE", "SAMPLESHEET_PARAM", "OUTDIR_PARAM", "RUN_RECORDS", "RUN_RECORD_FILES",
           "RUN_RECORD_CONFIG", "RUN_RECORD_BLOCK"]
