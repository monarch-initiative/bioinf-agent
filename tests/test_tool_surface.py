"""The tool surface is DATA, and this is what keeps it honest.

WHAT WENT WRONG. `agent/skills/tool_surface.py` opens with the measurement: 67 registered
tools, 34 in CLAUDE.md's routing table, 9 named elsewhere in it, and 24 named NOWHERE.
The design premise — "the agent picks the right primitive; the primitive enforces its
category's invariants internally" — silently assumed the agent could see the menu.

The cost was real. `run_install_command` mutates a conda prefix and appends a HAND-TYPED
install_step; every install primitive instead writes a typed
`installed_packages[].install_method`, which is precisely the field
`freeze.non_conda_installs` reads to decide whether it may ADOPT a prebuilt BioContainer.
An install routed through the untyped tool is invisible to that decision, so freeze
adopted a container that did not contain the tool — and every clause of the honesty
contract passed, because each was true of the container it adopted.

So these tests fail the build when:

  * a registered tool has NO position (someone added a tool and never placed it on the
    menu — the 24-tool gap, forming again);
  * the registry positions a tool that is no longer registered (the roster advertising a
    ghost — the failure `test_invariant_registry` catches for invariants);
  * a PRIMITIVE is missing from CLAUDE.md's routing table, or a table row names a tool
    that does not exist — the table and the registry are the SAME claim in two places, and
    two places is how this codebase's defects start;
  * a LOW_LEVEL tool appears in the routing table (it would be advertising itself as a
    first-class pick while also carrying a note telling you not to use it);
  * a `prefer` target is not itself a registered PRIMITIVE (a guardrail that routes to
    another low-level tool routes in a circle, and one that routes to a deleted tool
    routes into a wall);
  * a `prefer` is set with no `records_more` ("prefer X" with no reason is exactly the
    hand-wavy prose this registry replaced);
  * a LOW_LEVEL tool's SERVED description does not carry its generated guardrail.

That last one is the load-bearing test. The guardrails are composed onto descriptions at
import time by reaching into FastMCP's component store, which has moved between versions.
If that reach ever returns nothing, it returns nothing SILENTLY — the server still starts,
every tool still works, and 11 routing notes quietly stop being delivered to the model.
This test reads the description back through the PUBLIC `list_tools()`, which is the same
payload the model receives, so a silent no-op is a red build.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from agent.skills import tool_surface as ts

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = ROOT / "CLAUDE.md"


# --------------------------------------------------------------------------------------
# What the server actually serves. Read once — importing the app is not free.
# --------------------------------------------------------------------------------------

def _served() -> dict[str, str]:
    """`{tool_name: served_description}` exactly as the model receives it."""
    from agent.mcp_server import mcp
    return {t.name: (t.description or "") for t in asyncio.run(mcp.list_tools())}


_SERVED_CACHE: dict[str, str] | None = None


def served() -> dict[str, str]:
    global _SERVED_CACHE
    if _SERVED_CACHE is None:
        _SERVED_CACHE = _served()
    return _SERVED_CACHE


def _routing_table_tools() -> set[str]:
    """The tool names in CLAUDE.md's routing table.

    Bounded by the section heading and the "Below the primitives" sentence that closes it,
    so a `resolve_tool` mentioned in the prose below the table is not miscounted as a row.
    """
    text = CLAUDE_MD.read_text()
    start = text.index("## Primitives — the only tools the agent needs")
    end = text.index("Below the primitives there are still lower-level tools", start)
    names: set[str] = set()
    for line in text[start:end].splitlines():
        if not line.startswith("| `"):
            continue
        cell = line.split("|")[1]
        # digits included — `add_core_pod5_data` is a real tool name, and a `[a-z_]+`
        # class silently failed to see it while reporting the row as missing.
        names.update(re.findall(r"`([a-z0-9_]+)`", cell))
    return names


# --------------------------------------------------------------------------------------
# The registry covers the surface, in both directions.
# --------------------------------------------------------------------------------------

def test_every_registered_tool_has_a_position():
    unplaced = sorted(set(served()) - set(ts.REGISTRY))
    assert not unplaced, (
        f"{len(unplaced)} registered MCP tool(s) have no position in tool_surface.REGISTRY: "
        f"{unplaced}. A tool the agent can call but cannot find on the menu is the "
        "24-tool gap re-forming. Add it as PRIMITIVE (and a CLAUDE.md routing-table row), "
        "PROTOCOL (named in CLAUDE.md where it is used), or LOW_LEVEL (with the fields "
        "that compose its guardrail)."
    )


def test_the_registry_positions_no_tool_that_is_not_registered():
    ghosts = sorted(set(ts.REGISTRY) - set(served()))
    assert not ghosts, (
        f"tool_surface.REGISTRY positions {ghosts}, which the server does not register. "
        "A roster that advertises a tool nobody can call sends the agent looking for it."
    )


@pytest.mark.parametrize("name", sorted(ts.REGISTRY))
def test_every_position_is_a_known_value(name):
    assert ts.REGISTRY[name].position in ts.POSITIONS, (
        f"{name} has position {ts.REGISTRY[name].position!r}; expected one of {ts.POSITIONS}"
    )


# --------------------------------------------------------------------------------------
# PRIMITIVE and the CLAUDE.md routing table are ONE claim. Keep them one.
# --------------------------------------------------------------------------------------

def test_every_primitive_has_a_routing_table_row():
    missing = sorted(set(ts.positioned(ts.PRIMITIVE)) - _routing_table_tools())
    assert not missing, (
        f"{missing} are positioned PRIMITIVE but named in no row of CLAUDE.md's routing "
        "table. CLAUDE.md is the agent's prompt: a primitive that is not in the table is "
        "one the agent will not pick."
    )


def test_every_routing_table_row_names_a_registered_primitive():
    table = _routing_table_tools()
    unregistered = sorted(n for n in table if n not in served())
    assert not unregistered, (
        f"CLAUDE.md's routing table names {unregistered}, which the server does not "
        "register. The agent will try to call it and get nothing."
    )
    misplaced = sorted(
        n for n in table
        if n in ts.REGISTRY and ts.REGISTRY[n].position != ts.PRIMITIVE
    )
    assert not misplaced, (
        f"CLAUDE.md's routing table names {misplaced}, which tool_surface positions as "
        "something other than PRIMITIVE. A LOW_LEVEL tool in the table advertises itself "
        "as a first-class pick while also carrying a note saying not to use it."
    )


def test_every_protocol_tool_is_actually_named_in_claude_md():
    """PROTOCOL means 'documented where it is used'. If it is not there, it is LOW_LEVEL."""
    text = CLAUDE_MD.read_text()
    absent = [n for n in ts.positioned(ts.PROTOCOL)
              if not re.search(rf"\b{re.escape(n)}\b", text)]
    assert not absent, (
        f"{absent} are positioned PROTOCOL — meaning CLAUDE.md documents them where they "
        "are used — but CLAUDE.md does not mention them at all. Either document them or "
        "reposition them as LOW_LEVEL so they carry a guardrail instead."
    )


# --------------------------------------------------------------------------------------
# The guardrails: well-formed, and actually delivered.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(ts.positioned(ts.LOW_LEVEL)))
def test_a_low_level_tool_is_not_also_in_the_routing_table(name):
    assert name not in _routing_table_tools(), (
        f"{name} is LOW_LEVEL and also a routing-table row."
    )


@pytest.mark.parametrize("name", sorted(ts.positioned(ts.LOW_LEVEL)))
def test_prefer_targets_route_upward_to_something_documented(name):
    """A guardrail must send the agent somewhere it can actually FIND.

    PRIMITIVE (a routing-table row) and PROTOCOL (named in CLAUDE.md where it is used) are
    both findable, and both are legitimate targets — `discard_pipeline_draft` correctly
    routes to `start_pipeline`, which is protocol step 1. What is never legitimate is
    routing to another LOW_LEVEL tool: that sends the agent from one thing it was told not
    to reach for to another, in a circle.
    """
    tp = ts.REGISTRY[name]
    for target in tp.prefer:
        assert target in served(), (
            f"{name}'s guardrail says to prefer {target!r}, which is not a registered "
            "tool. The guardrail routes into a wall."
        )
        assert ts.REGISTRY[target].position != ts.LOW_LEVEL, (
            f"{name}'s guardrail says to prefer {target!r}, which is itself LOW_LEVEL. "
            "That routes in a circle — from one tool the agent was told not to reach for "
            "to another."
        )


@pytest.mark.parametrize("name", sorted(ts.positioned(ts.LOW_LEVEL)))
def test_a_prefer_always_states_what_the_primitive_records_that_this_does_not(name):
    tp = ts.REGISTRY[name]
    if tp.prefer:
        assert tp.records_more.strip(), (
            f"{name} names a preferred primitive but no `records_more`. 'Prefer X' with no "
            "reason is the hand-wavy prose this registry replaced — the reason IS the "
            "guardrail. Say which field or check the primitive feeds that this one does not."
        )


@pytest.mark.parametrize("name", sorted(ts.positioned(ts.LOW_LEVEL)))
def test_a_low_level_tool_says_when_using_it_directly_is_correct(name):
    assert ts.REGISTRY[name].direct_use.strip(), (
        f"{name} has no `direct_use`. A guardrail that only forbids teaches the agent to "
        "route around it; naming the legitimate case is what makes the rest credible."
    )


@pytest.mark.parametrize("name", sorted(ts.positioned(ts.LOW_LEVEL)))
def test_the_generated_guardrail_reaches_the_served_description(name):
    """THE LOAD-BEARING TEST — see this module's docstring.

    The guardrails are composed onto descriptions at import time by reaching into
    FastMCP's private component store. If that reach stops working, it stops working
    SILENTLY. This reads the description back through the public API the model is served
    from, so silence becomes a red build.
    """
    desc = served().get(name, "")
    assert ts.MARKER in desc, (
        f"{name}'s SERVED description does not carry its routing note. The guardrail was "
        "generated but never delivered — check `tool_surface.apply_to` still finds "
        "FastMCP's tool store, and that it is still called at the bottom of "
        "agent/mcp_tools/__init__.py AFTER every submodule import."
    )
    assert ts.guardrail(ts.REGISTRY[name]) in desc, (
        f"{name}'s served routing note is not the one the registry generates today. A "
        "stale note was injected, or something rewrote the description afterwards."
    )


def test_no_primitive_or_protocol_tool_carries_a_guardrail():
    """A routing note on a first-class primitive would tell the agent not to use the
    thing the table just told it to use."""
    strays = [n for n, tp in ts.REGISTRY.items()
              if tp.position != ts.LOW_LEVEL and ts.MARKER in served().get(n, "")]
    assert not strays, f"{strays} are not LOW_LEVEL but their served description carries a routing note."


def test_guardrail_returns_nothing_for_a_non_low_level_position():
    for pos in (ts.PRIMITIVE, ts.PROTOCOL):
        assert ts.guardrail(ts.ToolPosition(tool="x", position=pos)) == ""


def test_a_query_only_tool_says_so_rather_than_going_silent():
    """`mutates=""` is a STATEMENT (query-only), not an omission — and the guardrail has
    to render it as one. A note that simply omits the sentence would read as 'nobody
    said', which is the absence-rounded-up-into-a-verdict shape this codebase refuses
    everywhere else."""
    q = ts.ToolPosition(tool="x", position=ts.LOW_LEVEL, direct_use="you are just looking")
    assert "QUERY-ONLY" in ts.guardrail(q)


def test_apply_to_is_idempotent():
    """The MCP server hot-reloads submodules on code change (BIOINF_MCP_AUTO_RELOAD=1), so
    apply_to runs repeatedly against the same live app. Appending a second copy of every
    guardrail each reload would grow the payload without bound."""
    from agent.mcp_server import mcp
    before = served()["run_install_command"]
    ts.apply_to(mcp)
    after = {t.name: (t.description or "") for t in asyncio.run(mcp.list_tools())}
    assert after["run_install_command"] == before
    assert after["run_install_command"].count(ts.MARKER) == 1


def test_a_reload_does_not_lose_the_routing_notes():
    """THE FULL-SUITE FAILURE, pinned.

    `importlib.reload(mcp_server)` cascades into every tool submodule so their
    `@mcp.tool()` decorators re-fire on the new app — which re-registers each tool from
    its DOCSTRING, producing fresh objects with fresh descriptions. `from agent import
    mcp_tools` does not re-run that package's __init__ when it is already in sys.modules,
    so the guardrails were composed exactly once, at first import, and vanished on every
    reload after it.

    This is not a test-ordering artifact: BIOINF_MCP_AUTO_RELOAD=1 is the default in this
    repo, so the running server reloads on code change and would have shipped the same
    silent loss. It showed up as two of eleven tools missing their note under the full
    suite while the file passed alone — the exact signature of a once-only side effect.
    """
    import importlib
    from agent import mcp_server as ms
    importlib.reload(ms)
    after = {t.name: (t.description or "") for t in asyncio.run(ms.mcp.list_tools())}
    missing = [n for n in ts.positioned(ts.LOW_LEVEL) if ts.MARKER not in after.get(n, "")]
    assert not missing, (
        f"{missing} lost their routing note across a reload — compose them after the "
        "cascade in mcp_server, not only on first import of mcp_tools."
    )
    global _SERVED_CACHE
    _SERVED_CACHE = None       # the app object changed; don't serve a stale read to later tests
