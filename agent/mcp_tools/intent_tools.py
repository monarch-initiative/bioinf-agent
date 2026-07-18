"""intent_tools — the typed front door.

ONE tool: `interpret_request`. It is the MCP surface of `agent/skills/intent.py`
(docs/design_reverse_theme_park.md §2-3) — you pass your structured reading of the
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
    """THE FRONT DOOR — turn your reading of the user's request into a routed decision.

    Call this FIRST, before any resolve / install / freeze primitive, whenever you are
    starting to act on a user request. You pass a `RequestIntent` as a JSON string —
    YOUR interpretation of the prompt — and this validates it and routes it.

    THE ONE RULE: "unknown" is a value you must STATE, never a default you invent. If
    the user did not give a version, `version` is `null` AND you add an entry to
    `unknowns[]` — do NOT silently fill "latest". A gap is a fact to write down; a
    fabricated default authors an intent the user never expressed (the reason this
    validates instead of trusting a dict).

    RequestIntent JSON (EVERY field required — state `null` explicitly for anything the
    user did not give):

        {
          "kind": one of install_env | add_to_env | run_step | transfer_data |
                  reproduce | out_of_scope | ambiguous,
          "raw_prompt": the user's words, verbatim,
          "tools": [ { "name": str, "version": str|null,
                       "source_hint": {"kind": github_repo|release_url|channel|image|recipe,
                                       "value": str} | null,
                       "purpose": str|null } ],   // [] when there are no tools
          "target_env": str|null,                 // an existing env, for add/run/reproduce
          "compute": local | cluster | unspecified,
          "data_ops": [ { "direction": upload|download,
                          "source": str|null, "dest": str|null } ] | null,  // null unless transfer
          "unknowns": [ { "field": str,           // dotted: "talos.version", "tool_name"
                          "findable": bool,        // can WE look it up? true→INVESTIGATE, false→ASK
                          "reason": str } ],
          "out_of_scope_reason": str|null          // required non-null iff kind==out_of_scope
        }

    THE GATE (what comes back in `outcome`):
      - decline     → out of scope. Explain the boundary; do nothing.
      - ask         → the intent is ambiguous, or a required detail is un-findable
                      (`findable: false`). Ask ONE targeted question, then re-interpret.
      - investigate → intent is clear but a mechanical detail is missing AND findable
                      (`findable: true`). Look it up (resolve_tool / discovery / a
                      near-miss resolve), RECORD how, then re-interpret to PROCEED.
      - proceed     → every required field is known. Run the rail named in `rail`.

    `findable` is the whole ask/investigate split: a version omitted is findable
    (INVESTIGATE — find latest); two versions transposed by accident (scenario 4) are
    NOT findable (ASK — do not silently 'correct' them). You decide `findable`; the gate
    routes on it.

    Returns on success:
        { "ok": true, "gate_outcome": ..., "rail": ...,
          "blocking": [ <the unknowns driving ask/investigate> ],
          "intent": { <the normalized, validated intent> } }

    (The key is `gate_outcome`, not `outcome`, on purpose: `outcome` is the honesty
    contract's namespace — proven/refused/broke — and the gate's verdict is a
    DIFFERENT axis. Not overloading one key with two meanings is the ShippedBinary
    lesson applied here.)

    Returns on a malformed intake (bad JSON, missing/extra field, a DECLINE with no
    reason): { "ok": false, "error": <the validation error> }. A fabricated or
    ill-formed interpretation fails HERE, loudly, instead of routing downstream — the
    input-side analog of the honesty contract refusing a malformed record.
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
