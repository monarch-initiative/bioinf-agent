"""
The MCP tool surface is one list, and drift from it is a build break.

`agent/mcp_server.py` re-exports every tool that has moved out into
`agent/mcp_tools/`, so an external caller (and every test that reaches a tool
through the `from agent import mcp_server as m` singleton idiom) keeps one
import path. That block is hand-maintained, and its own comment says so:
"This list grows alongside the mcp_tools package; populated per phase as tools
move out."

It did not grow. Measured 2026-07-31: 67 `@mcp.tool()` functions exist and 4
were unreachable —

    from agent.mcp_server import freeze_from_image             # ImportError
    from agent.mcp_server import build_env_from_authors_recipe # ImportError
    from agent.mcp_server import globus_task_status            # ImportError
    from agent.mcp_server import acquire_reference_via_recipe  # ImportError

— including `freeze_from_image`, the executor for the top-ranked routing tier
(adopt the authors' own image), and the one whose contract enforcement is the
most intricate in the codebase. Its absence made it the hardest tool to reach
from a test, which is not a coincidence worth preserving.

The list stays EXPLICIT rather than becoming a `getattr` loop: it is greppable,
it survives static analysis, and a reader can see what the module offers. What
changes is that forgetting an entry now fails the build instead of failing
silently at some future caller's import.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "agent" / "mcp_tools"


def _declared_tools() -> dict[str, str]:
    """Every `@mcp.tool()`-decorated function in agent/mcp_tools/, name → module.

    From the AST, so a tool is counted because the SOURCE declares it, not
    because an import happened to succeed."""
    found: dict[str, str] = {}
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any("mcp.tool" in ast.unparse(d) for d in node.decorator_list):
                found[node.name] = path.name
    return found


def test_every_mcp_tool_is_reachable_from_the_server_module():
    declared = _declared_tools()
    assert len(declared) > 50, \
        f"only found {len(declared)} @mcp.tool() functions — the AST walk has drifted"

    server = importlib.import_module("agent.mcp_server")
    missing = sorted((name, mod) for name, mod in declared.items()
                     if not hasattr(server, name))
    assert not missing, (
        "these tools exist but cannot be imported from agent.mcp_server:\n  "
        + "\n  ".join(f"{name}  ({mod})" for name, mod in missing)
        + "\nAdd them to the re-export block. A tool that is registered with the "
          "MCP server but unreachable through the module is reachable by agents "
          "and not by tests, which is the wrong way round.")


def test_the_re_export_block_advertises_nothing_that_is_not_a_tool():
    """The other direction. A name left behind after a tool was renamed or removed
    would keep importing (it still resolves in the source module) while pointing at
    something the MCP server no longer serves — a surface that claims more than it
    has, which is the failure mode this repo cares most about."""
    tree = ast.parse((ROOT / "agent" / "mcp_server.py").read_text())
    reexported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "agent.mcp_tools."):
            reexported |= {a.asname or a.name for a in node.names}

    declared = set(_declared_tools())
    # Private helpers are deliberately re-exported for back-compat with tests that
    # probe them via the singleton; they are not tools and are not claimed to be.
    stray = sorted(n for n in reexported if not n.startswith("_") and n not in declared)
    assert not stray, (
        f"agent.mcp_server re-exports {stray} from agent.mcp_tools, but no "
        f"@mcp.tool() by that name exists. Remove the stale name.")
