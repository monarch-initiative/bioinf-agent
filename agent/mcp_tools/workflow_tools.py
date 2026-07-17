"""workflow_tools — pipeline-draft lifecycle + Layer-2 seal + R-deps helper.

The pipeline-draft surface (start / show / patch / discard / mark / stage),
plus the Layer-2 deliverable producers (seal_workflow / write_pipeline_provenance
/ list_installed_pipelines), plus the R-package DESCRIPTION/dep scanner used
before installing CRAN/Bioconductor packages from GitHub.

LATENT BUG NOTE — `_scan_r_runtime_installs` originally called `json.loads`
without `import json` in scope; the bare-except returned `[]` silently, so
`undeclared_runtime_installs` had been empty for every call. Adding the
`import json` at the top of THIS module is the fix.
"""
from __future__ import annotations

import hashlib
import json   # <-- fix for _scan_r_runtime_installs latent bug (see module docstring)
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# IMPORT-BINDING NOTE: singletons that tests monkeypatch (the L5 conftest
# swaps `_pipeline_state` to a tmp-rooted instance per test) MUST be reached
# through the mcp_server MODULE attribute, not captured as a bare name at
# import time — otherwise `monkeypatch.setattr(mcp_server, "_pipeline_state",
# ...)` only updates the module attr, not the bare ref this module would
# have captured. The pattern below (`_ms._pipeline_state` inside each tool)
# survives monkeypatching. `config` is included because tests can swap the
# config too, and the cost of going through `_ms.` is one attribute lookup.
from agent import mcp_server as _ms
from agent.mcp_server import mcp  # the FastMCP app is never monkeypatched
from agent.skills.outcomes import broke, proven, refused  # terminal outcome tags


# ---------------------------------------------------------------------------
# Layer-2 seal — workflow validation + WorkflowSpec write
# ---------------------------------------------------------------------------

def _refresh_reference_databases(rdbs: list) -> list:
    """Re-derive available / sha256 / size_bytes from DISK for each declared
    ReferenceDatabase at seal time (the draft's flags are stale — a download
    is async and finalize was retired). The sha256 comes from the
    `<local_path>.source.sha256` sidecar written during download (the hash of
    the bytes the URL served — see download_reference_database), so the sealed
    WorkflowSpec pins each DB by CONTENT, not merely by name+URL. Missing
    sidecar ⇒ sha256 stays absent (honest: we never fabricate a hash)."""
    out: list = []
    for e in rdbs or []:
        if not isinstance(e, dict):
            out.append(e)
            continue
        e = dict(e)
        # Cluster-locus entries live on the cluster; their local_path is a CLUSTER
        # path that isn't visible on this machine. ssh-derive available / size /
        # sha256 from the cluster instead of reading local disk (which would
        # wrongly mark them unavailable). Best-effort — I5 is the hard gate.
        if e.get("locus") == "cluster":
            from agent.skills import acquire_data
            out.append(acquire_data.refresh_cluster_reference_db(e))
            continue
        lp = e.get("local_path")
        if lp:
            p = Path(lp)
            e["available"] = p.exists()
            sidecar = Path(f"{lp}.source.sha256")
            if sidecar.is_file():
                try:
                    parts = sidecar.read_text().strip().split()
                    if parts:
                        e["sha256"] = parts[0]
                except Exception:
                    pass
            if p.is_file():
                try:
                    e["size_bytes"] = p.stat().st_size
                except Exception:
                    pass
        out.append(e)
    return out


class _ImageUsageRunner:
    """An env_manager-shaped runner that executes the I4 self-test INSIDE a frozen env
    image, so the how-to is proven against the exact bytes the user runs.

    It exists to satisfy ONE call — `env_manager.run_in_env(env, cmd, timeout, watch_dir)`
    — which is the only thing self_test_usage asks of its runner. That seam was already
    injectable; nothing in spec_writer needed to learn about containers.

    Mounts are bound at their OWN host paths so the trial's absolute substitutions resolve
    verbatim inside the image (the same trick run_step_in_container uses), and outputs land
    back on the host where OutputValidator can read them.
    """
    is_image_runner = True

    def __init__(self, image: str, platform: str, mounts: list):
        self.image, self.platform, self.mounts = image, platform, mounts

    def run_in_env(self, env_name, command, timeout=600, watch_dir=None, **_):
        mounts = list(self.mounts)
        if watch_dir:
            mounts.append((str(watch_dir), str(watch_dir)))
        return _ms._docker.run_in_container(
            self.image, command, mounts=mounts, workdir=str(watch_dir) if watch_dir else None,
            platform=self.platform, timeout=timeout)


def _image_usage_runner(fr: dict, draft: dict):
    """Build an _ImageUsageRunner for this frozen env, or None when the how-to can't be
    self-tested in-image here.

    Returns None (→ seal records not_attempted + reason) rather than raising, because
    "we couldn't run it" is missing evidence, not a broken how-to. Preconditions, each of
    which is a real case on disk:
      - the image must resolve locally. An ADOPTED biocontainer is a remote ref that may
        need a pull; offline, that is not the workflow's fault.
      - every path-valued substitution must EXIST on this host. talos_validate_moi's inputs
        are /work/... on the cluster — unreachable here, and failing it for that would be
        refusing the environment, not the artifact.
    Scalar substitutions ({THREADS}) are ignored rather than treated as missing paths, so a
    non-path param can't silently disable the self-test. Output slots are skipped — they
    are overwritten with the trial's fresh scratch dir and are not expected to pre-exist.

    The substitutions are resolved EXACTLY as self_test_usage resolves them — declared
    trials if any, else inferred from pipeline_steps[*].inputs. Checking only `usage.trials`
    looked right and was wrong: talos_validate_moi declares `trials: []`, so the check
    inspected an empty set, waved through a run whose inferred inputs are all on the
    cluster, and turned a legitimately-unrunnable-here workflow into a hard seal refusal.
    A precondition must inspect the same values the runner will use, or it guards nothing.
    """
    from pathlib import Path
    from agent.skills.spec_writer import _infer_substitutions, _is_output_slot
    image = (fr.get("image") or "").strip()
    if not image:
        return None
    usage = draft.get("usage") or {}
    # ONE reading of command_template (str or list[str]) — core_data.usage_commands. This
    # precondition MUST see the same placeholders self_test_usage will resolve, across
    # every command; reading only the first would re-create the tier-2 bug where a
    # precondition inspected something other than what the runner actually uses.
    from agent.models.core_data import usage_commands
    template = "\n".join(usage_commands(usage))
    if not template:
        return None
    placeholders = set(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", template))
    trials = [t for t in (usage.get("trials") or []) if isinstance(t, dict)]
    if trials:
        subs_sets = [dict(t.get("substitutions") or {}) for t in trials]
    else:
        subs_sets = [_infer_substitutions(draft, placeholders, usage.get("inputs") or [])]

    mounts: list[tuple[str, str]] = []
    for subs in subs_sets:
        for slot, val in subs.items():
            if _is_output_slot(slot):
                continue                       # replaced by the per-trial scratch dir
            s = str(val)
            if not s.startswith("/"):
                continue                       # not a path — a scalar param
            p = Path(s)
            if not p.exists():
                return None                    # cluster-locus / missing input → not_attempted
            d = str(p.parent if p.is_file() else p)
            if d == "/":
                continue                       # never bind-mount the host root
            if (d, d) not in mounts:
                mounts.append((d, d))
    try:
        if not _ms._docker.image_digest(image):
            return None                        # not resolvable locally (adopted, or no daemon)
    except Exception:
        return None
    platform = _ms._CONDA_TO_DOCKER_PLATFORM.get(fr.get("platform", ""), "linux/amd64")
    return _ImageUsageRunner(image, platform, mounts)


def _derive_step_dependencies(pipeline_steps: list) -> list:
    """Materialize each step's `depends_on` (prior step numbers it consumes an
    output of) into the sealed spec.

    The StepInput/PipelineStep model documents depends_on as 'derived at finalize
    from input/output overlap if absent' — but finalize_pipeline was retired in
    the respine and seal never picked the derivation up, so depends_on was ALWAYS
    empty. The multi-step chaining probe surfaced it: a 2-step pipeline sealed with
    step2 recording depends_on=[] even though its BAM input IS step1's output. The
    seal already re-computes this exact edge to CHECK I8 (composition-coherence /
    lineage), but never wrote it back — so the self-verifying WorkflowSpec was not
    self-DOCUMENTING: a reader couldn't tell step2's input came from step1 vs an
    external source without re-deriving it. This closes that gap by stamping the
    edge the seal already knows.

    Derivation is exact-path input↔output overlap (the byte-identical lineage
    edge), last-writer-wins in step order (matches _check_lineage_integrity). Only
    fills depends_on when currently absent/empty (honors the 'if absent' contract);
    an explicit value is left untouched. Returns NEW step dicts (no draft mutation)."""
    # Walk in step-number order to build the producer index (last writer wins),
    # deriving depends_on into a per-step-number map; then emit in the caller's
    # original list order so the sealed YAML keeps its shape.
    ordered = sorted(
        [s for s in (pipeline_steps or []) if isinstance(s, dict)],
        key=lambda s: s.get("step") or 0,
    )
    producer: dict[str, int] = {}          # output path -> latest producing step
    enriched: dict[int, dict] = {}         # step number -> enriched dict
    for s in ordered:
        sn = s.get("step") or 0
        s2 = dict(s)
        if not s2.get("depends_on"):                       # absent/empty -> derive
            deps: set[int] = set()
            for inp in s2.get("inputs", []) or []:
                ipath = inp.get("path") if isinstance(inp, dict) else inp
                if isinstance(ipath, str) and producer.get(ipath, sn) < sn:
                    deps.add(producer[ipath])
            s2["depends_on"] = sorted(deps)
        enriched[sn] = s2
        # Register outputs AFTER deriving deps so a step can't depend on itself.
        for o in (s2.get("detected_outputs") or []) + (s2.get("outputs") or []):
            if isinstance(o, str) and o:
                producer[o] = sn

    out: list = []
    for s in (pipeline_steps or []):
        if isinstance(s, dict) and (s.get("step") or 0) in enriched:
            out.append(enriched[s.get("step") or 0])
        else:
            out.append(s)
    return out


def _render_run_dashboard(wf: dict, env_record: dict, out_dir: Path) -> Optional[str]:
    """Render THIS workflow's Layer-2 run dashboard — `{workflow_name}.RUN.html`.

    Two-artifact split (deliberately NOT the old accreting-env-report model): the
    env's `{env}.ENV.html` is a Layer-1 artifact written ONCE at freeze and never
    mutated by a seal; the run dashboard is the Layer-2 artifact, one per sealed
    workflow, that accretes validated evidence per compute locus. Both render
    purely from their machine-verified record. Returns the RUN.html path, or None
    on a render hiccup (never fatal to a verified seal)."""
    from agent.skills.run_dashboard_html import render_run_dashboard_html

    name = wf.get("workflow_name") or "workflow"
    try:
        html = render_run_dashboard_html(wf, env_record=env_record)
    except Exception:
        return None
    report_path = Path(out_dir) / f"{name}.RUN.html"
    report_path.write_text(html)
    return str(report_path)


@mcp.tool()
def seal_workflow(
    pipeline_id: str,
    freeze_request_key: str,
    workflow_name: str = "",
    description: str = "",
    write: bool = True,
) -> dict:
    """Seal the Layer-2 WORKFLOW: validate the run-side invariants, PIN the
    frozen environment by digest, render the user guide, write the WorkflowSpec.

    The two-layer split. Layer 1 (ENVIRONMENT) = finalize + freeze: a
    content-addressed, reusable artifact. Layer 2 (WORKFLOW) CONSUMES it. Call
    freeze() first to produce the env artifact, then seal_workflow with its
    request_key to pin the env by digest — the wall that lets the workflow layer
    ignore install concerns.

    Refuses to write if any workflow invariant fails (I0 shape · I3 validated
    outputs · I6 paths · I7 resources · I8 input provenance); the env-build
    invariants are Layer 1's concern. Writes {workflow_name}.workflow.yaml (the
    machine-verified spec) + {workflow_name}.RUN.html (the Layer-2 run dashboard
    rendered from the passing run — validated evidence per compute locus plus a
    distinct how-to panel; the env's ENV.html is a separate, immutable Layer-1
    artifact and is not touched here).
    """
    draft = _ms._pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return refused("seal.unknown_pipeline_id", success=False,
                       error=f"unknown pipeline_id: {pipeline_id}")
    # A WorkflowSpec PINS this env by digest and asserts Layer-2 on top of Layer-1.
    # Sealing against a record that can no longer earn its Layer-1 green would make
    # the spec's own foundation unverifiable — so ask the serving question, and say
    # which clause failed rather than reporting it as absent.
    fr, env_violations = _ms._env_cache.lookup_verified(freeze_request_key)
    if env_violations:
        return refused("seal.env_contract_violated", success=False,
                       error=f"the frozen env '{freeze_request_key}' no longer satisfies the "
                             f"Layer-1 honesty contract — re-run freeze() before sealing",
                       honesty_violations=env_violations,
                       violation_count=len(env_violations))
    if not fr:
        return refused("seal.no_frozen_env", success=False,
                       error=f"no frozen env for '{freeze_request_key}' — run freeze() first")

    from agent.skills.spec_writer import (check_workflow_invariants, self_test_usage,
                                          write_workflow_spec)
    violations = check_workflow_invariants(draft)
    if violations:
        return refused("seal.workflow_invariants", success=False,
                       stage="workflow_invariants",
                       violations=violations, violation_count=len(violations))

    # The usage.command_template IS the workflow's run contract — establish
    # usage_verified honestly by self-testing it (I4), since the draft doesn't
    # persist the field (it's derived only at validate/finalize). A verified
    # template is what the guide shows as the runnable form.
    #
    # I4 GATES THE SEAL (fix H2): if the draft declares a usage block, its
    # command_template MUST self-test green against every declared trial —
    # otherwise the guide would publish a runnable form that doesn't actually
    # run. Previously `usage_verified` was computed then rendered cosmetically
    # while the seal proceeded regardless; that let a broken usage template
    # ship with a "verified" badge.
    #
    # THE LOCUS FIX (audit 2026-07-16): this gate used to read
    # `if draft.get("usage") and draft.get("conda_env")`. self_test_usage needs a runner,
    # and the only runner it knew was a HOST conda env — but the architecture moved
    # container-native, so the primary path has no host env and the gate SILENTLY SKIPPED.
    # Every one of the 4 sealed workflows on disk has conda_env=None; 3 carry
    # usage_verified=False that means "never attempted" while rendering as a verdict.
    # We now prefer the FROZEN IMAGE as the runner (validated == shipped: the how-to is
    # tested against the exact bytes the user runs), fall back to the host env for the
    # pre-freeze path, and when neither can run it we record not_attempted + WHY rather
    # than fabricating a False.
    usage_ok = False
    usage_detail: dict | None = None
    if draft.get("usage"):
        runner = _image_usage_runner(fr, draft) or (_ms._env_mgr if draft.get("conda_env") else None)
        try:
            usage_detail = self_test_usage(draft, runner, validator=_ms._validator)
            usage_ok = bool(usage_detail.get("ok"))
        except Exception as e:
            usage_ok = False
            usage_detail = {"ok": False, "status": "not_attempted",
                            "reason": f"the self-test runner raised: {type(e).__name__}: {e}"}
        # Refuse ONLY on a real failure. `not_attempted` is missing evidence, not a
        # broken how-to — refusing it would make legitimately unsealable-here workflows
        # (cluster-locus inputs, no frozen image locally) permanently unsealable.
        #
        # FAIL CLOSED on an unrecognized shape: a result with ok=False and no `status` is
        # read as "failed", never waved through. Reading a missing status as "not failed"
        # would turn absence of data into a pass — and it is not hypothetical: it silently
        # disarmed this very gate for any caller still returning the pre-three-state shape.
        status = usage_detail.get("status") or ("verified" if usage_ok else "failed")
        usage_detail["status"] = status
        if status == "failed":
            return refused(
                "seal.usage_self_test_failed", success=False, stage="usage_self_test",
                error="I4: usage.command_template failed its self-test — it did not "
                      "execute green against every declared trial, so it cannot be "
                      "sealed as the workflow's verified run contract. Fix the template "
                      "(or the trials' substitutions) and re-seal.",
                usage_self_test=usage_detail,
            )

    # MULTI-ENV CHAINING: a workflow may chain steps that each ran in their OWN
    # frozen env (their own freeze). Validate every step's container digest against
    # the set of ALL frozen env digests, not just the one we sealed from — so a
    # multi-env workflow is still "validated == shipped" when every step ran in some
    # shipped env. `fr` remains the PRIMARY env (the guide's get-the-image section).
    all_envs = _ms._env_cache.all()
    valid_digests = {r.get("image_digest") for r in all_envs.values() if r.get("image_digest")}
    by_digest = {r.get("image_digest"): (k, r) for k, r in all_envs.items() if r.get("image_digest")}
    envs_used: list[dict] = []
    seen_dig: set = set()
    for s in draft.get("pipeline_steps", []):
        if not isinstance(s, dict):
            continue
        d = s.get("container_image_digest")
        is_validated = s.get("validation") or s.get("validation_status") == "passed"
        if d and is_validated and d not in seen_dig:
            seen_dig.add(d)
            rk, rr = by_digest.get(d, ("", {}))
            envs_used.append({"request_key": rk, "image": rr.get("image", s.get("container_image", "")),
                              "image_digest": d})

    # The how-to is rendered from the verified `usage` block into the Layer-2 run
    # dashboard (HTML) below — NOT a markdown guide (retired). We still pull
    # key_packages for the spec's driver_env record.
    key_packages = _ms._user_guide.key_packages(draft)

    wname = workflow_name or f"{draft.get('pipeline_name', 'workflow')}_workflow"
    wf = {
        "workflow_name":      wname,
        "description":        description or draft.get("description", ""),
        "created_at":         datetime.now(timezone.utc).isoformat(),
        "env_request_key":    fr.get("request_key", freeze_request_key),
        "env_content_digest": fr.get("content_digest", ""),
        "env_image":          fr.get("image", ""),
        "env_hpc_delivery":   fr.get("hpc_delivery", {}),
        # every frozen env this workflow chains (multi-env); the primary env above
        # is for the guide's get-the-image step. Each step also carries its own
        # container_image_digest, so a reader can map step → env.
        "envs":               envs_used,
        "pipeline_status":    draft.get("pipeline_status", "in_progress"),
        "usage_verified":     usage_ok,
        # The three-state truth behind the bool. `usage_verified: False` alone cannot
        # distinguish "tested and broken" from "never tested", and since seal refuses the
        # former, False on disk ALWAYS meant the latter — a verdict nobody reached.
        # Renderers must read this, not the bool, before saying anything about the how-to.
        "usage_verification": ({"status": usage_detail.get("status", "not_attempted"),
                                "reason": usage_detail.get("reason", ""),
                                "locus":  usage_detail.get("locus", ""),
                                "trial_count": usage_detail.get("trial_count", 0),
                                "passed": usage_detail.get("passed", 0)}
                               if usage_detail else None),
        "validated_in_shipped_image": _ms._user_guide.validated_in_shipped_image(
            draft, fr, valid_digests=valid_digests),
        "usage":              draft.get("usage"),
        # Stamp each step's depends_on (input↔prior-output overlap) — the edge the
        # seal already computes to CHECK I8 — so the sealed spec is self-documenting,
        # not just self-verifying (finalize used to derive this; it was retired).
        "pipeline_steps":     _derive_step_dependencies(draft.get("pipeline_steps", [])),
        # External sources carried so the artifact self-verifies (I8 standalone).
        "test_data":            draft.get("test_data"),
        "reference_databases":  _refresh_reference_databases(draft.get("reference_databases", [])),
        "runtime_configs":      draft.get("runtime_configs", []),
        "authored_artifacts":   draft.get("authored_artifacts", []),
        "driver_env":         {"conda_env": draft.get("conda_env"),
                               "python_version": draft.get("python_version"),
                               "key_packages": key_packages},
    }
    # The artifact must pass its OWN run-side invariants — validate what we WRITE,
    # not just the draft we sealed from (the draft is richer; the artifact must
    # stand on its own).
    self_violations = check_workflow_invariants(wf)
    if self_violations:
        return refused("seal.self_verify_failed", success=False,
                       stage="workflow_self_verify",
                       reason="constructed WorkflowSpec failed its own run-side invariants — "
                              "it would not be re-verifiable standalone",
                       violations=self_violations, violation_count=len(self_violations))
    result = proven(
        "seal.sealed", success=True, workflow_name=wname,
        env_pinned_digest=fr.get("content_digest"), env_image=fr.get("image"),
        commands_shown=len(_ms._user_guide.executed_commands(draft)),
    )
    if write:
        out = write_workflow_spec(wf, _ms.config)
        if out.get("error"):
            return broke("seal.spec_write_failed", success=False, **out)
        result.update(out)
        # Layer-2 deliverable: render THIS workflow's run dashboard
        # ({workflow_name}.RUN.html) from the verified spec — validated evidence
        # per compute locus + a distinct how-to panel. The env's ENV.html (Layer 1)
        # is left untouched: it is immutable post-freeze and never claims the
        # cluster-worthiness this dashboard carries. Non-fatal — a render hiccup
        # never fails a verified seal.
        project_root = Path(__file__).resolve().parents[2]
        out_dir = project_root / _ms.config["paths"]["pipelines_dir"]
        report_path = _render_run_dashboard(wf, fr, out_dir)
        if report_path:
            result["run_report_path"] = report_path
    else:
        result["workflow"] = wf
    return result


@mcp.tool()
def write_pipeline_provenance(
    pipeline: str,
    conda_env_path: str,
    pipeline_spec_path: str,
    output_files: list[dict],
    output_dir: str,
    sample_key: str,
    # genome reference — optional for tools that don't use a reference FASTA
    genome_build: str = "",
    chromosome: str = "",
    reference_path: str = "",
    # input types — at least one must be provided
    reads: Optional[dict] = None,
    bam_input: Optional[dict] = None,
    vcf_input: Optional[dict] = None,
    assembly_input: Optional[dict] = None,
    phenotype: Optional[dict] = None,
    pedigree: Optional[dict] = None,
    genotype_array: Optional[dict] = None,
    quantitative_traits: Optional[dict] = None,
    upstream_pipelines: Optional[list[str]] = None,
    parameters: Optional[dict] = None,
) -> dict:
    """Write a validated provenance YAML for a completed pipeline run.

    output_files: list of {file: str, type: str, indexed: bool}

    Input types (at least one required):
      reads:               {r1, r2?, sample, accession, subset, num_reads, assay_type, end_type, database}
      bam_input:           {bam: str, bai: str}
      vcf_input:           {vcf: str, tbi?: str, genome_build: str, upstream_pipeline?: str, sample_ids?: []}
      assembly_input:      {assembly: str, upstream_pipeline?: str}
      phenotype:           {ontology?: str, terms: [str], source?: str}
      pedigree:            {ped: str, proband?: str}
      genotype_array:      {file: str, format: hapmap|plink_bed|vcf|dosage|bgen,
                            bim?: str, fam?: str, n_samples?: int, n_snps?: int,
                            genome_build?: str, upstream_pipeline?: str}
      quantitative_traits: {traits: [str], file: str, n_samples?: int,
                            measurement_type?: continuous|binary|ordinal}

    genome_build / chromosome / reference_path are optional for tools that do not
    consume a reference FASTA (e.g. variant prioritizers, phenotype scorers, GWAS)."""
    inputs: dict[str, Any] = {
        "pipeline":           pipeline,
        "conda_env_path":     conda_env_path,
        "pipeline_spec_path": pipeline_spec_path,
        "genome_build":       genome_build,
        "chromosome":         chromosome,
        "reference_path":     reference_path,
        "output_files":       output_files,
        "output_dir":         output_dir,
        "sample_key":         sample_key,
    }
    if reads:                inputs["reads"]                = reads
    if bam_input:            inputs["bam_input"]            = bam_input
    if vcf_input:            inputs["vcf_input"]            = vcf_input
    if assembly_input:       inputs["assembly_input"]       = assembly_input
    if phenotype:            inputs["phenotype"]            = phenotype
    if pedigree:             inputs["pedigree"]             = pedigree
    if genotype_array:       inputs["genotype_array"]       = genotype_array
    if quantitative_traits:  inputs["quantitative_traits"]  = quantitative_traits
    if upstream_pipelines:   inputs["upstream_pipelines"]   = upstream_pipelines
    if parameters:           inputs["parameters"]           = parameters
    try:
        return _ms._write_provenance(inputs, _ms.config)
    except Exception as e:
        # The provenance record is pydantic-validated; malformed/partial inputs
        # raise ValidationError (and a bad output_dir raises OSError). Surface a
        # tag the agent can act on instead of an uncaught exception (C4).
        return refused("provenance.invalid_inputs", success=False,
                       error=f"{type(e).__name__}: {str(e)[:400]}")


@mcp.tool()
def list_installed_pipelines() -> dict:
    """What has ALREADY been built here — check this before solving a tool again.

    Returns both layers:
      `envs[]`     — Layer 1: the frozen, content-addressed envs in the EnvCache.
                     These are the reusable "solved components": ask for one of
                     these tools again and freeze serves it BY DIGEST instead of
                     rebuilding. Each carries its REQUESTED tools with semantic
                     versions (from the shipped image's SBOM), image_digest,
                     content_digest, build_method (adopt/build/authors-dockerfile)
                     and validation_locus.
      `workflows[]`— Layer 2: the sealed WorkflowSpecs, each pinning its env by
                     digest, with steps_validated / validated_in_shipped_image /
                     usage_verified.

    **`contract_ok` is earned, not remembered.** Every env is re-anchored against
    the full honesty contract AS YOU ASK (`env_honesty.check_build`, the same
    question freeze/run/stage/seal ask at serve time). `contract_ok: False` means
    "on disk, but the serving paths would REFUSE it today" — a record whose green
    has expired, e.g. because a gate got stronger since it was frozen. Read
    `contract_violations[]` for the failing clause. An inventory that showed a
    stale record as usable would be precisely the false green tier 5 closed.
    """
    return _ms._list_pipelines(_ms.config, env_cache=_ms._env_cache)


# ---------------------------------------------------------------------------
# R package utilities
# ---------------------------------------------------------------------------

def _parse_dcf(text: str) -> dict[str, str]:
    """Parse a Debian Control File (R DESCRIPTION format) into a flat dict."""
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")):
            if current_key:
                fields[current_key] += " " + line.strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            current_key = key.strip()
            fields[current_key] = val.strip()
    return fields


_R_RUNTIME_INSTALL_RE = re.compile(
    r"""(?:BiocManager::install
        |install\.packages
        |requireNamespace            # if(!requireNamespace("X")) is the canonical lazy-install signal
        |library
        |require)
        \s*\(
    """,
    re.VERBOSE,
)

_R_RUNTIME_QUOTED_RE = re.compile(r"""(['"])([A-Za-z0-9._]+)\1""")


def _scan_r_runtime_installs(github_repo: str, ref: str) -> list[str]:
    """Look for `BiocManager::install("X")` / `install.packages("X")` calls in the
    R/ source directory of a GitHub R package. Returns a deduplicated list of
    package names. Best-effort — failures are silent (we still want the
    DESCRIPTION info to be returned)."""
    api_url = f"https://api.github.com/repos/{github_repo}/contents/R?ref={ref}"
    try:
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            listing = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    found: set[str] = set()
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if not name.endswith(".R") and not name.endswith(".r"):
            continue
        raw_url = entry.get("download_url")
        if not raw_url:
            continue
        try:
            with urllib.request.urlopen(raw_url, timeout=15) as f:
                source = f.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        # Strip comment lines so we don't grab examples from comments.
        cleaned = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        # Find each call site, then pluck every quoted name from the argument list.
        for m in _R_RUNTIME_INSTALL_RE.finditer(cleaned):
            # The regex captures only the first name; scan the substring containing
            # the call's arg list for all quoted strings (handles c('a','b','c')).
            start = m.start()
            depth = 0
            i = start
            while i < len(cleaned) and cleaned[i] != "(":
                i += 1
            arglist_start = i + 1
            depth = 1
            i = arglist_start
            while i < len(cleaned) and depth > 0:
                if cleaned[i] == "(":
                    depth += 1
                elif cleaned[i] == ")":
                    depth -= 1
                i += 1
            arglist = cleaned[arglist_start:i-1]
            for q in _R_RUNTIME_QUOTED_RE.finditer(arglist):
                pkg = q.group(2)
                # Filter out obvious non-package strings (version numbers, paths, options)
                if pkg and not pkg.startswith(("/", "./", "http", "https")) and "." not in pkg[:1]:
                    if re.match(r"^[A-Za-z][A-Za-z0-9._]*$", pkg):
                        found.add(pkg)
    return sorted(found)


def _parse_pkg_list(raw: str) -> list[str]:
    """Extract bare package names from a comma-separated dep field, stripping version specs."""
    names = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name = re.split(r"[\s(]", item)[0].strip()
        if name and name != "R":
            names.append(name)
    return names


@mcp.tool()
def fetch_r_package_deps(github_repo: str, ref: str = "HEAD") -> dict:
    """Fetch the DESCRIPTION file from a GitHub R package and parse all dependencies.

    Use this BEFORE installing any R package from GitHub so you can pre-install
    all dependencies, then call remotes::install_github(..., dependencies=FALSE).

    github_repo: owner/repo, e.g. "jiabowang/GAPIT3"
    ref:         branch, tag, or commit SHA (default HEAD → main/master)

    Returns:
      package_name, version, r_version_required
      imports, depends, suggests, linking_to — raw dep name lists
      all_required  — union of imports + depends + linking_to (what must be installed)
      install_strategy — ordered steps: conda first, then BiocManager for everything
                         else (BiocManager resolves both CRAN and Bioconductor),
                         GitHub last with dependencies=FALSE."""
    url = f"https://raw.githubusercontent.com/{github_repo}/{ref}/DESCRIPTION"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        return broke("fetch_r_deps.fetch_failed", success=False, error=str(e), url=url)

    fields = _parse_dcf(content)

    imports    = _parse_pkg_list(fields.get("Imports", ""))
    depends    = _parse_pkg_list(fields.get("Depends", ""))
    suggests   = _parse_pkg_list(fields.get("Suggests", ""))
    linking_to = _parse_pkg_list(fields.get("LinkingTo", ""))

    r_ver_match = re.search(r"R\s*\(>=[^)]*\)", fields.get("Depends", ""))
    r_version_required = r_ver_match.group(0) if r_ver_match else ""

    all_required = sorted(set(imports + depends + linking_to))

    # Hunt for *undeclared* transitive deps — R packages whose .onLoad hooks or
    # zzz.R do `BiocManager::install("X")` / `install.packages("X")` for things
    # not listed in DESCRIPTION. GAPIT does this with snpStats; without this
    # discovery the agent learns about it only by the install failing.
    undeclared_runtime_installs = _scan_r_runtime_installs(github_repo, ref)
    undeclared_set = sorted({
        pkg for pkg in undeclared_runtime_installs
        if pkg not in set(all_required + suggests) and pkg != fields.get("Package", "")
    })

    return proven(
        "fetch_r_deps.parsed",
        success=True,
        github_repo=github_repo,
        ref=ref,
        url=url,
        package_name=fields.get("Package", ""),
        **{
        "version": fields.get("Version", ""),
        "r_version_required": r_version_required,
        "imports": imports,
        "depends": depends,
        "suggests": suggests,
        "linking_to": linking_to,
        "all_required": all_required,
        "undeclared_runtime_installs": undeclared_set,
        "install_strategy": [
            "1. For each dep in all_required, call search_package to check conda-forge "
            "(r-{lowercase}) and bioconda (bioconductor-{lowercase}) availability.",
            "2. Install all conda-available deps in one install_conda_packages call.",
            "3. For deps not found on conda, install via BiocManager — it resolves both "
            "CRAN and Bioconductor packages without needing to know which is which: "
            "Rscript -e \"lib<-file.path(Sys.getenv('CONDA_PREFIX'),'lib','R','library'); "
            "if(!requireNamespace('BiocManager',quietly=TRUE)) "
            "install.packages('BiocManager',lib=lib); "
            "BiocManager::install(c('pkg1','pkg2'), lib=lib, ask=FALSE, update=FALSE)\"",
            f"4. Finally: remotes::install_github('{github_repo}', "
            "lib=file.path(Sys.getenv('CONDA_PREFIX'),'lib','R','library'), "
            "dependencies=FALSE)",
        ],
    })


# ---------------------------------------------------------------------------
# Pipeline state accumulator
# ---------------------------------------------------------------------------

@mcp.tool()
def start_pipeline(pipeline_name: str, description: str) -> dict:
    """Start a new pipeline draft (or silently resume an existing one).

    Returns pipeline_id (= pipeline_name). Pass pipeline_id to subsequent
    tools (search_package, create_conda_env, verify_installation, run_in_env,
    validate_output, run_pipeline_step, select_test_data) so their results
    are auto-merged into a server-side draft. You don't hand-assemble the
    final artifacts — freeze(pipeline_id) produces the Layer-1 env image and
    seal_workflow(pipeline_id, freeze_request_key) writes the Layer-2
    WorkflowSpec + guide from the validated run.

    Resume semantics: if a draft for this pipeline_name already exists (e.g.
    after an MCP restart mid-install), it is loaded and resumed silently;
    `resumed: true` is set in the return so you know. On resume, a `summary`
    block is included describing what's already in the draft so the agent can
    decide whether to continue from where the prior run left off or call
    discard_pipeline_draft and start fresh."""
    r = _ms._pipeline_state.start(pipeline_name, description)
    if r.get("resumed"):
        draft = _ms._pipeline_state.get_draft(pipeline_name) or {}
        install_steps = draft.get("install_steps", []) or []
        pipeline_steps = draft.get("pipeline_steps", []) or []
        last_install = install_steps[-1] if install_steps else None
        last_pipeline = pipeline_steps[-1] if pipeline_steps else None
        # Surface any pipeline step with no detected outputs and no validation —
        # the silent-empty-success pattern that bit the prior Exomiser run.
        suspect_steps = [
            s.get("step")
            for s in pipeline_steps
            if not (s.get("detected_outputs") or s.get("outputs") or s.get("validation"))
        ]
        r["summary"] = {
            "conda_env":              draft.get("conda_env"),
            "env_status":             draft.get("env_status"),
            "pipeline_status":        draft.get("pipeline_status"),
            "install_steps_count":    len(install_steps),
            "install_steps_failed":   sum(1 for s in install_steps if s.get("returncode") not in (None, 0)),
            "pipeline_steps_count":   len(pipeline_steps),
            "pipeline_steps_failed":  sum(1 for s in pipeline_steps if s.get("returncode") not in (None, 0)),
            "packages_recorded":      len(draft.get("packages", []) or []),
            "test_data":              draft.get("test_data") is not None,
            "docker":                 draft.get("docker") is not None,
            "usage":                  draft.get("usage") is not None,
            "last_install_tool":      last_install.get("tool") if last_install else None,
            "last_pipeline_tool":     last_pipeline.get("tool") if last_pipeline else None,
            "suspect_unvalidated_steps": suspect_steps,
        }
    return r


@mcp.tool()
def discard_pipeline_draft(pipeline_id: str) -> dict:
    """Delete a pipeline draft without finalizing. Use when you want to start
    fresh with the same pipeline_name and don't want resume semantics."""
    return _ms._pipeline_state.discard(pipeline_id)


@mcp.tool()
def show_pipeline_draft(pipeline_id: str) -> dict:
    """Return the current accumulated draft for inspection. Does not modify
    or finalize anything."""
    draft = _ms._pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return refused("show_draft.unknown_pipeline", error=f"unknown pipeline_id: {pipeline_id}")
    return proven("show_draft.ok", pipeline_id=pipeline_id, draft=draft)


@mcp.tool()
def patch_pipeline(pipeline_id: str, patches: dict) -> dict:
    """Deep-merge agent-authored patches into the draft. Accepts only the
    keys no primitive produces directly: description, notes, final_summary,
    conda_env, created_at, python_version, reference_free, runtime_environment,
    runtime_configs, reference_databases, service_dependencies, usage.

    Patches to runtime-captured or finalize-derived fields (pipeline_steps,
    install_steps, packages, verifications, test_data, authored_artifacts,
    docker, env_status, pipeline_status, docker_status, usage_verified,
    lock_sha256) are rejected — use the dedicated primitive instead so the
    spec stays anchored to observed reality.

    Lists are replaced wholesale (pipeline_steps and install_steps are blocked
    here anyway; they merge by step number through their own primitives).

    Deletion: pass the literal string "__DELETE__" as a value inside a
    patchable key's subtree to remove that nested key. e.g.
    {"runtime_environment": {"min_ram_gb": "__DELETE__"}} drops the
    min_ram_gb hint while leaving the rest of runtime_environment intact.
    Use this rather than setting to None — None can trigger downstream
    attribute errors."""
    return _ms._pipeline_state.patch(pipeline_id, patches)


@mcp.tool()
def stage_authored_artifact(
    pipeline_id: str,
    path: str,
    role: str,
    description: str,
    content: str = "",
    generated_by: str = "",
    language: str = "",
    overwrite: bool = True,
) -> dict:
    """Stage an agent-authored artifact and record its provenance in the spec.

    Use this whenever you write a file or perform a transformation OUTSIDE the
    pipeline_step / install_step primitives — driver scripts, synthetic test
    inputs, hand-staged BAM/VCF/FASTA, sed-massaged configs, generated CSV/TSV
    fixtures. These artifacts are otherwise invisible to the spec; a step that
    consumes them looks like an orphan to the I8 composition-coherence walk
    and finalize fails.

    Two modes:

      content mode      — supply `content` (text). The runtime writes the file
                          at `path`, computes sha256, and stores the contents
                          (full if small, excerpt + full sha256 if large)
                          inside the spec. Reviewers can audit exactly what
                          the agent generated.

      generated_by mode — supply `generated_by` (the shell command you ran),
                          with the file already on disk at `path`. The runtime
                          records the command as the genesis and sha256s the
                          bytes. Use for binary outputs (BAM, FASTA, indexed
                          DB, pickled models).

    Honesty effect:
      - Path is added to the I8 universe of external sources, so downstream
        pipeline_steps can legally name it as an input.
      - sha256 is re-verified at finalize (I9). If the on-disk content drifts
        from the recorded sha256, the spec is refused — silent tampering is
        impossible.
      - `authored_artifacts` is blocked from patch_pipeline, so the only path
        to record an artifact is this primitive (the sha256 anchor is
        compulsory).

    Returns: {success, path, sha256, size_bytes, role, mode}.
    """
    if bool(content) == bool(generated_by):
        return refused(
            "stage_artifact.mode_ambiguous",
            error="exactly one of `content` (text content to write) "
                  "or `generated_by` (genesis command for an existing file) must be supplied",
        )
    if not Path(path).is_absolute():
        return refused("stage_artifact.path_not_absolute", error=f"path must be absolute, got: {path!r}")

    p = Path(path)
    mode: str

    if content:
        if p.exists() and not overwrite:
            return refused("stage_artifact.exists_no_overwrite", error=f"path already exists and overwrite=False: {path}")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        except Exception as e:
            return broke("stage_artifact.write_failed", error=f"could not write artifact: {e!r}", path=path)
        mode = "content"
    else:
        if not p.exists():
            return refused(
                "stage_artifact.source_missing",
                error=f"generated_by mode requires the file to already exist on disk: {path}",
            )
        mode = "generated_by"

    try:
        raw = p.read_bytes()
    except Exception as e:
        return broke("stage_artifact.readback_failed", error=f"could not read back artifact for sha256: {e!r}", path=path)

    sha256 = hashlib.sha256(raw).hexdigest()
    size_bytes = len(raw)

    # For content mode, embed the full text up to ~64 KiB; otherwise an excerpt.
    # Binary-mode artifacts get a short hex preview so a reviewer can sanity-check.
    SPEC_FULL_LIMIT = 65536
    excerpt: Optional[str] = None
    content_full_in_spec = False
    if mode == "content":
        if size_bytes <= SPEC_FULL_LIMIT:
            excerpt = content
            content_full_in_spec = True
        else:
            excerpt = content[:4096] + f"\n... [truncated, total {size_bytes} bytes]"
    else:
        # Show a short hex preview for binary artifacts so the spec isn't blind.
        excerpt = f"<binary; first 64 bytes hex: {raw[:64].hex()}>"

    artifact = {
        "path":        str(p),
        "role":        role,
        "description": description,
        "sha256":      sha256,
        "size_bytes":  size_bytes,
        "created_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if language:
        artifact["language"] = language
    if excerpt is not None:
        artifact["content_excerpt"] = excerpt
    artifact["content_full_in_spec"] = content_full_in_spec
    if generated_by:
        artifact["generated_by"] = generated_by

    idx = _ms._pipeline_state.add_authored_artifact(pipeline_id, artifact)
    if idx is None:
        return refused("stage_artifact.unknown_pipeline", error=f"unknown pipeline_id: {pipeline_id}", path=path)

    return proven(
        "stage_artifact.staged",
        success=True,
        path=str(p),
        sha256=sha256,
        size_bytes=size_bytes,
        role=role,
        mode=mode,
        pipeline_merge={
            "status":        "merged",
            "pipeline_id":   pipeline_id,
            "artifact_index": idx,
        },
    )


@mcp.tool()
def mark_step_validated(
    pipeline_id: str,
    step: int,
    validation_status: str = "passed",
) -> dict:
    """Set the validation_status of a pipeline_step. The pipeline-level
    pipeline_status derives from these at finalize time.

    Use this when a step's outputs are known good but validate_output wasn't
    called for them (e.g. the step's outputs were checked by the next step's
    success, or the LLM verified them by other means). Operates only on
    pipeline_steps — install_steps don't carry a validation_status field;
    their success is captured by status (returncode==0)."""
    if validation_status not in {"passed", "failed"}:
        return refused("mark_validated.bad_status",
                       error="validation_status must be 'passed' or 'failed'",
                       got=validation_status)
    # `step` is a 1-based index; a non-int (e.g. a stringly-typed arg) must not
    # reach the `1 <= step <= len(steps)` comparison as a TypeError (C4).
    if not isinstance(step, int) or isinstance(step, bool):
        try:
            step = int(step)
        except (TypeError, ValueError):
            return refused("mark_validated.bad_step",
                           error=f"step must be a 1-based integer, got {step!r}")
    # Guard against the silent-empty-success trap: a step that exited 0 but
    # produced no detected outputs and was never validated cannot be honestly
    # called "passed". The agent should use "failed" or retry the step.
    if validation_status == "passed":
        draft = _ms._pipeline_state.get_draft(pipeline_id) or {}
        steps = draft.get("pipeline_steps", [])
        if 1 <= step <= len(steps):
            s = steps[step - 1]
            outs  = s.get("detected_outputs") or s.get("outputs") or []
            vlds  = s.get("validation") or {}
            if not outs and not vlds:
                return refused(
                    "mark_validated.empty_success_guard",
                    error="cannot mark step 'passed' with no detected outputs and no validation entries — "
                          "this is the 'ran without error but produced nothing' pattern. Investigate the step "
                          "or use validation_status='failed' if the run was actually broken.",
                    pipeline_id=pipeline_id, step=step,
                )
    ok = _ms._pipeline_state.mark_pipeline_step_validated(pipeline_id, step, validation_status)
    if ok:
        return proven("mark_validated.set", status="set", pipeline_id=pipeline_id,
                      step=step, validation_status=validation_status)
    return refused("mark_validated.unknown_step",
                   error="unknown pipeline_id or step out of range",
                   pipeline_id=pipeline_id, step=step)
