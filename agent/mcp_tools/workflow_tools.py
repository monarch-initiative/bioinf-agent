"""workflow_tools — pipeline-draft lifecycle + Layer-2 seal + R-deps helper.

The pipeline-draft surface (start / show / patch / discard / mark / stage),
plus the Layer-2 deliverable producer (seal_workflow
/ list_installed_pipelines), plus the R-package DESCRIPTION/dep scanner used
before installing CRAN/Bioconductor packages from GitHub.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# IMPORT-BINDING NOTE: singletons that tests monkeypatch (the L5 conftest
# swaps `_pipeline_state` to a tmp-rooted instance per test) MUST be reached
# through the mcp_server MODULE attribute, not captured as a bare name at
# import time — otherwise `monkeypatch.setattr(mcp_server, "_pipeline_state",
# ...)` only updates the module attr, not the bare ref this module would
# have captured. The pattern below (`_ms._pipeline_state` inside each tool)
# survives monkeypatching. `config` is included because tests can swap the
# config too, and the cost of going through `_ms.` is one attribute lookup.
from agent import mcp_server as _ms
from agent.models import core_data as _core_data
from agent.models.core_data import (executed_commands as _executed_commands, key_packages as _key_packages,
                                    validated_in_shipped_image as _validated_in_shipped_image,
                                    is_script_path as _is_script_path)
from agent.mcp_server import mcp  # the FastMCP app is never monkeypatched
from agent.skills.backgroundable import backgroundable
from agent.skills.outcomes import broke, degraded, proven, refused  # terminal outcome tags
from agent.skills import workspace as _workspace


# ---------------------------------------------------------------------------
# Layer-2 seal — workflow validation + WorkflowSpec write
# ---------------------------------------------------------------------------

def _refresh_reference_databases(rdbs: list) -> list:
    """Re-derive available / sha256 / size_bytes from DISK for each declared
    ReferenceDatabase at seal time (the draft's flags are stale — a download
    is async, so only the disk knows what landed). The sha256 comes from the
    `<local_path>.source.sha256` sidecar written during download (the hash of
    the bytes the URL served — see download_reference_database), so the sealed
    WorkflowSpec pins each DB by CONTENT, not merely by name+URL. Missing
    sidecar ⇒ sha256 stays absent (honest: we never fabricate a hash).

    FILL-ONLY, NEVER OVERWRITE. `available` and `size_bytes` are observations and are
    refreshed every time; `sha256` is an ANCHOR — a claim about what the bytes were
    when the run was validated — and re-deriving it from today's sidecar erases the
    only value the I5 comparison could be against. This refresh runs while building
    the artifact and the artifact is re-validated immediately after, so an overwrite
    would make that check compare a value with itself. The same rule holds in
    acquire_data.refresh_cluster_reference_db."""
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
            if sidecar.is_file() and not e.get("sha256"):
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


def _derive_step_dependencies(pipeline_steps: list) -> list:
    """Materialize each step's `depends_on` (prior step numbers it consumes an
    output of) into the sealed spec.

    The seal re-computes this exact edge to CHECK I8 (lineage); stamping it into
    the spec makes the WorkflowSpec self-DOCUMENTING too — a reader can tell a
    step's input came from a prior step vs an external source without re-deriving
    the overlap.

    Derivation is exact-path input↔output overlap (the byte-identical lineage
    edge), last-writer-wins in step order (matches _check_lineage_integrity). A
    step's `remote_outputs` count as its outputs: a cluster step's files live at the
    locus that made them, and the next cluster step consumes them there. Only
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
        for o in ((s2.get("detected_outputs") or []) + (s2.get("outputs") or [])
                  + (s2.get("remote_outputs") or [])):
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

    Two-artifact split: the
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


def _spec_env_identity(spec: dict) -> frozenset:
    """The frozen-env fingerprint set a WorkflowSpec pins. Two seals of the SAME
    workflow_name that share this identity are locus ACCRETION (the same env(s),
    more or updated validated evidence); a DIFFERENT identity means the new seal
    pins a distinct environment, so overwriting the file would destroy the prior
    sealed artifact's provenance.

    Uses the shipped `image_digest` of every chained env PLUS the primary env's
    `content_digest` and `request_key`, so a REBUILT env (same request, new
    content — a non-reproducible rebuild) reads as DIFFERENT and cannot silently
    overwrite. (Per the user's ruling: a re-seal against a rebuilt env requires
    an explicit supersede, not a silent additive update.)"""
    ids: set[str] = set()
    for e in spec.get("envs", []) or []:
        if isinstance(e, dict) and e.get("image_digest"):
            ids.add(e["image_digest"])
    for k in ("env_content_digest", "env_request_key"):
        if spec.get(k):
            ids.add(spec[k])
    return frozenset(ids)


def _validated_step_count(spec: dict) -> int:
    """How many pipeline_steps carry real validation evidence. A re-seal that
    LOWERS this over an existing spec is dropping evidence — not accretion — and
    must not silently replace the richer artifact."""
    from agent.models.core_data import step_is_validated
    return sum(1 for s in spec.get("pipeline_steps", []) or []
               if isinstance(s, dict) and step_is_validated(s))


def _next_superseded_path(out_dir: Path, wname: str) -> Path:
    """A non-colliding name to PRESERVE a superseded spec under, rather than
    destroy it: {wname}.superseded.{n}.workflow.yaml (n = next free integer)."""
    n = 1
    while True:
        p = out_dir / _workspace.superseded_workflow_name(wname, n)
        if not p.exists():
            return p
        n += 1


def _env_name_of(frozen: dict, draft: dict) -> str:
    """The env directory a seal files under: the frozen record's name, else the
    draft's conda env (the name freeze was given)."""
    return (frozen.get("name") or frozen.get("env_name") or draft.get("conda_env")
            or frozen.get("request_key", "env").split("|")[0])


def _guard_spec_overwrite(wf: dict, out_dir: Path, supersede: bool) -> tuple[bool, Optional[dict]]:
    """The seal write-guard.

    write_workflow_spec overwrites {name}.workflow.yaml with NO exists-check, so
    an unguarded re-seal over an existing sealed spec silently DESTROYS a
    digest-pinned provenance artifact. The honesty contract already re-validates
    the NEW spec standalone, so the new file is never a lie — but the SILENT
    REPLACEMENT of the existing one is. The guard belongs at the terminal WRITE, not
    on the draft: a draft lock would break locus accretion,
    one-env-many-workflows, and every ad-hoc pipeline_id="" call, and a
    LEGAL_TRANSITIONS table would be a second, drifting definition of ordering.

      - no existing spec           -> write (first seal)
      - same env identity, no
        evidence dropped           -> write (locus ACCRETION — the documented
                                      flow: seal locally, later run on cluster,
                                      re-seal so the dashboard accretes the locus)
      - different env identity, or
        evidence dropped, and no
        supersede                  -> REFUSE seal.would_clobber_sealed_spec
      - ...with supersede=True     -> PRESERVE the prior spec under a
                                      .superseded.N name, stamp a note, write

    Returns (proceed, refusal|None); renames the prior file as a side effect on
    supersede. Freeze needs no analogous guard: the EnvCache is content-addressed
    and append-only, so a re-freeze cannot silently clobber a shipped env."""
    import yaml
    wname = wf.get("workflow_name", "")
    # Across every env directory: a workflow name is unique over the whole store,
    # so a re-seal on a different env finds the prior spec where it was filed.
    found = _workspace.find_sealed_workflows(wname)
    if not found:
        return True, None
    path = found[0]
    try:
        prior = yaml.safe_load(path.read_text()) or {}
    except Exception:
        prior = {}
    same_env = _spec_env_identity(wf) == _spec_env_identity(prior)
    drops_evidence = _validated_step_count(wf) < _validated_step_count(prior)
    if same_env and not drops_evidence:
        return True, None                       # locus accretion — additive, supersedes nothing
    if not supersede:
        return False, refused(
            "seal.would_clobber_sealed_spec", success=False,
            error=(f"a sealed WorkflowSpec already exists at {path.name} pinning a "
                   f"different environment (or richer validated evidence); sealing here "
                   f"would silently REPLACE it and destroy its provenance. Pass "
                   f"supersede=True to replace it — the prior spec is preserved "
                   f"alongside (as {path.stem}.superseded.N.workflow.yaml), not deleted."),
            existing_spec=str(path),
            existing_env_identity=sorted(_spec_env_identity(prior)),
            new_env_identity=sorted(_spec_env_identity(wf)),
            existing_validated_steps=_validated_step_count(prior),
            new_validated_steps=_validated_step_count(wf))
    backup = _next_superseded_path(path.parent, wname)
    path.rename(backup)
    wf["superseded"] = {"replaced_spec": backup.name,
                        "at": datetime.now(timezone.utc).isoformat(),
                        "prior_env_identity": sorted(_spec_env_identity(prior))}
    return True, None


@mcp.tool()
@backgroundable("workflow_name", "pipeline_id")
def seal_workflow(
    pipeline_id: str,
    freeze_request_key: str,
    workflow_name: str = "",
    description: str = "",
    write: bool = True,
    supersede: bool = False,
) -> dict:
    """Seal the Layer-2 WORKFLOW: validate the run-side invariants, DERIVE the how-to
    from the recorded steps, PIN the frozen environment by digest, write the
    WorkflowSpec and render the RUN dashboard (the how-to panel is part of it).

    The two-layer split. Layer 1 (ENVIRONMENT) = finalize + freeze: a
    content-addressed, reusable artifact. Layer 2 (WORKFLOW) CONSUMES it. Call
    freeze() first to produce the env artifact, then seal_workflow with its
    request_key to pin the env by digest — the wall that lets the workflow layer
    ignore install concerns.

    THE HOW-TO IS THE RUN. Every step the run tools recorded carries the command with its
    `{UPPER_SNAKE}` slots and the values they took (`bindings=`); the seal's how-to is
    those templates in step order, one trial per sample (the steps grouped by agreeing
    bindings), the outputs the steps were observed writing, with the sample's values put
    back as slots. Nothing is typed by hand and nothing is re-run: a how-to that differs
    from what ran cannot exist (I4). A step whose output a later step binds as a slot
    value, and which binds no per-sample slot (an index build, an annotation slice), is a
    preparation step: provenance of that value, not a how-to command. A sample that ran
    only some of the commands is named under `samples_incomplete`; a draft in which no
    sample ran every command, or whose steps were recorded without their template
    (`run_in_env`), is refused with the step to re-run.

    Refuses to write if any workflow invariant fails (I0 shape · I3 validated
    outputs · I4 the how-to matches the steps · I5 reference databases · I6 paths ·
    I7 resources · I8 input provenance · I10 service health); the env-build
    invariants are Layer 1's concern. Writes {workflow_name}.workflow.yaml (the
    machine-verified spec) + {workflow_name}.RUN.html (the Layer-2 run dashboard
    rendered from the passing run — validated evidence per compute locus plus a
    distinct how-to panel; the env's ENV.html is a separate, immutable Layer-1
    artifact and is not touched here).

    A sealed {workflow_name}.workflow.yaml is a digest-pinned provenance artifact,
    so re-sealing over an EXISTING one is guarded: re-sealing
    the SAME env with more/updated validated evidence writes through (locus
    accretion), but sealing a DIFFERENT env (or dropping evidence) over it
    REFUSES `seal.would_clobber_sealed_spec` unless `supersede=True` — which
    preserves the prior spec under a `.superseded.N` name rather than destroying
    it. Freeze is not guarded this way: the EnvCache is content-addressed, so a
    re-freeze cannot silently overwrite a shipped env.
    """
    draft = _ms._pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return _ms._pipeline_state.unknown_draft_refusal("seal.unknown_pipeline_id", pipeline_id)
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

    from agent.skills.spec_writer import (check_workflow_invariants, derive_pipeline_status, derive_usage,
                                          verify_test_data, write_workflow_spec)

    # A WorkflowSpec is the record of a validated run, so a draft with no rc=0 step is
    # refused here: every run-side invariant iterates pipeline_steps and passes
    # vacuously over none, and derive_pipeline_status([]) is a valid "in_progress".
    _ran = [s for s in (draft.get("pipeline_steps") or [])
            if isinstance(s, dict) and s.get("returncode") in (None, 0)]
    if not _ran:
        _total = len([s for s in (draft.get("pipeline_steps") or []) if isinstance(s, dict)])
        return refused(
            "seal.no_validated_run", success=False, stage="validated_run",
            error=(f"this draft records no validated run: {_total} recorded pipeline_steps, none of "
                   f"which exited 0. A WorkflowSpec IS the record of a run — with no "
                   f"successful step every run-side invariant examines nothing "
                   f"and passes vacuously, so the seal would be green over an empty premise. "
                   f"Run a step with run_step_in_container / run_step_on_cluster first."),
            pipeline_steps_total=_total, pipeline_steps_succeeded=0,
        )

    violations = check_workflow_invariants(draft)
    if violations:
        return refused("seal.workflow_invariants", success=False,
                       stage="workflow_invariants",
                       violations=violations, violation_count=len(violations))

    # Typed-record gate, re-run at serve. The draft on
    # disk normally cannot violate this — check_draft refuses the write — but a
    # hand-edited yaml is adopted in-process, and the clauses the types carry
    # (I6.absolute_paths, I7.resource_usage_recorded) are not in the walk
    # above. Gating HERE, before the I4 self-test, refuses the cheap way (no
    # container runs spent on a malformed draft) and holds for write=False,
    # where write_workflow_spec's own model_validate never runs.
    try:
        from agent.skills import typed_nouns
        typed_nouns.check_draft(draft, source=f"seal:{pipeline_id}")
    except typed_nouns.TypedNounViolation as e:
        return refused("seal.record_shape_violation", success=False,
                       stage="typed_record_gate",
                       error=str(e),
                       fix="A record already in the draft does not satisfy its typed model. "
                           "pipeline_steps is not patchable: re-run the offending step "
                           "(replace_step=N) through its run primitive, or "
                           "discard_pipeline_draft and rebuild the draft.")

    # WHICH FROZEN ENVS THIS RUN TOUCHED.
    #
    # MULTI-ENV CHAINING: a workflow may chain steps that each ran in their OWN
    # frozen env (their own freeze). Validate every step's container digest against
    # the set of ALL frozen env digests, not just the one we sealed from — so a
    # multi-env workflow is still "validated == shipped" when every step ran in some
    # shipped env. `fr` remains the PRIMARY env (the guide's get-the-image section).
    from agent.models.core_data import step_is_validated
    all_envs = _ms._env_cache.all()
    valid_digests = {r.get("image_digest") for r in all_envs.values() if r.get("image_digest")}
    by_digest = {r.get("image_digest"): (k, r) for k, r in all_envs.items() if r.get("image_digest")}
    envs_used: list[dict] = []
    seen_dig: set = set()
    for s in draft.get("pipeline_steps", []):
        if not isinstance(s, dict):
            continue
        d = s.get("container_image_digest")
        is_validated = step_is_validated(s)
        if d and is_validated and d not in seen_dig:
            seen_dig.add(d)
            rk, rr = by_digest.get(d, ("", {}))
            envs_used.append({"request_key": rk, "image": rr.get("image", s.get("container_image", "")),
                              "image_digest": d})

    # I4: THE HOW-TO IS DERIVED FROM THE STEPS. The run tools recorded each step's
    # template, bindings and working directory; the seal reads those into the `usage` +
    # `usage_verification` record every reader already knows. Nothing is re-run — the
    # steps' own validated outputs are the proof — and a how-to that says anything the
    # steps did not run cannot be written.
    derived = derive_usage(draft)
    if derived.get("problems"):
        return refused(
            "seal.howto_underivable", success=False, stage="howto",
            error=("the how-to is derived from the recorded steps and cannot be: "
                   + "; ".join(derived["problems"])),
            problems=derived["problems"],
        )
    usage_detail = derived["usage_verification"]


    # The how-to is rendered from the verified `usage` block into the Layer-2 run
    # dashboard (HTML) below. We still pull
    # key_packages for the spec's driver_env record.
    key_packages = _key_packages(draft)

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
        # DERIVED from the run, not the draft's dead stamp — the producer states
        # the real status (fully_validated / partially_validated / …). See
        # spec_writer.derive_pipeline_status; the model field is now required.
        "pipeline_status":    derive_pipeline_status(draft.get("pipeline_steps", [])),
        "usage_verified":     True,
        # The how-to's verification record: source `steps`, and per sample the literal
        # commands the steps ran, the image each ran in, what they produced and what
        # each file was validated as — the transcript a reader needs to answer "proven
        # against WHICH genome, WHICH reads", carried through from the steps.
        "usage_verification": usage_detail,
        "validated_in_shipped_image": _validated_in_shipped_image(
            draft, fr, valid_digests=valid_digests),
        "usage":              derived["usage"],
        # Stamp each step's depends_on (input↔prior-output overlap) — the edge the
        # seal already computes to CHECK I8 — so the sealed spec is self-documenting,
        # not just self-verifying.
        "pipeline_steps":     _derive_step_dependencies(draft.get("pipeline_steps", [])),
        # External sources carried so the artifact self-verifies (I8 standalone).
        "test_data":            draft.get("test_data"),
        # Which of the three the test-data pin actually is. Stated rather than inferred:
        # the invariant only ever emits a violation, so "no complaint" reads identically
        # for "every input re-verified against its selection-time anchor" and "nothing
        # was anchored, so nothing was compared".
        "test_data_integrity":  {k: v for k, v in verify_test_data(draft).items()
                                 if k != "violations"},
        "reference_databases":  _refresh_reference_databases(draft.get("reference_databases", [])),
        "runtime_configs":      draft.get("runtime_configs", []),
        "authored_artifacts":   draft.get("authored_artifacts", []),
        # Runtime prerequisites, carried for the same self-verification reason:
        # without them the artifact's I10 re-check has nothing to look at.
        "service_dependencies": draft.get("service_dependencies", []),
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
    # THE SEAL IS GREEN ON THE RUN, AND THE HOW-TO IS THE RUN: one terminal. A sample that
    # ran only some of the commands is a stated limit on the record and on the response,
    # never a second verdict. A run validated OUTSIDE the image this seal pins is the other
    # stated limit: the record is sealed, but what was validated is not what ships, so the
    # answer is degraded — the pipeline layer refuses such a seal (no stage has an image).
    fields = dict(
        success=True, workflow_name=wname,
        env_pinned_digest=fr.get("content_digest"), env_image=fr.get("image"),
        commands_shown=len(_executed_commands(draft)),
        howto_commands=len(_core_data.usage_commands(derived["usage"])),
        samples=[t.get("name") for t in usage_detail.get("trials") or []],
        usage_verification=usage_detail,
        validated_in_shipped_image=wf["validated_in_shipped_image"],
    )
    if wf["validated_in_shipped_image"]:
        result = proven("seal.sealed", **fields)
    else:
        result = degraded(
            "seal.sealed_host_only", **fields,
            reason="the record is sealed, but the steps it validated did not run inside the image this seal "
                   "pins (the host, or another image): what was validated is not what ships, and "
                   "render_pipeline refuses a seal whose stages have no image",
            remedy="run the data steps with run_step_in_container (the same commands, bindings and "
                   "watch_dir) and seal again")
    if usage_detail.get("partial"):
        result["samples_incomplete"] = usage_detail["partial"]
        result["advisory"] = ("these samples ran only some of the how-to's commands and are not "
                              "trials of the record: " + "; ".join(
                                  f"{p['name']} is missing command(s) {p['missing_commands']}"
                                  for p in usage_detail["partial"]))
    if write:
        env_name = _env_name_of(fr, draft)
        out_dir = _workspace.env_dir(env_name)
        # refuse to silently clobber a prior sealed spec that
        # pins a DIFFERENT env (locus accretion writes through). See
        # _guard_spec_overwrite — this is the terminal-WRITE gate, and it fires
        # BEFORE write_workflow_spec so no digest-pinned provenance is destroyed.
        proceed, refusal = _guard_spec_overwrite(wf, out_dir, supersede)
        if not proceed:
            return refusal
        if wf.get("superseded"):
            result["superseded"] = wf["superseded"]
        out = write_workflow_spec(wf, _ms.config, env_name=env_name)
        if out.get("error"):
            return broke("seal.spec_write_failed", success=False, **out)
        result.update(out)
        # Orientation pointer: record that this pipeline sealed
        # `wname`, so current_state can RE-EARN SEALED. Best-effort — a pointer
        # hiccup must never fail a verified seal.
        try:
            _ms._pipeline_state.append_sealed_pointer(pipeline_id, wname)
        except Exception:
            pass
        # Layer-2 deliverable: render THIS workflow's run dashboard
        # ({workflow_name}.RUN.html) from the verified spec — validated evidence
        # per compute locus + a distinct how-to panel. The env's ENV.html (Layer 1)
        # is left untouched: it is immutable post-freeze and never claims the
        # cluster-worthiness this dashboard carries. Non-fatal — a render hiccup
        # never fails a verified seal.
        report_path = _render_run_dashboard(wf, fr, out_dir)
        if report_path:
            result["run_report_path"] = report_path
    else:
        result["workflow"] = wf
    return result


@mcp.tool()
def list_installed_pipelines(detail: bool = False) -> dict:
    """What has ALREADY been built here — check this before solving a tool again.

    Returns both layers:
      `envs[]`     — Layer 1: the frozen, content-addressed envs in the EnvCache.
                     These are the reusable "solved components": ask for one of
                     these tools again and freeze serves it BY DIGEST instead of
                     rebuilding.
      `workflows[]`— Layer 2: the sealed WorkflowSpecs, each pinning its env by
                     digest.

    COMPACT BY DEFAULT — ~83 tokens per env and ~64 per workflow, enough to answer
    "do I already have this, and would it still be served?". Pass `detail=True` for
    image/content digests, platform, validation_locus, created_at, per-clause
    contract coverage, full per-tool version records and artifact paths (~5x larger).

    **`contract_ok` is earned, not remembered.** Every env is re-anchored against
    the full honesty contract AS YOU ASK (`env_honesty.check_build`, the same
    question freeze/run/stage/seal ask at serve time). `contract_ok: False` means
    "on disk, but the serving paths would REFUSE it today" — a record whose green
    has expired, e.g. because a gate got stronger since it was frozen. An inventory
    that showed a stale record as usable would be precisely the false green tier 5
    closed.

    **`assurance_unproven[]` — a green that rests on less.** `contract_ok` is a
    two-state answer to a three-state question: a clause can pass, fail, or have had
    NOTHING TO LOOK AT, and only the first two reach a boolean. This field names the
    ASSURANCE clauses that reached no verdict, so a record which passed without ever
    being tested on a point is distinguishable from one that was. Most commonly
    `VALIDATED_IN_IMAGE.discriminates` — the evidence was never re-run in a control
    image lacking the tool, so nothing establishes it would FAIL there. Absent when the
    contract was fully observed; re-freeze to earn it. (Disclosure-only shortfalls, like
    `shipped_binaries` on an adopt, stay on `detail=True` — they are unobserved on
    nearly every adopt record, and a warning that is always present is one nobody reads.)

    Compaction never hides a problem: `contract_violations[]` (naming the failing
    clause), `assurance_unproven[]`, a tool whose installed version DIVERGES from what
    was requested, and the reason a how-to is unverified all appear in the compact form
    too. Omitting what is absent or default is compression; omitting a warning is
    something else.
    """
    return _ms._list_pipelines(_ms.config, env_cache=_ms._env_cache, detail=detail)


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
        # ONE lifecycle answer, re-earned from the artifacts — replaces the dead
        # env_status/pipeline_status nominal stamps that were never transitioned
        #. state_checks binds the re-earned frozen/sealed checks.
        from agent.skills.pipeline_state import current_state, state_checks
        _state = current_state(draft, **state_checks(_ms._env_cache))
        r["summary"] = {
            "conda_env":              draft.get("conda_env"),
            "state":                  _state,
            "install_steps_count":    len(install_steps),
            "install_steps_failed":   sum(1 for s in install_steps if s.get("returncode") not in (None, 0)),
            "pipeline_steps_count":   len(pipeline_steps),
            "pipeline_steps_failed":  sum(1 for s in pipeline_steps if s.get("returncode") not in (None, 0)),
            "packages_recorded":      len(draft.get("packages", []) or []),
            "test_data":              draft.get("test_data") is not None,
            "docker":                 draft.get("docker") is not None,
            "last_install_tool":      last_install.get("tool") if last_install else None,
            "last_pipeline_tool":     last_pipeline.get("tool") if last_pipeline else None,
            "suspect_unvalidated_steps": suspect_steps,
        }
    return r


@mcp.tool()
def discard_pipeline_draft(pipeline_id: str, step: int = 0) -> dict:
    """Delete a pipeline draft without finalizing. Use when you want to start
    fresh with the same pipeline_name and don't want resume semantics.

    `step=N` drops only step N instead, when that step FAILED (a recorded attempt you
    abandon rather than retry; a draft seals as a failed run while it stays). A step
    that ran clean is evidence and is never dropped — replace it with `step=N` on its
    run primitive. Later steps are renumbered."""
    if step:
        r = _ms._pipeline_state.remove_step(pipeline_id, step)
        if not r.get("ok"):
            return refused("discard_draft.step_kept", success=False, pipeline_id=pipeline_id,
                           step=step, error=r.get("reason", ""))
        return proven("discard_draft.step_dropped", pipeline_id=pipeline_id, step=step,
                      remaining_steps=r.get("remaining"))
    return _ms._pipeline_state.discard(pipeline_id)


@mcp.tool()
def show_pipeline_draft(pipeline_id: str) -> dict:
    """Return the current accumulated draft for inspection. Does not modify
    or finalize anything."""
    draft = _ms._pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return _ms._pipeline_state.unknown_draft_refusal("show_draft.unknown_pipeline", pipeline_id)
    return proven("show_draft.ok", pipeline_id=pipeline_id, draft=draft)


@mcp.tool()
def patch_pipeline(pipeline_id: str, patches: dict) -> dict:
    """Deep-merge agent-authored patches into the draft. Accepts only the
    keys no primitive produces directly (`PipelineState.PATCHABLE_KEYS`):
    description, notes, final_summary, conda_env, created_at, python_version,
    reference_free, runtime_environment, runtime_configs, reference_databases,
    accelerator, license_gated, licenses, redistributable.

    The how-to is not patched: `usage` is DERIVED by seal_workflow from the recorded
    steps (their command with its `{UPPER_SNAKE}` slots and the `bindings=` each took),
    so the variable parts of a step are declared at the run call, never typed again.

    Patches to runtime-captured or finalize-derived fields (pipeline_steps,
    install_steps, packages, verifications, search_cache, test_data, usage,
    authored_artifacts, service_dependencies, docker, env_status,
    pipeline_status, docker_status, usage_verified, lock_sha256, frozen_as,
    sealed_as) are rejected — use the dedicated primitive instead so the spec
    stays anchored to observed reality.

    `reference_databases` merges by `name` (a patch adds or amends records and keeps
    the ones the download and acquire primitives wrote); every record must fit
    ReferenceDatabase (`local_path` required, no unknown keys) or the patch is
    refused with the field named. Other lists are replaced wholesale.

    Deletion: pass the literal string "__DELETE__" as a value inside a
    patchable key's subtree to remove that nested key. e.g.
    {"runtime_environment": {"min_ram_gb": "__DELETE__"}} drops the
    min_ram_gb hint while leaving the rest of runtime_environment intact.
    Use this rather than setting to None — None can trigger downstream
    attribute errors."""
    return _ms._pipeline_state.patch(pipeline_id, patches)


#: The largest file whose full text an authored-artifact record keeps: a script up to
#: this size is carried verbatim into a rendered pipeline's bin/.
SPEC_FULL_LIMIT = 65536


def record_generated_artifact(pipeline_id: str, path: str, role: str,
                              description: str, generated_by: str,
                              language: str = "") -> tuple[Optional[int], dict]:
    """THE one construction of a generated_by-mode authored-artifact record.

    Reads the existing file at `path`, builds the artifact dict (sha256 anchor, size,
    genesis command, and, for a script of UTF-8 text up to 64 KiB, its full text as
    content mode keeps it, so a render carries it into bin/; any other file gets a
    binary preview and stays a path input) and merges it into the draft. Returns
    (artifact_index | None-for-unknown-pipeline, artifact). Raises OSError if the file
    cannot be read — callers stage files they just observed on disk.

    Extracted so a PRODUCER PRIMITIVE can register its own output: a producer such as
    `phenopacket_to_vcf` that hands back an unanchored path is handing back an I8-orphan
    seal refusal on delay, so it anchors what it writes here, just as `select_test_data`
    registers what it selects. `stage_authored_artifact`'s generated_by
    mode delegates here — one implementation, two doors.
    """
    p = Path(path)
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8") if _is_script_path(path) and len(raw) <= SPEC_FULL_LIMIT else None
    except UnicodeDecodeError:
        text = None
    artifact = {
        "path":        str(p),
        "role":        role,
        "description": description,
        "sha256":      hashlib.sha256(raw).hexdigest(),
        "size_bytes":  len(raw),
        "created_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "content_excerpt": text if text is not None else f"<binary; first 64 bytes hex: {raw[:64].hex()}>",
        "content_full_in_spec": text is not None,
        "generated_by": generated_by,
    }
    if language:
        artifact["language"] = language
    return _ms._pipeline_state.add_authored_artifact(pipeline_id, artifact), artifact


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
                          bytes, keeping a script's full text (up to 64 KiB)
                          as content mode does. Use for a small fixture you
                          produced (a staged BAM slice, a pickled model) or an
                          existing script run unchanged (`cp <script> <path>`
                          as the genesis), so it is never re-sent as text.
                          A reference you downloaded
                          or BUILT — an index, an annotation, a reference subset,
                          any directory — is a reference database instead:
                          download_reference_database(name, url) or
                          download_reference_database(name, url="", local_path=…).

    At render, a file whose full text the record keeps (any content-mode file, or a
    generated_by script up to 64 KiB) is carried into the pipeline's bin/; any other
    file bound to a step input stays a path input pinned by sha256.

    Honesty effect:
      - Path is added to the I8 universe of external sources, so downstream
        pipeline_steps can legally name it as an input.
      - sha256 is re-verified at seal (`I8.authored_artifact_mutated`). If the
        on-disk content drifts from the recorded sha256, the seal is refused.
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
    try:
        corpus = _workspace.resources_root().resolve()
        in_corpus = corpus == p.resolve() or corpus in p.resolve().parents
    except Exception:
        in_corpus = False
    if in_corpus:
        return refused(
            "stage_artifact.path_in_resources",
            error=f"{path} is under the resources corpus ({corpus}), which holds inputs, not files you "
                  "wrote; an existing dataset file is recorded with select_test_data or "
                  "download_reference_database(url='', local_path=…), and a file you author is "
                  "written under the workspace",
            path=path,
        )

    if not content:
        # generated_by mode: the record construction lives in record_generated_artifact
        # (one implementation — producer primitives like phenopacket_to_vcf ride the
        # same helper to register their own outputs).
        if not p.exists():
            return refused(
                "stage_artifact.source_missing",
                error=f"generated_by mode requires the file to already exist on disk: {path}",
            )
        if p.is_dir():
            return refused(
                "stage_artifact.path_is_directory",
                error=f"an artifact is one file and this is a directory: {path}",
                remedy="stage each file inside it (one generated_by call per file); a step "
                       "whose input is the directory then traces through them, because I8 "
                       "accepts a directory input when a declared file sits directly in it",
            )
        try:
            idx, artifact = record_generated_artifact(
                pipeline_id, path, role, description, generated_by, language)
        except OSError as e:
            return broke("stage_artifact.readback_failed", error=f"could not read back artifact for sha256: {e!r}", path=path)
        if idx is None:
            return _ms._pipeline_state.unknown_draft_refusal("stage_artifact.unknown_pipeline", pipeline_id, path=path)
        return proven(
            "stage_artifact.staged",
            success=True,
            path=str(p),
            sha256=artifact["sha256"],
            size_bytes=artifact["size_bytes"],
            role=role,
            mode="generated_by",
            pipeline_merge={
                "status":        "merged",
                "pipeline_id":   pipeline_id,
                "artifact_index": idx,
            },
        )

    if p.exists() and not overwrite:
        return refused("stage_artifact.exists_no_overwrite", error=f"path already exists and overwrite=False: {path}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    except Exception as e:
        return broke("stage_artifact.write_failed", error=f"could not write artifact: {e!r}", path=path)

    try:
        raw = p.read_bytes()
    except Exception as e:
        return broke("stage_artifact.readback_failed", error=f"could not read back artifact for sha256: {e!r}", path=path)

    sha256 = hashlib.sha256(raw).hexdigest()
    size_bytes = len(raw)

    # Embed the full text up to SPEC_FULL_LIMIT; otherwise an excerpt.
    if size_bytes <= SPEC_FULL_LIMIT:
        excerpt = content
        content_full_in_spec = True
    else:
        excerpt = content[:4096] + f"\n... [truncated, total {size_bytes} bytes]"
        content_full_in_spec = False

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
    artifact["content_excerpt"] = excerpt
    artifact["content_full_in_spec"] = content_full_in_spec
    if generated_by:
        artifact["generated_by"] = generated_by

    idx = _ms._pipeline_state.add_authored_artifact(pipeline_id, artifact)
    if idx is None:
        return _ms._pipeline_state.unknown_draft_refusal("stage_artifact.unknown_pipeline", pipeline_id, path=path)

    return proven(
        "stage_artifact.staged",
        success=True,
        path=str(p),
        sha256=sha256,
        size_bytes=size_bytes,
        role=role,
        mode="content",
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
    draft = _ms._pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return _ms._pipeline_state.unknown_draft_refusal("mark_validated.unknown_step", pipeline_id, step=step)
    steps = sorted(int(s.get("step")) for s in (draft.get("pipeline_steps") or [])
                   if isinstance(s, dict) and str(s.get("step", "")).isdigit())
    return refused("mark_validated.unknown_step",
                   error=f"step {step} is not in the draft; recorded steps: {steps or 'none'}",
                   pipeline_id=pipeline_id, step=step, recorded_steps=steps)
