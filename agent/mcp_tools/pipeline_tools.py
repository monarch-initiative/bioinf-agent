"""pipeline_tools — the PIPELINE layer's MCP surface.

ONE tool, `render_pipeline`. A sealed workflow proves that ONE run of the how-to works;
a pipeline is that how-to over MANY samples, as a directory a person runs without the
agent: a samplesheet (one row per sample), one stage per how-to command so a stage can
be re-run alone, the frozen image per stage, and an explain page — run with Nextflow,
locally through docker or on a cluster through apptainer and SLURM. The tool reads the
sealed spec through the typed seam, derives the record (`pipeline_record`), renders the
directory (`pipeline_render`) and returns where it landed. It EXECUTES NOTHING.

It also INVENTS nothing. The record is derived from the seal (stages = the how-to's
commands, images = the sealed steps' digests, columns = the per-sample inputs, defaults
= the sealed values), and the directory writer refuses any rendered command that is
not a sealed how-to command with only its placeholders rebound. What the caller may
choose is how to CUT the how-to into stages, how to size them, which placeholders are
per-sample, and which cluster the launcher is for.

Singletons via `_ms.X` — the same binding convention as the other tool modules.
"""
from __future__ import annotations

from typing import Any, Optional

from agent import mcp_server as _ms
from agent.mcp_server import OptStrList, mcp
from agent.skills import compute_access
from agent.skills import workspace as _workspace
from agent.skills.outcomes import proven, refused


def _local_runtime() -> Optional[dict]:
    """How THIS machine provides nextflow, as the page tells the user: the checkout's
    activation script and the runtime env's nextflow binary. Recorded only when the
    script exists; the binary's absence is stated, never assumed."""
    activate = _workspace.code_root() / "scripts" / "activate.sh"
    if not activate.is_file():
        return None
    nf = _workspace.runtime_env_dir() / "bin" / "nextflow"
    return {"activate": str(activate), "nextflow": str(nf) if nf.is_file() else None}


def _cluster_hints(env_block: dict, spec: Any) -> dict:
    """What the cluster files need from the env block and the EnvCache: the module
    names, and — when the env has a container zone and the sealed env is in the cache —
    the .sif path `stage_apptainer_image` writes: `<container zone>/<env name>_<12 hex
    of the content digest>.sif`. Absent pieces stay absent; nothing is guessed."""
    from agent.skills.stage_apptainer import _short_digest
    mods = compute_access.get_container_modules(env_block)
    modules = [v for k in ("apptainer_module", "nextflow_module") for v in [mods.get(k)] if v]
    sif_paths: dict[str, str] = {}
    env_names: dict[str, str] = {}
    key = str(getattr(spec, "env_request_key", "") or "")
    cached = _ms._env_cache.lookup(key) if key else None
    target = compute_access.get_container_upload_target(env_block)
    if cached and cached.get("name"):
        env_names[key] = str(cached["name"])
        digest = cached.get("content_digest") or getattr(spec, "env_content_digest", "")
        if target and target.get("path") and digest:
            base = str(target["path"]).rstrip("/")
            sif_paths[key] = f"{base}/{cached['name']}_{_short_digest(str(digest))}.sif"
    return {"modules": modules, "sif_paths": sif_paths, "env_names": env_names}


@mcp.tool()
def render_pipeline(sealed_workflow: str,
                    name: Optional[str] = None,
                    stages: Optional[list[list[int]]] = None,
                    stage_names: OptStrList = None,
                    per_sample: OptStrList = None,
                    shared: OptStrList = None,
                    resources: Optional[dict[str, dict[str, Any]]] = None,
                    env: Optional[str] = None,
                    overwrite: bool = False) -> dict:
    """**The PIPELINE layer.** Render a SEALED workflow as a runnable pipeline directory
    under ``<workspace>/pipelines/<name>/``. Executes nothing. The directory is a
    TEMPLATE: copy it next to the data, put the samples in ``samples.csv`` and the
    paths in ``params.yaml``, and run it there.

    **One way to run it.** ``main.nf`` + ``nextflow.config`` + ``params.yaml`` +
    ``samples.csv`` run every row of the samplesheet with Nextflow, one stage per
    how-to command, ``-resume`` re-running only what changed: ``nextflow run main.nf
    -profile local -params-file params.yaml -resume`` on a laptop (docker),
    ``sbatch launcher.sh`` on a cluster (apptainer + SLURM; the launcher loads the
    modules). WHERE it runs is ``nextflow.config``'s business — the ``local`` profile
    names the docker image, the ``slurm`` profile the ``.sif`` — and ``params.yaml``
    holds only the pipeline's parameters. ``pipeline.html`` shows all of it in the
    files' own vocabulary, with the command main.nf runs per stage. Every run writes
    ``runs/<timestamp>/trace.txt`` (each task's command) and ``report.html``.

    **What is derived from the seal and what you choose.** The samplesheet's example
    rows are the seal's own I4 trials — one or more; a one-trial seal renders a
    one-row sheet — and ``sample`` is the row key. A placeholder whose value differs
    across trials is a samplesheet COLUMN; one that is the same everywhere is a
    shared PARAM with the sealed value as its default (with one trial: the sample id
    and read inputs are per-sample, everything else shared). Override with
    ``per_sample=`` / ``shared=`` (placeholder names). ``stages=`` groups how-to
    commands into stages by their 1-based numbers, e.g. ``[[1, 2], [3]]`` runs
    commands 1 and 2 as one job; default is one stage per command so a stage can be
    re-run alone. ``stage_names=`` names them. ``resources={STAGE: {cpus, mem, time,
    gpus}}`` sizes a stage; an unsized stage carries a DEFAULT request that every
    file labels as unsized, with the sealed run's measurement beside it to size from.
    ``env=`` names a compute env in projects_access.yaml: the launcher then carries
    its SLURM policy and module names, and the ``slurm`` profile's ``container`` is
    the path ``stage_apptainer_image`` writes in that env's container zone.

    **Refuses** (``refused``, with a remedy) when the seal cannot support the render — no
    how-to, no proven trial, a placeholder no trial binds, an artifact no sealed step was
    observed writing, a stage cut that mixes images, a cohort stage — and when the target
    directory holds files edited since they were rendered (``pipeline.dir_edited``; pass
    ``overwrite=True`` to replace them, or a new ``name``). **Every rendered command is
    checked against the sealed how-to before any file is written** — a drift is a
    refusal, never a file.

    Returns ``proven("pipeline.rendered")`` with ``dir``, ``page``, ``files``, the
    ``stages`` (name, scope, commands, image digest, sized?), the ``params`` and
    ``samplesheet_columns``, the ``sif`` path when an env was named, and every
    derivation ``note``. Open ``page`` first.
    """
    from agent.skills.pipeline_record import (LocalRuntime, PipelineDerivationError,
                                              derive_pipeline_record, sha256_of)
    from agent.skills.pipeline_render import render_pipeline_dir
    from agent.skills.spec_writer import load_workflow_spec

    reports_dir = _workspace.reports_dir()
    spec_path = reports_dir / f"{sealed_workflow}.workflow.yaml"
    if not spec_path.exists():
        available = sorted(p.name[: -len(".workflow.yaml")]
                           for p in reports_dir.glob("*.workflow.yaml"))
        return refused("pipeline.no_sealed_workflow", success=False,
                       error=f"no sealed workflow '{sealed_workflow}' at {spec_path}",
                       available_workflows=available,
                       remedy="pass sealed_workflow= one of available_workflows, or seal_workflow first")
    try:
        spec = load_workflow_spec(spec_path)
    except Exception as e:
        return refused("pipeline.spec_invalid", success=False,
                       error=f"sealed workflow '{sealed_workflow}' is not a valid WorkflowSpec: "
                             f"{type(e).__name__}: {str(e)[:400]}",
                       remedy="the record on disk is malformed; re-seal the workflow")

    groups: Optional[list[list[int]]] = None
    if stages is not None:
        try:
            groups = [[int(n) - 1 for n in group] for group in stages]
        except (TypeError, ValueError):
            groups = [[-1]]
        if any(n < 0 for g in groups for n in g) or not groups:
            return refused("pipeline.bad_stages", success=False,
                           error=f"stages= must be groups of 1-based how-to command numbers, got {stages!r}",
                           remedy="e.g. stages=[[1, 2], [3]] runs how-to commands 1 and 2 as one stage")

    env_block: Optional[dict] = None
    hints = {"modules": [], "sif_paths": {}, "env_names": {}}
    if env:
        try:
            env_block = compute_access.get_compute_env(env, compute_access.load_access(None))
            hints = _cluster_hints(env_block, spec)
        except Exception as e:
            return refused("pipeline.env_unknown", success=False,
                           error=f"compute env '{env}' could not be loaded from projects_access.yaml: "
                                 f"{type(e).__name__}: {str(e)[:300]}",
                           remedy="pass env= a compute_envs[].name from projects_access.yaml, or omit it")

    pipeline_name = name or sealed_workflow
    out_dir = _workspace.pipelines_dir() / pipeline_name
    runtime = _local_runtime()
    try:
        record = derive_pipeline_record(
            spec, name=pipeline_name, spec_path=str(spec_path), spec_sha256=sha256_of(spec_path),
            stages=groups, stage_names=list(stage_names) if stage_names else None,
            per_sample=list(per_sample) if per_sample else None,
            shared=list(shared) if shared else None,
            resources=resources, env_names=hints["env_names"], sif_paths=hints["sif_paths"],
            compute_env=env or None, modules=hints["modules"],
            local_runtime=LocalRuntime(**runtime) if runtime else None)
        written = render_pipeline_dir(record, out_dir, env=env_block, overwrite=overwrite)
    except PipelineDerivationError as e:
        return refused(e.code, success=False, error=e.error, remedy=e.remedy,
                       sealed_workflow=sealed_workflow, pipeline=pipeline_name)

    sif = next((s.sif_path for s in record.stages if s.sif_path), None)
    return proven(
        "pipeline.rendered", success=True,
        pipeline=pipeline_name, sealed_workflow=sealed_workflow,
        dir=written["dir"], page=written["page"], record=written["record"],
        files=written["files"],
        stages=[{"name": s.name, "scope": s.scope, "commands": s.commands,
                 "image_digest": s.image_digest, "sized": s.resources.requested_by == "caller"}
                for s in record.stages],
        params=[{"name": p.name, "kind": p.kind, "default": p.default} for p in record.params],
        samplesheet_columns=([c.name for c in record.samplesheet.columns]
                             if record.samplesheet else []),
        example_rows=len(record.samplesheet.rows) if record.samplesheet else 0,
        compute_env=env or None, sif=sif,
        local_runtime=runtime,
        notes=record.notes,
        replaced_previous_render=written["replaced_previous_render"],
        removed=written["removed"],
    )
