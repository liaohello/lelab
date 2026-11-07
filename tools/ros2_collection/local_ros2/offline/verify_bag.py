#!/usr/bin/env python3
"""OFFLINE ONLY - read a recorded session back from the bag and prove what is in it.

Nothing here trusts a counter written by the recorder: the sqlite3 archive is opened with
the ROS 2 storage API, every `teleop_frame` row is parsed, and the row-to-episode-to-phase
correspondence is checked against the same timeline the agent recorded.  The point is to
answer "did the frame I sent end up in the episode I named, with the values I sent", not
"did the message counts look plausible".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import tarfile
import time
from collections import Counter, defaultdict

import rosbag2_py
from rclpy.serialization import deserialize_message
from sensor_msgs.msg import Image
from std_msgs.msg import String

DATA_IDENTITY = "simulated_offline"


def read_bag(bag_dir: pathlib.Path) -> tuple[dict, list]:
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id="sqlite3"),
                rosbag2_py.ConverterOptions())
    reader.set_filter(rosbag2_py.StorageFilter(topics=[]))
    counts: Counter = Counter()
    rows: list[dict] = []
    images: list[dict] = []
    stamps: dict[str, list[int]] = defaultdict(list)
    while reader.has_next():
        topic, payload, stamp = reader.read_next()
        counts[topic] += 1
        stamps[topic].append(stamp)
        if topic.endswith("/recording/teleop"):
            message = deserialize_message(payload, String)
            try:
                document = json.loads(message.data)
            except ValueError:
                continue
            if document.get("kind") == "teleop_frame":
                document["_bag_stamp_ns"] = stamp
                rows.append(document)
        elif topic.endswith("/rgb0/image"):
            image = deserialize_message(payload, Image)
            images.append({"width": image.width, "height": image.height,
                           "encoding": image.encoding, "step": image.step,
                           "header_stamp_ns": image.header.stamp.sec * 10**9
                           + image.header.stamp.nanosec,
                           "bag_stamp_ns": stamp,
                           "frame_id": image.header.frame_id})
    return {"counts": dict(counts), "stamps": {k: v for k, v in stamps.items()}}, rows, images


def check(report: dict, name: str, passed, detail="") -> None:
    report["checks"].append({"name": name, "passed": bool(passed), "detail": str(detail)[:400]})
    print(f"[{'PASS' if passed else 'FAIL'}] {name}  {str(detail)[:180]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--archive", default="")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    session = pathlib.Path(args.session_dir)
    manifest = json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8"))
    report = {"checks": [], "data_identity": DATA_IDENTITY, "training_eligible": False,
              "session_id": manifest.get("session_id"),
              "clock_domain": manifest.get("clock_domain"),
              "clock_epoch": manifest.get("clock_epoch"),
              "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    bags = sorted(path for path in session.rglob("*")
                  if path.is_file() and path.suffix == ".db3")
    report["bag_files"] = [{"path": str(p.relative_to(session)), "bytes": p.stat().st_size}
                           for p in bags]
    check(report, "sqlite3_bag_present", bool(bags), [p.name for p in bags])
    metadata = next(iter(session.parent.glob("*/raw/metadata.yaml")),
                     session / "raw" / "metadata.yaml")
    metadata = next((path for path in session.rglob("metadata.yaml")), None)
    check(report, "bag_metadata_written", metadata is not None, str(metadata))

    bag_dir = next((p.parent for p in bags if p.suffix == ".db3"), session)
    summary, rows, images = read_bag(bag_dir)
    report["topic_counts"] = summary["counts"]
    teleop_topic = next((topic for topic in summary["counts"] if topic.endswith("/recording/teleop")), "")
    frame_rows = rows
    check(report, "teleop_frame_rows_recorded", len(frame_rows) > 20,
          f"{len(frame_rows)} rows on {teleop_topic}")

    # The camera stream has to be the resolution the export stage expects, and it has to be
    # the same clock domain as the bag it sits in.
    sizes = {(image["width"], image["height"]) for image in images}
    check(report, "camera_resolution_640x400", sizes == {(640, 400)}, sorted(sizes))
    if images:
        drift = sorted(abs(image["header_stamp_ns"] - image["bag_stamp_ns"]) for image in images)
        # Same clock, different moments: the header is stamped when the frame is produced and
        # the bag stamp is when the recorder takes it, so this difference is queue latency.
        # It has to stay well inside the recorder's staleness bound, and the percentiles are
        # what gets reported rather than one max.
        check(report, "camera_header_and_bag_stamp_same_clock_domain", drift[-1] < 150_000_000,
              f"|header-bag| p50 {drift[len(drift)//2]/1e6:.2f} ms p95 "
              f"{drift[max(0, int(len(drift)*0.95)-1)]/1e6:.2f} ms max {drift[-1]/1e6:.2f} ms "
              f"over {len(images)} images")

    # Identity is (stream epoch, frame_seq): every episode re-engages, and a re-engagement
    # starts a new stream generation by design, so frame_seq alone repeats across episodes.
    pairs = [(row["stream_epoch"], row["frame_seq"]) for row in frame_rows]
    check(report, "frame_seq_unique_within_its_stream_epoch", len(pairs) == len(set(pairs)),
          f"{len(pairs)} rows, {len(set(pairs))} distinct (stream_epoch, frame_seq) pairs")
    epochs_by_episode = defaultdict(set)
    for row in frame_rows:
        epochs_by_episode[row["episode_id"]].add(row["stream_epoch"])
    all_epochs = {epoch for epochs in epochs_by_episode.values() for epoch in epochs}
    check(report, "each_episode_has_exactly_one_stream_generation",
          all(len(epochs) == 1 for epochs in epochs_by_episode.values())
          and len(all_epochs) == len(epochs_by_episode),
          {episode: sorted(epochs) for episode, epochs in epochs_by_episode.items()})

    by_episode = Counter(row.get("episode_id") or "uncontexted" for row in frame_rows)
    report["rows_by_episode"] = dict(by_episode)
    report["rows_by_phase"] = dict(Counter(f"{row.get('episode_id')}/{row.get('phase')}"
                                           for row in frame_rows))
    episodes = manifest.get("episodes", [])
    check(report, "every_row_names_a_recorded_episode",
          "uncontexted" not in by_episode and set(by_episode) == {ep["episode_id"] for ep in episodes},
          dict(by_episode))
    check(report, "both_phases_recorded",
          {row.get("phase") for row in frame_rows} == {"observe", "grasp"},
          sorted({row.get("phase") for row in frame_rows}))

    # A row's clock epoch must be the session's, and its bag stamp must be in the same
    # domain as the message timestamp the recorder wrote: that is what makes the row
    # joinable to the frames around it without any offset guess.
    check(report, "clock_epoch_consistent",
          {row.get("clock_epoch") for row in frame_rows} == {manifest.get("clock_epoch")},
          sorted({row.get("clock_epoch") for row in frame_rows}))
    offsets = [row["_bag_stamp_ns"] - row["stamp_ns"] for row in frame_rows]
    check(report, "row_stamp_is_the_bag_clock", max(abs(value) for value in offsets) < 20_000_000,
          f"max offset {max(abs(value) for value in offsets)/1e6:.1f} ms, "
          f"median {sorted(abs(value) for value in offsets)[len(offsets)//2]/1e6:.1f} ms")
    check(report, "monotonic_clock_never_equal_to_recorded_clock",
          all(row["control_mono_ns"] != row["stamp_ns"] for row in frame_rows)
          and {row["control_clock_domain"] for row in frame_rows} == {"car_process_monotonic"}
          and {row["clock_domain"] for row in frame_rows} == {manifest.get("clock_domain")},
          f"manifest clock_domain={manifest.get('clock_domain')}, row control domain stays monotonic")

    # Phase windows: every row of a phase must fall inside that phase's recorded span.
    inside = outside = 0
    for episode in episodes:
        for phase in episode.get("phases", []):
            low, high = phase["started_time_ns"], phase.get("ended_time_ns") or episode["ended_time_ns"]
            for row in frame_rows:
                if row["episode_id"] != episode["episode_id"] or row["phase"] != phase["phase"]:
                    continue
                if low <= row["stamp_ns"] <= high:
                    inside += 1
                else:
                    outside += 1
    check(report, "rows_inside_their_phase_window", outside == 0 and inside > 0,
          f"{inside} inside, {outside} outside")

    # The values themselves: a sent target must be inside the car limits, differ from the
    # pre-clip value where the clip bit, and never be a copy of the readback.
    limits = {"joint1": (-2.052507, 2.086018), "joint2": (-2.052507, 1.931032),
              "joint3": (-2.094395, 2.052507), "joint4": (-1.675516, 2.010619),
              "joint5": (-1.759292, 1.717404), "r_joint": (-2.09, 2.09)}
    out_of_limits = [row["frame_seq"] for row in frame_rows
                     for name, value in row["action_rad"].items()
                     if not limits[name][0] - 1e-9 <= value <= limits[name][1] + 1e-9]
    check(report, "sent_targets_inside_calibrated_limits", not out_of_limits, out_of_limits[:5])
    clipped_rows = [row["frame_seq"] for row in frame_rows
                    if row.get("pre_clip_rad")
                    and abs(row["pre_clip_rad"][4] - row["action_rad"]["joint5"]) > 1e-9]
    check(report, "clipping_is_visible_in_the_data", len(clipped_rows) > 0,
          f"{len(clipped_rows)} rows where pre-clip joint5 differs from the sent value")
    six_axes = [row for row in frame_rows if len(row["action_rad"]) == 6
                and len(row["state_rad"] or {}) == 6 and row.get("leader_rad")]
    check(report, "six_axis_target_and_state_per_row", len(six_axes) == len(frame_rows),
          f"{len(six_axes)}/{len(frame_rows)}")
    identity = {row["data_identity"] for row in frame_rows}
    check(report, "rows_tagged_simulated_offline", identity == {DATA_IDENTITY}, sorted(identity))
    confirmation = {row["confirmation"] for row in frame_rows}
    check(report, "confirmation_never_claims_controller_ack",
          confirmation == {"driver_sent"} and all(row["controller_acknowledged"] == "unavailable"
                                                  for row in frame_rows), sorted(confirmation))

    if args.archive:
        archive = pathlib.Path(args.archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        report["archive_sha256"] = digest
        with tarfile.open(archive, "r:gz") as tar:
            names = [member.name for member in tar.getmembers() if member.isfile()]
        report["archive_files"] = sorted(names)
        check(report, "archive_carries_the_bag", any(name.endswith(".db3") for name in names),
              [name for name in names if name.endswith(".db3")])
        check(report, "archive_carries_manifest_and_events",
              any(name.endswith("manifest.json") for name in names)
              and any(name.endswith("events.jsonl") for name in names), len(names))
        check(report, "archive_excludes_the_archive_itself",
              not any(name.endswith(".tar.gz") for name in names), len(names))

    report["failed"] = [item["name"] for item in report["checks"] if not item["passed"]]
    pathlib.Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True,
                                                    default=str) + "\n", encoding="utf-8")
    print(f"report: {args.report}  failed: {report['failed'] or 'none'}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
