#!/usr/bin/env bash
# _env.sh — the ONE answer to "where is conda?" and "which python runs the agent?"
#
# Sourced by setup.sh, start_mcp_server.sh and setup_core_test_data.sh; invoked as a
# CLI by scripts/doctor.py (which is stdlib-only by design).
#
#   source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
#     -> $BIOINF_ROOT $BIOINF_RUNTIME $BIOINF_RUNTIME_PY $BIOINF_PRIVATE_CONDA
#     -> bioinf_find_conda · bioinf_conda_on_path · bioinf_bootstrap_python
#
#   ./scripts/_env.sh conda            # print the conda binary, or exit 1
#   ./scripts/_env.sh runtime-python   # always print the runtime interpreter's path;
#                                      # exit status says whether it is usable, so a
#                                      # caller can name a missing env by path
#
# "Where is conda" has exactly ONE answer — the repo-private ./.miniforge — and
# deliberately NO search. Searching a machine's condas gives each caller a
# different answer (and the doctor a PASS on a conda setup nothing else uses),
# and any machine conda carries that machine's variance. setup.sh installs the
# private copy unconditionally; every clone bootstraps identically.
# Pinned by tests/test_setup_surface_resolution.py.

# ${BASH_SOURCE[0]:-$0}: bash names the sourced file in BASH_SOURCE, zsh in $0, so a
# user's zsh can `source scripts/activate.sh` (which sources this) and land here too.
BIOINF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
BIOINF_RUNTIME="$BIOINF_ROOT/.conda_runtime"
BIOINF_RUNTIME_PY="$BIOINF_RUNTIME/bin/python"
BIOINF_PRIVATE_CONDA="$BIOINF_ROOT/.miniforge/condabin/conda"

# conda's "a newer version of conda exists" banner tells the user to update BASE
# conda — the one thing this repo's setup story keeps them away from, and never
# relevant here: every env is created by a pinned conda against pinned specs.
# Suppressed for every caller that sources this file (setup, the server and its
# child conda runs). A user who wants the notice back can export "true".
export CONDA_NOTIFY_OUTDATED_CONDA="${CONDA_NOTIFY_OUTDATED_CONDA:-false}"

# THE conda is the repo-private miniforge, full stop. There is no search: a
# machine's own conda can be arbitrarily broken (half-updated base, exotic
# channels, shell hooks), and an agent system meant to run the same on every
# machine cannot inherit that variance. setup.sh installs ./.miniforge
# unconditionally when it is absent; until then this returns 1 and every
# caller's failure message names setup.sh as the fix.
bioinf_find_conda() {
    if [ -x "$BIOINF_PRIVATE_CONDA" ]; then
        echo "$BIOINF_PRIVATE_CONDA"; return 0
    fi
    return 1
}

# Resolve conda and put it on PATH: EnvManager and every conda-run shell find it
# through PATH (shutil.which), so children must inherit the one we resolved.
bioinf_conda_on_path() {
    local conda
    conda="$(bioinf_find_conda)" || return 1
    case ":$PATH:" in
        *":$(dirname "$conda"):"*) ;;
        *) export PATH="$(dirname "$conda"):$PATH" ;;
    esac
    echo "$conda"
}

# The interpreter for scripts that may run before the runtime env exists —
# bootstrap_core.py, which needs 3.10+ (the macOS system python3 is often 3.9).
bioinf_bootstrap_python() {
    if [ -x "$BIOINF_RUNTIME_PY" ]; then
        echo "$BIOINF_RUNTIME_PY"; return 0
    fi
    if [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
        echo "$CONDA_PREFIX/bin/python"; return 0
    fi
    if command -v python >/dev/null 2>&1; then echo "python"; return 0; fi
    echo "python3"
}

# CLI mode — only when executed, never when sourced.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    case "${1:-}" in
        conda)          bioinf_find_conda ;;
        runtime-python) echo "$BIOINF_RUNTIME_PY"; [ -x "$BIOINF_RUNTIME_PY" ] ;;
        *) echo "usage: ./scripts/_env.sh {conda|runtime-python}" >&2; exit 2 ;;
    esac
fi
