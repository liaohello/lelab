"""Offline diagnostics for a cloud (or local) async-inference session.

The async-inference stack writes three logs per session:

* ``robot_client_<epoch>.log`` (Jetson, DEBUG) — control-loop timings, action
  queue depth, camera/bus read times and the follower safety clamps;
* ``remote-<epoch>.log`` (Jetson, INFO) — the same run as captured from the
  client's stdout, useful when the DEBUG file log was rotated away;
* ``policy_server/.../run.log`` (train server, INFO) — per-chunk inference
  timings produced by :mod:`lerobot.async_inference.policy_server`.

This module turns those logs into the three numbers that explain a jittering
arm: sustained inference latency, action-queue depth (does it ever hit zero)
and how often the safety limits clamped the policy's targets.

Usage::

    python -m lelab.utils.inference_diag \
        --client-log ~/Desktop/lelab/app/logs/robot_client_1789823037.log \
        --server-log /tmp/policy_server_run.log

    # newest logs of the last session, wherever they live
    python -m lelab.utils.inference_diag \
        --client-log-dir ~/Desktop/lelab/app/logs \
        --console-log-dir ~/.cache/huggingface/lerobot/inference_logs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_TIMESTAMP_RE = re.compile(r"^[A-Z]+ (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_LOOP_RE = re.compile(r"Control loop \(ms\): ([\d.]+)")
_QUEUE_RE = re.compile(r"QUEUE SIZE: (\d+) \(Must go: (\w+)\)")
_READ_TIME_RE = re.compile(r"read (?:state|[\w-]+): ([\d.]+)ms")
_SERIALIZE_RE = re.compile(r"Observation serialization time: ([\d.]+)s")
_CHUNK_RECEIVED_RE = re.compile(r"Received action chunk for step #(\d+)")
_ACTIONS_RECEIVED_RE = re.compile(r"Received actions on device")
_CHUNK_GENERATED_RE = re.compile(r"Action chunk #(\d+) generated \| Total time: ([\d.]+)ms")
_INFERENCE_RE = re.compile(r"Preprocessing and inference took ([\d.]+)s")
_SERVER_OBS_RE = re.compile(r"Running inference for observation #(\d+) \(must_go: (\w+)\)")
_CONFIG_ACTIONS_RE = re.compile(r"'actions_per_chunk': (\d+)")
_CONFIG_FPS_RE = re.compile(r"'fps': (\d+)")
_CONFIG_THRESHOLD_RE = re.compile(r"'chunk_size_threshold': ([\d.]+)")
_PERF_RE = re.compile(
    r"\[PERF\] rate=(?P<rate>[\d.]+)Hz "
    r"queue min/avg/max=(?P<qmin>[-\d.]+)/(?P<qavg>[-\d.]+)/(?P<qmax>[-\d.]+) "
    r"sends=(?P<sends>\d+) drop=(?P<drop>\d+) "
    r"rtt_ms n=(?P<rtt_n>\d+) p50=(?P<rtt_p50>[-\d.]+) p90=(?P<rtt_p90>[-\d.]+) "
    r"must_go=(?P<must_go>\d+)"
)

CLAMP_RELATIVE = "relative"
CLAMP_WORKSPACE = "workspace"
_CLAMP_MARKERS = {
    "Relative goal position magnitude had to be clamped": CLAMP_RELATIVE,
    "exceeded the configured startup-position workspace and was clamped": CLAMP_WORKSPACE,
}
_JOINT_ENTRY_RE = re.compile(
    r"'([a-z0-9_]+)':\s*\{\s*'original goal_pos':\s*(-?[\d.]+),\s*'safe goal_pos':\s*(-?[\d.]+)"
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "count": len(values),
        "min": round(min(values), 2),
        "p50": round(_percentile(values, 0.5) or 0.0, 2),
        "p90": round(_percentile(values, 0.9) or 0.0, 2),
        "p99": round(_percentile(values, 0.99) or 0.0, 2),
        "max": round(max(values), 2),
        "mean": round(sum(values) / len(values), 2),
    }


def _parse_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


@dataclass
class _LoopIteration:
    timestamp: datetime | None
    duration_ms: float
    observation_sends: int = 0
    queue_samples: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------
# client log
# --------------------------------------------------------------------------


def parse_client_log(path: Path) -> dict[str, Any]:
    """Extract control-loop, queue and clamp statistics from a client log."""
    iterations: list[_LoopIteration] = []
    pending_sends = 0
    pending_queue: list[int] = []
    queue_samples: list[tuple[datetime | None, int, bool]] = []
    read_times: list[float] = []
    serialize_times: list[float] = []
    chunks_received = 0
    clamps: list[dict[str, Any]] = []
    timestamps: list[datetime] = []
    perf_windows: list[dict[str, Any]] = []
    actions_per_chunk: int | None = None
    fps: int | None = None
    chunk_size_threshold: float | None = None

    lines = _read_lines(path)
    index = 0
    while index < len(lines):
        line = lines[index]
        timestamp = _parse_timestamp(line)
        if timestamp is not None:
            timestamps.append(timestamp)

        if actions_per_chunk is None:
            match = _CONFIG_ACTIONS_RE.search(line)
            if match:
                actions_per_chunk = int(match.group(1))
        if fps is None:
            match = _CONFIG_FPS_RE.search(line)
            if match:
                fps = int(match.group(1))
        if chunk_size_threshold is None:
            match = _CONFIG_THRESHOLD_RE.search(line)
            if match:
                chunk_size_threshold = float(match.group(1))

        match = _QUEUE_RE.search(line)
        if match:
            size = int(match.group(1))
            must_go = match.group(2).lower() == "true"
            queue_samples.append((timestamp, size, must_go))
            pending_sends += 1
            pending_queue.append(size)

        match = _LOOP_RE.search(line)
        if match:
            iterations.append(
                _LoopIteration(
                    timestamp=timestamp,
                    duration_ms=float(match.group(1)),
                    observation_sends=pending_sends,
                    queue_samples=pending_queue,
                )
            )
            pending_sends = 0
            pending_queue = []

        match = _READ_TIME_RE.search(line)
        if match:
            read_times.append(float(match.group(1)))

        match = _SERIALIZE_RE.search(line)
        if match:
            serialize_times.append(float(match.group(1)) * 1000)

        if _CHUNK_RECEIVED_RE.search(line) or _ACTIONS_RECEIVED_RE.search(line):
            chunks_received += 1

        match = _PERF_RE.search(line)
        if match:
            perf_windows.append(
                {
                    "timestamp": timestamp.isoformat(sep=" ") if timestamp else None,
                    "rate_hz": float(match.group("rate")),
                    "queue_min": _maybe_float(match.group("qmin")),
                    "queue_avg": _maybe_float(match.group("qavg")),
                    "queue_max": _maybe_float(match.group("qmax")),
                    "sends": int(match.group("sends")),
                    "drops": int(match.group("drop")),
                    "rtt_samples": int(match.group("rtt_n")),
                    "rtt_p50_ms": _maybe_float(match.group("rtt_p50")),
                    "rtt_p90_ms": _maybe_float(match.group("rtt_p90")),
                    "must_go": int(match.group("must_go")),
                }
            )

        clamp_kind = next((kind for marker, kind in _CLAMP_MARKERS.items() if marker in line), None)
        if clamp_kind is not None:
            body: list[str] = []
            index += 1
            depth = 0
            while index < len(lines):
                current = lines[index]
                depth += current.count("{") - current.count("}")
                body.append(current)
                index += 1
                if depth <= 0:
                    break
            text = "\n".join(body)
            for joint, original, safe in _JOINT_ENTRY_RE.findall(text):
                clamps.append(
                    {
                        "timestamp": timestamp,
                        "kind": clamp_kind,
                        "joint": joint,
                        "original": float(original),
                        "safe": float(safe),
                    }
                )
            continue
        index += 1

    window_start = timestamps[0] if timestamps else None
    window_end = timestamps[-1] if timestamps else None
    window_s = (window_end - window_start).total_seconds() if window_start and window_end else 0.0

    loop_durations = [iteration.duration_ms for iteration in iterations]
    with_observation = [iteration.duration_ms for iteration in iterations if iteration.observation_sends]
    without_observation = [
        iteration.duration_ms for iteration in iterations if not iteration.observation_sends
    ]
    queue_values = [size for _, size, _ in queue_samples]
    first_filled = next((position for position, (_, size, _) in enumerate(queue_samples) if size > 0), None)
    zeros_after_fill = 0
    if first_filled is not None:
        zeros_after_fill = sum(1 for _, size, _ in queue_samples[first_filled:] if size == 0)

    return {
        "path": str(path),
        "window_start": window_start.isoformat(sep=" ") if window_start else None,
        "window_end": window_end.isoformat(sep=" ") if window_end else None,
        "window_s": round(window_s, 1),
        "config": {
            "actions_per_chunk": actions_per_chunk,
            "fps": fps,
            "chunk_size_threshold": chunk_size_threshold,
        },
        "control_loop": {
            "iterations": len(iterations),
            "rate_hz": round(len(iterations) / window_s, 2) if window_s > 0 else None,
            "duration_ms": _summary(loop_durations),
            "with_observation_ms": _summary(with_observation),
            "without_observation_ms": _summary(without_observation),
        },
        "action_queue": {
            "samples": len(queue_samples),
            "min": min(queue_values) if queue_values else None,
            "max": max(queue_values) if queue_values else None,
            "mean": round(sum(queue_values) / len(queue_values), 2) if queue_values else None,
            "p50": _percentile([float(value) for value in queue_values], 0.5),
            "zero_samples": sum(1 for value in queue_values if value == 0),
            "zero_samples_after_fill": zeros_after_fill,
            "first_fill_sample": first_filled,
            "must_go_true": sum(1 for _, _, must_go in queue_samples if must_go),
            "timeline": [
                {
                    "timestamp": timestamp.isoformat(sep=" ") if timestamp else None,
                    "size": size,
                    "must_go": must_go,
                }
                for timestamp, size, must_go in queue_samples
            ],
        },
        "client_io": {
            "observation_sends": len(queue_samples),
            "serialize_ms": _summary(serialize_times),
            "joint_read_ms": _summary(read_times),
            "chunks_received": chunks_received,
        },
        "perf": _summarize_perf(perf_windows),
        "clamps": _summarize_clamps(clamps),
    }


def _maybe_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _summarize_perf(windows: list[dict[str, Any]]) -> dict[str, Any]:
    if not windows:
        return {"windows": [], "rate_hz": None, "rtt_p50_ms": None, "sends": 0, "drops": 0}
    rates = [window["rate_hz"] for window in windows]
    rtt_p50 = [window["rtt_p50_ms"] for window in windows if window["rtt_p50_ms"] is not None]
    rtt_p90 = [window["rtt_p90_ms"] for window in windows if window["rtt_p90_ms"] is not None]
    return {
        "windows": windows,
        "rate_hz": _summary(rates),
        "rtt_p50_ms": _summary(rtt_p50),
        "rtt_p90_ms": _summary(rtt_p90),
        "sends": sum(window["sends"] for window in windows),
        "drops": sum(window["drops"] for window in windows),
        "must_go": sum(window["must_go"] for window in windows),
    }


def _summarize_clamps(clamps: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind = Counter(clamp["kind"] for clamp in clamps)
    by_joint = Counter(f"{clamp['kind']}:{clamp['joint']}" for clamp in clamps)
    magnitudes: dict[str, list[float]] = {}
    for clamp in clamps:
        magnitudes.setdefault(f"{clamp['kind']}:{clamp['joint']}", []).append(
            abs(clamp["original"] - clamp["safe"])
        )
    saturation: dict[str, dict[str, Any]] = {}
    for clamp in clamps:
        key = f"{clamp['kind']}:{clamp['joint']}"
        entry = saturation.setdefault(key, {"safe_values": Counter(), "count": 0, "timestamps": []})
        entry["safe_values"][round(clamp["safe"], 3)] += 1
        entry["count"] += 1
        if clamp["timestamp"] is not None:
            entry["timestamps"].append(clamp["timestamp"])

    per_second = Counter(
        clamp["timestamp"].strftime("%H:%M:%S") for clamp in clamps if clamp["timestamp"] is not None
    )
    details: dict[str, Any] = {}
    for key, entry in saturation.items():
        top_safe, top_count = entry["safe_values"].most_common(1)[0]
        details[key] = {
            "count": entry["count"],
            "magnitude_deg": _summary(magnitudes[key]),
            "distinct_safe_values": len(entry["safe_values"]),
            "dominant_safe_value": top_safe,
            "dominant_safe_value_hits": top_count,
            "saturated": top_count >= 5 and len(entry["safe_values"]) <= 2,
        }
    return {
        "total": len(clamps),
        "by_kind": dict(by_kind),
        "by_joint": dict(by_joint),
        "details": details,
        "busiest_second": per_second.most_common(3),
        "events": [
            {
                "timestamp": clamp["timestamp"].isoformat(sep=" ") if clamp["timestamp"] else None,
                "kind": clamp["kind"],
                "joint": clamp["joint"],
                "original": round(clamp["original"], 3),
                "safe": round(clamp["safe"], 3),
            }
            for clamp in clamps
        ],
    }


# --------------------------------------------------------------------------
# server log
# --------------------------------------------------------------------------


def parse_server_log(path: Path) -> dict[str, Any]:
    """Extract per-chunk inference timings from a PolicyServer run log."""
    chunk_total_ms: list[float] = []
    inference_ms: list[float] = []
    chunk_times: list[datetime] = []
    inference_observations: list[tuple[datetime | None, int, bool]] = []

    for line in _read_lines(path):
        match = _CHUNK_GENERATED_RE.search(line)
        if match:
            chunk_total_ms.append(float(match.group(2)))
            timestamp = _parse_timestamp(line)
            if timestamp is not None:
                chunk_times.append(timestamp)
            continue
        match = _INFERENCE_RE.search(line)
        if match:
            inference_ms.append(float(match.group(1)) * 1000)
            continue
        match = _SERVER_OBS_RE.search(line)
        if match:
            inference_observations.append(
                (_parse_timestamp(line), int(match.group(1)), match.group(2).lower() == "true")
            )

    gaps_s = [
        (later - earlier).total_seconds()
        for earlier, later in zip(chunk_times, chunk_times[1:], strict=False)
    ]
    window_s = (chunk_times[-1] - chunk_times[0]).total_seconds() if len(chunk_times) > 1 else 0.0
    cold_start_ms = chunk_total_ms[0] if chunk_total_ms else None
    warm_total = chunk_total_ms[1:] if len(chunk_total_ms) > 1 else []
    return {
        "path": str(path),
        "window_start": chunk_times[0].isoformat(sep=" ") if chunk_times else None,
        "window_end": chunk_times[-1].isoformat(sep=" ") if chunk_times else None,
        "chunks": len(chunk_total_ms),
        "chunk_rate_hz": round(len(chunk_total_ms) / window_s, 2) if window_s > 0 else None,
        "chunk_total_ms": _summary(chunk_total_ms),
        "chunk_total_ms_warm": _summary(warm_total),
        "cold_start_ms": cold_start_ms,
        "gpu_inference_ms": _summary(inference_ms),
        "chunk_gap_s": _summary(gaps_s),
        "observations_run": len(inference_observations),
        "must_go_runs": sum(1 for _, _, must_go in inference_observations if must_go),
    }


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _findings(
    client: dict[str, Any] | None,
    server: dict[str, Any] | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if client is not None:
        loop = client["control_loop"]
        queue = client["action_queue"]
        config = client["config"]
        baseline = loop["without_observation_ms"]
        blocked = loop["with_observation_ms"]
        if blocked and baseline and blocked["p50"] > 50 and blocked["p50"] > 5 * baseline["p50"]:
            findings.append(
                {
                    "severity": "high",
                    "code": "control_loop_blocked_by_observation",
                    "message": (
                        f"Control-loop iterations that upload an observation take "
                        f"{blocked['p50']:.0f}ms (p50) vs {baseline['p50']:.1f}ms for the other "
                        f"{loop['iterations'] - blocked['count']} iterations; the loop runs at "
                        f"{loop['rate_hz']} Hz instead of the configured {config['fps']} Hz."
                    ),
                }
            )
        if queue["zero_samples_after_fill"] > 0:
            findings.append(
                {
                    "severity": "medium",
                    "code": "action_queue_starvation",
                    "message": (
                        f"The action queue hit zero {queue['zero_samples_after_fill']} times after "
                        "it first filled: the arm had no action to play while waiting for the next chunk."
                    ),
                }
            )
        elif queue["samples"] and queue["min"] is not None and queue["min"] > 0:
            findings.append(
                {
                    "severity": "info",
                    "code": "action_queue_ok",
                    "message": (
                        f"The queue never emptied after warmup (min depth {queue['min']}, "
                        f"{queue['samples']} samples): chunk starvation is not the jitter source."
                    ),
                }
            )
        perf = client.get("perf") or {}
        if perf.get("windows"):
            rtt = perf.get("rtt_p50_ms")
            rtt_p90 = perf.get("rtt_p90_ms")
            if perf["drops"] > max(5, 3 * perf["sends"]):
                findings.append(
                    {
                        "severity": "medium",
                        "code": "observation_upload_backlog",
                        "message": (
                            f"The client dropped {perf['drops']} superseded observations against "
                            f"{perf['sends']} uploads: the link cannot carry every frame."
                        ),
                    }
                )
            if rtt and rtt["p50"] > 1000:
                findings.append(
                    {
                        "severity": "medium",
                        "code": "cloud_round_trip_slow",
                        "message": (
                            f"Observation upload -> chunk arrival p50 is {rtt['p50']:.0f}ms "
                            f"(typical p90 {rtt_p90['p50']:.0f}ms); the loop no longer blocks, "
                            "but chunks are stale."
                        ),
                    }
                )
            elif rtt:
                findings.append(
                    {
                        "severity": "info",
                        "code": "cloud_round_trip_ok",
                        "message": (
                            f"Observation upload -> chunk arrival p50 is {rtt['p50']:.0f}ms "
                            f"(typical p90 {rtt_p90['p50']:.0f}ms)."
                        ),
                    }
                )
        details = client["clamps"]["details"]
        saturated = [
            key for key, entry in details.items() if entry["saturated"] and entry["count"] >= 10
        ]
        if saturated:
            summary = ", ".join(
                f"{key} ({details[key]['count']}x pinned at "
                f"{details[key]['dominant_safe_value']}deg)"
                for key in saturated
            )
            findings.append(
                {
                    "severity": "high",
                    "code": "safety_limits_saturated",
                    "message": (
                        f"Safety limits pinned the arm on {summary}: the policy kept requesting "
                        "targets outside the allowed range."
                    ),
                }
            )
        elif client["clamps"]["total"]:
            findings.append(
                {
                    "severity": "medium",
                    "code": "safety_limits_active",
                    "message": (
                        f"Safety limits clamped {client['clamps']['total']} joint targets but no "
                        "joint was pinned at a single boundary value."
                    ),
                }
            )
    if client is not None and server is not None:
        config = client["config"]
        if config.get("actions_per_chunk") and config.get("fps"):
            playout_s = config["actions_per_chunk"] / config["fps"]
            warm = server["chunk_total_ms_warm"]
            if warm and playout_s > 0:
                utilization = (warm["p90"] / 1000) / playout_s
                if utilization > 0.8:
                    findings.append(
                        {
                            "severity": "medium",
                            "code": "inference_budget_tight",
                            "message": (
                                f"Server chunk time p90 is {warm['p90']:.0f}ms against a "
                                f"{playout_s:.2f}s chunk playout ({utilization:.0%} of the budget)."
                            ),
                        }
                    )
                else:
                    findings.append(
                        {
                            "severity": "info",
                            "code": "inference_budget_ok",
                            "message": (
                                f"Server chunk time p90 is {warm['p90']:.0f}ms against a "
                                f"{playout_s:.2f}s chunk playout ({utilization:.0%} of the budget)."
                            ),
                        }
                    )
    return findings


def build_report(
    client_log: Path | None = None,
    server_log: Path | None = None,
) -> dict[str, Any]:
    client = parse_client_log(client_log) if client_log is not None else None
    server = parse_server_log(server_log) if server_log is not None else None
    report: dict[str, Any] = {"client": client, "server": server}
    report["findings"] = _findings(client, server)
    return report


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    client = report.get("client")
    server = report.get("server")
    if client is not None:
        loop = client["control_loop"]
        queue = client["action_queue"]
        clamps = client["clamps"]
        lines.append("== Client (robot_client) ==")
        lines.append(f"log          : {client['path']}")
        lines.append(
            f"window       : {client['window_start']} -> {client['window_end']} "
            f"({client['window_s']}s)"
        )
        config = client["config"]
        lines.append(
            "config       : "
            f"actions_per_chunk={config['actions_per_chunk']} fps={config['fps']} "
            f"chunk_size_threshold={config['chunk_size_threshold']}"
        )
        if loop["duration_ms"]:
            lines.append(
                f"control loop : {loop['iterations']} iters, {loop['rate_hz']} Hz "
                f"(p50 {loop['duration_ms']['p50']}ms, p90 {loop['duration_ms']['p90']}ms, "
                f"max {loop['duration_ms']['max']}ms)"
            )
        if loop["with_observation_ms"]:
            blocked = loop["with_observation_ms"]
            baseline = loop["without_observation_ms"]
            lines.append(
                f"  observation iters : n={blocked['count']} p50 {blocked['p50']}ms "
                f"p90 {blocked['p90']}ms"
            )
            if baseline:
                lines.append(
                    f"  other iters       : n={baseline['count']} p50 {baseline['p50']}ms "
                    f"p90 {baseline['p90']}ms"
                )
        lines.append(
            f"action queue : samples={queue['samples']} min={queue['min']} p50={queue['p50']} "
            f"max={queue['max']} zeros={queue['zero_samples']} "
            f"(after first fill: {queue['zero_samples_after_fill']}) must_go={queue['must_go_true']}"
            "  [sampled while the client chooses to upload, i.e. <= threshold; only dips matter]"
        )
        io = client["client_io"]
        if io["serialize_ms"]:
            lines.append(
                f"client i/o   : sends={io['observation_sends']} "
                f"pickle p50 {io['serialize_ms']['p50']}ms, "
                f"chunks received={io['chunks_received']}"
            )
        perf = client.get("perf") or {}
        if perf.get("windows"):
            rate = perf["rate_hz"]
            rtt = perf["rtt_p50_ms"]
            lines.append(
                f"client perf  : windows={len(perf['windows'])} rate p50={rate['p50']}Hz "
                f"sends={perf['sends']} drops={perf['drops']} must_go={perf['must_go']}"
            )
            if rtt:
                lines.append(
                    f"  upload->chunk: p50={rtt['p50']}ms "
                    f"typical p90={perf['rtt_p90_ms']['p50']:.0f}ms max={rtt['max']}ms"
                )
        lines.append(f"clamps       : total={clamps['total']} by_kind={clamps['by_kind']}")
        for key, detail in clamps["details"].items():
            magnitude = detail["magnitude_deg"]
            lines.append(
                f"  {key:<28} n={detail['count']:<5} p50={magnitude['p50']:.2f}deg "
                f"p90={magnitude['p90']:.2f}deg max={magnitude['max']:.2f}deg "
                f"dominant safe={detail['dominant_safe_value']}deg "
                f"x{detail['dominant_safe_value_hits']} "
                f"{'PINNED' if detail['saturated'] else ''}"
            )
    if server is not None:
        warm = server["chunk_total_ms_warm"] or server["chunk_total_ms"]
        lines.append("")
        lines.append("== PolicyServer (train server) ==")
        lines.append(f"log          : {server['path']}")
        lines.append(
            f"window       : {server['window_start']} -> {server['window_end']}"
        )
        lines.append(
            f"chunks       : {server['chunks']} ({server['chunk_rate_hz']} Hz), "
            f"cold start {server['cold_start_ms']}ms"
        )
        if warm:
            lines.append(
                f"chunk time   : p50 {warm['p50']}ms p90 {warm['p90']}ms p99 {warm['p99']}ms "
                f"max {warm['max']}ms"
            )
        if server["gpu_inference_ms"]:
            gpu = server["gpu_inference_ms"]
            lines.append(
                f"gpu infer    : p50 {gpu['p50']}ms p90 {gpu['p90']}ms max {gpu['max']}ms"
            )
        if server["chunk_gap_s"]:
            gap = server["chunk_gap_s"]
            lines.append(
                f"chunk gap    : p50 {gap['p50']}s p90 {gap['p90']}s max {gap['max']}s "
                f"(observations run: {server['observations_run']}, must_go: {server['must_go_runs']})"
            )
    findings = report.get("findings") or []
    if findings:
        lines.append("")
        lines.append("== Findings ==")
        for finding in findings:
            lines.append(f"[{finding['severity']:<6}] {finding['code']}: {finding['message']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _newest(directory: Path, pattern: str) -> Path | None:
    candidates = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def _resolve_client_log(args: argparse.Namespace) -> Path | None:
    if args.client_log:
        return Path(args.client_log).expanduser()
    if args.client_log_dir:
        return _newest(Path(args.client_log_dir).expanduser(), "robot_client_*.log")
    return None


def _resolve_console_log(args: argparse.Namespace) -> Path | None:
    if getattr(args, "console_log", None):
        return Path(args.console_log).expanduser()
    if getattr(args, "console_log_dir", None):
        return _newest(Path(args.console_log_dir).expanduser(), "remote-*.log")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cloud inference jitter diagnostics")
    parser.add_argument("--client-log", help="robot_client_<epoch>.log (DEBUG, preferred)")
    parser.add_argument("--client-log-dir", help="directory holding robot_client_*.log files")
    parser.add_argument(
        "--console-log",
        help="remote-<epoch>.log fallback when the DEBUG file log is missing",
    )
    parser.add_argument("--console-log-dir", help="directory holding remote-*.log files")
    parser.add_argument("--server-log", help="PolicyServer run.log from the train server")
    parser.add_argument("--json", action="store_true", help="print the raw report as JSON")
    args = parser.parse_args(argv)

    client_log = _resolve_client_log(args)
    if client_log is None:
        console = _resolve_console_log(args)
        if console is not None and console.exists():
            client_log = console
    if client_log is not None and not client_log.exists():
        print(f"client log not found: {client_log}", file=sys.stderr)
        return 2
    server_log = Path(args.server_log).expanduser() if args.server_log else None
    if server_log is not None and not server_log.exists():
        print(f"server log not found: {server_log}", file=sys.stderr)
        return 2
    if client_log is None and server_log is None:
        parser.error("provide --client-log/--client-log-dir or --server-log")

    report = build_report(client_log, server_log)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
