#!/usr/bin/env bash
# End-to-end smoke for multi-dataset (composite) training, driven through the
# LeLab API on the Jetson:
#
#   封存 → 同步 → 兼容性预检 → 训练机构建组合 → 单/多策略并行训练 → 封存复检
#
# Examples:
#   bash smoke-composite-training.sh --dry-run \
#     --dataset local/red_bowl_01 --dataset local/red_bowl_02
#   bash smoke-composite-training.sh --seal --weights equal \
#     --policy act --policy smolvla --gpu-list 0,1 --steps 200
#   bash smoke-composite-training.sh --dataset local/a --dataset local/b \
#     --skip-sync --timeout-min 60 --trash-composite
set -euo pipefail

api=${LELAB_API:-http://127.0.0.1:8000}
weights_mode=frames
custom_weights=
steps=200
batch_size=8
save_freq=100
timeout_min=45
do_seal=0
skip_sync=0
trash_composite=0
dry_run=0
gpu_list=
datasets=()
policies=()

usage() {
  cat <<'EOF'
Usage: smoke-composite-training.sh [options]

  --dataset REPO_ID       参与组合的数据集，可重复；≥2 个，缺省时自动挑选本地已封存数据集
  --policy NAME           策略类型，可重复（默认 act；例如 act / diffusion / smolvla）
  --gpu-list 0,1          为每个策略依次指定训练机 GPU，缺省自动选空闲卡
  --weights MODE          frames（默认）/ equal / custom
  --custom-weights 1,2    仅 custom：每个数据集的权重，自动归一化
  --steps N               训练步数（默认 200）
  --batch-size N          batch size（默认 8）
  --save-freq N           检查点间隔（默认 100）
  --timeout-min N         单个阶段超时（默认 45 分钟）
  --seal                  对未封存的成员先执行封存（异步 SHA-256）
  --skip-sync             跳过同步（成员已在训练机 .ready）
  --trash-composite       训练完成后把组合数据集移入训练机回收站
  --dry-run               只打印将要执行的请求，不调用 API（需显式 --dataset）
  -h, --help
EOF
}

die() { echo "错误：$*" >&2; exit 1; }
note() { printf '\n== %s ==\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset) [[ $# -ge 2 ]] || die "--dataset 缺参数"; datasets+=("$2"); shift 2 ;;
    --policy) [[ $# -ge 2 ]] || die "--policy 缺参数"; policies+=("$2"); shift 2 ;;
    --gpu-list) [[ $# -ge 2 ]] || die "--gpu-list 缺参数"; gpu_list=$2; shift 2 ;;
    --weights) [[ $# -ge 2 ]] || die "--weights 缺参数"; weights_mode=$2; shift 2 ;;
    --custom-weights) [[ $# -ge 2 ]] || die "--custom-weights 缺参数"; custom_weights=$2; shift 2 ;;
    --steps) [[ $# -ge 2 ]] || die "--steps 缺参数"; steps=$2; shift 2 ;;
    --batch-size) [[ $# -ge 2 ]] || die "--batch-size 缺参数"; batch_size=$2; shift 2 ;;
    --save-freq) [[ $# -ge 2 ]] || die "--save-freq 缺参数"; save_freq=$2; shift 2 ;;
    --timeout-min) [[ $# -ge 2 ]] || die "--timeout-min 缺参数"; timeout_min=$2; shift 2 ;;
    --seal) do_seal=1; shift ;;
    --skip-sync) skip_sync=1; shift ;;
    --trash-composite) trash_composite=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

case "$weights_mode" in
  frames|equal|custom) ;;
  *) die "--weights 只支持 frames / equal / custom" ;;
esac
[[ ${#policies[@]} -eq 0 ]] && policies=(act)

command -v curl >/dev/null || die "缺少 curl"
command -v python3 >/dev/null || die "缺少 python3"

# curl failures return an empty body so `require_ok` reports one clear error
# instead of aborting with curl's exit code.
api_get() { curl -sS --max-time 60 "$api$1" 2>/dev/null || true; }
api_post() {
  curl -sS --max-time 60 -X POST -H 'content-type: application/json' -d "$2" "$api$1" 2>/dev/null || true
}

# jget '<expr over d>'  — read a field from the JSON on stdin.
jget() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
print(eval(sys.argv[1], {"d": d}))
' "$1"
}

is_api_error() {
  printf '%s' "$1" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
sys.exit(0 if isinstance(d, dict) and set(d.keys()) == {"detail"} else 1)
'
}

require_ok() { # require_ok BODY LABEL
  if is_api_error "$1"; then
    detail=$(printf '%s' "$1" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("detail") or "")
except ValueError:
    print("")
' 2>/dev/null || true)
    [[ -n "$detail" ]] || detail="API 无响应或返回非 JSON：${1:0:200}"
    die "$2：$detail"
  fi
}

wait_until() { # wait_until SECONDS_LIMIT DESCRIPTION COMMAND...
  local limit=$1 description=$2; shift 2
  local deadline=$((SECONDS + limit))
  while (( SECONDS < deadline )); do
    if "$@"; then return 0; fi
    sleep 5
  done
  return 1
}

poll_seal() { # poll_seal REPO_ID
  local body state
  body=$(api_get "/datasets/local/$1/seal")
  state=$(printf '%s' "$body" | jget 'd["seal"].get("state")')
  case "$state" in
    sealed) echo "  封存完成 $1"; return 0 ;;
    failed|cancelled) die "封存 $1 失败：$(printf '%s' "$body" | jget 'd["seal"].get("error")')" ;;
  esac
  local progress
  progress=$(printf '%s' "$body" | jget 'd["seal"].get("progress")')
  echo "  封存中 $1 … ${progress:-0}"
  return 1
}

poll_sync() { # poll_sync REPO_ID
  local body state
  body=$(api_get "/datasets/local/$1/sync-status")
  state=$(printf '%s' "$body" | jget 'd["state"]["state"]')
  case "$state" in
    synced) echo "  同步完成 $1"; return 0 ;;
    failed|cancelled) die "同步 $1 失败：$(printf '%s' "$body" | jget 'd["state"].get("error")')" ;;
  esac
  local progress
  progress=$(printf '%s' "$body" | jget 'd["state"].get("progress") or 0')
  echo "  同步中 $1 … ${progress}%"
  return 1
}

poll_composite() { # poll_composite COMPOSITE_REPO_ID
  local body build_state remote_state phase progress
  body=$(api_get "/datasets/composite/status?key=$1")
  build_state=$(printf '%s' "$body" | jget 'd["build"].get("state")')
  remote_state=$(printf '%s' "$body" | jget '(d.get("remote") or {}).get("state")')
  if [[ "$build_state" == "ready" || "$remote_state" == "ready" ]]; then
    echo "  组合数据集就绪 $1"; return 0
  fi
  if [[ "$build_state" == "failed" || "$build_state" == "cancelled" ]]; then
    die "组合数据集构建失败：$(printf '%s' "$body" | jget 'd["build"].get("error")')"
  fi
  phase=$(printf '%s' "$body" | jget 'd["build"].get("phase") or "pending"')
  progress=$(printf '%s' "$body" | jget 'round((d["build"].get("progress") or 0) * 100)')
  echo "  构建中（$phase）… ${progress}%"
  return 1
}

poll_jobs() { # poll_jobs JOB_ID...
  local pending=0 body state step loss
  for job in "$@"; do
    body=$(api_get "/jobs/$job")
    state=$(printf '%s' "$body" | jget 'd["state"]')
    if [[ "$state" == "running" ]]; then
      step=$(printf '%s' "$body" | jget 'd["metrics"].get("current_step")')
      loss=$(printf '%s' "$body" | jget 'd["metrics"].get("current_loss")')
      echo "  $job 训练中 step=$step loss=$loss"
      pending=1
    fi
  done
  (( pending == 0 )) && return 0
  return 1
}

# ---------------------------------------------------------------------------
# 0) 选择数据集
# ---------------------------------------------------------------------------
if [[ ${#datasets[@]} -eq 0 ]]; then
  [[ $dry_run -eq 1 ]] && die "--dry-run 需要显式 --dataset"
  note "自动挑选本地已封存数据集"
  listing=$(api_get "/datasets/local")
  require_ok "$listing" "读取本地数据集"
  mapfile -t datasets < <(
    printf '%s' "$listing" | python3 -c '
import json, sys
rows = [row for row in json.load(sys.stdin) if row.get("sealed") and row.get("finalized", True)]
rows.sort(key=lambda row: row.get("last_recorded_at") or "", reverse=True)
for row in rows[:2]:
    print(row["repo_id"])
'
  )
fi
[[ ${#datasets[@]} -ge 2 ]] || die "至少需要 2 个数据集（可用 --dataset 指定，或先封存数据）"

echo "API:        $api"
echo "数据集:     ${datasets[*]}"
echo "权重模式:   $weights_mode${custom_weights:+（$custom_weights）}"
echo "策略:       ${policies[*]}"
echo "GPU:        ${gpu_list:-auto}"
echo "训练:       ${steps} steps / batch ${batch_size} / save ${save_freq}"

if [[ $dry_run -eq 1 ]]; then
  note "dry-run：将要执行的请求"
  printf 'POST /datasets/compatibility {"repo_ids": %s, "weights_mode": "%s"}\n' \
    "$(printf '%s\n' "${datasets[@]}" | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')" \
    "$weights_mode"
  printf 'POST /datasets/composite     {"repo_ids": %s, "weights_mode": "%s"}\n' \
    "$(printf '%s\n' "${datasets[@]}" | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')" \
    "$weights_mode"
  for index in "${!policies[@]}"; do
    printf 'POST /jobs/training          policy=%s steps=%s batch=%s\n' \
      "${policies[$index]}" "$steps" "$batch_size"
  done
  echo
  echo "dry-run 完成（未调用 API）"
  exit 0
fi

# ---------------------------------------------------------------------------
# 1) 封存（可选）
# ---------------------------------------------------------------------------
if [[ $do_seal -eq 1 ]]; then
  note "1/6 封存成员数据集"
  for repo_id in "${datasets[@]}"; do
    body=$(api_get "/datasets/local/$repo_id/seal")
    require_ok "$body" "读取 $repo_id 封存状态"
    if [[ "$(printf '%s' "$body" | jget 'd["seal"].get("state")')" == "sealed" ]]; then
      echo "  已封存 $repo_id"
      continue
    fi
    started=$(api_post "/datasets/local/$repo_id/seal" '{}')
    require_ok "$started" "封存 $repo_id"
    wait_until $((timeout_min * 60)) "封存 $repo_id" poll_seal "$repo_id" \
      || die "封存 $repo_id 超时"
  done
else
  note "1/6 跳过封存（--seal 未指定）"
fi

# ---------------------------------------------------------------------------
# 2) 同步到训练机
# ---------------------------------------------------------------------------
if [[ $skip_sync -eq 1 ]]; then
  note "2/6 跳过同步（--skip-sync）"
else
  note "2/6 同步成员到训练机"
  for repo_id in "${datasets[@]}"; do
    started=$(api_post "/datasets/local/$repo_id/sync-train" '{"dry_run":false}')
    require_ok "$started" "同步 $repo_id"
    wait_until $((timeout_min * 60)) "同步 $repo_id" poll_sync "$repo_id" \
      || die "同步 $repo_id 超时"
  done
fi

# ---------------------------------------------------------------------------
# 3) 兼容性预检 + 构建组合
# ---------------------------------------------------------------------------
note "3/6 兼容性预检"
repo_ids_json=$(printf '%s\n' "${datasets[@]}" | python3 -c 'import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')
weights_json=null
if [[ -n "$custom_weights" ]]; then
  weights_json="[${custom_weights}]"
fi
compat=$(api_post "/datasets/compatibility" \
  "{\"repo_ids\": $repo_ids_json, \"weights_mode\": \"$weights_mode\", \"weights\": $weights_json}")
require_ok "$compat" "兼容性预检"
if [[ "$(printf '%s' "$compat" | jget 'd["ok"]')" != "True" ]]; then
  printf '%s' "$compat" | python3 -c '
import json, sys
for blocker in json.load(sys.stdin).get("blockers", []):
    print("  阻塞：" + str(blocker.get("message", "")))
' >&2
  die "数据集不兼容，终止"
fi
composite_repo_id=$(printf '%s' "$compat" | jget 'd["composite_repo_id"]')
echo "  兼容性通过：$composite_repo_id"

note "4/6 训练机构建组合数据集"
build=$(api_post "/datasets/composite" \
  "{\"repo_ids\": $repo_ids_json, \"weights_mode\": \"$weights_mode\", \"weights\": $weights_json}")
require_ok "$build" "构建组合数据集"
if [[ "$(printf '%s' "$build" | jget 'd["cached"]')" == "True" ]]; then
  echo "  已缓存，直接复用"
else
  wait_until $((timeout_min * 60)) "构建组合数据集" poll_composite "$composite_repo_id" \
    || die "组合数据集构建超时"
fi

# ---------------------------------------------------------------------------
# 5) 启动训练（单策略，或每个策略一张 GPU 并行）
# ---------------------------------------------------------------------------
note "5/6 启动训练"
if [[ -n "$gpu_list" ]]; then
  IFS=',' read -r -a gpus <<< "$gpu_list"
else
  gpus=()
fi

job_ids=()
for index in "${!policies[@]}"; do
  policy=${policies[$index]}
  gpu_field=""
  if [[ ${#gpus[@]} -gt 0 ]]; then
    gpu=${gpus[$((index % ${#gpus[@]}))]}
    gpu_field=", \"gpu\": $gpu"
  fi
  payload=$(python3 - "$policy" "$steps" "$batch_size" "$save_freq" "$weights_mode" "$repo_ids_json" "$weights_json" "$gpu_field" <<'PY'
import json, sys

policy, steps, batch_size, save_freq, weights_mode, repo_ids, weights, gpu_field = sys.argv[1:9]
config = {
    "dataset_repo_ids": json.loads(repo_ids),
    "dataset_weights_mode": weights_mode,
    "policy_type": policy,
    "steps": int(steps),
    "batch_size": int(batch_size),
    "save_freq": int(save_freq),
    "num_workers": 4,
    "save_checkpoint": True,
    "env_eval_freq": 0,
    "wandb_enable": False,
}
if weights != "null":
    config["dataset_weights"] = json.loads(weights)
target = {"runner": "ssh_remote" + gpu_field}
print(json.dumps({"target": target, "config": config}))
PY
)
  started=$(api_post "/jobs/training" "$payload")
  require_ok "$started" "启动 $policy 训练"
  job_id=$(printf '%s' "$started" | jget 'd["id"]')
  job_ids+=("$job_id")
  echo "  $policy → $job_id（GPU ${gpu:-auto}）"
done

wait_until $((timeout_min * 60)) "训练作业" poll_jobs "${job_ids[@]}" || die "训练超时"

failed=0
for job_id in "${job_ids[@]}"; do
  body=$(api_get "/jobs/$job_id")
  state=$(printf '%s' "$body" | jget 'd["state"]')
  exit_code=$(printf '%s' "$body" | jget 'd.get("exit_code")')
  checkpoints=$(printf '%s' "$body" | jget 'd.get("checkpoint_count")')
  echo "  $job_id state=$state exit_code=$exit_code checkpoints=$checkpoints"
  [[ "$state" == "done" ]] || failed=1
done

# ---------------------------------------------------------------------------
# 6) 源数据只读复检（可选回收组合数据集）
# ---------------------------------------------------------------------------
note "6/6 复检封存成员"
for repo_id in "${datasets[@]}"; do
  verified=$(api_post "/datasets/local/$repo_id/seal/verify" '{}')
  require_ok "$verified" "校验 $repo_id"
  if [[ "$(printf '%s' "$verified" | jget 'd["ok"]')" == "True" ]]; then
    echo "  未修改：$repo_id"
  else
    echo "  已被修改：$repo_id" >&2
    failed=1
  fi
done

if [[ $trash_composite -eq 1 ]]; then
  trashed=$(api_post "/datasets/composites/trash" "{\"repo_id\": \"$composite_repo_id\"}")
  require_ok "$trashed" "回收组合数据集"
  echo "  已移入训练机回收站：$composite_repo_id"
fi

if (( failed != 0 )); then
  die "smoke 未通过（见上面的作业状态）"
fi
echo
echo "smoke 通过：${#datasets[@]} 个数据集 · ${#policies[@]} 个策略 · 组合 $composite_repo_id"
