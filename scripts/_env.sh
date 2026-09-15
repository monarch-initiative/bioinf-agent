#!/usr/bin/env bash
# _env.sh — the ONE answer to "where is conda?" and "which python runs the agent?"
#
# Sourced by setup.sh, start_mcp_server.sh and setup_core_test_data.sh; invoked as a
# CLI by scripts/doctor.py (which is stdlib-only by design and must not grow a second
# copy of the search).
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
# Single implementation on purpose: four callers resolving conda independently can
# resolve it differently, and then the systems check reports on a conda setup did not
# use. Pinned by tests/test_setup_surface_resolution.py.

BIOINF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIOINF_RUNTIME="$BIOINF_ROOT/.conda_runtime"
BIOINF_RUNTIME_PY="$BIOINF_RUNTIME/bin/python"
BIOINF_PRIVATE_CONDA="$BIOINF_ROOT/.miniforge/condabin/conda"

# The repo-local private copy wins over $CONDA_EXE and PATH: a clone that installed
# its own miniforge built its runtime env and every envs/bioinf_* with it, and the
# launcher puts it on PATH for the server and its children anyway.
bioinf_find_conda() {
    if [ -x "$BIOINF_PRIVATE_CONDA" ]; then
        echo "$BIOINF_PRIVATE_CONDA"; return 0
    fi
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

# Launcher fallback for machines provisioned before the runtime env existed: a
# base-conda interpreter that can actually import the server's deps — importability
# is probed, not assumed from the path existing.
bioinf_legacy_server_python() {
    for base in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" \
                "/opt/conda" "/opt/homebrew/opt/miniforge3"; do
        if [ -x "$base/bin/python" ] && \
           "$base/bin/python" -c "import fastmcp" >/dev/null 2>&1; then
            echo "$base/bin/python"; return 0
        fi
    done
    if command -v python3 >/dev/null 2>&1 && \
       python3 -c "import fastmcp" >/dev/null 2>&1; then
        command -v python3; return 0
    fi
    return 1
}

# CLI mode — only when executed, never when sourced.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    case "${1:-}" in
        conda)          bioinf_find_conda ;;
        runtime-python) echo "$BIOINF_RUNTIME_PY"; [ -x "$BIOINF_RUNTIME_PY" ] ;;
        *) echo "usage: ./scripts/_env.sh {conda|runtime-python}" >&2; exit 2 ;;
    esac
fi
