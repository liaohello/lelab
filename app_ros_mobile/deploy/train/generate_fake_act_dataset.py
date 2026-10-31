#!/usr/bin/env python3
"""Generate a tiny deterministic LeRobot dataset for ACT training smoke tests.

The schema intentionally matches ``l-e-n/act_so101``: front and wrist 640x480
RGB cameras plus 6-dimensional state and action vectors. Nothing in this script
opens a camera, serial port, teleoperator, or robot connection.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from lerobot.datasets.lerobot_dataset import LeRobotDataset

DEFAULT_REPO_ID = "lelab-test/act-so101-smoke"
HEIGHT = 480
WIDTH = 640
STATE_DIM = 6


def _image(frame_index: int, episode_index: int, camera_index: int) -> np.ndarray:
    """Build a compressible but non-constant RGB frame with moving markers."""
    x = np.linspace(0, 255, WIDTH, dtype=np.uint8)
    y = np.linspace(0, 255, HEIGHT, dtype=np.uint8)
    image = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
    image[..., 0] = x[None, :]
    image[..., 1] = y[:, None]
    image[..., 2] = np.uint8((episode_index * 67 + frame_index * 13 + camera_index * 53) % 256)

    left = 24 + frame_index * 11 + camera_index * 120
    top = 40 + episode_index * 90 + camera_index * 35
    image[top : top + 48, left : left + 48] = (255, 255, 255)
    return image


def _vector(frame_index: int, episode_index: int, phase: float) -> np.ndarray:
    values = [
        math.sin(phase + joint * 0.23) + episode_index * 0.05
        for joint in range(STATE_DIM)
    ]
    values[-1] = frame_index / 10.0
    return np.asarray(values, dtype=np.float32)


def generate(root: Path, repo_id: str, episodes: int, frames: int, fps: int) -> dict:
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Refusing to overwrite non-empty dataset directory: {root}")

    features = {
        "action": {"dtype": "float32", "shape": (STATE_DIM,), "names": None},
        "observation.state": {"dtype": "float32", "shape": (STATE_DIM,), "names": None},
    }
    for camera in ("wrist", "front"):
        features[f"observation.images.{camera}"] = {
            "dtype": "image",
            "shape": (HEIGHT, WIDTH, 3),
            "names": ["height", "width", "channel"],
        }
    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        fps=fps,
        features=features,
        root=root,
        robot_type="synthetic_aloha",
        use_videos=False,
        image_writer_threads=2,
    )

    for episode_index in range(episodes):
        for frame_index in range(frames):
            phase = (episode_index * frames + frame_index) * 0.2
            state = _vector(frame_index, episode_index, phase)
            action = _vector(frame_index + 1, episode_index, phase + 0.08)
            dataset.add_frame(
                {
                    "observation.state": state,
                    "observation.images.wrist": _image(frame_index, episode_index, 0),
                    "observation.images.front": _image(frame_index, episode_index, 1),
                    "action": action,
                    "task": "Synthetic transfer-cube smoke test; no robot hardware involved.",
                }
            )
        dataset.save_episode()

    dataset.finalize()
    info_path = root / "meta" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    return {
        "repo_id": repo_id,
        "root": str(root),
        "episodes": info.get("total_episodes"),
        "frames": info.get("total_frames"),
        "features": info.get("features"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    if args.episodes < 1 or args.frames < 2 or args.fps < 1:
        parser.error("episodes and fps must be >= 1; frames must be >= 2")
    result = generate(args.root.expanduser().resolve(), args.repo_id, args.episodes, args.frames, args.fps)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
