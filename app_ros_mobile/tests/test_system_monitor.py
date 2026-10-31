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
from types import SimpleNamespace

import pytest


def test_system_snapshot_is_json_serializable_and_uses_dataset_disk(tmp_path: Path, monkeypatch) -> None:
    from lelab import system_monitor

    monkeypatch.setattr(system_monitor, "_gpu_snapshot", lambda: {"available": False, "source": None})
    monkeypatch.setattr(system_monitor, "_temperature_snapshot", lambda: [])
    snapshot = system_monitor.system_snapshot(tmp_path)

    json.dumps(snapshot, allow_nan=False)
    assert snapshot["disk"]["path"] == str(tmp_path)
    assert snapshot["disk"]["total_bytes"] > 0
    assert set(snapshot) >= {"timestamp", "platform", "cpu", "memory", "network", "gpu"}


def test_nvidia_smi_parser_is_bounded_one_shot(monkeypatch) -> None:
    from lelab import system_monitor

    monkeypatch.setattr(system_monitor.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    seen: list[list[str]] = []

    def fake_probe(command: list[str]) -> str:
        seen.append(command)
        return "Jetson GPU, 12, 8192, 1024, 43, 7.5"

    monkeypatch.setattr(system_monitor, "_run_probe", fake_probe)
    result = system_monitor._nvidia_smi_snapshot()

    assert len(seen) == 1
    assert result is not None
    assert result["utilization_percent"] == 12
    assert result["memory_total_mib"] == 8192
    assert result["power_w"] == 7.5


def test_tegrastats_parser_requests_exactly_one_sample(tmp_path: Path, monkeypatch) -> None:
    from lelab import system_monitor

    release = tmp_path / "nv_tegra_release"
    release.write_text("# R36", encoding="utf-8")
    monkeypatch.setattr(system_monitor, "_JETSON_RELEASE_PATH", release)
    monkeypatch.setattr(system_monitor.shutil, "which", lambda name: "/usr/bin/tegrastats")
    seen: list[list[str]] = []

    def fake_probe(command: list[str], *, timeout: float = 0.35) -> str:
        seen.append(command)
        return "RAM 2048/8192MB GR3D_FREQ 17% cpu@41.5C gpu@44C VDD_IN 5250mW/5300mW"

    monkeypatch.setattr(system_monitor, "_run_streaming_probe", fake_probe)
    result = system_monitor._tegrastats_snapshot()

    assert seen == [["/usr/bin/tegrastats", "--interval", "100"]]
    assert result is not None
    assert result["memory_used_mib"] == 2048
    assert result["temperature_c"] == 44.0
    assert result["power_w"] == 5.25


def test_streaming_probe_uses_partial_output_after_timeout(monkeypatch) -> None:
    from lelab import system_monitor

    def fake_run(*args, **kwargs):
        raise system_monitor.subprocess.TimeoutExpired(
            cmd=args[0], timeout=kwargs["timeout"], output=b"one sample\n"
        )

    monkeypatch.setattr(system_monitor.subprocess, "run", fake_run)
    assert system_monitor._run_streaming_probe(["tegrastats"]) == "one sample"


def test_jetson_gpu_snapshot_fills_nvidia_smi_gaps_from_tegrastats(
    tmp_path: Path, monkeypatch
) -> None:
    from lelab import system_monitor

    release = tmp_path / "nv_tegra_release"
    release.write_text("# R36", encoding="utf-8")
    monkeypatch.setattr(system_monitor, "_JETSON_RELEASE_PATH", release)
    monkeypatch.setattr(
        system_monitor,
        "_nvidia_smi_snapshot",
        lambda: {
            "available": True,
            "source": "nvidia-smi",
            "device_count": 1,
            "name": "Orin (nvgpu)",
            "utilization_percent": None,
            "memory_total_mib": None,
            "memory_used_mib": None,
            "temperature_c": None,
            "power_w": None,
        },
    )
    monkeypatch.setattr(
        system_monitor,
        "_tegrastats_snapshot",
        lambda: {
            "available": True,
            "source": "tegrastats",
            "device_count": 1,
            "name": "NVIDIA Jetson",
            "utilization_percent": 17,
            "memory_total_mib": 8192,
            "memory_used_mib": 2048,
            "temperature_c": 44,
            "power_w": None,
            "temperatures_c": {"GPU": 44.0},
        },
    )

    result = system_monitor._gpu_snapshot()
    assert result["name"] == "Orin (nvgpu)"
    assert result["source"] == "nvidia-smi+tegrastats"
    assert result["utilization_percent"] == 17
    assert result["memory_used_mib"] == 2048
    assert result["temperature_c"] == 44


def test_snapshot_degrades_when_psutil_counters_fail(tmp_path: Path, monkeypatch) -> None:
    from lelab import system_monitor

    def fail(*args, **kwargs):
        raise OSError("counter unavailable")

    monkeypatch.setattr(system_monitor.psutil, "cpu_percent", fail)
    monkeypatch.setattr(system_monitor.psutil, "virtual_memory", fail)
    monkeypatch.setattr(system_monitor.psutil, "net_io_counters", fail)
    monkeypatch.setattr(system_monitor.psutil, "boot_time", fail)
    monkeypatch.setattr(system_monitor.psutil, "disk_usage", fail)
    monkeypatch.setattr(system_monitor.psutil, "cpu_count", lambda logical: None)
    monkeypatch.setattr(system_monitor, "_gpu_snapshot", lambda: {"available": False})
    monkeypatch.setattr(system_monitor, "_temperature_snapshot", lambda: [])

    snapshot = system_monitor.system_snapshot(tmp_path)
    assert snapshot["cpu"]["percent"] is None
    assert snapshot["memory"]["total_bytes"] is None
    assert snapshot["network"]["bytes_sent"] is None
    assert snapshot["uptime_seconds"] is None
    assert snapshot["disk"]["total_bytes"] is None


def test_temperature_snapshot_converts_named_tuples(monkeypatch) -> None:
    from lelab import system_monitor

    sensor = SimpleNamespace(label="CPU", current=42.5, high=90.0, critical=None)
    monkeypatch.setattr(
        system_monitor.psutil,
        "sensors_temperatures",
        lambda fahrenheit: {"thermal": [sensor]},
        raising=False,
    )
    assert system_monitor._temperature_snapshot() == [
        {
            "group": "thermal",
            "label": "CPU",
            "current_c": 42.5,
            "high_c": 90.0,
            "critical_c": None,
        }
    ]


def test_temperature_snapshot_normalizes_jetson_trip_point_scale(monkeypatch) -> None:
    from lelab import system_monitor

    sensor = SimpleNamespace(label="CPU", current=60.5, high=0.1045, critical=1.045e-7)
    monkeypatch.setattr(
        system_monitor.psutil,
        "sensors_temperatures",
        lambda fahrenheit: {"thermal": [sensor]},
        raising=False,
    )

    reading = system_monitor._temperature_snapshot()[0]
    assert reading["high_c"] == 104.5
    assert reading["critical_c"] == pytest.approx(104.5)
