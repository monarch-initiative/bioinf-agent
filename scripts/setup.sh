#!/usr/bin/env bash
# setup.sh — the one-command setup for bioinf-agent.
#
#   ./scripts/setup.sh            # runtime env + editable install + minimal data + systems check
#   ./scripts/setup.sh --full     # same, but pull the full read-dataset corpus (multi-GB)
#   ./scripts/setup.sh --check    # systems check only (scripts/doctor.py) — changes nothing
#   ./scripts/setup.sh --yes      # non-interactive consent (e.g. allow the private conda install)
#
# What "setup" means here, concretely:
#   0. Find conda — or, if the machine has none, install a PRIVATE miniforge at
#      ./.miniforge (with consent: interactive prompt, or --yes). No shell
#      integration, nothing outside the repo dir; delete .miniforge/ to remove.
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

# Conda + interpreter resolution lives in ONE place (scripts/_env.sh) so setup, the
# doctor, the launcher and the bootstrap cannot answer the same question differently.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"

PROJECT_ROOT="$BIOINF_ROOT"
RUNTIME="$BIOINF_RUNTIME"
RUNTIME_PY="$BIOINF_RUNTIME_PY"
PRIVATE_CONDA="$BIOINF_PRIVATE_CONDA"
PYTHON_VERSION="3.11"

say() { echo "[setup] $*"; }

MODE="setup"
DATA_FLAG="--minimal"
ASSUME_YES="${BIOINF_AUTO_CONDA:-0}"
for arg in "$@"; do
    case "$arg" in
        --check) MODE="check" ;;
        --full)  DATA_FLAG="" ;;
        --yes|-y) ASSUME_YES=1 ;;
        *) echo "usage: ./scripts/setup.sh [--full|--check|--yes]" >&2; exit 2 ;;
    esac
done

sha256_of() {
    shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1 || sha256sum "$1" | cut -d' ' -f1
}

# The machine has no conda at all. conda is how EVERY environment here gets
# built (including the runtime env this script is about to create), so setup
# can absorb it — as a private copy inside the repo, never as a system change:
# no `conda init`, no rc-file edits, nothing outside $PROJECT_ROOT. It is an
# install on the user's machine, though, so it never happens without consent.
install_private_conda() {
    say "conda not found on this machine."
    say "bioinf-agent can install a PRIVATE miniforge at ./.miniforge — no shell"
    say "integration, nothing outside this directory; delete .miniforge/ to remove it."
    if [ "$ASSUME_YES" != "1" ]; then
        if [ -t 0 ]; then
            read -r -p "[setup] install the private miniforge now? [Y/n] " reply
            case "${reply:-Y}" in
                [Yy]*|"") ;;
                *) say "declined — install miniforge yourself (https://github.com/conda-forge/miniforge) and re-run"; exit 1 ;;
            esac
        else
            echo "ERROR: conda not found and this is not a terminal, so setup cannot ask consent" >&2
            echo "  fix: re-run with --yes (allows the private ./.miniforge install), or install" >&2
            echo "       miniforge yourself: https://github.com/conda-forge/miniforge" >&2
            exit 1
        fi
    fi
    local url installer
    url="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
    # The installer's own header refuses unless $0 ends in ".sh" (its sourced-
    # invocation guard), and mktemp templates can't carry a suffix portably —
    # so make a temp DIR and give the file its required name inside it.
    installer="$(mktemp -d "${TMPDIR:-/tmp}/miniforge_installer.XXXXXX")/miniforge.sh"
    say "downloading $url"
    curl -fsSL "$url" -o "$installer"
    say "installer sha256 (observed): $(sha256_of "$installer")"
    bash "$installer" -b -p "$PROJECT_ROOT/.miniforge" >/dev/null
    rm -f "$installer"
    say "private miniforge installed: $("$PRIVATE_CONDA" --version 2>&1)"
}

# --- mode: --check -----------------------------------------------------------
if [ "$MODE" = "check" ]; then
    if [ -x "$RUNTIME_PY" ]; then
        exec "$RUNTIME_PY" "$PROJECT_ROOT/scripts/doctor.py"
    fi
    # No runtime env yet — run the doctor under any python3; it will report
    # the missing runtime env as its own FAIL row rather than crashing here.
    exec python3 "$PROJECT_ROOT/scripts/doctor.py"
fi

bioinf_find_conda >/dev/null || install_private_conda
# bioinf_conda_on_path resolves it and makes it reachable to every child — child
# scripts and the agent's own EnvManager find conda via PATH.
CONDA="$(bioinf_conda_on_path)"
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
