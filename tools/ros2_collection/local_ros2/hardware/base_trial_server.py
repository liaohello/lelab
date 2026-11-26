#!/usr/bin/env python3
"""LanderPi chassis trial UI: js0 -> mapping -> ros_robot_controller/set_motor.

Single board owner: this process never opens /dev/rrc.  The vendor
ros_robot_controller node owns the serial port; this page only publishes
MotorsState on its set_motor topic.  Outputs stay zero until the page is
heartbeating, the stick is centred for the dwell window, and the operator
presses 解锁.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AXES = (
    {"name": "vx", "index": 1, "direction": -1, "label": "左摇杆 前后  →  vx"},
    {"name": "vy", "index": 0, "direction": -1, "label": "左摇杆 左右  →  vy"},
    {"name": "wz", "index": 2, "direction": -1, "label": "右摇杆 左右  →  wz"},
)
MOTOR_IDS = (1, 2, 3, 4)
WHEELBASE, TRACK, WHEEL_DIAMETER = 0.1368, 0.1446, 0.065
HEARTBEAT_TIMEOUT_S = 8.0
HID_STALE_S = 0.35
NEUTRAL_DWELL_S = 0.25


def wheel_rps(vx: float, vy: float, wz: float):
    """Vendor MecanumChassis.set_velocity geometry and ROS motor order."""
    turn = wz * (WHEELBASE + TRACK) / 2
    circumference = math.pi * WHEEL_DIAMETER
    return [round(v / circumference, 4) for v in (
        -(vx - vy - turn), -(vx + vy - turn), vx + vy + turn, vx - vy + turn)]



TESTS = [
    {"id": "forward", "title": "前向行驶与轮向",
     "steps": ["点开始（需先解锁）", "左摇杆缓慢推到最前，保持 2 s", "松手回中"],
     "operator": "车头朝前直行，没有斜走或自转"},
    {"id": "stop", "title": "松杆停转",
     "steps": ["点开始", "推杆到 0.15 以上保持 1 s", "直接松手，不要点急停"],
     "operator": "四个轮子完全停住，没有蠕动"},
    {"id": "estop", "title": "急停发零",
     "steps": ["点开始", "推杆让车动起来", "行驶中立刻点顶部“急停·发零”"],
     "operator": "车立刻停住，不是缓慢减速"},
    {"id": "strafe", "title": "横移方向",
     "steps": ["点开始", "左摇杆向右拨到底，保持 1.5 s", "回中"],
     "operator": "车向右平移，车头不转"},
    {"id": "yaw", "title": "偏航方向与轮位",
     "steps": ["点开始", "右摇杆向右拨到底，保持 1.5 s", "回中"],
     "operator": "原地顺时针转；左侧两轮向前、右侧两轮向后"},
    {"id": "linkloss", "title": "断链停车",
     "steps": ["点开始（会临时把限幅降到 0.1）", "左摇杆前推到底并保持",
               "由第二个人关闭发射器电源或把发射器拿远", "观察车是否停；结束后重新配对"],
     "operator": "发射器断电后车停住了"},
    {"id": "distance", "title": "命令积分 vs 实测距离",
     "steps": ["点开始", "直线向前开约 2 m 后松杆停住", "用卷尺量车实际前进的距离并填在下面"],
     "operator": "页面估计位姿与卷尺距离接近"},
]
TEST_INDEX = {item["id"]: item for item in TESTS}


def _sustained(rows, pred, seconds):
    start = None
    for row in rows:
        if pred(row):
            if start is None:
                start = row["t"]
            if (row["t"] - start) / 1e9 >= seconds:
                return True
        else:
            start = None
    return False


def _sign(value):
    return 0 if abs(value) < 1e-6 else (1 if value > 0 else -1)


def _check(key, label, state, detail=""):
    return {"key": key, "label": label, "state": state, "detail": detail}


class Trial:
    def __init__(self, port: str, publish: bool, domain: int | None, trace_dir: str):
        self.lock = threading.Lock()
        self.port = port
        self.publish_enabled = publish
        self.trace_dir = trace_dir
        os.makedirs(trace_dir, exist_ok=True)
        self.trace_path = os.path.join(trace_dir, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-base-trial.jsonl")
        self.trace = open(self.trace_path, "a", encoding="utf-8")

        self.cfg = {
            "limits": {"vx": 0.15, "vy": 0.15, "wz": 0.30},
            "accel": {"vx": 0.30, "vy": 0.30, "wz": 0.50},
            "deadzone": 0.10,
            "center": {"vx": 0.0, "vy": 0.0, "wz": 0.0},
            "span_low": {"vx": 1.0, "vy": 1.0, "wz": 1.0},
            "span_high": {"vx": 1.0, "vy": 1.0, "wz": 1.0},
            "require_page": False,
        }
        self.cfg_path = os.path.join(trace_dir, "base-trial-config.json")
        if os.path.exists(self.cfg_path):
            try:
                with open(self.cfg_path, encoding="utf-8") as handle:
                    self.cfg.update(json.load(handle))
            except (OSError, ValueError):
                pass
        self.raw_min = {axis["name"]: None for axis in AXES}
        self.raw_max = {axis["name"]: None for axis in AXES}
        self.axes_raw = [None] * 8
        self.buttons_raw = [None] * 16
        self.connected = False
        self.generation = 0
        self.reader_error = None
        self.hid_axis = [None] * 4
        self.hid_reports = 0
        self.hid_window = 0
        self.hid_window_start = time.monotonic()
        self.hid_rate = 0.0
        self.hid_last_ns = None
        self.js_last_change_ns = None
        self.board_joy = None
        self.board_joy_ns = None
        self.board_owner = False
        self.armed = False
        self.reason = "waiting_operator"
        self.neutral_since_ns = None
        self.neutral_seen = False
        self.target = (0.0, 0.0, 0.0)
        self.published = (0.0, 0.0, 0.0)
        self.publish_count = 0
        self.publish_error = None
        self.pose = [0.0, 0.0, 0.0]
        self.pose_ns = None
        self.path = deque(maxlen=40000)
        self.path_ns = 0
        self.path_length = 0.0
        self.path_save_ns = 0
        self.last_zero_ns = None
        self.samples = deque(maxlen=6000)
        self.eval_tick = 0
        self.test = {"id": None, "state": "idle", "started_ns": None, "checks": [],
                     "metrics": {}, "operator": None, "note": "", "estop_ns": None,
                     "saved_limits": None}
        self.results = {}
        self.results_path = os.path.join(trace_dir, "base-trial-tests.json")
        self.path_path = os.path.join(trace_dir, "base-trial-path.json")
        if os.path.exists(self.path_path):
            try:
                with open(self.path_path, encoding="utf-8") as handle:
                    saved = json.load(handle)
                self.pose = [float(v) for v in saved.get("pose", [0, 0, 0])]
                self.path_length = float(saved.get("length_m") or 0.0)
                self.path.extend(tuple(p) for p in saved.get("points", []))
            except (OSError, ValueError, TypeError):
                pass
        if os.path.exists(self.results_path):
            try:
                with open(self.results_path, encoding="utf-8") as handle:
                    self.results = {item["id"]: item for item in json.load(handle)}
            except (OSError, ValueError, KeyError):
                self.results = {}
        self.log = deque(maxlen=400)
        self.last_heartbeat_ns = None
        self.presence_ns = None
        self.presence_clients = 0
        self.started_ns = time.monotonic_ns()
        self.t0 = time.monotonic()
        self.events = 0
        self.last_js_event = None

    # ---------------------------------------------------------------- logging
    def _save_cfg(self):
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as handle:
                json.dump(self.cfg, handle, indent=2)
        except OSError:
            pass

    def record(self, kind: str, **fields):
        row = {"kind": kind, "mono_ns": time.monotonic_ns(), **fields}
        self.trace.write(json.dumps(row) + "\n")
        self.trace.flush()
        if kind != "sample":
            with self.lock:
                self.log.appendleft(row)

    # ------------------------------------------------------------------ input
    def read_loop(self):
        from lelab_mobile_agent.linux_gamepad import LinuxJoystick, open_hid
        reader = LinuxJoystick(self.port)
        hid_fd = None
        while True:
            now = time.monotonic_ns()
            events = reader.poll(now)
            status = reader.status()
            if len(self.axes_raw) != len(status["axes_raw"]):
                self.axes_raw = list(status["axes_raw"])
                self.buttons_raw = list(status["buttons_raw"])
            if reader.hid_parent is not None and hid_fd is None:
                try:
                    hid_fd = open_hid(reader.hid_parent)
                except OSError as exc:
                    self.reader_error = f"hidraw: {exc}"
            for event in events:
                self.events += 1
                self.last_js_event = event
                if not event["initial"]:
                    self.js_last_change_ns = now
                if event["target"] == "axis" and event["index"] < len(self.axes_raw):
                    self.axes_raw[event["index"]] = event["value"]
                elif event["target"] == "button" and event["index"] < len(self.buttons_raw):
                    self.buttons_raw[event["index"]] = event["value"]
                self.record("js_event", index=event["index"], value=event["value"],
                            target=event["target"], initial=event["initial"])
            if hid_fd is not None:
                try:
                    for _ in range(64):
                        data = os.read(hid_fd, 512)
                        if not data:
                            raise OSError("hid EOF")
                        self.hid_reports += 1
                        self.hid_window += 1
                        self.hid_last_ns = now
                        if len(data) >= 7:
                            self.hid_axis = list(data[3:7])
                except BlockingIOError:
                    pass
                except OSError as exc:
                    self.reader_error = f"hidraw: {exc}"
                    os.close(hid_fd)
                    hid_fd = None
            if now - self.hid_window_start >= 1_000_000_000:
                self.hid_rate = self.hid_window / ((now - self.hid_window_start) / 1e9)
                self.hid_window = 0
                self.hid_window_start = now
            self.connected = status["connected"]
            self.generation = status["generation"]
            if status["error"]:
                self.reader_error = status["error"]
            # learn the physical range so a stick with a biased rest can still be mapped
            for axis in AXES:
                index = axis["index"]
                if index < len(self.axes_raw) and self.axes_raw[index] is not None:
                    value = self.axes_raw[index] / 32767
                    name = axis["name"]
                    self.raw_min[name] = value if self.raw_min[name] is None else min(self.raw_min[name], value)
                    self.raw_max[name] = value if self.raw_max[name] is None else max(self.raw_max[name], value)
            time.sleep(0.004)

    def normalize(self, name: str):
        axis = next(item for item in AXES if item["name"] == name)
        axes = self.axes_raw
        raw = axes[axis["index"]] if axis["index"] < len(axes) else None
        if raw is None:
            return 0.0
        value = raw / 32767
        center = self.cfg["center"][name]
        delta = value - center
        span = self.cfg["span_high"][name] - center if delta >= 0 else center - self.cfg["span_low"][name]
        span = max(span, 0.05)
        normalized = max(-1.0, min(1.0, delta / span))
        deadzone = self.cfg["deadzone"]
        magnitude = max(0.0, abs(normalized) - deadzone) / (1 - deadzone)
        return math.copysign(magnitude, normalized) * axis["direction"]

    # --------------------------------------------------------------- control
    def control_loop(self, node=None, publisher=None):
        last = time.monotonic_ns()
        while True:
            time.sleep(0.02)
            now = time.monotonic_ns()
            dt = min(max(0.0, (now - last) / 1e9), 0.1)
            last = now
            intent = {axis["name"]: self.normalize(axis["name"]) for axis in AXES}
            neutral = all(value == 0.0 for value in intent.values())
            owner = self._board_owner(node) if node is not None else False
            self.board_owner = owner
            reason = self._gate(now, intent, neutral, owner)
            if reason is not None:
                was_armed = self.armed
                self.armed = False
                self.reason = reason
                if was_armed:
                    self.record("disarm", reason=reason, target=list(self.published))
                force = self.last_zero_ns is None or (now - self.last_zero_ns) / 1e9 > 1.0
                self._dispatch(node, publisher, (0.0, 0.0, 0.0), now, dt, force=force)
            else:
                self.reason = "stick_deflected" if not neutral else "armed_neutral"
                wanted = tuple(intent[axis["name"]] * self.cfg["limits"][axis["name"]] for axis in AXES)
                current = self.published
                target = []
                for name, want, cur in zip(("vx", "vy", "wz"), wanted, current):
                    step = self.cfg["accel"][name] * dt
                    target.append(round(cur + max(-step, min(step, want - cur)), 4))
                self.target = tuple(target)
                self._dispatch(node, publisher, self.target, now, dt)
            hid_age = 1e9 if self.hid_last_ns is None else (now - self.hid_last_ns) / 1e9
            self._sample(now, intent, wheel_rps(*self.published), hid_age)

    def _gate(self, now, intent, neutral, owner):
        if not self.publish_enabled:
            return "read_only_mode"
        if not self.connected:
            return "gamepad_disconnected"
        if self.hid_last_ns is None or (now - self.hid_last_ns) / 1e9 > HID_STALE_S:
            return "receiver_reports_stopped"
        if not owner:
            return "board_owner_missing"
        presence = max(filter(None, (self.presence_ns, self.last_heartbeat_ns)), default=None)
        if (self.armed and self.cfg.get("require_page", True)
                and self.published != (0.0, 0.0, 0.0)
                and (presence is None or (now - presence) / 1e9 > HEARTBEAT_TIMEOUT_S)):
            return "page_heartbeat_lost"
        if any(abs(value) > 1.2 for value in intent.values()):
            return "axis_calibration_invalid"
        if not self.armed:
            if not neutral:
                self.neutral_since_ns = None
                self.neutral_seen = False
                return "center_sticks_to_arm"
            if self.neutral_since_ns is None:
                self.neutral_since_ns = now
            if (now - self.neutral_since_ns) / 1e9 < NEUTRAL_DWELL_S:
                return "centering"
            self.neutral_seen = True
            return "ready_to_arm"
        return None

    def _dispatch(self, node, publisher, target, now, dt, force=False):
        if publisher is None:
            self.published = target
            self.pose_ns = now
            return
        if not force and target == self.published:
            self._integrate(target, now, dt)
            return
        from ros_robot_controller_msgs.msg import MotorState, MotorsState
        message = MotorsState()
        message.data = [MotorState(id=motor_id, rps=rps)
                        for motor_id, rps in zip(MOTOR_IDS, wheel_rps(*target))]
        try:
            publisher.publish(message)
            self.publish_error = None
        except Exception as exc:  # noqa: BLE001 - surface, never swallow silently
            self.publish_error = f"{type(exc).__name__}: {exc}"
            self.armed = False
            return
        self.publish_count += 1
        if target == (0.0, 0.0, 0.0):
            self.last_zero_ns = now
        self._integrate(target, now, dt)
        self.published = target
        self.record("dispatch", target=list(target), wheel_rps=wheel_rps(*target),
                    source="gamepad_stick", armed=self.armed, reason=self.reason,
                    command_integrated_estimate=[round(v, 4) for v in self.pose])

    def _integrate(self, target, now, dt):
        if self.pose_ns is None:
            self.pose_ns = now
            return
        vx, vy, wz = target
        if vx == 0 and vy == 0 and wz == 0:
            return
        x, y, yaw = self.pose
        half = wz * dt / 2
        heading = yaw + half
        scale = dt if half == 0 else dt * math.sin(half) / half
        self.pose = [x + scale * (math.cos(heading) * vx - math.sin(heading) * vy),
                     y + scale * (math.sin(heading) * vx + math.cos(heading) * vy),
                     yaw + 2 * half]
        self.pose_ns = now
        if self.path and now - self.path_ns < 100_000_000:
            return
        if self.path:
            px, py = self.path[-1]
            self.path_length += math.hypot(self.pose[0] - px, self.pose[1] - py)
        self.path.append((round(self.pose[0], 4), round(self.pose[1], 4)))
        self.path_ns = now
        if now - self.path_save_ns > 2_000_000_000:
            self.path_save_ns = now
            try:
                with open(self.path_path, "w", encoding="utf-8") as handle:
                    json.dump({"pose": [round(v, 4) for v in self.pose],
                               "length_m": round(self.path_length, 3),
                               "points": list(self.path)[-4000:]}, handle)
            except OSError:
                pass

    def _board_owner(self, node):
        try:
            return "ros_robot_controller" in node.get_node_names()
        except Exception:  # noqa: BLE001 - a dead graph is a missing owner
            return False


    # ------------------------------------------------------------- test engine
    def _window(self):
        start = self.test["started_ns"]
        rows = []
        for row in reversed(self.samples):
            if start is None or row["t"] < start:
                break
            rows.append(row)
        rows.reverse()
        return rows

    def _sample(self, now, intent, wheels, hid_age):
        self.samples.append({"t": now, "vx": self.published[0], "vy": self.published[1],
                             "wz": self.published[2],
                             "intent": [intent[name] for name in ("vx", "vy", "wz")],
                             "wheels": list(wheels), "armed": self.armed, "hid_age": hid_age,
                             "axes": list(self.axes_raw), "events": self.events})
        if self.test["state"] != "running":
            return
        self.eval_tick += 1
        if self.eval_tick % 3 == 0:
            self._evaluate()

    def start_test(self, test_id):
        if self.test["state"] == "running":
            return "test_already_running"
        if not self.publish_enabled:
            return "read_only_mode"
        if not self.armed:
            return "arm_outputs_first"
        item = TEST_INDEX.get(test_id)
        if item is None:
            return "unknown_test"
        saved = None
        if test_id == "linkloss":
            saved = dict(self.cfg["limits"])
            self.cfg["limits"].update({"vx": 0.1, "vy": 0.1, "wz": 0.3})
        self.test = {"id": test_id, "state": "running", "started_ns": time.monotonic_ns(),
                     "checks": [], "metrics": {}, "operator": None, "note": "",
                     "estop_ns": None, "saved_limits": saved}
        self.record("test_start", test=test_id, limits=dict(self.cfg["limits"]))
        return None

    def abort_test(self, reason="operator"):
        if self.test["state"] != "running":
            return "no_active_test"
        self._finish("aborted", None, reason)
        return None

    def verdict(self, passed, note, value):
        if self.test["state"] != "running":
            return "no_active_test"
        checks = self.test["checks"]
        if passed and (not checks or any(item["state"] != "pass" for item in checks)):
            return "checks_incomplete"
        auto_failed = any(item["state"] == "fail" for item in checks)
        status = "pass" if passed and not auto_failed else ("fail" if not passed else "pass_with_notes")
        self._finish(status, bool(passed), note, value)
        return None

    def _finish(self, status, operator, note, value=None):
        test = self.test
        result = {"id": test["id"], "title": TEST_INDEX.get(test["id"], {}).get("title", test["id"]),
                  "status": status, "operator_pass": operator, "note": note or "",
                  "checks": test["checks"], "metrics": test["metrics"],
                  "measured_m": value, "ended_wall": time.time()}
        if test["id"] == "distance" and value:
            pose_x = abs(float(test["metrics"].get("pose_x") or 0.0))
            error = abs(pose_x - float(value)) / max(float(value), 1e-6) * 100
            result["distance_error_pct"] = round(error, 1)
            result["checks"] = [*test["checks"], _check(
                "error", "估计距离与卷尺误差 ≤ 15%",
                "pass" if error <= 15 else "fail", f"{error:.1f}%")]
        self.results[test["id"]] = result
        try:
            with open(self.results_path, "w", encoding="utf-8") as handle:
                json.dump(list(self.results.values()), handle, indent=2)
        except OSError:
            pass
        if test["saved_limits"]:
            self.cfg["limits"].update(test["saved_limits"])
        self.record("test_finish", test=test["id"], status=status, metrics=test["metrics"],
                    operator=operator, note=note or "", measured_m=value)
        test["state"] = "idle"

    def _evaluate(self):
        rows = self._window()
        if not rows:
            return
        test_id = self.test["id"]
        handler = getattr(self, f"_checks_{test_id}", None)
        if handler is None:
            return
        checks, metrics = handler(rows)
        self.test["checks"] = checks
        self.test["metrics"] = metrics

    def _checks_forward(self, rows):
        moved = _sustained(rows, lambda r: r["vx"] >= 0.05, 0.3)
        checks = [_check("drive", "vx ≥ 0.05 m/s 并保持 0.3 s", "pass" if moved else "pending")]
        metrics = {"peak_vx": round(max(r["vx"] for r in rows), 4),
                   "peak_wheels": max(rows, key=lambda r: r["vx"])["wheels"]}
        if moved:
            active = [r for r in rows if r["vx"] >= 0.05]
            clean = all(abs(r["vy"]) <= 0.02 and abs(r["wz"]) <= 0.03 for r in active)
            checks.append(_check("decoupled", "没有混入 vy / wz", "pass" if clean else "fail",
                                 f"max |vy|={max(abs(r['vy']) for r in active):.3f} "
                                 f"max |wz|={max(abs(r['wz']) for r in active):.3f}"))
            peak = max(rows, key=lambda r: r["vx"])["wheels"]
            ordered = peak[0] < 0 and peak[1] < 0 and peak[2] > 0 and peak[3] > 0
            checks.append(_check("wheel_signs", "四轮符号 M1− M2− M3+ M4+",
                                 "pass" if ordered else "fail",
                                 " ".join(f"{v:+.2f}" for v in peak)))
        return checks, metrics

    def _checks_stop(self, rows):
        release = None
        for index in range(1, len(rows)):
            if all(abs(v) < 1e-9 for v in rows[index]["intent"]) and                     any(abs(rows[index - 1][k]) > 1e-9 for k in ("vx", "vy", "wz")):
                release = rows[index]["t"]
                break
        checks = [_check("release", "检测到松杆", "pass" if release else "pending")]
        metrics = {}
        if release:
            stopped = next((r["t"] for r in rows if r["t"] >= release
                            and r["vx"] == 0 and r["vy"] == 0 and r["wz"] == 0), None)
            stop_ms = None if stopped is None else round((stopped - release) / 1e6, 1)
            metrics["stop_ms"] = stop_ms
            ok = stop_ms is not None and stop_ms <= 800
            checks.append(_check("stop_time", "松杆到四轮目标归零 ≤ 800 ms",
                                 "pass" if ok else ("fail" if stop_ms is not None else "pending"),
                                 f"{stop_ms} ms" if stop_ms is not None else "尚未归零"))
            if stopped:
                after = [r for r in rows if stopped <= r["t"] <= stopped + 1_000_000_000]
                held = all(r["vx"] == 0 and r["vy"] == 0 and r["wz"] == 0 for r in after)
                elapsed = rows[-1]["t"] - stopped >= 1_000_000_000
                checks.append(_check("stay_zero", "归零后 1 s 内保持为零",
                                     "fail" if not held else "pass" if elapsed else "pending"))
        return checks, metrics

    def _checks_estop(self, rows):
        estop = self.test.get("estop_ns")
        checks = [_check("pressed", "检测到急停按钮", "pass" if estop else "pending")]
        metrics = {}
        if estop:
            zero = next((r["t"] for r in rows if r["t"] >= estop
                         and r["vx"] == 0 and r["vy"] == 0 and r["wz"] == 0), None)
            ms = None if zero is None else round((zero - estop) / 1e6, 1)
            metrics["estop_ms"] = ms
            checks.append(_check("zero_latency", "急停到发零 ≤ 100 ms",
                                 "pass" if ms is not None and ms <= 100 else
                                 ("fail" if ms is not None else "pending"),
                                 f"{ms} ms" if ms is not None else "尚未发零"))
        return checks, metrics

    def _checks_strafe(self, rows):
        moved = _sustained(rows, lambda r: r["vy"] <= -0.05, 0.3)
        checks = [_check("drive", "vy ≤ −0.05 m/s 并保持 0.3 s", "pass" if moved else "pending")]
        metrics = {"peak_vy": round(min(r["vy"] for r in rows), 4),
                   "peak_wheels": min(rows, key=lambda r: r["vy"])["wheels"]}
        if moved:
            active = [r for r in rows if r["vy"] <= -0.05]
            clean = all(abs(r["vx"]) <= 0.02 and abs(r["wz"]) <= 0.03 for r in active)
            checks.append(_check("decoupled", "没有混入 vx / wz", "pass" if clean else "fail"))
            peak = min(rows, key=lambda r: r["vy"])["wheels"]
            ordered = peak[0] < 0 and peak[1] > 0 and peak[2] < 0 and peak[3] > 0
            checks.append(_check("wheel_signs", "四轮符号 M1− M2+ M3− M4+",
                                 "pass" if ordered else "fail",
                                 " ".join(f"{v:+.2f}" for v in peak)))
        return checks, metrics

    def _checks_yaw(self, rows):
        moved = _sustained(rows, lambda r: r["wz"] <= -0.05, 0.3)
        checks = [_check("drive", "wz ≤ −0.05 rad/s 并保持 0.3 s", "pass" if moved else "pending")]
        metrics = {"peak_wz": round(min(r["wz"] for r in rows), 4)}
        if moved:
            active = [r for r in rows if r["wz"] <= -0.05]
            clean = all(abs(r["vx"]) <= 0.02 and abs(r["vy"]) <= 0.02 for r in active)
            checks.append(_check("decoupled", "没有混入 vx / vy", "pass" if clean else "fail"))
            peak = min(rows, key=lambda r: r["wz"])["wheels"]
            same = len({_sign(v) for v in peak}) == 1 and _sign(peak[0]) != 0
            checks.append(_check("wheel_signs", "四轮同号（左右电机镜像安装）",
                                 "pass" if same else "fail",
                                 " ".join(f"{v:+.2f}" for v in peak)))
            metrics["peak_wheels"] = peak
        return checks, metrics

    def _checks_linkloss(self, rows):
        """Two independent ways a lost link may reach us: the receiver stops reporting,
        or it drops the axes to neutral in one step.  A normal release never looks like
        the second: it arrives as a ramp of several js events, not one jump."""
        gap = next((r for r in rows if r["hid_age"] > 0.35), None)
        drop = None
        for index in range(1, len(rows)):
            before, after = rows[index - 1], rows[index]
            held = max(abs(v) for v in before["intent"]) >= 0.5
            released = all(v == 0.0 for v in after["intent"])
            single_event = after["events"] - before["events"] == 1
            if held and released and single_event:
                drop = after
                break
        if drop is None:
            checks = [_check("gap", "接收器 HID 报告中断", "pass" if gap else "pending",
                             "" if gap else "接收器仍在按原频率上报")]
        else:
            checks = [_check("gap", "保持偏转时输入单步归零（接收器 failsafe）", "pass",
                             f"无断报；轴在测试内 {(drop['t'] - rows[0]['t']) / 1e9:.1f} s 处一帧归零")]
        metrics = {}
        moment = gap["t"] if gap else (drop["t"] if drop else None)
        if moment is not None:
            zero = next((r["t"] for r in rows if r["t"] >= moment
                         and r["vx"] == 0 and r["vy"] == 0 and r["wz"] == 0), None)
            ms = None if zero is None else round((zero - moment) / 1e6, 1)
            metrics["loss_to_zero_ms"] = ms
            metrics["path"] = "hid_gap" if gap else "axis_step_to_neutral"
            checks.append(_check("stop", "失联后 1 s 内四轮目标归零",
                                 "pass" if ms is not None and ms <= 1000 else
                                 ("fail" if ms is not None else "pending"),
                                 f"{ms} ms" if ms is not None else "尚未发零"))
        return checks, metrics

    def _checks_distance(self, rows):
        moved = _sustained(rows, lambda r: r["vx"] >= 0.05, 0.3)
        checks = [_check("drive", "已经开动", "pass" if moved else "pending")]
        return checks, {"pose_x": round(self.pose[0], 3), "pose_y": round(self.pose[1], 3)}

    def report(self):
        now = time.time()
        return {"kind": "landerpi_base_trial_report", "generated_wall": now,
                "data_identity": "hardware", "board_owner_present": self.board_owner,
                "calibration": {key: self.cfg[key] for key in ("center", "deadzone", "limits", "accel")},
                "wheel_geometry": {"wheelbase_m": WHEELBASE, "track_m": TRACK,
                                   "wheel_diameter_m": WHEEL_DIAMETER,
                                   "motor_ids": list(MOTOR_IDS)},
                "command_odometry_source": "command_integrated_estimate",
                "results": [self.results[item["id"]] for item in TESTS if item["id"] in self.results],
                "missing": [item["id"] for item in TESTS if item["id"] not in self.results],
                "progress": {"pass": sum(1 for r in self.results.values() if r["status"] == "pass"),
                             "fail": sum(1 for r in self.results.values() if r["status"].startswith("fail")),
                             "total": len(TESTS)},
                "trace_path": self.trace_path}

    def tests_status(self):
        return {"plan": TESTS, "active": {**self.test, "started_ns": None},
                "results": list(self.results.values()),
                "progress": {"pass": sum(1 for r in self.results.values() if r["status"] == "pass"),
                             "fail": sum(1 for r in self.results.values() if r["status"].startswith("fail")),
                             "total": len(TESTS)}}

    # ----------------------------------------------------------------- status
    def status(self):
        now = time.monotonic_ns()
        with self.lock:
            log = list(self.log)[:60]
        return {
            "t": round((now - self.started_ns) / 1e9, 3),
            "js": {
                "connected": self.connected, "generation": self.generation,
                "axes_raw": self.axes_raw, "buttons_raw": self.buttons_raw,
                "hid_axis": self.hid_axis, "hid_rate": round(self.hid_rate, 1),
                "events": self.events, "error": self.reader_error,
                "last_change_age_s": None if self.js_last_change_ns is None else round((now - self.js_last_change_ns) / 1e9, 3),
            },
            "intent": {axis["name"]: round(self.normalize(axis["name"]), 4) for axis in AXES},
            "cfg": self.cfg,
            "range": {name: [None if self.raw_min[name] is None else round(self.raw_min[name], 4),
                             None if self.raw_max[name] is None else round(self.raw_max[name], 4)]
                      for name in ("vx", "vy", "wz")},
            "out": {
                "publish_enabled": self.publish_enabled, "board_owner": self.board_owner,
                "armed": self.armed, "reason": self.reason,
                "neutral_seen": self.neutral_seen, "publish_count": self.publish_count,
                "publish_error": self.publish_error, "target": list(self.published),
                "wheel_rps": wheel_rps(*self.published),
                "pose_command_integrated_estimate": [round(v, 4) for v in self.pose],
                "trace": self.trace_path,
                "presence_clients": self.presence_clients,
                "require_page": bool(self.cfg.get("require_page", True)),
                "heartbeat_age_s": None if max(filter(None, (self.presence_ns, self.last_heartbeat_ns)), default=None) is None
                else round((now - max(filter(None, (self.presence_ns, self.last_heartbeat_ns)))) / 1e9, 3),
            },
            "board_joy": None if self.board_joy is None else {
                "axes": self.board_joy["axes"], "buttons": self.board_joy["buttons"],
                "age_s": round((now - self.board_joy["mono_ns"]) / 1e9, 2)},
            "log": log,
            "tests": self.tests_status(),
        }

    def close(self):
        try:
            self.trace.close()
        except Exception:
            pass


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LanderPi 底盘试验台</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--text:#1b1f24;--muted:#6b7280;--blue:#2563eb;--red:#dc2626;--green:#15803d;--amber:#b45309}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;background:#f1f3f5;color:var(--muted);font-size:12px}
.pill b{font-weight:600;color:var(--text)}
.dot{width:8px;height:8px;border-radius:50%;background:#9ca3af}
.dot.ok{background:var(--green)}.dot.bad{background:var(--red)}.dot.warn{background:var(--amber)}
.spacer{flex:1}
button{font:inherit;border:1px solid var(--line);background:#fff;color:var(--text);padding:8px 14px;border-radius:8px;cursor:pointer}
button:hover{background:#f8fafc}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
button.primary:disabled{background:#c7d2fe;border-color:#c7d2fe;cursor:not-allowed}
button.danger{background:var(--red);border-color:var(--red);color:#fff;font-weight:650;padding:10px 22px}
main{max-width:1180px;margin:0 auto;padding:18px 20px 60px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 12px;font-weight:650}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.grid2{display:grid;grid-template-columns:1.35fr 1fr;gap:16px}
.axis{display:grid;gap:8px}
.axis .top{display:flex;justify-content:space-between;align-items:baseline}
.axis .name{font-weight:650}
.axis .raw{font-variant-numeric:tabular-nums;font-size:20px}
.bar{position:relative;height:12px;background:#eef1f4;border-radius:6px;overflow:hidden}
.bar i{position:absolute;top:0;bottom:0;left:50%;width:0;background:var(--blue);border-radius:6px}
.bar u{position:absolute;top:-3px;bottom:-3px;width:2px;background:#94a3b8}
.sub{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.wheels{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px}
.wheel{background:#f8fafc;border:1px solid var(--line);border-radius:10px;padding:10px 12px;display:flex;justify-content:space-between;align-items:center}
.wheel b{font-size:18px;font-variant-numeric:tabular-nums}
.chassis{position:relative;display:grid;place-items:center;padding:10px 0 4px}
.body{width:150px;height:190px;border:2px solid #cbd5e1;border-radius:16px;background:#fbfcfd;display:grid;place-items:center;position:relative}
.body .arrow{font-size:26px;color:#94a3b8}
.body .tl{position:absolute;top:8px;left:50%;transform:translateX(-50%);font-size:11px;color:var(--muted)}
.vec{display:flex;gap:18px;justify-content:center;margin-top:8px;font-variant-numeric:tabular-nums}
.vec div{text-align:center}.vec span{display:block;color:var(--muted);font-size:11px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef1f4;font-variant-numeric:tabular-nums;white-space:nowrap}
th{color:var(--muted);font-weight:600;position:sticky;top:0;background:#fff}
.scroll{max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
label{display:grid;gap:4px;font-size:12px;color:var(--muted)}
input[type=number]{width:92px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit;color:var(--text)}
.reason{font-size:13px;color:var(--muted)}
.reason b{color:var(--text)}
.warnbox{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;border-radius:10px;padding:10px 12px;font-size:12.5px}
.armed{background:#f0fdf4;border-color:#bbf7d0}
.tgrid{display:grid;grid-template-columns:300px 1fr;gap:16px}
#tlist{display:grid;gap:6px;align-content:start}
.trow{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--line);border-radius:9px;cursor:pointer;background:#fff}
.trow:hover{background:#f8fafc}
.trow.sel{border-color:var(--blue);box-shadow:0 0 0 2px #dbeafe}
.trow .nm{flex:1;font-size:13px}
.trow .st{font-size:11px;color:var(--muted)}
.trow.pass .st{color:var(--green)}.trow.fail .st{color:var(--red)}.trow.run .st{color:var(--blue)}
.ttitle{font-weight:650;font-size:15px;margin-bottom:6px}
.tsteps{margin:0 0 10px 18px;padding:0;color:var(--muted);font-size:12.5px}
.tsteps li{margin:3px 0}
.tchecks{display:grid;gap:6px;margin:8px 0}
.tcheck{display:flex;gap:8px;align-items:baseline;font-size:12.5px}
.tcheck .ic{width:16px;text-align:center;font-weight:700}
.tcheck.pass .ic{color:var(--green)}.tcheck.fail .ic{color:var(--red)}.tcheck.pending .ic{color:#94a3b8}
.tcheck .dt{color:var(--muted);font-variant-numeric:tabular-nums}
.tbtns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.tnote{display:flex;gap:8px;margin-top:10px}
.tnote input{padding:8px 10px;border:1px solid var(--line);border-radius:8px;font:inherit;color:var(--text)}
.tnote input#tnote{flex:1}
.tsummary{margin-top:10px;font-size:12px;color:var(--muted)}
.pgrid{display:grid;grid-template-columns:1fr 210px;gap:16px;align-items:start}
#pathcv{width:100%;height:auto;background:#fbfcfd;border:1px solid var(--line);border-radius:10px}
.pside{display:grid;gap:10px}
.pstat{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid #eef1f4;padding-bottom:4px}
.pstat span{color:var(--muted);font-size:12px}
.pstat b{font-variant-numeric:tabular-nums;font-size:16px}
@media(max-width:900px){.pgrid{grid-template-columns:1fr}}
</style></head>
<body>
<header>
  <h1>LanderPi 底盘试验台</h1>
  <span class="pill"><i id="d-js" class="dot"></i>手柄 <b id="js-gen">-</b></span>
  <span class="pill"><i id="d-hid" class="dot"></i>接收器 <b id="hid-rate">-</b></span>
  <span class="pill"><i id="d-owner" class="dot"></i>板卡 owner <b id="owner">-</b></span>
  <span class="pill"><i id="d-hb" class="dot"></i>页面心跳 <b id="hb">-</b></span>
  <span class="pill"><i id="d-out" class="dot"></i>输出 <b id="out-state">锁定</b></span>
  <span class="spacer"></span>
  <button class="danger" id="stop">急停 · 发零</button>
</header>
<main>
  <div class="card" id="tests">
    <h2>功能测试 <span class="sub" id="tprog">未开始</span>
      <span style="float:right;display:flex;gap:8px">
        <button onclick="window.open('/api/test/report','_blank')" style="padding:5px 10px">导出报告</button>
        <button id="treset" style="padding:5px 10px">清空结果</button>
      </span></h2>
    <div class="tgrid">
      <div id="tlist"></div>
      <div id="tpanel">
        <div class="ttitle" id="ttitle">选择左侧一项测试</div>
        <ol class="tsteps" id="tsteps"></ol>
        <div class="tchecks" id="tchecks"></div>
        <div class="tnote">
          <input id="tnote" placeholder="现场备注（可选，例如“右后轮先动”）">
          <input id="tmeasured" type="number" step="0.01" min="0" placeholder="实测距离 m" style="width:130px;display:none">
        </div>
        <div class="tbtns" id="tbtns"></div>
        <div class="tsummary" id="tsummary"></div>
      </div>
    </div>
  </div>

  <div class="grid3" id="axes"></div>

  <div class="grid2">
    <div class="card">
      <h2>车体与四轮目标</h2>
      <div class="chassis">
        <div class="body"><span class="tl">车头</span><span class="arrow">↑</span></div>
      </div>
      <div class="vec">
        <div><span>vx m/s</span><b id="vx">0</b></div>
        <div><span>vy m/s</span><b id="vy">0</b></div>
        <div><span>wz rad/s</span><b id="wz">0</b></div>
      </div>
      <div class="wheels" id="wheels"></div>
      <div class="sub" style="margin-top:10px">轮序 1 左前 · 2 左后 · 3 右前 · 4 右后（厂商 ROS motor id）。目标由厂商麦轮公式计算，实际轮速未测。</div>
    </div>

    <div class="card" id="panel">
      <h2>授权与限幅</h2>
      <div class="reason" id="reason">-</div>
      <div class="controls" style="margin-top:12px">
        <button class="primary" id="arm">解锁输出</button>
        <button id="center">把当前值设为中心</button>
        <button id="zero-pose">清零估计位姿</button>
      </div>
      <div class="controls" style="margin-top:12px">
        <label style="display:flex;flex-direction:row;align-items:center;gap:8px;color:var(--text)">
          <input id="guard" type="checkbox" style="width:16px;height:16px"> 页面守护（断页 8 s 内停车）
        </label>
      </div>
      <div class="controls" style="margin-top:10px">
        <label>vx 上限<input id="lim-vx" type="number" step="0.01" min="0" max="0.2"></label>
        <label>vy 上限<input id="lim-vy" type="number" step="0.01" min="0" max="0.2"></label>
        <label>wz 上限<input id="lim-wz" type="number" step="0.01" min="0" max="0.5"></label>
        <label>死区<input id="dz" type="number" step="0.01" min="0" max="0.5"></label>
      </div>
      <div class="controls" style="margin-top:8px">
        <label>vx 加速度<input id="acc-vx" type="number" step="0.1" min="0.05" max="3"></label>
        <label>vy 加速度<input id="acc-vy" type="number" step="0.1" min="0.05" max="3"></label>
        <label>wz 加速度<input id="acc-wz" type="number" step="0.1" min="0.1" max="6"></label>
      </div>
      <div class="warnbox" style="margin-top:14px" id="note">
        解锁顺序：摇杆回中 250 ms → 点“解锁输出”。松杆即回零；接收器报告中断 0.35 s、板卡 owner 消失会立刻发零。勾上“页面守护”后，页面断开 8 s 也会停车——面板被挂起时会误触发，默认关闭。
      </div>
      <div class="sub" style="margin-top:12px">命令积分位姿（command_integrated_estimate）：<span id="pose">0.00, 0.00, 0.00</span></div>
      <div class="sub">日志：<span id="trace">-</span></div>
    </div>
  </div>

  <div class="card">
    <h2>路径积分 <span class="sub">command_integrated_estimate · 由已派发速度积分，不是实测轨迹</span></h2>
    <div class="pgrid">
      <canvas id="pathcv" width="760" height="380"></canvas>
      <div class="pside">
        <div class="pstat"><span>X 前向 m</span><b id="px">0.00</b></div>
        <div class="pstat"><span>Y 左向 m</span><b id="py">0.00</b></div>
        <div class="pstat"><span>朝向 deg</span><b id="pyaw">0.0</b></div>
        <div class="pstat"><span>路径长度 m</span><b id="plen">0.00</b></div>
        <button id="preset">清零路径与位姿</button>
        <div class="sub">每 100 ms 采一个点；与卷尺距离的比对用“距离”测试项。</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>事件</h2>
    <div class="scroll"><table><thead><tr><th>t</th><th>类型</th><th>轴/目标</th><th>四轮 rps</th><th>说明</th></tr></thead><tbody id="log"></tbody></table></div>
  </div>
</main>
<script>
const $=id=>document.getElementById(id);
const AX=[{n:'vx',i:1,t:'vx'},{n:'vy',i:0,t:'vy'},{n:'wz',i:2,t:'wz'}];
let cfgReady=false, lastState=null;

function buildAxes(){
  $('axes').innerHTML=AX.map(a=>`
   <div class="card axis">
     <div class="top"><span class="name">${a.n.toUpperCase()} <span class="sub">轴 ${a.i}</span></span>
       <span class="raw" id="raw-${a.n}">-</span></div>
     <div class="bar"><u id="ctr-${a.n}"></u><i id="bar-${a.n}"></i></div>
     <div class="sub"><span id="norm-${a.n}">0.00</span> · HID <span id="hid-${a.n}">-</span> · 范围 <span id="range-${a.n}">-</span></div>
   </div>`).join('');
  $('wheels').innerHTML=[1,2,3,4].map(i=>`<div class="wheel"><span>M${i}</span><b id="w${i}">0.00</b></div>`).join('');
}

function reasonText(s){
  const o=s.out, m={
    read_only_mode:'只读模式：本页不发任何电机目标。',
    gamepad_disconnected:'手柄设备不可见，已发零。',
    receiver_reports_stopped:'接收器 USB 报告停止，已发零。',
    board_owner_missing:'未发现 ros_robot_controller 板卡 owner，输出锁定。',
    page_heartbeat_lost:'页面守护触发（断页/心跳超时），已发零。',
    axis_calibration_invalid:'轴值超出标定范围，已发零。',
    center_sticks_to_arm:'摇杆不在中位，先回中。',
    centering:'中位确认中…',
    ready_to_arm:'已在中位，可以解锁。',
    stick_deflected:'输出中：摇杆偏转。',
    armed_neutral:'输出中：摇杆在中位（发零）。',
    waiting_operator:'等待操作。',
    heartbeat_wait:'等待页面心跳。'
  };
  return m[o.reason]||o.reason;
}

function render(s){
  lastState=s;
  const js=s.js, out=s.out;
  $('d-js').className='dot '+(js.connected?'ok':'bad'); $('js-gen').textContent=js.generation||'-';
  $('d-hid').className='dot '+(js.hid_rate>60?'ok':(js.hid_rate>0?'warn':'bad')); $('hid-rate').textContent=(js.hid_rate||0).toFixed(0)+' Hz';
  $('d-owner').className='dot '+(out.board_owner?'ok':'bad'); $('owner').textContent=out.board_owner?'在线':'未发现';
  $('d-out').className='dot '+(out.armed?'ok':'warn'); $('out-state').textContent=out.armed?'已解锁':'锁定';
  const hb=out.heartbeat_age_s; $('d-hb').className='dot '+(hb===null?'bad':(hb<1.5?'ok':'warn'));
  $('hb').textContent=hb===null?'无':hb.toFixed(1)+'s';
  $('panel').className='card'+(out.armed?' armed':'');
  $('reason').innerHTML='<b>'+reasonText(s)+'</b>'+(js.error?' · '+js.error:'');
  $('arm').textContent=out.armed?'撤销解锁':'解锁输出';
  $('arm').disabled=!out.armed && !(out.reason==='ready_to_arm'||out.reason==='centering');

  AX.forEach(a=>{
    const raw=js.axes_raw[a.i];
    $('raw-'+a.n).textContent=raw===null?'-':raw;
    const v=(s.intent[a.n]||0);
    $('norm-'+a.n).textContent=v.toFixed(2);
    const bar=$('bar-'+a.n), half=Math.min(50,Math.abs(v)*50);
    bar.style.width=half+'%'; bar.style.left=(v>=0?50:50-half)+'%';
    bar.style.background=v>0?'#2563eb':(v<0?'#7c3aed':'#cbd5e1');
    const c=s.cfg.center[a.n], h=(c+1)/2*100;
    $('ctr-'+a.n).style.left=Math.min(99,Math.max(1,h))+'%';
    $('hid-'+a.n).textContent=js.hid_axis[a.i]??'-';
    const r=s.range[a.n];
    $('range-'+a.n).textContent=(r[0]===null?'-':r[0].toFixed(2))+'…'+(r[1]===null?'-':r[1].toFixed(2));
  });

  $('vx').textContent=out.target[0].toFixed(3);
  $('vy').textContent=out.target[1].toFixed(3);
  $('wz').textContent=out.target[2].toFixed(3);
  out.wheel_rps.forEach((v,i)=>{$('w'+(i+1)).textContent=v.toFixed(2)});
  $('pose').textContent=out.pose_command_integrated_estimate.map(v=>v.toFixed(2)).join(', ');
  $('trace').textContent=out.trace.split('/').pop();

  if(!cfgReady){cfgReady=true;
    $('lim-vx').value=s.cfg.limits.vx; $('lim-vy').value=s.cfg.limits.vy; $('lim-wz').value=s.cfg.limits.wz; $('dz').value=s.cfg.deadzone;
    $('acc-vx').value=s.cfg.accel.vx; $('acc-vy').value=s.cfg.accel.vy; $('acc-wz').value=s.cfg.accel.wz;
    $('guard').checked=s.cfg.require_page!==false;}

  if(s.tests) renderTests(s.tests);

  $('log').innerHTML=s.log.map(r=>{
    const ax=r.target?('['+r.target.map(v=>v.toFixed(2)).join(', ')+']'):(r.index!==undefined?('轴'+r.index+'='+r.value):'');
    const wh=r.wheel_rps?('['+r.wheel_rps.map(v=>v.toFixed(1)).join(', ')+']'):'';
    const note=r.reason||r.target?'':'';
    return `<tr><td>${(r.mono_ns/1e9%1000).toFixed(2)}</td><td>${r.kind}</td><td>${ax}</td><td>${wh}</td><td>${r.reason||''}</td></tr>`;
  }).join('');
}

async function poll(){
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    render(await r.json());
    await fetch('/api/heartbeat',{method:'POST'});
  }catch(e){ $('d-js').className='dot bad'; }
  setTimeout(poll,120);
}

async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body||{})});
  if(!r.ok){ let t=''; try{ t=(await r.json()).error||''; }catch(e){} alert('操作被拒绝：'+(t||r.status)); }
  return r;
}

$('arm').onclick=async()=>{
  const s=lastState; if(!s)return;
  await post('/api/arm',{armed:!s.out.armed});
};
$('stop').onclick=()=>post('/api/stop');
$('guard').onchange=e=>post('/api/config',{require_page:e.target.checked});
$('center').onclick=()=>post('/api/center');
$('zero-pose').onclick=()=>post('/api/pose');
['vx','vy','wz','dz'].forEach(k=>{
  $(k==='dz'?'dz':'lim-'+k).onchange=async e=>{
    const body={};
    if(k==='dz'){body.deadzone=parseFloat(e.target.value);}
    else{body.limits={};body.limits[k]=parseFloat(e.target.value);}
    await post('/api/config',body); cfgReady=false;
  };
});
['vx','vy','wz'].forEach(k=>{
  $('acc-'+k).onchange=async e=>{
    const body={accel:{}}; body.accel[k]=parseFloat(e.target.value);
    await post('/api/config',body); cfgReady=false;
  };
});

// Presence is a held connection, not a timer: hidden tabs keep SSE alive, and
// closing the page drops TCP so the server zeroes the motors.
const presence=new EventSource('/api/presence');
presence.onerror=()=>{};

// ---------------------------------------------------------------- tests
let testSel=null, testBuilt=false, btnSig='', chkSig='', stepSig='';
const STLABEL={pass:'通过',fail:'不通过',pass_with_notes:'通过(告警)',aborted:'中止'};

function statusOf(t,id){ const r=t.results.find(x=>x.id===id); if(r) return r.status;
  if(t.active.state==='running'&&t.active.id===id) return 'run'; return 'idle'; }

function buildTestList(t){
  const box=$('tlist'); box.innerHTML='';
  t.plan.forEach((p,i)=>{
    const row=document.createElement('div');
    row.className='trow'; row.dataset.id=p.id;
    row.innerHTML=`<i class="dot"></i><span class="nm">${i+1}. ${p.title}</span><span class="st">未测</span>`;
    row.onclick=()=>{testSel=p.id; renderTests(lastState.tests); };
    box.appendChild(row);
  });
  testBuilt=true;
}

function renderTests(t){
  if(!testBuilt) buildTestList(t);
  $('tlist').querySelectorAll('.trow').forEach(row=>{
    const id=row.dataset.id, st=statusOf(t,id), dot=row.querySelector('.dot'), lbl=row.querySelector('.st');
    row.classList.toggle('sel', testSel===id);
    row.classList.toggle('run', st==='run');
    if(st==='run'){dot.className='dot warn'; lbl.textContent='进行中';}
    else if(st==='idle'){dot.className='dot'; lbl.textContent='未测';}
    else if(st==='pass'){dot.className='dot ok'; lbl.textContent='通过';}
    else if(st==='pass_with_notes'){dot.className='dot warn'; lbl.textContent='告警';}
    else {dot.className='dot bad'; lbl.textContent=STLABEL[st]||st;}
    row.classList.toggle('pass', st==='pass'||st==='pass_with_notes');
    row.classList.toggle('fail', st==='fail'||st==='aborted');
  });
  const p=t.progress;
  $('tprog').textContent=`通过 ${p.pass} · 失败 ${p.fail} · 共 ${p.total}`;

  const plan=t.plan.find(x=>x.id===testSel);
  const active=t.active;
  const isActive=active.state==='running'&&active.id===testSel;
  if(!plan){
    $('ttitle').textContent='选择左侧一项测试';
    $('tsteps').innerHTML=''; $('tchecks').innerHTML=''; $('tbtns').innerHTML=''; $('tsummary').textContent='';
    $('tmeasured').style.display='none';
    return;
  }
  $('ttitle').textContent=plan.title;
  if(stepSig!==plan.id){stepSig=plan.id;
    $('tsteps').innerHTML=plan.steps.map(x=>`<li>${x}</li>`).join('')+
      `<li style="color:var(--text)">现场确认：${plan.operator}</li>`;}
  $('tmeasured').style.display=(testSel==='distance'&&isActive)?'':'none';
  $('tsummary').textContent=isActive?('进行中 · 已采样 '+(active.checks.length||0)+' 项判定'):
    (()=>{const r=t.results.find(x=>x.id===testSel); if(!r) return '';
      const m=Object.entries(r.metrics||{}).map(([k,v])=>`${k}=${Array.isArray(v)?'['+v.map(x=>x.toFixed(2)).join(', ')+']':v}`).join(' · ');
      return `结果：${STLABEL[r.status]||r.status}${r.measured_m?` · 实测 ${r.measured_m} m`:''}${r.note?' · '+r.note:''}${m?' · '+m:''}`;})();

  const checks=isActive?active.checks:(t.results.find(x=>x.id===testSel)?.checks||[]);
  const csig=JSON.stringify(checks);
  if(csig!==chkSig){chkSig=csig;
    $('tchecks').innerHTML=checks.length?checks.map(c=>{
      const ic=c.state==='pass'?'✓':(c.state==='fail'?'✕':'·');
      return `<div class="tcheck ${c.state}"><span class="ic">${ic}</span><span>${c.label}</span><span class="dt">${c.detail||''}</span></div>`;
    }).join(''):'';}

  const armed=lastState.out.armed;
  let btns='';
  if(isActive){
    btns=`<button class="primary" id="tpass">通过</button>
          <button id="tfail">不通过</button>
          <button id="tabort">中止</button>`;
  }else{
    btns=`<button class="primary" id="tstart" ${armed?'':'disabled'}>开始测试</button>`;
    if(!armed) btns+=`<span class="sub" style="align-self:center">先在右侧解锁输出</span>`;
  }
  const bsig=isActive+'|'+armed+'|'+testSel;
  if(bsig!==btnSig){btnSig=bsig;
    $('tbtns').innerHTML=btns;
    if($('tstart')) $('tstart').onclick=()=>post('/api/test/start',{id:testSel});
    if($('tpass')) $('tpass').onclick=()=>post('/api/test/verdict',{pass:true,note:$('tnote').value,measured_m:$('tmeasured').value});
    if($('tfail')) $('tfail').onclick=()=>post('/api/test/verdict',{pass:false,note:$('tnote').value,measured_m:$('tmeasured').value});
    if($('tabort')) $('tabort').onclick=()=>post('/api/test/abort',{});}
}

$('treset').onclick=()=>{ if(confirm('清空所有测试结果？')) post('/api/test/reset',{}); };

// ---------------------------------------------------------------- path
const cv=$('pathcv'), ctx=cv.getContext('2d');
function drawPath(d){
  const W=cv.width, H=cv.height, pad=34;
  ctx.clearRect(0,0,W,H);
  const pts=d.points||[], pose=d.pose||[0,0,0];
  let xs=pts.map(p=>p[0]).concat([pose[0]]), ys=pts.map(p=>p[1]).concat([pose[1]]);
  let x0=Math.min(...xs), x1=Math.max(...xs), y0=Math.min(...ys), y1=Math.max(...ys);
  const span=Math.max(x1-x0, y1-y0, 1.5), cxw=(x0+x1)/2, cyw=(y0+y1)/2;
  x0=cxw-span/2; x1=cxw+span/2; y0=cyw-span/2; y1=cyw+span/2;
  const s=Math.min((W-2*pad)/(y1-y0), (H-2*pad)/(x1-x0));
  const sx=y=>W/2-(y-cyw)*s, sy=x=>H/2-(x-cxw)*s;
  // grid 0.5 m
  ctx.strokeStyle='#eef1f4'; ctx.fillStyle='#9ca3af'; ctx.font='10px sans-serif'; ctx.lineWidth=1;
  for(let g=Math.ceil(y0*2)/2; g<=y1; g+=0.5){
    const X=sx(g); ctx.beginPath(); ctx.moveTo(X,0); ctx.lineTo(X,H); ctx.stroke();
  }
  for(let g=Math.ceil(x0*2)/2; g<=x1; g+=0.5){
    const Y=sy(g); ctx.beginPath(); ctx.moveTo(0,Y); ctx.lineTo(W,Y); ctx.stroke();
    if(Math.abs(g*2-Math.round(g*2))<1e-6 && Math.abs(g-Math.round(g))<1e-6){
      ctx.fillText(g.toFixed(0)+' m',4,Y-3);
    }
  }
  // axes hint
  ctx.fillStyle='#6b7280'; ctx.font='11px sans-serif';
  ctx.fillText('↑ X 前向', W/2-28, 14); ctx.fillText('← Y 左向', 8, H/2-8);
  if(pts.length>1){
    ctx.strokeStyle='#2563eb'; ctx.lineWidth=2; ctx.beginPath();
    pts.forEach((p,i)=>{ const X=sx(p[1]), Y=sy(p[0]); i?ctx.lineTo(X,Y):ctx.moveTo(X,Y); });
    ctx.stroke();
    // start
    const st=pts[0];
    ctx.fillStyle='#15803d'; ctx.beginPath(); ctx.arc(sx(st[1]),sy(st[0]),4,0,7); ctx.fill();
  }
  // current + heading
  const X=sx(pose[1]), Y=sy(pose[0]);
  ctx.strokeStyle='#dc2626'; ctx.lineWidth=2; ctx.beginPath();
  ctx.moveTo(X,Y); ctx.lineTo(X-Math.sin(pose[2])*26, Y-Math.cos(pose[2])*26); ctx.stroke();
  ctx.fillStyle='#dc2626'; ctx.beginPath(); ctx.arc(X,Y,5,0,7); ctx.fill();
  if(!pts.length){ ctx.fillStyle='#9ca3af'; ctx.font='13px sans-serif'; ctx.fillText('还没有位移', W/2-38, H/2+4); }
  $('px').textContent=pose[0].toFixed(2);
  $('py').textContent=pose[1].toFixed(2);
  $('pyaw').textContent=(d.pose_deg??0).toFixed(1);
  $('plen').textContent=(d.length_m??0).toFixed(2);
}
async function pollPath(){
  try{ const r=await fetch('/api/path',{cache:'no-store'}); drawPath(await r.json()); }catch(e){}
  setTimeout(pollPath,300);
}
$('preset').onclick=async()=>{ await post('/api/pose',{}); pollPath(); };
pollPath();

buildAxes(); poll();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    trial: Trial = None

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, json.dumps(self.trial.status()))
        elif self.path == "/api/path":
            with self.trial.lock:
                points = list(self.trial.path)
            if len(points) > 1500:
                step = len(points) // 1500 + 1
                points = points[::step] + [points[-1]]
            self._send(200, json.dumps({
                "source": "command_integrated_estimate",
                "pose": [round(v, 4) for v in self.trial.pose],
                "pose_deg": round(math.degrees(self.trial.pose[2]), 1),
                "points": points, "length_m": round(self.trial.path_length, 3),
                "measured": False}))
        elif self.path == "/api/test/report":
            self.trial.record("report_export")
            self._send(200, json.dumps(self.trial.report(), indent=2), "application/json")
        elif self.path == "/api/presence":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.trial.presence_clients += 1
            self.trial.record("presence_open", clients=self.trial.presence_clients)
            try:
                while True:
                    self.trial.presence_ns = time.monotonic_ns()
                    self.wfile.write(b": presence\n\n")
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.trial.presence_clients -= 1
                if self.trial.presence_clients <= 0:
                    self.trial.presence_clients = 0
                    self.trial.presence_ns = None
                self.trial.record("presence_close", clients=self.trial.presence_clients)
            return
        else:
            self._send(404, json.dumps({"error": "not_found"}))

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        trial = self.trial
        if self.path == "/api/heartbeat":
            trial.last_heartbeat_ns = time.monotonic_ns()
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/api/arm":
            if payload.get("armed") is True:
                if trial.reason not in ("ready_to_arm", "centering"):
                    self._send(409, json.dumps({"error": trial.reason}))
                    return
                trial.armed = True
                trial.last_heartbeat_ns = time.monotonic_ns()
                trial.record("arm", limits=trial.cfg["limits"], center=trial.cfg["center"])
            else:
                trial.armed = False
                trial.reason = "waiting_operator"
                trial.record("disarm", reason="operator")
            self._send(200, json.dumps({"armed": trial.armed}))
        elif self.path == "/api/stop":
            if trial.test["state"] == "running":
                trial.test["estop_ns"] = time.monotonic_ns()
            trial.armed = False
            trial.reason = "operator_stop"
            trial.record("stop", target=list(trial.published))
            self._send(200, json.dumps({"armed": False}))
        elif self.path == "/api/test/start":
            error = trial.start_test(str(payload.get("id") or ""))
            self._send(409 if error else 200, json.dumps({"error": error} if error else {"started": True}))
        elif self.path == "/api/test/abort":
            error = trial.abort_test(str(payload.get("reason") or "operator"))
            self._send(409 if error else 200, json.dumps({"error": error} if error else {"aborted": True}))
        elif self.path == "/api/test/reset":
            trial.results = {}
            trial.test = {**trial.test, "state": "idle", "id": None, "checks": [], "metrics": {}}
            try:
                os.remove(trial.results_path)
            except OSError:
                pass
            trial.record("test_reset")
            self._send(200, json.dumps({"reset": True}))
        elif self.path == "/api/test/verdict":
            measured = payload.get("measured_m")
            error = trial.verdict(payload.get("pass") is True, str(payload.get("note") or ""),
                                  None if measured in (None, "") else float(measured))
            self._send(409 if error else 200, json.dumps({"error": error} if error else {"ok": True}))
        elif self.path == "/api/center":
            axes = trial.axes_raw
            if not trial.connected or any(axis["index"] >= len(axes) or axes[axis["index"]] is None
                                          for axis in AXES):
                self._send(409, json.dumps({"error": "gamepad_input_unavailable"}))
                return
            for axis in AXES:
                trial.cfg["center"][axis["name"]] = round(axes[axis["index"]] / 32767, 5)
            trial.neutral_since_ns = None
            trial._save_cfg()
            trial.record("center_captured", center=dict(trial.cfg["center"]))
            self._send(200, json.dumps(trial.cfg["center"]))
        elif self.path == "/api/config":
            if "limits" in payload:
                for name, value in payload["limits"].items():
                    trial.cfg["limits"][name] = max(0.0, float(value))
            if "deadzone" in payload:
                trial.cfg["deadzone"] = min(0.5, max(0.0, float(payload["deadzone"])))
            if "accel" in payload:
                for name, value in payload["accel"].items():
                    trial.cfg["accel"][name] = max(0.0, float(value))
            if "require_page" in payload:
                trial.cfg["require_page"] = bool(payload["require_page"])
            trial._save_cfg()
            trial.record("config", cfg=trial.cfg)
            self._send(200, json.dumps(trial.cfg))
        elif self.path == "/api/pose":
            trial.pose = [0.0, 0.0, 0.0]
            trial.pose_ns = None
            trial.path.clear()
            trial.path_length = 0.0
            self._send(200, json.dumps({"pose": trial.pose}))
        else:
            self._send(404, json.dumps({"error": "not_found"}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="platform-xhci-hcd.1-usb-0:2:1.0-joystick")
    parser.add_argument("--http-port", type=int, default=8790)
    parser.add_argument("--publish", action="store_true", help="publish set_motor; needs the vendor board owner")
    parser.add_argument("--domain", type=int, default=None)
    parser.add_argument("--trace-dir", default="/tmp/base_trial")
    args = parser.parse_args()

    trial = Trial(args.port, args.publish, args.domain, args.trace_dir)
    with open(os.path.join(args.trace_dir, "server.pid"), "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    Handler.trial = trial
    threading.Thread(target=trial.read_loop, name="js-reader", daemon=True).start()

    node = publisher = None
    if args.publish:
        if args.domain is not None:
            os.environ["ROS_DOMAIN_ID"] = str(args.domain)
        import rclpy
        from ros_robot_controller_msgs.msg import MotorsState
        from sensor_msgs.msg import Joy
        rclpy.init()
        node = rclpy.create_node("lelab_base_trial")
        publisher = node.create_publisher(MotorsState, "ros_robot_controller/set_motor", 10)

        def on_joy(message):
            trial.board_joy = {"axes": [round(v, 2) for v in message.axes],
                               "buttons": list(message.buttons),
                               "mono_ns": time.monotonic_ns()}
        node.create_subscription(Joy, "ros_robot_controller/joy", on_joy, 1)
        threading.Thread(target=rclpy.spin, args=(node,), name="ros-spin", daemon=True).start()

    threading.Thread(target=trial.control_loop, args=(node, publisher), name="control", daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.http_port), Handler)

    def shutdown(*_):
        # Never call server.shutdown() from this thread: it waits for serve_forever
        # and deadlocks the whole process on SIGTERM.
        trial.armed = False
        try:
            if publisher is not None:
                from ros_robot_controller_msgs.msg import MotorState, MotorsState
                message = MotorsState()
                message.data = [MotorState(id=motor_id, rps=0.0) for motor_id in MOTOR_IDS]
                publisher.publish(message)
                trial.record("exit_zero")
        finally:
            trial.close()
            os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({"started": True, "http": f"http://0.0.0.0:{args.http_port}",
                      "publish": args.publish, "trace": trial.trace_path}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        trial.armed = False
        trial.close()


if __name__ == "__main__":
    main()
