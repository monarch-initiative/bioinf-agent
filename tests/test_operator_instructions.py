"""The operating protocol ships with the server, not with the developer's checkout.

A client that attaches the server anywhere receives `agent/operator_instructions.md` as the
server's MCP instructions beside the tool list; with the tool schemas deferred behind a
search, those instructions are the one thing the model always sees. They name the protocol
in order, the route order, how to read an outcome and how long work is waited on, and
every tool they name exists. CLAUDE.md points at the file rather than carrying a copy.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = ROOT / "agent" / "operator_instructions.md"

#: Backticked identifiers in the instructions that are fields or values, not tools.
_NOT_TOOLS = {"pipeline_id", "outcome", "code", "result", "state", "job_id", "usage", "url", "local_path",
              "tools", "evidence", "patches", "github_repo", "background", "wait_s", "exited", "degraded",
              "proven", "refused", "broke", "vanished", "loop", "env_name", "tool", "command", "name",
              "description", "file", "find", "replace", "reason", "where", "grep", "ls", "test",
              "evidence_timeout_s"}

#: The protocol, in the order the instructions must give it.
_PROTOCOL_ORDER = ("agent_status", "interpret_request", "resolve_tool", "start_pipeline", "freeze",
                   "select_test_data", "run_pipeline_step", "patch_pipeline", "run_step_in_container",
                   "seal_workflow")
#: The route order: one tool per resolver tier, in the resolver's order.
_ROUTE_ORDER = ("freeze_from_image", "install_conda_packages", "build_env_from_authors_recipe", "synth_fetch")
_ROUTE_TIERS = ("author_image", "conda", "authors_recipe", "synthesis")


def _text() -> str:
    return INSTRUCTIONS.read_text(encoding="utf-8")


def _served() -> set[str]:
    from agent.mcp_server import mcp
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_the_server_hands_every_client_the_instructions():
    from agent.mcp_server import mcp, operator_instructions
    text = operator_instructions()
    assert text == _text() and text.strip()
    assert mcp.instructions == text, "FastMCP was not given the instructions"
    assert len(text) <= 9000, f"{len(text)} chars: the instructions are read on every turn; keep them short"


def test_the_instructions_give_the_protocol_and_the_route_order_in_order():
    text = _text()
    def at(name: str) -> int:
        m = re.search(rf"`{name}[`(]", text)
        assert m, f"{name} is not named in the instructions"
        return m.start()
    positions = [at(n) for n in _PROTOCOL_ORDER]
    assert positions == sorted(positions), dict(zip(_PROTOCOL_ORDER, positions))
    routes = [at(n) for n in _ROUTE_ORDER]
    assert routes == sorted(routes), dict(zip(_ROUTE_ORDER, routes))
    for phrase in ("authors' own machinery first", "evidence must RUN the tool", "never patch by hand",
                   "Never end your turn to wait", "Do not repeat the same call", "Report only what the records show",
                   "`check_job(job_id, wait_s=300)`", "`patches=[{file, find, replace, reason}]`"):
        assert phrase in text, phrase


def test_the_route_order_is_the_resolvers():
    """The instructions rank the routes the way `resolve_tool` does; a page that ranks
    them otherwise sends the agent to a Dockerfile build for a tool the ecosystem has
    already packaged and containerized."""
    from agent.skills.resolver import TIER_ORDER
    ranks = [TIER_ORDER.index(t) for t in _ROUTE_TIERS]
    assert ranks == sorted(ranks), dict(zip(_ROUTE_TIERS, ranks))


def test_every_tool_the_instructions_name_is_served():
    served = _served()
    named = {m for m in re.findall(r"`([a-z][a-z0-9_]+)(?:\(|`|=)", _text())}
    ghosts = sorted(n for n in named - _NOT_TOOLS if n not in served)
    assert not ghosts, f"the instructions name tools the server does not register: {ghosts}"
    assert len(named & served) >= 40, "the instructions should route the bulk of the surface"


def test_the_background_tools_the_instructions_list_are_the_ones_that_take_the_flag():
    import inspect
    from agent.mcp_server import mcp
    text = _text()
    para = text[text.index("## Long work"):text.index("## Compute environments")]
    listed = set(re.findall(r"`([a-z_]+)`", para.split("take `background=True`")[0]))
    tools = asyncio.run(mcp.list_tools())
    with_flag = {t.name for t in tools
                 if "background" in ((getattr(t, "parameters", None) or {}).get("properties") or {})}
    assert listed == with_flag - {"run_in_background", "download_reference_database"}, (listed, with_flag)
    assert "run_in_background" in para


def test_claude_md_points_at_the_instructions_and_carries_no_copy():
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "agent/operator_instructions.md" in claude_md
    assert "\n## Protocol\n" not in claude_md and "## Async pattern" not in claude_md
