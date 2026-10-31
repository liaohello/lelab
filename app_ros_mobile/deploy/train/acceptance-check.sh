#!/usr/bin/env bash
# Post-deploy acceptance sweep for M0-M7 (run ON the Jetson).
set -uo pipefail
api=${LELAB_API:-http://127.0.0.1:8000}
pass=0; fail=0
check() { # check <label> <expected-substring> <body>
  local label=$1 want=$2 body=$3
  if printf '%s' "$body" | grep -q "$want"; then echo "  ok   $label"; pass=$((pass+1));
  else echo "  FAIL $label -> ${body:0:120}"; fail=$((fail+1)); fi
}

echo "== M0 HF 登录 =="
check "hf-auth-status 三态字段" '"token_present"' "$(curl -s --max-time 20 "$api/hf-auth-status")"

echo "== M1 控制面 =="
status=$(curl -s --max-time 60 "$api/system/train-server/status")
check "ssh_ok" '"ssh_ok":true' "$status"
check "GPU 数量" '"gpus"' "$status"
check "28081 三态" '"state"' "$status"

echo "== M2/M3 会话与显存 =="
check "遗留会话接口" '"configured"' "$(curl -s --max-time 30 "$api/inference/remote/server-status")"

echo "== M5/M6 权重与模型库 =="
check "权重清单" '"keep_n"' "$(curl -s --max-time 60 "$api/system/train-server/checkpoints")"
check "远程模型库" '"library"' "$(curl -s --max-time 60 "$api/system/train-server/models")"
check "本机模型" '"models"' "$(curl -s --max-time 20 "$api/models/local")"
check "下载进度" '"phase"' "$(curl -s --max-time 20 "$api/models/pull-status")"

echo "== M7 孤儿作业 =="
check "孤儿列表" '"jobs"' "$(curl -s --max-time 60 "$api/jobs/orphans")"

echo "== M8 数据集封存与组合 =="
check "本地数据集列表带封存字段" '"seal_state"' "$(curl -s --max-time 30 "$api/datasets/local")"
check "组合数据集清单" '"composites"' "$(curl -s --max-time 60 "$api/datasets/composites")"
check "组合数据集回收站" '"trash"' "$(curl -s --max-time 60 "$api/datasets/composites/trash")"

echo
echo "acceptance: $pass ok / $fail failed"
[[ $fail -eq 0 ]]
