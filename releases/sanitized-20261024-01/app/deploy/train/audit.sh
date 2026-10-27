#!/usr/bin/env bash
# Read-only training/inference host audit.
set -u

policy_port=${LEROBOT_POLICY_PORT:-28081}
warnings=0
failures=0

ok() { printf 'OK    %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; warnings=$((warnings + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; failures=$((failures + 1)); }
section() { printf '\n[%s]\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

section "identity"
printf 'host=%s\n' "$(hostname 2>/dev/null || printf unknown)"
printf 'kernel=%s\n' "$(uname -srmo 2>/dev/null || printf unknown)"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  printf 'os=%s\n' "${PRETTY_NAME:-unknown}"
fi

section "gpu and runtime"
if have nvidia-smi; then
  if nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader; then
    ok "NVIDIA GPU is visible"
  else
    fail "nvidia-smi exists but cannot query the GPU"
  fi
else
  fail "nvidia-smi not found"
fi

if have python3; then
  python_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || printf unknown)
  printf 'python3=%s\n' "$python_version"
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null \
    && ok "Python satisfies >=3.12" \
    || warn "Python is older than 3.12; use a compatible venv/container"
else
  fail "python3 not found"
fi
have uv && ok "uv available: $(uv --version 2>/dev/null)" || warn "uv not found"
have docker && ok "Docker available: $(docker --version 2>/dev/null)" || warn "Docker not found (native uv deployment remains possible)"

section "storage"
project_parent=${LELAB_PROJECT_ROOT:-/home/trainer/Desktop/lelab_server}
probe_path=$project_parent
while [[ ! -e "$probe_path" && "$probe_path" != "/" ]]; do
  probe_path=$(dirname "$probe_path")
done
if df -Pk "$probe_path" >/dev/null 2>&1; then
  df -h "$probe_path" | tail -n 1
  free_kib=$(df -Pk "$probe_path" | awk 'NR==2 {print $4}')
  if [[ "$free_kib" =~ ^[0-9]+$ ]] && (( free_kib < 50 * 1024 * 1024 )); then
    fail "less than 50 GiB free for datasets/checkpoints"
  elif [[ "$free_kib" =~ ^[0-9]+$ ]] && (( free_kib < 100 * 1024 * 1024 )); then
    warn "less than 100 GiB free; training retention needs review"
  else
    ok "free space is above the audit threshold"
  fi
else
  fail "cannot inspect filesystem capacity for $probe_path"
fi

section "network exposure"
if have ss; then
  listen_lines=$(ss -ltn 2>/dev/null | awk -v p=":${policy_port}" '$4 ~ p"$" {print}')
  if [[ -z "$listen_lines" ]]; then
    ok "policy port $policy_port is not currently listening"
  else
    printf '%s\n' "$listen_lines"
    if grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\[::\]):' <<<"$listen_lines"; then
      fail "policy port $policy_port is publicly bound; bind to 127.0.0.1 and use SSH forwarding"
    else
      ok "policy port $policy_port is loopback/restricted"
    fi
  fi
else
  warn "ss is unavailable; cannot verify policy port binding"
fi
for tool in ssh rsync curl sha256sum; do
  have "$tool" && ok "$tool available" || warn "$tool not found"
done

if [[ "${HTTP_PROXY:-}" == *"192.0.2.23:7890"* || "${HTTPS_PROXY:-}" == *"192.0.2.23:7890"* ]]; then
  fail "private LAN proxy 192.0.2.23:7890 is configured on the public train host"
else
  ok "private Jetson/Windows proxy is not configured"
fi

section "result"
printf 'warnings=%d failures=%d\n' "$warnings" "$failures"
if (( failures > 0 )); then
  echo "AUDIT_RESULT=FAIL"
  exit 2
fi
if (( warnings > 0 )); then
  echo "AUDIT_RESULT=WARN"
  exit 0
fi
echo "AUDIT_RESULT=PASS"
