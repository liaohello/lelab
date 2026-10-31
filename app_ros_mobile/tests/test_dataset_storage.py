# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_dataset(root: Path, repo_id: str = "lab/pick-cube") -> Path:
    dataset = root / repo_id
    (dataset / "meta").mkdir(parents=True)
    (dataset / "data" / "chunk-000").mkdir(parents=True)
    info = {
        "codebase_version": "v3.0",
        "robot_type": "so101_follower",
        "fps": 30,
        "total_episodes": 2,
        "total_frames": 120,
        "total_tasks": 1,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [6], "names": ["joint"]},
            "observation.images.front": {
                "dtype": "video",
                "shape": [480, 640, 3],
                "names": ["height", "width", "channel"],
                "info": {"video.codec": "h264"},
            },
        },
    }
    (dataset / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    (dataset / "data" / "chunk-000" / "episode_000000.parquet").write_bytes(b"not-real-parquet")
    return dataset


@pytest.mark.parametrize(
    "repo_id",
    ["../secret", "team/../../secret", "/absolute", r"team\secret", "C:/secret", "team//secret", "."],
)
def test_resolve_dataset_path_rejects_unsafe_repo_ids(tmp_path: Path, repo_id: str) -> None:
    from lelab.dataset_storage import resolve_dataset_path

    with pytest.raises(ValueError):
        resolve_dataset_path(repo_id, tmp_path)


def test_resolve_dataset_path_accepts_hf_repo_id(tmp_path: Path) -> None:
    from lelab.dataset_storage import resolve_dataset_path

    assert resolve_dataset_path("lab/pick-cube", tmp_path) == (tmp_path / "lab" / "pick-cube").resolve()


def test_dataset_detail_exposes_format_tree_and_unavailable_parquet(tmp_path: Path, monkeypatch) -> None:
    from lelab import dataset_storage

    _make_dataset(tmp_path)
    monkeypatch.setattr(dataset_storage, "_load_parquet_module", lambda: None)
    detail = dataset_storage.dataset_detail("lab/pick-cube", tmp_path)

    json.dumps(detail, allow_nan=False)
    assert detail["format"]["robot_type"] == "so101_follower"
    assert detail["format"]["storage_templates"]["data_path"].startswith("data/")
    by_name = {item["name"]: item for item in detail["format"]["features"]}
    assert by_name["observation.state"]["storage"] == "parquet"
    assert by_name["observation.images.front"]["storage"] == "video"
    assert detail["storage"]["file_count"] == 2
    assert detail["file_tree"]["entry_count"] > 0
    assert detail["parquet_schema"]["status"] == "unavailable"
    assert "pyarrow" in detail["parquet_schema"]["reason"]


def test_dataset_file_tree_obeys_entry_and_depth_limits(tmp_path: Path) -> None:
    from lelab.dataset_storage import dataset_file_tree

    dataset = _make_dataset(tmp_path)
    for index in range(8):
        (dataset / f"file-{index}.txt").write_text("x", encoding="utf-8")

    tree = dataset_file_tree(dataset, max_entries=3, max_depth=1)
    assert tree["entry_count"] <= 3
    assert tree["truncated"] is True


def test_storage_summary_finds_local_dataset_and_capacity(tmp_path: Path) -> None:
    from lelab.dataset_storage import storage_summary

    _make_dataset(tmp_path)
    summary = storage_summary(tmp_path)

    json.dumps(summary, allow_nan=False)
    assert summary["exists"] is True
    assert summary["dataset_count"] == 1
    assert summary["datasets"] == ["lab/pick-cube"]
    assert summary["dataset_items"] == [
        {
            "repo_id": "lab/pick-cube",
            "codebase_version": "v3.0",
            "robot_type": "so101_follower",
            "fps": 30,
            "total_episodes": 2,
            "total_frames": 120,
            "camera_count": 1,
            "duration_seconds": 4.0,
            "finalized": False,
            "size_bytes": None,
            "last_modified": summary["dataset_items"][0]["last_modified"],
            "metadata_error": None,
        }
    ]
    assert summary["dataset_storage"]["size_bytes"] > 0
    assert summary["disk"]["free_bytes"] > 0


def test_dataset_disk_capacity_does_not_walk_dataset_files(tmp_path: Path, monkeypatch) -> None:
    from lelab import dataset_storage

    _make_dataset(tmp_path)
    monkeypatch.setattr(
        dataset_storage.os,
        "walk",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not walk")),
    )

    capacity = dataset_storage.dataset_disk_capacity(tmp_path)
    assert capacity["total_bytes"] > 0
    assert capacity["free_bytes"] > 0


def test_dataset_detail_rejects_non_dataset(tmp_path: Path) -> None:
    from lelab.dataset_storage import dataset_detail

    (tmp_path / "lab" / "not-a-dataset").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        dataset_detail("lab/not-a-dataset", tmp_path)


def test_dataset_detail_reports_invalid_metadata_without_throwing(tmp_path: Path) -> None:
    from lelab.dataset_storage import dataset_detail

    dataset = tmp_path / "lab" / "broken"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text("not-json", encoding="utf-8")

    detail = dataset_detail("lab/broken", tmp_path)
    assert detail["metadata"] is None
    assert detail["metadata_error"]
    assert detail["format"]["features"] == []


def test_dataset_detail_normalizes_non_finite_json_numbers(tmp_path: Path) -> None:
    from lelab.dataset_storage import dataset_detail

    dataset = tmp_path / "lab" / "non-finite"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta" / "info.json").write_text('{"fps": NaN}', encoding="utf-8")

    detail = dataset_detail("lab/non-finite", tmp_path)
    assert detail["format"]["fps"] is None
    json.dumps(detail, allow_nan=False)
