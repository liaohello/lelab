#!/usr/bin/env bash
# Screen both arms on a frozen 12-anchor subset of the dev split.
#
# Four collections, same anchors, same noise seed, same absolute view:
#   step0-a  base weights          + arm A's saved (absolute) processors
#   a        trained adapter A     + arm A's saved processors
#   step0-b  base weights          + arm B's saved (relative) processors
#   b        trained adapter B     + arm B's saved processors
#
# The step0 directories are copies of the trained checkpoint with the adapter
# files removed, so the processors (and therefore the normalisation) are exactly
# the ones each arm trained with.
set -uo pipefail
C=/home/lfy/lelab-rmr/closure/so101-ab
V=/home/lfy/lelab-rmr/assets/so101_left_red_view
PY=/home/lfy/lelab-rmr/.venv/bin/python
RUNS=/home/lfy/lelab-rmr/runs/so101-ab
OUT=$RUNS/eval
ANCHOR_EPS=$(/home/lfy/lelab-rmr/.venv/bin/python -c "
import json;d=json.load(open('/home/lfy/lelab-rmr/assets/so101_left_red_view/splits.json'))
v=sorted(int(e) for e in (d.get('validation') or d.get('dev')))
print(','.join(str(e) for e in v[:3]))")

export HF_HOME=/home/lfy/lelab-rmr/hf-cache
export HF_HUB_OFFLINE=1
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

mkdir -p "$OUT"
echo "anchors: episodes=$ANCHOR_EPS x4 fractions $(date +%H:%M:%S)"

for arm in a b; do
  src=$RUNS/$arm/checkpoints/last/pretrained_model
  dst=$OUT/step0-$arm
  rm -rf "$dst"; mkdir -p "$dst"
  for f in "$src"/*; do
    case "$(basename "$f")" in
      adapter_model.safetensors|adapter_config.json) continue ;;
      *) cp -a "$f" "$dst/" ;;
    esac
  done
done

collect() {  # $1 label  $2 checkpoint dir
  echo "=== collect $1 $(date +%H:%M:%S) ==="
  env -u PYTHONPATH $PY $C/collect_ab.py --checkpoint "$2" --view "$V" --role dev \
    --episodes "$ANCHOR_EPS" --label "$1" --isolation-check \
    --app-root /home/lfy/lelab-rmr/releases/sanitized-20261024-01/app \
    --out "$OUT/preds-$1.npz" --report "$OUT/collect-$1.json" || echo "COLLECT_FAILED $1"
}

collect step0-a "$OUT/step0-a"
collect a       "$RUNS/a/checkpoints/last/pretrained_model"
collect step0-b "$OUT/step0-b"
collect b       "$RUNS/b/checkpoints/last/pretrained_model"

echo "=== score $(date +%H:%M:%S) ==="
$PY ${C}/score_v2.py --preds "$OUT/preds-a.npz" --reference "$OUT/preds-step0-a.npz" \
  --train-stats /home/lfy/lelab-rmr/closure/so101-lora/train_stats.json \
  --windows ${C}/windows.json --label a --out "$OUT/score-a.json" || true
$PY ${C}/score_v2.py --preds "$OUT/preds-b.npz" --reference "$OUT/preds-step0-b.npz" \
  --train-stats /home/lfy/lelab-rmr/closure/so101-lora/train_stats.json \
  --windows ${C}/windows.json --label b --out "$OUT/score-b.json" || true
echo "=== done $(date +%H:%M:%S) ==="
