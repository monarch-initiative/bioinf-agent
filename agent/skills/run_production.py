"""
run_production — run a RENDERED pipeline in production on a compute env.

ONE verb, `run_production_pipeline(project, env, pipeline, run_dir, …)`. The rendered
pipeline directory — what `render_pipeline` wrote under <workspace>/pipelines/<name>/ —
is copied into `run_dir` on the env named, the caller's samplesheet becomes its
samples.csv, and the run starts the one way a rendered pipeline runs: `sbatch
launcher.sh` on an ssh env, `nextflow run main.nf -profile local …` in the background on
a local env. The submission is documented in a manifest and the call returns; it polls
nothing (`cluster_job_status` / `check_job` do) and seals nothing.

Before anything is copied or launched it checks, AT THE LOCUS: every image the stages run
in is there (the staged .sif on a cluster, the docker image on a laptop); every path the
run binds — the shared path parameters and every path cell of the samplesheet — exists;
and the shared references still hash to what the workflow was SEALED against where a
sealed anchor exists. A reference that diverged launches, reported as `degraded`, never
as a bare success; so does a value the seal's self-test never ran the how-to with
(`value_check`).

A run directory is Nextflow's launch directory, and `-resume` only works from the same
one. The first launch creates it (on a cluster, as one transfer of the whole directory);
a second call naming a `run_dir` that holds all of this pipeline launches there again
without re-copying (the samples.csv in place is the one used). Any other existing
directory is refused: one holding a different render, one holding part of this one, or
one holding no run at all.
"""
from __future__ import annotations

import csv
import glob
import io
import os
import base64
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Collection

import yaml

from agent.models import core_data
from agent.skills import cluster_jobs, compute_access, data_pins, stage_apptainer, submit_workflow, transfer, workspace
from agent.skills.container_build import image_present as _docker_image_present
from agent.skills.outcomes import proven, refused, broke, degraded
from agent.skills.pipeline_record import PipelineRecord, load_pipeline_record
from agent.skills.pipeline_render import (MANIFEST_PATH, RECORD_PATH, SAMPLESHEET_FILENAME,
                                          _edited_since_render, parse_manifest)
from agent.skills.pipeline_render_nextflow import OUTDIR_PARAM, RUN_LOCAL, SAMPLESHEET_PARAM, params_yaml_line
from agent.skills.snapshot import _SEEN_FN, _ssh_argv, _ssh_failure_hint

#: A params= value: a path, a word, a number. Nextflow pastes params into the pipeline's
#: shell commands, so a value never carries a character a shell would act on.
_VALUE_RE = re.compile(r"^[A-Za-z0-9_./+:,=@%-]{0,4096}$")
#: A SLURM walltime: minutes, M:S, H:M:S, D-H, D-H:M, D-H:M:S.
_WALLTIME_RE = re.compile(r"^(\d+|\d+:\d{2}|\d+:\d{2}:\d{2}|\d+-\d+|\d+-\d+:\d{2}|\d+-\d+:\d{2}:\d{2})$")
_SAMPLE_KEY = "sample"
_SAMPLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CSV_BREAKERS = re.compile(r'[,"\r\n]')
_PARAMS_FILE = "params.yaml"
_LAUNCHER = "launcher.sh"
#: What a launch reads of a run directory beyond which files are there: the manifest
#: that names its render, the two files a launch's params= and walltime= go into, and
#: the samplesheet a re-launch runs.
_RUN_TEXTS = (MANIFEST_PATH, _PARAMS_FILE, _LAUNCHER, SAMPLESHEET_FILENAME)
_REMOTE_CHECK_CHUNK = 400


class _Refusal(Exception):
    """A gate said no before anything was copied or launched; carries the tagged return."""
    def __init__(self, result: dict):
        super().__init__(str(result.get("error", "")))
        self.result = result


def _refuse(code: str, error: str, **fields) -> _Refusal:
    return _Refusal(refused(code, success=False, error=error, **fields))


# ---------------------------------------------------------------------------
# The rendered pipeline
# ---------------------------------------------------------------------------

def _load_pipeline(pipeline: str) -> tuple[PipelineRecord, Path, dict[str, str], list[str]]:
    """The record, its directory, the manifest {relative path: sha256} and the files
    edited since the render. `pipeline` is a name under the pipelines zone or an absolute
    path to a rendered directory."""
    if not isinstance(pipeline, str) or not pipeline.strip():
        raise _refuse("run_production.pipeline_required",
                      "name a rendered pipeline, or give its directory",
                      remedy="list_installed_pipelines shows what is rendered; render_pipeline makes one")
    p = Path(pipeline)
    zone = workspace.pipelines_dir()
    pdir = p if p.is_absolute() else zone / pipeline
    if not (pdir / RECORD_PATH).is_file() or not (pdir / MANIFEST_PATH).is_file():
        have = sorted(d.name for d in zone.iterdir() if (d / RECORD_PATH).is_file()) if zone.is_dir() else []
        raise _refuse("run_production.no_rendered_pipeline",
                      f"{pdir} is not a rendered pipeline (no {RECORD_PATH})",
                      rendered_pipelines=have,
                      remedy="render_pipeline(sealed_workflow=…) first, or name one of rendered_pipelines")
    try:
        record = load_pipeline_record(pdir / RECORD_PATH)
    except Exception as e:
        raise _refuse("run_production.pipeline_record_invalid",
                      f"{pdir / RECORD_PATH} is not a pipeline record this system wrote: "
                      f"{type(e).__name__}: {str(e)[:300]}",
                      remedy="re-render the pipeline")
    manifest = parse_manifest((pdir / MANIFEST_PATH).read_text())
    return record, pdir, manifest, _edited_since_render(pdir, manifest)


def _carried_files(pdir: Path, manifest: Mapping[str, str]) -> list[str]:
    """Relative paths copied into a run directory: every rendered file but the example
    samplesheet, plus the manifest that identifies the render."""
    rels = [rel for rel in sorted(manifest) if rel != SAMPLESHEET_FILENAME and (pdir / rel).is_file()]
    if MANIFEST_PATH not in rels:
        rels.append(MANIFEST_PATH)
    return rels


def _parse_params(text: str, where: str) -> dict:
    """params.yaml's values, as text — the file, not the record, because the file is
    the one a person edits after the render."""
    try:
        raw = yaml.safe_load(text) or {}
    except Exception as e:
        raise _refuse("run_production.params_unreadable",
                      f"{where} is not valid YAML: {type(e).__name__}: {str(e)[:200]}",
                      remedy="fix the file; it is read as `-params-file`")
    if not isinstance(raw, dict):
        raise _refuse("run_production.params_unreadable", f"{where} is not a mapping",
                      remedy="one `key: value` per line")
    return {str(k): ("" if v is None else str(v)) for k, v in raw.items()}


def _check_params(overrides: Optional[Mapping], text: str, where: str) -> None:
    """Refuse a params= key the pipeline lacks, or a value that is not a path, a word
    or a number — before anything is asked of the env."""
    allowed = set(_parse_params(text, where)) - {SAMPLESHEET_PARAM[0]}
    for key, value in (overrides or {}).items():
        k, sv = str(key), str(value)
        if k not in allowed:
            raise _refuse("run_production.unknown_param", f"{k!r} is not a parameter of this pipeline",
                          params=sorted(allowed),
                          remedy=f"params= takes the keys of {_PARAMS_FILE}; the samplesheet is always "
                                 f"{SAMPLESHEET_FILENAME} in the run directory")
        if not _VALUE_RE.match(sv):
            raise _refuse("run_production.unsafe_param_value",
                          f"params[{k!r}] is not a path, a word or a number: {sv[:80]!r}",
                          remedy=f"edit {_PARAMS_FILE} in the run directory for anything else")


def _with_params(text: str, overrides: Optional[Mapping], where: str) -> tuple[str, dict]:
    """params.yaml with each override written onto its key's own line, every other
    line (comments included) as it was, and the values it then holds. A key that is
    not on one `key: value` line of its own is refused, as is a file that does not
    read back each override as written."""
    _check_params(overrides, text, where)
    lines = text.splitlines(keepends=True)
    for key, value in (overrides or {}).items():
        k = str(key)
        at = [i for i, line in enumerate(lines) if line.split(":", 1)[0] == k]
        if len(at) == 1:
            lines[at[0]] = params_yaml_line(k, str(value)) + "\n"
        try:
            reads_back = len(at) == 1 and _parse_params("".join(lines), where).get(k) == str(value)
        except _Refusal:
            reads_back = False          # the line was part of a longer value: the edit broke it
        if not reads_back:
            raise _refuse("run_production.param_line_not_found",
                          f"{where} does not hold {k!r} on one `{k}: value` line of its own",
                          remedy=f"set it in {where} by hand, or launch without params[{k!r}]")
    return "".join(lines), _parse_params("".join(lines), where)


_TIME_LINE_RE = re.compile(r"(?m)^#SBATCH --time=(\S*)")


def _with_walltime(launcher: str, walltime: str, where: str) -> str:
    """launcher.sh with the manager job's time limit set to `walltime`."""
    text, n = _TIME_LINE_RE.subn(f"#SBATCH --time={walltime}", launcher, count=1)
    if not n:
        raise _refuse("run_production.launcher_without_time", f"{where} has no `#SBATCH --time=` line",
                      remedy=f"add one to {where} by hand, or launch without walltime=")
    return text


def _read_samplesheet(path: str, record: PipelineRecord) -> tuple[list[str], list[dict]]:
    """The caller's samplesheet, checked against the columns the pipeline reads."""
    sp = Path(path)
    if not sp.is_file():
        raise _refuse("run_production.samplesheet_missing", f"samplesheet {path!r} is not a file on this machine",
                      remedy="give the local path of a CSV with one row per sample")
    return _check_samplesheet(sp.read_text(encoding="utf-8"), record, path)


def _check_samplesheet(text: str, record: PipelineRecord, where: str) -> tuple[list[str], list[dict]]:
    """A samplesheet's header and rows, refused unless every column the pipeline reads
    is there and every cell can be run: the sample names a directory and the files
    under it, a bound value is pasted into the stage's shell command (the rule params=
    values follow), a path is absolute and plain, and no cell holds what splits a CSV
    row without quoting (Nextflow reads samples.csv that way)."""
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]
    need = [c.name for c in record.samplesheet.columns]
    missing = [c for c in need if c not in header]
    if missing:
        raise _refuse("run_production.samplesheet_columns",
                      f"samplesheet {where} lacks column(s) {missing}; the pipeline reads {need}",
                      required_columns=need, remedy="add the column(s); columns beyond these are carried through")
    if not rows:
        raise _refuse("run_production.samplesheet_empty", f"samplesheet {where} has no rows",
                      remedy="one row per sample under the header line")
    ids = [r.get(_SAMPLE_KEY, "") for r in rows]
    if any(not s for s in ids) or len(set(ids)) != len(ids):
        raise _refuse("run_production.samplesheet_sample_ids",
                      "every row needs a unique, non-empty `sample`",
                      remedy="`sample` is the row key: it tags every task and names results/<sample>/")
    for col in record.samplesheet.columns:
        cells = [r.get(col.name, "") for r in rows]
        if col.name == _SAMPLE_KEY:
            bad, rule = [c for c in cells if not _SAMPLE_RE.match(c)], "letters, digits and ._- (it names a directory)"
        elif col.value_kind in ("path", "prefix"):
            relative = [c for c in cells if not c.startswith("/")]
            if relative:
                raise _refuse("run_production.samplesheet_relative_path",
                              f"column {col.name!r} must hold absolute paths at the locus; got {relative[:3]}",
                              remedy="absolute paths for data: a run resolves relative ones against its launch directory")
            bad, rule = [c for c in cells if not cluster_jobs._ABS_SAFE_PATH_RE.match(c)], "an absolute path of letters, digits and ._/+-"
        elif not col.read_by:
            bad, rule = [c for c in cells if not _VALUE_RE.match(c)], "a path, a word or a number (it is pasted into a command)"
        else:
            bad, rule = [c for c in cells if _CSV_BREAKERS.search(c)], "no comma, quote or line break"
        if bad:
            raise _refuse("run_production.samplesheet_unsafe_cell",
                          f"samplesheet {where}: column {col.name!r} holds {bad[:3]}; each cell must be {rule}",
                          remedy=f"rename or move the value(s) so column {col.name!r} holds {rule}")
    return header, rows


def _bound_paths(record: PipelineRecord, effective: Mapping[str, str], rows: list[dict]) -> tuple[dict[str, str], list[str], list[str]]:
    """What the run binds at the locus: {param key: absolute path} for the shared path
    parameters, the prefixes (an index family) among them, and every path cell of the
    samplesheet. A relative parameter value names a file inside the run directory (a
    carried script) and is not checked at the locus."""
    shared: dict[str, str] = {}
    prefixes: list[str] = []
    for p in record.params:
        if p.kind != "shared" or p.value_kind not in ("path", "prefix"):
            continue
        key = p.name.lower()
        val = effective.get(key, "")
        if not val.startswith("/"):
            continue
        shared[key] = val
        if p.value_kind == "prefix":
            prefixes.append(val)
    cells: list[str] = []
    path_cols = [c.name for c in record.samplesheet.columns if c.value_kind == "path"]
    for r in rows:
        for c in path_cols:
            v = r.get(c, "")
            if v and v not in cells:
                cells.append(v)
    return shared, prefixes, cells


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

_ZONE_TARGET = {"scratch": compute_access.get_agent_scratch_target,
                "pipelines": compute_access.get_agent_pipelines_target}


def _results_dir(run_dir: str, effective: Mapping[str, str]) -> str:
    """Where the run publishes: params.yaml's outdir, resolved against the run
    directory Nextflow starts in."""
    return os.path.normpath(os.path.join(run_dir, effective.get(OUTDIR_PARAM[0], OUTDIR_PARAM[1])))


def _authorize_run_dir(project: dict, env: dict, env_name: str, run_dir: str) -> str:
    """The zone `run_dir` falls in, with upload AND exec checked for it: a run writes
    files there and the job runs there."""
    zone = transfer._classify_zone_and_authorize(
        project=project, env=env, remote_abs_path=f"{run_dir}/main.nf", op="upload", primitive_name="upload")
    name = zone["zone"]
    if name in ("common_data", "container_upload"):
        raise _refuse("run_production.run_dir_zone",
                      f"{run_dir} is in the env's {name} zone, which holds reference data and images, not runs",
                      remedy="use the env's pipelines zone, the agent's scratch, or a directory the project grants with upload+exec")
    if name == "project_path":
        compute_access.check_permission(project, env_name, run_dir, "run_production_pipeline")
    else:
        compute_access.check_env_target_capability(project, env_name, _ZONE_TARGET[name](env),
                                                   "run_production_pipeline", zone["auth_target"])
    return name


# ---------------------------------------------------------------------------
# The locus: images, paths, the data pins
# ---------------------------------------------------------------------------

def _stage_images(record: PipelineRecord) -> list[dict]:
    """One entry per distinct image the stages run in."""
    seen: dict[str, dict] = {}
    for s in record.stages:
        key = s.image or s.name
        if key not in seen:
            seen[key] = {"image": s.image, "sif_path": s.sif_path, "request_key": s.request_key,
                         "env_name": s.env_name, "stages": []}
        seen[key]["stages"].append(s.name)
    return list(seen.values())


def _check_images_local(record: PipelineRecord) -> None:
    missing = [im for im in _stage_images(record) if not im["image"] or not _docker_image_present(im["image"])]
    if missing:
        raise _refuse("run_production.image_missing",
                      f"{len(missing)} image(s) the pipeline runs in are not in the local Docker daemon: "
                      f"{[im['image'] for im in missing]}",
                      missing_images=missing,
                      remedy="`docker load -i <workspace>/environments/<env>/<env>.tar`, or re-run freeze for the env")


def _check_images_cluster(record: PipelineRecord, env: dict, env_name: str, project_name: str, timeout: int) -> None:
    images = _stage_images(record)
    unrendered = [im for im in images if not im["sif_path"]]
    if unrendered:
        raise _refuse("run_production.rendered_without_cluster",
                      f"the pipeline was rendered without a .sif path for {[im['image'] for im in unrendered]}",
                      remedy=f"render_pipeline(…, env={env_name!r}) so nextflow.config names the staged images")
    zone = str((compute_access.get_container_upload_target(env) or {}).get("path") or "").rstrip("/")
    elsewhere = [im for im in images
                 if zone and not (im["sif_path"] == zone or im["sif_path"].startswith(zone + "/"))]
    if elsewhere:
        # Staging writes into the env's CURRENT container zone; a render made before that
        # zone moved names paths nothing will ever stage to, so "stage it" would loop.
        cohort = ", cohort=[…] as before (see .pipeline/pipeline.yaml)" if record.cohort_workflows else ""
        raise _refuse("run_production.rendered_for_other_zone",
                      f"the pipeline was rendered when {env_name!r} kept its images elsewhere: "
                      f"{[im['sif_path'] for im in elsewhere]} are not under its container zone {zone!r}",
                      sif_paths=[im["sif_path"] for im in elsewhere], container_zone=zone,
                      remedy=f"re-render so nextflow.config names the current zone: render_pipeline("
                             f"sealed_workflow={record.sealed_workflow!r}, name={record.name!r}, "
                             f"env={env_name!r}, overwrite=True{cohort})")
    not_staged = [im for im in images
                  if not stage_apptainer._remote_sif_exists(env, im["sif_path"], timeout=min(timeout, 120))]
    if not_staged:
        calls = [f"stage_apptainer_image(project_name={project_name!r}, compute_env_name={env_name!r}, "
                 f"freeze_request_key={im['request_key']!r})" for im in not_staged]
        raise _refuse("run_production.sif_not_staged",
                      f"{len(not_staged)} image(s) are not staged on {env_name!r}: "
                      f"{[im['sif_path'] for im in not_staged]}",
                      not_staged=not_staged, remedy="stage each first: " + "; ".join(calls))


def _check_paths_local(paths: list[str], prefixes: list[str]) -> None:
    missing = [p for p in paths if not os.path.exists(p)]
    missing += [p for p in prefixes if not glob.glob(p + "*")]
    if missing:
        raise _refuse("run_production.inputs_missing",
                      f"{len(missing)} bound path(s) do not exist on this machine: {missing[:5]}"
                      + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""),
                      missing_inputs=missing, locus="local",
                      remedy="fix the samplesheet or params.yaml; a prefix names a family of files (<prefix>*)")


def _remote_prefixes_exist(env: dict, prefixes: list[str], *, timeout: int) -> dict:
    """{ok, missing} for index-family prefixes: a prefix exists when `<prefix>*` matches."""
    clean = [p for p in prefixes if p]
    if not clean:
        return {"ok": True, "missing": []}
    bad = [p for p in clean if not cluster_jobs._ABS_SAFE_PATH_RE.match(p)]
    if bad:
        raise _refuse("run_production.unsafe_input_path",
                      f"path(s) hold characters a remote check cannot carry: {bad[:5]}")
    checks = _SEEN_FN + "; ".join(f'fresh "$(dirname {shlex.quote(p)})"; compgen -G {shlex.quote(p + "*")} > /dev/null '
                                  f'|| echo MISSING:{shlex.quote(p)}' for p in clean)
    argv = _ssh_argv(env, f"bash -lc {shlex.quote(checks)}")
    res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        raise _Refusal(broke("run_production.input_check_ssh_failed",
                             error=f"remote prefix check ssh failed (rc={res.returncode}): "
                                   f"{(res.stderr or '').strip()[:300]}", **({"hint": hint} if hint else {})))
    missing = [ln.split("MISSING:", 1)[1] for ln in (res.stdout or "").splitlines() if ln.startswith("MISSING:")]
    return {"ok": not missing, "missing": missing}


def _check_paths_cluster(env: dict, paths: list[str], prefixes: list[str], *, timeout: int) -> None:
    missing: list[str] = []
    for i in range(0, len(paths), _REMOTE_CHECK_CHUNK):
        res = cluster_jobs.remote_paths_exist(env, paths[i:i + _REMOTE_CHECK_CHUNK], timeout=timeout)
        if res.get("missing_paths"):
            missing += list(res["missing_paths"])
        elif "error" in res:
            raise _Refusal({**res, "code": "run_production.input_check_ssh_failed"} if res.get("outcome") == "broke"
                           else res)
    missing += _remote_prefixes_exist(env, prefixes, timeout=timeout)["missing"]
    if missing:
        raise _refuse("run_production.inputs_missing",
                      f"{len(missing)} bound path(s) do not exist on the cluster: {missing[:5]}"
                      + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""),
                      missing_inputs=missing, locus="cluster",
                      remedy="upload the data (or point the samplesheet / params.yaml at data already there)")


def _load_spec(path: str) -> tuple[Optional[dict], Optional[str]]:
    """(spec, error): a sealed workflow read through the typed seam."""
    from agent.skills.spec_writer import load_workflow_spec
    p = Path(path) if path else None
    if p is None or not p.is_file():
        return None, f"the sealed workflow is not at {path!r}"
    try:
        spec = load_workflow_spec(p)
    except Exception as e:                                      # pragma: no cover
        return None, f"{p.name} is not a valid WorkflowSpec: {type(e).__name__}: {e}"
    return (spec.model_dump() if hasattr(spec, "model_dump") else dict(spec)), None


def _spec_for(record: PipelineRecord, param_key: str) -> str:
    """The sealed workflow a shared parameter belongs to: a cohort's when every stage
    that reads it is that cohort's, else the base workflow's."""
    used_by = {s for p in record.params if p.name.lower() == param_key for s in p.used_by}
    for cw in record.cohort_workflows:
        if used_by and used_by <= set(cw.stages):
            return cw.sealed_workflow_path
    return record.sealed_workflow_path


def _value_check(record: PipelineRecord, effective: Mapping[str, str], rows: list[dict]) -> dict:
    """The value parameters this launch binds (params.yaml values and the samplesheet's
    value columns, never the sample's name) against what the seal's self-test ran the
    how-to with: {status: checked | unrecorded, unproven: [{param, values, proven}]}.
    A value it never ran is a run the shipped how-to was not proven on — legitimate,
    but not one to start without being told; `unrecorded` says the seal kept no
    transcript of its self-test, so there was nothing to compare against."""
    ran_with: dict[str, set[str]] = {}
    for path in {record.sealed_workflow_path, *(cw.sealed_workflow_path for cw in record.cohort_workflows)}:
        spec, _ = _load_spec(path)
        for trial in core_data.usage_proven_trials(spec):
            for slot, value in (trial.get("substitutions") or {}).items():
                ran_with.setdefault(slot, set()).add(str(value))
    if not ran_with:
        return {"status": "unrecorded", "unproven": []}
    bound = {p.name: {effective.get(p.name.lower(), "")} for p in record.params
             if p.kind == "shared" and p.value_kind == "value"}
    bound |= {c.placeholder: {r.get(c.name, "") for r in rows} for c in record.samplesheet.columns
              if rows and c.value_kind == "value" and c.name != _SAMPLE_KEY}
    return {"status": "checked",
            "unproven": [{"param": name, "values": sorted(values - ran_with[name]), "proven": sorted(ran_with[name])}
                         for name, values in sorted(bound.items()) if name in ran_with and values - ran_with[name]]}


def _observe_cluster(env: dict, paths: list[str], *, timeout: int) -> dict:
    """{path: {exists, sha256}} observed on the cluster: the sidecar hash where the
    download job left one, never a hash computed on the head node."""
    from agent.skills.acquire_data import _probe_cluster_path
    observed: dict[str, dict] = {}
    for path in paths:
        probe = _probe_cluster_path(env, path, timeout=timeout)
        if "error" in probe:
            observed[path] = {"exists": None, "sha256": "", "probe_error": probe["error"]}
        else:
            observed[path] = {"exists": bool(probe.get("exists")), "sha256": probe.get("sha256") or ""}
    return observed


def _reference_check(record: PipelineRecord, shared: Mapping[str, str], *, locus: str,
                     env: dict, timeout: int, prefixes: Collection[str] = ()) -> dict:
    """The shared references this run binds against what the workflow was SEALED with.
    Grouped by the sealed workflow each parameter belongs to; one verdict. A `prefixes`
    entry names a family of files (an index), observed as such rather than as one file."""
    if not shared:
        return {"status": data_pins.NOT_ATTEMPTED, "reason": "the pipeline binds no shared path parameter",
                "locus": locus, "findings": []}
    by_spec: dict[str, dict[str, str]] = {}
    for key, path in shared.items():
        by_spec.setdefault(_spec_for(record, key), {})[key] = path
    observed: dict[str, dict] = {}
    if locus == "cluster":
        plain = sorted(set(shared.values()) - set(prefixes))
        observed = _observe_cluster(env, plain, timeout=timeout) if plain else {}
        family = sorted(set(shared.values()) & set(prefixes))
        if family:
            missing = set(_remote_prefixes_exist(env, family, timeout=timeout)["missing"])
            observed.update({p: {"exists": p not in missing, "sha256": ""} for p in family})
    findings: list[dict] = []
    unreadable: list[str] = []
    for spec_path, inputs in by_spec.items():
        spec, err = _load_spec(spec_path)
        if err:
            unreadable.append(err)
            continue
        check = data_pins.check_bound_inputs(
            spec, inputs, locus=locus,
            remote_presence={p: o.get("exists") for p, o in observed.items()},
            remote_sha256={p: o.get("sha256", "") for p, o in observed.items()})
        findings += check["findings"]
    # An artifact the sealed workflow's own step PRODUCED (an index it built, an annotation
    # it fetched) has no external anchor to compare against; say which step, not merely
    # that the path is unknown.
    produced = {p.name.lower(): str(p.source) for p in record.params
                if str(p.source or "").startswith("sealed_step:")}
    for f in findings:
        if f["verdict"] == data_pins.UNANCHORED and f.get("slot") in produced:
            step = produced[f["slot"]].split(":", 1)[1]
            f["reason"] = (f"produced by the sealed workflow's own step {step}; the seal recorded no "
                           f"external anchor for it, so nothing pins the copy at this path")
    counts = {v: sum(1 for f in findings if f["verdict"] == v)
              for v in (data_pins.MATCH, data_pins.DIVERGED, data_pins.UNANCHORED, data_pins.UNVERIFIED)}
    # Four answers, stated apart: a pinned reference that CHANGED (diverged — the run
    # launches, downgraded); a pinned one we could not compare at this locus (unverified
    # — downgraded too, the pin existed); every pinned one matched (verified); and a
    # reference the seal never pinned at all (unanchored — nothing to compare, said so,
    # never rounded up into a verdict either way).
    if counts[data_pins.DIVERGED]:
        status = data_pins.DIVERGED
    elif counts[data_pins.UNVERIFIED]:
        status = data_pins.UNVERIFIED
    elif not findings:
        status = data_pins.NOT_ATTEMPTED
    elif counts[data_pins.MATCH] == len(findings):
        status = data_pins.VERIFIED
    else:
        status = data_pins.UNANCHORED
    out = {"status": status, "findings": findings, "counts": counts, "locus": locus,
           "sealed_workflows": sorted(by_spec)}
    if unreadable:
        out["unreadable"] = unreadable
        if status == data_pins.NOT_ATTEMPTED:
            out["reason"] = "; ".join(unreadable)
    out["summary"] = data_pins.summarize(out) if findings else out.get("reason", "")
    return out


def _disclose(result: dict, reference_check: Mapping, value_check: Mapping) -> dict:
    """Attach the data-pin verdict and the value check, and DOWNGRADE a launch that
    could not stand behind its data or its values. Neither refuses — re-running
    against a newer reference, or in a mode the seal never ran, is legitimate; doing
    it without being told is the failure this closes."""
    if not isinstance(result, dict):                            # pragma: no cover
        return result
    out = {**result, "reference_check": dict(reference_check), "value_check": dict(value_check)}
    if out.get("outcome") != "proven":
        return out
    status = reference_check.get("status")
    rest = {k: v for k, v in out.items() if k not in ("outcome", "code")}
    if status == data_pins.DIVERGED:
        return degraded("run_production.reference_diverged", **rest,
                        reference_divergence=reference_check.get("summary", ""))
    if status == data_pins.UNVERIFIED:
        return degraded("run_production.reference_unverified", **rest,
                        reference_divergence=reference_check.get("summary", ""))
    if value_check.get("unproven"):
        return degraded("run_production.values_unproven", **rest)
    return out


# ---------------------------------------------------------------------------
# The run directory
# ---------------------------------------------------------------------------

def _remote_run_dir_state(env: dict, run_dir: str, rels: list[str], *, timeout: int) -> dict:
    """What `run_dir` holds on the cluster: whether it exists, which of the carried
    files `rels` it lacks and the sha256 of those it holds, whether it holds a
    samplesheet, and the text of each of `_RUN_TEXTS` there ("" when absent). One
    ssh hop."""
    q = shlex.quote
    script = (_SEEN_FN + f'seen {q(run_dir)} && echo EXISTS=1 || echo EXISTS=0; '
              + "".join(f'seen {q(run_dir + "/" + rel)} || echo {q("MISSING=" + rel)}; ' for rel in rels)
              + f'seen {q(run_dir + "/" + SAMPLESHEET_FILENAME)} && echo SHEET=1 || echo SHEET=0; '
              # `echo` after `cat` ends a file that has no last newline before the END line
              + "".join(f'echo {q("BEGIN " + rel)}; cat {q(run_dir + "/" + rel)} 2>/dev/null; echo; '
                        f'echo {q("END " + rel)}; ' for rel in _RUN_TEXTS)
              + f"( cd {q(run_dir)} 2>/dev/null && sha256sum -- {' '.join(q(r) for r in rels)} "
                f"2>/dev/null | sed 's/^/SUM /' ); "
              # The run's own jobs the scheduler still holds: the manager ran in run_dir and
              # its tasks under run_dir/work, by the path as given or as resolved (a link).
              # A scheduler that cannot answer is said so, never read as "nothing runs".
              + f"p=$(cd {q(run_dir)} 2>/dev/null && pwd -P); "
                f"held=$(squeue -h -u \"$USER\" -o '%i %T %Z') || echo SQUEUE_FAILED; "
                f"printf '%s\\n' \"$held\" | awk -v d={q(run_dir)} -v p=\"$p\" "
                f"'$3 == d || index($3, d \"/\") == 1 || (p != \"\" && ($3 == p || index($3, p \"/\") == 1)) "
                f"{{print \"LIVE=\" $1 \" \" $2}}'")
    argv = _ssh_argv(env, f"bash -lc {q(script)}")
    res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        raise _Refusal(broke("run_production.run_dir_probe_failed",
                             error=f"could not inspect {run_dir} on the cluster (rc={res.returncode}): "
                                   f"{(res.stderr or '').strip()[:300]}", **({"hint": hint} if hint else {})))
    lines = (res.stdout or "").splitlines()
    if "SQUEUE_FAILED" in lines:
        raise _Refusal(broke("run_production.run_dir_probe_failed",
                             error=f"the scheduler could not say whether a run of {run_dir} is still "
                                   f"queued or running (squeue failed); a launch must not start over one",
                             remedy="launch again in a minute; `squeue -u $USER` on the login node says "
                                    "whether the scheduler answers"))
    texts = {}
    for rel in _RUN_TEXTS:
        a, b = lines.index(f"BEGIN {rel}"), lines.index(f"END {rel}")
        texts[rel] = "\n".join(lines[a + 1:b])
    sums = {m[2]: m[1] for m in (re.fullmatch(r"SUM ([0-9a-f]{64})  (.+)", ln) for ln in lines) if m}
    return {"exists": "EXISTS=1" in lines,
            "missing": [ln[len("MISSING="):] for ln in lines if ln.startswith("MISSING=")],
            "samplesheet": "SHEET=1" in lines, "texts": texts, "sums": sums,
            "live": [ln[len("LIVE="):] for ln in lines if ln.startswith("LIVE=")]}


def _local_run_dir_state(run_dir: Path, rels: list[str], jobs: list[dict]) -> dict:
    """`_remote_run_dir_state`, for a run directory on this machine; `jobs` are the
    job manager's, and one still running in `run_dir` is a run in progress."""
    here = run_dir.resolve()
    live = [f"{j.get('job_id')} running" for j in jobs
            if j.get("state") == "running" and j.get("working_dir")
            and Path(j["working_dir"]).resolve() == here]
    return {"exists": run_dir.exists(), "missing": [rel for rel in rels if not (run_dir / rel).is_file()],
            "samplesheet": (run_dir / SAMPLESHEET_FILENAME).is_file(),
            "texts": {rel: (run_dir / rel).read_text() if (run_dir / rel).is_file() else ""
                      for rel in _RUN_TEXTS},
            "sums": {rel: core_data.sha256_file(run_dir / rel) for rel in rels
                     if (run_dir / rel).is_file()},
            "live": live}


def _write_remote(env: dict, run_dir: str, texts: Mapping[str, str], *, timeout: int) -> None:
    """Rewrite files of a run directory on the cluster, in one ssh hop: each sent as
    base64 (one line, so any login shell passes it intact), written beside the file
    and moved over it, so a failed write never leaves a file half written."""
    if not texts:
        return
    q = shlex.quote
    script = " && ".join(
        f"printf %s {base64.b64encode(text.encode()).decode()} | base64 -d > {q(path + '.tmp')} && "
        f"mv {q(path + '.tmp')} {q(path)} && echo {q('WROTE=' + rel)}"
        for rel, text in texts.items() for path in [f"{run_dir}/{rel}"])
    res = subprocess.run(_ssh_argv(env, f"bash -lc {q(script)}"), capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        wrote = [ln[len("WROTE="):] for ln in (res.stdout or "").splitlines() if ln.startswith("WROTE=")]
        raise _Refusal(broke("run_production.config_write_failed",
                             error=f"could not rewrite {sorted(set(texts) - set(wrote))} in {run_dir} "
                                   f"(rc={res.returncode}): {(res.stderr or '').strip()[:300]}",
                             rewritten=wrote))


def _reuse_or_refuse(state: Mapping, *, ours: str, run_dir: str, samplesheet: str) -> bool:
    """True when `run_dir` already holds all of THIS render (a re-launch, nothing
    copied); False when it does not exist yet (the first launch creates it). Anything
    else is refused, so a launch never writes over files it did not put there, and
    never starts in a directory its first copy did not finish."""
    manifest = state["texts"][MANIFEST_PATH].strip()
    if state["live"]:
        raise _refuse("run_production.run_in_progress",
                      f"{run_dir} has a run the scheduler still holds (job, state: {state['live']}); a second "
                      f"launch would change what it runs or race it for the directory",
                      remedy="wait for it (cluster_job_status(…, wait_s=300)) or cancel it, then launch again")
    if "main.nf" in state["missing"]:
        if state["exists"]:
            raise _refuse("run_production.run_dir_exists",
                          f"{run_dir} already exists and holds no run of this pipeline",
                          remedy="name a run directory that does not exist yet: the first launch creates it")
        if not samplesheet:
            raise _refuse("run_production.samplesheet_required",
                          f"{run_dir} holds no run yet and no samplesheet was given",
                          remedy="samplesheet=<local CSV>: one row per sample; it becomes the run's samples.csv")
        return False
    if manifest and manifest != ours.strip():
        raise _refuse("run_production.run_dir_holds_another_render",
                      f"{run_dir} already holds a pipeline that is not this render",
                      remedy="use a new run directory, or re-render the pipeline that lives there")
    if state["missing"]:
        raise _refuse("run_production.run_dir_incomplete",
                      f"{run_dir} holds part of this pipeline: it lacks {state['missing'][:5]}",
                      remedy="if the first launch's upload is still running, wait for it "
                             "(globus_task_status) and launch again; otherwise launch into a new "
                             "run directory")
    if samplesheet:
        raise _refuse("run_production.samplesheet_already_there",
                      f"{run_dir} already holds {SAMPLESHEET_FILENAME}; a re-launch resumes with it",
                      remedy="omit samplesheet= to resume, or start the new samples in a new run directory")
    if not state["samplesheet"]:
        raise _refuse("run_production.samplesheet_required",
                      f"{run_dir} holds the pipeline but no {SAMPLESHEET_FILENAME}",
                      remedy="samplesheet=<local CSV> cannot be added to a directory that already holds a run; "
                             "put one there by hand, or use a new run directory")
    return True


def _place_run_files(pdir: Path, rels: list[str], samplesheet: str, run_dir: Path,
                     texts: Mapping[str, str]) -> list[str]:
    """Create `run_dir` with a run's first contents: every carried file (copied with
    its mode, so the render's scripts stay executable; a file in `texts` then holds
    that text instead) and the samplesheet as samples.csv. Returns their relative paths."""
    sources = [(rel, pdir / rel) for rel in rels] + [(SAMPLESHEET_FILENAME, Path(samplesheet))]
    for rel, src in sources:
        dst = run_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for rel, text in texts.items():
        (run_dir / rel).write_text(text)
    return [rel for rel, _ in sources]


def _materialize_remote(*, project_name: str, env: dict, env_name: str, run_dir: str, pdir: Path,
                        rels: list[str], samplesheet: str, reused: bool, changed: Mapping[str, str],
                        access_path: Optional[str], timeout: int) -> dict:
    if reused:
        _write_remote(env, run_dir, changed, timeout=timeout)
        return {"reused": True, "files_uploaded": [], "files_rewritten": sorted(changed)}
    # The run directory is assembled here and sent as ONE transfer, which creates it on
    # the cluster, and only there, under the path the project is authorized to write.
    staged = Path(tempfile.mkdtemp(prefix="run_", dir=transfer.staging_dir("cluster_render_staging")))
    tree = staged / "run"     # made with the usual mode; mkdtemp's 0700 would travel with `scp -p`
    placed = _place_run_files(pdir, rels, samplesheet, tree, changed)
    up = transfer.upload(project_name=project_name, compute_env_name=env_name, local_path=str(tree),
                         remote_abs_path=run_dir, access_path=access_path, timeout=timeout)
    if "error" in up:
        # The staged copy stays: a transfer that stopped waiting may still be reading it.
        raise _Refusal(broke("run_production.upload_failed",
                             error=f"the run directory did not arrive before launch: {up['error']}",
                             staged=str(tree), **({"task_id": up["task_id"]} if up.get("task_id") else {}),
                             remedy="if the transfer is still running (globus_task_status), wait for it "
                                    "to succeed and launch again with the same run_dir and no "
                                    "samplesheet; otherwise launch into a new run directory"))
    shutil.rmtree(staged)
    return {"reused": False, "files_uploaded": [f"{run_dir}/{rel}" for rel in placed]}


def _materialize_local(*, run_dir: str, pdir: Path, rels: list[str], samplesheet: str, reused: bool,
                       changed: Mapping[str, str]) -> dict:
    rd = Path(run_dir)
    if reused:
        for rel, text in changed.items():
            (rd / rel).write_text(text)
        return {"reused": True, "files_copied": [], "files_rewritten": sorted(changed)}
    return {"reused": False,
            "files_copied": [str(rd / rel) for rel in _place_run_files(pdir, rels, samplesheet, rd, changed)]}


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------

def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def run_production_pipeline(project_name: str,
                            compute_env_name: str,
                            pipeline: str,
                            run_dir: str,
                            *,
                            samplesheet: str = "",
                            params: Optional[Mapping] = None,
                            walltime: str = "",
                            access_path: Optional[str] = None,
                            timeout: int = 300,
                            _job_manager=None) -> dict:
    """Run the rendered `pipeline` in production in `run_dir` on `compute_env_name`.

    pipeline:    a name under <workspace>/pipelines/, or a rendered directory's path.
    run_dir:     an absolute path on the env — the launch directory, created by the first
                 launch (on the cluster, as one transfer); it may be in the env's pipelines
                 zone, the agent's scratch, or a directory the project grants with `upload`
                 and `exec`. A directory that already holds this pipeline is re-launched
                 (`-resume`), nothing copied; any other existing directory is refused.
    samplesheet: a local CSV, one row per sample, with the columns the pipeline reads
                 (`sample` first); it becomes the run's samples.csv. Required on the first
                 launch into a directory; refused on a re-launch.
    params:      {params.yaml key: value}, written into the run directory's params.yaml
                 before the launch, so the directory says what runs in it and a later
                 launch, by this tool or by hand, runs the same. A path, a word or a number.
    walltime:    the manager job's SLURM time limit, written into the run directory's
                 launcher.sh header the same way. ssh envs only.
    """
    try:
        if walltime and not _WALLTIME_RE.match(str(walltime)):
            raise _refuse("run_production.bad_walltime", f"walltime {walltime!r} is not a SLURM time",
                          remedy="minutes, MM:SS, HH:MM:SS, D-HH, D-HH:MM or D-HH:MM:SS")
        access = compute_access.load_access(Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)
        env_type = env.get("type")
        if env_type not in ("local", "ssh"):
            raise _refuse("run_production.unknown_env_type",
                          f"compute env {compute_env_name!r} has type={env_type!r}; "
                          f"run_production_pipeline supports 'local' and 'ssh'")
        locus = "cluster" if env_type == "ssh" else "local"
        if walltime and env_type != "ssh":
            raise _refuse("run_production.walltime_on_local",
                          "walltime= sets a cluster manager job's time limit; a local run has none",
                          remedy="launch without walltime=")

        record, pdir, manifest, edited = _load_pipeline(pipeline)
        rels = _carried_files(pdir, manifest)
        normed_dir = submit_workflow._validate_workflow_dir(run_dir)
        zone = _authorize_run_dir(project, env, compute_env_name, normed_dir)
        if env_type == "ssh" and record.compute_env and record.compute_env != compute_env_name:
            raise _refuse("run_production.rendered_for_other_env",
                          f"the pipeline was rendered for compute env {record.compute_env!r}, not "
                          f"{compute_env_name!r}: its nextflow.config names that env's images and modules",
                          remedy=f"render_pipeline(…, env={compute_env_name!r}, name=…) for this env")
        if env_type == "local" and Path(normed_dir).resolve() == pdir.resolve():
            raise _refuse("run_production.run_dir_is_the_template",
                          f"{normed_dir} is the rendered template itself; a run lives in its own directory",
                          remedy="make a new directory for the run; the template stays a template")

        header: list[str] = []
        rows: list[dict] = []
        if samplesheet:
            header, rows = _read_samplesheet(samplesheet, record)
        _check_params(params, (pdir / _PARAMS_FILE).read_text(), str(pdir / _PARAMS_FILE))

        # ─── The run directory, and the configuration that runs there ──
        # params= and walltime= are written into the run's own params.yaml and launcher.sh
        # (a first launch's copies, or a re-launch's files in place), so the directory is
        # the record of what runs in it.
        if env_type == "ssh":
            state = _remote_run_dir_state(env, normed_dir, rels, timeout=timeout)
        else:
            from agent import mcp_server as _ms
            jm = _job_manager or _ms._job_manager
            state = _local_run_dir_state(Path(normed_dir), rels, jm.list_jobs(include_terminated=False))
        reused = _reuse_or_refuse(state, ours=(pdir / MANIFEST_PATH).read_text(), run_dir=normed_dir,
                                  samplesheet=samplesheet)
        if reused:
            # a re-launch runs the samples.csv the run directory holds: held to the same rules
            header, rows = _check_samplesheet(state["texts"].get(SAMPLESHEET_FILENAME, ""), record,
                                              f"{normed_dir}/{SAMPLESHEET_FILENAME}")
            # What runs is the run directory's copy, not the template it came from. The
            # two files a launch writes itself are reported by what they hold (`params`,
            # `walltime`), not as edits.
            edited = sorted(rel for rel, digest in manifest.items()
                            if rel not in (_PARAMS_FILE, _LAUNCHER)
                            and rel in state["sums"] and state["sums"][rel] != digest)
        base = ({rel: state["texts"][rel] for rel in (_PARAMS_FILE, _LAUNCHER)} if reused
                else {rel: (pdir / rel).read_text() for rel in (_PARAMS_FILE, _LAUNCHER)})
        where = {rel: f"{normed_dir}/{rel}" if reused else str(pdir / rel) for rel in base}
        params_text, effective = _with_params(base[_PARAMS_FILE], params, where[_PARAMS_FILE])
        config = {_PARAMS_FILE: params_text,
                  _LAUNCHER: (_with_walltime(base[_LAUNCHER], walltime, where[_LAUNCHER]) if walltime
                              else base[_LAUNCHER])}
        changed = {rel: text for rel, text in config.items() if text != base[rel]}
        results = _results_dir(normed_dir, effective)
        if not transfer._under(normed_dir, results):
            # the run writes its results there: the same grant a run directory needs
            _authorize_run_dir(project, env, compute_env_name, results)
        time_line = _TIME_LINE_RE.search(config[_LAUNCHER])

        shared, prefixes, cells = _bound_paths(record, effective, rows)
        # A prefix names a family of files (<prefix>*), never a file of its own.
        plain = sorted((set(shared.values()) - set(prefixes)) | set(cells))

        # ─── AT THE LOCUS: images, paths, the data pins ─────────────────
        if env_type == "ssh":
            _check_images_cluster(record, env, compute_env_name, project_name, timeout)
            _check_paths_cluster(env, plain, prefixes, timeout=timeout)
        else:
            from agent import mcp_server as _ms
            docker_refusal = _ms._check_docker_available()
            if docker_refusal:
                return docker_refusal
            if not record.local_runtime or not Path(record.local_runtime.activate).is_file():
                raise _refuse("run_production.no_local_runtime",
                              "the pipeline was rendered without a local Nextflow runtime for this machine",
                              remedy="re-render on this machine (render_pipeline records scripts/activate.sh), "
                                     "or run `source scripts/activate.sh` and launch by hand")
            _check_images_local(record)
            _check_paths_local(plain, prefixes)
        reference_check = _reference_check(record, shared, locus=locus, env=env, timeout=timeout,
                                           prefixes=prefixes)
        value_check = _value_check(record, effective, rows)

        # ─── The run directory ──────────────────────────────────────────
        if env_type == "ssh":
            placed = _materialize_remote(project_name=project_name, env=env, env_name=compute_env_name,
                                         run_dir=normed_dir, pdir=pdir, rels=rels, samplesheet=samplesheet,
                                         reused=reused, changed=changed, access_path=access_path,
                                         timeout=timeout)
        else:
            placed = _materialize_local(run_dir=normed_dir, pdir=pdir, rels=rels, samplesheet=samplesheet,
                                        reused=reused, changed=changed)

        # ─── Launch ─────────────────────────────────────────────────────
        submitted_at = datetime.now(timezone.utc).isoformat()
        if env_type == "ssh":
            sb = submit_workflow.sbatch_via_ssh(env, normed_dir, timeout=timeout)
            if "error" in sb:
                return {**sb, **placed}
            job_id = sb["job_id"]
            launch = sb.get("sbatch_command", "")
            follow_up = {"poll": f"cluster_job_status(project_name={project_name!r}, compute_env_name="
                                 f"{compute_env_name!r}, job_id={job_id!r}, run_dir={normed_dir!r})",
                         "results": f"{results}/ — download(…) to fetch",
                         "run_records": f"{normed_dir}/runs/<stamp>/ (params.json, samples.csv, trace.txt, "
                                        f"report.html, timeline.html, nextflow.log)",
                         "manager_log": f"{normed_dir}/{record.name}-{job_id}.out"}
            code = "run_production.submitted"
        else:
            launch = f"source {shlex.quote(record.local_runtime.activate)} && {RUN_LOCAL}"
            started = jm.start(command=launch, job_id=f"pipeline_{record.name}_{_stamp()}",
                               working_dir=normed_dir, tool="run_production_pipeline")
            if "error" in started:
                return {**started, **placed}
            job_id = started.get("job_id", "")
            follow_up = {"poll": f"check_job(job_id={job_id!r}, wait_s=300)",
                         "results": f"{results}/",
                         "run_records": f"{normed_dir}/runs/<stamp>/ (params.json, samples.csv, trace.txt, "
                                        f"report.html, timeline.html)"}
            code = "run_production.local_launched"

        manifest_record = {
            "locus": locus, "project_name": project_name, "compute_env": compute_env_name,
            "pipeline": record.name, "pipeline_dir": str(pdir),
            "sealed_workflow": record.sealed_workflow, "sealed_workflow_sha256": record.sealed_workflow_sha256,
            "cohort_workflows": [cw.sealed_workflow for cw in record.cohort_workflows],
            "env_digests": list(record.env_digests),
            "images": _stage_images(record),
            "run_dir": normed_dir, "run_dir_zone": zone, "job_id": job_id,
            "reused_run_dir": placed["reused"], **{k: v for k, v in placed.items() if k != "reused"},
            "samplesheet": ({"source": samplesheet, "rows": len(rows), "columns": header} if samplesheet
                            else {"source": f"{normed_dir}/{SAMPLESHEET_FILENAME} (already there)"}),
            "params": effective, "params_overridden": {str(k): str(v) for k, v in (params or {}).items()},
            "walltime": (time_line.group(1) if time_line and env_type == "ssh" else None), "launch": launch,
            "edited_since_render": edited, "reference_check": dict(reference_check),
            "value_check": value_check,
            "submitted_at": submitted_at, "follow_up": follow_up,
        }
        manifest_path = submit_workflow._write_submission_manifest(
            project_name=project_name, workflow_name=record.name, job_id=job_id, manifest=manifest_record)

        result = proven(code, success=True, locus=locus, compute_env=compute_env_name,
                        pipeline=record.name, run_dir=normed_dir, job_id=job_id,
                        reused_run_dir=placed["reused"],
                        **{k: v for k, v in placed.items() if k != "reused"},
                        samplesheet=manifest_record["samplesheet"], params=effective,
                        launch=launch, submitted_at=submitted_at, manifest_path=manifest_path,
                        edited_since_render=edited, follow_up=follow_up)
        return _disclose(result, reference_check, value_check)

    except _Refusal as r:
        return r.result
    except (ValueError, compute_access.PermissionDenied, compute_access.ConfigError) as e:
        return refused("run_production.refused", error=f"{type(e).__name__}: {e}")
    except (FileNotFoundError, KeyError) as e:
        return broke("run_production.failed", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("run_production.timeout", error=f"a remote probe timed out after {e.timeout}s")


__all__ = ["run_production_pipeline"]
