#!/usr/bin/env bash
# Start the bioinf MCP server on the interpreter that actually has its deps.
#
# This wrapper is the dev-mode launcher invoked from .mcp.json. It enables
# the file-watch auto-reload so edits to agent/ or config/ trigger a server
# restart on the next MCP call. Production deployments that want stable code
# should call `python -m agent` directly without this var set.
#
# Interpreter resolution, in order (the path lists themselves live in _env.sh):
#   1. $BIOINF_RUNTIME_PY — the repo-local runtime env that scripts/setup.sh
#      creates and installs the agent into. This is the designed path: the env
#      the deps were installed into IS the env the server runs on, per clone,
#      no discovery.
#   2. bioinf_legacy_server_python — the usual base-conda pythons, but only one
#      that can actually import the server's deps. (The old behavior exec'd the
#      first base python found, installed-into or not; a fresh machine then died
#      with ModuleNotFoundError inside the MCP client. Cold-start finding CS2.)
#   3. Otherwise: fail LOUDLY, naming the fix.
#
# NOTE: it's `python -m agent`, NOT `python -m agent.mcp_server` — running
# mcp_server.py as __main__ creates two FastMCP instances and the wrong one
# gets .run()'d (zero tools visible to stdio clients). See agent/__main__.py.
#
# Override by exporting BIOINF_MCP_AUTO_RELOAD=0 before launch to opt out.
set -e
# Interpreter and conda resolution live in scripts/_env.sh — one implementation for
# this launcher, setup.sh, the bootstrap wrapper and doctor.py.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"
cd "$BIOINF_ROOT"

export BIOINF_MCP_AUTO_RELOAD="${BIOINF_MCP_AUTO_RELOAD:-1}"

# EnvManager and every conda-run shell resolve conda through PATH, and children of
# the server inherit it — so put the conda this clone actually uses there.
bioinf_conda_on_path >/dev/null || true

if [ -x "$BIOINF_RUNTIME_PY" ]; then
    if "$BIOINF_RUNTIME_PY" -c "import fastmcp" >/dev/null 2>&1; then
        exec "$BIOINF_RUNTIME_PY" -m agent
    fi
    echo "[start_mcp_server] .conda_runtime exists but cannot import the server's deps." >&2
    echo "[start_mcp_server] fix: re-run ./scripts/setup.sh" >&2
    exit 1
fi

if LEGACY_PY="$(bioinf_legacy_server_python)"; then
    exec "$LEGACY_PY" -m agent
fi

echo "[start_mcp_server] no interpreter with the server's deps was found." >&2
echo "[start_mcp_server] fix: run ./scripts/setup.sh — it creates ./.conda_runtime and installs the agent into it." >&2
exit 1
