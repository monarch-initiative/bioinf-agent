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

# Conda + interpreter resolution live in scripts/_env.sh — one implementation for
# this wrapper, setup.sh, the launcher and doctor.py.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"

# bootstrap_core.py drives conda directly and EnvManager resolves it via PATH.
if ! bioinf_conda_on_path >/dev/null; then
  echo "ERROR: conda not found. Run ./scripts/setup.sh (it can install a private copy), or activate your conda base environment first." >&2
  exit 1
fi

exec "$(bioinf_bootstrap_python)" "$BIOINF_ROOT/scripts/bootstrap_core.py" "$@"
