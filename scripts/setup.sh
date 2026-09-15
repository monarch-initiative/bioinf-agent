#!/usr/bin/env bash
# setup.sh — the one-command setup for bioinf-agent.
#
#   ./scripts/setup.sh            # runtime env + editable install + minimal data + systems check
#   ./scripts/setup.sh --full     # same, but pull the full read-dataset corpus (multi-GB)
#   ./scripts/setup.sh --check    # systems check only (scripts/doctor.py) — changes nothing
#
# What "setup" means here, concretely:
#   1. Create the repo-local RUNTIME env at ./.conda_runtime (conda, Python 3.11).
#      This is the Python that runs the MCP server. It is per-clone and gitignored;
#      start_mcp_server.sh and setup_core_test_data.sh resolve it by path, so there
#      is no guessing about which interpreter carries the deps.
#   2. `pip install -e ".[dev]"` into that env (editable — this repo is a
#      workspace-rooted service, not a site-packages library).
#   3. Bootstrap the core toolkit env + test data (setup_core_test_data.sh;
#      --minimal by default: core_tools env + chr22 reference, no multi-GB pulls).
#   4. Run the systems check (doctor.py). Every FAIL names its fix.
#
# Idempotent: re-running skips the env create when the env exists and re-verifies
# the rest. It never deletes anything.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$PROJECT_ROOT/.conda_runtime"
RUNTIME_PY="$RUNTIME/bin/python"
PYTHON_VERSION="3.11"

say() { echo "[setup] $*"; }

# --- locate conda (the one hard prerequisite besides Docker) -----------------
find_conda() {
    if [ -n "${CONDA_EXE:-}" ] && [ -x "$CONDA_EXE" ]; then
        echo "$CONDA_EXE"; return 0
    fi
    if command -v conda >/dev/null 2>&1; then
        command -v conda; return 0
    fi
    for c in "$HOME/miniforge3/condabin/conda" "$HOME/miniconda3/condabin/conda" \
             "$HOME/anaconda3/condabin/conda" "/opt/conda/condabin/conda" \
             "/opt/homebrew/opt/miniforge3/condabin/conda"; do
        if [ -x "$c" ]; then echo "$c"; return 0; fi
    done
    return 1
}

# --- mode: --check -----------------------------------------------------------
if [ "${1:-}" = "--check" ]; then
    if [ -x "$RUNTIME_PY" ]; then
        exec "$RUNTIME_PY" "$PROJECT_ROOT/scripts/doctor.py"
    fi
    # No runtime env yet — run the doctor under any python3; it will report
    # the missing runtime env as its own FAIL row rather than crashing here.
    exec python3 "$PROJECT_ROOT/scripts/doctor.py"
fi

DATA_FLAG="--minimal"
if [ "${1:-}" = "--full" ]; then
    DATA_FLAG=""
elif [ -n "${1:-}" ]; then
    echo "usage: ./scripts/setup.sh [--full|--check]" >&2
    exit 2
fi

CONDA="$(find_conda)" || {
    echo "ERROR: conda not found (checked \$CONDA_EXE, PATH, and the usual install locations)." >&2
    echo "  fix: install miniforge — https://github.com/conda-forge/miniforge — then re-run ./scripts/setup.sh" >&2
    exit 1
}
say "conda: $CONDA"

# --- 1. runtime env ----------------------------------------------------------
if [ -x "$RUNTIME_PY" ]; then
    say "runtime env exists at .conda_runtime ($("$RUNTIME_PY" -V 2>&1)) — keeping it"
else
    say "creating runtime env at .conda_runtime (Python $PYTHON_VERSION)..."
    "$CONDA" create -y --prefix "$RUNTIME" "python=$PYTHON_VERSION" >/dev/null
    say "runtime env created ($("$RUNTIME_PY" -V 2>&1))"
fi

# --- 2. editable install -----------------------------------------------------
say "installing bioinf-agent (editable) into the runtime env..."
"$RUNTIME_PY" -m pip install -q -e "$PROJECT_ROOT[dev]"
say "installed: $("$RUNTIME_PY" -c 'import fastmcp; print("fastmcp", fastmcp.__version__)')"

# --- 3. core toolkit + test data --------------------------------------------
say "bootstrapping core toolkit + test data (${DATA_FLAG:-full corpus})..."
"$PROJECT_ROOT/scripts/setup_core_test_data.sh" $DATA_FLAG

# --- 4. systems check --------------------------------------------------------
say "running the systems check..."
"$RUNTIME_PY" "$PROJECT_ROOT/scripts/doctor.py" || {
    say "setup finished, but the systems check has FAIL rows above — each names its fix."
    exit 1
}
say "setup complete. Next: run 'claude' from the repo root and drive the bioinf tools."
