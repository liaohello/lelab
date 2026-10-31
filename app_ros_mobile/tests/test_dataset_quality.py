"""Tests for the dataset quality gate (synthetic parquet datasets, no hardware)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from lelab.utils import dataset_quality
from lelab.utils.dataset_quality import analyze, main, render_text

_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def _write_dataset(tmp_path, episodes: dict[int, np.ndarray], fps: int = 30):
    root = tmp_path / "local" / "test_ds"
    (root / "meta").mkdir(parents=True)
    (root / "data" / "chunk-000").mkdir(parents=True)
    frames = []
    for episode, actions in episodes.items():
        for index, action in enumerate(actions):
            frames.append(
                {
                    "action": action.astype(np.float32),
                    "observation.state": action.astype(np.float32),
                    "episode_index": episode,
                    "frame_index": index,
                }
            )
    frame = pd.DataFrame(frames)
    frame.to_parquet(root / "data" / "chunk-000" / "file-000.parquet")
    (root / "meta" / "info.json").write_text(
        json.dumps(
            {
                "fps": fps,
                "total_episodes": len(episodes),
                "total_frames": len(frame),
                "features": {
                    "action": {"dtype": "float32", "shape": [6], "names": [f"{j}.pos" for j in _JOINTS]},
                    "observation.state": {"dtype": "float32", "shape": [6], "names": [f"{j}.pos" for j in _JOINTS]},
                    "observation.images.up": {"dtype": "video", "shape": [480, 640, 3]},
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _clean_episode(frames: int = 300) -> np.ndarray:
    base = np.array([0.0, -100.0, 90.0, 70.0, 0.0, 5.0])
    steps = np.arange(frames)[:, None] * np.array([0.5, 1.0, 2.0, 1.0, 0.5, 0.3])
    return base + steps


def test_clean_batch_has_no_errors(tmp_path) -> None:
    root = _write_dataset(tmp_path, {0: _clean_episode(), 1: _clean_episode()})

    report = analyze(root)
    codes = {finding["code"] for finding in report["findings"]}

    assert report["verdict"] != "error"
    assert "episode_long_pause" not in codes
    assert "batch_outside_calibration" not in codes
    assert report["episodes"][0]["duration_s"] == 10.0


def test_pause_jerk_and_start_shift_are_attributed(tmp_path) -> None:
    paused = _clean_episode(150)
    paused[30:130] = paused[29]  # 100 frames = 3.3s frozen
    shifted = _clean_episode(150) + np.array([0.0, -30.0, 0.0, 0.0, 0.0, 0.0])
    jerky = _clean_episode(150)
    jerky[40] = jerky[39] + np.array([0.0, 0.0, 12.0, 0.0, 0.0, 0.0])

    root = _write_dataset(tmp_path, {0: paused, 1: shifted, 2: jerky})
    report = analyze(root)
    by_code = {finding["code"]: finding for finding in report["findings"]}

    assert by_code["episode_long_pause"]["episode"] == 0
    assert by_code["episode_start_pose_off"]["episode"] == 1
    assert by_code["episode_jerky_steps"]["episode"] == 2
    assert "elbow_flex" in by_code["episode_jerky_steps"]["joints"]


def test_calibration_check_reports_offending_episodes(tmp_path, monkeypatch) -> None:
    root = _write_dataset(tmp_path, {0: _clean_episode()})
    monkeypatch.setattr(
        dataset_quality,
        "_calibrated_limits",
        lambda calibration, joints: dict.fromkeys(joints, (-50.0, 50.0)),
    )

    report = analyze(root, calibration=tmp_path / "calibration.json")
    findings = [finding for finding in report["findings"] if finding["code"] == "batch_outside_calibration"]

    shoulder_lift = next(finding for finding in findings if finding["joint"] == "shoulder_lift")
    assert "episodes [0]" in shoulder_lift["message"]


def test_cli_json_and_listing(tmp_path, capsys) -> None:
    root = _write_dataset(tmp_path, {0: _clean_episode()})

    exit_code = main([str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["total_episodes"] == 1

    assert main(["--root", str(root.parent)]) == 0
    assert "test_ds" in capsys.readouterr().out


def test_render_text_lists_worst_episode_first(tmp_path) -> None:
    still = _clean_episode()
    still[10:50] = still[9]
    root = _write_dataset(tmp_path, {0: _clean_episode(), 1: still})

    text = render_text(analyze(root))

    assert text.index("  1 ") < text.index("  0 ")
    assert "verdict" in text
