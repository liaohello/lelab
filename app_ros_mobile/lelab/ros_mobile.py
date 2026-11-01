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
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
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
EXPORT_JOB_ROOT = COLLECTION_ROOT / "upper" / "export_jobs"
EXPORT_JOB_ROOT.mkdir(parents=True, exist_ok=True)
EXPORT_JOBS: dict[str, dict] = {}
EXPORT_JOB_LOCK = threading.Lock()


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
        self.read_only = False

    def _lease(self):
        return get_hardware_lease_manager()

    def acquire(self, owner: str) -> dict:
        with self.lock:
            status = _http("GET", "/status", timeout=5.0)
            self.read_only = bool(status.get("read_only"))
            self.last_status = status
            if self.read_only:
                return {"owner": owner, "read_only": True, "mode": "readonly", "reused": True}
            if self.agent_token:
                try:
                    _http("POST", "/control/renew", {}, self.agent_token)
                    return {"owner": self.owner, "mode": self.last_status.get("control", {}).get("mode", "idle"), "reused": True}
                except BridgeError as exc:
                    if exc.status not in (401, 403):
                        raise
                    # The car agent was restarted; its token no longer exists.
                    self.agent_token = None
                    if self.lease_token:
                        try:
                            self._lease().release(token=self.lease_token, reason="agent-token-stale")
                        except Exception:
                            pass
                    self.lease_token = None
                    self.owner = None
                    self.seq = -1
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
            if self.read_only:
                return {"ok": True, "read_only": True}
            if not self.agent_token:
                raise BridgeError(409, "ROS mobile control is not acquired")
            _http("POST", "/control/renew", {}, self.agent_token)
            self._lease().renew(token=self.lease_token, ttl_seconds=LEASE_TTL)
            return {"ok": True, "expires_in_s": AGENT_TTL}

    def release(self, reason: str = "released") -> dict:
        with self.lock:
            if self.read_only:
                return {"ok": True, "read_only": True}
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
            if self.read_only:
                return {"read_only": True, "estop": False, "detail": "no actuator output in readonly mode"}
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
            if not self.last_status_wall:
                # A freshly started upper backend has not polled /status yet.
                # Do not mistake a read-only agent for a controllable one.
                status = _http("GET", "/status", timeout=5.0)
                self.last_status = status
                self.last_status_wall = time.time()
                self.read_only = bool(status.get("read_only"))
            if self.read_only:
                return ""
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
            self.read_only = bool(status.get("read_only"))
            self.last_status_wall = time.time()
        return {
            "agent": status,
            "bridge": {
                "owner": owner,
                "read_only": self.read_only,
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
    except ExportRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


def fetch_preview_jpeg(timeout: float = 4.0) -> bytes:
    """Fetch one JPEG frame from the car agent. Kept separate so it can be stubbed."""
    with urllib.request.urlopen(AGENT_URL + "/preview.jpg", timeout=timeout) as response:
        return response.read()


@router.get("/preview.jpg")
def preview_jpg():
    try:
        data = fetch_preview_jpeg()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"preview unavailable: {exc}") from exc
    fetched_at = time.time()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-LeLab-Fetched-At": f"{fetched_at:.3f}",
        },
    )


# ------------------------------------------------------- hardware read-only采集
#
# The hardware profile is a repository file, not an agent secret: it records
# which real sensor topics were verified, which actuator topics must stay
# silent, and why training export is refused.  It is served together with the
# live agent status so the采集 page can show "what is real" next to the frame.

HARDWARE_ROOT = COLLECTION_ROOT / "upper" / "hardware"
HARDWARE_ROOT.mkdir(parents=True, exist_ok=True)
DEFAULT_HARDWARE_PROFILE = "hardware-readonly-v1.json"
# SOF markers that carry the frame size in a JPEG stream (0xC4/0xC8/0xCC are tables).
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _repo_root() -> Path:
    # app_ros_mobile/lelab/ros_mobile.py -> repository root
    return Path(__file__).resolve().parents[2]


def hardware_profile_path() -> Path:
    override = os.environ.get("LELAB_ROS_HARDWARE_PROFILE")
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / "configs" / "ros2_collection" / DEFAULT_HARDWARE_PROFILE


def load_hardware_profile() -> dict:
    path = hardware_profile_path()
    if not path.is_file():
        raise BridgeError(404, f"hardware profile not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BridgeError(500, f"hardware profile is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BridgeError(500, f"hardware profile must be a JSON object: {path}")
    return {"path": str(path), "sha256": _sha256(path), "profile": data}


def jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Return (width, height) from a JPEG SOF segment, or (None, None)."""
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None, None
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            return None, None
        marker = data[index + 1]
        if marker == 0xFF:  # fill byte
            index += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            index += 2
            continue
        segment = int.from_bytes(data[index + 2 : index + 4], "big")
        if segment < 2:
            return None, None
        if marker in _JPEG_SOF:
            if index + 9 > len(data):
                return None, None
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        index += 2 + segment
    return None, None


def _sensor_rows(sensors: dict) -> list[dict]:
    rows = [
        ("rgb", "RGB", True),
        ("depth", "深度", False),
        ("lidar", "雷达", False),
        ("imu", "IMU", False),
    ]
    out = []
    for key, label, required in rows:
        item = sensors.get(key) or {}
        age = item.get("age_s")
        out.append(
            {
                "key": key,
                "label": label,
                "topic": item.get("topic"),
                "age_s": age,
                "required": required,
                "present": item.get("topic") is not None and age is not None,
            }
        )
    return out


@router.get("/hardware")
def hardware_overview():
    def run() -> dict:
        loaded = load_hardware_profile()
        profile = loaded["profile"]
        runtime = profile.get("runtime") or {}
        agent_status: dict[str, Any] = {}
        agent_error: str | None = None
        try:
            agent_status = _http("GET", "/status", timeout=4.0)
        except BridgeError as exc:
            agent_error = exc.detail
        sensors = agent_status.get("sensors") or {}
        rgb = sensors.get("rgb") or {}
        features = profile.get("features") or {}
        image_meta = features.get("image") or {}
        recording = profile.get("recording") or {}
        return {
            "mode": runtime.get("mode"),
            "read_only": bool(agent_status.get("read_only", True)),
            "profile": profile,
            "profile_path": loaded["path"],
            "profile_sha256": loaded["sha256"],
            "camera": {
                "topic": rgb.get("topic") or (profile.get("sensors", {}).get("wrist_rgb") or {}).get("rgb_topic"),
                "expected_width": (profile.get("sensors", {}).get("wrist_rgb") or {}).get("capture_width"),
                "expected_height": (profile.get("sensors", {}).get("wrist_rgb") or {}).get("capture_height"),
                "fps": rgb.get("fps"),
                "age_s": rgb.get("age_s"),
                "stale": rgb.get("stale"),
                "preview_seq": rgb.get("preview_seq"),
                "preview_backend": rgb.get("preview_backend"),
                "feature_key": image_meta.get("name"),
                "frame_id": image_meta.get("frame"),
            },
            "sensors": _sensor_rows(sensors),
            "raw_sensors": sensors,
            "topics": agent_status.get("topics"),
            "recording": agent_status.get("recording"),
            "echoes": {
                "actuator_outputs": runtime.get("actuator_outputs"),
                "training_export": bool((profile.get("training_export") or {}).get("enabled")),
                "training_export_reason": (profile.get("training_export") or {}).get("reason"),
                "alignments": profile.get("alignment"),
                "recording_topics": recording.get("topics") or [],
                "required_topics": recording.get("required_topics") or [],
                "raw_storage": recording.get("raw_storage"),
                "min_free_bytes": recording.get("min_free_bytes"),
            },
            "agent_ok": bool(agent_status),
            "agent_error": agent_error,
            "checked_at": time.time(),
        }

    return _guard(run)


@router.post("/hardware/snapshot")
def hardware_snapshot():
    """Save one real frame plus the status that accompanied it as evidence.

    The car link drops to NO-CARRIER for minutes at a time, so a single click
    that keeps the bytes, the frame size, the topic and the sensor ages is the
    only way a "画面已跑通" claim stays checkable afterwards.
    """

    def run() -> dict:
        captured_at = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(captured_at))
        target = HARDWARE_ROOT / f"snapshot-{stamp}-{uuid.uuid4().hex[:6]}"
        target.mkdir(parents=True, exist_ok=False)
        notes: list[str] = []

        jpeg = b""
        try:
            jpeg = fetch_preview_jpeg()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"preview unavailable: {exc}")

        agent_status: dict[str, Any] = {}
        try:
            agent_status = _http("GET", "/status", timeout=4.0)
        except BridgeError as exc:
            notes.append(f"agent status unavailable: {exc.detail}")

        width = height = None
        jpeg_sha = None
        if jpeg:
            width, height = jpeg_dimensions(jpeg)
            jpg_tmp = target / "frame.jpg.tmp"
            jpg_tmp.write_bytes(jpeg)
            os.replace(jpg_tmp, target / "frame.jpg")
            jpeg_sha = _sha256(target / "frame.jpg")

        status_tmp = target / "agent_status.json.tmp"
        status_tmp.write_text(json.dumps(agent_status, ensure_ascii=False, indent=1, sort_keys=True))
        os.replace(status_tmp, target / "agent_status.json")

        expected_width = expected_height = None
        try:
            profile = load_hardware_profile()["profile"]
            wrist = (profile.get("sensors") or {}).get("wrist_rgb") or {}
            expected_width = wrist.get("capture_width")
            expected_height = wrist.get("capture_height")
        except BridgeError as exc:
            notes.append(exc.detail)

        rgb = (agent_status.get("sensors") or {}).get("rgb") or {}
        capture = {
            "captured_at_unix": captured_at,
            "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(captured_at)),
            "agent_url": AGENT_URL,
            "camera_topic": rgb.get("topic"),
            "camera_age_s": rgb.get("age_s"),
            "camera_fps": rgb.get("fps"),
            "camera_stale": rgb.get("stale"),
            "preview_seq": rgb.get("preview_seq"),
            "preview_backend": rgb.get("preview_backend"),
            "jpeg_bytes": len(jpeg) or None,
            "jpeg_sha256": jpeg_sha,
            "jpeg_width": width,
            "jpeg_height": height,
            "expected_width": expected_width,
            "expected_height": expected_height,
            "size_matches_profile": (
                None if (width is None or expected_width is None) else (width, height) == (expected_width, expected_height)
            ),
            "has_frame": bool(jpeg),
            "notes": notes,
        }
        (target / "capture.json").write_text(json.dumps(capture, ensure_ascii=False, indent=1, sort_keys=True))
        return {"snapshot_id": target.name, "path": str(target), **capture}

    return _guard(run)


@router.get("/hardware/snapshots")
def hardware_snapshots(limit: int = 50):
    def run() -> dict:
        items: list[dict] = []
        if HARDWARE_ROOT.is_dir():
            # Newest first, by directory mtime: names share a 1 s UTC stamp and
            # would otherwise order by their random suffix.
            directories = sorted(
                (path for path in HARDWARE_ROOT.iterdir() if path.is_dir()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in directories:
                meta_path = path / "capture.json"
                item: dict[str, Any] = {"snapshot_id": path.name, "path": str(path)}
                if meta_path.is_file():
                    try:
                        item.update(json.loads(meta_path.read_text()))
                    except json.JSONDecodeError:
                        item["corrupt"] = True
                else:
                    item["corrupt"] = True
                items.append(item)
                if len(items) >= max(1, limit):
                    break
        return {"snapshots": items, "root": str(HARDWARE_ROOT)}

    return _guard(run)


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
    if bridge.read_only:
        raise HTTPException(status_code=403, detail="readonly hardware mode does not accept actuator commands")
    token = _guard(bridge.agent_token_required)
    return _guard(lambda: _http("POST", "/control/mode", {"mode": body.get("mode", "idle")}, token))


@router.post("/control/base")
def control_base(body: dict):
    bridge = get_bridge()
    if bridge.read_only:
        raise HTTPException(status_code=403, detail="readonly hardware mode does not accept actuator commands")
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
    if bridge.read_only:
        raise HTTPException(status_code=403, detail="readonly hardware mode does not accept actuator commands")
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
    if bridge.read_only:
        raise HTTPException(status_code=403, detail="readonly hardware mode does not accept actuator commands")
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
    bridge = get_bridge()
    if bridge.read_only:
        raise HTTPException(status_code=403, detail="readonly hardware mode has no actuator to stop")
    return _guard(lambda: bridge.emergency_stop((body or {}).get("reason", "web-estop")))


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
            if member.issym() or member.islnk():
                raise BridgeError(422, f"archive contains a link: {member.name}")
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise BridgeError(422, f"archive path escape: {member.name}")
        tar.extractall(destination, filter="data")


def _verify_synced_session(session_dir: Path, manifest: dict) -> dict:
    if manifest.get("status") != "raw_ready" or manifest.get("ready_eligible") is not True:
        raise BridgeError(409, f"session is not ready_eligible: {manifest.get('status')}/{manifest.get('error')}")
    if int(manifest.get("bag_exit_code", -1)) != 0 or manifest.get("bag_readable") is not True:
        raise BridgeError(409, "recorder did not stop normally or bag is not readable")
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BridgeError(422, "manifest missing on upper host")
    ready_path = session_dir / "READY"
    if not ready_path.is_file():
        raise BridgeError(409, "READY marker missing")
    ready = json.loads(ready_path.read_text())
    if ready.get("manifest_sha256") != _sha256(manifest_path):
        raise BridgeError(422, "READY manifest hash mismatch")
    expected = {entry["path"]: entry for entry in manifest.get("files", [])}
    actual = {
        str(path.relative_to(session_dir)): path
        for path in session_dir.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "READY"} and not path.name.endswith(".tar.gz")
    }
    if set(expected) != set(actual):
        raise BridgeError(422, f"file set mismatch: missing={sorted(set(expected)-set(actual))} extra={sorted(set(actual)-set(expected))}")
    for relative, entry in expected.items():
        path = session_dir / relative
        if path.stat().st_size != entry["size"]:
            raise BridgeError(422, f"size mismatch: {relative}")
        if _sha256(path) != entry["sha256"]:
            raise BridgeError(422, f"sha256 mismatch: {relative}")
    bag_dir = session_dir / "raw" / "bag"
    if not bag_dir.is_dir():
        raise BridgeError(422, "bag directory missing")
    from rosbags.highlevel import AnyReader
    try:
        with AnyReader([bag_dir]) as reader:
            topics = {c.topic for c in reader.connections}
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(422, f"bag is not readable by rosbags: {exc}") from exc
    required = set(manifest.get("required_topics", []))
    missing = sorted(required - topics)
    if missing:
        raise BridgeError(422, f"required bag topics missing: {missing}")
    return {"manifest_sha256": _sha256(manifest_path), "topics": sorted(topics), "verified_files": len(expected)}


def sync_session(session_id: str) -> dict:
    if not session_id or "/" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="invalid session id")
    manifest = _guard(lambda: _http("GET", f"/sessions/{session_id}/manifest", timeout=10.0))
    if manifest.get("status") != "raw_ready" or manifest.get("ready_eligible") is not True:
        raise HTTPException(status_code=409, detail=f"agent session is not ready: {manifest.get('status')}/{manifest.get('error')}")
    final = UPPER_SESSIONS / session_id
    if final.exists():
        try:
            verification = _verify_synced_session(final, manifest)
            return {"session_id": session_id, "status": "already_synced", "path": str(final), **verification}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=f"existing upper copy failed verification: {exc}") from exc
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
        ready = json.loads((extract / "READY").read_text())
        if ready.get("manifest_sha256") != _sha256(extract / "manifest.json"):
            raise BridgeError(422, "agent READY manifest hash mismatch")
        # Publish the extracted session itself, not a wrapper staging dir.
        os.replace(extract, final)
        shutil.rmtree(staging, ignore_errors=True)
        verification = _verify_synced_session(final, manifest)
        return {"session_id": session_id, "status": "raw_ready", "path": str(final), **verification}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


@router.post("/sync")
def sync(body: dict):
    return _guard(lambda: sync_session(str(body.get("session_id", ""))))


# ---------------------------------------------------------------------------
# MCAP alignment and LeRobot export
# ---------------------------------------------------------------------------


def _read_bag(bag_dir: Path, start_ns: int, end_ns: int, image_topic: str, state_topic: str, base_topic: str) -> dict[str, Any]:
    from rosbags.highlevel import AnyReader

    images = []
    states = []
    bases = []
    events = []
    topics = {image_topic, state_topic, base_topic, "/lelab/events"}
    with AnyReader([bag_dir]) as reader:
        for connection, timestamp, raw in reader.messages():
            topic = connection.topic
            if topic not in topics:
                continue
            msg = reader.deserialize(raw, connection.msgtype)
            header = getattr(msg, "header", None)
            header_stamp = 0
            if header is not None:
                header_stamp = int(header.stamp.sec * 1e9 + header.stamp.nanosec)
            if header_stamp <= 0:
                stamp = int(timestamp)
                stamp_source = "bag_receive"
            else:
                stamp = header_stamp
                stamp_source = "header"
            if topic == "/lelab/events":
                # Event strings have no header: their payload clock is the
                # episode clock, while the bag timestamp is host receive time.
                try:
                    event = json.loads(msg.data)
                except Exception:
                    continue
                event_time = event.get("time_ns", event.get("sim_time_ns"))
                if (
                    event.get("kind") == "command"
                    and isinstance(event.get("action"), list)
                    and len(event["action"]) == 9
                    and event_time is not None
                ):
                    event_time = int(event_time)
                    if start_ns - 2_000_000_000 <= event_time <= end_ns + 2_000_000_000:
                        event["_time_ns"] = event_time
                        event["_received_ns"] = int(timestamp)
                        events.append(event)
                continue
            if stamp < start_ns - 2_000_000_000 or stamp > end_ns + 2_000_000_000:
                continue
            if topic == image_topic:
                images.append({"stamp": stamp, "received_ns": int(timestamp), "stamp_source": stamp_source,
                               "width": msg.width, "height": msg.height, "encoding": msg.encoding, "data": bytes(msg.data)})
            elif topic == state_topic:
                states.append({"stamp": stamp, "received_ns": int(timestamp), "stamp_source": stamp_source,
                               "names": list(msg.name), "positions": list(msg.position)})
            elif topic == base_topic:
                bases.append({"stamp": stamp, "received_ns": int(timestamp), "stamp_source": stamp_source,
                              "vx": float(msg.twist.twist.linear.x), "vy": float(msg.twist.twist.linear.y),
                              "wz": float(msg.twist.twist.angular.z),
                              "pose": [float(msg.pose.pose.position.x), float(msg.pose.pose.position.y), float(msg.pose.pose.position.z)]})
    images.sort(key=lambda x: x["stamp"])
    states.sort(key=lambda x: x["stamp"])
    bases.sort(key=lambda x: x["stamp"])
    events.sort(key=lambda x: x["_time_ns"])
    return {"images": images, "states": states, "bases": bases, "events": events}


def _latest(items: list[dict], stamp: int) -> dict | None:
    import bisect

    keys = [item["stamp"] for item in items]
    index = bisect.bisect_right(keys, stamp) - 1
    return items[index] if index >= 0 else None


def _latest_event(events: list[dict], stamp: int) -> dict | None:
    import bisect

    keys = [item["_time_ns"] for item in events]
    index = bisect.bisect_right(keys, stamp) - 1
    return events[index] if index >= 0 else None


class ExportRejected(Exception):
    pass


def _check_exportable(session_id: str) -> tuple[Path, dict, dict]:
    session_dir = UPPER_SESSIONS / session_id
    if not (session_dir / "READY").exists():
        raise HTTPException(status_code=409, detail="session is not synced and verified on the upper host")
    manifest = json.loads((session_dir / "manifest.json").read_text())
    if manifest.get("status") != "raw_ready" or manifest.get("ready_eligible") is not True:
        raise HTTPException(status_code=409, detail=f"session is not ready for export: {manifest.get('status')}/{manifest.get('error')}")
    meta = manifest.get("metadata", {})
    if meta.get("source") != "simulation":
        raise HTTPException(status_code=409, detail="only simulation sessions with action/feedback can be exported for training")
    if not bool(meta.get("training_export", True)):
        raise HTTPException(status_code=409, detail="session profile declares training_export=false")
    return session_dir, manifest, meta


def export_session(session_id: str, include_failed: bool = False) -> dict:
    from lerobot.datasets import LeRobotDataset
    import numpy as np

    session_dir, manifest, meta = _check_exportable(session_id)
    image_topic = meta.get("camera_topic") or "/lelab/wrist/rgb/image_raw"
    state_topic = meta.get("record_joint_state_topic") or meta.get("joint_state_topic") or "/joint_states"
    base_topic = meta.get("base_velocity_topic") or "/lelab/base/odom"
    base_source = meta.get("base_velocity_source") or ("gazebo_model_odometry" if base_topic == "/lelab/base/model_odom" else "legacy_wheel_odometry")
    camera_profile = meta.get("camera_profile") or "legacy_unlabeled"
    camera_extrinsics = meta.get("camera_extrinsics") or {}
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
        "source": meta.get("source"),
        "profile": manifest.get("profile"),
        "profile_sha256": meta.get("profile_sha256"),
        "clock_domain": meta.get("clock_domain"),
        "camera_profile": camera_profile,
        "camera_extrinsics": camera_extrinsics,
        "topics": {"image": image_topic, "state": state_topic, "base_velocity": base_topic, "action_events": "/lelab/events"},
        "episodes": [],
        "alignment": "latest observation at or before each frame; image age <= 0.20 s, state/base <= 0.10 s, action <= 3.0 s zero-order hold; no future data",
        "action_source": "gateway_applied_target_with_zero_order_hold",
        "base_velocity_source": base_source,
        "warnings": ([] if base_source == "gazebo_model_odometry" else ["legacy session uses wheel odometry; body velocity may not match the model"]) + ([] if camera_profile != "legacy_unlabeled" else ["legacy session has no camera profile/extrinsics label"]),
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
            start = int(episode.get("started_time_ns", episode.get("started_sim_ns")))
            end = int(episode.get("ended_time_ns") or episode.get("ended_sim_ns") or start)
            data = _read_bag(bag_dir, start, end, image_topic, state_topic, base_topic)
            if not data["images"] or not data["states"] or not data["bases"]:
                raise ExportRejected(f"episode {episode['episode_id']} lacks image/state/base data")
            if not data["events"]:
                raise ExportRejected(f"episode {episode['episode_id']} has no applied-action events; refusing synthetic action")
            # Do not fabricate a frame before the first available observation.
            # If the recorder starts a few milliseconds after the episode marker,
            # begin at the first moment all measured streams exist.
            observed_start = max(
                int(data["images"][0]["stamp"]),
                int(data["states"][0]["stamp"]),
                int(data["bases"][0]["stamp"]),
                int(data["events"][0]["_time_ns"]),
            )
            original_start = start
            start = max(start, observed_start)
            frame_records = []
            count = 0
            t = start
            while t <= end:
                state = _latest(data["states"], t)
                odom = _latest(data["bases"], t)
                image = _latest(data["images"], t)
                event = _latest_event(data["events"], t)
                if state is None or odom is None or image is None:
                    raise ExportRejected(f"episode {episode['episode_id']} source gap at {t - start} ns")
                if event is None:
                    raise ExportRejected(f"episode {episode['episode_id']} has no action at +{((t-start)/1e9):.2f}s")
                action_age = (t - event["_time_ns"]) / 1e9
                # Held joint trajectory targets are explicit gateway commands,
                # not synthetic actions; a 2.5 s arm segment is the longest hold.
                if action_age < 0 or action_age > 3.0:
                    raise ExportRejected(f"episode {episode['episode_id']} action age {action_age:.3f}s at +{((t-start)/1e9):.2f}s")
                image_age = (t - image["stamp"]) / 1e9
                state_age = (t - state["stamp"]) / 1e9
                odom_age = (t - odom["stamp"]) / 1e9
                if image_age > 0.20 or state_age > 0.10 or odom_age > 0.10:
                    raise RuntimeError(
                        f"episode {episode['episode_id']} stale source at +{((t-start)/1e9):.2f}s "
                        f"image={image_age:.3f}s state={state_age:.3f}s odom={odom_age:.3f}s"
                    )
                action = [float(v) for v in event["action"]]
                if not all(np.isfinite(action)):
                    raise ExportRejected(f"episode {episode['episode_id']} non-finite action")
                action_stamp = int(event["_time_ns"])
                joints = dict(zip(state["names"], state["positions"]))
                required_joints = ("joint1", "joint2", "joint3", "joint4", "joint5", "gripper_base_joint")
                missing_joints = [name for name in required_joints if name not in joints]
                if missing_joints:
                    raise ExportRejected(f"episode {episode['episode_id']} missing measured joints {missing_joints}; refusing zero fill")
                state_vec = np.array([
                    odom["vx"], odom["vy"], odom["wz"],
                    *(joints[name] for name in required_joints),
                ], dtype=np.float32)
                if image["encoding"] not in ("rgb8", "bgr8"):
                    raise RuntimeError(f"unsupported image encoding {image['encoding']}")
                expected_hw = features["observation.images.wrist_rgb"]["shape"][:2]
                if (image["height"], image["width"]) != expected_hw:
                    raise ExportRejected(
                        f"episode {episode['episode_id']} image size {image['width']}x{image['height']} does not match dataset feature {expected_hw[1]}x{expected_hw[0]}"
                    )
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
                    "time_ns": t,
                    "clock_domain": meta.get("clock_domain"),
                    "image_stamp_ns": image["stamp"],
                    "image_received_ns": image["received_ns"],
                    "image_stamp_source": image["stamp_source"],
                    "state_stamp_ns": state["stamp"],
                    "state_received_ns": state["received_ns"],
                    "state_stamp_source": state["stamp_source"],
                    "base_stamp_ns": odom["stamp"],
                    "base_received_ns": odom["received_ns"],
                    "base_stamp_source": odom["stamp_source"],
                    "action_time_ns": action_stamp,
                    "action_received_ns": event["_received_ns"],
                    "action_age_s": round(action_age, 4),
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
                "started_time_ns": original_start,
                "aligned_start_time_ns": start,
                "ended_time_ns": end,
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


def _job_log_tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_text(errors="replace")
    except FileNotFoundError:
        return ""
    return data[-limit:]


def _job_public(job: dict) -> dict:
    out = {key: value for key, value in job.items() if key != "proc"}
    out.pop("result_path", None)
    out.pop("log_path", None)
    return out


def _refresh_job(job: dict) -> dict:
    if job.get("status") != "running":
        return job
    proc = job.get("proc")
    if proc is None or proc.poll() is None:
        return job
    job["exit_code"] = proc.returncode
    job["finished_wall"] = time.time()
    result_path = Path(job["result_path"])
    result = None
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
        except Exception:
            result = None
    if result and result.get("status") == "completed":
        job["status"] = "completed"
        job["report"] = result.get("report")
    else:
        job["status"] = "failed"
        job["error"] = (result or {}).get("error") or _job_log_tail(Path(job["log_path"])) or f"worker exited {proc.returncode}"
        job["error_type"] = (result or {}).get("error_type")
    return job


@router.post("/export")
def export(body: dict):
    session_id = str(body.get("session_id", ""))
    include_failed = bool(body.get("include_failed", False))
    _guard(lambda: _check_exportable(session_id))
    with EXPORT_JOB_LOCK:
        for existing in EXPORT_JOBS.values():
            if existing.get("session_id") == session_id and existing.get("status") == "running":
                raise HTTPException(status_code=409, detail=f"export already running for {session_id}: {existing['job_id']}")
    job_id = f"exp_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    job_dir = EXPORT_JOB_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    result_path = job_dir / "result.json"
    log_path = job_dir / "worker.log"
    app_root = Path(__file__).resolve().parents[1]
    cmd = [
        sys.executable, "-m", "lelab.ros_export_worker", session_id,
        "1" if include_failed else "0", str(result_path),
    ]
    log = open(log_path, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(app_root), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:
        log.close()
        raise
    job = {
        "job_id": job_id, "session_id": session_id, "include_failed": include_failed,
        "status": "running", "pid": proc.pid, "started_wall": time.time(),
        "proc": proc, "result_path": str(result_path), "log_path": str(log_path),
    }
    with EXPORT_JOB_LOCK:
        EXPORT_JOBS[job_id] = job
    return _job_public(job)


@router.get("/export/status/{job_id}")
def export_status(job_id: str):
    with EXPORT_JOB_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown export job {job_id}")
        _refresh_job(job)
        out = _job_public(job)
    if out["status"] == "running":
        out["log_tail"] = _job_log_tail(Path(job["log_path"]))
    return out


@router.post("/export/cancel/{job_id}")
def export_cancel(job_id: str):
    with EXPORT_JOB_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown export job {job_id}")
        if job["status"] != "running":
            return _job_public(job)
        proc: subprocess.Popen = job["proc"]
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
        job["status"] = "canceled"
        job["exit_code"] = proc.returncode
        job["finished_wall"] = time.time()
        export_dir = UPPER_EXPORTS / job["session_id"]
        if export_dir.is_dir():
            for staging in export_dir.glob("staging-*"):
                shutil.rmtree(staging, ignore_errors=True)
        return _job_public(job)


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
