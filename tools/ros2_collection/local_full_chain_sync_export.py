#!/usr/bin/env python3
"""Sync and export the rehearsal's car session through the real upper-computer code path.

Everything up to this point in the rehearsal is exercised by the driver with its own transport, so
this script closes the last two links - 校验同步 and 数据检查与导出 - with no substitute: the
production `ros_mobile.sync_session` fetches the archive over HTTP from the running agent, verifies
every transferred file against the manifest, and `ros_mobile.export_session` then reads the sqlite3
bag and writes a real LeRobotDataset. Only the topics are synthetic; the code is the one the page
calls.

Run it in the app virtualenv while `local_car_rehearsal.py` is still up:

    LELAB_ROS_COLLECTION_ROOT=/tmp/lelab_car_rehearsal/upper \
    app_ros_mobile/.venv/bin/python tools/ros2_collection/local_full_chain_sync_export.py \
        --root /tmp/lelab_car_rehearsal --out /tmp/rehearsal_export.json

The session id is read from the agent that is answering, not from a report file left behind by an
earlier run: a stale report used to point this script at a session the running agent had never
seen, and the 404 was the only thing that gave it away.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]

# `lelab.ros_mobile` is imported inside main(), and the environment is read there too: the guards
# below are pure, and importing the app bridge pulls in FastAPI, which the rehearsal-env test run
# does not have. A tool that cannot be imported cannot be tested, and this one is the last link of
# the chain.
def _bridge():
    sys.path.insert(0, str(REPO / "app_ros_mobile"))
    os.environ.setdefault("LELAB_ROS_AGENT_URL", "http://127.0.0.1:18115")
    os.environ.setdefault("LELAB_ROS_COLLECTION_ROOT", str(Path("/tmp/lelab_car_rehearsal/upper")))
    from lelab import ros_mobile
    return ros_mobile


def ready_session(sessions: list[dict], marker: dict, root: Path, agent_url: str) -> str:
    """Pick the one session this agent finalised, and refuse to guess when it cannot be proven.

    A rehearsal root can hold a half-written session, and the driver writes its report wherever the
    operator pointed `--out`, so neither is evidence of what the agent is actually serving. The
    listing is: exactly one `raw_ready` session, under the car root the marker claims to own.
    """
    port = urlsplit(agent_url).port
    if port is None:
        raise SystemExit(f"--agent url {agent_url} has no port; the marker cannot be matched to it")
    if str(marker.get("port")) != str(port):
        raise SystemExit(f"the agent answering {agent_url} is not the one that wrote {root}: "
                         f"marker port {marker.get('port')} != url port {port}")
    if marker.get("car_root") != str(root):
        raise SystemExit(f"marker car_root {marker.get('car_root')} != --root {root}; "
                         "the session listing would describe a different car")
    ready = [s for s in sessions if s.get("status") == "raw_ready" and (Path(s.get("path", ""))
             .is_relative_to(root) if s.get("path") else False)]
    if len(ready) != 1:
        raise SystemExit("expected exactly one raw_ready session under this root, found "
                         f"{[s.get('session_id') for s in ready]}; pass --session to pick one")
    return ready[0]["session_id"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal")
    ap.add_argument("--session", default="",
                    help="session id; by default the single raw_ready session the agent reports")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    ros_mobile = _bridge()

    root = Path(args.root)
    marker = json.loads((root / "READY_FOR_DRIVER.json").read_text(encoding="utf-8"))
    sessions = ros_mobile._http("GET", "/sessions", timeout=5.0).get("sessions", [])
    session_id = args.session or ready_session(sessions, marker, root, ros_mobile.AGENT_URL)

    out: dict = {"agent_url": ros_mobile.AGENT_URL, "collection_root": str(ros_mobile.COLLECTION_ROOT),
                 "session_id": session_id,
                 "agent_marker": marker,
                 "agent_profile": json.loads((root / "hardware-teleop-v1.local.json").read_text(
                     encoding="utf-8"))["profile"]}

    out["sync"] = ros_mobile.sync_session(session_id)
    synced = ros_mobile.UPPER_SESSIONS / session_id
    out["sync"]["files_on_disk"] = sorted(str(p.relative_to(synced)) for p in synced.rglob("*")
                                          if p.is_file())[:12]

    export = ros_mobile.export_session(session_id)
    out["export_report"] = export

    from lerobot.datasets import LeRobotDataset

    dataset_dir = Path(export["dataset_root"])
    dataset = LeRobotDataset(repo_id=export["repo_id"], root=dataset_dir)
    features = [str(name) for name in dataset.meta.features]
    frame = dataset[0]
    out["readback"] = {
        "repo_id": export["repo_id"],
        "dataset_root": str(dataset_dir),
        "episodes": len(dataset.meta.episodes),
        "frames": int(dataset.meta.total_frames),
        "fps": dataset.meta.fps,
        "features": features,
        "state_dims": int(frame["observation.state"].shape[-1]),
        "action_dims": int(frame["action"].shape[-1]),
        "camera_keys": [f for f in features if f.startswith("observation.images")],
        "first_action": [round(float(v), 6) for v in frame["action"].tolist()],
        "first_state": [round(float(v), 6) for v in frame["observation.state"].tolist()],
    }
    # 八.7 asks for the SDK *and* the web read-back. The dataset produced here came out of the real
    # car code, so the page-side calls have to be run against it too - the export unit test feeds a
    # stubbed bag reader and cannot prove the chain's output is viewable.
    from lelab import dataset_viewer

    listing = dataset_viewer.list_episodes(dataset_dir)
    videos, series = {}, {}
    for entry in listing["episodes"]:
        index = entry["index"]
        videos[str(index)] = {
            camera: (str(path) if (path := dataset_viewer.video_path(dataset_dir, index, camera)) else None)
            for camera in listing["cameras"]}
        trace = dataset_viewer.episode_timeseries(dataset_dir, index)
        series[str(index)] = {"points": trace["points"],
                             "state_dims": sorted({len(v) for v in trace["series"].get("state", [])}),
                             "action_dims": sorted({len(v) for v in trace["series"].get("action", [])}),
                             "first_t": (trace["t"] or [None])[0],
                             "last_t": (trace["t"] or [None])[-1]}
    out["page_readback"] = {
        "cameras": listing["cameras"], "fps": listing["fps"], "robot_type": listing["robot_type"],
        "total_episodes": listing["total_episodes"], "total_frames": listing["total_frames"],
        "episodes": [{"index": e["index"], "length": e["length"], "duration_s": e["duration_s"],
                      "task": e["task"], "cameras": e["cameras"]} for e in listing["episodes"]],
        "video_files": videos, "series": series,
    }

    out["page_readback"]["episode_ranges"] = {str(e["index"]): e["video_ranges"] for e in listing["episodes"]}

    # The export shares one mp4 across episodes, so the page must locate each episode by timestamp,
    # not just find a file. Contiguous ranges starting at 0 are what the player's #t= fragment uses.
    ranges = [[e["video_ranges"][c] for c in e["cameras"]][0] if e["video_ranges"] else None
              for e in listing["episodes"]]
    contiguous = bool(ranges) and ranges[0] is not None and all(
        r and r.get("from_s") is not None and r.get("to_s") is not None for r in ranges) and all(
        abs(ranges[i]["from_s"] - ranges[i - 1]["to_s"]) < 1e-6 or
        listing["episodes"][i]["videos"] != listing["episodes"][i - 1]["videos"]
        for i in range(1, len(ranges)))
    out["expectations"] = {
        "schema_is_arm_gripper_6d": out["readback"]["state_dims"] == 6 and out["readback"]["action_dims"] == 6,
        "chassis_not_zero_filled": not any("base" in f or "odom" in f or "velocity" in f for f in features),
        "both_episodes_exported": out["readback"]["episodes"] == 2,
        "report_records_action_semantics": "action" in json.dumps(export, default=str),
        "success_labeling_is_manual_or_unknown": "manual_or_unknown" in json.dumps(export, default=str),
        "page_lists_both_episodes": [e["index"] for e in out["page_readback"]["episodes"]] == [0, 1],
        "page_lengths_match_the_dataset": sum(e["length"] for e in listing["episodes"]) == out["readback"]["frames"]
                                          and all(e["length"] > 0 for e in listing["episodes"]),
        "page_shows_the_wrist_camera": listing["cameras"] == ["wrist_real"],
        "page_resolves_a_video_for_every_episode_camera": all(
            path for by_camera in videos.values() for path in by_camera.values()),
        "page_series_carries_six_columns_both_sides": all(
            s["state_dims"] == [6] and s["action_dims"] == [6] and s["points"] > 0 for s in series.values()),
        "page_can_locate_each_episode_inside_the_shared_file": bool(contiguous) and all(
            abs((e["video_ranges"][e["cameras"][0]].get("to_s") or 0.0)
                - (e["video_ranges"][e["cameras"][0]].get("from_s") or 0.0)
                - e["length"] / listing["fps"]) < 0.05 for e in listing["episodes"] if e["video_ranges"]),
        "page_fps_is_the_rounded_export_rate": float(listing["fps"]) == float(export["fps"] if "fps" in export
                                                                              else out["readback"]["fps"]),
    }
    out["expectations"]["failed"] = [k for k, v in out["expectations"].items() if v is False]

    text = json.dumps(out, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if out["expectations"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
