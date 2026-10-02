#!/usr/bin/env bash
# Linux launcher
set -euo pipefail

# Project and Isaac Sim paths
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ISAAC_SIM_ROOT="$(dirname -- "$PROJECT_DIR")/isaac-sim-standalone-6.1.0-linux-x86_64"
ISAAC_SIM_ROOT="${ISAAC_SIM_ROOT:-$DEFAULT_ISAAC_SIM_ROOT}"
KIT_EXECUTABLE="$ISAAC_SIM_ROOT/kit/kit"

# Isaac Sim installation check
if [[ ! -x "$KIT_EXECUTABLE" ]]; then
    printf 'Isaac Sim 6.1.0 Kit executable not found: %s\n' "$KIT_EXECUTABLE" >&2
    printf 'Set ISAAC_SIM_ROOT to your Isaac Sim 6.1.0 folder and try again.\n' >&2
    exit 1
fi

# DELTA launch command
exec "$KIT_EXECUTABLE" "$PROJECT_DIR/delta.robot.app.kit" \
    --ext-folder "$ISAAC_SIM_ROOT/apps" \
    --ext-folder "$ISAAC_SIM_ROOT/exts" \
    --ext-folder "$ISAAC_SIM_ROOT/extscache" \
    --ext-folder "$ISAAC_SIM_ROOT/extsUser" \
    --ext-folder "$ISAAC_SIM_ROOT/extsDeprecated" \
    --ext-folder "$PROJECT_DIR/exts" \
    "$@"
