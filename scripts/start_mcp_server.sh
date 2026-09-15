#!/usr/bin/env bash
# Start the bioinf MCP server on the interpreter that actually has its deps.
#
# This wrapper is the dev-mode launcher invoked from .mcp.json. It enables
# the file-watch auto-reload so edits to agent/ or config/ trigger a server
# restart on the next MCP call. Production deployments that want stable code
# should call `python -m agent` directly without this var set.
#
# Interpreter resolution, in order:
#   1. ./.conda_runtime/bin/python — the repo-local runtime env that
#      scripts/setup.sh creates and installs the agent into. This is the
#      designed path: the env the deps were installed into IS the env the
#      server runs on, per clone, no discovery.
#   2. Legacy fallback: the usual base-conda pythons — but only one that can
#      actually import the server's deps. (The old behavior exec'd the first
#      base python found, installed-into or not; a fresh machine then died
#      with ModuleNotFoundError inside the MCP client. Cold-start finding CS2.)
#   3. Otherwise: fail LOUDLY, naming the fix.
#
# NOTE: it's `python -m agent`, NOT `python -m agent.mcp_server` — running
# mcp_server.py as __main__ creates two FastMCP instances and the wrong one
# gets .run()'d (zero tools visible to stdio clients). See agent/__main__.py.
#
# Override by exporting BIOINF_MCP_AUTO_RELOAD=0 before launch to opt out.
set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export BIOINF_MCP_AUTO_RELOAD="${BIOINF_MCP_AUTO_RELOAD:-1}"

RUNTIME_PY="$PWD/.conda_runtime/bin/python"
if [ -x "$RUNTIME_PY" ]; then
    if "$RUNTIME_PY" -c "import fastmcp" >/dev/null 2>&1; then
        exec "$RUNTIME_PY" -m agent
    fi
    echo "[start_mcp_server] .conda_runtime exists but cannot import the server's deps." >&2
    echo "[start_mcp_server] fix: re-run ./scripts/setup.sh" >&2
    exit 1
fi

for base in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" \
            "/opt/conda" "/opt/homebrew/opt/miniforge3"; do
    if [ -x "$base/bin/python" ] && \
       "$base/bin/python" -c "import fastmcp" >/dev/null 2>&1; then
        exec "$base/bin/python" -m agent
    fi
done

if command -v python3 >/dev/null 2>&1 && \
   python3 -c "import fastmcp" >/dev/null 2>&1; then
    exec python3 -m agent
fi

echo "[start_mcp_server] no interpreter with the server's deps was found." >&2
echo "[start_mcp_server] fix: run ./scripts/setup.sh — it creates ./.conda_runtime and installs the agent into it." >&2
exit 1
