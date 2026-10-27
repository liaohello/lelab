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
import threading
import time
from typing import Any

from pydantic import BaseModel

from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig

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
    """

    leader_port: str
    follower_port: str
    leader_config: str
    follower_config: str


def get_joint_positions_from_robot(robot) -> dict[str, float]:
    """
    Extract current joint positions from the robot and convert to URDF joint format.

    lerobot drives the SO-101 with ``use_degrees=True`` by default, so each
    ``observation["<motor>.pos"]`` is already the joint angle in degrees relative
    to the calibration center — which is also the URDF's zero pose. The URDF
    joint value is therefore just that angle converted to radians, for every
    joint. (The gripper reports 0–100 rather than degrees, but that range lands
    inside the Jaw limit, matching the open/closed sweep.)

    Args:
        robot: The robot instance (SO101Follower)

    Returns:
        Dictionary mapping URDF joint names to radian values

    中文说明：
    - 数据来源：robot.get_observation() 返回的字典中，每个电机对应一个
      "<电机名>.pos" 键（度数，相对标定零位）。
    - 转换：角度乘 pi/180 转为弧度后，按 motor_to_urdf_mapping 映射为 URDF
      关节名，供前端 3D 模型渲染；键缺失兜底 0.0，异常时返回全零字典。
    """
    motor_to_urdf_mapping = {
        "shoulder_pan": "Rotation",
        "shoulder_lift": "Pitch",
        "elbow_flex": "Elbow",
        "wrist_flex": "Wrist_Pitch",
        "wrist_roll": "Wrist_Roll",
        "gripper": "Jaw",
    }

    try:
        observation = robot.get_observation()

        joint_positions: dict[str, float] = {}
        debug_rows = []
        for motor_name, urdf_joint_name in motor_to_urdf_mapping.items():
            motor_key = f"{motor_name}.pos"
            if motor_key not in observation:
                logger.warning(f"Motor {motor_key} not found in observation")
                joint_positions[urdf_joint_name] = 0.0
                continue

            angle_degrees = observation[motor_key]
            joint_positions[urdf_joint_name] = angle_degrees * math.pi / 180.0
            debug_rows.append(f"{motor_name:14s} {angle_degrees:+8.2f}° → {urdf_joint_name:11s}")

        # Throttled debug print (~once per second at 20 Hz broadcast).
        now = time.time()
        if now - getattr(get_joint_positions_from_robot, "_last_log", 0) > 1.0:
            get_joint_positions_from_robot._last_log = now
            logger.info("[joint-debug]\n  " + "\n  ".join(debug_rows))

        return joint_positions

    except Exception as e:
        logger.error(f"Error getting joint positions: {e}")
        return dict.fromkeys(motor_to_urdf_mapping.values(), 0.0)


def _safe_disconnect(device) -> None:
    """Disconnect a robot/teleop device, swallowing (but logging) any error.

    Used on the connection-failure cleanup path so one device's failure can't
    leave the other holding its serial port open.

    中文说明：连接失败路径上的兜底清理——内部转调 safe_disconnect_device，
    吞掉并记录断开过程中的异常，避免一台设备断开失败导致另一台的串口被占死。
    """
    safe_disconnect_device(device, logger)


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
       SO101LeaderConfig，逐台 connect、写标定、连相机、configure；任一步
       失败立即断开已连接设备并返回错误；
    3. 连接成功后启动 daemon 工作线程 teleoperation_worker 执行跟随循环，
       HTTP 线程随即返回 success（连接失败不会假报成功）；
    4. 会话状态通过模块级 teleoperation_active 暴露给 /teleoperation-status。
    """
    global teleoperation_active, teleoperation_thread, current_robot, current_teleop

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
        robot.configure()
        teleop_device.configure()
        logger.info("Successfully connected to both devices")

        current_robot = robot
        current_teleop = teleop_device

        # Stream the arms in the background; the worker owns disconnect so stop()
        # does not race the serial bus from the request thread.
        def teleoperation_worker():
            """遥操作后台工作线程（跟随循环）。

            中文说明：
            - 主循环：主臂 get_action() 读取人工拖出的关节目标 → 从臂
              send_action() 执行（max_relative_target 限幅在从臂内部生效）。
            - 广播：按 20FPS 节流，把从臂关节角（URDF 弧度）打包成
              joint_update 消息，经 websocket_manager 同步广播给前端做可视化；
              广播失败只记日志，不影响遥操作本身。
            - 异常处理：循环内任何异常都会结束线程；finally 中断开两臂设备、
              复位全局状态并把 teleoperation_active 置 False。
            - 停止方式：handle_stop_teleoperation 置 teleoperation_active=False，
              while 条件失效后自然走到 finally 完成清理。
            """
            global teleoperation_active, current_robot, current_teleop

            logger.info("Starting teleoperation loop...")
            try:
                last_broadcast_time = 0
                broadcast_interval = 0.05  # 20 FPS

                while teleoperation_active:
                    # 核心跟随链路：读主臂当前姿态 → 作为目标下发给从臂执行
                    action = teleop_device.get_action()
                    robot.send_action(action)

                    current_time = time.time()
                    # 按 broadcast_interval(50ms≈20FPS) 节流，把关节角广播给前端
                    if current_time - last_broadcast_time >= broadcast_interval:
                        try:
                            joint_positions = get_joint_positions_from_robot(robot)
                            joint_data = {
                                "type": "joint_update",
                                "joints": joint_positions,
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
                _safe_disconnect(robot)
                _safe_disconnect(teleop_device)
                logger.info("Teleoperation stopped")
                teleoperation_active = False
                current_robot = None
                current_teleop = None

        teleoperation_thread = threading.Thread(
            target=teleoperation_worker, name="teleoperation-worker", daemon=True
        )
        teleoperation_thread.start()

        return {
            "success": True,
            "message": "Teleoperation started successfully",
            "leader_port": request.leader_port,
            "follower_port": request.follower_port,
        }

    except Exception as e:
        # Connection (or setup) failed before the loop started: release any
        # device that did open, reset state, and surface the error.
        _safe_disconnect(robot)
        _safe_disconnect(teleop_device)
        teleoperation_active = False
        current_robot = None
        current_teleop = None
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
        "available_controls": {
            "stop_teleoperation": teleoperation_active,
        },
        "message": "Teleoperation status retrieved successfully",
    }


def handle_get_joint_positions() -> dict[str, Any]:
    """Handle get current robot joint positions request

    中文说明：/joint-positions 接口。仅当遥操作会话存活（teleoperation_active
    且 current_robot 非空）时，对当前从臂调用 get_joint_positions_from_robot
    读取关节角（弧度、URDF 关节名）返回给前端；失败时返回 success=False。
    """
    global current_robot

    if not teleoperation_active or current_robot is None:
        return {"success": False, "message": "No active teleoperation session"}

    try:
        joint_positions = get_joint_positions_from_robot(current_robot)
        return {"success": True, "joint_positions": joint_positions, "timestamp": time.time()}
    except Exception as e:
        logger.error(f"Error getting joint positions: {e}")
        return {"success": False, "message": f"Failed to get joint positions: {str(e)}"}
