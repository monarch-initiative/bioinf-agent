#!/usr/bin/env bash
# config.sh — open the configuration menu for projects_access.yaml.
#
# Thin wrapper for scripts/configure.py, which needs pyyaml and the agent's own
# validator and so must run on the runtime env ./scripts/setup.sh creates.
#
# Usage:
#   ./scripts/config.sh            # the interactive menu
#   ./scripts/config.sh --web      # the same menu, rendered in the browser
#   ./scripts/config.sh --show     # print the current configuration and exit
#   ./scripts/config.sh --validate # validate and exit (rc = number of errors)

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"

if [ ! -x "$BIOINF_RUNTIME_PY" ]; then
  echo "ERROR: no runtime environment at $BIOINF_RUNTIME" >&2
  echo "fix: run ./scripts/setup.sh — it creates the runtime env this menu runs on." >&2
  exit 1
fi

exec "$BIOINF_RUNTIME_PY" "$BIOINF_ROOT/scripts/configure.py" "$@"
