#!/usr/bin/env bash
# Start the bioinf MCP server using whatever conda/Python is available.
set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."

for base in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" \
            "/opt/conda" "/opt/homebrew/opt/miniforge3"; do
    if [ -x "$base/bin/python" ]; then
        exec "$base/bin/python" -m agent.mcp_server
    fi
done

exec python3 -m agent.mcp_server
