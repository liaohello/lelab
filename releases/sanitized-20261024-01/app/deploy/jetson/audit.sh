#!/usr/bin/env bash
# Read-only Jetson host audit. This script does not open serial/camera devices or control a robot.
set -u

proxy_url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy-url)
      [[ $# -ge 2 ]] || { echo "ERROR: --proxy-url requires a value" >&2; exit 64; }
      proxy_url=$2
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--proxy-url http://192.0.2.23:7890]"
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

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

if [[ -r /etc/nv_tegra_release ]]; then
  ok "Jetson L4T marker found: $(head -n 1 /etc/nv_tegra_release)"
elif have dpkg-query && dpkg-query -W nvidia-l4t-core >/dev/null 2>&1; then
  ok "Jetson L4T package found: $(dpkg-query -W -f='${Version}' nvidia-l4t-core 2>/dev/null)"
else
  fail "Jetson/L4T was not detected; run this audit on the Jetson host, not inside a generic container"
fi

if have dpkg-query && dpkg-query -W nvidia-jetpack >/dev/null 2>&1; then
  ok "JetPack package: $(dpkg-query -W -f='${Version}' nvidia-jetpack 2>/dev/null)"
else
  warn "nvidia-jetpack meta-package not found; record the L4T release and confirm JetPack manually"
fi

section "runtime"
if have python3; then
  python_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || printf unknown)
  printf 'python3=%s\n' "$python_version"
  if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    ok "native Python satisfies LeRobot >=3.12"
  else
    warn "native Python is older than 3.12; use an audited container rather than replacing JetPack Python"
  fi
else
  warn "python3 not found"
fi

if have docker; then
  ok "Docker available: $(docker --version 2>/dev/null)"
  if docker info 2>/dev/null | grep -qiE 'nvidia|runtimes:.*nvidia'; then
    ok "NVIDIA container runtime appears in docker info"
  else
    warn "NVIDIA runtime not visible to the current user; verify nvidia-container-toolkit and Docker permissions"
  fi
else
  fail "Docker not found; use the JetPack-supported NVIDIA container runtime installation path"
fi

if have nvcc; then
  ok "CUDA compiler available: $(nvcc --version 2>/dev/null | tail -n 1)"
elif [[ -d /usr/local/cuda ]]; then
  ok "CUDA directory exists at /usr/local/cuda"
else
  warn "CUDA toolkit not found on the host (container runtime may still provide it)"
fi

printf '%s\n' "CANDIDATE_IMAGE=nvcr.io/nvidia/pytorch:25.04-py3-igpu"
warn "candidate image is NOT approved by this script; compare the detected JetPack/L4T version with NVIDIA's compatibility matrix before pulling or running it"

section "storage"
project_parent=${LELAB_PROJECT_ROOT:-/home/nvidia/Desktop/lelab}
probe_path=$project_parent
while [[ ! -e "$probe_path" && "$probe_path" != "/" ]]; do
  probe_path=$(dirname "$probe_path")
done
if df -Pk "$probe_path" >/dev/null 2>&1; then
  df -h "$probe_path" | tail -n 1
  free_kib=$(df -Pk "$probe_path" | awk 'NR==2 {print $4}')
  if [[ "$free_kib" =~ ^[0-9]+$ ]] && (( free_kib < 20 * 1024 * 1024 )); then
    fail "less than 20 GiB free for code, datasets, and recordings"
  elif [[ "$free_kib" =~ ^[0-9]+$ ]] && (( free_kib < 50 * 1024 * 1024 )); then
    warn "less than 50 GiB free; plan dataset retention before recording"
  else
    ok "free space is above the audit threshold"
  fi
else
  fail "cannot inspect filesystem capacity for $probe_path"
fi

section "robot and camera presence (read-only)"
shopt -s nullglob
serial_devices=(/dev/serial/by-id/* /dev/ttyACM* /dev/ttyUSB*)
video_devices=(/dev/video*)
shopt -u nullglob
if (( ${#serial_devices[@]} > 0 )); then
  printf 'serial_candidates=%s\n' "${serial_devices[*]}"
  ok "serial candidates found; select a stable /dev/serial/by-id path before configuration"
else
  warn "no serial candidate found; check USB cable, power, and dialout membership"
fi
if (( ${#video_devices[@]} > 0 )); then
  printf 'video_candidates=%s\n' "${video_devices[*]}"
  ok "video nodes found; this audit did not open them"
else
  warn "no /dev/video* camera nodes found"
fi
groups_text=$(id -nG 2>/dev/null || true)
[[ " $groups_text " == *" dialout "* ]] && ok "user belongs to dialout" || warn "user is not in dialout"
[[ " $groups_text " == *" video "* ]] && ok "user belongs to video" || warn "user is not in video"

section "network tools"
for tool in ssh rsync curl sha256sum; do
  have "$tool" && ok "$tool available" || warn "$tool not found"
done
if [[ -n "$proxy_url" ]]; then
  if [[ "$proxy_url" != "http://192.0.2.23:7890" && "$proxy_url" != "https://192.0.2.23:7890" ]]; then
    warn "proxy differs from the planned LAN endpoint: $proxy_url"
  fi
  if have curl && curl --silent --show-error --head --max-time 5 --proxy "$proxy_url" https://pypi.org/ >/dev/null; then
    ok "optional proxy is reachable from Jetson"
  else
    warn "optional proxy probe failed; direct download may still work"
  fi
else
  printf '%s\n' "proxy=not-tested (pass --proxy-url to test the optional Jetson-only proxy)"
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
