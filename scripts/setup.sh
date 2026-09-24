#!/usr/bin/env bash
# setup.sh — the one-command setup for bioinf-agent.
#
#   ./scripts/setup.sh            # the system builds itself (one mode, no options)
#   ./scripts/setup.sh --check    # systems check only (scripts/doctor.py) — changes nothing
#
# UNATTENDED — no questions, no prompts, same result on every machine. Everything
# it installs lands inside the repo dir, untracked: the clone stays small (git
# tracks only code + the download instructions), and deleting the clone undoes
# all of it. What the agent later PRODUCES (reports, containers) goes outside the
# repo — see agent/skills/workspace.py.
#
# The five steps:
#   1. Install a repo-private miniforge at ./.miniforge — unconditionally, never
#      a machine conda. No shell integration, nothing outside the repo dir.
#   2. Create the repo-local RUNTIME env at ./.conda_runtime (Python 3.11) —
#      the interpreter that runs the MCP server.
#   3. `pip install -e ".[dev,hpc]"` into that env (editable; [hpc] carries the
#      Globus CLI).
#   4. Bootstrap the core toolkit env (./envs/bioinf_core_tools) + the test-data
#      corpus (./resources/: chr22 reference + the read datasets + phenopacket).
#      Multi-GB of downloads — this is most of setup's wall clock.
#   5. Run the systems check (doctor.py). Every FAIL names its fix.
#
# Idempotent: re-running skips what already exists and re-verifies the rest.
# It never deletes anything.
set -euo pipefail

# Conda + interpreter resolution: scripts/_env.sh, shared with the launcher, the
# bootstrap wrapper and the doctor.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"

PROJECT_ROOT="$BIOINF_ROOT"
RUNTIME="$BIOINF_RUNTIME"
RUNTIME_PY="$BIOINF_RUNTIME_PY"
PRIVATE_CONDA="$BIOINF_PRIVATE_CONDA"
PYTHON_VERSION="3.11"

say() { echo "[setup] $*"; }

MODE="setup"
for arg in "$@"; do
    case "$arg" in
        --check) MODE="check" ;;
        # --yes was non-interactive consent back when setup asked questions; it
        # asks none now. Accepted so older scripts and docs don't break.
        --yes|-y) ;;
        *) echo "usage: ./scripts/setup.sh [--check]" >&2; exit 2 ;;
    esac
done

sha256_of() {
    shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1 || sha256sum "$1" | cut -d' ' -f1
}

# Install conda as a private copy inside the repo, never as a system change: no
# `conda init`, no rc-file edits, nothing outside $PROJECT_ROOT — which is why it
# needs no consent question: deleting .miniforge/ (or the clone) undoes all of it.
# Installed UNCONDITIONALLY when absent, whatever condas the machine already has:
# a machine's own conda carries that machine's variance, and every clone of this
# system is meant to bootstrap identically.
install_private_conda() {
    say "installing the repo-private miniforge at ./.miniforge"
    say "(no shell integration, nothing outside this directory; delete .miniforge/ to remove it)"
    local url installer
    url="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
    # The installer refuses to run unless $0 ends in ".sh", and mktemp templates
    # can't carry a suffix portably — hence a temp dir holding a correctly named file.
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

# --- 1. private conda ----------------------------------------------------------
bioinf_find_conda >/dev/null || install_private_conda
# Also puts it on PATH, which is how child scripts and EnvManager find it.
CONDA="$(bioinf_conda_on_path)"
say "conda: $CONDA"

# --- 2. runtime env ----------------------------------------------------------
if [ -x "$RUNTIME_PY" ]; then
    say "runtime env exists at .conda_runtime ($("$RUNTIME_PY" -V 2>&1)) — keeping it"
else
    say "creating runtime env at .conda_runtime (Python $PYTHON_VERSION)..."
    "$CONDA" create -y --prefix "$RUNTIME" "python=$PYTHON_VERSION" >/dev/null
    say "runtime env created ($("$RUNTIME_PY" -V 2>&1))"
fi

# --- 3. editable install -----------------------------------------------------
say "installing bioinf-agent (editable) into the runtime env..."
# [hpc] carries globus-cli so the Globus wire works out of the box; the only
# step left to the user is `globus login` (interactive OAuth, not ours to run).
"$RUNTIME_PY" -m pip install -q -e "$PROJECT_ROOT[dev,hpc]"
say "installed: $("$RUNTIME_PY" -c 'import fastmcp; print("fastmcp", fastmcp.__version__)')"

# --- 4. core toolkit + test data --------------------------------------------
say "bootstrapping the core toolkit env + test-data corpus (multi-GB downloads)..."
"$PROJECT_ROOT/scripts/setup_core_test_data.sh"

# --- 5. systems check --------------------------------------------------------
say "running the systems check..."
"$RUNTIME_PY" "$PROJECT_ROOT/scripts/doctor.py" || {
    say "setup finished, but the systems check has FAIL rows above — each names its fix."
    exit 1
}
say "setup complete. Next: run 'claude' from the repo root and drive the bioinf tools."
