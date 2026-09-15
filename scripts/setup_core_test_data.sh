#!/usr/bin/env bash
# setup_core_test_data.sh — Thin wrapper for scripts/bootstrap_core.py.
#
# The real work is in Python so it can dogfood our skill modules (PipelineState,
# EnvManager, PackageSearch, spec_writer, OutputValidator) — the same code the
# MCP-driven agent uses for user pipelines.
#
# Usage:
#   ./scripts/setup_core_test_data.sh [--genome-build hg38] [--skip-smoke]

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# A private miniforge (installed by scripts/setup.sh on machines with no conda)
# is reached via PATH, same as in start_mcp_server.sh.
if [ -d "$PROJECT_ROOT/.miniforge/condabin" ]; then
  export PATH="$PROJECT_ROOT/.miniforge/condabin:$PATH"
fi

if ! command -v conda >/dev/null 2>&1; then
  echo "ERROR: conda not found in PATH. Run ./scripts/setup.sh (it can install a private copy), or activate your conda base environment first." >&2
  exit 1
fi

# Prefer the repo-local runtime env (created by scripts/setup.sh), then the
# active conda env's Python (3.10+), over system python3 — which on macOS is
# often 3.9 and predates PEP 604 (str | None syntax).
if [[ -x "$PROJECT_ROOT/.conda_runtime/bin/python" ]]; then
  PY="$PROJECT_ROOT/.conda_runtime/bin/python"
elif [[ -n "${CONDA_PREFIX:-}" && -x "$CONDA_PREFIX/bin/python" ]]; then
  PY="$CONDA_PREFIX/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  PY="python3"
fi

exec "$PY" "$PROJECT_ROOT/scripts/bootstrap_core.py" "$@"
