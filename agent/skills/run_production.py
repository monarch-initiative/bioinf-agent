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
as a bare success.

A run directory is Nextflow's launch directory, and `-resume` only works from the same
one: a second call naming a `run_dir` that already holds this pipeline launches there
again without re-copying (the samples.csv in place is the one used), and a `run_dir`
holding a different render is refused.
"""
from __future__ import annotations

import csv
import glob
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

import yaml

from agent.skills import cluster_jobs, compute_access, data_pins, stage_apptainer, submit_workflow, transfer, workspace
from agent.skills.outcomes import proven, refused, broke, degraded
from agent.skills.pipeline_record import PipelineRecord, load_pipeline_record
from agent.skills.pipeline_render import (MANIFEST_PATH, RECORD_PATH, SAMPLESHEET_FILENAME,
                                          _edited_since_render, parse_manifest)
from agent.skills.pipeline_render_nextflow import RUN_LOCAL, SAMPLESHEET_PARAM
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint

#: A value handed to Nextflow on the launch line: a path, a word, a number. Every character
#: is one `shlex.quote` leaves bare, so the line reads as written on both loci.
_VALUE_RE = re.compile(r"^[A-Za-z0-9_./+:,=@%-]{1,4096}$")
#: A SLURM walltime: minutes, M:S, H:M:S, D-H, D-H:M, D-H:M:S.
_WALLTIME_RE = re.compile(r"^(\d+|\d+:\d{2}|\d+:\d{2}:\d{2}|\d+-\d+|\d+-\d+:\d{2}|\d+-\d+:\d{2}:\d{2})$")
_SAMPLE_KEY = "sample"
_PARAMS_FILE = "params.yaml"
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


def _params_file(pdir: Path) -> dict:
    """params.yaml as the run will read it — the file, not the record, because the file
    is the one a person edits after the render."""
    try:
        raw = yaml.safe_load((pdir / _PARAMS_FILE).read_text()) or {}
    except Exception as e:
        raise _refuse("run_production.params_unreadable",
                      f"{pdir / _PARAMS_FILE} is not valid YAML: {type(e).__name__}: {str(e)[:200]}",
                      remedy="fix the file; it is read as `-params-file`")
    if not isinstance(raw, dict):
        raise _refuse("run_production.params_unreadable", f"{pdir / _PARAMS_FILE} is not a mapping",
                      remedy="one `key: value` per line")
    return {str(k): ("" if v is None else str(v)) for k, v in raw.items()}


def _effective_params(pdir: Path, overrides: Optional[Mapping]) -> tuple[dict[str, str], list[str]]:
    """{params.yaml key: value} as the run will see it — the file's values under the
    caller's overrides — and the launch-line tokens that carry the overrides."""
    effective = _params_file(pdir)
    allowed = set(effective) - {SAMPLESHEET_PARAM[0]}
    tokens: list[str] = []
    for key, value in (overrides or {}).items():
        k = str(key)
        if k not in allowed:
            raise _refuse("run_production.unknown_param", f"{k!r} is not a parameter of this pipeline",
                          params=sorted(allowed),
                          remedy=f"params= takes the keys of {_PARAMS_FILE}; the samplesheet is always "
                                 f"{SAMPLESHEET_FILENAME} in the run directory")
        sv = str(value)
        if not _VALUE_RE.match(sv):
            raise _refuse("run_production.unsafe_param_value",
                          f"params[{k!r}] holds characters the launch line cannot carry: {sv[:80]!r}",
                          remedy=f"a path, a word or a number; edit {_PARAMS_FILE} in the run directory for anything else")
        effective[k] = sv
        tokens += [f"--{k}", sv]
    return effective, tokens


def _read_samplesheet(path: str, record: PipelineRecord) -> tuple[list[str], list[dict]]:
    """The caller's samplesheet, checked against the columns the pipeline reads."""
    sp = Path(path)
    if not sp.is_file():
        raise _refuse("run_production.samplesheet_missing", f"samplesheet {path!r} is not a file on this machine",
                      remedy="give the local path of a CSV with one row per sample")
    with sp.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip() for h in (reader.fieldnames or [])]
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]
    need = [c.name for c in record.samplesheet.columns]
    missing = [c for c in need if c not in header]
    if missing:
        raise _refuse("run_production.samplesheet_columns",
                      f"samplesheet {path} lacks column(s) {missing}; the pipeline reads {need}",
                      required_columns=need, remedy="add the column(s); columns beyond these are carried through")
    if not rows:
        raise _refuse("run_production.samplesheet_empty", f"samplesheet {path} has no rows",
                      remedy="one row per sample under the header line")
    ids = [r.get(_SAMPLE_KEY, "") for r in rows]
    if any(not s for s in ids) or len(set(ids)) != len(ids):
        raise _refuse("run_production.samplesheet_sample_ids",
                      "every row needs a unique, non-empty `sample`",
                      remedy="`sample` is the row key: it tags every task and names results/<sample>/")
    for col in record.samplesheet.columns:
        if col.value_kind != "path":
            continue
        bad = [r.get(col.name, "") for r in rows if not r.get(col.name, "").startswith("/")]
        if bad:
            raise _refuse("run_production.samplesheet_relative_path",
                          f"column {col.name!r} must hold absolute paths at the locus; got {bad[:3]}",
                          remedy="absolute paths for data: a run resolves relative ones against its launch directory")
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


def _docker_image_present(image: str) -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


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
    checks = "; ".join(f'compgen -G {shlex.quote(p + "*")} > /dev/null || echo MISSING:{shlex.quote(p)}' for p in clean)
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
                     env: dict, timeout: int) -> dict:
    """The shared references this run binds against what the workflow was SEALED with.
    Grouped by the sealed workflow each parameter belongs to; one verdict."""
    if not shared:
        return {"status": data_pins.NOT_ATTEMPTED, "reason": "the pipeline binds no shared path parameter",
                "locus": locus, "findings": []}
    by_spec: dict[str, dict[str, str]] = {}
    for key, path in shared.items():
        by_spec.setdefault(_spec_for(record, key), {})[key] = path
    observed = _observe_cluster(env, sorted(set(shared.values())), timeout=timeout) if locus == "cluster" else {}
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


def _disclose(result: dict, reference_check: Mapping) -> dict:
    """Attach the data-pin verdict and DOWNGRADE a launch that could not stand behind
    its data. A divergence does not refuse — re-running against a newer reference is
    legitimate; doing it without being told is the failure this closes."""
    if not isinstance(result, dict):                            # pragma: no cover
        return result
    out = {**result, "reference_check": dict(reference_check)}
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
    return out


# ---------------------------------------------------------------------------
# The run directory
# ---------------------------------------------------------------------------

def _remote_run_dir_state(env: dict, run_dir: str, *, timeout: int) -> dict:
    """What `run_dir` holds on the cluster: a rendered main.nf, a samplesheet, and the
    manifest text if there is one. One ssh hop."""
    q = shlex.quote
    script = (f'[ -e {q(run_dir + "/main.nf")} ] && echo MAIN=1 || echo MAIN=0; '
              f'[ -e {q(run_dir + "/" + SAMPLESHEET_FILENAME)} ] && echo SHEET=1 || echo SHEET=0; '
              f'echo MANIFEST_BEGIN; cat {q(run_dir + "/" + MANIFEST_PATH)} 2>/dev/null; echo MANIFEST_END')
    argv = _ssh_argv(env, f"bash -lc {q(script)}")
    res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        raise _Refusal(broke("run_production.run_dir_probe_failed",
                             error=f"could not inspect {run_dir} on the cluster (rc={res.returncode}): "
                                   f"{(res.stderr or '').strip()[:300]}", **({"hint": hint} if hint else {})))
    lines = (res.stdout or "").splitlines()
    manifest = ""
    if "MANIFEST_BEGIN" in lines and "MANIFEST_END" in lines:
        a, b = lines.index("MANIFEST_BEGIN"), lines.index("MANIFEST_END")
        manifest = "\n".join(lines[a + 1:b]).strip()
    return {"main_nf": "MAIN=1" in lines, "samplesheet": "SHEET=1" in lines, "manifest": manifest}


def _reuse_or_refuse(state: Mapping, *, ours: str, run_dir: str, samplesheet: str) -> bool:
    """True when `run_dir` already holds THIS render (a re-launch, nothing copied);
    False when it is empty of a render (copy everything). Anything else is refused."""
    if not state["main_nf"]:
        if not samplesheet:
            raise _refuse("run_production.samplesheet_required",
                          f"{run_dir} holds no run yet and no samplesheet was given",
                          remedy="samplesheet=<local CSV>: one row per sample; it becomes the run's samples.csv")
        return False
    if state["manifest"].strip() != ours.strip():
        raise _refuse("run_production.run_dir_holds_another_render",
                      f"{run_dir} already holds a pipeline that is not this render",
                      remedy="use a new run directory, or re-render the pipeline that lives there")
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


def _materialize_remote(*, project_name: str, env: dict, env_name: str, run_dir: str, pdir: Path,
                        rels: list[str], samplesheet: str, access_path: Optional[str], timeout: int) -> dict:
    parent = os.path.dirname(run_dir)
    probe = cluster_jobs.remote_paths_exist(env, [parent], timeout=timeout)
    if probe.get("missing_paths"):
        raise _refuse("run_production.run_dir_parent_missing",
                      f"{parent} does not exist on {env_name!r}; the run directory's parent is never created for you",
                      remedy="create it, or pick a run_dir under a directory that exists")
    if "error" in probe:
        raise _Refusal(probe)
    state = _remote_run_dir_state(env, run_dir, timeout=timeout)
    if _reuse_or_refuse(state, ours=(pdir / MANIFEST_PATH).read_text(), run_dir=run_dir, samplesheet=samplesheet):
        return {"reused": True, "files_uploaded": []}
    uploaded: list[str] = []
    for rel in rels + [SAMPLESHEET_FILENAME]:
        local = str(pdir / rel) if rel != SAMPLESHEET_FILENAME else samplesheet
        up = transfer.upload(project_name=project_name, compute_env_name=env_name, local_path=local,
                             remote_abs_path=f"{run_dir}/{rel}", access_path=access_path, timeout=timeout)
        if "error" in up:
            raise _Refusal(broke("run_production.upload_failed",
                                 error=f"upload of {rel} failed before launch: {up['error']}",
                                 files_uploaded=uploaded))
        uploaded.append(up.get("remote_abs_path", f"{run_dir}/{rel}"))
    return {"reused": False, "files_uploaded": uploaded}


def _materialize_local(*, run_dir: str, pdir: Path, rels: list[str], samplesheet: str) -> dict:
    rd = Path(run_dir)
    if rd.resolve() == pdir.resolve():
        raise _refuse("run_production.run_dir_is_the_template",
                      f"{run_dir} is the rendered template itself; a run lives in its own directory",
                      remedy="make a new directory for the run; the template stays a template")
    if not rd.parent.is_dir():
        raise _refuse("run_production.run_dir_parent_missing",
                      f"{rd.parent} does not exist; the run directory's parent is never created for you",
                      remedy="create it, or pick a run_dir under a directory that exists")
    rd.mkdir(exist_ok=True)
    manifest_here = rd / MANIFEST_PATH
    state = {"main_nf": (rd / "main.nf").is_file(), "samplesheet": (rd / SAMPLESHEET_FILENAME).is_file(),
             "manifest": manifest_here.read_text() if manifest_here.is_file() else ""}
    if _reuse_or_refuse(state, ours=(pdir / MANIFEST_PATH).read_text(), run_dir=run_dir, samplesheet=samplesheet):
        return {"reused": True, "files_copied": []}
    copied: list[str] = []
    for rel in rels:
        dst = rd / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdir / rel, dst)
        if rel.endswith(".sh") or rel.startswith("bin/"):
            dst.chmod(dst.stat().st_mode | 0o111)
        copied.append(str(dst))
    shutil.copy2(samplesheet, rd / SAMPLESHEET_FILENAME)
    copied.append(str(rd / SAMPLESHEET_FILENAME))
    return {"reused": False, "files_copied": copied}


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
    run_dir:     an absolute path on the env — the launch directory. Its parent must
                 exist; it may be in the env's pipelines zone, the agent's scratch, or a
                 directory the project grants with `upload` and `exec`. A directory that
                 already holds this pipeline is re-launched (`-resume`), nothing copied.
    samplesheet: a local CSV, one row per sample, with the columns the pipeline reads
                 (`sample` first); it becomes the run's samples.csv. Required on the first
                 launch into a directory; refused on a re-launch.
    params:      {params.yaml key: value} for this run only, passed on the launch line
                 and so winning over params.yaml. A path, a word or a number.
    walltime:    the manager job's SLURM time limit for this submission
                 (`sbatch --time=…`), overriding the launcher's header. ssh envs only.
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

        record, pdir, manifest, edited = _load_pipeline(pipeline)
        rels = _carried_files(pdir, manifest)
        normed_dir = submit_workflow._validate_workflow_dir(run_dir)
        zone = _authorize_run_dir(project, env, compute_env_name, normed_dir)
        effective, tokens = _effective_params(pdir, params)
        if env_type == "ssh" and walltime:
            tokens_sbatch = [f"--time={walltime}"]
        else:
            tokens_sbatch = []
        if env_type == "ssh" and record.compute_env and record.compute_env != compute_env_name:
            raise _refuse("run_production.rendered_for_other_env",
                          f"the pipeline was rendered for compute env {record.compute_env!r}, not "
                          f"{compute_env_name!r}: its nextflow.config names that env's images and modules",
                          remedy=f"render_pipeline(…, env={compute_env_name!r}, name=…) for this env")

        header: list[str] = []
        rows: list[dict] = []
        if samplesheet:
            header, rows = _read_samplesheet(samplesheet, record)
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
        reference_check = _reference_check(record, shared, locus=locus, env=env, timeout=timeout)

        # ─── The run directory ──────────────────────────────────────────
        if env_type == "ssh":
            placed = _materialize_remote(project_name=project_name, env=env, env_name=compute_env_name,
                                         run_dir=normed_dir, pdir=pdir, rels=rels, samplesheet=samplesheet,
                                         access_path=access_path, timeout=timeout)
        else:
            placed = _materialize_local(run_dir=normed_dir, pdir=pdir, rels=rels, samplesheet=samplesheet)

        # ─── Launch ─────────────────────────────────────────────────────
        submitted_at = datetime.now(timezone.utc).isoformat()
        if env_type == "ssh":
            sb = submit_workflow.sbatch_via_ssh(env, normed_dir, timeout=timeout,
                                                sbatch_args=tokens_sbatch, script_args=tokens)
            if "error" in sb:
                return {**sb, **placed}
            job_id = sb["job_id"]
            launch = sb.get("sbatch_command", "")
            follow_up = {"poll": f"cluster_job_status(project_name={project_name!r}, compute_env_name="
                                 f"{compute_env_name!r}, job_id={job_id!r}, run_dir={normed_dir!r})",
                         "results": f"{normed_dir}/{effective.get('outdir', 'results')}/ — download(…) to fetch",
                         "run_records": f"{normed_dir}/runs/<stamp>/ (params.json, samples.csv, trace.txt, "
                                        f"report.html, timeline.html, nextflow.log)",
                         "manager_log": f"{normed_dir}/{record.name}-{job_id}.out"}
            code = "run_production.submitted"
        else:
            from agent import mcp_server as _ms
            jm = _job_manager or _ms._job_manager
            launch = f"source {shlex.quote(record.local_runtime.activate)} && {RUN_LOCAL}" + \
                     "".join(f" {t}" for t in tokens)
            started = jm.start(command=launch, job_id=f"pipeline_{record.name}_{_stamp()}",
                               working_dir=normed_dir, tool="run_production_pipeline")
            if "error" in started:
                return {**started, **placed}
            job_id = started.get("job_id", "")
            follow_up = {"poll": f"check_job(job_id={job_id!r}, wait_s=300)",
                         "results": f"{normed_dir}/{effective.get('outdir', 'results')}/",
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
            "params": effective, "params_overridden": {t[2:]: v for t, v in zip(tokens[::2], tokens[1::2])},
            "walltime": walltime or None, "launch": launch,
            "edited_since_render": edited, "reference_check": dict(reference_check),
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
        return _disclose(result, reference_check)

    except _Refusal as r:
        return r.result
    except (ValueError, compute_access.PermissionDenied, compute_access.ConfigError) as e:
        return refused("run_production.refused", error=f"{type(e).__name__}: {e}")
    except (FileNotFoundError, KeyError) as e:
        return broke("run_production.failed", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("run_production.timeout", error=f"a remote probe timed out after {e.timeout}s")


__all__ = ["run_production_pipeline"]
