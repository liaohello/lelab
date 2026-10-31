"""GPU/VRAM preflight for concurrent remote training (M4)."""

from __future__ import annotations

import pytest

from lelab.jobs import (
    GpuSelectionError,
    check_train_disk_watermark,
    default_min_free_vram_mb,
    select_training_gpu,
)


def _status(*free_mb: int, free_percent: float = 50.0) -> dict:
    return {
        "gpus": [
            {"index": index, "mem_free_mb": free, "mem_total_mb": 24564}
            for index, free in enumerate(free_mb)
        ],
        "disk": {"free_percent": free_percent},
    }


def test_auto_picks_the_freest_card() -> None:
    assert select_training_gpu(_status(2000, 24544, 9000), requested_gpu=None, min_free_vram_mb=4096) == 1
    assert select_training_gpu(_status(5000, 9000), requested_gpu=None, min_free_vram_mb=4096) == 1


def test_explicit_card_must_clear_the_floor() -> None:
    assert select_training_gpu(_status(100, 24544), requested_gpu=1, min_free_vram_mb=4096) == 1
    with pytest.raises(GpuSelectionError) as excinfo:
        select_training_gpu(_status(100, 24544), requested_gpu=0, min_free_vram_mb=4096)
    assert "低于要求" in str(excinfo.value)


def test_refuses_when_no_card_qualifies_and_lists_numbers() -> None:
    with pytest.raises(GpuSelectionError) as excinfo:
        select_training_gpu(_status(100, 200), requested_gpu=None, min_free_vram_mb=4096)
    message = str(excinfo.value)
    assert "没有满足 4096 MB" in message
    assert "GPU 1 空闲 200MB" in message


def test_refuses_unknown_card_and_missing_gpus() -> None:
    with pytest.raises(GpuSelectionError):
        select_training_gpu(_status(24544), requested_gpu=3, min_free_vram_mb=4096)
    with pytest.raises(GpuSelectionError):
        select_training_gpu({"gpus": []}, requested_gpu=None, min_free_vram_mb=4096)


def test_disk_watermark_blocks_below_ten_percent() -> None:
    check_train_disk_watermark(_status(24544, free_percent=15.2))  # warn band: allowed
    check_train_disk_watermark(_status(24544, free_percent=10.0))  # exactly 10%: allowed
    with pytest.raises(GpuSelectionError) as excinfo:
        check_train_disk_watermark(_status(24544, free_percent=9.9))
    assert "低于 10%" in str(excinfo.value)
    check_train_disk_watermark({"gpus": [], "disk": {"free_percent": None}})  # unknown: allow


def test_default_floor_is_configurable(monkeypatch) -> None:
    assert default_min_free_vram_mb({}) == 4096
    assert default_min_free_vram_mb({"LELAB_TRAIN_MIN_FREE_VRAM_MB": "8192"}) == 8192
    assert default_min_free_vram_mb({"LELAB_TRAIN_MIN_FREE_VRAM_MB": "not-a-number"}) == 4096


def test_concurrent_job_ids_do_not_collide() -> None:
    from lelab.jobs import _generate_job_id

    ids = {_generate_job_id("act", "local/pick") for _ in range(50)}
    assert len(ids) == 50
