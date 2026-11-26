"""Car-local gamepad mapping and drive permit. No ROS or device I/O."""
from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

OFFLINE_PROFILE = "hardware_hybrid_manual_v1_offline6d_dds"
HARDWARE_TRIAL_PROFILE = "hardware_hybrid_base_trial_v1"
HARDWARE_JOINT_PROFILE = "hardware_hybrid_joint_capture_v1"
HARDWARE_TRIAL_TOPIC = "ros_robot_controller/set_motor"
HARDWARE_JOY_TOPIC = "/joy"
JOY_TOPIC = "/lelab_offline/base/joy"
TWIST_TOPIC = "/lelab_offline/base/cmd_vel"
TRACE_TOPIC = "/lelab_offline/recording/base"
PREVIEW_TOPIC = "/lelab/recording/base_gamepad_preview"
JOINT_TRACE_TOPIC = "/lelab/recording/base_gamepad"
OFFLINE_MOTOR_TOPIC = "/lelab_offline/ros_robot_controller/set_motor"
MOTOR_IDS = (1, 2, 3, 4)
# While the base is armed and idle it still repeats a zero target once a second: the
# board keeps the last command, so a single lost or skipped frame must not leave the
# previous non-zero target standing.
ZERO_HEARTBEAT_NS = 1_000_000_000
AXES = ("vx", "vy", "wz")
ZERO = (0.0, 0.0, 0.0)


def _number(value, low, high):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and low <= value <= high)


def wheel_rps(target):
    """LanderPi controller geometry, in vendor ROS motor-ID order."""
    vx, vy, wz = target
    turn = wz * (0.1368 + 0.1446) / 2
    circumference = math.pi * 0.065
    return tuple(speed / circumference for speed in (
        -(vx - vy - turn), -(vx + vy - turn), vx + vy + turn, vx - vy + turn))


def motor_message(target, motor_type, motors_type):
    message = motors_type()
    message.data = [motor_type(id=number, rps=rps)
                    for number, rps in zip(MOTOR_IDS, wheel_rps(target))]
    return message


@dataclass(frozen=True)
class Axis:
    index: int
    low: float
    center: float
    high: float
    deadzone: float
    direction: int

    @classmethod
    def from_dict(cls, config):
        axis = cls(**config)
        if (type(axis.index) is not int or axis.index < 0
                or not all(_number(v, -1, 1) for v in (axis.low, axis.center, axis.high))
                or not axis.low < axis.center < axis.high
                or not _number(axis.deadzone, 0, 0.5)
                or type(axis.direction) is not int or axis.direction not in (-1, 1)):
            raise ValueError("invalid gamepad axis calibration")
        return axis

    def normalize(self, sample):
        delta = sample - self.center
        span = self.high - self.center if delta >= 0 else self.center - self.low
        normalized = max(-1.0, min(1.0, delta / span))
        magnitude = max(0.0, abs(normalized) - self.deadzone) / (1 - self.deadzone)
        return math.copysign(magnitude, normalized) * self.direction


def offline_settings(profile):
    """Admit only an explicit offline fixture, with dedicated ROS topics."""
    raw = profile.get("local_gamepad_base")
    if raw is None:
        return None
    runtime = profile.get("runtime", {})
    recording = profile.get("recording", {})
    if (profile.get("profile") != OFFLINE_PROFILE
            or runtime.get("simulated") is not True
            or runtime.get("mode") != "offline_humble_simulated_board"
            or runtime.get("data_source") != "simulated"
            or profile.get("teleop", {}).get("chassis_locked") is not True
            or recording.get("data_identity") != "simulated_offline"
            or profile.get("training_export", {}).get("enabled") is not False
            or TRACE_TOPIC not in recording.get("topics", ())):
        raise ValueError("local gamepad base currently requires a raw-only offline profile")
    return raw


def hardware_trial_settings(profile):
    """Chassis-only trial: the vendor board owner keeps the serial port."""
    raw = profile.get("local_gamepad_base")
    if raw is None or profile.get("profile") in (OFFLINE_PROFILE, HARDWARE_JOINT_PROFILE):
        return None
    runtime = profile.get("runtime", {})
    safety = profile.get("safety", {})
    recording = profile.get("recording", {})
    if (profile.get("profile") != HARDWARE_TRIAL_PROFILE
            or runtime.get("mode") != "hardware_base_trial"
            or runtime.get("simulated") is True
            or runtime.get("data_source") != "hardware"
            or runtime.get("actuator_outputs") is not True
            or runtime.get("base_velocity_topic") is not None
            or runtime.get("wheel_odom_topic") is not None
            or profile.get("teleop", {}).get("chassis_locked") is not True
            or profile.get("teleop", {}).get("trial_motion") is not None
            or profile.get("training_export", {}).get("enabled") is not False
            or safety.get("firmware_watchdog_verified") is not False
            or recording.get("data_identity") != "hardware"
            or recording.get("training_eligible") is not False):
        raise ValueError("local gamepad hardware trial requires the chassis-trial profile")
    return raw


def hardware_joint_settings(profile):
    if profile.get("profile") != HARDWARE_JOINT_PROFILE:
        return None
    raw = profile.get("local_gamepad_base")
    runtime = profile.get("runtime", {})
    recording = profile.get("recording", {})
    if (not isinstance(raw, dict) or not isinstance(raw.get("usb_port"), str)
            or raw.get("deadman_button") is not None
            or runtime.get("mode") != "hardware_hybrid_follow"
            or runtime.get("data_source") != "hardware"
            or runtime.get("simulated") is True
            or runtime.get("actuator_outputs") is not True
            or runtime.get("output_transport") != "dds_v1"
            or runtime.get("allowed_transports") != ["dds_v1"]
            or profile.get("teleop", {}).get("chassis_locked") is not True
            or profile.get("local_gamepad_preview") is not None
            or profile.get("training_export", {}).get("enabled") is not False
            or recording.get("data_identity") != "hardware"
            or recording.get("training_eligible") is not False
            or any(JOINT_TRACE_TOPIC not in recording.get(key, ())
                   for key in ("topics", "required_topics"))):
        raise ValueError("local gamepad joint capture requires a raw-only hardware profile")
    return raw


def preview_settings(profile):
    raw = profile.get("local_gamepad_preview")
    if raw is None:
        return None
    runtime = profile.get("runtime", {})
    recording = profile.get("recording", {})
    if (set(raw) != {"usb_port"} or not isinstance(raw["usb_port"], str)
            or profile.get("local_gamepad_base") is not None
            or runtime.get("data_source") != "hardware"
            or runtime.get("simulated") is True
            or profile.get("teleop", {}).get("chassis_locked") is not True
            or recording.get("data_identity") != "hardware"
            or profile.get("training_export", {}).get("enabled") is not False
            or any(PREVIEW_TOPIC not in recording.get(key, ())
                   for key in ("topics", "required_topics"))):
        raise ValueError("local gamepad preview requires a chassis-locked raw-only hardware profile")
    return raw


class LinuxGamepadPreview:
    """Observe cached JS state without turning it into a fresh motor command."""

    def __init__(self, usb_port, *, monitor_usb=False):
        from .linux_gamepad import LinuxJoystick

        self.reader = LinuxJoystick(usb_port)
        self.monitor_usb = monitor_usb
        settings = stick_settings()
        self.axes = tuple(Axis.from_dict(settings["axes"][name]) for name in AXES)
        self.limits = tuple(settings["limits"][name] for name in AXES)
        self._stop = threading.Event()
        self._sample = self.reader.status()
        self._worker = threading.Thread(target=self._read, name="landerpi-gamepad-readonly", daemon=True)
        self._worker.start()

    def _read(self):
        from .linux_gamepad import open_hid

        hid_fd = None
        hid_generation = None
        usb_report_ns = None
        try:
            while not self._stop.is_set():
                now = time.monotonic_ns()
                self.reader.poll(now)
                sample = {**self.reader.status(), "reader_poll_mono_ns": now}
                if self.monitor_usb:
                    if (hid_fd is not None and (not sample["connected"]
                            or sample["generation"] != hid_generation)):
                        os.close(hid_fd)
                        hid_fd = None
                        usb_report_ns = None
                    error = None
                    if self.reader.hid_parent is not None:
                        try:
                            if hid_fd is None:
                                hid_fd = open_hid(self.reader.hid_parent)
                                hid_generation = sample["generation"]
                            for _ in range(32):
                                if not os.read(hid_fd, 512):
                                    raise OSError("receiver HID report stream closed")
                                usb_report_ns = now
                        except BlockingIOError:
                            pass
                        except OSError as exc:
                            error = str(exc)
                            if hid_fd is not None:
                                os.close(hid_fd)
                                hid_fd = None
                            usb_report_ns = None
                    sample.update(usb_report_mono_ns=usb_report_ns, usb_error=error)
                self._sample = sample
                self._stop.wait(0.02)
        finally:
            if hid_fd is not None:
                os.close(hid_fd)
            self.reader.close()
            self._sample = self.reader.status()

    def close(self):
        self._stop.set()
        self._worker.join(timeout=1.0)

    def status(self, now_ns):
        return preview_status(self._sample, now_ns, self.axes, self.limits)

    def snapshot(self):
        return self._sample


def preview_status(sample, now_ns, mapping, limits):
    axes = sample["axes_raw"]
    poll_ns = sample.get("reader_poll_mono_ns")
    preview = None
    if (sample["connected"] and poll_ns is not None and 0 <= now_ns - poll_ns < 300_000_000
            and all(axis.index < len(axes) and axes[axis.index] is not None for axis in mapping)):
        preview = tuple(axis.normalize(axes[axis.index] / 32767) * limit
                        for axis, limit in zip(mapping, limits))
    return {"available": True, "data_identity": "hardware", "capture_raw_only": True,
            "output_mode": "linux_preview", "activation": "stick_only", "sample": sample,
            "permitted": False, "armed": False, "reason": "physical_output_locked_rf_and_stop_unverified",
            "preview_si": preview, "preview_wheel_rps": None if preview is None else wheel_rps(preview),
            "target_si": None, "wheel_target_rps": None, "state_si": None,
            "wheel_measured_rps": None, "training_eligible": False}


class LocalGamepadBase:
    def __init__(self, config, publish: Callable[[tuple[float, float, float]], None],
                 mode: str = "offline"):
        common = {"axes", "deadman_button", "timeout_ms", "limits", "accel_limits"}
        if mode not in ("offline", "hardware_trial", "hardware_joint"):
            raise ValueError("unknown local gamepad base mode")
        self.mode = mode
        self.trial = mode == "hardware_trial"
        self.stick_only = config.get("deadman_button") is None
        expected = common | {"neutral_dwell_ms"} if self.stick_only else common
        if set(config) != expected:
            raise ValueError("invalid local gamepad settings fields")
        if not all(set(config[key]) == set(AXES) for key in ("axes", "limits", "accel_limits")):
            raise ValueError("local gamepad requires vx, vy, wz")
        self.axes = tuple(Axis.from_dict(config["axes"][key]) for key in AXES)
        self.deadman_button = config["deadman_button"]
        self.revoke_on_input_loss = self.stick_only and not self.trial
        neutral_dwell = config.get("neutral_dwell_ms", 0)
        timeout_ms = config["timeout_ms"]
        self.limits = tuple(config["limits"][key] for key in AXES)
        self.accel_limits = tuple(config["accel_limits"][key] for key in AXES)
        if (len({axis.index for axis in self.axes}) != 3
                or (not self.stick_only and (type(self.deadman_button) is not int or self.deadman_button < 0))
                or (self.stick_only and (type(neutral_dwell) is not int or not 200 <= neutral_dwell <= 1000))
                or type(timeout_ms) is not int or not 50 <= timeout_ms <= 300
                or not all(_number(value, 0.001, ceiling)
                           for value, ceiling in zip(self.limits, (0.2, 0.2, 0.5)))
                or not all(_number(value, 0.01, 1.0) for value in self.accel_limits)):
            raise ValueError("invalid local gamepad limits or deadman configuration")
        self.timeout_ns = timeout_ms * 1_000_000
        self.neutral_dwell_ns = neutral_dwell * 1_000_000
        self.publish = publish
        self.scope = None
        self.revoked_scope = None
        self.sample = None
        self.source_stamp_ns = None
        self.seq = 0
        self.last_tick_ns = None
        self.last_target = None
        self.last_dispatch_ns = None
        self.odom_episode = None
        self.command_pose = ZERO
        self.command_pose_ns = None
        self.armed = False
        self.neutral_seen = False
        self.neutral_since_ns = None
        self.previous_deadman = False
        self.reason = "base_not_permitted"

    def ingest(self, axes, buttons, *, now_ns: int, source_ns: int, ros_now_ns: int,
               source_clock: str = "ros_time"):
        try:
            raw = tuple(axes[axis.index] for axis in self.axes)
            deadman = None if self.stick_only else buttons[self.deadman_button]
            if (len(axes) > 64 or len(buttons) > 64
                    or not all(_number(value, -1, 1) for value in axes)
                    or not all(type(value) is int and value in (0, 1) for value in buttons)
                    or type(source_ns) is not int or not 0 < source_ns <= ros_now_ns
                    or ros_now_ns - source_ns >= self.timeout_ns
                    or (self.source_stamp_ns is not None and source_ns <= self.source_stamp_ns)):
                raise ValueError("invalid_or_stale_gamepad_sample")
        except (TypeError, ValueError, IndexError):
            self.stop("invalid_or_stale_gamepad_sample", now_ns)
            return
        intent = tuple(axis.normalize(value) for axis, value in zip(self.axes, raw))
        expired = self.sample is None or now_ns - self.sample["received_mono_ns"] >= self.timeout_ns
        if self.stick_only and expired and self.scope is not None and self.sample is not None:
            self.stop("gamepad_input_timeout", now_ns, revoke=self.revoke_on_input_loss)
        if expired:
            self._reset_gate()
        self.source_stamp_ns = source_ns
        self.seq += 1
        self.sample = {"seq": self.seq, "received_mono_ns": now_ns, "source_stamp_ns": source_ns,
                       "source_clock": source_clock,
                       "axes": list(axes), "buttons": list(buttons), "intent": intent,
                       "deadman": None if self.stick_only else bool(deadman)}
        if self.scope is None:
            return
        if expired and self.last_target not in (ZERO, None):
            self._dispatch(ZERO, now_ns)
        neutral = intent == ZERO
        if self.stick_only:
            if neutral:
                if self.neutral_since_ns is None:
                    self.neutral_since_ns = now_ns
                if now_ns - self.neutral_since_ns >= self.neutral_dwell_ns:
                    self.neutral_seen = self.armed = True
                if self.last_target not in (ZERO, None):
                    self._dispatch(ZERO, now_ns)
                self.last_tick_ns = None
            elif not self.armed:
                self.neutral_since_ns = None
            return
        if not deadman:
            self.armed = False
            self.neutral_seen = neutral
            if self.last_target not in (ZERO, None):
                self._dispatch(ZERO, now_ns)
        elif not self.previous_deadman:
            self.armed = self.neutral_seen and neutral
            self.neutral_seen = False
        self.previous_deadman = bool(deadman)

    def _reset_gate(self):
        self.armed = self.neutral_seen = self.previous_deadman = False
        self.neutral_since_ns = None

    def set_odom_episode(self, episode):
        if episode != self.odom_episode:
            self.odom_episode = episode
            self.command_pose = ZERO
            self.command_pose_ns = None

    def _command_pose_at(self, now_ns):
        if self.command_pose_ns is None or self.last_target in (ZERO, None):
            return self.command_pose
        dt = min(max(0, now_ns - self.command_pose_ns), self.timeout_ns) / 1e9
        x, y, yaw = self.command_pose
        vx, vy, wz = self.last_target
        half_turn = wz * dt / 2
        heading = yaw + half_turn
        scale = dt if half_turn == 0 else dt * math.sin(half_turn) / half_turn
        return (x + scale * (math.cos(heading) * vx - math.sin(heading) * vy),
                y + scale * (math.sin(heading) * vx + math.cos(heading) * vy),
                yaw + 2 * half_turn)

    def _dispatch(self, target, now_ns):
        self.publish(target)
        self.command_pose = self._command_pose_at(now_ns)
        self.command_pose_ns = now_ns
        self.last_target = target
        self.last_dispatch_ns = now_ns

    def stop(self, reason, now_ns, revoke=True):
        # A revoked scope cannot drive again until the upper grants a new scope.
        if revoke and self.stick_only and self.scope is not None:
            self.revoked_scope = self.scope
        self.scope = None
        self.sample = None
        self._reset_gate()
        self.last_tick_ns = None
        self.reason = reason
        if self.last_target not in (ZERO, None):
            self._dispatch(ZERO, now_ns)

    def tick(self, now_ns: int, scope: tuple | None, reason: str):
        if scope is None:
            self.stop(reason, now_ns, revoke=False)
            return
        if self.stick_only and scope == self.revoked_scope:
            self.stop("base_reauthorization_required", now_ns)
            return
        if scope != self.scope:
            self.stop("base_scope_changed", now_ns, revoke=False)
            self.scope = scope
            return
        if self.sample is None:
            self.reason = "waiting_for_fresh_gamepad"
            return
        if not 0 <= now_ns - self.sample["received_mono_ns"] < self.timeout_ns:
            self.stop("waiting_for_fresh_gamepad", now_ns, revoke=self.revoke_on_input_loss)
            if not self.stick_only:
                self.scope = scope
            return
        if not self.armed:
            if self.stick_only:
                self.reason = "center_sticks_to_arm"
            else:
                self.reason = "hold_deadman_at_neutral" if self.neutral_seen else "release_deadman_and_center"
            return
        if self.stick_only and self.sample["intent"] == ZERO:
            self.reason = "stick_neutral"
            if self.last_target is None or now_ns - (self.last_dispatch_ns or 0) >= ZERO_HEARTBEAT_NS:
                self._dispatch(ZERO, now_ns)
            return
        requested = tuple(value * limit for value, limit in zip(self.sample["intent"], self.limits))
        previous = self.last_target or ZERO
        dt = 0 if self.last_tick_ns is None else max(0, min(now_ns - self.last_tick_ns, 100_000_000)) / 1e9
        target = tuple(current + max(-rate * dt, min(rate * dt, want - current))
                       for current, want, rate in zip(previous, requested, self.accel_limits))
        self._dispatch(target, now_ns)
        self.last_tick_ns = now_ns
        self.reason = "stick_deflected" if self.stick_only else "deadman_held"

    def status(self, now_ns: int):
        sample = self.sample
        odom = None if self.odom_episode is None else {
            "source_kind": "command_integrated_estimate",
            "episode_id": self.odom_episode[1],
            "pose_xy_yaw": self._command_pose_at(now_ns),
            "max_command_hold_s": self.timeout_ns / 1e9,
        }
        physical = self.mode in ("hardware_trial", "hardware_joint")
        return {"available": True,
                "data_identity": "hardware" if physical else "simulated_offline",
                "permitted": self.scope is not None,
                "capture_raw_only": True,
                "output_mode": "hardware_set_motor" if physical else "simulated_offline",
                "reauthorization_required": self.reason == "base_reauthorization_required",
                "activation": "stick_only" if self.stick_only else "deadman_button",
                "armed": self.armed, "neutral_seen": self.neutral_seen, "reason": self.reason,
                "input_age_s": None if sample is None else (now_ns - sample["received_mono_ns"]) / 1e9,
                "sample": sample, "target_si": self.last_target, "state_si": None,
                "command_odometry": odom,
                "motor_ids": MOTOR_IDS,
                "wheel_target_rps": None if self.last_target is None else wheel_rps(self.last_target),
                "wheel_measured_rps": None, "board_ack": None,
                "physical_stop": "unverified" if physical else None,
                "dispatch_stage": None if self.last_dispatch_ns is None else (
                    "owner_topic_publish_returned" if physical else "simulated_ros_publish_returned"),
                "last_dispatch_mono_ns": self.last_dispatch_ns, "training_eligible": False}


def stick_settings():
    return {
        "axes": {name: {"index": index, "low": -1.0, "center": 0.0, "high": 1.0,
                        "direction": -1, "deadzone": 0.1} for name, index in zip(AXES, (1, 0, 2))},
        "deadman_button": None,
        "neutral_dwell_ms": 250,
        "timeout_ms": 300,
        "limits": {"vx": 0.15, "vy": 0.15, "wz": 0.3},
        "accel_limits": {"vx": 0.3, "vy": 0.3, "wz": 0.5},
    }


def make_offline_profile(profile):
    """Extend the isolated DDS fixture without duplicating its arm/camera configuration."""
    import copy

    profile = copy.deepcopy(profile)
    if TRACE_TOPIC not in profile["recording"]["topics"]:
        profile["recording"]["topics"].append(TRACE_TOPIC)
    profile["local_gamepad_base"] = stick_settings()
    offline_settings(profile)
    return profile
