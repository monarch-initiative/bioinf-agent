"""plan_tools — the composition front door.

ONE tool: `plan_request`. It is the MCP surface of `agent/skills/plan.py`
— you pass your decomposition of a multi-rail
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
    """Compose a multi-rail goal: validate and gate an `ExecutionPlan`, your decomposition
    of the goal into an ordered DAG of rail/ride nodes. Call it after `interpret_request`
    returns PROCEED on a goal spanning more than one rail. The gate is I8 at authoring time:
    every input traces to a prior node or a declared external source, the graph is acyclic,
    a sink produces the goal.

    It dispatches nothing. Walk the returned order yourself, one node per primitive (freeze
    for INSTALL_ENV, run_step_in_container / run_step_on_cluster for RUN_STEP, seal_workflow
    for SEAL), observing each real result before the next node.

    The plan (send only these; produces / depends_on / rail are derived, an extra key is an
    error):

        {"goal": {"statement": the user's words verbatim,
                  "produces": env_digest | file_set | transfer | workflow_spec},
         "steps": [{"id": str,
                    "intent": <a RequestIntent, as interpret_request takes> | null,  # rail node
                    "seam": "SEAL" | null,                                            # ride node
                    "consumes": [{"from_step": <a prior id>, "external": null}
                               | {"from_step": null,
                                  "external": {"kind": test_data | reference_databases |
                                                       runtime_configs | authored_artifacts,
                                               "ref": "project:/proj/xyz/reads"}}]}]}

    Rules: a node carries exactly one of `intent` / `seam`. A rail node's intent must itself
    be PROCEED-able (an ASK sub-intent blocks the plan; a findable INVESTIGATE gap rides
    along). A source node (INSTALL_ENV / REPRODUCE / TRANSFER_DATA) states `consumes: []`;
    every other node traces to a prior node or an external source, or sets
    intent.target_env for a RUN/ADD against an existing frozen env. Project and cluster
    data are external sources through the four kinds; the directory and compute resource
    are authorized via projects_access.yaml when the node is walked.

    Returns `{"ok": true, "plan_ready": bool, "violations": [{invariant, message, node_id}],
    "topo_order": [ids, empty on a cycle], "ready_frontier": [ids to start with],
    "pending_investigation": [{node_id, unknowns}], "goal_reached": bool, "plan": {the
    validated plan}}`. A malformed plan (bad JSON, both or neither of intent/seam, a consume
    with neither leg, a DECLINE or CLARIFY rail node, a faked `produces`) returns
    `{"ok": false, "error": …}`.
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
