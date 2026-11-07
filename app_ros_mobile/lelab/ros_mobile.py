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
from .follower_calibration_asset import (build_follower_asset, default_follower_identity,
                                         invalidate_mappings_for_follower, validate_evidence)
from .hardware_lease import HardwareLeaseError, get_hardware_lease_manager
from . import hybrid_export_schema as hybrid_schema
from .hybrid_recording import (
    CarRouteUnavailable,
    HybridRecordingController,
    HttpCarClient,
    InMemoryRecordingCarClient,
    RecordingError,
    describe_recording,
)

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
    except TimeoutError as exc:
        # A stalled tunnel reads as a socket timeout, not a URLError. Treat it as
        # "unknown car state": the caller must fail closed instead of assuming the
        # command landed.
        raise BridgeError(503, f"car agent timeout after {timeout}s: {exc}") from exc
    except OSError as exc:
        raise BridgeError(503, f"car agent connection error: {exc}") from exc


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


PREVIEW_FRAME_SEQ_HEADER = "X-LeLab-Frame-Seq"
PREVIEW_SOURCE_STAMP_HEADER = "X-LeLab-Source-Stamp-Ns"
_PREVIEW_FRAME_LOCK = threading.Lock()
_PREVIEW_FRAME_STATE = {"frame_seq": None, "source_stamp_ns": None}


class PreviewFrameError(RuntimeError):
    def __init__(self, reason: str, detail: str = "", *, status_code: int = 409):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.status_code = status_code


def reset_preview_frame_state() -> None:
    """Forget process-local frame monotonicity (tests and explicit recovery only)."""
    with _PREVIEW_FRAME_LOCK:
        _PREVIEW_FRAME_STATE.update(frame_seq=None, source_stamp_ns=None)


def fetch_preview_frame(timeout: float = 4.0,
                        frame_seq: int | None = None) -> tuple[bytes, dict[str, str]]:
    """Fetch one JPEG plus the car's raw frame identity headers.

    The upper host never creates a frame sequence.  A missing upstream header is
    returned as missing so the route can fail closed rather than inventing one.
    """
    suffix = "" if frame_seq is None else f"?frame_seq={int(frame_seq)}"
    with urllib.request.urlopen(AGENT_URL + "/preview.jpg" + suffix, timeout=timeout) as response:
        data = response.read()
        headers = {
            name: value
            for name in (PREVIEW_FRAME_SEQ_HEADER, PREVIEW_SOURCE_STAMP_HEADER)
            if (value := response.headers.get(name)) is not None
        }
    return data, headers


def fetch_preview_jpeg(timeout: float = 4.0) -> bytes:
    """Backward-compatible bytes-only wrapper around :func:`fetch_preview_frame`."""
    data, _headers = fetch_preview_frame(timeout=timeout)
    return data


def _header_value(headers: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _nonnegative_header_int(headers: dict[str, str], name: str) -> int:
    raw = _header_value(headers, name)
    if raw is None or not raw.strip():
        raise PreviewFrameError("missing_frame_header", f"{name} is required", status_code=502)
    token = raw.strip()
    if not token.isascii() or not token.isdigit():
        raise PreviewFrameError("invalid_frame_header", f"{name}={raw!r}", status_code=502)
    return int(token)


def _current_video_frame_seq() -> int | None:
    """Read the server status projection; never derive a sequence locally."""
    try:
        video = get_hybrid_facade().status().get("video")
    except Exception:  # noqa: BLE001 - this route still supports non-hybrid hardware mode
        return None
    if not isinstance(video, dict):
        return None
    value = video.get("frame_seq")
    if value is None:
        value = video.get("preview_seq")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PreviewFrameError("status_frame_seq_invalid", repr(value), status_code=503)
    return value


def validate_preview_frame_headers(headers: dict[str, str], *,
                                   expected_frame_seq: int | None = None) -> tuple[int, int]:
    """Validate and advance the shared JPEG/status frame identity.

    A repeated frame sequence is stale even if its bytes or source stamp changed.
    New sequences must advance the source stamp.  This gate is process-wide so a
    cached JPEG cannot be presented as a new frame merely because it was fetched
    again.
    """
    frame_seq = _nonnegative_header_int(headers, PREVIEW_FRAME_SEQ_HEADER)
    source_stamp_ns = _nonnegative_header_int(headers, PREVIEW_SOURCE_STAMP_HEADER)
    if expected_frame_seq is not None and frame_seq != expected_frame_seq:
        raise PreviewFrameError(
            "frame_seq_mismatch",
            f"JPEG frame_seq={frame_seq}, status/request frame_seq={expected_frame_seq}",
        )
    with _PREVIEW_FRAME_LOCK:
        last_seq = _PREVIEW_FRAME_STATE["frame_seq"]
        last_stamp = _PREVIEW_FRAME_STATE["source_stamp_ns"]
        if last_seq is not None and frame_seq < last_seq:
            # A restarted car agent begins a new sequence at zero.  Accept that
            # rollover only when the measured source stamp proves the pixels are
            # newer; an old cached frame cannot pass this branch.
            if last_stamp is None or source_stamp_ns <= last_stamp:
                raise PreviewFrameError(
                    "stale_frame_seq",
                    f"{frame_seq} < {last_seq} without a newer source stamp",
                )
            _PREVIEW_FRAME_STATE.update(
                frame_seq=frame_seq, source_stamp_ns=source_stamp_ns)
            return frame_seq, source_stamp_ns
        if last_seq is not None and frame_seq == last_seq:
            if source_stamp_ns != last_stamp:
                raise PreviewFrameError(
                    "frame_header_inconsistent",
                    f"frame_seq={frame_seq} repeated with a different source stamp",
                )
            raise PreviewFrameError("stale_frame_seq", f"frame_seq={frame_seq} repeated")
        if last_stamp is not None and source_stamp_ns <= last_stamp:
            raise PreviewFrameError(
                "frame_header_inconsistent",
                f"source stamp {source_stamp_ns} did not advance past {last_stamp}",
            )
        _PREVIEW_FRAME_STATE.update(frame_seq=frame_seq, source_stamp_ns=source_stamp_ns)
        return frame_seq, source_stamp_ns


@router.get("/preview.jpg")
def preview_jpg(frame_seq: int | None = None):
    try:
        if frame_seq is None:
            # Legacy preview URLs carry no exact-frame contract.  Fetching them
            # must not advance the monotonic state used by the hybrid panel, or
            # a concurrent thumbnail can make the panel's exact frame look stale.
            data, headers = fetch_preview_frame()
        else:
            data, headers = fetch_preview_frame(frame_seq=frame_seq)
            validate_preview_frame_headers(headers, expected_frame_seq=frame_seq)
    except PreviewFrameError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=f"preview frame rejected: {exc.reason}: {exc.detail}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"preview unavailable: {exc}") from exc
    fetched_at = time.time()
    frame_header = _header_value(headers, PREVIEW_FRAME_SEQ_HEADER)
    stamp_header = _header_value(headers, PREVIEW_SOURCE_STAMP_HEADER)
    assert frame_header is not None and stamp_header is not None
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-LeLab-Fetched-At": f"{fetched_at:.3f}",
            PREVIEW_FRAME_SEQ_HEADER: frame_header,
            PREVIEW_SOURCE_STAMP_HEADER: stamp_header,
            "Access-Control-Expose-Headers": (
                f"{PREVIEW_FRAME_SEQ_HEADER}, {PREVIEW_SOURCE_STAMP_HEADER}, X-LeLab-Fetched-At"
            ),
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


# ---------------------------------------------------------------------------
# SO-101 leader → mobile follower teleoperation (phase 1: arm + gripper only)
# ---------------------------------------------------------------------------

def _teleop_guard(fn):
    from .mobile_teleop_coordinator import BridgeError as TeleopBridgeError

    try:
        return fn()
    except TeleopBridgeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/teleop/status")
def teleop_status():
    from .mobile_teleop_coordinator import get_mobile_teleop_coordinator

    return _teleop_guard(lambda: get_mobile_teleop_coordinator().status())


@router.get("/teleop/devices")
def teleop_devices():
    from .mobile_leader_reader import list_leader_ports

    return {"ports": list_leader_ports(), "note": "leader arms hang off this host's USB; serial access is not network reachability"}


@router.get("/teleop/leader-calibrations")
def teleop_leader_calibrations():
    """List the SO-101 calibrations this host already has, so layer A reuses them instead of retyping."""
    from .mobile_leader_reader import list_leader_calibrations

    return _teleop_guard(list_leader_calibrations)


@router.get("/teleop/calibration")
def teleop_calibration(kind: str, identity: str):
    from .mobile_teleop import CalibrationStore, default_store_root

    if kind not in CalibrationStore.KINDS:
        raise HTTPException(status_code=400, detail=f"unknown calibration kind {kind!r}")

    def run():
        record = CalibrationStore(default_store_root()).load(kind, identity)
        return {**record.as_dict(), "data": record.data}

    return _teleop_guard(run)


@router.get("/teleop/calibration/readiness")
def teleop_calibration_readiness(leader: str, follower: str, mapping: str):
    from .mobile_teleop import CalibrationStore, default_store_root

    return _teleop_guard(lambda: CalibrationStore(default_store_root()).readiness(leader, follower, mapping))


TELEOP_EXAMPLE_DIR = Path(os.environ.get(
    "LELAB_MOBILE_TELEOP_EXAMPLES",
    str(Path(__file__).resolve().parents[2] / "configs" / "mobile_teleop"),
))


@router.get("/teleop/calibration/example")
def teleop_calibration_example(kind: str):
    """Serve the shipped layer template so the wizard starts from documented units, not a blank box."""
    from .mobile_teleop import CalibrationStore

    if kind not in CalibrationStore.KINDS:
        raise HTTPException(status_code=400, detail=f"unknown calibration kind {kind!r}")
    path = TELEOP_EXAMPLE_DIR / f"{kind}.example.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no example for {kind} at {path}")
    return {"kind": kind, "path": str(path), "payload": json.loads(path.read_text(encoding="utf-8"))}


@router.post("/teleop/calibration")
def teleop_calibration_save(body: dict):
    from .mobile_teleop import CalibrationStore, default_store_root

    kind = str(body.get("kind", ""))
    if kind not in CalibrationStore.KINDS:
        raise HTTPException(status_code=400, detail=f"unknown calibration kind {kind!r}")
    identity = str(body.get("identity", ""))
    if not identity:
        raise HTTPException(status_code=400, detail="calibration identity is required")

    def run():
        store = CalibrationStore(default_store_root())
        record = store.save(kind, identity, dict(body.get("payload") or {}))
        return record.as_dict()

    return _teleop_guard(run)


@router.post("/teleop/acquire")
def teleop_acquire(body: dict):
    from .mobile_teleop_coordinator import get_mobile_teleop_coordinator

    def run():
        coord = get_mobile_teleop_coordinator()
        if body.get("port"):
            # The leader must be live before the leases are taken, otherwise the session would
            # hold both leases while the state machine cannot leave `disconnected`.
            opened = coord.open_leader(str(body["port"]), leader_identity=body.get("leader_identity"))
            leader_state = opened.get("leader") or {}
            if not leader_state.get("connected"):
                raise HTTPException(status_code=409,
                                    detail="leader_open_failed: "
                                           + str(leader_state.get("reason") or "unavailable"))
        out = coord.acquire(str(body.get("owner", "browser")), str(body.get("leader_identity", "")),
                            str(body.get("follower_identity", "")), str(body.get("mapping_identity", "")))
        return out

    return _teleop_guard(run)


@router.post("/teleop/release")
def teleop_release(body: dict):
    from .mobile_teleop_coordinator import get_mobile_teleop_coordinator

    return _teleop_guard(lambda: get_mobile_teleop_coordinator().release(str(body.get("reason", "released"))))


@router.post("/teleop/leader")
def teleop_leader(body: dict):
    from .mobile_teleop_coordinator import get_mobile_teleop_coordinator

    def run():
        coord = get_mobile_teleop_coordinator()
        action = body.get("action")
        if action == "open":
            return coord.open_leader(str(body.get("port", "")), body.get("ids"),
                                     body.get("leader_identity"))
        if action == "close":
            return coord.close_leader()
        raise HTTPException(status_code=400, detail=f"unknown leader action {action!r}")

    return _teleop_guard(run)


@router.get("/teleop/preview")
def teleop_preview():
    from .mobile_teleop_coordinator import get_mobile_teleop_coordinator

    return _teleop_guard(lambda: get_mobile_teleop_coordinator().preview_step())


@router.get("/teleop/readiness")
def teleop_readiness():
    from .mobile_teleop_coordinator import get_mobile_teleop_coordinator

    return _teleop_guard(lambda: get_mobile_teleop_coordinator().readiness_report())


@router.post("/teleop/state")
def teleop_state(body: dict):
    from .mobile_teleop_coordinator import get_mobile_teleop_coordinator

    def run():
        coord = get_mobile_teleop_coordinator()
        event = str(body.get("event", ""))
        if event == "align":
            return coord.align()
        if event == "estop":
            return coord.emergency_stop(str(body.get("reason", "browser-estop")))
        if event == "heartbeat":
            return coord.heartbeat()
        return coord.set_state(event)

    return _teleop_guard(run)


@router.post("/teleop/recording")
def teleop_recording(body: dict):
    from .mobile_teleop_coordinator import get_mobile_teleop_coordinator

    action = str(body.pop("action", ""))
    return _teleop_guard(lambda: get_mobile_teleop_coordinator().recording(action, body))


_HYBRID_FACADE = None
_HYBRID_FACADE_LOCK = threading.Lock()


def get_hybrid_facade():
    """Use the coordinator's production public facade.

    The default remains adapter-free, but injected real adapters and explicitly
    marked simulation fixtures travel through the same production route.  Keeping
    the process-wide facade in the coordinator avoids the old split where HTTP used
    a bare ``HybridSession`` while other callers configured ``HybridPublicFacade``.
    """
    global _HYBRID_FACADE
    with _HYBRID_FACADE_LOCK:
        if _HYBRID_FACADE is not None:
            return _HYBRID_FACADE
        if os.environ.get("LELAB_HYBRID_SIMULATED") == "1":
            # Explicit opt-in only.  This installs the same production
            # HybridPublicFacade with every device/transport edge replaced by a
            # fixture marked simulated.  The default remains adapter-free.
            from .hybrid_fixtures import build_fixture_facade
            from .mobile_teleop_coordinator import configure_hybrid_facade

            seed = build_fixture_facade()
            facade = configure_hybrid_facade(
                upper=seed.upper,
                car=seed.car,
                observer=seed.observer,
                driver=seed.driver,
                clock_ns=seed.clock_ns,
                gamepad_deadzone=0.1,
                gamepad_timeout_ns=300_000_000,
            )
            facade.simulated_context = {
                **seed.simulated_context,
                "source": "LELAB_HYBRID_SIMULATED=1",
            }
            _HYBRID_FACADE = facade
            return facade
        if os.environ.get("LELAB_HYBRID_HARDWARE") == "1":
            from .hybrid_hardware import build_hardware_facade
            from .mobile_teleop_coordinator import configure_hybrid_facade

            seed = build_hardware_facade(os.environ.get(
                "LELAB_ROS_AGENT_URL", "http://127.0.0.1:8110"))
            facade = configure_hybrid_facade(
                facade=seed, replace=True,
            )
            _HYBRID_FACADE = facade
            return facade
    try:
        from .mobile_teleop_coordinator import get_hybrid_session_facade

        return get_hybrid_session_facade()
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="hybrid session facade unavailable") from exc


def _hybrid_guard(fn):
    try:
        return _teleop_guard(fn)
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except RecordingError as exc:
        raise HTTPException(status_code=exc.status_code,
                            detail=f"{exc.reason}: {exc.detail}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


_HYBRID_RECORDING = None
_HYBRID_RECORDING_LOCK = threading.Lock()


def get_hybrid_recording() -> HybridRecordingController:
    """Recording controller bound to the same facade lease and the car agent."""
    global _HYBRID_RECORDING
    with _HYBRID_RECORDING_LOCK:
        if _HYBRID_RECORDING is None:
            facade = get_hybrid_facade()
            simulated = bool(getattr(facade, "_is_simulated", lambda: False)())
            offline_agent = simulated and getattr(facade, "offline_agent_recording", False) is True
            if offline_agent:
                from urllib.parse import urlsplit
                car_url = str(getattr(getattr(facade, "car", None), "agent_url", "")).rstrip("/")
                parsed = urlsplit(car_url)
                if car_url != AGENT_URL or parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
                    raise HTTPException(status_code=503, detail="offline_agent_not_loopback")
                status = _http("GET", "/status", timeout=2.0)
                if (status.get("simulated") is not True or
                        status.get("profile") != "hardware_hybrid_manual_v1_offline6d"):
                    raise HTTPException(status_code=503, detail="offline_agent_identity_mismatch")
            if simulated and not offline_agent:
                car = InMemoryRecordingCarClient(clock_ns=facade.clock_ns, simulated=True)
            elif getattr(getattr(facade, "car", None), "agent_url", None):
                def post_with_session_lease(path, body):
                    if str(facade.car.agent_url).rstrip("/") != AGENT_URL:
                        raise BridgeError(409, "hybrid agent URL mismatch")
                    token = getattr(facade.car, "token", None)
                    if not token:
                        raise BridgeError(409, "hybrid car lease unavailable")
                    return _http("POST", path, body, token)
                car = HttpCarClient(post_with_session_lease)
            else:
                car = HttpCarClient(_agent_post)
            _HYBRID_RECORDING = HybridRecordingController(
                facade, car, offline_6d=offline_agent)
            bind = getattr(facade, "bind_recording_controller", None)
            if callable(bind):
                bind(_HYBRID_RECORDING, replace=True)
        return _HYBRID_RECORDING


def _validate_hybrid_request(facade: Any, body: dict, *, allow_estop: bool = False) -> None:
    """Apply the production facade's exact session/epoch gate when available."""
    validate = getattr(facade, "validate_session_request", None)
    if callable(validate):
        try:
            validate(body, allow_estop=allow_estop)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _validate_hybrid_calibration_request(facade: Any, body: dict) -> None:
    validate = getattr(facade, "validate_session_request_if_active", None)
    if callable(validate):
        try:
            validate(body)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/hybrid/status")
def hybrid_status():
    def run():
        facade = get_hybrid_facade()
        # Binding here makes status.recording the controller snapshot even before
        # the first recording action.
        get_hybrid_recording()
        return facade.status()

    return _hybrid_guard(run)


@router.get("/hybrid/capabilities")
def hybrid_capabilities():
    facade = get_hybrid_facade()
    capabilities = getattr(facade, "capabilities", None)
    return _hybrid_guard(lambda: capabilities() if callable(capabilities)
                         else facade.calibration_capabilities())


@router.post("/hybrid/session")
def hybrid_session(body: dict):
    action = body.get("action")
    if action not in {"acquire", "release"}:
        raise HTTPException(status_code=400, detail="unknown hybrid session action")
    facade = get_hybrid_facade()
    if action == "acquire":
        return _hybrid_guard(lambda: facade.acquire_session(dict(body)))
    _validate_hybrid_request(facade, body)

    def release():
        reason = str(body.get("reason") or "released")
        release_warning = None
        controller = get_hybrid_recording()
        release_recording = getattr(controller, "release_for_session", None)
        if callable(release_recording):
            try:
                release_recording({**body, "reason": reason})
            except (RecordingError, RuntimeError) as exc:
                # The control lease still has to be released.  Report the recorder
                # cleanup failure instead of hiding it or retaining stale ownership.
                release_warning = f"{type(exc).__name__}:{exc}"
        out = facade.release_session(
            reason, session_id=body.get("session_id"),
            session_epoch=body.get("session_epoch"))
        if release_warning:
            out["release_warning"] = release_warning
        return out

    return _hybrid_guard(release)


@router.post("/hybrid/heartbeat")
def hybrid_heartbeat(body: dict):
    facade = get_hybrid_facade()
    _validate_hybrid_request(facade, body)
    return _hybrid_guard(lambda: facade.heartbeat())


@router.post("/hybrid/clear-estop")
def hybrid_clear_estop_unbound(body: dict, request: Request):
    del body
    facade = get_hybrid_facade()
    return _hybrid_guard(lambda: facade.clear_estop_unbound(owner=_owner(request)))


@router.post("/hybrid/control")
def hybrid_control(body: dict):
    action = body.get("action")
    if action not in {"preview", "align", "arm", "engage", "release_clutch", "pause_all",
                      "estop", "clear_estop", "enable_base", "lock_base"}:
        raise HTTPException(status_code=400, detail="unknown hybrid control action")
    facade = get_hybrid_facade()
    # Emergency stop is the explicit preemption exception: it must not wait for or
    # depend on an identity that a damaged/old client may no longer have.
    _validate_hybrid_request(facade, body, allow_estop=(action == "estop"))
    if action == "estop":
        # The stop must not wait for a recorder save; the save is deferred.
        controller = get_hybrid_recording()
        return _hybrid_guard(lambda: controller.estop(str(body.get("reason") or "operator")))
    return _hybrid_guard(
        lambda: facade.preview(dict(body)) if action == "preview"
        else facade.set_arm_state(dict(body)))


@router.get("/hybrid/home")
def hybrid_home_status():
    facade = get_hybrid_facade()
    status = getattr(facade, "home_status", None)
    if not callable(status):
        raise HTTPException(status_code=503, detail="homing unavailable")
    return _hybrid_guard(status)


@router.post("/hybrid/home")
def hybrid_home(body: dict):
    action = body.get("action")
    if action not in {"start", "cancel"}:
        raise HTTPException(status_code=400, detail="unknown homing action")
    facade = get_hybrid_facade()
    _validate_hybrid_request(facade, body)
    method = getattr(facade, "start_home" if action == "start" else "cancel_home", None)
    if not callable(method):
        raise HTTPException(status_code=503, detail="homing unavailable")
    return _hybrid_guard(lambda: method(dict(body)))


@router.get("/hybrid/mapping")
def hybrid_mapping():
    facade = get_hybrid_facade()
    getter = getattr(facade, "mapping_directions", None)
    if not callable(getter):
        raise HTTPException(status_code=503, detail="mapping editor unavailable")
    return _hybrid_guard(getter)


@router.post("/hybrid/mapping/directions")
def hybrid_mapping_directions(body: dict):
    facade = get_hybrid_facade()
    setter = getattr(facade, "save_mapping_directions", None)
    if not callable(setter):
        raise HTTPException(status_code=503, detail="mapping editor unavailable")
    return _hybrid_guard(lambda: setter(dict(body)))


@router.post("/hybrid/calibration")
def hybrid_calibration(body: dict):
    action, kind = body.get("action"), body.get("kind")
    if action not in {"validate", "save"} or kind not in {"base", "follower", "leader", "mapping", "gamepad", "camera"}:
        raise HTTPException(status_code=400, detail="unknown calibration action or kind")
    facade = get_hybrid_facade()
    _validate_hybrid_calibration_request(facade, body)
    if action == "save":
        return _hybrid_guard(lambda: facade.calibration("save", kind, body))
    return _hybrid_guard(lambda: facade.calibration("validate", kind, body))


@router.post("/hybrid/recording")
def hybrid_recording(body: dict):
    action = body.get("action")
    if action not in {"start_session", "start_episode", "set_phase", "end_episode", "stop_session"}:
        raise HTTPException(status_code=400, detail="unknown hybrid recording action")
    controller = get_hybrid_recording()
    facade = getattr(controller, "facade", None) or get_hybrid_facade()
    _validate_hybrid_request(facade, body)
    payload = {k: v for k, v in body.items() if k != "action"}
    return _hybrid_guard(lambda: controller.dispatch(action, payload))


@router.get("/hybrid/recording/status")
def hybrid_recording_status():
    controller = get_hybrid_recording()
    return _hybrid_guard(lambda: describe_recording(controller))


@router.post("/hybrid/probe-leader")
def hybrid_probe_leader():
    facade = get_hybrid_facade()
    probe = getattr(getattr(facade, "upper", None), "probe", None)
    if not callable(probe):
        raise HTTPException(status_code=503, detail="hardware leader probe unavailable")
    return _hybrid_guard(probe)


FOLLOWER_CALIBRATION_ACTIONS = {
    "start": "/calibration/follower/start",
    "status": "/calibration/follower/status",
    "manual-ranges": "/calibration/follower/manual-ranges",
    "torque": "/calibration/follower/torque",
    "target": "/calibration/follower/gripper/target",
    "mark": "/calibration/follower/gripper/mark",
    "gripper-stop": "/calibration/follower/gripper/stop",
    "stop": "/calibration/follower/stop",
}


def _hybrid_calibration_store():
    facade = get_hybrid_facade()
    store = getattr(facade, "calibration_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="hybrid calibration store unavailable")
    return facade, store


def commit_follower_calibration(body: dict) -> dict:
    """Build the follower asset from car evidence; the browser only supplies the job.

    Everything that makes an asset trustworthy - measured, timestamps, board and
    controller identity, servo ids, method - is copied from the car's evidence
    document.  A browser cannot reach this function: the route is the only caller
    and it never forwards a payload.
    """
    facade, store = _hybrid_calibration_store()
    if getattr(facade, "session_id", None) is not None:
        raise HTTPException(status_code=409, detail="release_session_before_calibration_write")
    request = {key: body[key] for key in ("token", "job_id", "generation")
               if body.get(key) is not None}
    if "token" not in request:
        raise HTTPException(status_code=400, detail="calibration token is required")
    request["include_evidence"] = True
    try:
        status = _http("POST", "/calibration/follower/status", request, timeout=20.0)
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    evidence = status.get("evidence") if isinstance(status, dict) else None
    reasons = validate_evidence(evidence, now_ts=body.get("now_ts"))
    if reasons:
        return {"saved": False, "kind": "follower", "reasons": reasons,
                "phase": status.get("phase") if isinstance(status, dict) else None,
                "measured": False}
    identity = default_follower_identity()
    try:
        asset = build_follower_asset(evidence, identity=identity, now_ts=body.get("now_ts"))
        record = store.save_validated("follower", identity, asset)
    except ValueError as exc:
        return {"saved": False, "kind": "follower", "reasons": [str(exc)], "measured": False}
    invalidated = invalidate_mappings_for_follower(
        store.root, follower_identity=identity, follower_version=record.version)
    return {
        "saved": True,
        "kind": "follower",
        "identity": identity,
        "measured": True,
        "schema_version": asset["schema_version"],
        "record": record.as_dict(),
        "mapping_invalidated": invalidated,
        "requires_repair": bool(invalidated),
        "next": ("re_pair_leader_and_follower_before_preview" if invalidated else "preview"),
    }


@router.post("/hybrid/follower-calibration/{action}")
def hybrid_follower_calibration(action: str, body: dict | None = None):
    body = dict(body or {})
    if action == "commit":
        return _hybrid_guard(lambda: commit_follower_calibration(body))
    if action not in FOLLOWER_CALIBRATION_ACTIONS:
        raise HTTPException(status_code=404, detail="unknown follower calibration action")
    return _hybrid_guard(
        lambda: _http("POST", FOLLOWER_CALIBRATION_ACTIONS[action], body, timeout=20.0))


@router.get("/hybrid/follower-calibration/asset")
def hybrid_follower_calibration_asset(identity: str | None = None):
    """Read back the stored follower asset (used after save and after a refresh)."""
    _facade, store = _hybrid_calibration_store()
    identity = identity or default_follower_identity()
    try:
        record = store.load("follower", identity)
    except FileNotFoundError:
        return {"exists": False, "identity": identity}
    except ValueError as exc:
        return {"exists": False, "identity": identity, "reasons": [str(exc)]}
    return {
        "exists": True,
        "identity": identity,
        "schema_version": record.data.get("schema_version"),
        "version": record.version,
        "sha256": record.sha256,
        "asset": record.data,
    }


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
    return _guard(lambda: _agent_post("/recording/end_episode", {
        "discarded": bool(body.get("discarded", False)), "reason": body.get("reason", ""),
        "success": body.get("success"), "note": body.get("note", "")}))


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
    local = list_upper_sessions()
    try:
        remote = _http("GET", "/sessions", timeout=5.0).get("sessions", [])
        agent_error = None
    except BridgeError as exc:
        # The car listing is best-effort: losing the tunnel must not hide the copies that are
        # already verified on this host, which is exactly what an operator needs after a drop.
        remote, agent_error = [], exc.detail
    merged = []
    seen: set[str] = set()
    for item in remote:
        sid = item.get("session_id")
        seen.add(sid)
        local_item = next((x for x in local if x["session_id"] == sid), None)
        merged.append({**item, "upper": local_item})
    for item in local:
        if item["session_id"] not in seen:
            merged.append({"session_id": item["session_id"], "status": "local-only", "upper": item})
    out = {"sessions": merged}
    if agent_error:
        out["agent_error"] = agent_error
    return out


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


def _open_bag_reader(bag_dir: Path):
    """Open a rosbag2 bag with a fallback typestore.

    Humble sqlite3 bags carry no embedded type definitions, and recent rosbags
    releases refuse to read them unless a default typestore is supplied. Self
    describing bags keep using their own types, so the fallback is harmless.
    """
    from rosbags.highlevel import AnyReader
    from rosbags.typesys import Stores, get_typestore

    return AnyReader([bag_dir], default_typestore=get_typestore(Stores.LATEST))


def _verify_synced_session(session_dir: Path, manifest: dict) -> dict:
    if manifest.get("status") != "raw_ready" or manifest.get("ready_eligible") is not True:
        raise BridgeError(409, f"session is not ready_eligible: {manifest.get('status')}/{manifest.get('error')}")
    if int(manifest.get("bag_exit_code", -1)) != 0 or manifest.get("bag_readable") is not True:
        raise BridgeError(409, "recorder did not stop normally or bag is not readable")
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BridgeError(422, "manifest missing on upper host")
    if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        raise BridgeError(422, "transferred manifest differs from agent manifest")
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
    try:
        with _open_bag_reader(bag_dir) as reader:
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
        try:
            with urllib.request.urlopen(AGENT_URL + f"/sessions/{session_id}/archive", timeout=120) as response, open(archive, "wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # A big session can outrun the tunnel; the staging copy is discarded below, so the
            # upper side never publishes a half-transferred session.
            partial = archive.stat().st_size if archive.exists() else 0
            raise BridgeError(503, f"archive transfer interrupted after {partial} bytes: {exc}") from exc
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
        # Validate the complete staged copy before publishing it.
        verification = _verify_synced_session(extract, manifest)
        os.replace(extract, final)
        shutil.rmtree(staging, ignore_errors=True)
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


ARM_GRIPPER_SCHEMA = hybrid_schema.SCHEMA_ARM_GRIPPER_6DOF
HYBRID_SCHEMA = hybrid_schema.SCHEMA_MOBILE_HYBRID_9D
ARM_JOINTS = hybrid_schema.ARM_JOINTS


def _export_schema(meta: dict) -> dict[str, Any]:
    """Derive the LeRobot feature layout from the session's own profile metadata.

    The nine-dimension simulation layout stays the default; a hardware teleop session
    exports arm + gripper only, and never zero-fills the locked chassis.
    """
    features = meta.get("features") or {}
    state = features.get("state") or {}
    action = features.get("action") or {}
    names = state.get("names") or []
    if meta.get("export_schema") == HYBRID_SCHEMA:
        return {
            "kind": HYBRID_SCHEMA, "dims": 9, "action_form": "hybrid_commands",
            "include_base": True,
            "state_joints": list(hybrid_schema.STATE_JOINTS),
            "action_joints": list(hybrid_schema.STATE_JOINTS),
            "state_labels": list(hybrid_schema.STATE_9D_FIELDS),
            "action_labels": list(hybrid_schema.ACTION_9D_FIELDS),
            "state_units": list(hybrid_schema.STATE_9D_UNITS),
            "action_units": list(hybrid_schema.ACTION_9D_UNITS),
            "state_source": "verified_base_estimate_and_servo_position_readback",
            "action_source": "base_velocity_command_and_final_joint_targets",
            "image_name": (features.get("image") or {}).get("name") or "observation.images.wrist_real",
            "fps": float(features.get("fps") or 10.0),
            "robot_type": "lelab_mobile_hybrid_manual", "requires_manual_label": True,
        }
    if meta.get("export_schema") == ARM_GRIPPER_SCHEMA:
        expected = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
        if names != expected or (action.get("names") is not None and action["names"] != expected):
            raise HTTPException(status_code=409, detail="six-dimensional schema requires joint1..joint5 and r_joint in order")
        dims = len(names)
        return {
            "kind": ARM_GRIPPER_SCHEMA,
            "dims": dims,
            "action_form": "six_dof_commands",
            "include_base": False,
            "state_joints": list(names),
            "state_units": state.get("units") or ["rad"] * dims,
            # The action columns are joint targets in the same unit as the state; the
            # dataset feature info has to say so, or a reader can only guess.
            "action_units": (action.get("units") or ["rad"] * dims),
            "state_source": state.get("source") or "measured_follower_joint_states",
            "action_joints": list(action.get("names") or names),
            "action_source": action.get("source") or "gateway_sent_target_after_clamp",
            "image_name": (features.get("image") or {}).get("name") or "observation.images.wrist_real",
            "fps": float((features.get("fps") or 10.0)),
            "robot_type": "lelab_mobile_arm_gripper_phase1",
            "requires_manual_label": True,
        }
    return {
        "kind": "mobile_9dof_sim",
        "dims": 9,
        "action_form": "nine_dof_events",
        "include_base": True,
        "state_joints": ["joint1", "joint2", "joint3", "joint4", "joint5", "gripper_base_joint"],
        "state_units": ["rad"] * 6,
        # The simulation layout's first three columns are base velocities, not joints.
        "action_units": ["m/s", "m/s", "rad/s"] + ["rad"] * 6,
        "state_source": "joint_states_and_model_odom",
        "action_joints": ["joint1", "joint2", "joint3", "joint4", "joint5", "gripper_base_joint"],
        "action_source": "gateway_applied_target_with_zero_order_hold",
        # keep the names already published by earlier simulation exports unchanged
        "state_labels": ["base_vx", "base_vy", "base_wz", "joint1.position", "joint2.position",
                         "joint3.position", "joint4.position", "joint5.position", "gripper_base.position"],
        "action_labels": ["base_vx", "base_vy", "base_wz", "joint1.target", "joint2.target",
                          "joint3.target", "joint4.target", "joint5.target", "gripper_base.target"],
        "image_name": "observation.images.wrist_rgb",
        "fps": 10.0,
        "robot_type": "lelab_mobile_mecanum_5dof",
        "requires_manual_label": False,
    }


def _state_names(schema: dict) -> list[str]:
    return schema.get("state_labels") or (
        (["base_vx", "base_vy", "base_wz"] if schema["include_base"] else [])
        + [f"{j}.position" for j in schema["state_joints"]])


def _action_names(schema: dict) -> list[str]:
    return schema.get("action_labels") or (
        (["base_vx", "base_vy", "base_wz"] if schema["include_base"] else [])
        + [f"{j}.target" for j in schema["action_joints"]])


def _first_image_geometry(bag_dir: Path, image_topic: str) -> tuple[int, int, str]:
    """Take the image geometry from the recorded stream instead of a hardcoded shape."""
    with _open_bag_reader(bag_dir) as reader:
        for connection, _timestamp, raw in reader.messages():
            if connection.topic != image_topic:
                continue
            msg = reader.deserialize(raw, connection.msgtype)
            return int(msg.height), int(msg.width), str(msg.encoding)
    raise ExportRejected(f"no image message found on {image_topic}")


def _read_bag(bag_dir: Path, start_ns: int, end_ns: int, image_topic: str, state_topic: str,
              base_topic: str | None, schema: dict | None = None) -> dict[str, Any]:
    schema = schema or _export_schema({})
    images = []
    states = []
    bases = []
    events = []
    arms = []
    grippers = []
    base_cmds = []
    topics = {t for t in (image_topic, state_topic, base_topic, "/lelab/events") if t}
    with _open_bag_reader(bag_dir) as reader:
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
                if event.get("kind") != "command" or event_time is None:
                    continue
                event_time = int(event_time)
                if not start_ns <= event_time <= end_ns:
                    continue
                event["_time_ns"] = event_time
                event["_received_ns"] = int(timestamp)
                events.append(event)
                target = event.get("target")
                if schema["action_form"] == "six_dof_commands" and isinstance(target, list) and target:
                    if event.get("component") == "arm" and len(target) == schema["dims"] - 1:
                        arms.append(event)
                    elif event.get("component") == "gripper":
                        # The car records the gripper command as the whole mimic set it sent
                        # (gripper_base_joint plus the four derived jaw joints); its first entry is
                        # the clamped master angle, which is the schema's sixth action column.
                        # Requiring exactly one value here made every hardware teleop session
                        # unexportable ("no arm or gripper command events").
                        grippers.append(event)
                elif schema["action_form"] == "hybrid_commands" and isinstance(target, list) and target:
                    if event.get("component") == "base" and len(target) == 3:
                        base_cmds.append(event)
                    elif event.get("component") == "arm" and len(target) == len(ARM_JOINTS):
                        arms.append(event)
                    elif event.get("component") == "gripper":
                        grippers.append(event)
                continue
            if stamp < start_ns or stamp > end_ns:
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
                              "twist_covariance_diag": [float(msg.twist.covariance[i])
                                                        for i in (0, 7, 35)],
                              "pose_covariance_diag": [float(msg.pose.covariance[i])
                                                       for i in (0, 7, 35)],
                              "frame_id": str(getattr(getattr(msg, "header", None), "frame_id", "")),
                              "child_frame_id": str(getattr(msg, "child_frame_id", "")),
                              "pose": [float(msg.pose.pose.position.x), float(msg.pose.pose.position.y), float(msg.pose.pose.position.z)]})
    images.sort(key=lambda x: x["stamp"])
    states.sort(key=lambda x: x["stamp"])
    bases.sort(key=lambda x: x["stamp"])
    events.sort(key=lambda x: x["_time_ns"])
    arms.sort(key=lambda x: x["_time_ns"])
    grippers.sort(key=lambda x: x["_time_ns"])
    base_cmds.sort(key=lambda x: x["_time_ns"])
    return {"images": images, "states": states, "bases": bases, "events": events,
            "arms": arms, "grippers": grippers, "base_cmds": base_cmds}


def _row_topic_times(bag_dir: Path, topic: str) -> list[int]:
    """Every recorded teleop-row stamp in the bag, used to measure the real frame pace."""
    times: list[int] = []
    with _open_bag_reader(bag_dir) as reader:
        for connection, _timestamp, raw in reader.messages():
            if connection.topic != topic:
                continue
            msg = reader.deserialize(raw, connection.msgtype)
            try:
                row = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            if row.get("kind") != "teleop_frame":
                continue
            stamp = row.get("stamp_ns")
            if isinstance(stamp, int):
                times.append(stamp)
    times.sort()
    return times


def _measured_fps(times: list[int]) -> int:
    """The median row spacing, as a whole number the video encoder can accept.

    Under the offline contract a dataset frame is a recorded row, so the dataset's declared
    rate has to be the rate the car actually emitted - not the rate the profile hopes the
    camera runs at.
    """
    import statistics

    if len(times) < 3:
        raise ExportRejected("not enough teleop rows to measure a frame rate")
    steps = [b - a for a, b in zip(times, times[1:]) if 0 < b - a < 500_000_000]
    if not steps:
        raise ExportRejected("teleop rows carry no forward spacing")
    return max(1, round(1e9 / statistics.median(steps)))


def _read_frames(bag_dir: Path, topic: str, start_ns: int, end_ns: int,
                 episode: dict, identity: dict) -> list[dict]:
    """Read the control process's own teleop rows for one episode.

    Under the offline contract these rows are the action and the state: the frame transport
    emits in another process, so no gateway command event exists to read.  A row that
    contradicts the session it was recorded in - another episode, another clock epoch, an
    uncontexted frame, a stamp outside its phase - is refused here rather than smoothed
    over later.
    """
    frames: list[dict] = []
    phases = episode.get("phases") or []
    with _open_bag_reader(bag_dir) as reader:
        for connection, timestamp, raw in reader.messages():
            if connection.topic != topic:
                continue
            msg = reader.deserialize(raw, connection.msgtype)
            try:
                row = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            if row.get("kind") != "teleop_frame":
                continue
            frames.append(row)
    frames.sort(key=lambda row: int(row.get("stamp_ns", 0)))
    accepted: list[dict] = []
    seen_identity: set[tuple] = set()
    for row in frames:
        stamp = int(row.get("stamp_ns", 0))
        if not start_ns <= stamp <= end_ns:
            continue
        if row.get("recording_context") != "agent":
            raise ExportRejected(
                f"episode {episode['episode_id']} row frame_seq={row.get('frame_seq')} was "
                f"recorded without an agent context ({row.get('recording_context')!r}); it "
                "cannot be placed in this episode")
        if row.get("episode_id") != episode["episode_id"]:
            raise ExportRejected(f"episode {episode['episode_id']} row belongs to "
                                 f"{row.get('episode_id')!r}")
        if row.get("clock_epoch") != identity["clock_epoch"]:
            raise ExportRejected(f"episode {episode['episode_id']} row clock_epoch "
                                 f"{row.get('clock_epoch')!r} is not the session's")
        if row.get("data_identity") != hybrid_schema.SIMULATED_DATA_SOURCE:
            raise ExportRejected(f"episode {episode['episode_id']} row data_identity "
                                 f"{row.get('data_identity')!r}")
        key = (row.get("stream_epoch"), row.get("frame_seq"))
        if key in seen_identity:
            raise ExportRejected(f"episode {episode['episode_id']} repeats (stream_epoch, "
                                 f"frame_seq)={key}")
        seen_identity.add(key)
        phase = next((item for item in phases if item["started_time_ns"] <= stamp
                      < (item["ended_time_ns"] or end_ns)), None)
        if phase is None or phase.get("phase") != row.get("phase"):
            raise ExportRejected(
                f"episode {episode['episode_id']} row frame_seq={row.get('frame_seq')} phase "
                f"{row.get('phase')!r} is not the manifest phase "
                f"{(phase or {}).get('phase')!r} at +{(stamp - start_ns) / 1e9:.3f}s")
        accepted.append(row)
    if not accepted:
        raise ExportRejected(f"episode {episode['episode_id']} has no teleop rows in its window")
    return accepted


def _frame_streams(rows: list[dict], episode: dict, schema: dict,
                   identity: dict) -> tuple[list, list, list]:
    """Turn recorded rows into the action and state streams the exporter already consumes.

    The shapes match the gateway command events on purpose, so the freshness, hold and
    phase-boundary rules downstream stay the same code path.  What differs is declared:
    every entry names the row it came from, and the confirmation stays `driver_sent` -
    the control process recorded a target it sent, not a controller acknowledgement.
    """
    names = list(schema["state_joints"])
    arms, grippers, states = [], [], []
    for row in rows:
        stamp = int(row["stamp_ns"])
        action = row.get("action_rad") or {}
        state = row.get("state_rad") or {}
        # The row is the state sample, but the sample was read from the board earlier: its
        # own reported age is what has to stay inside the profile's staleness bound.
        age = row.get("feedback_age_s")
        if isinstance(age, bool) or not isinstance(age, (int, float)) or age < 0:
            raise ExportRejected(
                f"episode {episode['episode_id']} row frame_seq={row.get('frame_seq')} carries "
                f"no feedback age ({age!r}); its state cannot be certified fresh")
        if float(age) > identity["max_state_age_s"]:
            raise ExportRejected(
                f"episode {episode['episode_id']} row frame_seq={row.get('frame_seq')} state is "
                f"{float(age):.3f}s old, past the declared {identity['max_state_age_s']:.3f}s "
                "bound; refusing a stale position")
        missing_action = [name for name in names if name not in action]
        missing_state = [name for name in names if name not in state]
        if missing_action or missing_state:
            raise ExportRejected(
                f"episode {episode['episode_id']} row frame_seq={row.get('frame_seq')} lacks "
                f"{missing_action or missing_state}; refusing to zero-fill an axis")
        marker = {"_time_ns": stamp, "_received_ns": stamp,
                  "contract": "simulated_offline_6d_v1",
                  "state_feedback_age_s": round(float(age), 4),
                  "state_scan_span_s": row.get("feedback_scan_span_s"),
                  "frame_seq": row.get("frame_seq"), "sample_seq": row.get("sample_seq"),
                  "stream_epoch": row.get("stream_epoch"), "phase": row.get("phase"),
                  "episode_id": episode["episode_id"]}
        arm_values = [float(action[name]) for name in names[:-1]]
        leader = row.get("leader_rad")
        pre_clip = row.get("pre_clip_rad")
        arms.append({**marker, "component": "arm", "target": arm_values,
                     "seq": (row.get("channel_seq") or {}).get("arm"),
                     "ttl_ms": (row.get("ttl_ms") or {}).get("arm"),
                     "clutch": (row.get("clutch") or {}).get("arm"),
                     "leader_rad": list(leader) if isinstance(leader, list) else None,
                     "pre_clip": list(pre_clip) if isinstance(pre_clip, list) else None,
                     "source": "teleop_frame_row"})
        grippers.append({**marker, "component": "gripper",
                         "target": [float(action[names[-1]])],
                         "seq": (row.get("channel_seq") or {}).get("gripper"),
                         "ttl_ms": (row.get("ttl_ms") or {}).get("gripper"),
                         "clutch": (row.get("clutch") or {}).get("gripper"),
                         "source": "teleop_frame_row"})
        states.append({"stamp": stamp, "received_ns": stamp, "stamp_source": "teleop_frame_row",
                       "names": names, "positions": [float(state[name]) for name in names],
                       "source_kind": row.get("state_source"),
                       "confirmation": row.get("confirmation")})
    return arms, grippers, states


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


def _command_ttl_ms(event: dict) -> float | None:
    """Per-command lifetime from the event itself (``ttl_ms`` or legacy ``ttl_s``).

    A command with no declared lifetime is not held open-ended: ``None`` makes the
    exporter reject the frame instead of inventing a hold window.
    """
    value = event.get("ttl_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = event.get("ttl_s")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) * 1000.0
    return float(value)


def _resolve_action(schema: dict, data: dict, t: int, action_events: list[dict]):
    """Zero-order hold of the real gateway commands: return (action, stamp_ns, received_ns, extras).

    For the six-dimension teleop layout the arm and gripper command streams are held
    separately and the reported stamp is the *older* of the two, so a long pause on either
    side fails the freshness gate instead of silently extending a stale target.
    """
    if schema["action_form"] == "six_dof_commands":
        arm = _latest_event(data["arms"], t)
        gripper = _latest_event(data["grippers"], t)
        if arm is None or gripper is None:
            return None, 0, 0, {}
        stamp = min(int(arm["_time_ns"]), int(gripper["_time_ns"]))
        received = min(int(arm["_received_ns"]), int(gripper["_received_ns"]))
        # One action column for the gripper: the master angle the gateway clamped and sent. Older
        # and simulated sessions record it as a single value, the car records the whole mimic set
        # with the master first, so both shapes mean target[0] here.
        action = [float(v) for v in arm["target"]] + [float(gripper["target"][0])]
        return action, stamp, received, {
            "arm_command_seq": arm.get("seq"),
            "gripper_command_seq": gripper.get("seq"),
            "arm_action_age_s": round((t - int(arm["_time_ns"])) / 1e9, 4),
            "gripper_action_age_s": round((t - int(gripper["_time_ns"])) / 1e9, 4),
            "clutch": arm.get("clutch") if arm.get("clutch") is not None else gripper.get("clutch"),
            "calibration_version": arm.get("calibration_version") or gripper.get("calibration_version"),
            "pre_clip_rad": arm.get("pre_clip"),
            "leader_rad": arm.get("leader_rad"),
            "leader_gripper_percent": gripper.get("leader_gripper_percent"),
            "clipped_joints": list(arm.get("clipped") or []) + list(gripper.get("clipped") or []),
            "action_semantics": {
                "sent": "post-clamp target the gateway accepted",
                "acknowledged": "unavailable: driver transmission is not a controller acknowledgement",
                "executed": "inferred from later follower feedback, not directly measured",
            },
        }
    if schema["action_form"] == "hybrid_commands":
        base = _latest_event(data["base_cmds"], t)
        arm = _latest_event(data["arms"], t)
        gripper = _latest_event(data["grippers"], t)
        if base is None or arm is None or gripper is None:
            # A missing channel is refused, never zero-filled: zero velocity is a
            # real command, not the absence of one.
            missing = [name for name, value in (("base", base), ("arm", arm), ("gripper", gripper))
                       if value is None]
            return None, 0, 0, {"missing_channels": missing}
        stamp = min(int(base["_time_ns"]), int(arm["_time_ns"]), int(gripper["_time_ns"]))
        received = min(int(base["_received_ns"]), int(arm["_received_ns"]), int(gripper["_received_ns"]))
        action = ([float(v) for v in base["target"][:3]]
                  + [float(v) for v in arm["target"][:len(ARM_JOINTS)]]
                  + [float(gripper["target"][0])])
        if len(action) != 9:
            return None, 0, 0, {"wrong_action_shape": len(action)}
        return action, stamp, received, {
            "base_command_seq": base.get("seq"),
            "arm_command_seq": arm.get("seq"),
            "gripper_command_seq": gripper.get("seq"),
            "command_channel_times_ns": {
                "base": int(base["_time_ns"]), "arm": int(arm["_time_ns"]),
                "gripper": int(gripper["_time_ns"]),
            },
            "command_channel_ttl_ms": {
                "base": _command_ttl_ms(base), "arm": _command_ttl_ms(arm),
                "gripper": _command_ttl_ms(gripper),
            },
            "command_sources": {
                "base": base.get("source"), "arm": arm.get("source"),
                "gripper": gripper.get("source"),
            },
            "clutch": arm.get("clutch") if arm.get("clutch") is not None else gripper.get("clutch"),
            "action_semantics": {
                "sent": "final base velocity command plus post-clamp joint targets",
                "acknowledged": "unavailable: a UART write is not a controller acknowledgement",
                "executed": "not asserted; state carries the measured readback separately",
            },
        }
    event = _latest_event(action_events, t)
    if event is None:
        return None, 0, 0, {}
    return ([float(v) for v in event["action"]], int(event["_time_ns"]),
            int(event["_received_ns"]), {})


class ExportRejected(Exception):
    pass


def _check_command_stream(stream: list[dict], channel: str, episode_id: str) -> None:
    """Sources and sequence continuity of one recorded command stream."""
    last = None
    for event in stream:
        source = event.get("source")
        if source not in hybrid_schema.COMMAND_SOURCES:
            raise ExportRejected(
                f"episode {episode_id} {channel} command has untrusted source {source!r}")
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ExportRejected(f"episode {episode_id} {channel} command has no integer seq")
        if last is not None and seq <= last:
            raise ExportRejected(
                f"episode {episode_id} {channel} command seq not increasing ({last}->{seq})")
        last = seq


def _hybrid_state_vector(state: dict, odom: dict, provenance: dict, t: int,
                         max_age_s: float) -> tuple[list[float], dict[str, Any]]:
    """Nine measured values: verified base estimate plus six servo readbacks.

    Nothing here fills a gap.  A missing joint, an out-of-order joint set, a
    non-finite value or an estimate without covariance rejects the episode
    instead of producing a training row that looks valid.
    """
    names = list(state.get("names") or [])
    positions = list(state.get("positions") or [])
    if len(names) != len(positions):
        raise ExportRejected("joint state name/position length mismatch")
    if len(set(names)) != len(names):
        raise ExportRejected("joint state repeats a joint name")
    expected = list(hybrid_schema.STATE_JOINTS)
    if sorted(names) != sorted(expected):
        missing = [j for j in expected if j not in names]
        extra = [j for j in names if j not in expected]
        raise ExportRejected(f"joint state is not the six hybrid joints (missing={missing}, extra={extra})")
    by_name = dict(zip(names, positions))
    joints = [float(by_name[j]) for j in expected]

    covariance = odom.get("twist_covariance_diag")
    if not isinstance(covariance, (list, tuple)) or len(covariance) != 3:
        raise ExportRejected("base estimate carries no twist covariance; refusing an unverified 9D state")
    for value in covariance:
        if not isinstance(value, (int, float)) or not np_isfinite(value) or float(value) < 0:
            raise ExportRejected(f"base estimate covariance is not finite and non-negative: {covariance}")
    if not provenance.get("base_estimate_has_covariance"):
        raise ExportRejected("declared provenance does not confirm base covariance")

    values = [float(odom["vx"]), float(odom["vy"]), float(odom["wz"]), *joints]
    if not np_isfinite_all(values):
        raise ExportRejected("non-finite measured 9D state")
    base_age = (t - int(odom["stamp"])) / 1e9
    joint_age = (t - int(state["stamp"])) / 1e9
    if base_age > max_age_s or joint_age > max_age_s:
        raise ExportRejected(
            f"stale 9D state: base={base_age:.3f}s joints={joint_age:.3f}s > {max_age_s}s")
    return values, {
        "state_base_source_kind": provenance["base_source_kind"],
        "state_joint_source_kind": provenance["joint_source_kind"],
        "state_calibration_hash": provenance["calibration_hash"],
        "base_covariance_diag": [float(v) for v in covariance],
        "base_frame_id": odom.get("frame_id"),
        "base_child_frame_id": odom.get("child_frame_id"),
        "base_age_s": round(base_age, 4),
        "joint_age_s": round(joint_age, 4),
    }


def np_isfinite(value: Any) -> bool:
    import math

    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def np_isfinite_all(values: list[float]) -> bool:
    import math

    return all(math.isfinite(float(v)) for v in values)


def _verify_phase_events(session_dir: Path, session_id: str, episodes: list[dict], clock_domain: str | None) -> None:
    """Require manifest intervals to match the hashed append-only car event log."""
    phased = {e["episode_id"]: e for e in episodes if e.get("phases")}
    if not phased:
        return
    if not clock_domain:
        raise ExportRejected("phase clock domain missing")
    try:
        lines = (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError) as exc:
        raise ExportRejected("phase event log missing or invalid") from exc
    if len({e.get("episode_id") for e in episodes}) != len(episodes):
        raise ExportRejected("duplicate episode id")
    actual = {eid: [] for eid in phased}
    ids = set()
    for event in events:
        if not isinstance(event, dict):
            raise ExportRejected("invalid event log entry")
        if event.get("kind") not in {"phase_started", "phase_ended"}:
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in ids:
            raise ExportRejected("phase event id missing or duplicated")
        ids.add(event_id)
        eid = event.get("episode_id")
        if eid not in phased:
            continue
        if event.get("session_id") != session_id or event.get("clock_domain") != clock_domain:
            raise ExportRejected(f"episode {eid} phase event session or clock mismatch")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ExportRejected(f"episode {eid} phase event payload missing")
        actual[eid].append((event["kind"], event.get("time_ns"), payload.get("phase")))
    for eid, episode in phased.items():
        expected = []
        for phase in episode["phases"]:
            expected.extend([("phase_started", phase.get("started_time_ns"), phase.get("phase")),
                             ("phase_ended", phase.get("ended_time_ns"), phase.get("phase"))])
        if actual[eid] != expected:
            raise ExportRejected(f"episode {eid} phase events do not match manifest")


def _row_phase_slices(episode: dict, labels: list[str], label_times: list[int],
                      fps: int) -> list[dict]:
    """Per-phase frame ranges taken from the recorded rows, one dataset frame per row.

    A uniform grid would put frames at instants the car never sampled, so under the offline
    contract the ranges come from the rows themselves.  Two rules still apply: each phase
    must appear as one contiguous run, and its rows must lie inside the manifest's own
    window for that phase.
    """
    if not labels or len(labels) != len(label_times):
        raise ExportRejected(f"episode {episode['episode_id']} phase labels do not match frames")
    windows = {item["phase"]: item for item in (episode.get("phases") or [])}
    runs: list[tuple[str, int, int, int, int]] = []
    for index, (name, stamp) in enumerate(zip(labels, label_times)):
        if runs and runs[-1][0] == name:
            previous = runs[-1]
            runs[-1] = (name, previous[1], index + 1, previous[3], stamp)
        else:
            runs.append((name, index, index + 1, stamp, stamp))
    names = [run[0] for run in runs]
    if len(set(names)) != len(names):
        raise ExportRejected(f"episode {episode['episode_id']} phase {names} repeats: a row "
                             "stream may not switch back to an earlier phase")
    slices = []
    for name, first, stop, from_ns, to_ns in runs:
        window = windows.get(name)
        if window is None:
            raise ExportRejected(f"episode {episode['episode_id']} row phase {name!r} is not "
                                 "in the manifest")
        end_ns = window.get("ended_time_ns") or int(episode.get("ended_time_ns") or 0)
        if from_ns < int(window["started_time_ns"]) or to_ns > end_ns:
            raise ExportRejected(f"episode {episode['episode_id']} phase {name} rows fall "
                                 "outside the manifest window")
        slices.append({"phase": name, "started_time_ns": int(window["started_time_ns"]),
                       "ended_time_ns": end_ns, "frame_from": first, "frame_to_exclusive": stop,
                       "frames": stop - first, "frame_count": stop - first,
                       "from_s": round(first / fps, 6), "to_s": round(stop / fps, 6),
                       "row_from_ns": from_ns, "row_to_ns": to_ns,
                       "source": "teleop_frame_row.phase; event paced; one frame per row"})
    return slices


def _phase_slices(episode: dict, aligned_start: int, frame_count: int, fps: int) -> list[dict]:
    """Map car-clock phase boundaries onto this episode's exported frame grid."""
    import math

    phases = episode.get("phases") or []
    episode_start = int(episode.get("started_time_ns", episode.get("started_sim_ns")))
    episode_end = int(episode.get("ended_time_ns") or episode.get("ended_sim_ns") or episode_start)
    step = int(1e9 / fps)
    previous_end = episode_start
    slices = []
    for phase in phases:
        name = phase.get("phase")
        start, end = phase.get("started_time_ns"), phase.get("ended_time_ns")
        if (name not in {"observe", "grasp", "drive", "transport", "release"}
                or not isinstance(start, int) or not isinstance(end, int)
                or start < previous_end or end <= start or end > episode_end):
            raise ExportRejected(f"episode {episode['episode_id']} invalid phase boundaries")
        first = min(frame_count, max(0, math.ceil((start - aligned_start) / step)))
        stop = min(frame_count, max(0, math.ceil((end - aligned_start) / step)))
        slices.append({"phase": name, "started_time_ns": start, "ended_time_ns": end,
                       "frame_from": first, "frame_to_exclusive": stop,
                       "frames": max(0, stop - first),
                       "frame_count": max(0, stop - first),
                       "from_s": first / fps, "to_s": stop / fps,
                       "source": "manifest.episodes.phases; car clock; half-open interval"})
        previous_end = end
    return slices


def _hybrid_feedback_provenance(meta: dict) -> dict[str, Any]:
    """Read the car's declared 9D feedback provenance, or refuse to export.

    The bag holds raw ROS messages; it cannot prove by itself that ``/joint_states``
    came from a servo readback rather than a command echo.  So the car must declare
    its provenance in the session metadata and this function rejects a session whose
    declaration is missing, forbidden, or inconsistent.  A declaration is not a
    measurement, but an absent declaration must never be upgraded to a trusted one.
    """
    try:
        simulation = hybrid_schema.simulation_identity(meta)
    except hybrid_schema.SampleRejected as exc:
        raise HTTPException(status_code=409, detail=f"{exc.reason}: {exc.detail}") from exc
    declared = meta.get("feedback_provenance")
    if not isinstance(declared, dict):
        raise HTTPException(status_code=409, detail=(
            "hybrid 9D export unavailable: session metadata declares no feedback_provenance; "
            "refusing to guess that /joint_states is a measured servo readback"))
    joint_kind = declared.get("joint_source_kind")
    base_kind = declared.get("base_source_kind")
    if joint_kind in hybrid_schema.FORBIDDEN_STATE_SOURCES or \
            base_kind in hybrid_schema.FORBIDDEN_STATE_SOURCES:
        raise HTTPException(status_code=409, detail=(
            f"hybrid 9D export refused: declared state source {joint_kind!r}/{base_kind!r} is a "
            "command echo, not a measurement; a 9D state cannot be built from commands"))
    accepted_joints = (
        (hybrid_schema.SIMULATED_JOINT_SOURCE,)
        if simulation else (hybrid_schema.TRUSTED_JOINT_SOURCE,)
    )
    accepted_bases = (
        (hybrid_schema.SIMULATED_BASE_SOURCE,)
        if simulation else hybrid_schema.TRUSTED_BASE_SOURCES
    )
    if joint_kind not in accepted_joints:
        raise HTTPException(status_code=409, detail=(
            f"hybrid 9D export refused: joint_source_kind={joint_kind!r} is not "
            f"one of {accepted_joints!r}"))
    if base_kind not in accepted_bases:
        raise HTTPException(status_code=409, detail=(
            f"hybrid 9D export refused: base_source_kind={base_kind!r} is not one of "
            f"{accepted_bases!r}"))
    calibration_hash = declared.get("calibration_hash") or declared.get("calibration_bundle_hash")
    if not isinstance(calibration_hash, str) or not calibration_hash:
        raise HTTPException(status_code=409,
                            detail="hybrid 9D export refused: no calibration_hash declared")
    if declared.get("base_estimate_has_covariance") is not True:
        raise HTTPException(status_code=409,
                            detail="hybrid 9D export refused: base estimate declares no covariance")
    max_age = declared.get("max_state_age_s")
    if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or not 0 < float(max_age) <= 0.5:
        raise HTTPException(status_code=409, detail=(
            "hybrid 9D export refused: max_state_age_s must be declared and <= 0.5 s "
            f"(got {max_age!r})"))
    return {
        "joint_source_kind": joint_kind, "base_source_kind": base_kind,
        "calibration_hash": calibration_hash,
        "base_estimate_has_covariance": True,
        "max_state_age_s": float(max_age),
        "joint_topic": declared.get("joint_topic"),
        "base_topic": declared.get("base_topic"),
        "scan_span_ms": declared.get("scan_span_ms"),
        "declared": True,
        "simulated": simulation is not None,
        "simulation_identity": simulation,
    }


def _check_exportable(session_id: str, simulated_offline: bool = False) -> tuple[Path, dict, dict]:
    """Decide whether a synced session may be exported, and under which contract.

    `simulated_offline` is the restricted offline contract.  It does not relax a check: it
    replaces the two claims the hardware path makes - `runtime_mode == hardware_teleop` and
    a profile that permits training export - with a positive proof from the session's own
    metadata that this recording is declared simulation, tagged `simulated_offline`, and has
    `training_export` switched off.  A session that is not exactly that is refused by both
    entries, and the dataset that comes out of this one stays not-training-eligible.
    """
    if not session_id or "/" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="invalid session id")
    session_dir = UPPER_SESSIONS / session_id
    if not (session_dir / "READY").exists():
        raise HTTPException(status_code=409, detail="session is not synced and verified on the upper host")
    manifest = json.loads((session_dir / "manifest.json").read_text())
    if manifest.get("status") != "raw_ready" or manifest.get("ready_eligible") is not True:
        raise HTTPException(status_code=409, detail=f"session is not ready for export: {manifest.get('status')}/{manifest.get('error')}")
    meta = manifest.get("metadata", {})
    declared_schema = meta.get("export_schema")
    if declared_schema not in (None, ARM_GRIPPER_SCHEMA, HYBRID_SCHEMA, "mobile_9dof_sim"):
        raise HTTPException(status_code=409, detail=f"unknown export schema: {declared_schema}")
    schema = _export_schema(meta)
    hybrid = schema["kind"] == HYBRID_SCHEMA
    teleop = schema["kind"] == ARM_GRIPPER_SCHEMA
    identity = None
    if simulated_offline:
        if not teleop:
            raise HTTPException(status_code=409, detail=(
                "the offline simulated 6D contract only covers the arm+gripper six-dimensional "
                f"schema, not {schema['kind']}"))
        try:
            identity = hybrid_schema.simulated_6d_identity(meta)
        except hybrid_schema.SampleRejected as exc:
            raise HTTPException(status_code=409, detail=(
                f"session does not qualify for the offline simulated 6D contract: "
                f"{exc.reason}: {exc.detail}")) from exc
        meta["_simulated_6d"] = identity
    if hybrid:
        allowed_runtime = (
            {"hardware_hybrid_manual"} if meta.get("source") != hybrid_schema.SIMULATED_DATA_SOURCE
            else {"simulated_hybrid_manual", "hybrid_manual_sim"}
        )
        if meta.get("runtime_mode") not in allowed_runtime:
            raise HTTPException(status_code=409, detail=(
                f"hybrid 9D schema requires runtime_mode in {sorted(allowed_runtime)}, "
                f"got {meta.get('runtime_mode')!r}"))
        provenance = _hybrid_feedback_provenance(meta)
        meta["_feedback_provenance"] = provenance
        if provenance.get("simulation_identity") is not None:
            meta["_simulation_identity"] = provenance["simulation_identity"]
    if meta.get("source") != "simulation" and not teleop and not hybrid:
        raise HTTPException(status_code=409,
                            detail="only sessions with a defined action and measured feedback can be exported "
                                   "(read-only hardware sessions stay excluded)")
    if teleop and identity is None and meta.get("runtime_mode") != "hardware_teleop":
        raise HTTPException(status_code=409, detail="teleop export schema requires a hardware_teleop session")
    if identity is None and not bool(meta.get("training_export", True)):
        raise HTTPException(status_code=409, detail="session profile declares training_export=false")
    return session_dir, manifest, meta


def export_session(session_id: str, include_failed: bool = False,
                   simulated_offline: bool = False) -> dict:
    from lerobot.datasets import LeRobotDataset
    import numpy as np

    session_dir, manifest, meta = _check_exportable(session_id, simulated_offline=simulated_offline)
    identity = meta.get("_simulated_6d")
    schema = _export_schema(meta)
    teleop = schema["kind"] == ARM_GRIPPER_SCHEMA
    hybrid = schema["kind"] == HYBRID_SCHEMA
    provenance = meta.get("_feedback_provenance") if hybrid else None
    simulation = meta.get("_simulation_identity") if hybrid else None
    image_topic = (meta.get("camera_topic")
                   or ((meta.get("features") or {}).get("image") or {}).get("topic")
                   or "/lelab/wrist/rgb/image_raw")
    state_topic = meta.get("record_joint_state_topic") or meta.get("joint_state_topic") or "/joint_states"
    base_topic = ((meta.get("base_velocity_topic") or "/lelab/base/odom")
                  if schema["include_base"] else None)
    base_source = meta.get("base_velocity_source") or ("gazebo_model_odometry" if base_topic == "/lelab/base/model_odom" else "legacy_wheel_odometry")
    camera_profile = meta.get("camera_profile") or "legacy_unlabeled"
    camera_extrinsics = meta.get("camera_extrinsics") or {}
    episodes = [e for e in manifest.get("episodes", []) if e.get("status") == "saved"]
    if not include_failed:
        if teleop or hybrid:
            # Hardware manual runs have no simulator ground truth: keep unlabeled and
            # manually labelled successes, drop only episodes explicitly marked as failures.
            episodes = [e for e in episodes if (e.get("evaluation") or {}).get("success") not in (False, "failure")]
        else:
            episodes = [e for e in episodes if (e.get("evaluation") or {}).get("success") is True]
    if not episodes:
        raise HTTPException(status_code=409,
                            detail="no accepted episodes in this session (teleop episodes may be unknown-labelled)"
                            if (teleop or hybrid) else "no accepted successful episodes in this session")
    if hybrid:
        for episode in episodes:
            try:
                slices = hybrid_schema.phase_slices(
                    hybrid_schema.check_phase_timeline(
                        episode.get("phases"),
                        episode_start_ns=int(episode.get("started_time_ns")),
                        episode_end_ns=int(episode.get("ended_time_ns") or 0)),
                    aligned_start_ns=int(episode.get("started_time_ns")),
                    frame_count=0, fps=max(1.0, float(schema["fps"])))
                hybrid_schema.require_observe_grasp(slices, require_frames=False)
            except hybrid_schema.SampleRejected as exc:
                raise HTTPException(status_code=409, detail=(
                    f"hybrid episode {episode.get('episode_id')} not exportable: {exc.reason}: {exc.detail}")) from exc
    _verify_phase_events(session_dir, session_id, episodes, meta.get("clock_domain") or manifest.get("clock_domain"))
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
    height, width, encoding = _first_image_geometry(bag_dir, image_topic)
    if encoding not in ("rgb8", "bgr8"):
        raise HTTPException(status_code=409, detail=f"unsupported image encoding in bag: {encoding}")
    declared_fps = float(schema["fps"])
    # The video encoder needs a rational frame rate: a fractional camera rate such as the car's
    # measured 14.74 Hz aborts the export after the bag has already been read. The sampling grid
    # below uses this same value, so the dataset stays internally consistent, and the declared
    # rate remains visible in the report instead of being silently replaced.
    fps = max(1, round(declared_fps))
    row_topic = None
    if identity is not None:
        # Offline contract: one dataset frame per recorded row, at the rate the car actually
        # emitted.  The profile's declared camera rate would grid frames at instants nothing
        # was ever sampled at, and a row stream sparser than that grid cannot be made fresh
        # enough by pretending it is.
        row_topic = str(meta.get("teleop_state_topic") or "")
        if not row_topic.endswith(hybrid_schema.SIMULATED_6D_ROW_TOPIC_SUFFIX):
            raise HTTPException(status_code=409, detail=(
                "the offline simulated 6D contract reads the action and state from the control "
                f"process's teleop rows; this session recorded {row_topic!r}"))
        try:
            fps = _measured_fps(_row_topic_times(bag_dir, row_topic))
        except ExportRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        schema = {**schema, "state_source": identity["state_source"],
                  "action_source": identity["action_source"]}
    image_name = schema["image_name"]
    state_names = _state_names(schema)
    action_names = _action_names(schema)
    features = {
        image_name: {
            "dtype": "video", "shape": (height, width, 3), "names": ["height", "width", "channels"],
            "info": {"is_depth_map": False},
        },
        "observation.state": {
            "dtype": "float32", "shape": (len(state_names),), "names": state_names,
            "info": {"source": schema["state_source"], "units": schema["state_units"]},
        },
        "action": {
            "dtype": "float32", "shape": (len(action_names),), "names": action_names,
            "info": {"source": schema["action_source"],
                     "units": schema.get("action_units") or ["rad"] * len(action_names),
                     "leader_reading_is_not_action": bool(teleop)},
        },
    }
    report = {
        "session_id": session_id,
        "repo_id": repo_id,
        "fps": fps,
        "declared_camera_fps": declared_fps,
        "source": meta.get("source"),
        "profile": manifest.get("profile"),
        "profile_sha256": meta.get("profile_sha256"),
        "clock_domain": meta.get("clock_domain"),
        "camera_profile": camera_profile,
        "camera_extrinsics": camera_extrinsics,
        "export_schema": {"kind": schema["kind"], "dims": len(action_names),
                          "chassis_included": schema["include_base"]},
        "topics": {"image": image_topic, "state": state_topic, "base_velocity": base_topic,
                   "action_events": "/lelab/events", "teleop_state": meta.get("teleop_state_topic")},
        "episodes": [],
        "alignment": "latest observation at or before each frame; image age <= 0.20 s, state/base <= 0.10 s, action <= 3.0 s zero-order hold; no future data",
        "action_source": schema["action_source"],
        "base_velocity_source": (provenance["base_source_kind"] if hybrid
                                 else base_source if schema["include_base"] else "not_included_locked_chassis"),
        "success_labeling": ("manual_or_unknown_no_simulator_ground_truth" if (teleop or hybrid)
                             else "gazebo_physics_success_gate"),
        "data_identity": (
            "simulated_offline" if (simulation or identity) else "hardware_or_legacy_export"
        ),
        "training_eligible": False if (simulation or identity) else True,
        "warnings": ([] if not schema["include_base"] or hybrid or base_source == "gazebo_model_odometry"
                     else ["legacy session uses wheel odometry; body velocity may not match the model"])
        + ([] if camera_profile != "legacy_unlabeled" else ["legacy session has no camera profile/extrinsics label"])
        + ([] if not teleop or identity is not None else [
            "phase 1 export is arm+gripper only: the locked chassis is excluded rather than zero-filled",
            "action is the post-clamp target the gateway sent; the leader reading is recorded separately and is not the action",
            "state is the measured follower joint feedback; mechanical execution is inferred from later feedback, not observed",
        ])
        + ([] if not (teleop and identity is not None) else [
            "phase 1 export is arm+gripper only: the locked chassis is excluded rather than zero-filled",
            "action is the target the control process recorded sending; the leader reading and the "
            "pre-clamp value are recorded next to it and are not the action",
            "state is the servo readback the control process recorded at that instant; the board "
            "behind it is simulated, so nothing here is a hardware measurement",
        ])
        + ([] if not hybrid else [
            "9D action is three base velocity commands plus five post-clamp joint targets plus one gripper target",
            "9D state is a verified base estimate plus six measured servo readbacks; no channel is zero-filled",
            "a command is only held while its own ttl_ms is valid; an expired channel rejects the episode",
            "success is the operator's manual label or unknown; no simulator ground truth is applied",
            "mechanical execution is not asserted: driver_sent is a UART write, and controller_acknowledged/execution_observed are unavailable unless measured",
        ])
        + ([] if simulation is None else [
            "this dataset is explicit offline simulation evidence; simulation state sources were not hardware readbacks",
            "the dataset is not training-eligible and must not be mixed with hardware sessions",
        ]),
    }
    if hybrid:
        report["feedback_provenance"] = provenance
        report["simulation_identity"] = simulation
    if identity is not None:
        report["export_contract"] = identity["contract"]
        report["simulated_offline_identity"] = identity
        report["sampling"] = identity["sampling"]
        report["warnings"].extend([
            "offline simulated 6D evidence: the action is the target the car's control process "
            "recorded sending, and the state is the servo readback it saw at that instant - "
            "neither is a hardware measurement",
            "one dataset frame per recorded teleop row, so the dataset rate is the measured "
            f"row rate ({fps} Hz), not the declared camera rate",
            "the dataset is not training-eligible and must not be mixed with hardware sessions",
        ])
    if fps != declared_fps:
        report["warnings"].append(
            f"dataset sampled at {fps} Hz because the video encoder requires a whole-number rate; "
            f"the profile declared {declared_fps} Hz")
    dataset = None
    try:
        dataset = LeRobotDataset.create(
            repo_id=repo_id,
            fps=fps,
            features=features,
            root=staging,
            robot_type=schema["robot_type"],
            use_videos=True,
            batch_encoding_size=1,
        )
        for episode in episodes:
            start = int(episode.get("started_time_ns", episode.get("started_sim_ns")))
            end = int(episode.get("ended_time_ns") or episode.get("ended_sim_ns") or start)
            data = _read_bag(bag_dir, start, end, image_topic, state_topic, base_topic, schema)
            if identity is not None:
                # The rows are the recording of what the car sent and saw; the gateway event
                # stream does not exist on this path and is not substituted with anything.
                rows = _read_frames(bag_dir, row_topic, start, end, episode, identity)
                arms, grippers, states = _frame_streams(rows, episode, schema, identity)
                data = {**data, "arms": arms, "grippers": grippers, "states": states,
                        "frames": rows}
            required_streams = [data["images"], data["states"]] + ([data["bases"]] if schema["include_base"] else [])
            if not all(required_streams):
                raise ExportRejected(
                    f"episode {episode['episode_id']} lacks "
                    f"{'image/state/base' if schema['include_base'] else 'image/state'} data")
            action_events: list[dict] = []
            if schema["action_form"] == "six_dof_commands":
                if not data["arms"] or not data["grippers"]:
                    raise ExportRejected(
                        f"episode {episode['episode_id']} has no arm or gripper command events; "
                        "refusing to invent an action")
                first_action_ns = max(int(data["arms"][0]["_time_ns"]),
                                      int(data["grippers"][0]["_time_ns"]))
            elif schema["action_form"] == "hybrid_commands":
                empty = [name for name, stream in (("base", data["base_cmds"]), ("arm", data["arms"]),
                                                   ("gripper", data["grippers"])) if not stream]
                if empty:
                    raise ExportRejected(
                        f"episode {episode['episode_id']} has no {'/'.join(empty)} command stream; "
                        "refusing to zero-fill a missing 9D action channel")
                for channel, stream in (("base", data["base_cmds"]), ("arm", data["arms"]),
                                        ("gripper", data["grippers"])):
                    _check_command_stream(stream, channel, episode["episode_id"])
                first_action_ns = max(int(data["base_cmds"][0]["_time_ns"]),
                                      int(data["arms"][0]["_time_ns"]),
                                      int(data["grippers"][0]["_time_ns"]))
            else:
                action_events = [e for e in data["events"]
                                 if isinstance(e.get("action"), list) and len(e["action"]) == schema["dims"]]
                if not action_events:
                    raise ExportRejected(f"episode {episode['episode_id']} has no applied-action events; refusing synthetic action")
                first_action_ns = int(action_events[0]["_time_ns"])
            # Do not fabricate a frame before the first available observation.
            # If the recorder starts a few milliseconds after the episode marker,
            # begin at the first moment all measured streams exist.
            observed_start = max(
                int(data["images"][0]["stamp"]),
                int(data["states"][0]["stamp"]),
                first_action_ns,
                *(int(data["bases"][0]["stamp"]),) if schema["include_base"] else (),
            )
            original_start = start
            start = max(start, observed_start)
            frame_records = []
            count = 0
            phases = episode.get("phases") or []
            # Validate boundaries before using them to gate any sample.
            _phase_slices(episode, start, 0, fps)
            if identity is not None:
                # Event paced: every exported frame is one recorded row of this episode.
                sample_times = [int(row["stamp_ns"]) for row in data["frames"]
                                if int(row["stamp_ns"]) >= start and int(row["stamp_ns"]) <= end]
            else:
                sample_times = []
                cursor = start
                while (cursor < end if phases else cursor <= end):
                    sample_times.append(cursor)
                    cursor += int(1e9 / fps)
            frame_phase_labels: list[str] = []
            frame_phase_times: list[int] = []
            for t in sample_times:
                state = _latest(data["states"], t)
                odom = _latest(data["bases"], t) if schema["include_base"] else {"stamp": t}
                image = _latest(data["images"], t)
                action, action_stamp, action_received, action_extra = _resolve_action(schema, data, t, action_events)
                if state is None or image is None or (schema["include_base"] and odom is None):
                    raise ExportRejected(f"episode {episode['episode_id']} source gap at {t - start} ns")
                if action is None:
                    raise ExportRejected(f"episode {episode['episode_id']} has no action at +{((t-start)/1e9):.2f}s")
                if phases:
                    current_phase = next((p for p in phases if p["started_time_ns"] <= t < p["ended_time_ns"]), None)
                    if current_phase is None:
                        raise ExportRejected(f"episode {episode['episode_id']} frame lacks phase at {t}")
                    boundary = current_phase["started_time_ns"]
                    if min(state["stamp"], image["stamp"], action_stamp,
                           odom["stamp"] if schema["include_base"] else t) < boundary:
                        raise ExportRejected(f"episode {episode['episode_id']} source crosses phase boundary at {t}")
                action_age = (t - action_stamp) / 1e9
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
                if not all(np.isfinite(action)):
                    raise ExportRejected(f"episode {episode['episode_id']} non-finite action")
                hybrid_state_extra: dict[str, Any] = {}
                if hybrid:
                    # Each command channel must still be inside its own ttl at this
                    # frame; a refreshed receive never extends another channel's TTL.
                    ttl_by_channel = action_extra.get("command_channel_ttl_ms") or {}
                    times_by_channel = action_extra.get("command_channel_times_ns") or {}
                    for channel in ("base", "arm", "gripper"):
                        ttl_ms = ttl_by_channel.get(channel)
                        issued = times_by_channel.get(channel)
                        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, (int, float)) or ttl_ms <= 0:
                            raise ExportRejected(
                                f"episode {episode['episode_id']} {channel} command declares no ttl_ms; "
                                "refusing to hold it open-ended")
                        if not isinstance(issued, int):
                            raise ExportRejected(
                                f"episode {episode['episode_id']} {channel} command has no issue time")
                        channel_age_ns = t - issued
                        if channel_age_ns > int(ttl_ms) * 1_000_000:
                            raise ExportRejected(
                                f"episode {episode['episode_id']} {channel} command expired at "
                                f"+{((t - start) / 1e9):.2f}s (age {channel_age_ns / 1e9:.3f}s > "
                                f"{ttl_ms} ms)")
                    try:
                        state_values, hybrid_state_extra = _hybrid_state_vector(
                            state, odom, provenance, t, provenance["max_state_age_s"])
                    except ExportRejected as exc:
                        raise ExportRejected(
                            f"episode {episode['episode_id']} 9D state rejected: {exc}") from exc
                    state_vec = np.asarray(state_values, dtype=np.float32)
                else:
                    joints = dict(zip(state["names"], state["positions"]))
                    missing_joints = [name for name in schema["state_joints"] if name not in joints]
                    if missing_joints:
                        raise ExportRejected(f"episode {episode['episode_id']} missing measured joints {missing_joints}; refusing zero fill")
                    state_vec = np.array([
                        *([odom["vx"], odom["vy"], odom["wz"]] if schema["include_base"] else []),
                        *(joints[name] for name in schema["state_joints"]),
                    ], dtype=np.float32)
                if not np.isfinite(state_vec).all():
                    raise ExportRejected(f"episode {episode['episode_id']} non-finite measured state")
                if (image["height"], image["width"]) != (height, width):
                    raise ExportRejected(
                        f"episode {episode['episode_id']} image size {image['width']}x{image['height']} does not match dataset feature {width}x{height}"
                    )
                image_array = np.frombuffer(image["data"], dtype=np.uint8).reshape(image["height"], image["width"], 3)
                if image["encoding"] == "bgr8":
                    image_array = image_array[:, :, ::-1]
                if identity is not None and phases:
                    # The row stream is the frame stream, so the phase of a dataset frame is
                    # the phase the car recorded on the row it came from.
                    frame_phase_labels.append(current_phase["phase"])
                    frame_phase_times.append(int(t))
                dataset.add_frame({
                    image_name: image_array,
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
                    "base_received_ns": odom.get("received_ns"),
                    "base_stamp_source": odom.get("stamp_source"),
                    "action_time_ns": action_stamp,
                    "action_received_ns": action_received,
                    "action_age_s": round(action_age, 4),
                    "image_age_s": round(image_age, 4),
                    "state_age_s": round(state_age, 4),
                    "phase": current_phase["phase"] if phases else None,
                    **action_extra,
                    **hybrid_state_extra,
                })
                count += 1
            if count < 2:
                raise RuntimeError(f"episode {episode['episode_id']} produced only {count} frames")
            if identity is not None and count != len(data["frames"]):
                raise ExportRejected(
                    f"episode {episode['episode_id']} exported {count} frames from "
                    f"{len(data['frames'])} recorded rows: the offline contract maps them one to "
                    "one, so a row may be neither dropped nor invented")
            dataset.save_episode()
            episode_slices = (_row_phase_slices(episode, frame_phase_labels, frame_phase_times, fps)
                              if identity is not None else _phase_slices(episode, start, count, fps))
            if hybrid or identity is not None:
                # Now that the frame grid is known, every manual phase must own at
                # least one exported frame; a boundary that lands outside the grid
                # would silently produce an episode with an unusable segment.
                try:
                    hybrid_schema.require_observe_grasp(episode_slices)
                except hybrid_schema.SampleRejected as exc:
                    raise ExportRejected(
                        f"episode {episode['episode_id']} phase coverage invalid: "
                        f"{exc.reason}: {exc.detail}") from exc
            report["episodes"].append({
                "episode_id": episode["episode_id"],
                "source": episode.get("source"),
                "success": episode.get("evaluation", {}).get("success"),
                "frames": count,
                "started_time_ns": original_start,
                "aligned_start_time_ns": start,
                "ended_time_ns": end,
                "frames_detail": frame_records,
                "phases": episode_slices,
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
        image_key = schema["image_name"]
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
        if identity is not None:
            # Written next to the dataset, not only in the report: whoever opens the dataset
            # later must be able to see that it is offline simulated evidence without
            # trusting the file that described the export run.
            (staging / "meta" / "lelab_export_identity.json").write_text(json.dumps({
                **identity, "session_id": session_id, "repo_id": repo_id,
                "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, indent=2), encoding="utf-8")
        (staging / "meta" / "lelab_phase_index.json").write_text(json.dumps({
            "version": 1, "session_id": session_id, "clock_domain": meta.get("clock_domain"),
            "episodes": [{"episode_index": i, "episode_id": e["episode_id"],
                          "phases": e["phases"]} for i, e in enumerate(report["episodes"])],
        }, indent=2), encoding="utf-8")
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


def _spawn_export_job(session_id: str, include_failed: bool, simulated_offline: bool) -> dict:
    _guard(lambda: _check_exportable(session_id, simulated_offline=simulated_offline))
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
        # Fourth argument: the restricted offline contract.  Absent means the normal
        # hardware path, which still refuses a session that is not hardware teleop.
        "1" if simulated_offline else "0",
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
        "simulated_offline_6d": simulated_offline,
        "status": "running", "pid": proc.pid, "started_wall": time.time(),
        "proc": proc, "result_path": str(result_path), "log_path": str(log_path),
    }
    with EXPORT_JOB_LOCK:
        EXPORT_JOBS[job_id] = job
    return _job_public(job)


@router.post("/export")
def export(body: dict):
    """The normal export entry: a hardware teleop session, or nothing."""
    return _spawn_export_job(str(body.get("session_id", "")),
                             bool(body.get("include_failed", False)), False)


@router.post("/export/offline-simulated-6d")
def export_offline_simulated_6d(body: dict):
    """The restricted offline entry for the simulated six-dimensional contract.

    Kept as its own route rather than a flag on `/export` so the ordinary training-oriented
    path can never pick it up by accident, and so the contract shows up in the request that
    made the dataset.  `_check_exportable` still has to be satisfied by the session's own
    metadata: declared simulation, `simulated_offline` identity, the offline profile, and
    `training_export` switched off.  What comes out is integration evidence, not a
    training-eligible dataset.
    """
    return _spawn_export_job(str(body.get("session_id", "")),
                             bool(body.get("include_failed", False)), True)


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
