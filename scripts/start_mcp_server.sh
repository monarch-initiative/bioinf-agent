#!/usr/bin/env bash
# Start the bioinf MCP server using whatever conda/Python is available.
#
# This wrapper is the dev-mode launcher invoked from .mcp.json. It enables
# the file-watch auto-reload so edits to agent/ or config/ trigger a server
# restart on the next MCP call. Production deployments that want stable code
# should call `python -m agent` directly without this var set.
#
# NOTE: it's `python -m agent`, NOT `python -m agent.mcp_server` — running
# mcp_server.py as __main__ creates two FastMCP instances and the wrong one
# gets .run()'d (zero tools visible to stdio clients). See agent/__main__.py.
#
# Override by exporting BIOINF_MCP_AUTO_RELOAD=0 before launch to opt out.
set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export BIOINF_MCP_AUTO_RELOAD="${BIOINF_MCP_AUTO_RELOAD:-1}"

for base in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" \
            "/opt/conda" "/opt/homebrew/opt/miniforge3"; do
    if [ -x "$base/bin/python" ]; then
        exec "$base/bin/python" -m agent
    fi
done

exec python3 -m agent
