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
import re
import shutil
import threading
import time
import traceback
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from lerobot.configs.dataset import DatasetRecordConfig
from lerobot.datasets import LeRobotDataset
from lerobot.robots.so_follower import SO101FollowerConfig

# Import the main record functionality to reuse it
from lerobot.scripts.lerobot_record import RecordConfig
from lerobot.teleoperators.so_leader import SO101LeaderConfig

from .dataset_repair import DatasetRepairError, repair_local_dataset
from .safety import follower_safety_kwargs
from .utils.config import setup_calibration_files, with_lelab_tag
from .utils.devices import safe_disconnect_device

logger = logging.getLogger(__name__)

_LOCAL_DATASET_NAMESPACE = "local"
_INVALID_REPO_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")

# Global variables for recording state
# 录制会话的全局状态（HTTP 线程写事件标志，工作线程读写进度，供 /recording-status、
# /camera-feed 等接口读取）：
# - recording_active：会话存活标志；current_robot 为已连接的从臂实例，供相机预览读帧；
# - recording_events：网页版"键盘事件"字典（替代 ESC/左右方向键），传给 LeRobot record_loop；
# - current_phase 为阶段机：preparing → recording → resetting →（逐集循环）→ completed/error；
# - current_episode/saved_episodes 跟踪进度，last_recording_info 存最近一次会话结果快照。
recording_active = False
recording_thread: threading.Thread | None = None
# Live SO101Follower for the active session, set once the robot connects so the
# /camera-feed endpoint can peek its cameras' latest frames. Reset to None on
# teardown. Always gated behind `recording_active` by readers.
current_robot = None
recording_events = None  # Events dict for controlling recording session
recording_config = None  # Store recording configuration
recording_start_time = None  # Track when recording started
session_end_elapsed_seconds = None  # Final session duration after the run ends
current_episode = 1  # Track current episode number
saved_episodes = 0  # Track how many episodes have been saved
current_phase = "preparing"  # Track current phase: "preparing", "recording", "resetting", "completed"
phase_start_time = None  # Track when current phase started
last_recording_info: dict[str, Any] | None = (
    None  # Snapshot of the most recently completed dataset (for /dataset-info)
)
# Guards the start path so two concurrent POST /start-recording calls cannot
# both pass the active-flag check.
_state_lock = threading.Lock()


class RecordingRequest(BaseModel):
    """录制启动请求（前端录制表单 POST /start-recording 的请求体）。

    中文说明：字段与 LeRobot CLI 的 record 命令一一对应——
    - leader_port/follower_port、leader_config/follower_config：双臂串口与标定名；
    - dataset_repo_id：数据集 id（Hub 风格 namespace/name，纯本地名会被归入
      local/ 命名空间并加时间戳）；single_task：自然语言任务描述，写入数据集元信息；
    - num_episodes/episode_time_s/reset_time_s/fps：集数、每集采集时长、
      每集间环境重置时长、控制频率；
    - video：相机帧是否编码为 mp4（False 时像素直接进 parquet）；
    - push_to_hub/private/tags：录制完成后是否推送 HuggingFace Hub 及其可见性/标签；
    - resume：续录既有数据集；streaming_encoding：边采集边编码视频；
    - cameras：前端相机选择弹窗回传的 {名称: {camera_index, fps, width, ...}}；
    - test_mode：跳过真实设备连接（仅测试用）。
    """

    leader_port: str
    follower_port: str
    leader_config: str
    follower_config: str
    dataset_repo_id: str
    single_task: str
    num_episodes: int = 5
    episode_time_s: int = 30
    reset_time_s: int = 10
    fps: int = 30
    video: bool = True
    push_to_hub: bool = False
    tags: list[str] = []
    private: bool = False
    resume: bool = False
    streaming_encoding: bool = True
    cameras: dict = {}
    test_mode: bool = False  # Skip robot connection for testing


class UploadRequest(BaseModel):
    """数据集上传请求（录制完成后推送 HuggingFace Hub）。

    中文说明：dataset_repo_id 指定要上传的本地数据集，tags 追加标签，
    private 控制仓库可见性。
    """

    dataset_repo_id: str
    tags: list[str] = []
    private: bool = False


class DatasetInfoRequest(BaseModel):
    """查询数据集元信息 / 删除数据集时的请求体（仅需数据集 id）。"""

    dataset_repo_id: str


def _normalize_dataset_repo_id(repo_id: str) -> str:
    """Return a two-component repo id accepted by LeRobot.

    The web UI supports fully local recording without a Hugging Face account.
    LeRobot still expects every dataset id to have the Hub-style
    ``namespace/name`` shape, so bare local names are placed under the stable
    ``local`` namespace.

    中文说明：去首尾空格 → 按 "/" 拆分（无斜杠则归入 local/ 命名空间）→ 用
    _INVALID_REPO_COMPONENT 正则把非法字符替换为下划线；namespace/name 为空
    或为 "."、".." 时抛 ValueError。
    """
    candidate = repo_id.strip()
    if not candidate:
        raise ValueError("Dataset name cannot be empty")

    if "/" in candidate:
        namespace, name = candidate.split("/", 1)
    else:
        namespace, name = _LOCAL_DATASET_NAMESPACE, candidate

    namespace = _INVALID_REPO_COMPONENT.sub("_", namespace.strip())
    name = _INVALID_REPO_COMPONENT.sub("_", name.strip())
    if namespace in {"", ".", ".."} or name in {"", ".", ".."}:
        raise ValueError("Dataset id must contain a valid namespace and dataset name")

    return f"{namespace}/{name}"


def _prepare_dataset_repo_id(repo_id: str, *, resume: bool, now: datetime | None = None) -> str:
    """Normalize a dataset id and add a unique suffix for a new session.

    中文说明：resume=True 时直接返回规范化 id（续录旧数据集）；否则在名字后
    拼当前时间戳后缀（name_YYYYmmdd_HHMMSS），让每次新会话落在独立目录、
    互不覆盖。
    """
    normalized = _normalize_dataset_repo_id(repo_id)
    if resume:
        return normalized

    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    namespace, name = normalized.split("/", 1)
    return f"{namespace}/{name}_{timestamp}"


def _platform_backend():
    """Pin the OpenCV backend per-platform so the index→camera mapping matches
    what the /available-cameras thumbnails were captured with. cv2.CAP_ANY can
    pick different backends across calls on macOS, silently reordering cameras
    between the modal preview and the recording.

    中文说明：按操作系统固定 OpenCV 后端（macOS→AVFoundation、Linux→V4L2、
    Windows→DirectShow），避免 CAP_ANY 在不同调用间选择不同后端，导致相机
    顺序与 /available-cameras 预览不一致。
    """
    import platform

    from lerobot.cameras.configs import Cv2Backends

    system = platform.system()
    if system == "Darwin":
        return Cv2Backends.AVFOUNDATION
    if system == "Linux":
        return Cv2Backends.V4L2
    if system == "Windows":
        # DirectShow, matching the order /available-cameras enumerates (via
        # pygrabber) so a camera_index always opens the previewed device.
        return Cv2Backends.DSHOW
    return Cv2Backends.ANY


def _build_camera_configs(cameras: dict, default_backend) -> dict:
    """Convert the frontend camera dict into OpenCVCameraConfig objects.

    `backend` (a Cv2Backends name) and `fourcc` (a 4-char code) are optional per
    camera; when omitted they fall back to `default_backend` and auto-detect.

    中文说明：遍历前端传来的 {相机名: 参数}，仅接受 type=="opencv" 的条目；
    每台相机生成一个 OpenCVCameraConfig（索引/分辨率/fps/后端/像素格式）。
    Linux(V4L2) 下未指定像素格式时默认补 MJPG，降低 Jetson 多相机共享 USB
    带宽时的占用。
    """
    from lerobot.cameras.configs import Cv2Backends
    from lerobot.cameras.opencv import OpenCVCameraConfig

    camera_configs: dict = {}
    for camera_name, camera_data in cameras.items():
        if camera_data.get("type") != "opencv":
            logger.warning(
                f"⚠️ CAMERA CONFIG: Unsupported camera type '{camera_data.get('type')}' for {camera_name}"
            )
            continue

        backend_name = camera_data.get("backend")
        backend = Cv2Backends[backend_name] if backend_name else default_backend
        fourcc = camera_data.get("fourcc") or None
        if fourcc is None and backend == Cv2Backends.V4L2:
            # Multiple USB cameras on Jetson share one hub. MJPG avoids the
            # much higher USB bandwidth of V4L2's common YUYV default.
            fourcc = "MJPG"

        camera_configs[camera_name] = OpenCVCameraConfig(
            index_or_path=camera_data.get("camera_index", 0),
            backend=backend,
            fps=camera_data.get("fps"),
            width=camera_data.get("width"),
            height=camera_data.get("height"),
            fourcc=fourcc,
        )
        logger.info(
            f"✅ CAMERA CONFIG: {camera_name} -> OpenCVCameraConfig("
            f"index={camera_data.get('camera_index')}, backend={backend.name}, "
            f"{camera_data.get('width')}x{camera_data.get('height')}@{camera_data.get('fps')}fps, "
            f"fourcc={fourcc})"
        )
    return camera_configs


def create_record_config(request: RecordingRequest) -> RecordConfig:
    """Create a RecordConfig from the recording request

    中文说明：网页参数 → LeRobot RecordConfig 的转换枢纽：
    1. setup_calibration_files 由标定名生成/定位标定文件；
    2. _build_camera_configs 把前端相机字典转成 OpenCVCameraConfig（后端按
       平台固定，请求可逐相机覆盖）；
    3. 组装 SO101FollowerConfig（挂上相机并注入 follower_safety_kwargs 安全
       限幅）与 SO101LeaderConfig；
    4. 组装 DatasetRecordConfig（repo_id/任务/集数/时长/fps/video/push 等，
       未开启 push 时 tags 置 None 以沿用 LeRobot 默认）；
    5. 最终合成 lerobot_record.RecordConfig（与 CLI 同一配置结构），API 模式
       下关闭桌面显示与提示音。
    """
    # Setup calibration files
    leader_config_name, follower_config_name = setup_calibration_files(
        request.leader_config, request.follower_config
    )

    # Convert the frontend camera dict into OpenCVCameraConfig objects. Backend
    # defaults to the platform pin unless the request overrides it per camera.
    camera_configs = _build_camera_configs(request.cameras, _platform_backend())

    # Create robot config
    robot_config = SO101FollowerConfig(
        port=request.follower_port,
        id=follower_config_name,
        cameras=camera_configs,
        **follower_safety_kwargs(),
    )

    # Create teleop config
    teleop_config = SO101LeaderConfig(
        port=request.leader_port,
        id=leader_config_name,
    )

    # Create dataset config
    dataset_config = DatasetRecordConfig(
        repo_id=request.dataset_repo_id,
        single_task=request.single_task,
        num_episodes=request.num_episodes,
        episode_time_s=request.episode_time_s,
        reset_time_s=request.reset_time_s,
        fps=request.fps,
        video=request.video,
        push_to_hub=request.push_to_hub,
        # Upstream typing: tags is `list[str] | None`. None when push is off
        # keeps the lerobot default.
        tags=with_lelab_tag(request.tags) if request.push_to_hub else None,
        private=request.private,
        streaming_encoding=request.streaming_encoding,
    )

    # Create the main record config
    record_config = RecordConfig(
        robot=robot_config,
        teleop=teleop_config,
        dataset=dataset_config,
        resume=request.resume,
        display_data=False,  # Don't display data in API mode
        play_sounds=False,  # Don't play sounds in API mode
    )

    return record_config


def handle_start_recording(request: RecordingRequest) -> dict[str, Any]:
    """Handle start recording request by using the existing record() function

    中文说明（启动流程）：
    1. 加 _state_lock 做互斥检查：录制/遥操作/推理三者同时只允许一个，通过后
       置 recording_active=True 并复位全部进度状态（集数、阶段、结果快照）；
    2. 规范化 dataset_repo_id（新会话加时间戳后缀）；resume 时先确认数据集
       未被封存（sealing.ensure_mutable），否则直接失败返回；
    3. 初始化 recording_events 事件字典（网页按钮 → 原键盘按键的映射），并
       调 create_record_config 生成 LeRobot 配置；
    4. 启动 daemon 线程 recording_worker 执行实际录制，HTTP 线程立即返回
       success；进度由前端轮询 /recording-status（读 recording_active/
       current_phase 等）获得。启动阶段任何异常都会复位 recording_active。
    """
    global \
        recording_active, \
        recording_thread, \
        recording_events, \
        recording_config, \
        recording_start_time, \
        session_end_elapsed_seconds, \
        current_episode, \
        saved_episodes, \
        current_phase, \
        phase_start_time, \
        last_recording_info

    from . import rollout as _rollout, teleoperate as _teleoperate

    # Claim the active flag under the lock so two concurrent starts can't both
    # pass the precondition check.
    with _state_lock:
        if recording_active:
            return {"success": False, "message": "Recording is already active"}
        if _teleoperate.teleoperation_active:
            return {"success": False, "message": "Teleoperation is currently active. Stop it first."}
        if _rollout.inference_active:
            return {"success": False, "message": "Inference is currently active. Stop it first."}
        recording_active = True
        recording_thread = None
        recording_events = None
        recording_config = None
        recording_start_time = None
        session_end_elapsed_seconds = None
        current_episode = 1
        saved_episodes = 0
        current_phase = "preparing"
        phase_start_time = None
        last_recording_info = None

    try:
        # LeRobot requires the Hub-style ``namespace/name`` shape even when the
        # dataset remains entirely local. Bare names therefore live under the
        # local namespace. New sessions receive a unique timestamped name.
        request.dataset_repo_id = _prepare_dataset_repo_id(
            request.dataset_repo_id,
            resume=request.resume,
        )

        if request.resume:
            # Resuming appends episodes to an existing dataset; a sealed
            # dataset is immutable by definition.
            from .sealing import SealError, ensure_mutable

            try:
                ensure_mutable(request.dataset_repo_id, action="resume recording into")
            except SealError as exc:
                with _state_lock:
                    recording_active = False
                    recording_thread = None
                return {"success": False, "message": str(exc)}

        logger.info(f"Starting recording for dataset: {request.dataset_repo_id}")
        logger.info(f"Task: {request.single_task}")

        recording_config = request
        recording_events = {
            "exit_early": False,  # Right arrow key -> "Skip to next episode" button
            "stop_recording": False,  # ESC key -> "Stop recording" button
            "rerecord_episode": False,  # Left arrow key -> "Re-record episode" button
        }

        record_config = create_record_config(request)

        def recording_worker():
            """录制后台工作线程主体。

            中文说明：先等前端释放相机设备（~2s），再调 record_with_web_events
            执行录制主循环（设备连接、逐集 record_loop、落盘都在其中）。成功时
            把数据集摘要写入 last_recording_info；失败时记录完整 traceback、置
            current_phase="error"，并保留 saved_episodes 供前端上传部分数据集；
            finally 中复位 recording_active 等全局状态（设备断开由
            record_with_web_events 的 finally 负责，这里不重复处理）。
            """
            global \
                recording_active, \
                recording_start_time, \
                session_end_elapsed_seconds, \
                current_phase, \
                phase_start_time, \
                current_episode, \
                saved_episodes, \
                last_recording_info
            recording_start_time = time.time()
            current_episode = 1
            saved_episodes = 0

            try:
                logger.info(
                    "Recording session started: dataset=%s task=%r episodes=%d",
                    request.dataset_repo_id,
                    request.single_task,
                    request.num_episodes,
                )

                # Give the frontend's camera streams time to release the
                # underlying devices before lerobot tries to open them.
                if request.cameras:
                    logger.info(
                        "Waiting for camera resources to be released (cameras: %s)",
                        list(request.cameras.keys()),
                    )
                    # 等前端相机预览释放设备，避免 lerobot 打开相机失败
                    time.sleep(2.0)

                dataset = record_with_web_events(record_config, recording_events)
                logger.info(f"Recording completed successfully. Dataset has {dataset.num_episodes} episodes")
                last_recording_info = {
                    "success": True,
                    "dataset_repo_id": request.dataset_repo_id,
                    "num_episodes": dataset.num_episodes,
                    "single_task": request.single_task,
                    "fps": dataset.fps,
                    "features": list(dataset.features.keys()),
                    "total_frames": dataset.num_frames,
                    "robot_type": getattr(dataset.meta, "robot_type", "Unknown robot"),
                }
            except Exception as e:
                # lerobot's init_logging() installs a root formatter that only
                # renders the message, so logger.exception() would drop the
                # traceback. Fold it into the message instead.
                logger.error("Recording session failed: %s\n%s", e, traceback.format_exc())
                current_phase = "error"
                if recording_start_time:
                    session_end_elapsed_seconds = int(time.time() - recording_start_time)
                # Episodes already saved survive the failure (it can happen in
                # the reset phase, in finalize, or in the Hub push), so keep the
                # count: the frontend still offers them for upload.
                last_recording_info = {
                    "success": False,
                    "error": str(e),
                    "dataset_repo_id": request.dataset_repo_id,
                    "saved_episodes": saved_episodes,
                }
            finally:
                if current_phase != "error":
                    current_phase = "completed"
                if recording_start_time:
                    session_end_elapsed_seconds = int(time.time() - recording_start_time)
                recording_active = False
                recording_start_time = None
                phase_start_time = None
                current_episode = 1
                saved_episodes = 0
                logger.info("Recording session ended")

        recording_thread = threading.Thread(target=recording_worker, name="recording-worker", daemon=True)
        recording_thread.start()

        return {
            "success": True,
            "message": "Recording started successfully",
            "dataset_id": request.dataset_repo_id,
            "num_episodes": request.num_episodes,
        }

    except Exception as e:
        recording_active = False
        logger.error(f"Failed to start recording: {e}")
        return {"success": False, "message": f"Failed to start recording: {str(e)}"}


def handle_stop_recording() -> dict[str, Any]:
    """Handle stop recording request - replaces ESC key

    中文说明：仅置 recording_events 中的 stop_recording 与 exit_early 两个标志
    （替代键盘 ESC），由工作线程在合适时机响应：先保存当前集，随后退出循环并
    finalize 落盘。真正的结束/保存发生在 record_with_web_events 内，本接口只
    负责"发起请求"并立即返回，不阻塞 HTTP 线程。
    """
    global current_phase, phase_start_time

    if not recording_active or recording_events is None:
        return {"success": False, "message": "No recording session is active"}

    recording_events["stop_recording"] = True
    recording_events["exit_early"] = True
    current_phase = "stopping"
    phase_start_time = None
    logger.info("Stop recording triggered from web interface")
    return {
        "success": True,
        "message": "Recording stop requested successfully",
        "session_ending": True,
    }


def handle_exit_early() -> dict[str, Any]:
    """Handle exit early request - replaces right arrow key

    中文说明：置 exit_early 标志（替代右方向键），让 record_loop 提前结束当前
    阶段（采集或重置）；采集阶段被提前结束时已采的帧仍会正常保存为当前集。
    _exit_early_triggered 是本模块附加的跟踪标志（record_loop 不会复位它），
    用于区分"用户主动跳过"与"episode_time_s 自然到时"——后者会触发重录逻辑。
    """
    if not recording_active or recording_events is None:
        return {"success": False, "message": "No recording session is active"}
    recording_events["exit_early"] = True
    # Tracking flag that record_loop won't reset, so the worker can tell
    # "user pressed skip" from "control_time_s elapsed naturally".
    recording_events["_exit_early_triggered"] = True
    logger.info("Exit early triggered (current phase: %s)", current_phase)
    phase_name = "recording phase" if current_phase == "recording" else "reset phase"
    return {
        "success": True,
        "message": f"Exit early triggered successfully for {phase_name}",
        "current_phase": current_phase,
        "events_state": dict(recording_events),
    }


def handle_rerecord_episode() -> dict[str, Any]:
    """Handle rerecord episode request - replaces left arrow key

    中文说明：置 rerecord_episode 与 exit_early 标志（替代左方向键）。工作线程
    在保存前检查到 rerecord_episode 后，丢弃当前集帧缓冲（clear_episode_buffer）、
    不递增集数，经重置阶段后重新采集同一集。
    """
    if not recording_active or recording_events is None:
        return {"success": False, "message": "No recording session is active"}
    recording_events["rerecord_episode"] = True
    recording_events["exit_early"] = True
    logger.info("Re-record episode triggered")
    return {
        "success": True,
        "message": "Re-record episode requested successfully",
        "events_state": dict(recording_events),
    }


def handle_recording_status() -> dict[str, Any]:
    """Handle recording status request

    中文说明：/recording-status 接口，前端据此轮询渲染进度条与按钮可用性。
    返回会话是否存活、当前阶段（preparing/recording/resetting/completed/error）、
    当前集/已存集数、各阶段耗时与时间上限；会话结束后返回 session_ended=True
    提示前端停止轮询；出错时附带错误原因与已保存集数，便于前端提供"上传
    部分数据集"的选项。
    """
    # If recording is not active and phase is completed or error, indicate session has ended
    session_ended = not recording_active and current_phase in ["completed", "error"]

    # Log when session has ended to help debug frontend polling
    if session_ended:
        if current_phase == "error":
            logger.info(
                "📡 RECORDING STATUS REQUEST: Session failed with error - frontend should stop polling"
            )
            print("📡 STATUS CHANGE: Frontend is still polling after session error - should stop now")
        else:
            logger.info("📡 RECORDING STATUS REQUEST: Session has ended - frontend should stop polling")
            print("📡 STATUS CHANGE: Frontend is still polling after session end - should stop now")

    status = {
        "recording_active": recording_active,
        "current_phase": current_phase,  # "preparing", "recording", "resetting", "completed"
        "session_ended": session_ended,  # New field to indicate session completion
        "available_controls": {
            "stop_recording": recording_active,  # ESC key replacement
            "exit_early": recording_active,  # Right arrow key replacement
            "rerecord_episode": recording_active
            and current_phase == "recording",  # Only during recording phase
        },
        "message": "Recording session failed with error - check logs"
        if current_phase == "error"
        else (
            "Recording session has ended - stop polling"
            if session_ended
            else "Recording status retrieved successfully"
        ),
    }

    # Always echo the stamped dataset id whenever a config exists, so the frontend
    # can read the actual on-disk repo_id (post stamp) for upload navigation.
    if recording_config:
        status["dataset_repo_id"] = recording_config.dataset_repo_id

    # Carry the failure reason and how much of the session survived it, so the
    # frontend can offer a partial dataset for upload and only send the user
    # home when nothing was saved.
    if current_phase == "error" and last_recording_info:
        status["error"] = last_recording_info.get("error", "Unknown error")
        status["saved_episodes"] = last_recording_info.get("saved_episodes", 0)

    # Add episode information if recording is active
    if recording_active and recording_config:
        status["current_episode"] = current_episode
        status["total_episodes"] = recording_config.num_episodes
        status["saved_episodes"] = saved_episodes  # Track completed episodes
        # Names of the cameras the user configured for this session. The frontend
        # renders a live /camera-feed/{name} preview for exactly these — nothing
        # else — so only configured cameras are ever shown.
        status["cameras"] = list(recording_config.cameras.keys())

        # Add session start time if available
        if recording_start_time:
            status["session_start_time"] = recording_start_time
            status["session_elapsed_seconds"] = int(time.time() - recording_start_time)

        # Add phase timing information
        if phase_start_time:
            status["phase_start_time"] = phase_start_time
            status["phase_elapsed_seconds"] = int(time.time() - phase_start_time)

            # Add phase time limits
            if current_phase == "recording":
                status["phase_time_limit_s"] = recording_config.episode_time_s
            elif current_phase == "resetting":
                status["phase_time_limit_s"] = recording_config.reset_time_s
    elif session_end_elapsed_seconds is not None:
        status["session_elapsed_seconds"] = session_end_elapsed_seconds

    return status


def camera_feed_frames(cam_key: str, fps: float = 15.0):
    """Yield multipart-MJPEG chunks of one recording camera's latest frames.

    Reads via the camera's non-blocking `read_latest()` peek, so it shares the
    record loop's lock-protected frame buffer with zero device contention (never
    `async_read()`, which would consume the new-frame event and could disturb
    record timing). Streams only while a session is live and the named camera
    exists; ends cleanly otherwise so the browser <img> stops. Capped well below
    the record fps so it stays a passive observer.

    中文说明（MJPEG 帧生成器）：循环 yield multipart/x-mixed-replace 格式的
    JPEG 帧块给浏览器 <img>。关键是读帧方式：用相机的 read_latest() 读取
    record 循环已缓存并加锁保护的最新帧（零设备竞争），绝不能调 async_read()
    去抢新帧事件，否则会干扰录制节拍。会话结束或相机不存在时生成器自然结束，
    浏览器端视频流随之停止。
    """
    import cv2

    interval = 1.0 / fps
    # The session sets `current_robot` only after the robot connects (which is
    # preceded by a ~2s camera-release wait), so give it a moment to appear.
    deadline = time.time() + 15.0
    while recording_active and current_robot is None and time.time() < deadline:
        time.sleep(0.1)

    while recording_active:
        # Snapshot the module global once per iteration. The recording teardown
        # clears current_robot before it disconnects the cameras, so reading it
        # a single time avoids dereferencing a robot that went away between the
        # check and the access.
        robot = current_robot
        if robot is None:
            break
        cam = robot.cameras.get(cam_key)
        if cam is None:
            # Unknown/removed camera — nothing to stream.
            break
        try:
            # read_latest() returns an RGB frame (OpenCVCamera default color
            # mode); may raise if the camera hasn't produced a fresh frame yet.
            frame = cam.read_latest()
        except (TimeoutError, RuntimeError):
            time.sleep(interval)
            continue
        except Exception as e:
            logger.warning("camera-feed %s: unexpected read error: %s", cam_key, e)
            break

        bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpg", bgr)
        if not ok:
            time.sleep(interval)
            continue
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
        time.sleep(interval)


def handle_get_dataset_info(request: DatasetInfoRequest) -> dict[str, Any]:
    """Return dataset metadata — from the most recent session if it matches,
    otherwise by loading the local LeRobot cache copy.

    中文说明：优先返回最近一次会话的内存快照 last_recording_info；不匹配时先
    repair_local_dataset 修复，再从本地缓存加载 LeRobotDataset 读取集数/fps/
    特征列表等元信息。
    """
    if last_recording_info and last_recording_info.get("dataset_repo_id") == request.dataset_repo_id:
        return last_recording_info

    try:
        from lerobot.datasets import LeRobotDataset

        repair_local_dataset(request.dataset_repo_id)

        dataset = LeRobotDataset(request.dataset_repo_id)
        return {
            "success": True,
            "dataset_repo_id": request.dataset_repo_id,
            "num_episodes": dataset.num_episodes,
            "single_task": getattr(dataset.meta, "single_task", "Unknown task"),
            "fps": dataset.fps,
            "features": list(dataset.features.keys()),
            "total_frames": dataset.num_frames,
            "robot_type": getattr(dataset.meta, "robot_type", "Unknown robot"),
        }
    except DatasetRepairError as e:
        logger.warning(f"Could not repair local dataset {request.dataset_repo_id}: {e}")
        return {"success": False, "message": str(e)}

    except Exception as e:
        logger.warning(f"Could not load local dataset {request.dataset_repo_id}: {e}")
        return {
            "success": False,
            "message": f"Dataset {request.dataset_repo_id} not found locally",
        }


def handle_delete_dataset(request: DatasetInfoRequest) -> dict[str, Any]:
    """Remove a recorded dataset's directory from local disk.

    中文说明：先 ensure_mutable 确认数据集未被封存，再在 HF_LEROBOT_HOME 内
    解析目标目录（拒绝路径穿越），shutil.rmtree 删除后同步清理内存快照。
    """
    global last_recording_info
    from pathlib import Path

    from lerobot.utils.constants import HF_LEROBOT_HOME

    from .sealing import SealError, ensure_mutable

    repo_id = request.dataset_repo_id
    try:
        ensure_mutable(repo_id, action="delete")
    except SealError as exc:
        return {"success": False, "message": str(exc)}
    root = Path(HF_LEROBOT_HOME).resolve()
    target = (root / repo_id).resolve()

    # Reject path traversal: target must stay strictly inside HF_LEROBOT_HOME.
    if target == root or root not in target.parents:
        return {"success": False, "message": "Invalid dataset path"}

    if not target.exists():
        return {"success": False, "message": f"Dataset not found on disk: {repo_id}"}

    try:
        shutil.rmtree(target)
    except Exception as e:
        logger.error(f"Failed to delete dataset {repo_id}: {e}")
        return {"success": False, "message": f"Failed to delete dataset: {e}"}

    if last_recording_info and last_recording_info.get("dataset_repo_id") == repo_id:
        last_recording_info = None

    logger.info(f"Deleted dataset directory {target}")
    return {"success": True, "message": f"Deleted {repo_id}"}


def handle_upload_dataset(request: UploadRequest) -> dict[str, Any]:
    """Handle dataset upload to HuggingFace Hub

    中文说明：数据管线末端——repair_local_dataset 修复 → 本地加载
    LeRobotDataset → push_to_hub（附加 lelab 标签与用户标签、按需私有）；
    对 401 类认证错误返回 hf auth login 的指引。
    """
    try:
        # Import LeRobotDataset to load and upload the dataset
        from lerobot.datasets import LeRobotDataset

        repair_local_dataset(request.dataset_repo_id)

        logger.info(f"Loading dataset {request.dataset_repo_id} for upload")

        # Load the dataset from local storage
        dataset = LeRobotDataset(request.dataset_repo_id)

        logger.info(f"Dataset loaded with {dataset.num_episodes} episodes")
        tags = with_lelab_tag(request.tags)
        logger.info(f"Uploading to HuggingFace Hub with tags: {tags}, private: {request.private}")

        # Upload dataset to HuggingFace Hub
        dataset.push_to_hub(tags=tags, private=request.private)

        logger.info(f"Dataset {request.dataset_repo_id} uploaded successfully to HuggingFace Hub")

        return {
            "success": True,
            "message": f"Dataset {request.dataset_repo_id} uploaded successfully to HuggingFace Hub",
            "dataset_url": f"https://huggingface.co/datasets/{request.dataset_repo_id}",
            "num_episodes": dataset.num_episodes,
        }

    except DatasetRepairError as e:
        logger.error(f"Cannot upload {request.dataset_repo_id}: {e}")
        return {"success": False, "message": str(e)}

    except Exception as e:
        logger.error(f"Error uploading dataset {request.dataset_repo_id}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")

        err_text = str(e).lower()
        looks_like_auth = any(
            m in err_text
            for m in ("401", "you must be authenticated", "authentication required", "huggingfacehub_token")
        )
        if looks_like_auth:
            return {
                "success": False,
                "message": "You're not logged into the Hugging Face Hub. Run `hf auth login` in your terminal, then retry.",
                "docs_url": "https://huggingface.co/docs/huggingface_hub/en/quick-start#authentication",
            }
        return {"success": False, "message": f"Failed to upload dataset: {str(e)}"}


def record_with_web_events(cfg: RecordConfig, web_events: dict) -> LeRobotDataset:
    """
    Implement recording with phase tracking - exactly mirrors original record() function behavior

    中文说明：LeLab 版录制主流程，镜像 LeRobot CLI 的 record()，但用 web_events
    字典替代键盘事件并增加 current_phase 阶段跟踪。整体步骤：
    1. 由配置实例化 robot/teleop，并生成默认的 action/observation 处理器；
    2. 用 hw_to_dataset_features 声明数据集特征（决定每帧写入哪些列）；
    3. resume 时 LeRobotDataset.resume 续录并做设备兼容性校验，否则
       LeRobotDataset.create 新建（含视频编码线程池/进程数等参数）；
    4. 按"总线 → 写标定 → 连相机 → configure"的顺序连接设备（不能直接用
       robot.connect()：它会在标定冲突时读 stdin，且最后 enable 力矩，破坏
       之后的标定寄存器写入）；
    5. 循环"采集 record_loop（带 dataset）→ save_episode → 重置 record_loop
       （不带 dataset，不落帧）"直至集数达标，期间响应跳过/重录/停止事件；
    6. finally 中先清 current_robot 停掉相机预览，再 dataset.finalize() 写
       元数据 footer（否则数据集在磁盘上不完整），最后 safe_disconnect_device
       释放硬件——嵌套保证 finalize 失败也能断开设备。
    """
    import time

    from lerobot.common.control_utils import (
        sanity_check_dataset_name,
        sanity_check_dataset_robot_compatibility,
    )
    from lerobot.datasets import LeRobotDataset
    from lerobot.processor import make_default_processors
    from lerobot.robots import make_robot_from_config
    from lerobot.scripts.lerobot_record import record_loop
    from lerobot.teleoperators import make_teleoperator_from_config
    from lerobot.utils.feature_utils import hw_to_dataset_features
    from lerobot.utils.utils import log_say

    global current_phase, phase_start_time, current_episode, saved_episodes, current_robot

    robot = make_robot_from_config(cfg.robot)
    teleop = make_teleoperator_from_config(cfg.teleop) if cfg.teleop is not None else None

    teleop_action_processor, robot_action_processor, robot_observation_processor = make_default_processors()

    # 数据集特征声明：action 为下发给从臂的目标关节角；observation 来自从臂观测，
    # 含 observation.state（各关节当前角度）与各相机的 observation.images.<名称>
    # （video=True 时图像在落盘阶段编码为 mp4，帧数据里只留视频路径引用）。
    action_features = hw_to_dataset_features(robot.action_features, "action", cfg.dataset.video)
    obs_features = hw_to_dataset_features(robot.observation_features, "observation", cfg.dataset.video)
    dataset_features = {**action_features, **obs_features}

    if cfg.resume:
        num_cameras = len(robot.cameras) if hasattr(robot, "cameras") else 0
        dataset = LeRobotDataset.resume(
            cfg.dataset.repo_id,
            root=cfg.dataset.root,
            batch_encoding_size=cfg.dataset.video_encoding_batch_size,
            rgb_encoder=cfg.dataset.rgb_encoder,
            depth_encoder=cfg.dataset.depth_encoder,
            streaming_encoding=cfg.dataset.streaming_encoding,
            encoder_queue_maxsize=cfg.dataset.encoder_queue_maxsize,
            encoder_threads=cfg.dataset.encoder_threads,
            image_writer_processes=cfg.dataset.num_image_writer_processes if num_cameras > 0 else 0,
            image_writer_threads=cfg.dataset.num_image_writer_threads_per_camera * num_cameras
            if num_cameras > 0
            else 0,
        )
        sanity_check_dataset_robot_compatibility(dataset, robot, cfg.dataset.fps, dataset_features)
    else:
        sanity_check_dataset_name(cfg.dataset.repo_id, None)
        dataset = LeRobotDataset.create(
            cfg.dataset.repo_id,
            cfg.dataset.fps,
            root=cfg.dataset.root,
            robot_type=robot.name,
            features=dataset_features,
            use_videos=cfg.dataset.video,
            image_writer_processes=cfg.dataset.num_image_writer_processes,
            image_writer_threads=cfg.dataset.num_image_writer_threads_per_camera * len(robot.cameras),
            batch_encoding_size=cfg.dataset.video_encoding_batch_size,
            rgb_encoder=cfg.dataset.rgb_encoder,
            depth_encoder=cfg.dataset.depth_encoder,
            streaming_encoding=cfg.dataset.streaming_encoding,
            encoder_queue_maxsize=cfg.dataset.encoder_queue_maxsize,
            encoder_threads=cfg.dataset.encoder_threads,
        )

    # Bring the devices up in the same order as teleoperation: buses, then
    # calibration, then cameras and motor configuration.
    #
    # `robot.connect()` can't be used here. It prompts on stdin when the motors
    # disagree with the calibration file (EOFError under the server), and it
    # ends with configure(), which leaves torque enabled — writing the
    # calibration registers afterwards corrupts the bus a few seconds into the
    # recording loop.
    # Start with episode 1 - but track it properly
    current_episode = 1
    saved_episodes = 0  # Track how many episodes we've actually saved

    # Setup runs inside the same try as the recording loop: a failure partway
    # through it still leaves buses and cameras open, and the teardown below is
    # what frees them for the next attempt (see issue #50).
    try:
        try:
            logger.info("🔧 ROBOT CONNECTION: Attempting to connect robot...")
            robot.bus.connect()
            logger.info("✅ ROBOT CONNECTION: Robot bus connected successfully")
        except Exception as e:
            logger.error(f"❌ ROBOT CONNECTION: Failed to connect robot: {e}")
            raise

        if teleop is not None:
            try:
                logger.info("🔧 TELEOP CONNECTION: Attempting to connect teleoperator...")
                teleop.bus.connect()
                logger.info("✅ TELEOP CONNECTION: Teleoperator bus connected successfully")
            except Exception as e:
                logger.error(f"❌ TELEOP CONNECTION: Failed to connect teleoperator: {e}")
                raise

        # Torque is still disabled at this point, which is what writing the
        # calibration registers requires.
        logger.info("Writing calibration to motors...")
        robot.bus.write_calibration(robot.calibration)
        if teleop is not None:
            teleop.bus.write_calibration(teleop.calibration)

        try:
            logger.info("🔧 CAMERA CONNECTION: Connecting cameras...")
            for cam in robot.cameras.values():
                cam.connect()
            logger.info("✅ CAMERA CONNECTION: Cameras connected successfully")
        except Exception as e:
            logger.error(f"❌ CAMERA CONNECTION: Failed to connect cameras: {e}")
            logger.error("💡 Make sure frontend camera streams are released before recording")
            raise

        logger.info("Configuring motors...")
        robot.configure()
        if teleop is not None:
            teleop.configure()
        logger.info("✅ Devices ready")

        # Expose the connected robot so /camera-feed can stream its cameras'
        # latest frames during the session (cleared in the finally below). Set
        # only after the cameras are connected so the feed never peeks a
        # half-open device.
        current_robot = robot

        while saved_episodes < cfg.dataset.num_episodes:
            # RECORDING PHASE - with dataset (matches original record.py exactly)
            current_phase = "recording"
            phase_start_time = time.time()
            logger.info(f"Starting recording phase for episode {current_episode}")
            logger.info(f"Events state at start of recording phase: {web_events}")
            print(
                f"🎬 STATUS CHANGE: Starting recording phase for episode {current_episode}/{cfg.dataset.num_episodes}"
            )

            log_say(f"Recording episode {current_episode}", cfg.play_sounds)

            # Add a tracking flag that won't be reset by record_loop
            web_events["_exit_early_triggered"] = False
            logger.info(f"Recording phase - calling record_loop with events: {web_events}")

            # 调 LeRobot 核心 record_loop 采集当前集：按 fps 周期执行
            # 主臂 get_action → 从臂 send_action → robot.get_observation →
            # dataset.add_frame（帧先进内存缓冲，save_episode 时才真正落盘）。
            record_loop(
                robot=robot,
                events=web_events,
                fps=cfg.dataset.fps,
                teleop_action_processor=teleop_action_processor,
                robot_action_processor=robot_action_processor,
                robot_observation_processor=robot_observation_processor,
                teleop=teleop,
                dataset=dataset,
                control_time_s=cfg.dataset.episode_time_s,
                single_task=cfg.dataset.single_task,
                display_data=cfg.display_data,
            )

            logger.info(f"Recording phase completed - events state: {web_events}")

            # Check if exit_early was triggered (use our tracking flag)
            recording_interrupted_by_exit_early = web_events.get("_exit_early_triggered", False)
            if recording_interrupted_by_exit_early:
                logger.info("🟡 RECORDING PHASE INTERRUPTED BY EXIT_EARLY - proceeding to save episode")
                print(
                    f"🟡 STATUS CHANGE: Recording phase interrupted by user - episode {current_episode} data collected"
                )
                # Reset our tracking flag
                web_events["_exit_early_triggered"] = False
            else:
                # Recording completed due to timeout - trigger re-record behavior
                logger.info("⏰ RECORDING PHASE COMPLETED DUE TO TIMEOUT - triggering re-record")
                print(
                    f"⏰ STATUS CHANGE: Recording timeout reached for episode {current_episode} - re-recording"
                )
                web_events["rerecord_episode"] = True

            # Handle rerecord logic first (before saving)
            if web_events["rerecord_episode"]:
                log_say("Re-record episode", cfg.play_sounds)
                print(
                    f"🔄 STATUS CHANGE: Re-recording episode {current_episode} (episode number stays the same)"
                )
                web_events["rerecord_episode"] = False
                web_events["exit_early"] = False
                # 丢弃当前集未保存的帧缓冲，集数不递增，随后重录同一集
                dataset.clear_episode_buffer()

                # Go through reset phase before re-recording (don't increment episode counters)
                # RESET PHASE - without dataset (matches original record.py exactly)
                current_phase = "resetting"
                phase_start_time = time.time()
                logger.info(f"Starting reset phase for re-record of episode {current_episode}")
                logger.info(f"Events state at start of reset phase: {web_events}")
                print(f"🔄 STATUS CHANGE: Starting reset phase for episode {current_episode}")

                log_say("Reset the environment", cfg.play_sounds)

                # Reset exit_early flag at the start of each phase
                web_events["exit_early"] = False
                logger.info(f"Reset phase - calling record_loop with events: {web_events}")

                record_loop(
                    robot=robot,
                    events=web_events,
                    fps=cfg.dataset.fps,
                    teleop_action_processor=teleop_action_processor,
                    robot_action_processor=robot_action_processor,
                    robot_observation_processor=robot_observation_processor,
                    teleop=teleop,
                    # NOTE: NO dataset parameter here - matches LeRobot CLI exactly
                    # This means NO recording happens during reset phase
                    control_time_s=cfg.dataset.reset_time_s,
                    single_task=cfg.dataset.single_task,
                    display_data=cfg.display_data,
                )

                logger.info(f"Reset phase completed - events state: {web_events}")

                # Check if reset was interrupted by exit_early
                if web_events["exit_early"]:
                    logger.info("🟡 RESET PHASE INTERRUPTED BY EXIT_EARLY during re-record")
                    print("🟡 STATUS CHANGE: Reset phase interrupted by user during re-record")
                    web_events["exit_early"] = False

                # Check if stop recording was requested during re-record reset phase
                if web_events["stop_recording"]:
                    logger.info("🛑 STOP RECORDING requested during re-record reset phase - ending session")
                    print(
                        "🛑 STATUS CHANGE: Stop recording requested during re-record reset - ending session"
                    )
                    break

                # Don't increment current_episode or saved_episodes - we're re-recording the same episode
                continue

            # Save episode immediately after recording phase (matches expected flow)
            # 一集落盘：save_episode 把该集内存缓冲写成 parquet + mp4，
            # 并在 meta 中登记该 episode 的起止帧索引
            logger.info(f"💾 Saving episode {current_episode}...")
            print(f"💾 STATUS CHANGE: Saving episode {current_episode}")
            dataset.save_episode()
            logger.info(f"✅ Episode {current_episode} saved successfully")
            print(f"✅ STATUS CHANGE: Episode {current_episode} saved successfully")

            # Increment episode counters after successful save
            saved_episodes += 1
            current_episode += 1

            # Check if we should stop recording
            if web_events["stop_recording"]:
                print("🛑 STATUS CHANGE: Recording manually stopped by user")
                break

            # Check if we've completed all episodes
            if saved_episodes >= cfg.dataset.num_episodes:
                break

            # Execute reset phase to prepare for next episode
            # Skip reset for the last episode that was just saved
            if saved_episodes < cfg.dataset.num_episodes:
                # RESET PHASE - without dataset (matches original record.py exactly)
                current_phase = "resetting"
                phase_start_time = time.time()
                logger.info(f"Starting reset phase for next episode {current_episode}")
                logger.info(f"Events state at start of reset phase: {web_events}")
                print(f"🔄 STATUS CHANGE: Starting reset phase for episode {current_episode}")

                log_say("Reset the environment", cfg.play_sounds)

                # Reset exit_early flag at the start of each phase
                web_events["exit_early"] = False
                logger.info(f"Reset phase - calling record_loop with events: {web_events}")

                record_loop(
                    robot=robot,
                    events=web_events,
                    fps=cfg.dataset.fps,
                    teleop_action_processor=teleop_action_processor,
                    robot_action_processor=robot_action_processor,
                    robot_observation_processor=robot_observation_processor,
                    teleop=teleop,
                    # NOTE: NO dataset parameter here - matches LeRobot CLI exactly
                    # This means NO recording happens during reset phase
                    control_time_s=cfg.dataset.reset_time_s,
                    single_task=cfg.dataset.single_task,
                    display_data=cfg.display_data,
                )

                logger.info(f"Reset phase completed - events state: {web_events}")

                # Check if reset was interrupted by exit_early
                if web_events["exit_early"]:
                    logger.info("🟡 RESET PHASE INTERRUPTED BY EXIT_EARLY - proceeding to next episode")
                    print("🟡 STATUS CHANGE: Reset phase interrupted by user - proceeding to next episode")
                    web_events["exit_early"] = False

                # Check if stop recording was requested during reset phase
                if web_events["stop_recording"]:
                    logger.info("🛑 STOP RECORDING requested during reset phase - ending session")
                    print("🛑 STATUS CHANGE: Stop recording requested during reset - ending session")
                    break

        # Recording completed
        current_phase = "completed"
        phase_start_time = None
        print("🏁 STATUS CHANGE: Recording session completed - all episodes finished")
        log_say("Stop recording", cfg.play_sounds, blocking=True)

    finally:
        # Stop the camera-feed endpoint from reading before tearing the cameras
        # down, so it can't peek a half-disconnected device.
        current_robot = None
        try:
            # Writes the parquet footers for meta/episodes/. Without it the
            # dataset is invalid on disk, so reopening it (upload,
            # /dataset-info) misses meta/episodes and falls back to downloading
            # from the Hub — which 404s for a dataset that was never pushed.
            # 收尾落盘：finalize 写出元数据 footer，数据集才算完整有效
            dataset.finalize()
        finally:
            # safe_disconnect_device force-releases the serial port / cameras if
            # a normal disconnect fails, so a flaky teardown can't leave the
            # device busy and block the next recording session (see issue #50).
            # Nested so a failed finalize can't strand the hardware.
            safe_disconnect_device(robot, logger, context="recording cleanup")
            if teleop:
                safe_disconnect_device(teleop, logger, context="recording cleanup")

    if cfg.dataset.push_to_hub:
        if dataset.num_episodes > 0:
            dataset.push_to_hub(tags=cfg.dataset.tags, private=cfg.dataset.private)
        else:
            logger.warning("No episodes saved — skipping push to hub")

    log_say("Exiting", cfg.play_sounds)
    return dataset
