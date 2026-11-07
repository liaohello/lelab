"""Fixtures for T4 recording/export tests.

Two kinds of fixture, both offline:

* :func:`write_bag` / :func:`build_synced_session` build a real rosbag2 **sqlite3**
  bag and the exact ``session.json`` / ``events.jsonl`` / ``manifest.json`` /
  ``READY`` layout the car produces, so the production sync verifier and the
  production exporter are exercised against real files rather than mocks.
* :class:`FakeFacade` / :class:`FakeCar` stand in for T3's lease facade and the
  car's HTTP agent in controller tests.  They never claim hardware readiness:
  ``outputs_enabled`` is always false and an unimplemented car route raises.

No test here opens a serial port, a camera, a gamepad or a service.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from lelab.hybrid_recording import CarRouteUnavailable

WIDTH, HEIGHT = 64, 48
JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
CLOCK_DOMAIN = "car_monotonic_ros"
BASE_TOPIC = "/lelab/base/odom"
STATE_TOPIC = "/joint_states"
IMAGE_TOPIC = "/lelab/wrist/rgb/image_raw"
EVENTS_TOPIC = "/lelab/events"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _uint8(data: bytes):
    """ROS ``uint8[]`` fields are serialized from a numpy view, not a tuple."""
    import numpy as np

    return np.frombuffer(data, dtype=np.uint8)


def _f64(values) -> Any:
    """ROS float arrays serialize from a numpy view."""
    import numpy as np

    return np.asarray(values, dtype=np.float64)


def _image_bytes(seed: int) -> bytes:
    """Deterministic RGB frame: a moving vertical bar so frames differ."""
    row = bytearray()
    for x in range(WIDTH):
        value = 255 if (x + seed) % 8 == 0 else 32
        row += bytes((value, (x * 3) % 256, seed % 256))
    return bytes(row) * HEIGHT


def write_bag(bag_dir: Path, frames: list[dict[str, Any]]) -> None:
    """Write a rosbag2 sqlite3 bag with image, joints, base estimate and commands.

    ``frames`` items: ``time_ns``, ``joints`` (six floats), ``base`` (vx, vy, wz,
    covariance triple), ``events`` (list of command event dicts to emit at that
    timestamp).
    """
    from rosbags.rosbag2 import Writer
    from rosbags.typesys import Stores, get_typestore

    typestore = get_typestore(Stores.LATEST)
    Image = typestore.types["sensor_msgs/msg/Image"]
    JointState = typestore.types["sensor_msgs/msg/JointState"]
    Odometry = typestore.types["nav_msgs/msg/Odometry"]
    String = typestore.types["std_msgs/msg/String"]
    Header = typestore.types["std_msgs/msg/Header"]
    Time = typestore.types["builtin_interfaces/msg/Time"]
    Pose = typestore.types["geometry_msgs/msg/Pose"]
    PoseWithCovariance = typestore.types["geometry_msgs/msg/PoseWithCovariance"]
    Twist = typestore.types["geometry_msgs/msg/Twist"]
    TwistWithCovariance = typestore.types["geometry_msgs/msg/TwistWithCovariance"]
    Point = typestore.types["geometry_msgs/msg/Point"]
    Quaternion = typestore.types["geometry_msgs/msg/Quaternion"]
    Vector3 = typestore.types["geometry_msgs/msg/Vector3"]

    def header(stamp_ns: int, frame_id: str = ""):
        return Header(stamp=Time(sec=stamp_ns // 1_000_000_000,
                                 nanosec=stamp_ns % 1_000_000_000),
                      frame_id=frame_id)

    bag_dir.parent.mkdir(parents=True, exist_ok=True)
    if bag_dir.exists():
        raise RuntimeError(f"{bag_dir} already exists; rosbags Writer refuses to overwrite")
    with Writer(bag_dir, version=8) as writer:
        image_conn = writer.add_connection(IMAGE_TOPIC, Image.__msgtype__, typestore=typestore)
        joint_conn = writer.add_connection(STATE_TOPIC, JointState.__msgtype__, typestore=typestore)
        base_conn = writer.add_connection(BASE_TOPIC, Odometry.__msgtype__, typestore=typestore)
        event_conn = writer.add_connection(EVENTS_TOPIC, String.__msgtype__, typestore=typestore)
        for index, frame in enumerate(frames):
            t = int(frame["time_ns"])
            data = _image_bytes(index)
            writer.write(image_conn, t, typestore.serialize_cdr(Image(
                header=header(t, "wrist"), height=HEIGHT, width=WIDTH, encoding="rgb8",
                is_bigendian=0, step=WIDTH * 3, data=_uint8(data)),
                Image.__msgtype__))
            writer.write(joint_conn, t, typestore.serialize_cdr(JointState(
                header=header(t), name=list(JOINTS),
                position=_f64(frame["joints"]), velocity=_f64([]), effort=_f64([])),
                JointState.__msgtype__))
            vx, vy, wz, cov = frame["base"]
            covariance = [0.0] * 36
            covariance[0], covariance[7], covariance[35] = float(cov[0]), float(cov[1]), float(cov[2])
            writer.write(base_conn, t, typestore.serialize_cdr(Odometry(
                header=header(t, "odom"), child_frame_id="base_link",
                pose=PoseWithCovariance(
                    pose=Pose(position=Point(x=0.0, y=0.0, z=0.0),
                              orientation=Quaternion(x=0.0, y=0.0, z=0.0, w=1.0)),
                    covariance=_f64([0.0] * 36)),
                twist=TwistWithCovariance(
                    twist=Twist(linear=Vector3(x=float(vx), y=float(vy), z=0.0),
                                angular=Vector3(x=0.0, y=0.0, z=float(wz))),
                    covariance=_f64(covariance))),
                Odometry.__msgtype__))
            for event in frame.get("events") or []:
                payload = json.dumps({**event, "time_ns": t, "clock_domain": CLOCK_DOMAIN})
                writer.write(event_conn, t, typestore.serialize_cdr(
                    String(data=payload), String.__msgtype__))


def make_frames(start_ns: int, end_ns: int, *, fps: int = 10,
                commands: bool = True, **command_kwargs) -> list[dict[str, Any]]:
    """Frames on a uniform grid, one command batch per channel at each frame."""
    step = int(1e9 / fps)
    base_cov = command_kwargs.get("base_cov", (1e-4, 1e-4, 2e-4))
    frames: list[dict[str, Any]] = []
    t = start_ns
    index = 0
    while t <= end_ns:
        frames.append({
            "time_ns": t,
            "joints": [0.10 * index, -0.20, 0.30, -0.40, 0.05 * index, 0.25],
            "base": (0.05, 0.0, 0.02, base_cov),
            "events": [],
        })
        index += 1
        t += step
    if not commands:
        return frames
    base_source = command_kwargs.get("base_source", "gamepad")
    arm_source = command_kwargs.get("arm_source", "so101_leader")
    base_ttl = command_kwargs.get("base_ttl_ms", 200)
    arm_ttl = command_kwargs.get("arm_ttl_ms", 500)
    gripper_ttl = command_kwargs.get("gripper_ttl_ms", 500)
    skip_channel = command_kwargs.get("skip_channel")
    # A sparse stream is how a real stale command looks: the hold runs out.
    interval = int(command_kwargs.get("command_interval_frames", 1) or 1)
    for seq, frame in enumerate(frames, start=1):
        if interval > 1 and (seq - 1) % interval:
            frame["events"] = []
            continue
        batch: list[dict[str, Any]] = []
        if skip_channel != "base":
            base = {"kind": "command", "component": "base", "source": base_source,
                    "seq": seq, "target": [0.05, 0.0, 0.02]}
            if base_ttl is not None:
                base["ttl_ms"] = base_ttl
            batch.append(base)
        if skip_channel != "arm":
            arm = {"kind": "command", "component": "arm", "source": arm_source,
                   "seq": seq, "target": [0.10, -0.20, 0.30, -0.40, 0.50]}
            if arm_ttl is not None:
                arm["ttl_ms"] = arm_ttl
            batch.append(arm)
        if skip_channel != "gripper":
            gripper = {"kind": "command", "component": "gripper", "source": arm_source,
                       "seq": seq, "target": [0.25, 0.25, 0.25, 0.25, 0.25]}
            if gripper_ttl is not None:
                gripper["ttl_ms"] = gripper_ttl
            batch.append(gripper)
        frame["events"] = batch
    return frames


def hybrid_metadata(*, provenance: dict | None = None, schema: str = "mobile_hybrid_manual_9d_v1",
                    runtime_mode: str = "hardware_hybrid_manual", training_export: bool = True,
                    fps: float = 10.0) -> dict[str, Any]:
    meta = {
        "source": "hardware",
        "runtime_mode": runtime_mode,
        "training_export": training_export,
        "export_schema": schema,
        "clock_domain": CLOCK_DOMAIN,
        "camera_topic": IMAGE_TOPIC,
        "joint_state_topic": STATE_TOPIC,
        "record_joint_state_topic": STATE_TOPIC,
        "base_velocity_topic": BASE_TOPIC,
        "camera_profile": "wrist_real",
        "features": {
            "fps": fps,
            "image": {"name": "observation.images.wrist_real", "topic": IMAGE_TOPIC},
            "state": {"names": list(JOINTS), "units": ["rad"] * 6},
            "action": {"names": list(JOINTS), "source": "gateway_sent_target_after_clamp"},
        },
        "feedback_provenance": provenance if provenance is not None else {
            "joint_source_kind": "servo_position_readback",
            "base_source_kind": "wheel_odometry_estimate",
            "calibration_hash": "sha256:abc123",
            "base_estimate_has_covariance": True,
            "max_state_age_s": 0.2,
            "joint_topic": STATE_TOPIC,
            "base_topic": BASE_TOPIC,
            "scan_span_ms": 1.5,
        },
    }
    return meta


def build_synced_session(session_root: Path, session_id: str, *, episodes: int = 2,
                         fps: int = 10, seconds: float = 1.5, meta: dict | None = None,
                         write_ready: bool = True, manifest_overrides: dict | None = None,
                         break_phase_events: bool = False,
                         frame_kwargs: dict | None = None,
                         phases_for_episode=None) -> dict[str, Any]:
    """Create the exact on-disk layout the upper host expects after a pull.

    Returns the manifest plus the phase boundaries used, so a test can assert the
    exported slices against the same numbers.
    """
    session_dir = session_root / session_id
    (session_dir / "episodes").mkdir(parents=True, exist_ok=True)
    bag_dir = session_dir / "raw" / "bag"
    meta = meta or hybrid_metadata()

    frames: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    episode_records: list[dict[str, Any]] = []
    phase_plan: list[dict[str, Any]] = []
    t = 1_000_000_000
    step = int(1e9 / fps)
    span = int(seconds * 1e9)
    for index in range(episodes):
        start = t
        observe_start = start
        grasp_start = start + span // 2
        end = start + span
        frames.extend(make_frames(start, end, fps=fps, **(frame_kwargs or {})))
        phases = (phases_for_episode(start, end, step) if phases_for_episode else [
            {"phase": "observe", "started_time_ns": observe_start, "ended_time_ns": grasp_start},
            {"phase": "grasp", "started_time_ns": grasp_start, "ended_time_ns": end},
        ])
        episode_id = f"ep_{index:04d}"
        for phase in phases:
            events.append({
                "kind": "phase_started", "event_id": f"{episode_id}-{phase['phase']}-s",
                "session_id": session_id, "episode_id": episode_id,
                "time_ns": phase["started_time_ns"], "clock_domain": CLOCK_DOMAIN,
                "payload": {"phase": phase["phase"]},
            })
            events.append({
                "kind": "phase_ended", "event_id": f"{episode_id}-{phase['phase']}-e",
                "session_id": session_id, "episode_id": episode_id,
                "time_ns": phase["ended_time_ns"], "clock_domain": CLOCK_DOMAIN,
                "payload": {"phase": phase["phase"]},
            })
        if break_phase_events:
            events = [e for e in events if e["kind"] != "phase_ended" or
                      e["episode_id"] != episode_id]
        episode_records.append({
            "episode_id": episode_id,
            "task": "Pick the cube and drop it in the container.",
            "source": "manual_hybrid",
            "status": "saved",
            "started_time_ns": start,
            "ended_time_ns": end,
            "started_wall": 1.0,
            "end_wall": 2.0,
            "initial": {"task": "pick"},
            "evaluation": {"success": "unknown" if index % 2 == 0 else "success",
                           "labels": "manual_only"},
            "phases": phases,
        })
        phase_plan.append({"episode_id": episode_id, "start_time_ns": start,
                           "end_time_ns": end, "phases": phases})
        t = end + int(0.5e9)

    write_bag(bag_dir, frames)

    (session_dir / "session.json").write_text(json.dumps({
        "schema_version": 1, "session_id": session_id, "task": "hybrid manual",
        "status": "recording", "started_wall": 1.0, "started_time_ns": 1_000_000_000,
        "clock_domain": CLOCK_DOMAIN,
        "topics": [IMAGE_TOPIC, STATE_TOPIC, BASE_TOPIC, EVENTS_TOPIC],
        "required_topics": [IMAGE_TOPIC, STATE_TOPIC, BASE_TOPIC],
        "metadata": meta,
    }, indent=2), encoding="utf-8")
    with (session_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in [{"kind": "session_start", "session_id": session_id,
                       "time_ns": 1_000_000_000, "clock_domain": CLOCK_DOMAIN,
                       "payload": {}}] + events:
            handle.write(json.dumps(event) + "\n")
    for record in episode_records:
        (session_dir / "episodes" / f"{record['episode_id']}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")

    files = []
    for path in sorted(session_dir.rglob("*")):
        if path.is_file() and path.name not in {"manifest.json", "READY"}:
            files.append({"path": str(path.relative_to(session_dir)),
                          "size": path.stat().st_size, "sha256": _sha256(path)})
    manifest = {
        "schema_version": 1,
        "session_id": session_id,
        "profile": "hardware_hybrid_manual_v1",
        "status": "raw_ready",
        "ready_eligible": True,
        "error": None,
        "bag_exit_code": 0,
        "bag_readable": True,
        "clock_domain": CLOCK_DOMAIN,
        "required_topics": [IMAGE_TOPIC, STATE_TOPIC, BASE_TOPIC],
        "metadata": meta,
        "episodes": episode_records,
        "files": files,
        "task": "hybrid manual",
    }
    manifest.update(manifest_overrides or {})
    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if write_ready:
        (session_dir / "READY").write_text(json.dumps({
            "manifest_sha256": _sha256(manifest_path),
            "session_id": session_id,
            "files": len(files),
        }), encoding="utf-8")
    return {"session_dir": session_dir, "manifest": manifest, "phases": phase_plan,
            "meta": meta, "fps": fps}


def package_session(session_dir: Path) -> tuple[dict[str, Any], bytes]:
    """Tar a session directory the way the car serves it, plus its manifest."""
    import io
    import tarfile

    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for path in sorted(session_dir.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(session_dir)))
    return manifest, archive.getvalue()


# ---------------------------------------------------------------------------
# Controller fixtures
# ---------------------------------------------------------------------------
class FakeFacade:
    """Stand-in for T3's ``HybridSession`` lease facade (offline, no hardware)."""

    def __init__(self, *, ready: bool = True, pause_fails: bool = False):
        self.ready = ready
        self.pause_fails = pause_fails
        self.session_id = "sess-fixture" if ready else None
        self.session_epoch = 4242 if ready else None
        self.calls: list[tuple[str, Any]] = []
        self.estop_latched = False
        self.base_enabled = False

    def status(self) -> dict[str, Any]:
        return {
            "state": "ready" if self.ready and not self.estop_latched else
                     ("estop" if self.estop_latched else "idle"),
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "calibrations": {"bundle_hash": "sha256:bundle", "ready": self.ready},
            "outputs_enabled": False,
            "last_error": None,
        }

    def set_arm_state(self, event: Any) -> dict[str, Any]:
        action = event if isinstance(event, str) else str((event or {}).get("action"))
        self.calls.append(("set_arm_state", action))
        if action == "estop":
            self.estop_latched = True
            return {"state": "estop", "outputs_enabled": False}
        if action == "pause_all" and self.pause_fails:
            return {"state": "fault", "last_error": "stop_failed:fixture"}
        if action == "pause_all":
            self.base_enabled = False
        return {"state": "ready", "outputs_enabled": False}

    def recording(self, action: str, payload: dict) -> dict[str, Any]:
        self.calls.append(("recording", action))
        return {"ok": True, "action": action}


class FakeCar:
    """In-memory car agent.  Records every route call; unknown routes 404."""

    def __init__(self, *, clock_domain: str | None = CLOCK_DOMAIN,
                 route_phase: bool = True, fail_end_episode: bool = False):
        self.clock_domain = clock_domain
        self.route_phase = route_phase
        self.fail_end_episode = fail_end_episode
        self.calls: list[tuple[str, dict]] = []
        self.time_ns = 5_000_000_000
        self.phase_events: list[dict[str, Any]] = []
        self.episode_open: str | None = None
        self.session_open = False
        self.stopped_after: list[str] = []

    def _tick(self, amount: int = 250_000_000) -> int:
        self.time_ns += amount
        return self.time_ns

    def post(self, path: str, body: dict) -> dict:
        self.calls.append((path, dict(body)))
        if path == "/recording/start_session":
            if self.session_open:
                raise RuntimeError("a recording session is already active")
            self.session_open = True
            return {"session_id": "car-session", "path": "/tmp/car/sess",
                    "topics": [IMAGE_TOPIC, STATE_TOPIC, BASE_TOPIC],
                    "clock_domain": self.clock_domain}
        if path == "/recording/start_episode":
            if not self.session_open:
                raise RuntimeError("no active recording session")
            self.episode_open = body.get("episode_id") or "car-ep"
            return {"episode_id": self.episode_open, "source": body.get("source"),
                    "initial": {"time_ns": self._tick(), "clock_domain": self.clock_domain}}
        if path == "/recording/set_phase":
            if not self.route_phase:
                raise CarRouteUnavailable(path)
            if self.episode_open is None:
                raise RuntimeError("no active episode")
            t = self._tick()
            self.phase_events.append({"phase": body.get("phase"),
                                      "episode_id": self.episode_open, "time_ns": t})
            return {"kind": "phase_started", "time_ns": t,
                    "payload": {"phase": body.get("phase")}}
        if path == "/recording/end_episode":
            if self.fail_end_episode:
                raise RuntimeError("car refused end_episode")
            t = self._tick()
            self.episode_open = None
            return {"episode_id": body.get("episode_id"), "event": {"time_ns": t},
                    "evaluation": {"success": body.get("success", "unknown")}}
        if path == "/recording/stop_session":
            self.session_open = False
            return {"stopped": True, "time_ns": self._tick()}
        raise CarRouteUnavailable(path)
