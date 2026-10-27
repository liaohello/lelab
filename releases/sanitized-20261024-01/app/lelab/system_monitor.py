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
"""Small, failure-tolerant system snapshots for the LeLab dashboard.

The functions in this module deliberately return plain JSON-compatible values.
Hardware-specific probes are best effort and bounded by short timeouts: a
dashboard refresh must never leave a ``tegrastats`` process running in the
background or make the API unavailable because a sensor is missing.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

_JETSON_RELEASE_PATH = Path("/etc/nv_tegra_release")
_PROBE_TIMEOUT_SECONDS = 2.0


def _number(value: Any) -> int | float | None:
    """Return finite-looking numeric values without leaking psutil objects."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # JSON encoders differ in how they handle NaN/Inf. Avoid both.
        return value if value == value and abs(value) != float("inf") else None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _read_first_line(path: Path, *, limit: int = 4096) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            value = stream.readline(limit).strip()
        return value or None
    except OSError:
        return None


def _run_probe(command: list[str]) -> str | None:
    """Run a one-shot hardware probe; never raise and never leave a process."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return output or None


def _run_streaming_probe(command: list[str], *, timeout: float = 0.35) -> str | None:
    """Collect the first output from a continuous probe and always stop it.

    JetPack 6.2's ``tegrastats`` has no one-shot/count option.  ``subprocess.run``
    kills and waits for the child when the timeout expires; the partial stdout
    carried by ``TimeoutExpired`` is enough for one dashboard sample.
    """
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output: str | bytes | None = completed.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout
    except (OSError, subprocess.SubprocessError):
        return None

    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    output = (output or "").strip()
    return output or None


def _nvidia_smi_snapshot() -> dict[str, Any] | None:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    output = _run_probe(
        [
            executable,
            "--query-gpu=name,utilization.gpu,memory.total,memory.used,temperature.gpu,power.draw",
            "--format=csv,noheader,nounits",
        ]
    )
    if output is None:
        return None
    # Multiple GPUs are possible. The Jetson/LeLab dashboard currently displays
    # the first device, while preserving the device count for diagnostics.
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    fields = [part.strip() for part in lines[0].split(",")]
    if len(fields) < 6:
        return None

    def parsed(index: int) -> int | float | None:
        try:
            value = float(fields[index])
        except (IndexError, ValueError):
            return None
        return int(value) if value.is_integer() else value

    return {
        "available": True,
        "source": "nvidia-smi",
        "device_count": len(lines),
        "name": fields[0] or None,
        "utilization_percent": parsed(1),
        "memory_total_mib": parsed(2),
        "memory_used_mib": parsed(3),
        "temperature_c": parsed(4),
        "power_w": parsed(5),
    }


def _tegrastats_snapshot() -> dict[str, Any] | None:
    """Capture exactly one tegrastats sample on Jetson.

    Current JetPack releases do not provide a count option, so the continuous
    command is bounded by :func:`_run_streaming_probe` instead.
    """
    if not _JETSON_RELEASE_PATH.is_file():
        return None
    executable = shutil.which("tegrastats")
    if executable is None:
        return None
    output = _run_streaming_probe([executable, "--interval", "100"])
    if output is None:
        return None
    line = output.splitlines()[0].strip()

    ram = re.search(r"\bRAM\s+(\d+)/(\d+)MB\b", line)
    gpu = re.search(r"\bGR3D_FREQ\s+(\d+(?:\.\d+)?)%", line)
    temperatures = {
        name.lower(): float(value)
        for name, value in re.findall(r"\b([A-Za-z][\w-]*)@(\d+(?:\.\d+)?)C\b", line)
    }
    input_power = re.search(r"\bVDD_IN\s+(\d+(?:\.\d+)?)mW\b", line)
    return {
        "available": True,
        "source": "tegrastats",
        "device_count": 1,
        "name": "NVIDIA Jetson",
        "utilization_percent": _number(gpu.group(1)) if gpu else None,
        "memory_total_mib": int(ram.group(2)) if ram else None,
        "memory_used_mib": int(ram.group(1)) if ram else None,
        "temperature_c": temperatures.get("gpu"),
        "power_w": round(float(input_power.group(1)) / 1000, 3) if input_power else None,
        "temperatures_c": temperatures,
    }


def _gpu_snapshot() -> dict[str, Any]:
    snapshot = _nvidia_smi_snapshot()
    tegra = _tegrastats_snapshot() if _JETSON_RELEASE_PATH.is_file() else None
    if snapshot is None:
        snapshot = tegra
    elif tegra is not None:
        # Jetson's nvidia-smi commonly reports only the device name while
        # utilization, shared-memory usage and temperatures are N/A. Keep the
        # authoritative name but fill the missing telemetry from tegrastats.
        for key in (
            "utilization_percent",
            "memory_total_mib",
            "memory_used_mib",
            "temperature_c",
            "power_w",
        ):
            if snapshot.get(key) is None:
                snapshot[key] = tegra.get(key)
        if tegra.get("temperatures_c"):
            snapshot["temperatures_c"] = tegra["temperatures_c"]
        snapshot["source"] = "nvidia-smi+tegrastats"
    if snapshot is not None:
        return snapshot
    return {
        "available": False,
        "source": None,
        "device_count": 0,
        "name": None,
        "utilization_percent": None,
        "memory_total_mib": None,
        "memory_used_mib": None,
        "temperature_c": None,
        "power_w": None,
    }


def _normalize_temperature_limit(raw: Any, current: int | float | None) -> int | float | None:
    value = _number(raw)
    # Some Jetson hwmon drivers expose trip points in a different scale from
    # the current reading. psutil can consequently report 0.1045 (or
    # 1.045e-7) for a real 104.5 C limit.
    if current is not None and current > 10 and value is not None and value > 0:
        for _ in range(4):
            if value >= 10:
                break
            value *= 1000
    return value


def _temperature_snapshot() -> list[dict[str, Any]]:
    try:
        groups = psutil.sensors_temperatures(fahrenheit=False)
    except (AttributeError, OSError, RuntimeError):
        return []

    result: list[dict[str, Any]] = []
    for group, readings in sorted(groups.items()):
        for reading in readings:
            current = _number(getattr(reading, "current", None))
            result.append(
                {
                    "group": str(group),
                    "label": str(getattr(reading, "label", "") or group),
                    "current_c": current,
                    "high_c": _normalize_temperature_limit(
                        getattr(reading, "high", None), current
                    ),
                    "critical_c": _normalize_temperature_limit(
                        getattr(reading, "critical", None), current
                    ),
                }
            )
    return result


def _load_average() -> dict[str, float | None]:
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):
        one = five = fifteen = None
    return {
        "one_minute": _number(one),
        "five_minutes": _number(five),
        "fifteen_minutes": _number(fifteen),
    }


def _cpu_count(*, logical: bool) -> int | None:
    try:
        value = psutil.cpu_count(logical=logical)
    except (OSError, RuntimeError):
        return None
    return int(value) if value is not None else None


def _existing_disk_path(path: Path) -> Path:
    """Find an existing ancestor suitable for a filesystem usage probe."""
    expanded = path.expanduser()
    try:
        candidate = expanded.resolve(strict=False)
    except (OSError, RuntimeError):
        candidate = expanded.absolute()
    while candidate != candidate.parent:
        try:
            if candidate.exists():
                break
        except OSError:
            pass
        candidate = candidate.parent
    return candidate


def _disk_snapshot(path: Path) -> dict[str, Any]:
    probe = _existing_disk_path(path)
    try:
        usage = psutil.disk_usage(str(probe))
    except (OSError, ValueError):
        return {
            "path": str(path.expanduser()),
            "probe_path": str(probe),
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "percent": None,
        }
    return {
        "path": str(path.expanduser()),
        "probe_path": str(probe),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "percent": _number(usage.percent),
    }


def system_snapshot(dataset_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return a cross-platform, JSON-serializable system status snapshot.

    Args:
        dataset_root: Filesystem whose capacity should be reported. Defaults to
            ``HF_LEROBOT_HOME`` and then ``~/.cache/huggingface/lerobot``.

    Missing counters and unsupported sensors are represented by ``None`` (or an
    empty list), rather than turning a status refresh into an exception.
    """
    root = Path(
        dataset_root
        if dataset_root is not None
        else os.environ.get("HF_LEROBOT_HOME", "~/.cache/huggingface/lerobot")
    )

    try:
        cpu_percent = _number(psutil.cpu_percent(interval=None))
    except (OSError, RuntimeError):
        cpu_percent = None
    try:
        memory = psutil.virtual_memory()
        memory_snapshot = {
            "total_bytes": int(memory.total),
            "available_bytes": int(memory.available),
            "used_bytes": int(memory.used),
            "percent": _number(memory.percent),
        }
    except (OSError, RuntimeError):
        memory_snapshot = {
            "total_bytes": None,
            "available_bytes": None,
            "used_bytes": None,
            "percent": None,
        }
    try:
        network = psutil.net_io_counters(pernic=False, nowrap=True)
    except (OSError, RuntimeError, TypeError):
        network = None
    try:
        boot_time = float(psutil.boot_time())
        uptime_seconds = max(0.0, time.time() - boot_time)
    except (OSError, RuntimeError, ValueError):
        boot_time = None
        uptime_seconds = None
    try:
        boot_time_iso = datetime.fromtimestamp(boot_time, tz=UTC).isoformat() if boot_time else None
    except (OSError, OverflowError, ValueError):
        boot_time_iso = None

    jetson_release = _read_first_line(_JETSON_RELEASE_PATH)
    return {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "platform": {
            "system": platform.system() or None,
            "release": platform.release() or None,
            "machine": platform.machine() or None,
            "hostname": platform.node() or None,
            "python_version": platform.python_version() or None,
            "is_jetson": jetson_release is not None,
            "jetson_release": jetson_release,
        },
        "uptime_seconds": _number(uptime_seconds),
        "boot_time": boot_time_iso,
        "cpu": {
            "percent": cpu_percent,
            "logical_count": _cpu_count(logical=True),
            "physical_count": _cpu_count(logical=False),
            "load_average": _load_average(),
        },
        "memory": memory_snapshot,
        "disk": _disk_snapshot(root),
        "network": {
            "bytes_sent": int(network.bytes_sent) if network else None,
            "bytes_received": int(network.bytes_recv) if network else None,
            "packets_sent": int(network.packets_sent) if network else None,
            "packets_received": int(network.packets_recv) if network else None,
            "errors_in": int(network.errin) if network else None,
            "errors_out": int(network.errout) if network else None,
            "drops_in": int(network.dropin) if network else None,
            "drops_out": int(network.dropout) if network else None,
        },
        "temperatures": _temperature_snapshot(),
        "gpu": _gpu_snapshot(),
    }
