# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import math
import os
import threading
import time
from typing import Any, Callable

from pydantic import BaseModel

from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig

from .desktop_follow import capture as capture_alignment, follow_target
from .safety import follower_safety_kwargs
from .utils.config import setup_calibration_files
from .utils.devices import safe_disconnect_device

logger = logging.getLogger(__name__)

# Global variables for teleoperation state
# 遥操作会话的全局状态（HTTP 线程与工作线程共享）：
# - teleoperation_active：既是"会话进行中"标志，也是工作线程的循环条件/停止标志；
# - teleoperation_thread：后台工作线程引用，停止时对其 join；
# - current_robot / current_teleop：已连接的从臂、主臂实例，供状态与关节查询接口使用。
teleoperation_active = False
teleoperation_thread: threading.Thread | None = None
current_robot = None
current_teleop = None
# What the live session is actually driving: the two ports, the two calibration files and
# the binding evidence the identity check used. The page must show this and not what its
# pickers happen to say, otherwise a stale selection reads like the active session.
# Cleared with the session (worker teardown), never left pointing at a finished run.
teleoperation_session: dict[str, Any] | None = None
# How many consecutive unreadable follower frames are tolerated before the session stops.
# Read per session so an operator can retune it without a code change, like the other
# motion guards in safety.py. 20 FPS broadcast × 10 ≈ half a second of missing feedback.
DEFAULT_MAX_FOLLOWER_READ_FAILURES = 10


def max_follower_read_failures() -> int:
    raw = os.getenv("LELAB_MAX_FOLLOWER_READ_FAILURES", str(DEFAULT_MAX_FOLLOWER_READ_FAILURES))
    try:
        value = int(raw)
    except ValueError:
        logger.warning("LELAB_MAX_FOLLOWER_READ_FAILURES=%r is not an int; using %d",
                       raw, DEFAULT_MAX_FOLLOWER_READ_FAILURES)
        return DEFAULT_MAX_FOLLOWER_READ_FAILURES
    return max(1, value)


# Why the last session ended on its own, for the status endpoint and the page.
stop_reason: str | None = None

# The follow loop is gated by an explicit clutch, mirroring the mobile state machine: a
# session starts disengaged and sends nothing until the operator asks to follow, and that
# request captures an alignment from the leader reading and the *measured* follower reading
# of one iteration. Releasing clears the reference, so re-engaging re-aligns instead of
# replaying whatever pose the leader happens to be in now.
_clutch_lock = threading.Lock()
clutch: dict[str, Any] = {"engage_requested": False, "engaged": False,
                          "alignment": None, "refusal": None}
# Guards the start path; the worker owns disconnect so stop() does not race.
# _state_lock：互斥保护"启动路径"，防止并发请求同时通过 active 检查；设备断开由工作线程独占负责。
_state_lock = threading.Lock()


class TeleoperateRequest(BaseModel):
    """遥操作启动请求（前端遥操作页面 POST /start-teleoperation 的请求体）。

    中文说明：
    - leader_port：主臂（leader，人工拖动的示教臂）串口；follower_port：
      从臂（follower，被跟随执行的机械臂）串口。
    - leader_config / follower_config：两臂的标定配置名，后端据此定位/生成
      标定文件（见 utils.config.setup_calibration_files）。
    - preview：只映射不执行。工作线程照常读主臂、读从臂并广播，但**跳过
      send_action，也不给从臂 configure（力矩保持关闭）**。页面用它在接合
      之前看清"目标会是什么、和实测差多少"，而机械臂不动。
    """

    leader_port: str
    follower_port: str
    leader_config: str
    follower_config: str
    preview: bool = False
    # False means the session comes up with the clutch out: both arms are read and streamed,
    # and nothing is written until the operator asks to follow. A caller that means following
    # from the first measured frame says so here rather than getting it by surprise.
    engage: bool = False


class TeleopClutchRequest(BaseModel):
    """离合接合请求：engage=True 请求跟随（由环路在下一个实测帧建立参考），
    engage=False 立刻停止下发并清掉参考。"""

    engage: bool


# lerobot drives the SO-101 with ``use_degrees=True``, so every ``<motor>.pos``
# reading — leader or follower — is an angle relative to that arm's calibration
# center, which is also the URDF zero pose.
MOTOR_TO_URDF = {
    "shoulder_pan": "Rotation",
    "shoulder_lift": "Pitch",
    "elbow_flex": "Elbow",
    "wrist_flex": "Wrist_Pitch",
    "wrist_roll": "Wrist_Roll",
    "gripper": "Jaw",
}


def joints_from_motor_reading(reading: dict[str, float]) -> tuple[dict[str, float], list[str], list[str]]:
    """Convert an ``<motor>.pos`` reading (both arms use this shape) to URDF radians.

    The gripper reports 0–100 rather than degrees, but that range lands inside the
    Jaw limit, so both arms stay directly comparable after the same conversion.
    A motor that is not in the reading is reported as missing and **not** filled
    with 0.0: a flat zero pose on the chart or the 3D view would otherwise be read
    as a measured position, which is exactly what requirement 三 forbids.
    Returns (joints, missing_motor_names, debug_rows).
    """
    joints: dict[str, float] = {}
    missing: list[str] = []
    rows: list[str] = []
    for motor_name, urdf_joint in MOTOR_TO_URDF.items():
        value = reading.get(f"{motor_name}.pos")
        if value is None:
            missing.append(motor_name)
            continue
        joints[urdf_joint] = float(value) * math.pi / 180.0
        rows.append(f"{motor_name:14s} {float(value):+8.2f}° → {urdf_joint:11s}")
    return joints, missing, rows


def read_joint_side(read: Callable[[], tuple[dict[str, float], list[str], list[str]]],
                    label: str) -> dict[str, Any]:
    """One side's joints plus the health of that reading, for the stream and the REST poll.

    ``status`` is "measured" only when all six joints came back; "read_failed" when the
    bus raised; "partial" when some motors were missing. In the latter two the ``joints``
    field is None so no consumer can draw a pose that was never measured, and the caller
    still gets the host time the attempt was made.
    """
    attempted_at = time.time()
    try:
        joints, missing, rows = read()
    except Exception as exc:
        logger.error(f"{label} joint read failed: {exc}")
        return {"status": "read_failed", "joints": None, "missing": [], "error": str(exc),
                "attempted_at": attempted_at}
    if missing:
        logger.warning(f"{label} joint reading is missing {missing}")
        return {"status": "partial", "joints": None, "missing": missing, "error": None,
                "attempted_at": attempted_at}

    # Throttled debug print (~once per second at 20 Hz broadcast).
    if attempted_at - getattr(read_joint_side, "_last_log", {}).get(label, 0) > 1.0:
        stamps = getattr(read_joint_side, "_last_log", {})
        stamps[label] = attempted_at
        read_joint_side._last_log = stamps
        logger.info(f"[joint-debug:{label}]\n  " + "\n  ".join(rows))

    return {"status": "measured", "joints": joints, "missing": [], "error": None,
            "attempted_at": attempted_at}


def get_joint_positions_from_robot(robot) -> dict[str, float] | None:
    """
    Extract current joint positions from the robot and convert to URDF joint format.

    Args:
        robot: The robot instance (SO101Follower)

    Returns:
        URDF 关节名→弧度的字典；读取失败或关节残缺时返回 None（绝不返回全零字典）

    中文说明：
    - 数据来源：robot.get_observation() 返回的字典中，每个电机对应一个
      "<电机名>.pos" 键（度数，相对标定零位）。
    - 转换与健康状态都由 read_joint_side 统一处理（与主臂共用同一条映射）；
      缺键或异常都不再补 0.0，避免把"读不到"画成"臂在零位"。
    """
    return read_joint_side(lambda: joints_from_motor_reading(robot.get_observation()),
                           "follower")["joints"]


def _safe_disconnect(device) -> None:
    """Disconnect a robot/teleop device, swallowing (but logging) any error.

    Used on the connection-failure cleanup path so one device's failure can't
    leave the other holding its serial port open.

    中文说明：连接失败路径上的兜底清理——内部转调 safe_disconnect_device，
    吞掉并记录断开过程中的异常，避免一台设备断开失败导致另一台的串口被占死。
    """
    safe_disconnect_device(device, logger)


def reset_clutch() -> None:
    with _clutch_lock:
        clutch.update({"engage_requested": False, "engaged": False,
                       "alignment": None, "refusal": None})


def clutch_snapshot() -> dict[str, Any]:
    with _clutch_lock:
        return {"engage_requested": clutch["engage_requested"], "engaged": clutch["engaged"],
                "aligned": clutch["alignment"] is not None, "refusal": clutch["refusal"]}


def handle_teleop_clutch(engage: bool) -> dict[str, Any]:
    """Engage or release the follow clutch of the running desktop session.

    The loop, not this request thread, captures the alignment: only the loop knows which
    leader reading and which follower measurement belong to the same iteration, and pulling
    the follower bus from here would put a second driver on it.
    """
    if not teleoperation_active:
        return {"success": False, "message": "遥操作会话未激活，不能接合离合", **clutch_snapshot()}
    if not engage:
        reset_clutch()
        return {"success": True, "message": "离合已断开，不再下发目标；重新接合会重新建立参考", **clutch_snapshot()}
    if (teleoperation_session or {}).get("preview"):
        with _clutch_lock:
            clutch["refusal"] = "preview_session_sends_nothing"
        return {"success": False, "message": "预览会话不下发目标，请先停止预览再启动跟随", **clutch_snapshot()}
    with _clutch_lock:
        clutch["engage_requested"] = True
        clutch["refusal"] = None
    return {"success": True, "message": "已请求接合，等待下一个实测帧建立参考", **clutch_snapshot()}


def _try_engage(action: dict[str, Any], follower_raw: dict[str, Any], follower_status: str) -> None:
    with _clutch_lock:
        if not clutch["engage_requested"] or clutch["engaged"]:
            return
        if follower_status == "pending":
            # Nothing has been measured yet; keep the request waiting rather than refusing it.
            return
        clutch["engage_requested"] = False
        if follower_status != "measured":
            clutch["refusal"] = f"follower_feedback_not_measured: {follower_status}"
            logger.warning("Clutch refused: %s", clutch["refusal"])
            return
        alignment, missing = capture_alignment(action, follower_raw)
        if alignment is None:
            clutch["refusal"] = f"alignment_incomplete: {missing}"
            logger.warning("Clutch refused: %s", clutch["refusal"])
            return
        clutch["alignment"] = alignment
        clutch["engaged"] = True
        logger.info("Clutch engaged: reference captured from this iteration's readings")


def _identity_guard(leader_device, follower_device, leader_config: str, follower_config: str,
                    leader_port: str, follower_port: str) -> dict[str, Any]:
    """Refuse to write a calibration onto an arm the evidence says it isn't for.

    Runs after the two devices are constructed but before either port is opened, so a
    refusal never reaches the boards. See lelab/arm_bindings.py for why motor ids and
    joint names cannot tell two arms apart, and why the only identity here is the
    /dev/serial/by-id path. The same check guards the desktop recording entry point.
    """
    from . import arm_bindings

    verdict = arm_bindings.check_desktop_pair(leader_port, follower_port, leader_device,
                                              follower_device, leader_config, follower_config)
    for warning in verdict["warnings"]:
        logger.warning("Teleop identity: %s", warning)
    return verdict


def handle_start_teleoperation(request: TeleoperateRequest, websocket_manager=None) -> dict[str, Any]:
    """Handle start teleoperation request.

    Connects to both arms *synchronously* so that a connection failure (arm
    unplugged, port busy, power off) is reported back to the caller, rather than
    dying silently in the worker thread while the API has already claimed
    success. Only the teleoperation loop runs in the background thread.

    中文说明（核心入口，流程分四步）：
    1. 加 _state_lock 做互斥检查：遥操作/录制/推理三者同时只允许一个会话，
       通过后先置 teleoperation_active=True 占位；
    2. 同步连接设备：由请求中的端口+标定名构造 SO101FollowerConfig（此处经
       follower_safety_kwargs() 注入 max_relative_target 等安全限幅）与
       SO101LeaderConfig；两台设备构造完、串口仍未打开时先做"标定归属"核查
       （_identity_guard），证据对不上就拒绝——绝不把别的臂的标定写进舵机；
       随后逐台 connect、写标定、连相机、configure；任一步失败立即断开已连接
       设备并返回错误；
    3. 连接成功后启动 daemon 工作线程 teleoperation_worker 执行跟随循环，
       HTTP 线程随即返回 success（连接失败不会假报成功）；
    4. 会话状态通过模块级 teleoperation_active 暴露给 /teleoperation-status。
    """
    global teleoperation_active, teleoperation_thread, current_robot, current_teleop, teleoperation_session, stop_reason

    from . import record as _record, rollout as _rollout

    with _state_lock:
        if teleoperation_active:
            return {"success": False, "message": "Teleoperation is already active"}
        if _record.recording_active:
            return {"success": False, "message": "Recording is currently active. Stop it first."}
        if _rollout.inference_active:
            return {"success": False, "message": "Inference is currently active. Stop it first."}
        teleoperation_active = True

    robot = None
    teleop_device = None
    try:
        logger.info(
            f"Starting teleoperation with leader port: {request.leader_port}, follower port: {request.follower_port}"
        )

        # Setup calibration files
        leader_config_name, follower_config_name = setup_calibration_files(
            request.leader_config, request.follower_config
        )

        # Create robot and teleop configs
        # follower_safety_kwargs() 从环境变量读取安全限幅注入从臂配置：
        # max_relative_target（单步目标最大增量）等最终在从臂 send_action 内部生效。
        robot_config = SO101FollowerConfig(
            port=request.follower_port,
            id=follower_config_name,
            **follower_safety_kwargs(),
        )

        teleop_config = SO101LeaderConfig(
            port=request.leader_port,
            id=leader_config_name,
        )

        # Connect synchronously. If either device fails to connect, clean up the
        # other (so its serial port is released) and report the error — do NOT
        # leave the caller thinking teleoperation started.
        logger.info("Initializing robot and teleop device...")
        robot = SO101Follower(robot_config)
        teleop_device = SO101Leader(teleop_config)

        # Identity check while both buses are still closed: a calibration measured
        # on a different arm has to be refused here, not written two lines later.
        identity = _identity_guard(teleop_device, robot, leader_config_name,
                                   follower_config_name, request.leader_port, request.follower_port)

        # Connect each arm separately so the error names which one failed and
        # tells the user what to do, instead of a generic "failed to start".
        logger.info("Connecting to follower arm...")
        try:
            robot.bus.connect()
        except Exception as e:
            raise RuntimeError(
                f"Could not connect to the follower arm on {request.follower_port}. "
                "Make sure it's plugged in and powered on, then try again."
            ) from e

        logger.info("Connecting to leader arm...")
        try:
            teleop_device.bus.connect()
        except Exception as e:
            raise RuntimeError(
                f"Could not connect to the leader arm on {request.leader_port}. "
                "Make sure it's plugged in and powered on, then try again."
            ) from e

        # Write calibration to motors' memory
        logger.info("Writing calibration to motors...")
        robot.bus.write_calibration(robot.calibration)
        teleop_device.bus.write_calibration(teleop_device.calibration)

        # Connect cameras and configure motors
        logger.info("Connecting cameras and configuring motors...")
        for cam in robot.cameras.values():
            cam.connect()
        if request.preview:
            # configure() is what puts torque on the follower. A preview session has
            # to leave it off, otherwise "no output" would only mean "no goal written
            # yet" and the arms would still be holding a position we never asked for.
            logger.warning("Preview session: the follower is not configured, torque stays off")
        else:
            robot.configure()
        teleop_device.configure()
        logger.info("Successfully connected to both devices")

        current_robot = robot
        current_teleop = teleop_device
        stop_reason = None
        reset_clutch()
        if request.engage and not request.preview:
            with _clutch_lock:
                clutch["engage_requested"] = True
        teleoperation_session = {
            "leader_port": request.leader_port,
            "follower_port": request.follower_port,
            "leader_config": leader_config_name,
            "follower_config": follower_config_name,
            "preview": bool(request.preview),
            # The binding evidence (which port was measured as which side, with which
            # calibration and hash) that the identity check accepted.
            "identity": identity,
        }

        # Stream the arms in the background; the worker owns disconnect so stop()
        # does not race the serial bus from the request thread.
        def teleoperation_worker():
            """遥操作后台工作线程（跟随循环）。

            中文说明：
            - 主循环：主臂 get_action() 读取人工拖出的关节目标 → 从臂
              send_action() 执行（max_relative_target 限幅在从臂内部生效）。
            - 广播：按 20FPS 节流，把从臂实测关节角与**同一次主臂读数**（URDF
              弧度）打包成 joint_update 消息，经 websocket_manager 同步广播给
              前端做可视化。主臂一侧直接复用本轮 get_action() 的结果，不再向
              主臂总线追加第二次 sync_read —— 遥操作总线负载不变。
              广播失败只记日志，不影响遥操作本身。
            - 异常处理：循环内任何异常都会结束线程；finally 中断开两臂设备、
              复位全局状态并把 teleoperation_active 置 False。
            - 停止方式：handle_stop_teleoperation 置 teleoperation_active=False，
              while 条件失效后自然走到 finally 完成清理。
            """
            global teleoperation_active, current_robot, current_teleop, teleoperation_session, stop_reason

            unreadable_reads = 0
            limit = max_follower_read_failures()

            logger.info("Starting teleoperation loop...")
            try:
                last_broadcast_time = 0
                broadcast_interval = 0.05  # 20 FPS
                # The most recent follower measurement the loop has, so an engagement request
                # is honoured on the next iteration instead of waiting for a broadcast window.
                last_raw_follower: dict[str, Any] = {}
                last_follower_status = "pending"

                while teleoperation_active:
                    # 核心跟随链路：读主臂当前姿态 → 作为目标下发给从臂执行
                    try:
                        action = teleop_device.get_action()
                    except Exception as exc:
                        # The leader is the operator's input. If it stops answering, name
                        # that as the reason instead of letting the loop die anonymously:
                        # "the arm stopped following" and "the leader went dark" need
                        # different actions from whoever is holding the arms.
                        stop_reason = f"leader_read_failed: {exc}"
                        raise
                    # The same reading drives the follower and is broadcast below, so
                    # target and measured always come from one iteration.
                    _try_engage(action, last_raw_follower, last_follower_status)
                    with _clutch_lock:
                        alignment = clutch["alignment"] if clutch["engaged"] else None
                    if request.preview or alignment is None:
                        # Two ways to stay silent: an explicit preview session, and a session whose
                        # clutch has never been engaged. Neither writes a byte to the follower bus;
                        # the frame below still carries the target the operator would get.
                        target_reading = action
                        target_sent = False
                    else:
                        target_reading = follow_target(action, alignment)
                        try:
                            robot.send_action(target_reading)
                        except Exception as exc:
                            stop_reason = f"follower_send_failed: {exc}"
                            raise
                        target_sent = True

                    current_time = time.time()
                    # 按 broadcast_interval(50ms≈20FPS) 节流，把关节角广播给前端
                    if current_time - last_broadcast_time >= broadcast_interval:
                        try:
                            raw_follower: dict[str, Any] = {}

                            def _read_follower():
                                nonlocal raw_follower
                                raw_follower = dict(robot.get_observation())
                                return joints_from_motor_reading(raw_follower)

                            follower_side = read_joint_side(_read_follower, "follower")
                            leader_side = read_joint_side(
                                lambda: joints_from_motor_reading(action), "leader")
                            last_raw_follower, last_follower_status = raw_follower, follower_side["status"]

                            # Requirement: no following without fresh measured feedback. The
                            # follow loop is local and keeps the arm safe on its own, but a
                            # follower that has stopped answering means we are flying blind —
                            # end the session instead of streaming zeros or a stale pose.
                            # A preview session writes nothing, so a missing reading there is
                            # a gap on screen rather than a reason to stop.
                            if follower_side["status"] != "measured" and not request.preview:
                                unreadable_reads += 1
                                if unreadable_reads >= limit:
                                    stop_reason = (f"follower_feedback_unavailable: {unreadable_reads} "
                                                   f"consecutive reads {follower_side['status']}")
                                    logger.error("Teleoperation stopping itself: %s", stop_reason)
                                    teleoperation_active = False
                            else:
                                unreadable_reads = 0
                            joint_data = {
                                "type": "joint_update",
                                "joints": follower_side["joints"],
                                "leader": leader_side["joints"],
                                "preview": bool(request.preview),
                                "target_sent": target_sent,
                                "target": joints_from_motor_reading(target_reading)[0],
                                "clutch": clutch_snapshot(),
                                "follower_status": follower_side["status"],
                                "leader_status": leader_side["status"],
                                "follower_missing": follower_side["missing"],
                                "leader_missing": leader_side["missing"],
                                "timestamp": current_time,
                            }
                            if websocket_manager and websocket_manager.active_connections:
                                websocket_manager.broadcast_joint_data_sync(joint_data)
                            last_broadcast_time = current_time
                        except Exception as e:
                            logger.error(f"Error broadcasting joint data: {e}")

                    # 1ms 轮询间隔：兼顾跟随低延迟与 CPU 占用（约 1kHz）
                    time.sleep(0.001)
            except Exception as e:
                logger.error(f"Error during teleoperation loop: {e}")
            finally:
                reset_clutch()
                _safe_disconnect(robot)
                _safe_disconnect(teleop_device)
                logger.info("Teleoperation stopped")
                teleoperation_active = False
                current_robot = None
                current_teleop = None
                teleoperation_session = None

        teleoperation_thread = threading.Thread(
            target=teleoperation_worker, name="teleoperation-worker", daemon=True
        )
        teleoperation_thread.start()

        return {
            "success": True,
            "message": "Teleoperation started successfully",
            "leader_port": request.leader_port,
            "follower_port": request.follower_port,
            # Ports with no recorded measurement are allowed to start, but the page
            # has to say so — the operator is the one asserting these are the right arms.
            "identity_warnings": identity["warnings"],
        }

    except Exception as e:
        # Connection (or setup) failed before the loop started: release any
        # device that did open, reset state, and surface the error.
        _safe_disconnect(robot)
        _safe_disconnect(teleop_device)
        teleoperation_active = False
        current_robot = None
        current_teleop = None
        teleoperation_session = None
        logger.error(f"Failed to start teleoperation: {e}")
        # str(e) is already a user-facing message for the connection failures
        # raised above; the toast title supplies the "error starting" context.
        return {"success": False, "message": str(e)}


def handle_stop_teleoperation() -> dict[str, Any]:
    """Handle stop teleoperation request.

    Signals the worker via `teleoperation_active = False` and waits for it to
    exit. The worker owns the disconnect call, so this avoids racing the
    serial bus from the request thread.

    中文说明：停止流程——置停止标志 teleoperation_active=False（工作线程的
    while 条件随即失效）→ 对线程 join(5s) 等待退出 → 设备断开由工作线程自己的
    finally 完成（请求线程不碰串口，避免竞态）；join 超时仅告警不阻塞请求。
    """
    global teleoperation_active, teleoperation_thread

    if not teleoperation_active:
        return {"success": False, "message": "No teleoperation session is active"}

    logger.info("Stop teleoperation triggered from web interface")
    teleoperation_active = False

    worker = teleoperation_thread
    if worker is not None and worker.is_alive():
        worker.join(timeout=5.0)
        if worker.is_alive():
            logger.warning("Teleoperation worker did not exit within 5s")
    teleoperation_thread = None

    return {"success": True, "message": "Teleoperation stopped successfully"}


def handle_teleoperation_status() -> dict[str, Any]:
    """Handle teleoperation status request

    中文说明：/teleoperation-status 接口。直接读取模块级标志
    teleoperation_active（无锁读，布尔赋值原子），available_controls 告诉前端
    当前是否可以点击"停止遥操作"按钮。
    """
    return {
        "teleoperation_active": teleoperation_active,
        "clutch": clutch_snapshot(),
        # What the live session is actually driving, so the page cannot present a stale
        # picker selection as if it were the active arms.
        "session": teleoperation_session,
        # None unless the session ended itself (e.g. feedback became unreadable).
        "stop_reason": stop_reason,
        "available_controls": {
            "stop_teleoperation": teleoperation_active,
        },
        "message": "Teleoperation status retrieved successfully",
    }


def handle_get_joint_positions() -> dict[str, Any]:
    """Handle get current robot joint positions request

    中文说明：/joint-positions 接口。仅当遥操作会话存活时对当前从臂读一次；
    读取失败或关节残缺就如实返回 success=False，不再用全零字典冒充实测位置。
    """
    global current_robot

    if not teleoperation_active or current_robot is None:
        return {"success": False, "message": "No active teleoperation session"}

    side = read_joint_side(lambda: joints_from_motor_reading(current_robot.get_observation()),
                           "follower-rest")
    if side["status"] != "measured":
        return {"success": False,
                "message": f"Follower joint read {side['status']}"
                           + (f": {side['error']}" if side["error"] else f": missing {side['missing']}"),
                "status": side["status"], "missing": side["missing"]}
    return {"success": True, "joint_positions": side["joints"], "status": "measured",
            "timestamp": side["attempted_at"]}
