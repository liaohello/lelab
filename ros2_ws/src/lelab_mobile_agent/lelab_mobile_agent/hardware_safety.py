"""Fail-closed command supervisor logic, intended for a separate hardware-owner process.

The optional worker loop can be hosted in a separate process. Its sink must be
independently bounded and recheck the envelope at actual output. Neither a process
nor physical stop/hold effectiveness is implied merely by importing this module.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable


class SafetySupervisor:
    def __init__(self, sink: Any, feedback_ok: Callable[[str], bool], *,
                 calibration_hash: str, limits: dict[str, tuple[float, float]],
                 outputs_enabled: bool = False, clock_ns: Callable[[], int] = time.monotonic_ns):
        if not isinstance(outputs_enabled, bool) or not calibration_hash or not limits:
            raise ValueError("explicit calibration and limits required")
        for low, high in limits.values():
            if not all(math.isfinite(x) for x in (low, high)) or low > high:
                raise ValueError("invalid limits")
        self.sink, self.feedback_ok, self.clock_ns = sink, feedback_ok, clock_ns
        self.calibration_hash, self.limits = calibration_hash, dict(limits)
        self.outputs_enabled = outputs_enabled
        self._lock = threading.RLock()
        self.session = None
        self._retired_epochs: dict[str, int] = {}
        self.seq: dict[tuple[str, str], int] = {}
        self.active: dict[str, dict[str, Any]] = {}

    def arm_session(self, session_id: str, session_epoch: int, *, maintenance: bool = False) -> None:
        if not isinstance(session_id, str) or not session_id or not isinstance(maintenance, bool) or isinstance(session_epoch, bool) or not isinstance(session_epoch, int) or session_epoch < 0:
            raise ValueError("invalid session")
        with self._lock:
            if self.session is not None:
                raise RuntimeError("release previous session first")
            if session_epoch <= self._retired_epochs.get(session_id, -1):
                raise ValueError("session epoch already retired")
            self.session = (session_id, session_epoch, maintenance)
            self.seq.clear()

    def _validate(self, c: dict[str, Any], now: int, *, check_seq: bool) -> str | None:
        if (self.session is None or isinstance(c.get("session_epoch"), bool)
                or not isinstance(c.get("session_epoch"), int)
                or (c.get("session_id"), c.get("session_epoch")) != self.session[:2]):
            return "session_mismatch"
        if type(c.get("protocol_version")) is not int or c["protocol_version"] != 1:
            return "protocol_version"
        channel, source = c.get("channel"), c.get("source")
        allowed = {"base": "gamepad", "arm": "so101_leader", "gripper": "so101_leader"}
        if not isinstance(channel, str) or not isinstance(source, str) or channel not in allowed or (source != allowed[channel] and not (source == "maintenance" and self.session[2])):
            return "channel_source_mismatch"
        for key in ("seq", "sample_seq", "ttl_ms", "received_mono_ns", "deadline_mono_ns"):
            value = c.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return "invalid_" + key
        if c["ttl_ms"] <= 0 or c["received_mono_ns"] > now or c["deadline_mono_ns"] <= now or c["deadline_mono_ns"] > c["received_mono_ns"] + c["ttl_ms"] * 1_000_000:
            return "expired_or_invalid_deadline"
        if c.get("calibration_bundle_hash") != self.calibration_hash:
            return "calibration_mismatch"
        if channel in ("arm", "gripper") and c.get("clutch") is not True:
            return "clutch_not_engaged"
        key = (channel, source)
        if check_seq and key in self.seq and c["seq"] <= self.seq[key]:
            return "stale_seq"
        target = c.get("target")
        if not isinstance(target, dict):
            return "invalid_target"
        if channel == "base":
            keys = ("vx_m_s", "vy_m_s", "wz_rad_s")
            if set(target) != set(keys):
                return "invalid_base_target"
            values = dict(target)
        elif channel == "arm":
            names = [f"joint{i}" for i in range(1, 6)]
            positions = target.get("positions_rad")
            if set(target) != {"joint_names", "positions_rad"} or target.get("joint_names") != names or not isinstance(positions, list) or len(positions) != 5:
                return "invalid_arm_target"
            values = dict(zip(names, positions))
        else:
            if set(target) != {"joint_name", "position_rad"} or target.get("joint_name") != "r_joint":
                return "invalid_gripper_target"
            values = {"r_joint": target["position_rad"]}
        for name, value in values.items():
            if (name not in self.limits or isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not self.limits[name][0] <= value <= self.limits[name][1]):
                return "target_out_of_bounds:" + name
        try:
            if self.feedback_ok(channel) is not True:
                return "feedback_unavailable"
        except Exception:
            return "feedback_unavailable"
        return None

    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        # Copy nested lists/dicts: caller cannot mutate an accepted envelope after checking it.
        import copy
        with self._lock:
            c = copy.deepcopy(command)
            if not isinstance(c, dict):
                return {"accepted": False, "reason": "invalid_command"}
            reason = self._validate(c, self.clock_ns(), check_seq=True)
            if reason:
                if isinstance(c.get("channel"), str) and c["channel"] in self.active:
                    self._retire_locked(reason)
                return {"accepted": False, "reason": reason}
            if not self.outputs_enabled:
                return {"accepted": False, "reason": "outputs_disabled"}
            # Check again at the output boundary; sink must enforce this envelope too.
            reason = self._validate(c, self.clock_ns(), check_seq=True)
            if reason:
                self._retire_locked(reason)
                return {"accepted": False, "reason": reason}
            self.seq[(c["channel"], c["source"])] = c["seq"]
            self.active[c["channel"]] = c
            try:
                self.sink.submit(c)
            except Exception:
                self._retire_locked("sink_error")
                return {"accepted": False, "reason": "sink_error"}
            return {"accepted": True, "level": "gateway_accepted", "controller_acknowledged": "unavailable", "execution_observed": "unavailable"}

    def _stop_locked(self, reason: str) -> dict[str, Any]:
        had_active = bool(self.active)
        self.active.clear()
        error = None
        if had_active and self.outputs_enabled:
            try:
                self.sink.stop(reason)
            except Exception as exc:
                error = type(exc).__name__
        return {"stop_requested": had_active, "hardware_effect": "unverified", "error": error}

    def tick(self) -> dict[str, Any]:
        with self._lock:
            for c in list(self.active.values()):
                reason = self._validate(c, self.clock_ns(), check_seq=False)
                if reason:
                    return self._retire_locked(reason)
            return {"stop_requested": False, "hardware_effect": "unverified", "error": None}

    def _retire_locked(self, reason: str) -> dict[str, Any]:
        result = self._stop_locked(reason)
        if self.session is not None:
            sid, epoch, _ = self.session
            self._retired_epochs[sid] = max(epoch, self._retired_epochs.get(sid, -1))
        self.session = None
        self.seq.clear()
        return result

    def stop(self, reason: str) -> dict[str, Any]:
        with self._lock:
            return self._retire_locked(reason)


def run_supervisor(connection: Any, supervisor: SafetySupervisor, *, tick_interval_s: float = 0.02) -> None:
    """Serve a trusted local IPC connection in the independently launched owner process.

    Parent sends dictionaries with op=arm_session, submit, tick or stop. A tick is
    run every loop even if traffic is continuous. EOF, malformed IPC or cancellation
    requests stop and exit. The caller is responsible for isolated process startup,
    parent-death/transport behavior and a sink with bounded, deadline-aware writes.
    """
    if not math.isfinite(tick_interval_s) or not 0 < tick_interval_s <= 0.1:
        raise ValueError("invalid tick interval")
    try:
        while True:
            supervisor.tick()
            if not connection.poll(tick_interval_s):
                continue
            message = connection.recv()
            if not isinstance(message, dict):
                break
            op = message.get("op")
            if op == "arm_session":
                try:
                    supervisor.arm_session(message.get("session_id"), message.get("session_epoch"),
                                           maintenance=message.get("maintenance", False))
                    result = {"accepted": True}
                except (ValueError, RuntimeError):
                    result = {"accepted": False, "reason": "invalid_session"}
            elif op == "submit":
                result = supervisor.submit(message.get("command"))
            elif op == "tick":
                result = supervisor.tick()
            elif op == "stop":
                result = supervisor.stop(str(message.get("reason", "stop")))
                connection.send(result)
                break
            else:
                break
            connection.send(result)
    except (EOFError, OSError, BrokenPipeError):
        pass
    finally:
        supervisor.stop("ipc_closed")
        connection.close()
