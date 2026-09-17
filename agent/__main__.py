"""Entry point: `python -m agent` → start the bioinf MCP server.

Why this exists (and why we don't `python -m agent.mcp_server` directly):

When Python runs a file as `__main__`, that file lives in `sys.modules`
under the name `__main__`, NOT under its package-qualified name. The
moment anything inside the file (or its imports) does
`from agent.mcp_server import mcp`, Python sees `agent.mcp_server` is
absent from `sys.modules` and loads the file a SECOND time as
`agent.mcp_server`. The `mcp_tools/` submodules do exactly that —
`from agent.mcp_server import mcp` is how they bind the FastMCP app
they decorate against — so running mcp_server.py as `__main__` produces
two FastMCP instances. All 61 tool decorators land on the
`agent.mcp_server` copy; `__main__.mcp.run()` runs on the empty copy
and stdio clients see zero tools.

Routing the entry point through this file means mcp_server.py is only
ever loaded once, under its canonical name, so there's exactly one
FastMCP instance and the registered tools are the ones served.

Tests aren't affected — they `import agent.mcp_server` directly, which
goes through this same canonical path.
"""
from __future__ import annotations

import os

# Before fastmcp loads: its CLI banner otherwise carries a "🎉 Update available —
# pip install --upgrade fastmcp" call to action. The pin (pyproject: fastmcp<4)
# is deliberate, so the nag is never actionable here — and typed at a bare shell
# it upgrades BASE python's copy, changing nothing about this server. setdefault,
# so a user who wants the check back can export their own value.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

from agent.mcp_server import (  # noqa: E402
    _reap_orphan_service_pids,
    _watch_and_exit_on_change,
    mcp,
)


def main() -> None:
    """Canonical server startup. Exposed as a function so it can back the
    `bioinf-mcp` console_scripts entry point (pyproject.toml) as well as
    `python -m agent`. The module-level call below is guarded by
    `__name__ == "__main__"` so importing `agent.__main__` (which is what the
    console-script wrapper does) does NOT start the server on import and then
    a second time when it calls main()."""
    _reap_orphan_service_pids()
    if os.environ.get("BIOINF_MCP_AUTO_RELOAD") == "1":
        _watch_and_exit_on_change()
    mcp.run()


if __name__ == "__main__":
    main()
