"""Replay Pi JS observations in no-output preview, preserving USB and RF provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from lelab_mobile_agent.local_gamepad_base import (
    AXES,
    ZERO,
    Axis,
    preview_status,
    stick_settings,
)


def replay(path):
    config = stick_settings()
    mapping = tuple(Axis.from_dict(config["axes"][name]) for name in AXES)
    limits = tuple(config["limits"][name] for name in AXES)
    connected = False
    axes = []
    axis_times = {}
    generation = 0
    transitions = []
    stationary_with_usb = []
    center_after_deflection = None
    previous = None
    axis_events = windows = nonzero = 0
    observed_directions = set()
    source = path.read_bytes()
    for line in source.splitlines():
        row = json.loads(line)
        at = row["elapsed_s"]
        kind = row["kind"]
        if kind in ("open", "device"):
            if kind == "open":
                connected, generation = True, 1
                axes = [None] * row["axes"]
            else:
                connected, generation = row["connected"], row["generation"]
                axes = list(row["axes_raw"])
            axis_times.clear()
            transitions.append({"elapsed_s": at, "connected": connected,
                                "generation": generation, "initial_axes": list(axes)})
        elif kind in ("closed", "device_error", "eof"):
            connected, axes = False, []
        elif kind == "input" and row["target"] == "axis":
            axes[row["index"]] = row["value"]
            if not row["initial"]:
                axis_events += 1
                axis_times[row["index"]] = at
        elif kind == "hid_interval":
            windows += 1

        clock = row.get("mono_ns", round(at * 1e9))
        sample = {"connected": connected, "axes_raw": axes, "reader_poll_mono_ns": clock,
                  "rf_liveness": "unverified", "rf_sample_mono_ns": None}
        status = preview_status(sample, clock, mapping, limits)
        assert not status["permitted"] and status["target_si"] is None
        assert status["wheel_target_rps"] is None and status["state_si"] is None
        intent = status["preview_si"]
        if intent is not None and any(intent):
            nonzero += 1
            for name, value in zip(AXES, intent):
                if value:
                    observed_directions.add(name + ("+" if value > 0 else "-"))
        if intent == ZERO and previous not in (None, ZERO):
            center_after_deflection = at
        if kind == "hid_interval" and row["reports"] and intent is not None and any(intent):
            axis = next((axis for axis, value in zip(mapping, intent) if value), None)
            changed_at = axis_times.get(axis.index)
            if changed_at is not None and at - changed_at > 1:
                stationary_with_usb.append({"elapsed_s": at, "axis": axis.index,
                                            "axis_value": axes[axis.index],
                                            "since_js_change_s": round(at - changed_at, 3),
                                            "usb_reports_in_window": row["reports"]})
        previous = intent

    longest = max(stationary_with_usb, key=lambda window: window["since_js_change_s"], default=None)
    return {"file": str(path), "sha256": hashlib.sha256(source).hexdigest(),
            "js_axis_changes": axis_events, "usb_report_windows": windows,
            "device_transitions": transitions, "nonzero_preview_rows": nonzero,
            "preview_directions": sorted(observed_directions),
            "last_return_to_center_after_deflection_s": center_after_deflection,
            "oldest_unchanged_deflection_with_usb_reports": longest,
            "wireless_liveness": "unverified", "physical_dispatches": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([replay(path) for path in args.logs], indent=2, allow_nan=False))
