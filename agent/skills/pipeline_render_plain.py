"""
pipeline_render_plain — the PLAIN form of a pipeline: bash + SLURM job arrays.

`render_plain(record, env=None)` turns a `PipelineRecord` into the files of a pipeline
directory — strings in, `{relative path: content}` out; no disk, no subprocess. Every
command is the sealed how-to's, verbatim but for its placeholders; every default is the
value the sealed run was validated with; every container is the image the seal observed.
A record the form cannot carry is refused with a `ValueError` that names the remedy.

The file set
------------
  params.env                one `NAME=value` per shared param (default: the sealed value),
                            RESULTS_DIR, SAMPLESHEET (per_row), IMAGE_<STAGE> / SIF_<STAGE>
  samples.csv               per_row only — `pipeline_record.render_samplesheet`, THE one
                            samplesheet rendering (a CSV cannot carry a header comment)
  stages/NN_<stage>.sh      the stage's commands with placeholders bound to shell variables,
                            behind one precondition per artifact it consumes from an earlier
                            stage. Runs INSIDE the stage's image; sources nothing — its
                            caller sets the variables the header lists under `expects`
  stages/NN_<stage>.sbatch  the cluster job for one stage: `apptainer exec` of the stage
                            script. A per_row stage is a job ARRAY — the header carries no
                            --array; the driver passes it, since the row count is only known
                            at submit
  run_local.sh              the laptop runner: every row × stage in order, one `docker run`
                            per stage
  run_all.sh                the cluster driver: one sbatch per stage, chained with afterok
  HOWTO.md

Binding rules — a placeholder binds ONLY through the record
-----------------------------------------------------------
  {NAME}          a param or a samplesheet column            → ${NAME}
  {SLOT}/<art>    SLOT an output slot (`record.output_slots`) → ${OUTPUT_DIR}/<art>; a
                  placeholder inside <art> binds like any other; a bare {SLOT} → ${OUTPUT_DIR}
  {ID}            a per-sample identifier that is not a sheet column (the record folds it
                  into the sheet's `sample` column)          → ${SAMPLE}
  anything else   refused — the record decides what a placeholder is, never this renderer.
  A `${NAME}` the sealed command already carried is shell, not a placeholder, and is left
  alone.

ONE working directory per row, shared by every stage: OUTPUT_DIR is ${RESULTS_DIR}/${SAMPLE}
(per_row) or ${RESULTS_DIR} itself (linear). The sealed how-to ran its commands in one dir
and its artifacts flow between commands by name inside it; the plain form keeps that.

Values travel through shell lines, so a value — a default, a sheet cell, an image ref — may
hold only `_VALUE_CHARS`. The render refuses a default outside it; the scripts' pre-flight
refuses a run-time value outside it, from the SAME character class.

Variables reach the container EXPLICITLY: `docker run -e NAME=value` locally, and on the
cluster `apptainer exec --cleanenv … env NAME=value bash <stage script>` — the `env` form is
used rather than APPTAINERENV_* so the values are on the line a human reads. Nothing comes
from the host environment.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional

from agent.skills import compute_access
from agent.skills.pipeline_record import (DEFAULT_STAGE_REQUEST, PipelineRecord, PipelineStage,
                                          _PLACEHOLDER_RE, render_samplesheet)
from agent.skills.submit_workflow import _resolve_slurm_and_email
from agent.skills.workflow_render import (_check_abs_path, _check_email, _check_module,
                                          _check_safe_token, _check_slurm, _render_sbatch_header)

#: The variable every stage writes into; every output slot of the how-to binds to it.
_OUTPUT_VAR = "OUTPUT_DIR"
#: The row identifier: the samplesheet's `sample` column, exported under this name.
_ROW_VAR = "SAMPLE"
#: The row label of a linear pipeline (one implicit row) in `runs/<id>/<stage>.<row>.rc`.
_LINEAR_ROW = "single"
#: The exit code of a stage whose input from an earlier stage is missing.
_MISSING_INPUT_RC = 3
#: What a value may contain. Rendered into the shell pre-flight verbatim, so the two
#: checks cannot drift: a `-` last keeps it valid inside a `[...]` bracket in both dialects.
_VALUE_CHARS = "A-Za-z0-9_./:@%+=,-"
_VALUE_RE = re.compile(f"^[{_VALUE_CHARS}]*$")
#: A placeholder / shell variable name.
_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: An artifact name once its placeholders are removed: a basename or a relative path,
#: possibly a glob.
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9_.*/-]*$")
#: A `{NAME}` that is a record placeholder — not the `{NAME}` inside a `${NAME}` the sealed
#: command already carried.
_BIND_RE = re.compile(r"(?<!\$)" + _PLACEHOLDER_RE.pattern)
#: Variables the rendered scripts own; a placeholder may not shadow one.
_RESERVED = frozenset({"RESULTS_DIR", "SAMPLESHEET", "RUN_ID", "RUN_DIR", "PIPELINE_DIR",
                       "OUTPUT_DIR", "ROW", "BIND", "BOUND", "COLS", "VALS", "STAGES",
                       "NROWS", "SELECTED", "ONLY_STAGES", "ONLY_ROWS"})
_EDIT_NOTE = "do not hand-edit; edit params.env / samples.csv"
_KIND_TEXT = {"path": "a path", "prefix": "a prefix (a family of files named after it)",
              "value": "a value"}


def render_plain(record: PipelineRecord, *, env: Optional[dict] = None) -> dict[str, str]:
    """Render the plain form of `record`: `{relative path: content}`.

    `env` is a compute-env block from projects_access.yaml (`apptainer_module`, `slurm`
    {account, partition, gpu {partition, qos}}, `email`); when given, every stage's SLURM
    request is merged through `submit_workflow._resolve_slurm_and_email` and the job loads
    the env's apptainer module. `None` renders the cluster files with no module lines and
    no account, which still runs locally and still sbatches on a cluster whose defaults
    suffice. Raises ValueError, naming the remedy, on a record the form cannot carry."""
    if env is not None and not isinstance(env, Mapping):
        raise ValueError(f"env must be a mapping (a compute-env block), got {type(env).__name__}")
    bind = _check_record(record)
    files: dict[str, str] = {"params.env": _render_params_env(record)}
    if record.samplesheet is not None:
        files["samples.csv"] = render_samplesheet(record)
    for stage in record.stages:
        base = _stage_base(stage)
        script, expects = _render_stage_script(record, stage, bind)
        files[f"stages/{base}.sh"] = script
        files[f"stages/{base}.sbatch"] = _render_stage_sbatch(record, stage, expects, env)
    files["run_local.sh"] = _render_run_local(record, bind)
    files["run_all.sh"] = _render_run_all(record)
    files["HOWTO.md"] = _render_howto(record, bind, env)
    return files


# ── checks ──────────────────────────────────────────────────────────────────


def _check_var(label: str, name: str, *, reserved: bool = True) -> None:
    """A placeholder that becomes a shell variable in every rendered script. An output
    slot skips the reserved-name rule: it binds to OUTPUT_DIR, never to its own name."""
    _check_safe_token(label, name)
    if not _VAR_RE.match(name):
        raise ValueError(
            f"{label}={name!r} must be an upper-case shell identifier ([A-Z][A-Z0-9_]*): "
            f"it names a shell variable in every rendered script")
    if reserved and (name in _RESERVED or name.startswith(("IMAGE_", "SIF_"))):
        raise ValueError(
            f"{label}={name!r} collides with a variable the rendered scripts own "
            f"({', '.join(sorted(_RESERVED))}, IMAGE_*, SIF_*); rename the placeholder "
            f"in the how-to and re-seal")


def _check_value(label: str, value: object) -> None:
    """A value that will travel through a shell line."""
    if not isinstance(value, str) or not _VALUE_RE.match(value):
        raise ValueError(
            f"{label}={value!r} contains whitespace or a shell metacharacter; the plain form "
            f"passes values through shell lines, so a value may hold only [{_VALUE_CHARS}]")


def _check_artifact(label: str, artifact: Optional[str], bind: Mapping[str, str]) -> None:
    """An artifact is a filename (or a glob, or a relative path) whose placeholders bind."""
    if not artifact:
        raise ValueError(f"{label} names no artifact; re-derive the record from the seal")
    _bind(artifact, bind, label)
    residue = _PLACEHOLDER_RE.sub("", artifact)
    if (not _ARTIFACT_RE.match(residue) or artifact.startswith("/")
            or ".." in artifact.split("/")):
        raise ValueError(
            f"{label} artifact {artifact!r} is not a safe filename (letters, digits, "
            f"`_.-*/` and placeholders; relative, no `..`); rename what the how-to writes "
            f"into its output slot and re-seal")


def _bindings(record: PipelineRecord) -> dict[str, str]:
    """placeholder → the shell variable it binds to (the module docstring's rules)."""
    bind = {slot: _OUTPUT_VAR for slot in record.output_slots}
    columns = ({c.placeholder for c in record.samplesheet.columns}
               if record.samplesheet is not None else set())
    for p in record.params:
        folded_id = (record.samplesheet is not None and p.kind == "per_sample"
                     and p.name not in columns)
        bind[p.name] = _ROW_VAR if folded_id else p.name
    return bind


def _bind(text: str, bind: Mapping[str, str], where: str) -> str:
    """Substitute every record placeholder; refuse one the record does not know."""
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in bind:
            raise ValueError(
                f"{where} names {{{name}}}, which is neither a param, a samplesheet column "
                f"nor an output slot of the record; declare it as a how-to input and re-seal")
        return "${" + bind[name] + "}"
    return _BIND_RE.sub(sub, text)


def _bound_names(text: str, bind: Mapping[str, str]) -> set[str]:
    return {bind[p] for p in _BIND_RE.findall(text)}


def _check_record(record: PipelineRecord) -> dict[str, str]:
    """Refuse what the form cannot carry; return the binding map."""
    _check_safe_token("pipeline name", record.name)
    if not isinstance(record.sealed_workflow, str) or not record.sealed_workflow.strip() \
            or "\n" in record.sealed_workflow:
        raise ValueError("record.sealed_workflow must be a one-line workflow name")
    if not record.stages:
        raise ValueError("the record has no stages; nothing to render")
    if (record.shape == "per_row") != (record.samplesheet is not None):
        raise ValueError(
            f"record.shape={record.shape!r} but samplesheet is "
            f"{'present' if record.samplesheet is not None else 'absent'}; a per_row record "
            f"carries the sheet and a linear one does not — re-derive the record")
    for slot in record.output_slots:
        _check_var("output slot", slot, reserved=False)
    for p in record.params:
        _check_var("param", p.name)
        if p.default is not None:
            _check_value(f"default of {p.name}", p.default)
            if p.value_kind in ("path", "prefix") and p.default:
                _check_abs_path(f"default of {p.name}", p.default)
    if record.samplesheet is not None:
        for c in record.samplesheet.columns:
            _check_var("samplesheet column placeholder", c.placeholder)
            if c.name.upper() != c.placeholder:
                raise ValueError(
                    f"samplesheet column {c.name!r} does not upper-case to its placeholder "
                    f"{c.placeholder!r}; the header-driven runner exports column `x` as X")
        for row in record.samplesheet.rows:
            for col, v in row.items():
                _check_value(f"samplesheet row {row.get('sample')!r} column {col}", v)
            sid = str(row.get("sample", ""))
            if not sid or "/" in sid or sid in (".", ".."):
                raise ValueError(
                    f"samplesheet row identifier {sid!r} must be a plain name: it names the "
                    f"row's directory under RESULTS_DIR")
    bind = _bindings(record)
    for st in record.stages:
        if st.scope == "cohort":
            raise ValueError(
                f"stage {st.name} runs over the whole cohort: cohort stages are not "
                f"supported yet in the plain form; render the nextflow form, or cut the "
                f"pipeline before that stage")
        _check_safe_token("stage name", st.name)
        if not _VAR_RE.match(st.name):
            raise ValueError(
                f"stage name {st.name!r} must be UPPER_SNAKE_CASE ([A-Z][A-Z0-9_]*): it "
                f"names IMAGE_<STAGE> and SIF_<STAGE> in params.env; pass stage_names in "
                f"that form when deriving the record")
        if not st.commands:
            raise ValueError(f"stage {st.name} has no commands; re-derive the record")
        if st.image is not None:
            _check_value(f"image of stage {st.name}", st.image)
        for i in st.inputs:
            if i.origin == "stage":
                if not i.from_stage:
                    raise ValueError(f"stage {st.name} input {i.name!r} names no producing stage")
                _check_artifact(f"stage {st.name} input", i.artifact, bind)
        for o in st.outputs:
            _check_artifact(f"stage {st.name} output", o.artifact, bind)
        for cmd in st.commands:
            _bind(cmd, bind, f"stage {st.name} command")
    return bind


# ── shared pieces ───────────────────────────────────────────────────────────


def _header(record: PipelineRecord, role: str) -> str:
    return (f"# {record.name} — {role} — rendered from sealed workflow "
            f"{record.sealed_workflow} — {_EDIT_NOTE}")


def _stage_base(stage: PipelineStage) -> str:
    return f"{stage.index + 1:02d}_{stage.name.lower()}"


def _is_glob(artifact: str) -> bool:
    return any(ch in artifact for ch in "*?[")


def _measured(stage: PipelineStage) -> Optional[str]:
    r = stage.resources
    parts = []
    if r.measured_wall_seconds is not None:
        parts.append(f"wall {r.measured_wall_seconds:.1f}s")
    if r.measured_peak_rss_mb is not None:
        parts.append(f"peak RSS {r.measured_peak_rss_mb:.0f} MB")
    if not parts:
        return None
    return f"measured on test data: {', '.join(parts)} ({r.measured_authority})"


def _stage_request(stage: PipelineStage) -> tuple[dict, list[str]]:
    """The stage's SLURM request and the comment lines that qualify it. An unsized slot
    takes DEFAULT_STAGE_REQUEST's value and the header says so."""
    r = stage.resources
    req = {"time": r.time or DEFAULT_STAGE_REQUEST["time"],
           "mem": r.mem or DEFAULT_STAGE_REQUEST["mem"],
           "cpus": r.cpus or DEFAULT_STAGE_REQUEST["cpus"],
           "gpus": r.gpus}
    notes = []
    if r.requested_by == "default":
        notes.append("# DEFAULT request — not sized for your data")
    else:
        unsized = [k for k in ("time", "mem", "cpus") if getattr(r, k) is None]
        if unsized:
            notes.append(f"# DEFAULT {', '.join(unsized)} — the caller's request left them unsized")
    m = _measured(stage)
    if m:
        notes.append(f"# {m}")
    return req, notes


def _source_text(source: str) -> str:
    kind, _, rest = source.partition(":")
    return {"sealed_step": f"produced by sealed step {rest} of the workflow",
            "test_data": f"the workflow's test data ({rest})",
            "reference_database": f"reference database {rest}",
            "usage_input": "a declared how-to input",
            "literal": "a literal in the how-to"}.get(kind, source)


def _params_env_entries(record: PipelineRecord) -> list:
    """The params params.env carries: every shared param, plus — linear only — every
    per-sample param, since the one implicit row has nowhere else to live."""
    shared = [p for p in record.params if p.kind == "shared"]
    per_sample = [p for p in record.params if p.kind == "per_sample"] \
        if record.shape == "linear" else []
    return shared + per_sample


def _path_bindings(record: PipelineRecord) -> list[tuple[str, str]]:
    """(variable, what it is) for every path/prefix-valued value the container must see:
    its parent dir is bound at its own host path."""
    out = [(p.name, f"{p.name} ({p.value_kind})")
           for p in _params_env_entries(record) if p.value_kind in ("path", "prefix")]
    if record.samplesheet is not None:
        out += [(c.placeholder, f"{c.placeholder} (column, {c.value_kind})")
                for c in record.samplesheet.columns if c.value_kind in ("path", "prefix")]
    return out


# ── params.env ──────────────────────────────────────────────────────────────


def _render_params_env(record: PipelineRecord) -> str:
    L = [f"# {record.name} — parameters — rendered from sealed workflow {record.sealed_workflow} "
         f"— this file and samples.csv are the two you edit; every script sources it",
         "#",
         f"# A value may hold only [{_VALUE_CHARS}]: no whitespace, no quotes, no shell",
         "# metacharacters (values travel through shell lines). Paths are absolute.",
         "",
         "# ── shared parameters: the same for every row. Each default is the value the sealed",
         "#    run was validated with (test data) — replace it with yours."]
    for p in record.params:
        if p.kind != "shared":
            continue
        L.append("")
        L.append(f"# {p.name}: {_KIND_TEXT[p.value_kind]}; source: {_source_text(p.source)}"
                 + (f"; format {p.format}" if p.format else "")
                 + (f" — {p.description}" if p.description else ""))
        L.append(f"{p.name}={p.default or ''}")
    per_sample = [p for p in _params_env_entries(record) if p.kind == "per_sample"]
    if per_sample:
        L += ["",
              "# ── per-sample inputs: this pipeline has ONE implicit row, so they are set here.",
              ("#    The values are the sealed run's own trial — the worked example; replace them "
               "with yours." if all(p.default is not None for p in per_sample) else
               "#    REQUIRED — the record carries no default for a per-sample value.")]
        for p in per_sample:
            L.append("")
            L.append(f"# {p.name}: {_KIND_TEXT[p.value_kind]}"
                     + (f"; format {p.format}" if p.format else "")
                     + (f" — {p.description}" if p.description else ""))
            L.append(f"{p.name}={p.default or ''}")
    L += ["",
          "# ── where results land: one working directory per row, shared by every stage —",
          ("#    <RESULTS_DIR>/<sample>/" if record.samplesheet is not None else "#    <RESULTS_DIR>/ itself (one implicit row)")
          + ". Absolute; export RESULTS_DIR before running to override.",
          'RESULTS_DIR="${RESULTS_DIR:-$PWD/results}"']
    if record.samplesheet is not None:
        L += ["",
              "# ── the samplesheet: header-driven, one row per sample; column `x` reaches the",
              "#    stages as X (see HOWTO.md).",
              "SAMPLESHEET=samples.csv"]
    L += ["",
          "# ── containers, one per stage. IMAGE_*: the docker image the seal validated in",
          "#    (run_local.sh). SIF_*: the Apptainer image on the cluster (run_all.sh) — EMPTY",
          "#    at render because the cluster path is not known here; build the .sif from the",
          "#    digest named beside it and fill in its absolute path."]
    for st in record.stages:
        L.append("")
        L.append(f"IMAGE_{st.name}={st.image or ''}")
        L.append(f"# SIF_{st.name}: build from image digest {st.image_digest or 'unrecorded'}"
                 + (f" (freeze request {st.request_key})" if st.request_key else "")
                 + (f"; the sealed cluster run's .sif hashed {st.sif_sha256}" if st.sif_sha256 else ""))
        L.append(f"SIF_{st.name}=")
    return "\n".join(L) + "\n"


# ── stages/NN_<stage>.sh ────────────────────────────────────────────────────


def _render_stage_script(record: PipelineRecord, stage: PipelineStage,
                         bind: Mapping[str, str]) -> tuple[str, list[str]]:
    """The stage script and the variables it expects its caller to set."""
    where = f"stage {stage.name}"
    expects: set[str] = set()
    pre: list[str] = []
    for i in stage.inputs:
        if i.origin != "stage":
            continue
        art = _bind(i.artifact or "", bind, where)
        expects.add(_OUTPUT_VAR)
        expects |= _bound_names(i.artifact or "", bind)
        target = f'"${{{_OUTPUT_VAR}}}/{art}"'
        test = f"compgen -G {target} >/dev/null" if _is_glob(i.artifact or "") else f"[ -e {target} ]"
        pre.append(f'{test} || {{ echo "stage {stage.name} needs {art} from stage {i.from_stage}; '
                   f'run that stage first" >&2; exit {_MISSING_INPUT_RC}; }}')
    cmds = []
    for c in stage.commands:
        cmds.append(_bind(c, bind, where))
        expects |= _bound_names(c, bind)
    outputs = ", ".join(_bind(o.artifact, bind, where) + (" [published]" if o.published else "")
                        for o in stage.outputs)
    L = ["#!/usr/bin/env bash",
         _header(record, f"stage {stage.index + 1}/{len(record.stages)} {stage.name}"),
         f"# image: {stage.image or 'unrecorded'} (digest {stage.image_digest or 'unrecorded'})",
         (f"# derives from sealed step{'s' if len(stage.sealed_steps) != 1 else ''} "
          f"{', '.join(str(n) for n in stage.sealed_steps)}"
          if stage.sealed_steps else "# derives from no sealed step (the how-to command alone)")]
    m = _measured(stage)
    if m:
        L.append(f"# {m}")
    L.append(f"# expects (set by its caller; this script sources nothing): {' '.join(sorted(expects))}")
    if outputs:
        L.append(f"# writes into {_OUTPUT_VAR}: {outputs}")
    if stage.stage_in_copy:
        L.append("# rewrites in place an artifact it consumes, as the sealed run did")
    if stage.consumes_workdir:
        L.append(f"# consumes the whole working directory ({_OUTPUT_VAR}), not one artifact")
    L.append("set -euo pipefail")
    L += pre
    L += cmds
    return "\n".join(L) + "\n", sorted(expects)


# ── shell snippets shared by the runners and the jobs ──────────────────────


def _sh_checks(prog: str) -> str:
    """die + the value checks. The character class is `_VALUE_CHARS`, verbatim."""
    return f"""die() {{ echo "{prog}: $*" >&2; exit 2; }}

# A value may hold only [{_VALUE_CHARS}] — no whitespace, no shell metacharacters — because
# it travels through shell lines unquoted inside the stage scripts.
check_value() {{   # name value
  case "$2" in *[!{_VALUE_CHARS}]*) die "$1=$2 contains whitespace or a shell metacharacter; a value may hold only [{_VALUE_CHARS}]";; esac
}}
check_set() {{ [ -n "$2" ] || die "$1 is empty; set it in params.env / samples.csv"; check_value "$1" "$2"; }}
check_path() {{ check_set "$1" "$2"; [ -e "$2" ] || die "$1=$2 does not exist"; }}
check_prefix() {{ check_set "$1" "$2"; compgen -G "$2*" >/dev/null || die "$1=$2 matches no file (a prefix names a family of files)"; }}
check_id() {{ check_set "$1" "$2"; case "$2" in */*|.|..) die "$1=$2 must be a plain name: it names the row's directory";; esac; }}
"""


def _sh_sheet(record: PipelineRecord) -> str:
    """Header-driven samplesheet reading: column `x` becomes the variable X; row N is
    line N+1. ONE snippet, used by the local runner, the driver and every array job."""
    required = " ".join(c.name for c in record.samplesheet.columns)
    reserved = "|".join(sorted(_RESERVED - {_ROW_VAR}) + ["IMAGE_*", "SIF_*"])
    return f"""# The samplesheet is header-driven: column `x` becomes the variable X. Row N is line N+1.
sheet_columns() {{   # sets COLS from the header of $SAMPLESHEET; refuses a missing required column
  local header c
  [ -f "$SAMPLESHEET" ] || die "samplesheet $SAMPLESHEET not found"
  header="$(head -n 1 "$SAMPLESHEET")"
  IFS=, read -r -a COLS <<< "$header"
  for c in "${{COLS[@]}}"; do
    case "$c" in ""|*[!a-z0-9_]*) die "samplesheet column '$c' is not a lower-case identifier";; esac
    case "$(printf '%s' "$c" | tr '[:lower:]' '[:upper:]')" in {reserved}) die "samplesheet column '$c' would shadow a variable the scripts own";; esac
  done
  for c in {required}; do
    case " ${{COLS[*]}} " in *" $c "*) ;; *) die "samplesheet lacks the required column '$c' (header: $header)";; esac
  done
}}
sheet_row() {{   # $1 = row number (1-based): sets one variable per column, and ROW
  local line i name
  line="$(sed -n "$(( $1 + 1 ))p" "$SAMPLESHEET")"
  case "$line" in *$'\\r'*) die "samplesheet row $1 has a carriage return; convert the file to Unix line endings";; esac
  [ -n "$line" ] || die "samplesheet row $1 is empty; remove blank lines"
  IFS=, read -r -a VALS <<< "$line"
  [ "${{#VALS[@]}}" -eq "${{#COLS[@]}}" ] || die "samplesheet row $1 has ${{#VALS[@]}} fields, the header has ${{#COLS[@]}} (an empty last cell counts as missing)"
  for i in "${{!COLS[@]}}"; do
    name="$(printf '%s' "${{COLS[$i]}}" | tr '[:lower:]' '[:upper:]')"
    printf -v "$name" '%s' "${{VALS[$i]}}"
  done
  ROW="$SAMPLE"
}}
"""


def _sh_param_checks(record: PipelineRecord, indent: str = "  ") -> list[str]:
    """One check per params.env value: a path must exist, a prefix must match a file, a
    per-sample value (linear) must be set, any other value only has to be safe."""
    lines = [f'{indent}case "$RESULTS_DIR" in /*) ;; *) die "RESULTS_DIR=$RESULTS_DIR must be an absolute path";; esac',
             f'{indent}check_set RESULTS_DIR "$RESULTS_DIR"']
    for p in _params_env_entries(record):
        check = {"path": "check_path", "prefix": "check_prefix"}.get(
            p.value_kind, "check_set" if p.kind == "per_sample" else "check_value")
        lines.append(f'{indent}{check} {p.name} "${{{p.name}:-}}"')
    return lines


def _sh_row_checks(record: PipelineRecord, indent: str = "  ") -> list[str]:
    """The checks on one samplesheet row's values, after `sheet_row` set them."""
    lines = [f'{indent}check_id SAMPLE "$SAMPLE"']
    for c in record.samplesheet.columns:
        if c.placeholder == _ROW_VAR:
            continue
        check = {"path": "check_path", "prefix": "check_prefix"}.get(c.value_kind, "check_value")
        lines.append(f'{indent}{check} {c.placeholder} "${c.placeholder}"')
    return lines


def _sh_preflight(record: PipelineRecord, containers: str) -> str:
    """Every value checked BEFORE anything runs: the params, the sheet's columns and every
    row's values, RESULTS_DIR, and the container of every selected stage — `containers`
    is IMAGE (a docker ref: must be set) or SIF (a file on the cluster: must exist)."""
    lines = ["preflight() {"] + _sh_param_checks(record)
    check = "check_path" if containers == "SIF" else "check_set"
    lines.append(f'  local s v; for s in $SELECTED; do v="{containers}_$s"; {check} "$v" "${{!v:-}}"; done')
    if record.samplesheet is not None:
        lines += ['  sheet_columns',
                  '  NROWS=$(( $(grep -c "" "$SAMPLESHEET") - 1 ))',
                  '  [ "$NROWS" -ge 1 ] || die "samplesheet $SAMPLESHEET has no rows"',
                  '  local i seen=" "',
                  '  for ((i = 1; i <= NROWS; i++)); do',
                  '    sheet_row "$i"']
        lines += _sh_row_checks(record, indent="    ")
        lines += ['    case "$seen" in *" $SAMPLE "*) die "samplesheet row $i repeats sample $SAMPLE; every row needs its own working directory";; esac',
                  '    seen="$seen$SAMPLE "',
                  '  done']
    lines.append("}")
    return "\n".join(lines) + "\n"


def _sh_binds(record: PipelineRecord, flag: str) -> str:
    """The container's view of the host: the stages dir (read-only), RESULTS_DIR, and the
    parent dir of every path/prefix value — each bound at its own host path so the values
    in params.env / samples.csv resolve verbatim inside the container. `flag` is `-v`
    (docker, `src:dst`) or `--bind` (apptainer)."""
    ro = f'"$PIPELINE_DIR/stages:$PIPELINE_DIR/stages:ro"'
    rw = '"$RESULTS_DIR:$RESULTS_DIR"' if flag == "-v" else '"$RESULTS_DIR"'
    add = '"$1:$1"' if flag == "-v" else '"$1"'
    lines = ["set_binds() {   # per row: the values may differ",
             '  BOUND=" "',
             f"  BIND=({flag} {ro} {flag} {rw})"]
    for var, what in _path_bindings(record):
        lines.append(f'  add_bind "$(dirname "${var}")"   # {what}')
    lines += ["}",
              f'add_bind() {{ case "$BOUND" in *" $1 "*) return;; esac; BOUND="$BOUND$1 "; BIND+=({flag} {add}); }}']
    return "\n".join(lines) + "\n"


def _sh_args(record: PipelineRecord, prog: str, rows: Optional[bool]) -> str:
    """Argument parsing. `rows`: True — `--rows` selects samplesheet rows; False — a linear
    pipeline, refused; None — the cluster driver, where every row is one array task."""
    stages = " ".join(s.name for s in record.stages)
    usage = f"Usage: bash {prog} [--stages A,B]" + (" [--rows s1,s2]" if rows else "")
    rows_case = {
        True: '    --rows) ONLY_ROWS="${2//,/ }"; shift 2;;',
        False: '    --rows) die "a linear pipeline has one implicit row; --rows does not apply";;',
        None: '    --rows) die "every samplesheet row is one array task; select stages with --stages, or run_local.sh --rows";;',
    }[rows]
    return f"""STAGES="{stages}"
ONLY_STAGES=""
ONLY_ROWS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stages) ONLY_STAGES="${{2//,/ }}"; shift 2;;
{rows_case}
    -h|--help) echo "{usage}"; echo "stages, in order: $STAGES"; exit 0;;
    *) die "unknown argument $1 — {usage}";;
  esac
done
SELECTED=""
for s in $STAGES; do
  if [ -z "$ONLY_STAGES" ]; then SELECTED="$SELECTED $s"; else case " $ONLY_STAGES " in *" $s "*) SELECTED="$SELECTED $s";; esac; fi
done
for s in $ONLY_STAGES; do case " $STAGES " in *" $s "*) ;; *) die "unknown stage $s (stages: $STAGES)";; esac; done
"""


# ── stages/NN_<stage>.sbatch ────────────────────────────────────────────────


def _apptainer_module(env: Optional[Mapping]) -> Optional[str]:
    if env is None:
        return None
    return compute_access.get_container_modules(env).get("apptainer_module")


def _render_stage_sbatch(record: PipelineRecord, stage: PipelineStage, expects: list[str],
                         env: Optional[Mapping]) -> str:
    request, notes = _stage_request(stage)
    if env is not None:
        merged, email, _ = _resolve_slurm_and_email(request, env)
        module = _apptainer_module(env)
    else:
        merged, email, module = dict(request), "", None
    slurm_v = _check_slurm(merged)
    email_v = _check_email(email)
    if module:
        _check_module("apptainer_module", module)
    base = _stage_base(stage)
    prog = f"{base}.sbatch"
    nv = "--nv " if slurm_v["gpus"] > 0 else ""
    per_row = record.samplesheet is not None
    env_args = " ".join(f'{v}="${v}"' for v in expects)

    L = ["#!/usr/bin/env bash",
         _header(record, f"stage {stage.index + 1}/{len(record.stages)} {stage.name} cluster job"),
         _render_sbatch_header(stage.name, slurm_v, email_v).rstrip("\n")]
    L += notes
    if per_row:
        L.append("# a job ARRAY, one task per samplesheet row: the driver passes --array=1-N (N is only known at submit)")
    L += ["set -euo pipefail"]
    if module:
        L += ["module purge", f"module load {module}"]
    L += ["",
          "# SLURM stages this script into its spool dir, so $0 is useless there; the submit dir is",
          "# the pipeline dir (run_all.sh submits from it). Outside SLURM, stages/../ is the pipeline dir.",
          'cd "${SLURM_SUBMIT_DIR:-$(dirname "$(dirname "$(readlink -f "$0")")")}"',
          'PIPELINE_DIR="$PWD"',
          'RUN_ID="${RUN_ID:-manual}"   # run_all.sh exports it; a hand-run sbatch records under runs/manual/',
          _sh_checks(prog).rstrip("\n"),
          '[ -f params.env ] || die "params.env not found in $PWD; submit from the pipeline dir (run_all.sh does)"',
          "source ./params.env",
          f'[ -n "${{SIF_{stage.name}:-}}" ] || die "SIF_{stage.name} is empty in params.env: build the .sif from image digest {stage.image_digest or "unrecorded"} and set its absolute path"']
    L += _sh_param_checks(record, indent="")
    L.append("")
    if per_row:
        L += [_sh_sheet(record).rstrip("\n"),
              '[ -n "${SLURM_ARRAY_TASK_ID:-}" ] || die "this stage is a job array (one task per samplesheet row): submit it with --array=1-N, as run_all.sh does"',
              "sheet_columns",
              'sheet_row "$SLURM_ARRAY_TASK_ID"']
        L += _sh_row_checks(record, indent="")
        L.append(f'{_OUTPUT_VAR}="$RESULTS_DIR/$SAMPLE"')
    else:
        L += [f'ROW="{_LINEAR_ROW}"', f'{_OUTPUT_VAR}="$RESULTS_DIR"']
    L += ["",
          _sh_binds(record, "--bind").rstrip("\n"),
          "set_binds",
          f'mkdir -p "${_OUTPUT_VAR}" "runs/$RUN_ID"',
          "",
          "# --cleanenv: the image's own environment, nothing from the login node; the variables the",
          "# stage expects are passed explicitly on the line. --pwd: the row's working directory.",
          "set +e",
          f'apptainer exec {nv}--cleanenv "${{BIND[@]}}" --pwd "${_OUTPUT_VAR}" "$SIF_{stage.name}" \\']
    if env_args:
        L.append(f"  env {env_args} \\")
    L += [f'  bash "$PIPELINE_DIR/stages/{base}.sh"',
          "rc=$?",
          "set -e",
          f'echo "$rc" > "runs/$RUN_ID/{stage.name}.$ROW.rc"',
          'exit "$rc"']
    return "\n".join(L) + "\n"


# ── run_local.sh ────────────────────────────────────────────────────────────


def _render_run_local(record: PipelineRecord, bind: Mapping[str, str]) -> str:
    per_row = record.samplesheet is not None
    L = ["#!/usr/bin/env bash",
         _header(record, "local runner (docker)"),
         "# Runs every row × stage in order, each stage INSIDE its frozen image. Nothing is deleted.",
         "set -euo pipefail",
         'PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
         'cd "$PIPELINE_DIR"',
         _sh_checks("run_local.sh").rstrip("\n"),
         _sh_args(record, "run_local.sh", rows=per_row).rstrip("\n"),
         "source ./params.env",
         ""]
    if per_row:
        L += [_sh_sheet(record).rstrip("\n"), ""]
    L += [_sh_preflight(record, "IMAGE").rstrip("\n"),
          "",
          _sh_binds(record, "-v").rstrip("\n"),
          "",
          "# One function per stage: the literal docker line, variables passed explicitly (-e), the",
          "# stage script run from its own host path inside the image, the row's dir as the workdir."]
    for st in record.stages:
        base = _stage_base(st)
        _script, expects = _render_stage_script(record, st, bind)
        env_flags = " ".join(f'-e {v}="${v}"' for v in expects)
        gpu = "--gpus all " if st.resources.gpus > 0 else ""
        L += [f"stage_{st.name}() {{",
              f'  docker run --rm {gpu}"${{BIND[@]}}" -w "${_OUTPUT_VAR}" \\']
        if env_flags:
            L.append(f"    {env_flags} \\")
        L += [f'    "$IMAGE_{st.name}" bash "$PIPELINE_DIR/stages/{base}.sh"',
              "}"]
    L += ["",
          "run_stage() {   # $1 = stage; OUTPUT_DIR, ROW and BIND are set for the row",
          "  local rc",
          f'  mkdir -p "${_OUTPUT_VAR}"',
          f'  echo "== $1 / $ROW  (${_OUTPUT_VAR})"',
          '  set +e; "stage_$1"; rc=$?; set -e',
          '  echo "$rc" > "$RUN_DIR/$1.$ROW.rc"',
          '  [ "$rc" -eq 0 ] || die "stage $1 failed for row $ROW (rc=$rc); exit codes so far are under $RUN_DIR"',
          "}",
          "",
          "main() {",
          "  preflight",
          '  RUN_ID="$(date +%Y%m%d_%H%M%S)"',
          '  RUN_DIR="$PIPELINE_DIR/runs/$RUN_ID"',
          '  mkdir -p "$RUN_DIR"',
          '  cp params.env "$RUN_DIR/params.env"']
    if per_row:
        L += ['  cp "$SAMPLESHEET" "$RUN_DIR/samples.csv"',
              '  echo "run $RUN_ID: results under $RESULTS_DIR/<sample>/, exit codes under $RUN_DIR"',
              "  local i s",
              "  for ((i = 1; i <= NROWS; i++)); do",
              '    sheet_row "$i"',
              '    if [ -n "$ONLY_ROWS" ]; then case " $ONLY_ROWS " in *" $SAMPLE "*) ;; *) continue;; esac; fi',
              f'    {_OUTPUT_VAR}="$RESULTS_DIR/$SAMPLE"',
              "    set_binds",
              '    for s in $SELECTED; do run_stage "$s"; done',
              "  done"]
    else:
        L += ['  echo "run $RUN_ID: results under $RESULTS_DIR/, exit codes under $RUN_DIR"',
              "  local s",
              f'  ROW="{_LINEAR_ROW}"',
              f'  {_OUTPUT_VAR}="$RESULTS_DIR"',
              "  set_binds",
              '  for s in $SELECTED; do run_stage "$s"; done']
    L += ['  echo "done: run $RUN_ID"',
          "}",
          'main "$@"']
    return "\n".join(L) + "\n"


# ── run_all.sh ──────────────────────────────────────────────────────────────


def _render_run_all(record: PipelineRecord) -> str:
    per_row = record.samplesheet is not None
    L = ["#!/usr/bin/env bash",
         _header(record, "cluster driver (SLURM + Apptainer)"),
         "# Submits one job per stage, in order, each waiting on the previous (afterok), then",
         "# returns. Nothing is polled and nothing is deleted.",
         "set -euo pipefail",
         'PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
         'cd "$PIPELINE_DIR"',
         _sh_checks("run_all.sh").rstrip("\n"),
         _sh_args(record, "run_all.sh", rows=None).rstrip("\n"),
         "source ./params.env",
         ""]
    if per_row:
        L += [_sh_sheet(record).rstrip("\n"), ""]
    L += [_sh_preflight(record, "SIF").rstrip("\n"),
          "",
          "submit() {   # $1 = stage, $2 = the job it waits on (may be empty); prints the job id",
          "  local script dep=() out jid",
          '  case "$1" in']
    for st in record.stages:
        L.append(f"    {st.name}) script=stages/{_stage_base(st)}.sbatch;;")
    L += ['    *) die "no job script for stage $1";;',
          "  esac",
          '  [ -z "$2" ] || dep=(--dependency="afterok:$2")',
          '  out="$(sbatch --parsable --export="ALL,RUN_ID=$RUN_ID" \\',
          '    --output="$RUN_DIR/%x-%j.out" --error="$RUN_DIR/%x-%j.err" \\']
    if per_row:
        L.append('    --array="1-$NROWS" ${dep[@]+"${dep[@]}"} "$script")"')
    else:
        L.append('    ${dep[@]+"${dep[@]}"} "$script")"')
    L += ['  jid="${out%%;*}"',
          """  case "$jid" in ""|*[!0-9]*) die "sbatch for $1 printed '$out', not a job id";; esac""",
          '  echo "$jid"',
          "}",
          "",
          "main() {",
          "  preflight",
          '  RUN_ID="$(date +%Y%m%d_%H%M%S)"',
          '  RUN_DIR="$PIPELINE_DIR/runs/$RUN_ID"',
          '  mkdir -p "$RUN_DIR"',
          '  cp params.env "$RUN_DIR/params.env"']
    if per_row:
        L.append('  cp "$SAMPLESHEET" "$RUN_DIR/samples.csv"')
    size = '"$NROWS"' if per_row else '"-"'
    L += ["  printf 'stage\\tjob_id\\tarray_size\\n' > \"$RUN_DIR/jobs.tsv\"",
          '  local prev="" jobs="" jid',
          "  for s in $SELECTED; do",
          '    jid="$(submit "$s" "$prev")"',
          f"    printf '%s\\t%s\\t%s\\n' \"$s\" \"$jid\" {size} >> \"$RUN_DIR/jobs.tsv\"",
          ('    echo "submitted $s as job $jid (array 1-$NROWS${prev:+, after job $prev})"' if per_row
           else '    echo "submitted $s as job $jid${prev:+ (after job $prev)}"'),
          '    prev="$jid"; jobs="$jobs${jobs:+,}$jid"',
          "  done",
          '  echo "run $RUN_ID submitted; jobs.tsv, logs and exit codes land under $RUN_DIR"',
          '  echo "status:  sacct -j $jobs --format=JobID%20,JobName,State,Elapsed,ExitCode"',
          ('  echo "results: $RESULTS_DIR/<sample>/"' if per_row else '  echo "results: $RESULTS_DIR/"'),
          "}",
          'main "$@"']
    return "\n".join(L) + "\n"


# ── HOWTO.md ────────────────────────────────────────────────────────────────


def _prose(artifact: str) -> str:
    """`{SAMPLE}.counts.tsv` → `<sample>.counts.tsv` for a sentence."""
    return _PLACEHOLDER_RE.sub(lambda m: f"<{m.group(1).lower()}>", artifact)


def _render_howto(record: PipelineRecord, bind: Mapping[str, str], env: Optional[Mapping]) -> str:
    per_row = record.samplesheet is not None
    last = record.stages[-1].name
    module = _apptainer_module(env)
    results = "`results/<sample>/`" if per_row else "`results/`"
    L = [f"<!-- {record.name} — HOWTO — rendered from sealed workflow {record.sealed_workflow} — {_EDIT_NOTE} -->",
         f"# {record.name}",
         "",
         f"A pipeline rendered from the sealed workflow **{record.sealed_workflow}**. Its stages are "
         f"the sealed how-to's commands, its containers are the images the seal validated in, and "
         f"its defaults are the values the sealed run used. Plain form: bash on a laptop, SLURM job "
         f"arrays on a cluster. Nothing is deleted automatically.",
         "",
         "## Stages",
         "",
         "| # | stage | runs | needs from earlier stages | writes |",
         "|---|-------|------|---------------------------|--------|"]
    for st in record.stages:
        needs = ", ".join(f"`{_prose(i.artifact or '')}` ({i.from_stage})"
                          for i in st.inputs if i.origin == "stage") or "—"
        writes = ", ".join(f"`{_prose(o.artifact)}`" + (" *(published)*" if o.published else "")
                           for o in st.outputs) or "—"
        cmd = _bind(" && ".join(st.commands), bind, st.name).replace("|", "\\|")
        L.append(f"| {st.index + 1} | {st.name} | `{cmd}` | {needs} | {writes} |")
    L += ["",
          f"Every stage of a row runs in the row's own working directory ({results}), the way the "
          f"sealed how-to ran; artifacts pass between stages by name inside it. A stage refuses "
          f"(exit {_MISSING_INPUT_RC}) when an artifact it needs is not there yet.",
          "",
          "## Run locally (docker)",
          "",
          "1. Edit `params.env`: point every path at your data (absolute paths)."
          + (" Edit `samples.csv`: one row per sample, the header names the columns." if per_row else ""),
          "2. `bash run_local.sh` — every " + ("row × " if per_row else "") + "stage in order, each stage "
          "inside its `IMAGE_<STAGE>` via `docker run`; the stages dir, the results dir and the parent "
          "dir of every input are bound at their own host paths; variables are passed with `-e`.",
          f"3. Exit codes land in `runs/<timestamp>/<STAGE>.<row>.rc` beside a copy of the inputs "
          f"you ran with; the run stops at the first failure, naming the stage and row.",
          "",
          "## Run on the cluster (SLURM + Apptainer)",
          "",
          "1. Build each stage's `.sif` from the image digest named in `params.env` (`stage_apptainer_image` "
          "does this for a frozen env) and set `SIF_<STAGE>` to its absolute cluster path.",
          "2. `bash run_all.sh` — one job per stage, " + ("a job array with one task per samplesheet row, "
          if per_row else "") + "each chained to the previous with `--dependency=afterok`; the driver "
          "returns at once and prints the `sacct` line to watch."
          + (f" Jobs load `{module}` before `apptainer exec`." if module
             else " The jobs carry no `module load`: render with a compute env to add it."),
          "3. `runs/<timestamp>/jobs.tsv` lists stage, job id and array size; logs and `.rc` files land "
          "beside it. A stage that fails leaves the jobs after it pending forever (afterok can no longer "
          "be satisfied) — `scancel` them.",
          "",
          "Each job's `#SBATCH` request is the stage's declared resources; an unsized stage carries "
          "`DEFAULT_STAGE_REQUEST` and says `DEFAULT request — not sized for your data`, with the "
          "measurement from the sealed run beside it. Measurements are quoted, never used as the request.",
          "",
          "## Re-run one stage",
          "",
          f"`bash run_local.sh --stages {last}" + (" --rows <sample>" if per_row else "")
          + f"` or `bash run_all.sh --stages {last}`. Stages are selected by name (`--stages A,B`, "
          f"in pipeline order: {', '.join(s.name for s in record.stages)}); a selected stage checks "
          f"that its inputs from earlier stages already exist in the row's directory.",
          "",
          "## Where results land",
          "",
          (f"{results} — one directory per row under `RESULTS_DIR`" if per_row
           else f"{results} — `RESULTS_DIR` itself, the one implicit row's working directory")
          + " (default `<pipeline dir>/results`; export `RESULTS_DIR` to move it). Published outputs:",
          ""]
    for st in record.stages:
        pub = [_prose(o.artifact) for o in st.outputs if o.published]
        if pub:
            L.append(f"- {st.name}: {', '.join(f'`{p}`' for p in pub)}")
    L += ["",
          "## params.env",
          "",
          "- one line per shared parameter — the default is the value the sealed run was validated "
          "with (test data), so a first run reproduces the seal and a real run needs your paths",
          "- `RESULTS_DIR` (absolute)" + (", `SAMPLESHEET`" if per_row else "")
          + (" — and, this being a one-row pipeline, its per-sample inputs, which have no default" if
             any(p.kind == "per_sample" for p in _params_env_entries(record)) else ""),
          "- `IMAGE_<STAGE>` for docker and `SIF_<STAGE>` for the cluster, one pair per stage",
          "- values may not contain whitespace or shell metacharacters; every runner checks every "
          "value, the existence of every path and the columns of the sheet before it starts anything",
          "",
          "## Cleanup",
          "",
          f"Nothing is deleted automatically. `rm -rf results runs` (from the pipeline dir) is yours to run.",
          ""]
    return "\n".join(L)


__all__ = ["render_plain"]
