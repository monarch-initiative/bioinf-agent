"""
outcomes — the machine-readable outcome vocabulary for terminal returns.

Every terminal (a place the system succeeds, refuses, or fails) is stamped with
an `outcome` class + a stable `code`. This does DOUBLE DUTY:

  1. Runtime affordance — the LIVE response carries its own classification, so a
     caller (the agent) can branch on `outcome` / `code` instead of parsing an
     English error string. `refused` → fix inputs and retry; `broke` → likely
     rebuild; `proven` → proceed.
  2. A reconciled system model — scripts/extract_outcomes.py harvests these tags
     straight from the source (AST, no execution) into docs/outcomes_ledger.json,
     rendered to docs/outcomes_dashboard.html. That model is DERIVED from the code,
     so it cannot silently drift. tests/test_outcome_tags.py makes an untagged
     terminal a build failure.

The six classes:

    proven    ✅  honest green — a validated success
    refused   ⛔  a gate said no, loudly & recoverably, before writing anything
    broke     💥  a hard failure, RECORDED
    vanished  👻  a failure with NO durable trace — an honesty hole (avoid)
    degraded  ⚠️  proceeded with reduced assurance
    loop      🔁  recoverable, feeds back into a retry

Usage — wrap the terminal dict; existing fields are preserved verbatim, two keys
(`outcome`, `code`) are added:

    return refused("seal.no_frozen_env", success=False, error="...")
    return proven("seal.sealed", success=True, workflow_name=wname, ...)

`code` convention: "<subsystem>.<reason>" (e.g. "seal.usage_self_test_failed").
Keep it stable — it is an identifier tests and callers key off, not prose.

When the outcome is RUNTIME-CONDITIONAL, use an explicit if/else with a constant
code per branch — NOT a conditional expression:

    if verified: return proven("freeze.recipe_verified", **f)   # good
    return broke("freeze.recipe_not_reproduced", **f)

    return (proven if verified else broke)("freeze...", **f)     # BAD — invisible
                                                                 # to the extractor
scripts/extract_outcomes.py reads the helper NAME and the constant first arg; a
conditional expression hides both, so the terminal silently drops out of the model.
"""
from __future__ import annotations

PROVEN   = "proven"
REFUSED  = "refused"
BROKE    = "broke"
VANISHED = "vanished"
DEGRADED = "degraded"
LOOP     = "loop"

OUTCOME_CLASSES = (PROVEN, REFUSED, BROKE, VANISHED, DEGRADED, LOOP)

# The helper NAMES, exported so the extractor knows what call sites to harvest.
HELPER_NAMES = ("proven", "refused", "broke", "vanished", "degraded", "loop")


def _tag(kind: str, code: str, fields: dict) -> dict:
    if kind not in OUTCOME_CLASSES:
        raise ValueError(f"unknown outcome class {kind!r}")
    if not code or not isinstance(code, str):
        raise ValueError(f"outcome code must be a non-empty string, got {code!r}")
    d = dict(fields)
    d["outcome"] = kind
    d["code"] = code
    return d


# `code` is POSITIONAL-ONLY (the `/`). This is load-bearing: a boundary function
# routinely re-wraps an ALREADY-TAGGED inner result by spreading it —
#     return broke("transfer.provider_failed", **provider_result)
# where `provider_result` is itself `{... "code": "transfer.scp_upload_failed",
# "outcome": "broke"}`. With a normal `code` parameter, the spread `code` key
# and the positional `code` argument collide → `TypeError: got multiple values
# for argument 'code'` at CALL-BIND time (a landmine on untested paths). A
# positional-only `code` lets the same name reappear in `**fields` WITHOUT
# binding to it (PEP 570), so the spread is safe: the OUTER (boundary) code
# wins, `_tag` overwrites `outcome`, and business fields are preserved. The
# inner code is intentionally dropped at runtime — the model still sees it
# statically (the extractor reads the inner terminal from source); if a caller
# wants it in the response, it must pass `inner_code=...` explicitly.
#
# NOTE this does NOT cover an explicit business kwarg colliding with a same-named
# key in a spread — `broke("x", success=False, **d)` where `d` also has
# `success` still raises. Prefer the dict-literal merge there: `**{**d,
# "success": False}` (the literal de-dups before unpacking).
def proven(code, /, **fields):   return _tag(PROVEN,   code, fields)
def refused(code, /, **fields):  return _tag(REFUSED,  code, fields)
def broke(code, /, **fields):    return _tag(BROKE,    code, fields)
def vanished(code, /, **fields): return _tag(VANISHED, code, fields)
def degraded(code, /, **fields): return _tag(DEGRADED, code, fields)
def loop(code, /, **fields):     return _tag(LOOP,     code, fields)
