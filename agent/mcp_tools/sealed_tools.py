"""sealed_tools — RUN_STEP-of-a-sealed-workflow, the typed reader.

ONE tool: `describe_sealed_step`. It is the MCP surface for re-running a single
recorded step of an existing SEALED pipeline (docs/design_reverse_theme_park.md §7
scenario 5: "just generate step 2 of my existing pipeline").

WHY ITS OWN MODULE (not workflow_tools). This is the third member of the
reverse-theme-park ADVISORY-READER family — `interpret_request` (intent_tools, the
front gate) · `plan_request` (plan_tools, composition) · `describe_sealed_step` (here,
the RUN_STEP-of-sealed reader). All three DISPATCH NOTHING and return an untagged
`{ok: …}` query dict, deliberately OFF the proven/refused honesty namespace: a read
that reports facts is a different axis from an outcome that gates an action (the
ShippedBinary "one key, one meaning" lesson). workflow_tools.py holds the honesty
PRIMITIVES (seal_workflow / patch_pipeline / …), each returning a proven/refused
terminal — and it is a FULLY-TAGGED file (tests/test_outcome_tags.py). An advisory
reader's untagged `{ok/error}` returns belong with its siblings, not among tagged
primitives; keeping them here is what keeps that ratchet honest.

The typed SEAM this reader stands on lives in `spec_writer`
(`load_workflow_spec` / `select_pipeline_step`) — a sealed spec is read back through
the WorkflowSpec model, never scraped (the bcftools-1.23.1 lesson: a `.get`/regex over
a record returns the wrong field under a confident name, and "the wrong command in the
right container" is a silent, ground-truthed lie).

Singletons via `_ms.X` so test monkeypatching on mcp_server reaches us — same binding
convention as env_tools.py / workflow_tools.py.
"""
from __future__ import annotations

from pathlib import Path

from agent import mcp_server as _ms
from agent.mcp_server import mcp


@mcp.tool()
def describe_sealed_step(workflow_name: str, step: int) -> dict:
    """RUN_STEP-of-a-sealed-workflow — the typed reader for re-running ONE recorded
    step of an existing SEALED pipeline (docs/design_reverse_theme_park.md §7
    scenario 5: "just generate step 2 of my existing pipeline").

    Reads ``env_reports/{workflow_name}.workflow.yaml`` through the TYPED seam
    (``spec_writer.load_workflow_spec`` → a validated ``WorkflowSpec``, NEVER a
    scraped dict), selects the recorded step numbered ``step`` (1-based), and returns
    everything needed to re-run it IN THE FROZEN ENV IMAGE: the command verbatim, its
    input paths, the frozen env's ``freeze_request_key``, the pinned digest, and a
    precondition read of which inputs exist on disk right now.

    IT DISPATCHES NOTHING — advisory, exactly like ``interpret_request`` /
    ``plan_request`` (and returns ``{ok: …}``, off the proven/refused honesty
    namespace, because it is a READ, not an outcome). The RUN ride stays the sole
    executor with its gate intact: take the returned ``freeze_request_key`` +
    ``command`` + ``inputs`` and call ``run_step_in_container`` (which re-anchors the
    env against the Layer-1 contract and validates the produced output). Re-running
    happens in the frozen image — validated == shipped.

    NO AUTO-MATERIALIZE. Inputs are the paths the step consumed when it ran; this
    tool does not regenerate them by re-running upstream steps (that silent cascade
    would be the forbidden composite). If ``all_inputs_present`` is false, a prior
    step's output is missing — run that step (or supply the file) first, rather than
    letting the container run produce nothing. The precondition is surfaced HERE so
    the gap is loud before you spend a container run on it.

    Returns on success::

        { "ok": true,
          "workflow_name": ..., "step": N,
          "run_with": "run_step_in_container",
          "freeze_request_key": ...,        # pass as run_step_in_container(freeze_request_key=…)
          "command": ...,                    # the recorded command, verbatim
          "inputs": [ "<abs path>", … ],     # recorded input paths
          "tool": ...,
          "pinned_env": { "request_key", "content_digest", "image",
                          "available", "contract_ok", "contract_violations" },
          "preconditions": [ { "path", "exists" }, … ],
          "all_inputs_present": bool,
          "steps": [ { "step", "tool", "command" }, … ] }   # the full roster, for context

    Returns on a miss (so the caller re-picks against the real names/numbers)::

        { "ok": false, "error": ..., "available_workflows": [...] }   # no such workflow
        { "ok": false, "error": ..., "available_steps": [...], "steps": [...] }  # no such step
    """
    from agent.skills.spec_writer import load_workflow_spec, select_pipeline_step

    project_root = Path(__file__).resolve().parents[2]
    reports_dir = project_root / _ms.config["paths"]["pipelines_dir"]
    spec_path = reports_dir / f"{workflow_name}.workflow.yaml"
    if not spec_path.exists():
        available = (sorted(p.name[: -len(".workflow.yaml")]
                            for p in reports_dir.glob("*.workflow.yaml"))
                     if reports_dir.is_dir() else [])
        return {"ok": False,
                "error": f"no sealed workflow '{workflow_name}' at {spec_path}",
                "available_workflows": available}

    try:
        spec = load_workflow_spec(spec_path)
    except Exception as e:
        # The typed seam refused a malformed artifact — the whole point of reading
        # back through the model instead of scraping. Surface it, never execute past it.
        return {"ok": False,
                "error": f"sealed workflow '{workflow_name}' is not a valid WorkflowSpec: "
                         f"{type(e).__name__}: {str(e)[:400]}"}

    roster = [{"step": st.step, "tool": st.tool, "command": st.command}
              for st in spec.pipeline_steps]
    try:
        st = select_pipeline_step(spec, step)
    except Exception as e:
        return {"ok": False, "error": str(e),
                "available_steps": [r["step"] for r in roster], "steps": roster}

    input_paths = [si.path for si in st.inputs]
    preconditions = [{"path": p, "exists": Path(p).exists()} for p in input_paths]
    all_present = all(pc["exists"] for pc in preconditions)

    # Disclose whether the pinned env is STILL frozen and STILL passes the Layer-1
    # contract today — the same lookup_verified question run_step_in_container will
    # ask — so a stale/evicted env is visible here, before a run is attempted.
    rec, violations = _ms._env_cache.lookup_verified(spec.env_request_key)
    pinned_env = {
        "request_key":         spec.env_request_key,
        "content_digest":      spec.env_content_digest,
        "image":               spec.env_image,
        "available":           rec is not None,
        "contract_ok":         (rec is not None) and (not violations),
        "contract_violations": violations or [],
    }

    return {
        "ok": True,
        "workflow_name": spec.workflow_name,
        "step": step,
        "run_with": "run_step_in_container",
        "freeze_request_key": spec.env_request_key,
        "command": st.command,
        "inputs": input_paths,
        "tool": st.tool,
        "pinned_env": pinned_env,
        "preconditions": preconditions,
        "all_inputs_present": all_present,
        "steps": roster,
    }
