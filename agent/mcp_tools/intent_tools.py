"""intent_tools — the typed front door.

ONE tool: `interpret_request`. It is the MCP surface of `agent/skills/intent.py`
— you pass your structured reading of the
user's request as JSON, it validates that reading against the `RequestIntent` schema
and returns the completeness gate's verdict (decline / ask / investigate / proceed)
plus the rail the request belongs to.

BEHAVIOUR-NEUTRAL (Phase 1). This is ADVISORY, exactly like `resolve_tool`: nothing
forces a request through it and it changes no existing behaviour. Its value is that the
intake becomes a validated, routable, testable record instead of an interpretation
improvised fresh each time. The hard gates stay at the EXIT (the honesty contract).

Singletons via `_ms.X` so test monkeypatching on mcp_server reaches us — same binding
convention as env_tools.py / workflow_tools.py.
"""
from __future__ import annotations

import json

from agent import mcp_server as _ms
from agent.mcp_server import mcp


@mcp.tool()
def interpret_request(intent_json: str) -> dict:
    """THE FRONT DOOR — validate and route your reading of the user's request.

    Call this first, before any resolve / install / freeze primitive. Pass a `RequestIntent`
    as a JSON string; it is validated and routed. "unknown" is a value you STATE (`null`
    plus an `unknowns[]` entry), never a default you invent.

    RequestIntent (every field required; `null` for what the user did not give):

        {"kind": install_env | add_to_env | run_step | transfer_data | reproduce |
                 out_of_scope | ambiguous,
         "raw_prompt": the user's words verbatim,
         "tools": [{"name": the user's word verbatim, "version": str|null,
                    "source_hint": {"kind": github_repo|release_url|channel|image|recipe,
                                    "value": str} | null,
                    "purpose": str|null,
                    "resolution": {"package": str, "version": str|null, "why": str} | null}],
         "target_env": str|null, "compute": local | cluster | unspecified,
         "data_ops": [{"direction": upload|download, "source": str|null, "dest": str|null}] | null,
         "unknowns": [{"field": "talos.version", "findable": bool, "reason": str}],
         "out_of_scope_reason": str|null}

    `resolution` is which PACKAGE you believe `name` means, and why ("gatk" → gatk4, because
    the bare `gatk` package is GATK3 from 2017). It is required to proceed; `null` routes to
    INVESTIGATE: call resolve_tool with the raw name, read `package_family` and
    `identity.self_description`, then re-interpret. A tool with a `source_hint` is exempt.
    Then call `resolve_tool(tool=resolution.package, user_said=name)`, which verifies the
    two are one package family and refuses a substitution.

    `findable` is the ask/investigate split: an omitted version is findable (INVESTIGATE);
    two versions the user may have transposed are not (ASK).

    Returns `{"ok": true, "gate_outcome": decline | ask | investigate | proceed, "rail": …,
    "blocking": [the unknowns driving ask/investigate], "intent": {the validated intent}}`.
    decline → out of scope, do nothing. ask → one targeted question, then re-interpret.
    investigate → look the gap up, record how, re-interpret. proceed → run `rail`.
    A malformed intake (bad JSON, a missing or extra field, a decline with no reason)
    returns `{"ok": false, "error": …}` instead of routing anything.
    """
    try:
        raw = json.loads(intent_json)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"intent_json is not valid JSON: {e}"}
    if not isinstance(raw, dict):
        return {"ok": False, "error": "intent_json must be a JSON object (the RequestIntent)"}

    try:
        intent = _ms._intent.parse_intent(raw)
    except Exception as e:  # pydantic.ValidationError or the post-init shape check
        return {"ok": False, "error": f"RequestIntent validation failed: {e}"}

    result = _ms._intent.gate(intent)
    return {
        "ok": True,
        "gate_outcome": result.outcome.value,   # decline/ask/investigate/proceed — NOT an honesty tag
        "rail": result.rail.value,
        "blocking": [u.model_dump() for u in result.blocking],
        "intent": intent.model_dump(),
    }
