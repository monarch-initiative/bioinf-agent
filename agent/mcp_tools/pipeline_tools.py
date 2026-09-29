"""pipeline_tools — the PIPELINE layer's MCP surface.

ONE tool, `render_pipeline`. A sealed workflow proves that ONE run of the how-to works;
a pipeline is that how-to over MANY samples, as a directory a person runs without the
agent: a samplesheet (one row per sample), one stage per how-to command so a stage can
be re-run alone, the frozen image per stage, and an explain page. The tool reads the
sealed spec through the typed seam, derives the record (`pipeline_record`), renders the
requested forms (`pipeline_render`) and returns where they landed. It EXECUTES NOTHING.

It also INVENTS nothing. The record is derived from the seal (stages = the how-to's
commands, images = the sealed steps' digests, columns = the per-sample inputs, defaults
= the sealed values), and the directory writer refuses any rendered command that is
not a sealed how-to command with only its placeholders rebound. What the caller may
choose is how to CUT the how-to into stages, how to size them, which placeholders are
per-sample, and which forms to write.

Singletons via `_ms.X` — the same binding convention as the other tool modules.
"""
from __future__ import annotations

from typing import Any, Optional

from agent import mcp_server as _ms  # noqa: F401  (binding convention; see module docstring)
from agent.mcp_server import OptStrList, mcp
from agent.skills import compute_access
from agent.skills import workspace as _workspace
from agent.skills.outcomes import proven, refused

FORMS = ("plain", "nextflow")


@mcp.tool()
def render_pipeline(sealed_workflow: str,
                    name: Optional[str] = None,
                    forms: OptStrList = None,
                    stages: Optional[list[list[int]]] = None,
                    stage_names: OptStrList = None,
                    per_sample: OptStrList = None,
                    shared: OptStrList = None,
                    resources: Optional[dict[str, dict[str, Any]]] = None,
                    publish: str = "declared",
                    env: Optional[str] = None,
                    overwrite: bool = False) -> dict:
    """**The PIPELINE layer.** Render a SEALED workflow as a runnable pipeline directory
    under ``<workspace>/pipelines/<name>/`` — a samplesheet (one row per sample), one
    stage per how-to command, the frozen image per stage, runners for a laptop and for a
    SLURM cluster, and ``pipeline.html``, the explain page. Executes nothing.

    **Two forms. Plain is the default; Nextflow is a REQUEST.**
      * ``plain``: bash + SLURM job arrays. ``params.env`` + ``samples.csv`` +
        ``stages/NN_<stage>.sh`` (the literal commands) + ``.sbatch`` per stage +
        ``run_local.sh`` (docker, every row × stage) + ``run_all.sh`` (one job array per
        stage, chained). Re-run one stage with ``./run_all.sh --stages <STAGE>``.
      * ``nextflow``: ``main.nf`` (one inline process per stage, the literal command in
        each script block) + ``nextflow.config`` (``local`` docker / ``slurm`` apptainer
        profiles) + ``params.yaml`` + ``launcher.sh`` + ``nextflow_local.sh``.
      Both forms ship the SAME ``samples.csv`` and read the same record.

    **What is derived from the seal and what you choose.** Rows come from the seal's own
    I4 trials (the worked example the user replaces with their samples); a how-to proven
    on 2+ trials renders ``per_row`` (a samplesheet), on 1 trial ``linear`` (params only).
    A placeholder whose value differs across trials is a samplesheet COLUMN; one that is
    the same everywhere is a shared PARAM with the sealed value as its default. Override
    with ``per_sample=`` / ``shared=`` (placeholder names). ``stages=`` groups how-to
    commands into stages by their 1-based numbers, e.g. ``[[1, 2], [3]]`` runs commands
    1 and 2 as one job; default is one stage per command. ``stage_names=`` names them.
    ``resources={STAGE: {cpus, mem, time, gpus}}`` sizes a stage; an unsized stage is
    rendered with a DEFAULT request that every file labels as unsized — the sealed run's
    measured wall/RSS are printed beside it to size from. ``publish="all"`` publishes
    every observed artifact instead of only the declared outputs. ``env=`` names a compute
    env in projects_access.yaml so the cluster files carry its SLURM account/partitions
    and Lmod modules.

    **Refuses** (``refused``, with a remedy) when the seal cannot support the render — no
    how-to, no proven trial, a placeholder no trial binds, an artifact no sealed step was
    observed writing, a stage cut that mixes images — and when the target directory holds
    files edited since they were rendered (``pipeline.dir_edited``; pass
    ``overwrite=True`` to replace them, or a new ``name``). A form that cannot carry the
    record (Nextflow on a sealed command holding ``$``) is refused with the form to use
    instead. **Every rendered command is checked against the sealed how-to before any
    file is written** — a drift is a refusal, never a file.

    Returns ``proven("pipeline.rendered")`` with ``dir``, ``page``, ``files``, ``forms``,
    ``shape``, the ``stages`` (name, scope, commands, image digest, sized?), the ``params``
    and ``samplesheet_columns``, and every derivation ``note``. Open ``page`` first: it
    shows the picture, the parameters, the stages with their measured resources, and how
    to run each form.
    """
    from agent.skills.pipeline_record import PipelineDerivationError, derive_pipeline_record, sha256_of
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

    chosen_forms = tuple(forms) if forms else ("plain",)
    bad_forms = [f for f in chosen_forms if f not in FORMS]
    if bad_forms:
        return refused("pipeline.unknown_form", success=False,
                       error=f"unknown form(s) {bad_forms}",
                       remedy=f"forms= must name only {list(FORMS)}")

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
    if env:
        try:
            env_block = compute_access.get_compute_env(env, compute_access.load_access(None))
        except Exception as e:
            return refused("pipeline.env_unknown", success=False,
                           error=f"compute env '{env}' could not be loaded from projects_access.yaml: "
                                 f"{type(e).__name__}: {str(e)[:300]}",
                           remedy="pass env= a compute_envs[].name from projects_access.yaml, or omit it")

    pipeline_name = name or sealed_workflow
    out_dir = _workspace.pipelines_dir() / pipeline_name
    try:
        record = derive_pipeline_record(
            spec, name=pipeline_name, spec_path=str(spec_path), spec_sha256=sha256_of(spec_path),
            stages=groups, stage_names=list(stage_names) if stage_names else None,
            per_sample=list(per_sample) if per_sample else None,
            shared=list(shared) if shared else None,
            resources=resources, publish=publish, forms=chosen_forms)
        written = render_pipeline_dir(record, out_dir, forms=chosen_forms, env=env_block,
                                      overwrite=overwrite)
    except PipelineDerivationError as e:
        return refused(e.code, success=False, error=e.error, remedy=e.remedy,
                       sealed_workflow=sealed_workflow, pipeline=pipeline_name)

    return proven(
        "pipeline.rendered", success=True,
        pipeline=pipeline_name, sealed_workflow=sealed_workflow,
        dir=written["dir"], page=written["page"], record=written["record"],
        files=written["files"], forms=written["forms"], shape=record.shape,
        stages=[{"name": s.name, "scope": s.scope, "commands": s.commands,
                 "image_digest": s.image_digest, "sized": s.resources.requested_by == "caller"}
                for s in record.stages],
        params=[{"name": p.name, "kind": p.kind, "default": p.default} for p in record.params],
        samplesheet_columns=([c.name for c in record.samplesheet.columns]
                             if record.samplesheet else []),
        example_rows=len(record.samplesheet.rows) if record.samplesheet else 0,
        notes=record.notes,
        replaced_previous_render=written["replaced_previous_render"],
        removed=written["removed"],
    )
