"""plan_tools — the composition front door.

ONE tool: `plan_request`. It is the MCP surface of `agent/skills/plan.py`
(docs/design_reverse_theme_park.md §6) — you pass your decomposition of a multi-rail
goal as a JSON `ExecutionPlan`, it validates that decomposition against the schema and
returns the plan gate's verdict (I8 lifted to authoring time) plus the topological walk
order and where to start.

ADVISORY, DISPATCHES NOTHING (Phase 4). Exactly like `interpret_request`: this changes
no existing behaviour. It hands you a validated, gated itinerary; YOU walk it, calling
the existing single-purpose primitives (freeze / run_step_in_container /
run_step_on_cluster / seal_workflow) one node at a time in topo order, observing each
real result before advancing. The hard gates stay at the EXIT (the honesty contract).
There is deliberately NO plan_execute — a tool that loops the DAG calling those
primitives internally is the forbidden composite primitive AND would bury the per-seam
gates the whole thesis rests on.

Singletons via `_ms.X` so test monkeypatching on mcp_server reaches us — same binding
convention as intent_tools.py / workflow_tools.py.
"""
from __future__ import annotations

import json

from agent import mcp_server as _ms
from agent.mcp_server import mcp


@mcp.tool()
def plan_request(plan_json: str) -> dict:
    """COMPOSE A MULTI-RAIL GOAL — turn your decomposition into a gated ExecutionPlan.

    Call this AFTER `interpret_request` returns PROCEED on a goal that spans more than
    one rail (scenario 15: "install x, y, z as separate envs, build a pipeline, run it
    on the cluster"). You pass an `ExecutionPlan` as a JSON string — YOUR decomposition
    of the goal into an ordered DAG of rail/ride nodes — and this validates it and gates
    it (Layer-2's I8 at authoring time: does every input trace to a prior node or a
    declared external source? is it an acyclic DAG? does a sink produce the goal?).

    IT DISPATCHES NOTHING. It returns the topological order and the starting frontier;
    you then walk the plan yourself, one node per primitive, in that order — freeze for
    an INSTALL_ENV node, run_step_in_container / run_step_on_cluster for a RUN_STEP node,
    seal_workflow for a SEAL node — observing each real result before the next node.

    THE MODEL (send ONLY these; produces / depends_on / rail are DERIVED — do not send
    them, an extra key is a hard error):

        {
          "goal": { "statement": the user's words verbatim,
                    "produces": env_digest | file_set | transfer | workflow_spec },
          "steps": [
            { "id": str,
              // EXACTLY ONE of intent (a rail node) or seam (a ride node):
              "intent": <a full RequestIntent, exactly as interpret_request takes> | null,
              "seam":   "SEAL" | null,
              // what this node consumes; each entry states EXACTLY ONE leg:
              "consumes": [ { "from_step": <a prior step id>, "external": null }
                          | { "from_step": null,
                              "external": { "kind": test_data | reference_databases |
                                                    runtime_configs | authored_artifacts,
                                            "ref": a symbolic handle, e.g.
                                                   "project:/proj/xyz/reads" } } ]
            }
          ]
        }

    RULES (each a real discipline, not a nicety):
      - A rail node carries an `intent`; a SEAL node carries a `seam`. Never both, never
        neither. A node's intent must itself be PROCEED-able (an ASK sub-intent blocks
        the whole plan; a findable INVESTIGATE gap rides along, non-blocking).
      - A source node (INSTALL_ENV / REPRODUCE / TRANSFER_DATA) may consume nothing
        (`consumes: []`) — STATE the empty list. Every other node must trace its input
        to a prior node (from_step) or a declared external source (or, for a RUN/ADD
        against an EXISTING frozen env, set intent.target_env).
      - Project/cluster data is an external source expressed through the four kinds
        (e.g. kind=test_data, ref="project:/proj/xyz/reads"); the project directory +
        compute resource are authorized via projects_access.yaml and resolved when you
        walk the node — no new category here.

    Returns on success:
        { "ok": true, "plan_ready": bool,
          "violations": [ {invariant, message, node_id} ],   // why it's not ready
          "topo_order": [ step ids in walk order ],          // empty on a cycle
          "ready_frontier": [ step ids to START with ],      // the parallelizable sources
          "pending_investigation": [ {node_id, unknowns} ],  // findable gaps to fill while walking
          "goal_reached": bool,
          "plan": { <the normalized, validated plan> } }

    (The key is `plan_ready`, not `outcome`: `outcome` is the honesty contract's
    namespace — proven/refused/broke — and the plan gate's verdict is a DIFFERENT axis,
    same discipline as interpret_request's `gate_outcome`.)

    Returns on a malformed plan (bad JSON, a step with both intent and seam, a consume
    with neither leg, a DECLINE/CLARIFY rail node, a faked `produces` key):
    { "ok": false, "error": <the validation error> }. A fabricated or ill-formed
    itinerary fails HERE, loudly — the input-side analog of the honesty contract
    refusing a malformed record.
    """
    try:
        raw = json.loads(plan_json)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"plan_json is not valid JSON: {e}"}
    if not isinstance(raw, dict):
        return {"ok": False, "error": "plan_json must be a JSON object (the ExecutionPlan)"}

    try:
        plan = _ms._plan.parse_plan(raw)
    except Exception as e:  # pydantic.ValidationError or a post-init shape check
        return {"ok": False, "error": f"ExecutionPlan validation failed: {e}"}

    result = _ms._plan.gate_plan(plan)
    return {
        "ok": True,
        "plan_ready": result.plan_ready,      # NOT an honesty tag — the gate's own axis
        "violations": [v.model_dump() for v in result.violations],
        "topo_order": result.topo_order,
        "ready_frontier": result.ready_frontier,
        "pending_investigation": result.pending_investigation,
        "goal_reached": result.goal_reached,
        "plan": plan.model_dump(),
    }
