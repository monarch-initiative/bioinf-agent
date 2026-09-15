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
#   ./scripts/_env.sh runtime-python   # print the runtime interpreter's PATH always;
#                                      # exit status says whether it is usable, so a
#                                      # caller can name the missing thing it wanted
#
# WHY IT EXISTS. These two questions were answered in four places with four
# hand-maintained path lists, and two of them had already DIVERGED on order:
# setup.sh searched the repo-local private conda LAST, doctor.py searched it FIRST.
# On a machine carrying both a private ./.miniforge and an off-PATH system conda they
# resolve differently — so the systems check could PASS on a conda that setup never
# used, which is a report describing something other than what happened. That is the
# one defect class this repo does not tolerate, so the fix is structural: one
# implementation, four callers. It is tests/test_one_reading_per_field.py's rule
# applied to the shell layer, and tests/test_setup_surface_resolution.py keeps it.

BIOINF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIOINF_RUNTIME="$BIOINF_ROOT/.conda_runtime"
BIOINF_RUNTIME_PY="$BIOINF_RUNTIME/bin/python"
BIOINF_PRIVATE_CONDA="$BIOINF_ROOT/.miniforge/condabin/conda"

# The repo-local private copy is searched FIRST, ahead of $CONDA_EXE and PATH.
# A clone that installed its own miniforge is committed to it: its runtime env and
# every envs/bioinf_* were built with that conda, and start_mcp_server.sh already
# prepends .miniforge/condabin to PATH so it wins for the server and every child
# process. Resolving it first makes the search agree with what actually runs.
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

# EnvManager and every conda-run shell find conda through PATH (shutil.which), so
# children of whatever we launch must inherit the one we resolved.
bioinf_conda_on_path() {
    local conda
    conda="$(bioinf_find_conda)" || return 1
    case ":$PATH:" in
        *":$(dirname "$conda"):"*) ;;
        *) export PATH="$(dirname "$conda"):$PATH" ;;
    esac
    echo "$conda"
}

# The interpreter for scripts that must run BEFORE (or without) the runtime env —
# bootstrap_core.py, which needs 3.10+ and must not land on the macOS system 3.9.
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

# The legacy launcher fallback: a base-conda interpreter that can actually import the
# server's deps. Kept for machines provisioned before the runtime env existed; it
# probes importability rather than taking the first python it finds (cold-start CS2).
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
