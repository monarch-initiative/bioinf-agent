#!/usr/bin/env bash
# Start the bioinf MCP server using whatever conda/Python is available.
#
# This wrapper is the dev-mode launcher invoked from .mcp.json. It enables
# the file-watch auto-reload so edits to agent/ or config/ trigger a server
# restart on the next MCP call. Production deployments that want stable code
# should call `python -m agent.mcp_server` directly without this var set.
#
# Override by exporting BIOINF_MCP_AUTO_RELOAD=0 before launch to opt out.
set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export BIOINF_MCP_AUTO_RELOAD="${BIOINF_MCP_AUTO_RELOAD:-1}"

for base in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" \
            "/opt/conda" "/opt/homebrew/opt/miniforge3"; do
    if [ -x "$base/bin/python" ]; then
        exec "$base/bin/python" -m agent.mcp_server
    fi
done

exec python3 -m agent.mcp_server
