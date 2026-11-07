"""HTTP-only fixture for the LanderPi follower calibration job.

SIMULATED.  It runs the *real* car-side state machine
(``lelab_mobile_agent.follower_calibration_job``) behind the frozen HTTP routes,
with a fake six-servo bus, so the page and the upper host can be exercised without
the Pi, rclpy, a serial port or any hardware.  Every response carries
``fixture: true``; the device identity carries ``simulated: true`` so a fixture can
never be turned into a ``measured`` calibration asset.

Routes (the frozen contract):

    POST /calibration/follower/start
    POST /calibration/follower/status
    POST /calibration/follower/torque
    POST /calibration/follower/gripper/target
    POST /calibration/follower/gripper/mark
    POST /calibration/follower/gripper/stop
    POST /calibration/follower/stop
    GET  /status                 # hybrid-shaped status for the upper host
    GET  /fixture/log            # commands and requests, for test evidence
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ros2_ws/src/lelab_mobile_agent"))

from lelab_mobile_agent.follower_calibration_job import (  # noqa: E402
    ARM_JOINTS, GRIPPER_JOINT, JOINTS, SERVO_IDS, CalibrationJobError,
    FollowerCalibrationJob)

ARM_LOW, ARM_HIGH = 420, 560
ARM_PERIOD_S = 6.0


class SimulatedBus:
    """Six servos, one of which (id 10) follows the calibration job's targets."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.arm = {name: 500 for name in ARM_JOINTS}
        self.gripper = 450
        self.seq = 0
        self.time_ns = 0
        self.sweeping = False
        self._phase = {name: index * 0.19 for index, name in enumerate(ARM_JOINTS)}
        self._last_tick = time.monotonic()
        self.commands: list[dict[str, Any]] = []
        self.torque: list[dict[str, Any]] = []

    def tick(self, dt: float) -> None:
        if not self.sweeping:
            return
        with self.lock:
            for name in ARM_JOINTS:
                self._phase[name] = (self._phase[name] + dt / ARM_PERIOD_S) % 1.0
                phase = self._phase[name]
                ratio = 2.0 * phase if phase < 0.5 else 2.0 * (1.0 - phase)
                self.arm[name] = int(round(ARM_LOW + (ARM_HIGH - ARM_LOW) * ratio))

    def apply_target(self, servo_id: int, raw: int, duration_s: float) -> None:
        if servo_id != SERVO_IDS[GRIPPER_JOINT]:
            raise RuntimeError(f"fixture refuses to command servo {servo_id}")
        with self.lock:
            self.gripper = int(raw)
            self.commands.append({
                "mono": time.monotonic(),
                "wall": time.time(),
                "servo_id": int(servo_id),
                "raw": int(raw),
                "duration_s": float(duration_s),
            })

    def apply_torque(self, servo_ids: list[int], enabled: bool) -> None:
        with self.lock:
            self.torque.append({"mono": time.monotonic(), "servo_ids": list(servo_ids),
                                "enabled": bool(enabled)})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self.seq += 1
            self.time_ns += 200_000_000
            samples = {}
            for name in JOINTS:
                samples[name] = {
                    "name": name,
                    "servo_id": SERVO_IDS[name],
                    "raw_position": self.gripper if name == GRIPPER_JOINT else self.arm[name],
                    "sample_seq": self.seq,
                    "sample_time_ns": self.time_ns,
                    "age_s": 0.01,
                    "valid": True,
                    "error": None,
                    "source_kind": "servo_position_readback",
                }
            return {"reason": "ok", "joint_samples": samples,
                    "age_s": {name: 0.01 for name in JOINTS},
                    "stale_after_s": 0.5, "scan_span_s": 0.012}


class FixtureSink:
    def __init__(self, bus: SimulatedBus) -> None:
        self.bus = bus

    def publish_target(self, servo_id: int, raw: int, duration_s: float) -> None:
        self.bus.apply_target(servo_id, raw, duration_s)

    def set_torque(self, servo_ids: list[int], enabled: bool) -> None:
        self.bus.apply_torque(servo_ids, enabled)


def fixture_device_identity() -> dict[str, Any]:
    return {
        "kind": "landerpi_bus_servo_follower",
        "control_board": "fixture_simulated",
        "controller_owner": "ros_robot_controller",
        "joint_order": list(JOINTS),
        "servo_ids": dict(SERVO_IDS),
        "board_serial": None,
        "board_serial_source": "unavailable",
        "host_serial": "fixture",
        "agent_version": "fixture",
        "profile": "fixture",
        "simulated": True,
        "feedback_service": "/fixture/bus_servo/get_state",
        "command_topic": "/fixture/servo_controller",
    }


BUS = SimulatedBus()
JOB = FollowerCalibrationJob(
    sink=FixtureSink(BUS),
    read_scan=lambda: {**BUS.arm, GRIPPER_JOINT: BUS.gripper},
    readback_snapshot=BUS.snapshot,
    pause_readback=lambda: None,
    resume_readback=lambda: None,
    device_identity=fixture_device_identity,
    agent_version="fixture",
    profile="fixture",
)
REQUESTS: list[dict[str, Any]] = []
REQUESTS_LOCK = threading.Lock()
STOP = threading.Event()


def _worker() -> None:
    last = time.monotonic()
    while not STOP.wait(0.02):
        now = time.monotonic()
        BUS.tick(max(0.0, now - last))
        last = now
        try:
            JOB.tick()
            if JOB.phase == FollowerCalibrationJob.MANUAL and JOB.sampler.arm_valid()[0]:
                # Manual arm range capture is complete.  Freeze the simulated arm
                # before the operator starts driving only ID10; continuing to move
                # non-target axes must (correctly) fault the real state machine.
                BUS.sweeping = False
        except Exception:  # noqa: BLE001 - a fixture must not die on a job bug
            pass


def _record(entry: dict[str, Any]) -> None:
    with REQUESTS_LOCK:
        REQUESTS.append(entry)
        del REQUESTS[:-400]


def status_payload() -> dict[str, Any]:
    """Hybrid-shaped status so the upper host's observer can bind to the fixture."""
    snapshot = BUS.snapshot()
    snapshot["reason"] = "fixture"
    job = JOB
    return {
        "ok": True,
        "profile": "hardware_hybrid_manual_v1",
        "read_only": False,
        "simulated": True,
        "data_source": "http_fixture",
        "outputs_enabled": False,
        "control": {"active": False, "follower_feedback": snapshot},
        "hybrid": {"driver_bound": True, "outputs_enabled": False},
        "sensors": {"rgb": {"topic": "/fixture/camera", "stale": True}},
        "calibration": {
            "phase": job.phase,
            "job_id": job.job_id,
            "commands_sent": (job.calibrator.commands_sent if job.calibrator else 0),
        },
        "fixture": True,
    }


ROUTES = {
    "/calibration/follower/start": lambda body: JOB.start(body),
    "/calibration/follower/status": lambda body: JOB.status(body),
    "/calibration/follower/torque": lambda body: JOB.torque(body),
    "/calibration/follower/gripper/target": lambda body: JOB.gripper_target(body),
    "/calibration/follower/gripper/mark": lambda body: JOB.gripper_mark(body),
    "/calibration/follower/gripper/stop": lambda body: JOB.gripper_stop(body),
    "/calibration/follower/stop": lambda body: JOB.stop(body),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("invalid JSON body") from None
        if not isinstance(parsed, dict):
            raise ValueError("body must be an object")
        return parsed

    def do_GET(self):  # noqa: N802
        if self.path == "/status":
            self._json(200, status_payload())
            return
        if self.path == "/fixture/log":
            with BUS.lock:
                commands = list(BUS.commands)
                torque = list(BUS.torque)
            with REQUESTS_LOCK:
                requests = list(REQUESTS)
            self._json(200, {"fixture": True, "simulated": True,
                             "commands": commands, "torque": torque,
                             "requests": requests,
                             "job": {"phase": JOB.phase, "job_id": JOB.job_id,
                                     "stop_reason": JOB.stop_reason,
                                     "gripper": (JOB.calibrator.summary(time.monotonic())
                                                 if JOB.calibrator else None)}})
            return
        self._json(404, {"error": "not_found", "fixture": True})

    def do_POST(self):  # noqa: N802
        handler = ROUTES.get(self.path)
        if handler is None:
            self._json(404, {"error": "not_found", "detail": self.path, "fixture": True})
            return
        try:
            body = self._body()
        except ValueError as exc:
            self._json(400, {"error": "bad_request", "detail": str(exc), "fixture": True})
            return
        if self.path == "/calibration/follower/start" and JOB.phase in (
                FollowerCalibrationJob.MANUAL, FollowerCalibrationJob.DRAG,
                FollowerCalibrationJob.REVIEW):
            pass
        try:
            result = handler(body)
        except CalibrationJobError as exc:
            _record({"path": self.path, "status": exc.status, "error": exc.reason,
                     "detail": exc.detail, "mono": time.monotonic(), "body": body})
            self._json(exc.status, {"error": exc.reason, "detail": exc.detail,
                                    "fixture": True})
            return
        except Exception as exc:  # noqa: BLE001
            _record({"path": self.path, "status": 500, "error": type(exc).__name__,
                     "detail": str(exc), "mono": time.monotonic(), "body": body})
            self._json(500, {"error": type(exc).__name__, "detail": str(exc),
                             "fixture": True})
            return
        if self.path == "/calibration/follower/start":
            BUS.sweeping = True
            with BUS.lock:
                BUS.commands.clear()
                BUS.torque.clear()
        if self.path == "/calibration/follower/stop":
            BUS.sweeping = False
        _record({"path": self.path, "status": 200, "mono": time.monotonic(),
                 "body": body, "response": {key: result.get(key) for key in
                                            ("accepted", "target_raw", "phase", "reason",
                                             "pointer_generation", "saved")}})
        payload = dict(result)
        payload["fixture"] = True
        payload["simulated"] = True
        self._json(200, payload)


def serve(host: str, port: int) -> ThreadingHTTPServer:
    threading.Thread(target=_worker, name="fixture-job", daemon=True).start()
    server = ThreadingHTTPServer((host, port), Handler)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18111)
    args = parser.parse_args()
    server = serve(args.host, args.port)
    print(f"follower calibration fixture (SIMULATED) on http://{args.host}:{args.port}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()
