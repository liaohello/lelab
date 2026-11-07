#!/usr/bin/env python3
"""OFFLINE ONLY - cross-check what the web page plots against what the dataset stores.

The dataset viewer labels its curves by column index (state[0..5], action[0..5]), so the
names and units have to be confirmed from the dataset's own feature metadata and the plotted
numbers matched per axis against the stored rows.  This script does exactly that and writes
the result as evidence; it does not trust a rendered curve on its own.
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(sys.argv[1])
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:18260"
OUT = pathlib.Path(sys.argv[3])

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "app_ros_mobile"))

info = json.loads((ROOT / "meta" / "info.json").read_text())
repo_id = f"lelab/{ROOT.name}"
features = info["features"]
state_meta = features["observation.state"]
action_meta = features["action"]
image_key = next(key for key in features if key.startswith("observation.images"))

report = {
    "repo_id": repo_id,
    "fps": info.get("fps"),
    "total_frames": info.get("total_frames"),
    "total_episodes": info.get("total_episodes"),
    "state_names": state_meta["names"],
    "state_units": state_meta["info"]["units"],
    "state_source": state_meta["info"]["source"],
    "action_names": action_meta["names"],
    "action_units": action_meta["info"]["units"],
    "action_source": action_meta["info"]["source"],
    "image_key": image_key,
    "image_shape": list(features[image_key]["shape"]),
    "identity": json.loads((ROOT / "meta" / "lelab_export_identity.json").read_text()),
    "phase_index": json.loads((ROOT / "meta" / "lelab_phase_index.json").read_text()),
    "episodes": [],
}

from lerobot.datasets import LeRobotDataset

dataset = LeRobotDataset(repo_id=repo_id, root=ROOT)
rows = []
for index in range(len(dataset)):
    row = dataset[index]
    rows.append({"episode_index": int(row["episode_index"]),
                 "frame_index": int(row["frame_index"]),
                 "state": [float(value) for value in row["observation.state"]],
                 "action": [float(value) for value in row["action"]]})

for episode_index in range(int(info.get("total_episodes", 0))):
    with urllib.request.urlopen(
            f"{BASE}/datasets/local/{repo_id}/episodes/{episode_index}/timeseries",
            timeout=120) as response:
        page = json.loads(response.read())
    stored = [row for row in rows if row["episode_index"] == episode_index]
    stored.sort(key=lambda row: row["frame_index"])
    mismatches = []
    # The API returns one row per sampled point, each row holding every axis, so the
    # comparison is point-major on both sides.
    for axis in range(len(state_meta["names"])):
        page_state = [float(row[axis]) for row in page["series"]["state"]]
        page_action = [float(row[axis]) for row in page["series"]["action"]]
        stored_state = [row["state"][axis] for row in stored]
        stored_action = [row["action"][axis] for row in stored]
        if len(page_state) != len(stored_state):
            mismatches.append({"axis": axis, "reason": "point count",
                               "page": len(page_state), "stored": len(stored_state)})
            continue
        for position, (a, b) in enumerate(zip(page_state, stored_state)):
            if abs(a - b) > 1e-5:
                mismatches.append({"axis": axis, "kind": "state", "at": position,
                                   "page": a, "stored": b})
        for position, (a, b) in enumerate(zip(page_action, stored_action)):
            if abs(a - b) > 1e-5:
                mismatches.append({"axis": axis, "kind": "action", "at": position,
                                   "page": a, "stored": b})
    slices = report["phase_index"]["episodes"][episode_index]["phases"]
    report["episodes"].append({
        "episode_index": episode_index,
        "frames": len(stored),
        "page_points": page["points"],
        "page_phases": [[item["phase"], item["frame_from"], item["frame_to_exclusive"]]
                        for item in page["phases"]],
        "export_phases": [[item["phase"], item["frame_from"], item["frame_to_exclusive"]]
                          for item in slices],
        "video_ranges": page.get("video_ranges") or {},
        "axis_mismatches": mismatches,
        "first_row": {"state": stored[0]["state"], "action": stored[0]["action"]} if stored else None,
        "last_row": {"state": stored[-1]["state"], "action": stored[-1]["action"]} if stored else None,
    })

report["passed"] = (not any(item["axis_mismatches"] for item in report["episodes"])
                    and all(item["page_phases"] == item["export_phases"] for item in report["episodes"])
                    and report["identity"]["training_eligible"] is False
                    and report["identity"]["data_identity"] == "simulated_offline")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({
    "passed": report["passed"],
    "state_names": report["state_names"],
    "action_names": report["action_names"],
    "episodes": [[item["episode_index"], item["frames"], item["page_phases"],
                  len(item["axis_mismatches"])] for item in report["episodes"]],
}, indent=1))
print(f"report: {OUT}")
