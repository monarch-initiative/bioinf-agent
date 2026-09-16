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
#   1. Ask where the WORKSPACE goes — the directory every generated artifact
#      lives in (envs, images, reports, scratch, resources). Never inside the
#      checkout: these outlive any clone. Recorded in ./.bioinf_workspace.
#   2. Create the repo-local RUNTIME env at ./.conda_runtime (conda, Python 3.11).
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

# Install conda as a private copy inside the repo, never as a system change: no
# `conda init`, no rc-file edits, nothing outside $PROJECT_ROOT. Still an install on
# the user's machine, so it requires consent (prompt, or --yes).
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

bioinf_find_conda >/dev/null || install_private_conda
# Also puts it on PATH, which is how child scripts and EnvManager find it.
CONDA="$(bioinf_conda_on_path)"
say "conda: $CONDA"

# --- 1. workspace ------------------------------------------------------------
# Asked, never picked silently. The agent's product is auditable artifacts, and a
# system that chooses their location on the user's behalf is how a config menu
# came to write one file while the systems check read another.
#
# The offered default prefers ~/Desktop when one exists, because reports/ is a
# directory people are meant to OPEN. That preference lives HERE, in front of a
# human who confirms it — never in the resolver, which must answer the same way
# on a machine that grows a Desktop folder next month.
POINTER="$PROJECT_ROOT/.bioinf_workspace"
if [ -n "${BIOINF_WORKSPACE:-}" ]; then
    say "workspace: $BIOINF_WORKSPACE (from \$BIOINF_WORKSPACE)"
elif [ -f "$POINTER" ]; then
    say "workspace: $(grep -v '^#' "$POINTER" | grep -v '^$' | head -1) (from .bioinf_workspace)"
else
    if [ -d "$HOME/Desktop" ]; then
        WS_DEFAULT="$HOME/Desktop/bioinf_agent"
    else
        WS_DEFAULT="$HOME/bioinf_agent"
    fi
    echo
    echo "  Where should bioinf-agent keep what it BUILDS?"
    echo "    environments/  conda envs + container images   (rebuildable)"
    echo "    reports/       ENV + RUN reports, sealed specs (the record — never deleted)"
    echo "    resources/     reference genomes + test data   (expensive to refetch)"
    echo "    scratch/       job state, drafts, staging      (delete freely)"
    echo
    echo "  Not inside this checkout: these outlive it."
    if [ "$ASSUME_YES" = "1" ]; then
        WS="$WS_DEFAULT"
        say "workspace: $WS (default, accepted by --yes)"
    else
        printf "  workspace [%s]: " "$WS_DEFAULT"
        # `read` works on a pipe as well as a terminal, so a piped answer is an
        # answer. Only END OF INPUT — nobody there to ask — falls through, and it
        # REFUSES rather than picking: silently choosing where a user's records
        # live is the failure this prompt exists to prevent, and a non-interactive
        # caller has two ways to say what it wants.
        if read -r WS; then
            [ -n "$WS" ] || WS="$WS_DEFAULT"     # bare Enter accepts the shown default
        else
            echo >&2
            echo "[setup] no answer on stdin, and the workspace is not something to" >&2
            echo "[setup] guess at — it is where your envs, reports and sealed specs" >&2
            echo "[setup] will live. For a scripted install, either:" >&2
            echo "[setup]   BIOINF_WORKSPACE=/path/to/workspace ./scripts/setup.sh" >&2
            echo "[setup]   ./scripts/setup.sh --yes        # accept $WS_DEFAULT" >&2
            exit 2
        fi
    fi
    WS="${WS/#\~/$HOME}"
    case "$WS" in
        "$HOME"|"$HOME"/*) ;;
        *) echo "[setup] $WS is outside \$HOME. Docker bind-mounts resolve against the" >&2
           echo "[setup] Docker VM's shared prefixes, so an env frozen there would validate" >&2
           echo "[setup] against an empty directory. Choose a workspace under \$HOME." >&2
           exit 2 ;;
    esac
    case "$WS" in
        "$PROJECT_ROOT"|"$PROJECT_ROOT"/*)
           echo "[setup] $WS is inside the checkout. Artifacts must outlive it." >&2
           exit 2 ;;
    esac
    mkdir -p "$WS"
    printf '%s\n' \
        "# The bioinf-agent workspace: where every generated artifact lives." \
        "# Written by scripts/setup.sh. Override with \$BIOINF_WORKSPACE." \
        "$WS" > "$POINTER"
    say "workspace: $WS (recorded in .bioinf_workspace)"
fi

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
"$RUNTIME_PY" -m pip install -q -e "$PROJECT_ROOT[dev]"
say "installed: $("$RUNTIME_PY" -c 'import fastmcp; print("fastmcp", fastmcp.__version__)')"

# --- 4. core toolkit + test data --------------------------------------------
say "bootstrapping core toolkit + test data (${DATA_FLAG:-full corpus})..."
"$PROJECT_ROOT/scripts/setup_core_test_data.sh" $DATA_FLAG

# --- 5. systems check --------------------------------------------------------
say "running the systems check..."
"$RUNTIME_PY" "$PROJECT_ROOT/scripts/doctor.py" || {
    say "setup finished, but the systems check has FAIL rows above — each names its fix."
    exit 1
}
say "setup complete. Next: run 'claude' from the repo root and drive the bioinf tools."
