#!/usr/bin/env bash
# Container healthcheck: GPU stack + PolicyServer port, no hardware access.
set -euo pipefail
port=${LEROBOT_POLICY_PORT:-8080}
python3 -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" || exit 1
python3 - "$port" <<'PY' || exit 1
import socket, sys
port = int(sys.argv[1])
with socket.socket() as sock:
    sock.settimeout(2)
    if sock.connect_ex(("127.0.0.1", port)) != 0:
        raise SystemExit(f"PolicyServer 未监听 127.0.0.1:{port}")
PY
