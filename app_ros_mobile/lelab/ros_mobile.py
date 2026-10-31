"""Upper-host bridge for the LeLab ROS mobile manipulator collection mode.

The car-side agent is an independent process with its own storage and control
token.  This module owns the upper-host session protocol: it acquires the
existing LeLab hardware lease, proxies control, pulls raw sessions through
HTTP with hash verification, and exports accepted episodes through the local
LeRobot SDK.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .dataset_storage import lerobot_dataset_root
from .hardware_lease import HardwareLeaseError, get_hardware_lease_manager

AGENT_URL = os.environ.get("LELAB_ROS_AGENT_URL", "http://127.0.0.1:8110").rstrip("/")
COLLECTION_ROOT = Path(
    os.environ.get("LELAB_ROS_COLLECTION_ROOT", str(Path.home() / ".cache" / "lelab" / "ros2_collection"))
).expanduser()
UPPER_SESSIONS = COLLECTION_ROOT / "upper" / "sessions"
UPPER_EXPORTS = COLLECTION_ROOT / "upper" / "exports"
for directory in (UPPER_SESSIONS, UPPER_EXPORTS):
    directory.mkdir(parents=True, exist_ok=True)

LEASE_TASK = "recording"
LEASE_TTL = 60.0
AGENT_TTL = 30.0


class BridgeError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _http(method: str, path: str, payload: dict | None = None, token: str | None = None, timeout: float = 30.0) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(AGENT_URL + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-LeLab-Control-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
            detail = body.get("detail") or body.get("error") or str(exc)
        except Exception:
            detail = str(exc)
        raise BridgeError(exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise BridgeError(503, f"car agent unavailable: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RosMobileBridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.owner: str | None = None
        self.lease_token: str | None = None
        self.agent_token: str | None = None
        self.seq = -1
        self.estop = False
        self.last_status: dict[str, Any] = {}
        self.last_status_wall = 0.0

    def _lease(self):
        return get_hardware_lease_manager()

    def acquire(self, owner: str) -> dict:
        with self.lock:
            if self.agent_token:
                _http("POST", "/control/renew", {}, self.agent_token)
                return {"owner": self.owner, "mode": self.last_status.get("control", {}).get("mode", "idle"), "reused": True}
            lease = None
            try:
                lease = self._lease().acquire(LEASE_TASK, owner=owner, role="operator", ttl_seconds=LEASE_TTL)
                result = _http("POST", "/control/acquire", {"client_id": owner, "ttl_s": AGENT_TTL})
            except HardwareLeaseError as exc:
                raise BridgeError(exc.status_code, str(exc)) from exc
            except Exception:
                if lease is not None:
                    self._lease().release(token=lease.token, reason="agent-acquire-failed")
                raise
            self.owner = owner
            self.lease_token = lease.token
            self.agent_token = result["token"]
            self.seq = -1
            self.estop = False
            return {"owner": owner, "mode": result.get("mode", "idle"), "reused": False}

    def heartbeat(self) -> dict:
        with self.lock:
            if not self.agent_token:
                raise BridgeError(409, "ROS mobile control is not acquired")
            _http("POST", "/control/renew", {}, self.agent_token)
            self._lease().renew(token=self.lease_token, ttl_seconds=LEASE_TTL)
            return {"ok": True, "expires_in_s": AGENT_TTL}

    def release(self, reason: str = "released") -> dict:
        with self.lock:
            out: dict[str, Any] = {"ok": True}
            if self.agent_token:
                try:
                    out = _http("POST", "/control/release", {}, self.agent_token)
                except BridgeError as exc:
                    out = {"ok": False, "detail": exc.detail}
            if self.lease_token:
                try:
                    self._lease().release(token=self.lease_token, reason=reason)
                except Exception:
                    pass
            self.agent_token = None
            self.lease_token = None
            self.owner = None
            self.seq = -1
            self.estop = False
            return out

    def emergency_stop(self, reason: str = "emergency-stop") -> dict:
        with self.lock:
            self.estop = True
            try:
                out = _http("POST", "/control/estop", {"reason": reason}, self.agent_token or "")
            except BridgeError as exc:
                out = {"estop": False, "error": exc.detail}
            return out

    def clear_estop(self) -> dict:
        with self.lock:
            if not self.agent_token:
                raise BridgeError(409, "ROS mobile control is not acquired")
            out = _http("POST", "/control/clear_estop", {}, self.agent_token)
            self.estop = False
            return out

    def is_active(self) -> bool:
        with self.lock:
            return self.agent_token is not None

    def agent_token_required(self) -> str:
        with self.lock:
            if not self.agent_token:
                raise BridgeError(409, "ROS mobile control is not acquired")
            return self.agent_token

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def status(self) -> dict:
        with self.lock:
            token = self.agent_token
            owner = self.owner
        status = _http("GET", "/status", timeout=5.0)
        with self.lock:
            self.last_status = status
            self.last_status_wall = time.time()
        return {
            "agent": status,
            "bridge": {
                "owner": owner,
                "control_acquired": token is not None,
                "estop": self.estop,
                "upper_root": str(COLLECTION_ROOT),
                "agent_url": AGENT_URL,
                "lease": self._lease().status(),
            },
            "sync": {"sessions": list_upper_sessions()},
        }


_BRIDGE: RosMobileBridge | None = None


def get_bridge() -> RosMobileBridge:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = RosMobileBridge()
    return _BRIDGE


def _owner(request: Request) -> str:
    return (request.headers.get("x-lelab-client") or "").strip() or (
        request.client.host if request.client is not None else "browser"
    )


def _guard(call):
    try:
        return call()
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except HardwareLeaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


router = APIRouter(prefix="/ros-mobile", tags=["ros-mobile"])


@router.get("/health")
def health():
    return _guard(lambda: {"ok": True, "agent": _http("GET", "/health", timeout=3.0)})


@router.get("/status")
def status():
    return _guard(lambda: get_bridge().status())


@router.get("/preview.mjpg")
def preview():
    def stream() -> Iterator[bytes]:
        request = urllib.request.Request(AGENT_URL + "/preview.mjpg")
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"preview unavailable: {exc}") from exc
        try:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            response.close()

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame")


@router.get("/preview.jpg")
def preview_jpg():
    try:
        with urllib.request.urlopen(AGENT_URL + "/preview.jpg", timeout=4) as response:
            data = response.read()
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"preview unavailable: {exc}") from exc


@router.post("/control/acquire")
def control_acquire(request: Request):
    return _guard(lambda: get_bridge().acquire(_owner(request)))


@router.post("/control/heartbeat")
def control_heartbeat():
    return _guard(lambda: get_bridge().heartbeat())


@router.post("/control/release")
def control_release():
    return _guard(lambda: get_bridge().release())


@router.post("/control/mode")
def control_mode(body: dict):
    bridge = get_bridge()
    token = _guard(bridge.agent_token_required)
    return _guard(lambda: _http("POST", "/control/mode", {"mode": body.get("mode", "idle")}, token))


@router.post("/control/base")
def control_base(body: dict):
    bridge = get_bridge()
    token = _guard(bridge.agent_token_required)
    status = _guard(lambda: _http("GET", "/status", timeout=5.0))
    mode = status.get("control", {}).get("mode", "idle")
    if mode == "idle":
        _guard(lambda: _http("POST", "/control/mode", {"mode": "keyboard"}, token))
        mode = "keyboard"
    if mode not in ("keyboard", "scripted_expert"):
        raise HTTPException(status_code=409, detail=f"base control unavailable in mode {mode}")
    with bridge.lock:
        bridge.seq = max(bridge.seq, int(status.get("last_action", {}).get("seq", -1)))
    payload = {
        "session_id": token,
        "seq": bridge.next_seq(),
        "source": mode,
        "mode": mode,
        "base": {
            "vx": float(body.get("vx", 0.0)),
            "vy": float(body.get("vy", 0.0)),
            "wz": float(body.get("wz", 0.0)),
        },
    }
    return _guard(lambda: _http("POST", "/control/base", payload, token))


@router.post("/control/arm")
def control_arm(body: dict):
    bridge = get_bridge()
    token = _guard(bridge.agent_token_required)
    positions = body.get("positions")
    if not isinstance(positions, list) or len(positions) != 5:
        raise HTTPException(status_code=400, detail="positions must contain five joint targets")
    _guard(lambda: _http("POST", "/control/mode", {"mode": "scripted_expert"}, token))
    status = _guard(lambda: _http("GET", "/status", timeout=5.0))
    with bridge.lock:
        bridge.seq = max(bridge.seq, int(status.get("last_action", {}).get("seq", -1)))
    payload = {
        "session_id": token, "seq": bridge.next_seq(), "source": "scripted_expert", "mode": "scripted_expert",
        "arm": {"positions": [float(v) for v in positions], "duration_s": float(body.get("duration_s", 1.5))},
    }
    return _guard(lambda: _http("POST", "/control/arm", payload, token))


@router.post("/control/gripper")
def control_gripper(body: dict):
    bridge = get_bridge()
    token = _guard(bridge.agent_token_required)
    _guard(lambda: _http("POST", "/control/mode", {"mode": "scripted_expert"}, token))
    status = _guard(lambda: _http("GET", "/status", timeout=5.0))
    with bridge.lock:
        bridge.seq = max(bridge.seq, int(status.get("last_action", {}).get("seq", -1)))
    payload = {
        "session_id": token, "seq": bridge.next_seq(), "source": "scripted_expert", "mode": "scripted_expert",
        "gripper": {"position": float(body.get("position", 0.0)), "duration_s": float(body.get("duration_s", 1.0))},
    }
    return _guard(lambda: _http("POST", "/control/gripper", payload, token))


@router.post("/control/estop")
def control_estop(body: dict | None = None):
    return _guard(lambda: get_bridge().emergency_stop((body or {}).get("reason", "web-estop")))


@router.post("/control/clear-estop")
def control_clear_estop():
    return _guard(lambda: get_bridge().clear_estop())


def _agent_post(path: str, body: dict | None = None) -> dict:
    bridge = get_bridge()
    token = bridge.agent_token_required()
    return _http("POST", path, body or {}, token)


@router.post("/recording/start-session")
def recording_start(body: dict | None = None):
    return _guard(lambda: _agent_post("/recording/start_session", {"task": (body or {}).get("task", "")}))


@router.post("/recording/start-episode")
def episode_start(body: dict | None = None):
    body = body or {}
    return _guard(lambda: _agent_post("/recording/start_episode", {"source": body.get("source", "scripted_expert"), "task": body.get("task", "")}))


@router.post("/recording/end-episode")
def episode_end(body: dict | None = None):
    body = body or {}
    return _guard(lambda: _agent_post("/recording/end_episode", {"discarded": bool(body.get("discarded", False)), "reason": body.get("reason", "")}))


@router.post("/recording/discard-episode")
def episode_discard(body: dict | None = None):
    return _guard(lambda: _agent_post("/recording/discard_episode", {"reason": (body or {}).get("reason", "discarded")}))


@router.post("/recording/stop-session")
def recording_stop():
    return _guard(lambda: _agent_post("/recording/stop_session"))


@router.post("/expert/run")
def expert_run():
    return _guard(lambda: _agent_post("/expert/run"))


@router.post("/expert/stop")
def expert_stop():
    return _guard(lambda: _agent_post("/expert/stop"))


@router.post("/task/reset")
def task_reset():
    return _guard(lambda: _agent_post("/task/reset"))


@router.get("/sessions")
def sessions():
    remote = _guard(lambda: _http("GET", "/sessions", timeout=5.0))
    local = list_upper_sessions()
    merged = []
    for item in remote.get("sessions", []):
        sid = item.get("session_id")
        local_item = next((x for x in local if x["session_id"] == sid), None)
        merged.append({**item, "upper": local_item})
    return {"sessions": merged}


def list_upper_sessions() -> list[dict]:
    out = []
    if not UPPER_SESSIONS.is_dir():
        return out
    for path in sorted(UPPER_SESSIONS.iterdir()):
        if not path.is_dir():
            continue
        manifest = path / "manifest.json"
        ready = path / "READY"
        info: dict[str, Any] = {"session_id": path.name, "path": str(path), "ready": ready.exists()}
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text())
                info.update({"status": data.get("status"), "episodes": len(data.get("episodes", [])), "files": len(data.get("files", []))})
            except Exception:
                info["status"] = "invalid_manifest"
        out.append(info)
    return out


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise BridgeError(422, f"archive path escape: {member.name}")
        tar.extractall(destination)


def sync_session(session_id: str) -> dict:
    if not session_id or "/" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="invalid session id")
    manifest = _guard(lambda: _http("GET", f"/sessions/{session_id}/manifest", timeout=10.0))
    final = UPPER_SESSIONS / session_id
    if (final / "READY").exists():
        return {"session_id": session_id, "status": "already_synced", "path": str(final)}
    if final.exists():
        raise HTTPException(status_code=409, detail=f"incomplete upper copy exists at {final}")
    staging = UPPER_SESSIONS / f".{session_id}.staging-{int(time.time())}"
    staging.mkdir(parents=True)
    archive = staging / "session.tar.gz"
    try:
        with urllib.request.urlopen(AGENT_URL + f"/sessions/{session_id}/archive", timeout=120) as response, open(archive, "wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        extract = staging / "payload"
        extract.mkdir()
        _safe_extract(archive, extract)
        verified = []
        for entry in manifest.get("files", []):
            path = extract / entry["path"]
            if not path.is_file():
                raise BridgeError(422, f"transferred file missing: {entry['path']}")
            if path.stat().st_size != entry["size"]:
                raise BridgeError(422, f"size mismatch: {entry['path']}")
            digest = _sha256(path)
            if digest != entry["sha256"]:
                raise BridgeError(422, f"sha256 mismatch: {entry['path']}")
            verified.append(entry["path"])
        (extract / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (extract / "READY").write_text(json.dumps({
            "session_id": session_id,
            "verified_files": len(verified),
            "manifest_sha256": _sha256(extract / "manifest.json"),
            "synced_wall": time.time(),
        }))
        # Publish the extracted session itself, not a wrapper staging dir.
        os.replace(extract, final)
        shutil.rmtree(staging, ignore_errors=True)
        return {"session_id": session_id, "status": "raw_ready", "path": str(final), "verified_files": len(verified)}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


@router.post("/sync")
def sync(body: dict):
    return _guard(lambda: sync_session(str(body.get("session_id", ""))))


# ---------------------------------------------------------------------------
# MCAP alignment and LeRobot export
# ---------------------------------------------------------------------------


def _read_bag(bag_dir: Path, start_ns: int, end_ns: int) -> dict[str, Any]:
    from rosbags.highlevel import AnyReader

    images = []
    states = []
    odoms = []
    events = []
    with AnyReader([bag_dir]) as reader:
        for connection, timestamp, raw in reader.messages():
            topic = connection.topic
            if topic not in {
                "/lelab/wrist/rgb/image_raw", "/joint_states", "/lelab/base/odom", "/lelab/events",
            }:
                continue
            msg = reader.deserialize(raw, connection.msgtype)
            stamp = int(getattr(getattr(msg, "header", None), "stamp", None).sec * 1e9 + getattr(getattr(msg, "header", None), "stamp", None).nanosec) if hasattr(msg, "header") else 0
            if stamp <= 0:
                stamp = int(timestamp)
            if stamp < start_ns - 2_000_000_000 or stamp > end_ns + 2_000_000_000:
                continue
            if topic == "/lelab/wrist/rgb/image_raw":
                images.append({"stamp": stamp, "width": msg.width, "height": msg.height, "encoding": msg.encoding, "data": bytes(msg.data)})
            elif topic == "/joint_states":
                states.append({"stamp": stamp, "names": list(msg.name), "positions": list(msg.position)})
            elif topic == "/lelab/base/odom":
                odoms.append({"stamp": stamp, "vx": float(msg.twist.twist.linear.x), "vy": float(msg.twist.twist.linear.y), "wz": float(msg.twist.twist.angular.z)})
            elif topic == "/lelab/events":
                try:
                    event = json.loads(msg.data)
                except Exception:
                    continue
                if event.get("kind") == "command" and isinstance(event.get("action"), list) and len(event["action"]) == 9:
                    events.append(event)
    images.sort(key=lambda x: x["stamp"])
    states.sort(key=lambda x: x["stamp"])
    odoms.sort(key=lambda x: x["stamp"])
    events.sort(key=lambda x: int(x.get("sim_time_ns", 0)))
    return {"images": images, "states": states, "odoms": odoms, "events": events}


def _latest(items: list[dict], stamp: int) -> dict | None:
    import bisect

    keys = [item["stamp"] for item in items]
    index = bisect.bisect_right(keys, stamp) - 1
    return items[index] if index >= 0 else None


def _latest_event(events: list[dict], stamp: int) -> dict | None:
    import bisect

    keys = [int(item.get("sim_time_ns", 0)) for item in events]
    index = bisect.bisect_right(keys, stamp) - 1
    return events[index] if index >= 0 else None


def export_session(session_id: str, include_failed: bool = False) -> dict:
    from lerobot.datasets import LeRobotDataset
    import numpy as np

    session_dir = UPPER_SESSIONS / session_id
    if not (session_dir / "READY").exists():
        raise HTTPException(status_code=409, detail="session is not synced and verified on the upper host")
    manifest = json.loads((session_dir / "manifest.json").read_text())
    episodes = [e for e in manifest.get("episodes", []) if e.get("status") == "saved"]
    if not include_failed:
        episodes = [e for e in episodes if (e.get("evaluation") or {}).get("success") is True]
    if not episodes:
        raise HTTPException(status_code=409, detail="no accepted successful episodes in this session")
    bag_dir = session_dir / "raw" / "bag"
    if not bag_dir.is_dir():
        raise HTTPException(status_code=409, detail="raw bag directory missing")

    repo_short = session_id.replace("ros_", "").replace("_", "-").lower()
    repo_id = f"lelab/ros-mobile-{repo_short}"
    final_root = lerobot_dataset_root() / repo_id
    if final_root.exists():
        raise HTTPException(status_code=409, detail=f"dataset already exists: {final_root}")
    export_dir = UPPER_EXPORTS / session_id
    export_dir.mkdir(parents=True, exist_ok=True)
    staging = export_dir / f"staging-{int(time.time())}"
    # LeRobotDataset.create owns the root and requires it not to exist.
    fps = 10
    features = {
        "observation.images.wrist_rgb": {
            "dtype": "video", "shape": (480, 640, 3), "names": ["height", "width", "channels"],
            "info": {"is_depth_map": False},
        },
        "observation.state": {
            "dtype": "float32", "shape": (9,),
            "names": ["base_vx", "base_vy", "base_wz", "joint1.position", "joint2.position", "joint3.position", "joint4.position", "joint5.position", "gripper_base.position"],
        },
        "action": {
            "dtype": "float32", "shape": (9,),
            "names": ["base_vx", "base_vy", "base_wz", "joint1.target", "joint2.target", "joint3.target", "joint4.target", "joint5.target", "gripper_base.target"],
        },
    }
    report = {
        "session_id": session_id,
        "repo_id": repo_id,
        "fps": fps,
        "source": "simulation",
        "profile": manifest.get("profile"),
        "episodes": [],
        "alignment": "latest observation at or before each frame; image age <= 0.20 s, state/odom <= 0.10 s; no future data",
        "action_source": "gateway_applied_target_with_zero_order_hold",
        "base_velocity_source": "gazebo_wheel_odometry_simulation_feedback",
        "warnings": [
            "base body velocity is simulation wheel odometry and can differ from the velocity-controlled ground-truth motion",
            "scripted expert uses privileged cube/container truth for planning; truth is not included in state/action",
        ],
    }
    dataset = None
    try:
        dataset = LeRobotDataset.create(
            repo_id=repo_id,
            fps=fps,
            features=features,
            root=staging,
            robot_type="lelab_mobile_mecanum_5dof",
            use_videos=True,
            batch_encoding_size=1,
        )
        for episode in episodes:
            start = int(episode["started_sim_ns"])
            end = int(episode["ended_sim_ns"] or episode["started_sim_ns"])
            data = _read_bag(bag_dir, start, end)
            if not data["images"] or not data["states"] or not data["odoms"]:
                raise RuntimeError(f"episode {episode['episode_id']} has no usable sensor data")
            initial = episode.get("evaluation", {}).get("cube_initial") or [0, 0, 0]
            initial_state = None
            initial_event = None
            frame_records = []
            count = 0
            t = start
            while t <= end:
                state = _latest(data["states"], t)
                odom = _latest(data["odoms"], t)
                image = _latest(data["images"], t)
                event = _latest_event(data["events"], t)
                if state is None or odom is None or image is None:
                    raise RuntimeError(f"episode {episode['episode_id']} source gap at {t - start} ns")
                image_age = (t - image["stamp"]) / 1e9
                state_age = (t - state["stamp"]) / 1e9
                odom_age = (t - odom["stamp"]) / 1e9
                if image_age > 0.20 or state_age > 0.10 or odom_age > 0.10:
                    raise RuntimeError(
                        f"episode {episode['episode_id']} stale source at +{((t-start)/1e9):.2f}s "
                        f"image={image_age:.3f}s state={state_age:.3f}s odom={odom_age:.3f}s"
                    )
                if event is None:
                    action = [0.0, 0.0, 0.0, *(state["positions"][i] for i in range(5)), 0.0]
                    action_stamp = start
                else:
                    action = [float(v) for v in event["action"]]
                    action_stamp = int(event["sim_time_ns"])
                joints = dict(zip(state["names"], state["positions"]))
                state_vec = np.array([
                    odom["vx"], odom["vy"], odom["wz"],
                    *(joints.get(name, 0.0) for name in ("joint1", "joint2", "joint3", "joint4", "joint5")),
                    joints.get("gripper_base_joint", 0.0),
                ], dtype=np.float32)
                if image["encoding"] not in ("rgb8", "bgr8"):
                    raise RuntimeError(f"unsupported image encoding {image['encoding']}")
                image_array = np.frombuffer(image["data"], dtype=np.uint8).reshape(image["height"], image["width"], 3)
                if image["encoding"] == "bgr8":
                    image_array = image_array[:, :, ::-1]
                dataset.add_frame({
                    "observation.images.wrist_rgb": image_array,
                    "observation.state": state_vec,
                    "action": np.asarray(action, dtype=np.float32),
                    "task": episode.get("task") or manifest.get("task") or "Approach the cube and place it in the container.",
                })
                frame_records.append({
                    "frame": count,
                    "sim_time_ns": t,
                    "image_stamp_ns": image["stamp"],
                    "state_stamp_ns": state["stamp"],
                    "odom_stamp_ns": odom["stamp"],
                    "action_stamp_ns": action_stamp,
                    "image_age_s": round(image_age, 4),
                    "state_age_s": round(state_age, 4),
                })
                count += 1
                t += int(1e9 / fps)
            if count < 2:
                raise RuntimeError(f"episode {episode['episode_id']} produced only {count} frames")
            dataset.save_episode()
            report["episodes"].append({
                "episode_id": episode["episode_id"],
                "source": episode.get("source"),
                "success": episode.get("evaluation", {}).get("success"),
                "frames": count,
                "started_sim_ns": start,
                "ended_sim_ns": end,
                "frames_detail": frame_records,
            })
        dataset.finalize()
        dataset = None
        # Validate with the local SDK before publishing.
        from lerobot.datasets import LeRobotDataset as VerifyDataset
        verify = VerifyDataset(repo_id=repo_id, root=staging)
        total_frames = len(verify)
        total_episodes = int(verify.meta.total_episodes)
        sample = verify[0]
        sample_keys = sorted(sample.keys())
        image_key = "observation.images.wrist_rgb"
        image_shape = tuple(sample[image_key].shape) if image_key in sample else None
        verify = None
        if total_episodes != len(report["episodes"]):
            raise RuntimeError(f"episode count mismatch: {total_episodes} != {len(report['episodes'])}")
        report.update({
            "status": "validated",
            "total_frames": total_frames,
            "total_episodes": total_episodes,
            "sample_keys": sample_keys,
            "sample_image_shape": image_shape,
            "validated_wall": time.time(),
        })
        report["dataset_root"] = str(final_root)
        final_root.parent.mkdir(parents=True, exist_ok=True)
        temp_final = final_root.parent / f".{final_root.name}.publishing-{int(time.time())}"
        shutil.move(str(staging), str(temp_final))
        os.rename(temp_final, final_root)
        (export_dir / "export_report.json").write_text(json.dumps(report, indent=2))
        (export_dir / "READY").write_text(json.dumps({"repo_id": repo_id, "root": str(final_root), "total_frames": total_frames}))
        return report
    except Exception:
        if dataset is not None:
            try:
                dataset.finalize()
            except Exception:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise


@router.post("/export")
def export(body: dict):
    session_id = str(body.get("session_id", ""))
    return _guard(lambda: export_session(session_id, bool(body.get("include_failed", False))))


@router.get("/exports")
def exports():
    out = []
    if UPPER_EXPORTS.is_dir():
        for path in sorted(UPPER_EXPORTS.iterdir()):
            report = path / "export_report.json"
            if report.is_file():
                try:
                    out.append(json.loads(report.read_text()))
                except Exception:
                    pass
    return {"exports": out}
