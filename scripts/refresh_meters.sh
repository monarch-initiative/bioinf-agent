#!/usr/bin/env bash
# refresh_meters.sh — regenerate the decision-surface meters, in order:
#
#   docs/outcomes_ledger.json      (tracked RECORD — fast AST sweep, CI-ratcheted)
#   docs/terminal_coverage.json    (tracked RECORD — full suite under coverage, ~4 min)
#   docs/outcomes_dashboard.html   (untracked VIEW — rendered by both steps above)
#
# The two tracked files are pure functions of the tree, so a merge conflict in
# either is never resolved by line-merging: take EITHER side, run this on the
# merged tree, commit what it writes.
#
#   ./scripts/refresh_meters.sh          # everything (slow — runs the suite)
#   ./scripts/refresh_meters.sh --fast   # ledger + dashboard only (no re-measure)
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_env.sh"

"$BIOINF_RUNTIME_PY" "$BIOINF_ROOT/scripts/extract_outcomes.py"
if [ "${1:-}" != "--fast" ]; then
    "$BIOINF_RUNTIME_PY" "$BIOINF_ROOT/scripts/measure_terminal_coverage.py"
fi
