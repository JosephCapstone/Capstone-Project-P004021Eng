#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISAAC_SIM_ROOT="${ISAAC_SIM_ROOT:-$SCRIPT_DIR/../isaac-sim-standalone-6.1.0-linux-x86_64}"
KIT_EXECUTABLE="$ISAAC_SIM_ROOT/kit/kit"

if [[ ! -x "$KIT_EXECUTABLE" ]]; then
    echo "Isaac Sim 6.1 Kit executable not found: $KIT_EXECUTABLE" >&2
    echo "Set ISAAC_SIM_ROOT to the Isaac Sim 6.1 installation folder." >&2
    exit 1
fi

exec "$KIT_EXECUTABLE" "$SCRIPT_DIR/delta.robot.app.kit" \
    --ext-folder "$ISAAC_SIM_ROOT/apps" \
    --ext-folder "$ISAAC_SIM_ROOT/exts" \
    --ext-folder "$ISAAC_SIM_ROOT/extscache" \
    --ext-folder "$ISAAC_SIM_ROOT/extsUser" \
    --ext-folder "$ISAAC_SIM_ROOT/extsDeprecated" \
    --ext-folder "$SCRIPT_DIR/exts" "$@"
