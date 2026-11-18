"""Throwaway smoke test: drive score_predictions.py CLI with synthetic arrays.

Not evidence — it only proves the CLI, the train-only stats guard and the
zero-std handling work on the real 12-dim statistics.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
run = HERE.parent
stats = json.loads((run / "train_stats.json").read_text())
mean = np.asarray(stats["action"]["mean"], dtype=np.float64)
std = np.asarray(stats["action"]["std"], dtype=np.float64)

rng = np.random.default_rng(0)
N, T, D = 8, 32, 12
gt = np.zeros((N, T, D))
for d in range(D):
    wave = np.sin(np.linspace(0, 2 * np.pi, T) + d)
    gt[:, :, d] = mean[d] + std[d] * wave

pad = np.zeros((N, T), dtype=bool)
pad[:, -1] = True
state = gt[:, 0, :].copy()

perfect = gt.copy()
noisy = gt.copy()
noisy[:, :, :6] += rng.normal(0, std[:6] * 0.4, size=(N, T, 6))
hold = np.broadcast_to(state[:, None, :], gt.shape).copy()

for name, pred in (("perfect", perfect), ("noisy", noisy), ("hold", hold)):
    path = Path(f"/tmp/smoke-{name}.npz")
    np.savez_compressed(path, pred=pred, gt=gt, pad=pad, state=state,
                        episode=np.arange(N), anchor=np.zeros(N, dtype=int))

cmd = [
    sys.executable, str(HERE / "score_predictions.py"),
    "--preds", "/tmp/smoke-noisy.npz",
    "--train-stats", str(run / "train_stats.json"),
    "--reference", "/tmp/smoke-hold.npz",
    "--out", "/tmp/smoke-score.json",
    "--label", "smoke",
]
out = subprocess.run(cmd, capture_output=True, text=True)
print("exit", out.returncode)
print(out.stdout[-1200:])
print(out.stderr[-800:])
report = json.loads(Path("/tmp/smoke-score.json").read_text())
print("active dims", report["active_dimensions"])
print("static std", [round(v, 4) for v in report["static_dimension_std"]])
print("status", report["status"], report["verdict"]["checks"])
