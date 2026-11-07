"""Explicit arm/gripper output adapter publishing to the unique board owner."""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable

from .follower_feedback import RADIANS_PER_ENCODER_TICK as RADIANS_PER_TICK

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5")
GRIPPER = "r_joint"
SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4,
             "joint5": 5, "r_joint": 10}
DIRECTIONS = {"joint1": 1.0, "joint2": 1.0, "joint3": 1.0,
              "joint4": 1.0, "joint5": 1.0, "r_joint": -1.0}
OUTPUT_TOPIC = "/ros_robot_controller/bus_servo/set_position"
POSITION_PID_DEFAULTS = {
    "enabled": False,
    "kp": 0.30,
    "ki": 0.02,
    "kd": 0.01,
    "deadband_rad": 0.004,
    "integral_limit_rad": 0.03,
    "max_correction_rad": 0.04,
    "derivative_filter_alpha": 0.20,
}


def _normalize_position_pid(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(POSITION_PID_DEFAULTS)
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("position_pid must be an object")
        cfg.update(raw)
    if cfg.get("enabled") is not False and cfg.get("enabled") is not True:
        raise ValueError("position_pid.enabled must be boolean")
    for key in ("kp", "ki", "kd"):
        value = cfg.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or float(value) < 0):
            raise ValueError(f"invalid position_pid.{key}")
        cfg[key] = float(value)
    for key in ("deadband_rad", "integral_limit_rad", "max_correction_rad"):
        value = cfg.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or float(value) <= 0):
            raise ValueError(f"invalid position_pid.{key}")
        cfg[key] = float(value)
    alpha = cfg.get("derivative_filter_alpha")
    if (isinstance(alpha, bool) or not isinstance(alpha, (int, float))
            or not math.isfinite(float(alpha)) or not 0 < float(alpha) <= 1):
        raise ValueError("invalid position_pid.derivative_filter_alpha")
    cfg["derivative_filter_alpha"] = float(alpha)
    cfg["enabled"] = bool(cfg["enabled"])
    return cfg


def rad_to_raw(name: str, radians: float) -> int:
    if name not in SERVO_IDS:
        raise ValueError(f"unknown servo joint {name!r}")
    if isinstance(radians, bool) or not isinstance(radians, (int, float)):
        raise ValueError("position must be numeric")
    value = float(radians)
    if not math.isfinite(value):
        raise ValueError("position must be finite")
    raw = 500.0 + DIRECTIONS[name] * value / RADIANS_PER_TICK
    rounded = int(round(raw))
    if not 0 <= rounded <= 1000:
        raise ValueError(f"{name} position outside service pulse range")
    return rounded


class RosServoOutputDriver:
    """Gateway driver that emits bounded position packets; it opens no serial port."""

    def __init__(
        self,
        node: Any,
        publisher: Any,
        *,
        servos_position_type: Any,
        servo_position_type: Any,
        readback: Any,
        limits_rad: dict[str, tuple[float, float]],
        max_step_rad_s: float = 3.0,
        max_silence_s: float = 1.0,
        duration_s: float = 0.15,
        clock: Callable[[], float] = time.monotonic,
        command_topic: str = OUTPUT_TOPIC,
        trial_motion: dict[str, Any] | None = None,
        homing_motion: dict[str, Any] | None = None,
        servo_state_publisher: Any | None = None,
        bus_servo_state_type: Any | None = None,
        set_bus_servo_state_type: Any | None = None,
        torque_readback: Any | None = None,
        position_pid: dict[str, Any] | None = None,
    ) -> None:
        if not limits_rad:
            raise ValueError("calibrated limits are required")
        if not math.isfinite(max_step_rad_s) or not 0 < max_step_rad_s <= 10.0:
            raise ValueError("invalid maximum step rate")
        if not math.isfinite(max_silence_s) or not 0.1 <= max_silence_s <= 1.0:
            raise ValueError("invalid output watchdog")
        if not math.isfinite(duration_s) or not 0.02 <= duration_s <= 0.5:
            raise ValueError("invalid motion duration")
        self.node = node
        self.publisher = publisher
        self.servos_position_type = servos_position_type
        self.servo_position_type = servo_position_type
        self.readback = readback
        self.limits_rad = dict(limits_rad)
        self.max_step_rad_s = float(max_step_rad_s)
        self.max_silence_s = float(max_silence_s)
        self.duration_s = float(duration_s)
        self.clock = clock
        self.lock = threading.RLock()
        self.outputs_enabled = False
        self.last_command_mono: float | None = None
        self.last_target_rad: dict[str, float] = {}
        self.last_target_mono: dict[str, float] = {}
        self.last_publish_mono: float | None = None
        self.last_subscription_count: int | None = None
        self.publish_count = 0
        self.stop_count = 0
        self.step_clamp_count = 0
        self.last_step_clamp: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.command_topic = str(command_topic)
        if trial_motion is not None and homing_motion is not None:
            raise ValueError("trial and homing modes are mutually exclusive")
        self.trial_motion = dict(trial_motion) if trial_motion is not None else None
        self.homing_motion = dict(homing_motion) if homing_motion is not None else None
        self.trial_origin: dict[str, float] = {}
        self.trial_first_frame_pending = self.trial_motion is not None
        self.homing_first_frame_pending = self.homing_motion is not None
        self.homing_active_joint: str | None = None
        self.homing_last_target_raw: int | None = None
        self.servo_state_publisher = servo_state_publisher
        self.bus_servo_state_type = bus_servo_state_type
        self.set_bus_servo_state_type = set_bus_servo_state_type
        self.torque_readback = torque_readback
        self.position_pid = _normalize_position_pid(position_pid)
        self._pid_integral: dict[str, float] = {}
        self._pid_last_error: dict[str, float] = {}
        self._pid_last_time: dict[str, float] = {}
        self._pid_last_derivative: dict[str, float] = {}
        self.last_pid: dict[str, dict[str, Any]] = {}
        self.homing_torque_enabled = False
        self.torque_publish_count = 0
        self.torque_servo_ids: list[int] = []
        # requested -> verified/failed is a per-ID readback state machine, not a
        # publish-and-assume flag. It advances one bounded step per enable() call so
        # the control tick and ROS executor are never blocked waiting for a service.
        self.homing_torque_state = "idle"
        self.homing_torque_results: dict[int, dict[str, Any]] = {}
        self.homing_torque_error: str | None = None
        self._torque_load_queue: list[int] = []
        self._torque_verify_queue: list[int] = []
        self._torque_started_mono: float | None = None
        self._torque_verified_mono: float | None = None
        if self.trial_motion is not None:
            axes = self.trial_motion.get("allowed_joints")
            if (not isinstance(axes, list) or not axes or len(set(axes)) != len(axes)
                    or any(name not in SERVO_IDS for name in axes)):
                raise ValueError("invalid trial allowed_joints")
            if set(self.limits_rad) != set(SERVO_IDS):
                raise ValueError("trial requires calibrated limits for all six joints")
            for key in ("first_frame_tolerance_rad", "locked_tolerance_rad", "max_displacement_rad"):
                value = self.trial_motion.get(key)
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not 0 < value <= 0.25):
                    raise ValueError(f"invalid trial {key}")
        if self.homing_motion is not None:
            axes = self.homing_motion.get("axis_order")
            targets = self.homing_motion.get("targets_raw")
            if (not isinstance(axes, list) or set(axes) != set(SERVO_IDS)
                    or len(axes) != len(SERVO_IDS)):
                raise ValueError("invalid homing axis_order")
            if (not isinstance(targets, dict) or set(targets) != set(SERVO_IDS)
                    or any(type(value) is not int or not 0 <= value <= 1000
                           for value in targets.values())):
                raise ValueError("invalid homing targets_raw")
            if self.homing_motion.get("enable_torque") is not True:
                raise ValueError("homing requires enable_torque")
            if set(self.limits_rad) != set(SERVO_IDS):
                raise ValueError("homing requires calibrated limits for all six joints")
            for key, upper in (("tolerance_raw", 5), ("locked_tolerance_raw", 5),
                               ("first_frame_tolerance_raw", 20),
                               ("max_step_raw", 20)):
                value = self.homing_motion.get(key)
                if (type(value) is not int or not 1 <= value <= upper):
                    raise ValueError(f"invalid homing {key}")
            for name, raw in targets.items():
                radians = (float(raw) - 500.0) * RADIANS_PER_TICK * DIRECTIONS[name]
                low, high = self.limits_rad[name]
                if not low <= radians <= high:
                    raise ValueError(f"homing target outside calibrated limits: {name}")
        self.timer = node.create_timer(0.05, self._watchdog)

    def capabilities(self) -> dict[str, Any]:
        with self.lock:
            return {
                "driver": "ros_servo_set_position",
                "command_topic": self.command_topic,
                "joint_ids": dict(SERVO_IDS),
                "output_capable": True,
                "outputs_enabled": self.outputs_enabled,
                "max_step_rad_s": self.max_step_rad_s,
                "max_silence_s": self.max_silence_s,
                "duration_s": self.duration_s,
                "hardware_effect": "unverified",
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
                "homing_motion": self.homing_motion,
                "homing_active_joint": self.homing_active_joint,
                "homing_last_target_raw": self.homing_last_target_raw,
                "publisher_subscriptions": self.last_subscription_count,
                "homing_torque_enabled": self.homing_torque_enabled,
                "homing_torque_state": self.homing_torque_state,
                "homing_torque_results": dict(self.homing_torque_results),
                "homing_torque_error": self.homing_torque_error,
                "torque_publish_count": self.torque_publish_count,
                "torque_servo_ids": list(self.torque_servo_ids),
                "step_clamp_count": self.step_clamp_count,
                "last_step_clamp": self.last_step_clamp,
                "last_error": self.last_error,
                "position_pid": dict(self.position_pid),
            }

    def read_snapshot(self) -> dict[str, Any]:
        return self.readback.read_snapshot()

    def _feedback_ok(self) -> bool:
        snapshot = self.readback.read_snapshot()
        return bool(snapshot.get("available") and not snapshot.get("last_error"))

    def enable(self) -> None:
        snapshot = self.readback.read_snapshot()
        if not snapshot.get("available") or snapshot.get("last_error"):
            raise RuntimeError("trusted_feedback_unavailable")
        count_getter = getattr(self.publisher, "get_subscription_count", None)
        if callable(count_getter):
            deadline = time.monotonic() + 1.0
            count = int(count_getter())
            while count < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
                count = int(count_getter())
            self.last_subscription_count = count
            if count < 1:
                raise RuntimeError("no_actuator_subscriber")
        if (self.homing_motion is not None
                and self.homing_motion.get("enable_torque") is True
                and self.homing_torque_state != "verified"):
            self._progress_homing_torque()
        if (self.homing_motion is not None
                and self._torque_verified_mono is not None):
            # Torque was just loaded: the position snapshot that admits output must be
            # a fresh scan received after the load, not the pre-load pose.
            last_success = snapshot.get("last_success_mono")
            if (not isinstance(last_success, (int, float))
                    or last_success < self._torque_verified_mono):
                raise RuntimeError("homing_post_load_feedback_pending")
        measured = self._measured_positions(snapshot)
        origin = measured if self.trial_motion is not None else {}
        with self.lock:
            self.trial_origin = origin
            self.trial_first_frame_pending = self.trial_motion is not None
            self.homing_first_frame_pending = self.homing_motion is not None
            self.homing_active_joint = None
            self.homing_last_target_raw = None
            self.last_target_rad.clear()
            self.last_target_mono.clear()
            self._reset_position_pid()
            self.outputs_enabled = True
            # Enable is a permission, not a command. The watchdog arms at the first
            # emitted frame: the upper needs a bounded moment to observe that the
            # emitter (and its torque verification) is ready, and that handshake must
            # not be mistaken for a silent following stream. Once a frame exists the
            # usual per-command silence window applies unchanged.
            self.last_command_mono = None
            self.last_error = None

    # Total wall budget for one load+verify pass. Exceeding it latches a failure
    # instead of retrying torque writes forever; recovery is an explicit re-engage.
    TORQUE_TOTAL_BUDGET_S = 12.0

    def _progress_homing_torque(self) -> None:
        """Advance the per-ID load/readback state machine one non-blocking step.

        Returns only when every ID is verified; otherwise raises ``pending`` (the
        caller retries next tick) or a latched ``unverified`` failure. A missing
        confirmation never enables position output, and no axis is ever unloaded.
        """
        if self.homing_torque_state == "failed":
            raise RuntimeError(f"homing_torque_unverified:{self.homing_torque_error}")
        publisher = self.servo_state_publisher
        if (publisher is None or self.bus_servo_state_type is None
                or self.set_bus_servo_state_type is None):
            raise RuntimeError("homing_torque_adapter_unavailable")
        if self.torque_readback is None:
            raise RuntimeError("homing_torque_readback_unavailable")
        if self.homing_torque_state == "idle":
            count_getter = getattr(publisher, "get_subscription_count", None)
            if callable(count_getter) and int(count_getter()) < 1:
                raise RuntimeError("no_servo_state_subscriber")
            self._torque_load_queue = [SERVO_IDS[name] for name in (*JOINTS, GRIPPER)]
            self._torque_verify_queue = list(self._torque_load_queue)
            self.homing_torque_results = {}
            self.homing_torque_error = None
            self._torque_started_mono = time.monotonic()
            self.homing_torque_state = "loading"
        if (self._torque_started_mono is not None
                and time.monotonic() - self._torque_started_mono > self.TORQUE_TOTAL_BUDGET_S):
            self.homing_torque_state = "failed"
            self.homing_torque_error = "verification_budget_exceeded"
            self.torque_readback.cancel()
            raise RuntimeError(f"homing_torque_unverified:{self.homing_torque_error}")
        if self.homing_torque_state == "loading":
            if self._torque_load_queue:
                servo_id = self._torque_load_queue.pop(0)
                message = self.set_bus_servo_state_type()
                message.duration = 0.15
                state = self.bus_servo_state_type()
                state.present_id = [1, servo_id]
                state.enable_torque = [1, 1]
                message.state.append(state)
                publisher.publish(message)
                self.torque_publish_count += 1
                self.torque_servo_ids.append(servo_id)
                self.homing_torque_results[servo_id] = {
                    "servo_id": servo_id, "requested": True,
                    "verified": False, "state": "requested", "reason": None}
            else:
                self.homing_torque_state = "verifying"
            raise RuntimeError("homing_torque_verification_pending")
        # verifying
        retired = self.torque_readback.poll()
        if retired is not None:
            servo_id, result = retired
            result["requested"] = True
            result["state"] = "verified" if result.get("verified") is True else "failed"
            self.homing_torque_results[servo_id] = result
        if self.torque_readback.in_flight:
            raise RuntimeError("homing_torque_verification_pending")
        if self._torque_verify_queue:
            self.torque_readback.start(self._torque_verify_queue.pop(0))
            raise RuntimeError("homing_torque_verification_pending")
        failed = [sid for sid, row in self.homing_torque_results.items()
                  if row.get("verified") is not True]
        if failed:
            self.homing_torque_state = "failed"
            self.homing_torque_error = ",".join(
                f"{sid}:{self.homing_torque_results[sid].get('reason')}" for sid in failed)
            raise RuntimeError(f"homing_torque_unverified:{self.homing_torque_error}")
        self.homing_torque_state = "verified"
        self.homing_torque_enabled = True
        self._torque_verified_mono = time.monotonic()

    def invalidate_torque_verification(self) -> None:
        """Drop verified torque state after a feedback owner change.

        The servos may still be loaded, but the owner that proved it is gone; the
        next explicit enable must load and read back again rather than trust it.
        """
        with self.lock:
            if self.torque_readback is not None:
                self.torque_readback.cancel()
            self.homing_torque_state = "idle"
            self.homing_torque_enabled = False
            self.homing_torque_results = {}
            self.homing_torque_error = None
            self._torque_load_queue = []
            self._torque_verify_queue = []
            self._torque_started_mono = None
            self._torque_verified_mono = None

    def _check_position(self, name: str, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"invalid_{name}_position")
        number = float(value)
        if not math.isfinite(number):
            raise RuntimeError(f"nonfinite_{name}_position")
        low, high = self.limits_rad.get(name, (-math.inf, math.inf))
        if not low <= number <= high:
            raise RuntimeError(f"{name}_outside_calibrated_limits")
        return number

    def _reset_position_pid(self) -> None:
        self._pid_integral.clear()
        self._pid_last_error.clear()
        self._pid_last_time.clear()
        self._pid_last_derivative.clear()
        self.last_pid.clear()

    def _position_pid_target(self, name: str, target: float,
                             measured: float, now: float) -> tuple[float, dict[str, Any]]:
        cfg = self.position_pid
        error = float(target) - float(measured)
        previous_error = self._pid_last_error.get(name, error)
        previous_time = self._pid_last_time.get(name)
        dt = (max(1e-3, min(0.25, now - previous_time))
              if previous_time is not None else 0.0)
        previous_derivative = self._pid_last_derivative.get(name, 0.0)
        raw_derivative = ((error - previous_error) / dt) if dt > 0 else 0.0
        derivative = (cfg["derivative_filter_alpha"] * raw_derivative
                      + (1.0 - cfg["derivative_filter_alpha"]) * previous_derivative)
        usable_error = 0.0 if abs(error) <= cfg["deadband_rad"] else error
        integral = self._pid_integral.get(name, 0.0)
        candidate_integral = integral + usable_error * dt
        correction = (cfg["kp"] * usable_error
                      + cfg["ki"] * candidate_integral
                      + cfg["kd"] * derivative)
        if abs(correction) > cfg["max_correction_rad"]:
            correction = math.copysign(cfg["max_correction_rad"], correction)
            # Do not wind the integrator while the correction is saturated in
            # the direction that grows the error.
            if usable_error == 0.0 or correction * usable_error <= 0:
                candidate_integral = integral
        candidate_integral = max(
            -cfg["integral_limit_rad"],
            min(cfg["integral_limit_rad"], candidate_integral))
        self._pid_integral[name] = candidate_integral
        self._pid_last_error[name] = error
        self._pid_last_time[name] = now
        self._pid_last_derivative[name] = derivative
        command = float(target) + correction
        low, high = self.limits_rad.get(name, (-math.inf, math.inf))
        clipped = not low <= command <= high
        command = max(low, min(high, command))
        detail = {
            "requested_rad": round(float(target), 6),
            "measured_rad": round(float(measured), 6),
            "error_rad": round(error, 6),
            "integral_rad": round(candidate_integral, 6),
            "derivative_rad_s": round(derivative, 6),
            "correction_rad": round(command - float(target), 6),
            "command_rad": round(command, 6),
            "clipped": clipped,
        }
        self.last_pid[name] = detail
        return command, detail

    def check_only(self, name: str, value: Any) -> float:
        """Limits and finiteness, with no state and no publish: admission asks this.

        The step-rate check stays out of it on purpose - the rate reference is the
        previous emitted target, which only the commit path is allowed to touch.
        """
        return self._check_position(name, value)

    def _enforce_step(self, name: str, value: float, now: float) -> None:
        previous = self.last_target_rad.get(name)
        previous_time = self.last_target_mono.get(name)
        if previous is None or previous_time is None:
            return
        dt = max(0.0, now - previous_time)
        allowed = self.max_step_rad_s * max(dt, 0.02) + 0.03
        if abs(value - previous) > allowed:
            raise RuntimeError(f"{name}_target_step_exceeded")

    def _clamp_step(self, name: str, value: float, now: float) -> float:
        previous = self.last_target_rad.get(name)
        previous_time = self.last_target_mono.get(name)
        if previous is None or previous_time is None:
            return value
        dt = max(0.0, now - previous_time)
        allowed = self.max_step_rad_s * max(dt, 0.02) + 0.03
        if value > previous + allowed:
            limited = previous + allowed
        elif value < previous - allowed:
            limited = previous - allowed
        else:
            return value
        self.step_clamp_count += 1
        self.last_step_clamp = {
            "joint": name,
            "requested": round(value, 6),
            "limited": round(limited, 6),
            "allowed": round(allowed, 6),
        }
        return limited

    def _publish(self, positions: list[tuple[str, float]], now: float) -> None:
        count_getter = getattr(self.publisher, "get_subscription_count", None)
        if callable(count_getter):
            count = int(count_getter())
            self.last_subscription_count = count
            if count < 1:
                raise RuntimeError("no_actuator_subscriber")
        message = self.servos_position_type()
        message.duration = self.duration_s
        for name, radians in positions:
            item = self.servo_position_type()
            item.id = SERVO_IDS[name]
            item.position = int(rad_to_raw(name, radians))
            message.position.append(item)
        self.publisher.publish(message)
        self.last_publish_mono = now
        self.publish_count += 1

    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        if self.trial_motion is not None:
            raise RuntimeError("trial_requires_complete_frame")
        with self.lock:
            if not self.outputs_enabled:
                raise RuntimeError("outputs_disabled")
            if not self._feedback_ok():
                self._stop_locked("feedback_lost")
                raise RuntimeError("trusted_feedback_unavailable")
            channel = command.get("channel")
            target = command.get("target")
            if not isinstance(target, dict):
                raise RuntimeError("invalid_target")
            now = self.clock()
            positions: list[tuple[str, float]] = []
            if channel == "arm":
                names = target.get("joint_names")
                values = target.get("positions_rad")
                if names != list(JOINTS) or not isinstance(values, list) or len(values) != 5:
                    raise RuntimeError("invalid_arm_target")
                for name, value in zip(JOINTS, values, strict=True):
                    checked = self._check_position(name, value)
                    self._enforce_step(name, checked, now)
                    positions.append((name, checked))
            elif channel == "gripper":
                if set(target) != {"joint_name", "position_rad"} or target.get(
                        "joint_name") != GRIPPER:
                    raise RuntimeError("invalid_gripper_target")
                checked = self._check_position(GRIPPER, target.get("position_rad"))
                self._enforce_step(GRIPPER, checked, now)
                positions.append((GRIPPER, checked))
            else:
                raise RuntimeError("arm_output_only")
            self._publish(positions, now)
            for name, value in positions:
                self.last_target_rad[name] = value
                self.last_target_mono[name] = now
            self.last_command_mono = now
            self.last_error = None
            return {
                "driver_sent": True,
                "topic": self.command_topic,
                "servo_ids": [SERVO_IDS[name] for name, _ in positions],
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
            }

    def submit_frame(self, targets: list[tuple[str, float]]) -> dict[str, Any]:
        """Emit one whole-frame write: ids 1..5 then 10, inside a single message.

        Every value is checked before anything is published, so a frame that fails on
        the gripper cannot leave the arm half-written.  One publish is still only a
        submission to the board owner: it is not a serial write completion, a
        controller acknowledgement or a movement.
        """
        names = [name for name, _ in targets]
        if names != [*JOINTS, GRIPPER]:
            raise RuntimeError("invalid_frame_order")
        with self.lock:
            if not self.outputs_enabled:
                raise RuntimeError("outputs_disabled")
            if not self._feedback_ok():
                self._stop_locked("feedback_lost")
                raise RuntimeError("trusted_feedback_unavailable")
            now = self.clock()
            checked: list[tuple[str, float]] = []
            normal_follow = self.trial_motion is None and self.homing_motion is None
            pid_details: dict[str, dict[str, Any]] = {}
            pid_measured: dict[str, float] = {}
            if normal_follow and self.position_pid["enabled"]:
                pid_measured = self._measured_positions(self.readback.read_snapshot())
            if normal_follow and not self.last_target_rad:
                # Seed the first normal-follow frame from fresh measurement so
                # reconnecting cannot create an unbounded first jump.
                measured = self._measured_positions(self.readback.read_snapshot())
                self.last_target_rad = dict(measured)
                self.last_target_mono = {
                    name: now for name in measured}
            for name, value in targets:
                number = self._check_position(name, value)
                if normal_follow:
                    if self.position_pid["enabled"]:
                        number, pid_detail = self._position_pid_target(
                            name, number, pid_measured[name], now)
                        pid_details[name] = pid_detail
                    number = self._clamp_step(name, number, now)
                else:
                    self._enforce_step(name, number, now)
                checked.append((name, number))
            homing_active = None
            homing_target_raw = None
            if self.trial_motion is not None:
                self._check_trial_frame(checked)
                emitted = [(name, value) for name, value in checked
                           if name in self.trial_motion["allowed_joints"]]
            elif self.homing_motion is not None:
                homing_active, homing_target_raw = self._check_homing_frame(checked)
                emitted = [(name, value) for name, value in checked if name == homing_active]
            else:
                emitted = checked
            self._publish(emitted, now)
            if self.trial_motion is not None:
                self.trial_first_frame_pending = False
            if self.homing_motion is not None:
                self.homing_first_frame_pending = False
                self.homing_active_joint = homing_active
                self.homing_last_target_raw = homing_target_raw
            for name, value in checked:
                self.last_target_rad[name] = value
                self.last_target_mono[name] = now
            self.last_command_mono = now
            self.last_error = None
            return {
                "driver_sent": True,
                "topic": self.command_topic,
                "servo_ids": [SERVO_IDS[name] for name, _ in emitted],
                "combined": len(emitted) == len(JOINTS) + 1,
                "single_axis_trial": self.trial_motion is not None,
                "single_axis_homing": self.homing_motion is not None,
                "homing_active_joint": homing_active,
                "homing_target_raw": homing_target_raw,
                "duration_s": self.duration_s,
                "position_pid": pid_details,
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
            }

    @staticmethod
    def _measured_positions(snapshot: dict[str, Any]) -> dict[str, float]:
        samples = snapshot.get("joint_samples") or {}
        measured = {}
        for name in (*JOINTS, GRIPPER):
            sample = samples.get(name) or {}
            raw = sample.get("raw_position")
            if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                    or not math.isfinite(raw) or not 0 <= raw <= 1000):
                raise RuntimeError(f"trial_feedback_invalid:{name}")
            measured[name] = (float(raw) - 500.0) * RADIANS_PER_TICK * DIRECTIONS[name]
        return measured

    def _check_trial_frame(self, targets: list[tuple[str, float]]) -> None:
        assert self.trial_motion is not None
        snapshot = self.readback.read_snapshot()
        if not snapshot.get("available") or snapshot.get("last_error"):
            raise RuntimeError("trusted_feedback_unavailable")
        measured = self._measured_positions(snapshot)
        first = self.trial_first_frame_pending
        axes = set(self.trial_motion["allowed_joints"])
        for name, value in targets:
            if first and abs(value - measured[name]) > self.trial_motion["first_frame_tolerance_rad"]:
                # A rejected first frame must not stay "first" forever: otherwise every
                # following target is compared with the same stale measured pose and the
                # trial can never recover. Seed the rate limiter from the measured pose
                # and require the next frame to approach under the normal step control.
                now = self.clock()
                self.trial_first_frame_pending = False
                self.last_target_rad = dict(measured)
                self.last_target_mono = {joint: now for joint in measured}
                raise RuntimeError(f"trial_first_frame_discontinuous:{name}")
            if name not in axes and abs(value - measured[name]) > self.trial_motion["locked_tolerance_rad"]:
                raise RuntimeError(f"trial_joint_locked:{name}")
            if name in axes and abs(value - self.trial_origin[name]) > self.trial_motion["max_displacement_rad"]:
                raise RuntimeError(f"trial_displacement_exceeded:{name}")

    def _check_homing_frame(self, targets: list[tuple[str, float]]) -> tuple[str, int]:
        """Validate one fresh, single-axis step toward the configured raw pose.

        This deliberately replaces the temporary cumulative 0.08 rad trial
        envelope with a per-step raw boundary. The device identity, calibrated
        radian limits, feedback freshness, stream TTL and step-rate gate remain in
        force. Exactly one target may differ from the just-measured position.
        """
        assert self.homing_motion is not None
        snapshot = self.readback.read_snapshot()
        if not snapshot.get("available") or snapshot.get("last_error"):
            raise RuntimeError("trusted_feedback_unavailable")
        measured = self._measured_positions(snapshot)
        measured_raw = {name: rad_to_raw(name, value) for name, value in measured.items()}
        target_raw = {name: rad_to_raw(name, value) for name, value in targets}
        locked = self.homing_motion["locked_tolerance_raw"]
        changed = [name for name in (*JOINTS, GRIPPER)
                   if abs(target_raw[name] - measured_raw[name]) > locked]
        if not changed:
            raise RuntimeError("homing_no_active_axis")
        if len(changed) != 1:
            raise RuntimeError("homing_multiple_axes:" + ",".join(changed))
        active = changed[0]
        step = target_raw[active] - measured_raw[active]
        if abs(step) > self.homing_motion["max_step_raw"]:
            raise RuntimeError(
                f"homing_step_exceeded:{active}:step={step}:measured={measured_raw[active]}:target={target_raw[active]}")
        goal = self.homing_motion["targets_raw"][active]
        current_error = goal - measured_raw[active]
        target_error = goal - target_raw[active]
        if current_error and (target_error * current_error < 0
                              or (target_error != 0
                                  and abs(target_error) >= abs(current_error))):
            raise RuntimeError(f"homing_direction_or_overshoot:{active}")
        if (self.homing_first_frame_pending
                and abs(step) > self.homing_motion["first_frame_tolerance_raw"]):
            self.homing_first_frame_pending = False
            raise RuntimeError(f"homing_first_frame_discontinuous:{active}")
        return active, target_raw[active]

    def _hold_positions_locked(self) -> bool:
        snapshot = self.readback.read_snapshot()
        samples = snapshot.get("joint_samples")
        if not snapshot.get("available") or not isinstance(samples, dict):
            return False
        for name in (*JOINTS, GRIPPER):
            sample = samples.get(name)
            raw = sample.get("raw_position") if isinstance(sample, dict) else None
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return False
        # Publish measured raw directly rather than reconstructing it through rad.
        message = self.servos_position_type()
        message.duration = 0.05
        homing_names = {self.homing_active_joint} if self.homing_active_joint else set()
        for name, sample in samples.items():
            if name not in SERVO_IDS:
                continue
            if self.trial_motion is not None and name not in self.trial_motion["allowed_joints"]:
                continue
            if self.homing_motion is not None and name not in homing_names:
                continue
            item = self.servo_position_type()
            item.id = SERVO_IDS[name]
            item.position = int(sample["raw_position"])
            message.position.append(item)
        if not message.position:
            return False
        self.publisher.publish(message)
        self.last_publish_mono = self.clock()
        return True

    def _stop_locked(self, reason: str) -> dict[str, Any]:
        was_enabled = self.outputs_enabled
        self.outputs_enabled = False
        self.last_command_mono = None
        self.last_target_rad.clear()
        self.last_target_mono.clear()
        self._reset_position_pid()
        self.trial_first_frame_pending = self.trial_motion is not None
        self.homing_first_frame_pending = self.homing_motion is not None
        hold_published = False
        if was_enabled and self._feedback_ok():
            hold_published = self._hold_positions_locked()
        self.homing_active_joint = None
        self.homing_last_target_raw = None
        self.stop_count += 1
        self.last_error = str(reason)
        return {
            "stopped": True,
            "reason": str(reason),
            "driver_sent": hold_published,
            "hold_published": hold_published,
            "hardware_effect": "unverified",
        }

    def stop(self, reason: str) -> dict[str, Any]:
        with self.lock:
            return self._stop_locked(reason)

    def _watchdog(self) -> None:
        with self.lock:
            if not self.outputs_enabled or self.last_command_mono is None:
                return
            if self.clock() - self.last_command_mono <= self.max_silence_s:
                return
            self._stop_locked("command_timeout")
