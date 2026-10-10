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
(`outcome`, `code`) are added. A refused/broke/vanished terminal also always carries
a top-level `error`: when the builder set none, `derive_error` fills it from the
fields it did set (reason, the first violation, the last informative stderr line):

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

import re

PROVEN   = "proven"
REFUSED  = "refused"
BROKE    = "broke"
VANISHED = "vanished"
DEGRADED = "degraded"
LOOP     = "loop"

OUTCOME_CLASSES = (PROVEN, REFUSED, BROKE, VANISHED, DEGRADED, LOOP)

# The helper NAMES, exported so the extractor knows what call sites to harvest.
HELPER_NAMES = ("proven", "refused", "broke", "vanished", "degraded", "loop")


#: Classes whose terminal must say what went wrong. `degraded` did the work and
#: `loop` hands back for a retry; neither is an error.
_FAILURE_CLASSES = frozenset({REFUSED, BROKE, VANISHED})

ERROR_LINE_LIMIT = 300

#: Lines a process wrapper prints around the real cause. They carry no information
#: about the failure, so the line before them is the one to show.
_WRAPPER_LINE_RE = re.compile(
    r"^(ERROR conda\.cli\.main_run:execute|\(?See above for error|Traceback \(most recent call last\))")


def last_informative_line(text: object, limit: int = ERROR_LINE_LIMIT) -> str:
    """The last line of a stream that names a cause.

    Skips lines without letters (a stray `]` or `}`), and the wrapper lines a runner
    prints after the real error. Falls back to the last non-empty line when nothing
    better exists, and to "" for an empty stream."""
    if not isinstance(text, str):
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not re.search(r"[A-Za-z]", ln) or _WRAPPER_LINE_RE.match(ln):
            continue
        return ln[:limit]
    return lines[-1][:limit] if lines else ""


def _first_text(*values: object) -> str:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _violations_summary(violations: object) -> str:
    if not isinstance(violations, list) or not violations:
        return ""
    first = violations[0]
    if isinstance(first, dict):
        msg = _first_text(first.get("message"), first.get("detail"), first.get("reason"),
                          first.get("statement"), first.get("error"))
        head = _first_text(first.get("invariant"), first.get("clause"), first.get("id"))
        msg = f"{head}: {msg}" if head and msg else (msg or head)
    else:
        msg = str(first)
    n = len(violations)
    return f"{n} violation{'s' if n != 1 else ''}; first: {msg}" if msg else f"{n} violations"


def derive_error(fields: dict, code: str) -> str:
    """What a failure terminal says when its builder set no `error`.

    In order: a prose field that already names the cause (`reason`, `message`,
    `detail`), the first of its `violations`, the last informative line of its
    stderr then stdout (one nested level too — a failed build carries them under
    `build`), else the code and exit status. Never empty."""
    text = _first_text(fields.get("reason"), fields.get("message"), fields.get("detail"))
    if text:
        return text[:ERROR_LINE_LIMIT]
    text = _violations_summary(fields.get("violations"))
    if text:
        return text[:ERROR_LINE_LIMIT]
    streams = [fields.get("stderr"), fields.get("stdout")]
    for key in sorted(fields):
        inner = fields[key]
        if isinstance(inner, dict) and ("stderr" in inner or "stdout" in inner):
            streams += [inner.get("stderr"), inner.get("stdout")]
    for s in streams:
        line = last_informative_line(s)
        if line:
            return line
    rc = fields.get("returncode")
    return f"{code} (exit {rc})" if rc not in (None, "") else code


def _tag(kind: str, code: str, fields: dict) -> dict:
    if kind not in OUTCOME_CLASSES:
        raise ValueError(f"unknown outcome class {kind!r}")
    if not code or not isinstance(code, str):
        raise ValueError(f"outcome code must be a non-empty string, got {code!r}")
    d = dict(fields)
    d["outcome"] = kind
    d["code"] = code
    # A failure terminal always carries a top-level `error` a reader can act on. The
    # builder's own text wins; this only fills the key when it was left empty.
    if kind in _FAILURE_CLASSES and not d.get("error"):
        d["error"] = derive_error(d, code)
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
#: Outcome classes that mean the call did the work it was asked to do.
#: `degraded` counts — it proceeded, with its reduced assurance stated on the
#: record. `loop` does not: it is a hand-back for a retry, so the work is unfinished.
_DID_THE_WORK = frozenset({PROVEN, DEGRADED})


def call_verdict(result: object) -> bool | None:
    """Did this tool call succeed? THREE answers: True, False, or None — unstated.

    The one reading of a tool return's verdict. `outcome` is the contracted
    field (stamped by the helpers above, harvested by extract_outcomes.py,
    build-gated by tests/test_outcome_tags.py); `success` is the older ad-hoc
    convention, still emitted by most tools and still honoured here.

    `None` is the case that has to exist. Most terminals in this codebase carry
    neither key — the outcome-tag lint is scoped to the seal subsystem — so a
    reader that treats `.get("success")` as the whole answer converts *no
    statement* into *failure*, which is a verdict manufactured from a field
    nobody wrote. Callers must branch on all three; `if not call_verdict(r)`
    reintroduces the fault this function exists to remove.
    """
    if not isinstance(result, dict):
        return None
    outcome = result.get("outcome")
    if outcome in OUTCOME_CLASSES:
        return outcome in _DID_THE_WORK
    success = result.get("success")
    if isinstance(success, bool):
        return success
    return None


def proven(code, /, **fields):   return _tag(PROVEN,   code, fields)
def refused(code, /, **fields):  return _tag(REFUSED,  code, fields)
def broke(code, /, **fields):    return _tag(BROKE,    code, fields)
def vanished(code, /, **fields): return _tag(VANISHED, code, fields)
def degraded(code, /, **fields): return _tag(DEGRADED, code, fields)
def loop(code, /, **fields):     return _tag(LOOP,     code, fields)
