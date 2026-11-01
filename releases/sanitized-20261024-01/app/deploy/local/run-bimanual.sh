#!/usr/bin/env bash
# Local, loopback-only bimanual development entry point.
# Uses an isolated calibration/data/diagnostics root and never touches `current`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"          # .../app
RELEASE="$(cd "$HERE/.." && pwd)"                                    # releases/<snapshot>
LEROBOT_SRC="$RELEASE/lerobot/src"
LELAB_HOME="${LELAB_BIMANUAL_HOME:-$HOME/.local/share/lelab-bimanual}"
ROOT="$(cd "$RELEASE/../.." && pwd)"                                  # worktree root
if [[ -n "${LELAB_PYTHON:-}" ]]; then
  PYTHON="$LELAB_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi
PORT="${LELAB_PORT:-18080}"

export HF_LEROBOT_HOME="$LELAB_HOME/hf"
export HF_LEROBOT_CALIBRATION="$HF_LEROBOT_HOME/calibration"
export LELAB_SESSION_DIAGNOSTICS_DIR="$LELAB_HOME/diagnostics"
export PYTHONPATH="$HERE:$LEROBOT_SRC${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$HF_LEROBOT_HOME" "$HF_LEROBOT_CALIBRATION" "$LELAB_SESSION_DIAGNOSTICS_DIR"

echo "LeLab app:        $HERE"
echo "Local data root:  $LELAB_HOME"
echo "Bind:             127.0.0.1:$PORT (loopback only)"
exec "$PYTHON" -m uvicorn lelab.server:app --host 127.0.0.1 --port "$PORT"
