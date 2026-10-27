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

# ============================================================================
# LeLab 后端主入口（FastAPI + 静态前端）
#
# 职责：把各功能模块（录制 / 遥操作 / 标定 / 推理 / 训练作业 / 数据集管理）的
# handle_* 函数包装成统一的 HTTP + WebSocket 接口，并托管构建好的 React 前端，
# 使单个 lelab 进程在 :8000 上同时提供 API 和界面。
#
# 三条贯穿全文的主线：
#   1. 硬件租约（HARDWARE LEASE）：串口总线同一时刻只能被 标定/遥操作/录制/推理
#      中的一个任务占用。所有 start 路由先取租约，stop 路由、看门狗超时、
#      紧急停止负责释放。
#   2. WebSocket 广播：ConnectionManager 用“后台线程 + 队列”把关节数据推给前端；
#      系统状态、存储状态各有一条独立的定时推送通道。
#   3. 数据安全护栏：删除 / 同步 / 封存数据集前，统一检查它是否正在录制、
#      正在训练、正在传输或已被封存。
#
# 阅读顺序建议：ConnectionManager → HARDWARE LEASE → 各功能路由 → 启动/关闭钩子
# → 末尾的 SPA 静态文件挂载。
# ============================================================================

# ------------------------------- 标准库 -------------------------------
import asyncio
import contextlib
import glob
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Literal

# --------------------- 第三方：FastAPI / Starlette / Pydantic ---------------------
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

# ------------------------------ 本项目模块 ------------------------------
# 数据集浏览（Hub + 本地缓存列表）与录制模块（顺带借它读 recording_active 和相机流）
# Import our custom recording functionality
from . import datasets as dataset_browser, record as _record

# 标定：CalibrationManager 单例 + 标定请求模型
# Import our custom calibration functionality
from .calibrate import CalibrationRequest, calibration_manager
from .dataset_storage import (
    dataset_detail,
    dataset_disk_capacity,
    dataset_integrity,
    dataset_summary,
    list_trash,
    restore_dataset,
    storage_summary,
    trash_dataset,
)
from .hardware_lease import (
    HARDWARE_TASKS,
    HardwareLease,
    HardwareLeaseError,
    get_hardware_lease_manager,
    lease_required_from_env,
)
from .jobs import (
    JobAlreadyRunningError,
    JobNotFoundError,
    JobNotRunningError,
    JobTarget,
    job_registry,
)
from .record import (
    DatasetInfoRequest,
    RecordingRequest,
    UploadRequest,
    handle_delete_dataset,
    handle_exit_early,
    handle_get_dataset_info,
    handle_recording_status,
    handle_rerecord_episode,
    handle_start_recording,
    handle_stop_recording,
    handle_upload_dataset,
)
from .remote_inference import (
    handle_remote_inference_status,
    handle_remote_policy_server_status,
    handle_start_remote_inference,
    handle_stop_remote_inference,
    handle_stop_remote_policy_server,
    remote_inference_active,
    remote_status_owns_route,
)
from .remote_sync import (
    ACTIVE_SYNC_PHASES,
    DatasetSyncError,
    TrainServerConfig,
    get_dataset_sync_manager,
    ssh_base_command,
)
from .rollout import (
    InferenceRequest,
    handle_inference_status,
    handle_start_inference,
    handle_stop_inference,
)
from .sealing import (
    SealError,
    get_seal_manager,
    seal_status,
    unseal_dataset,
    verify_seal,
)
from .system_monitor import system_snapshot

# 遥操作：主臂→从臂的实时跟随
# Import our custom teleoperation functionality
from .teleoperate import (
    TeleoperateRequest,
    handle_get_joint_positions,
    handle_start_teleoperation,
    handle_stop_teleoperation,
    handle_teleoperation_status,
)

# 训练改为“作业（job）”模型，具体实现见 lelab/jobs.py，这里只引入请求模型。
# Training is now job-based; see app/jobs.py.
from .train import TrainingRequest
from .train_server import train_server_status
from .update import handle_run_update, handle_update_check
from .utils import config
from .utils.config import (
    FOLLOWER_CONFIG_PATH,
    LEADER_CONFIG_PATH,
    delete_robot_record,
    detect_port_after_disconnect,
    find_available_ports,
    find_robot_port,
    get_default_robot_port,
    get_robot_record,
    get_saved_robot_port,
    is_valid_robot_name,
    list_robot_records,
    robot_record_diagnostics,
    save_robot_port,
    save_robot_record,
)
from .utils.hf_auth import (
    cached_whoami,
    handle_hf_auth_status,
    handle_hf_login,
    handle_hf_logout,
    shared_hf_api,
)
from .utils.system import (
    handle_get_cuda_status,
    handle_get_policy_extra,
    handle_get_training_extra,
    handle_get_wandb_extra,
    handle_install_policy_extra,
    handle_install_policy_extra_status,
    handle_install_training_extra,
    handle_install_training_extra_status,
    handle_install_wandb_extra,
    handle_install_wandb_extra_status,
    warn_if_cuda_mismatch,
)

# 日志配置：uvicorn 之外单独初始化，级别 INFO，便于在终端观察启动/租约/相机等事件。
# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# 一路“服务端 MJPEG 相机预览”的取消握手对象。
#   stop_requested：外部（录制/推理启动前、关服时）置位，要求预览尽快停下；
#   released      ：由预览生成器在真正 release() 掉 OpenCV/V4L2 句柄后置位。
# 只有 released 被置位，才能确认设备已空闲、可以被独占任务（录制/推理）接手。
class _CameraPreviewSession:
    """Cancellation handshake for one server-side MJPEG camera preview."""

    def __init__(self) -> None:
        self.stop_requested = threading.Event()
        self.released = threading.Event()


# 当前活跃的预览会话集合；跨线程（事件循环线程 + 请求线程 + 关服线程）访问，故加锁。
_camera_preview_sessions_lock = threading.Lock()
_camera_preview_sessions: set[_CameraPreviewSession] = set()


# 登记一路新预览，返回其会话对象，供生成器在 finally 里释放。
def _register_camera_preview() -> _CameraPreviewSession:
    session = _CameraPreviewSession()
    with _camera_preview_sessions_lock:
        _camera_preview_sessions.add(session)
    return session


# 预览结束：先置 released（唤醒等待者：设备已松手），再从集合中移除。
def _finish_camera_preview(session: _CameraPreviewSession) -> None:
    session.released.set()
    with _camera_preview_sessions_lock:
        _camera_preview_sessions.discard(session)


# 停止所有活跃预览，并等待它们的 V4L2/OpenCV 句柄真正关闭。
# 录制与推理启动前必须调用：浏览器关掉 <img> 并不代表服务端生成器已经释放设备，
# 否则会报 "Failed to open OpenCVCamera(n)"。
# 返回 remaining > 0 表示设备仍被占用，调用方应拒绝启动。
def _release_camera_previews(timeout_s: float = 5.0) -> dict[str, int]:
    """Stop active previews and wait for their V4L2 handles to close."""
    with _camera_preview_sessions_lock:
        sessions = tuple(_camera_preview_sessions)

    for session in sessions:
        session.stop_requested.set()

    deadline = time.monotonic() + max(timeout_s, 0.0)
    for session in sessions:
        session.released.wait(max(0.0, deadline - time.monotonic()))

    released = sum(session.released.is_set() for session in sessions)
    remaining = len(sessions) - released
    if sessions:
        logger.info(
            "Camera preview release before exclusive device use: requested=%d released=%d remaining=%d",
            len(sessions),
            released,
            remaining,
        )
    return {"requested": len(sessions), "released": released, "remaining": remaining}


# POST /jobs/training 的请求体包装：新形状是 {config: ..., target: ...}，target 可选。
class StartTrainingBody(BaseModel):
    """Wrapping body for POST /jobs/training. Adds optional target spec."""

    config: TrainingRequest
    target: JobTarget | None = None

    @classmethod
    # 兼容旧形状：训练字段直接平铺在顶层、且没有 target 的历史请求。
    def from_legacy(cls, raw: dict) -> "StartTrainingBody":
        """Accept the old request shape (TrainingRequest fields at top level)
        as well as the new shape ({config: ..., target: ...}).
        """
        if "config" in raw and isinstance(raw["config"], dict):
            return cls.model_validate(raw)
        # Legacy: top-level training fields, no target.
        return cls(config=TrainingRequest.model_validate(raw))


# HF Jobs 硬件规格（flavors）的进程内缓存，TTL 5 分钟，
# 避免每次打开“训练目标”面板都去打一次 Hub API。
# Cache for HF Jobs hardware flavors (5-minute TTL)
_flavors_cache: dict = {"data": None, "fetched_at": 0.0}
_FLAVOR_CACHE_TTL_SECONDS = 300.0


# 全局唯一的 FastAPI 应用实例，下文所有路由都挂在它上面。
app = FastAPI()

# In dev mode the React app runs on :8080 while the API runs on :8000; in
# prod they share an origin and CORS is unnecessary. allow_credentials with
# a wildcard origin is rejected by browsers, so we drop it.
# 开发模式下 React 跑在 :8080、API 在 :8000，需要跨域；生产环境同源，CORS 用不上。
# 注意：allow_credentials=True 不能与 allow_origins=["*"] 同时使用（浏览器会拒绝），故置 False。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 已构建的前端产物目录（随 wheel 打包），文件末尾会把它挂载到 /。
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

# Get the path to the lerobot root directory (3 levels up from this script)
# lerobot 检出的上级目录：本文件位于 <该目录>/lelab/lelab/server.py，上溯 4 层即得。
# 目前只用于启动时打印一行诊断信息（lerobot 通过 pyproject 的 editable 路径依赖引入）。
LEROBOT_PATH = str(Path(__file__).parent.parent.parent.parent)
logger.info(f"LeRobot path: {LEROBOT_PATH}")


# WebSocket 广播中心（全局单例 manager）。
# 工作方式：任意线程调用 broadcast_joint_data_sync() 把数据丢进队列，
# 后台线程 _broadcast_worker 在自己的事件循环里把数据发给所有 /ws/joint-data 客户端。
# 这样机器人读取线程无需 await，也不会阻塞 uvicorn 的主事件循环。
class ConnectionManager:
    # 活跃连接列表 + 广播队列 + 后台线程状态；
    # _connections_lock 保护列表（广播线程在发送失败时也会改动它）。
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.broadcast_queue = queue.Queue()
        self.broadcast_thread = None
        self.is_running = False
        # Guards `active_connections` since the broadcast worker thread also
        # mutates it on send failure.
        self._connections_lock = threading.Lock()

    # 新客户端接入：accept → 登记到列表 → 若广播线程尚未启动则启动它。
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        with self._connections_lock:
            self.active_connections.append(websocket)
            count = len(self.active_connections)
        logger.info(f"WebSocket connected. Total connections: {count}")

        if not self.is_running:
            self.start_broadcast_thread()

    # 客户端断开：移出列表；最后一个连接离开时停掉广播线程，避免空转。
    def disconnect(self, websocket: WebSocket):
        with self._connections_lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
                count = len(self.active_connections)
                logger.info(f"WebSocket disconnected. Total connections: {count}")
            else:
                count = len(self.active_connections)

        if count == 0 and self.is_running:
            self.stop_broadcast_thread()

    # 启动后台广播线程（幂等：已在运行则直接返回）。
    def start_broadcast_thread(self):
        """Start the background thread for broadcasting data"""
        if self.is_running:
            return

        self.is_running = True
        self.broadcast_thread = threading.Thread(target=self._broadcast_worker, daemon=True)
        self.broadcast_thread.start()
        logger.info("📡 Broadcast thread started")

    # 停止后台广播线程（最多等 1 秒，避免关服被卡住）。
    def stop_broadcast_thread(self):
        """Stop the background thread"""
        self.is_running = False
        if self.broadcast_thread:
            self.broadcast_thread.join(timeout=1.0)
            logger.info("📡 Broadcast thread stopped")

    # 广播线程主体：为线程单独创建并绑定一个事件循环，
    # 然后循环从队列取数据（0.1 秒超时，便于检查 is_running）发往所有连接。
    def _broadcast_worker(self):
        """Background worker thread for broadcasting WebSocket data"""
        import asyncio

        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            while self.is_running:
                try:
                    # Get data from queue with timeout
                    data = self.broadcast_queue.get(timeout=0.1)
                    if data is None:  # Poison pill to stop
                        break

                    # Broadcast to all connections
                    if self.active_connections:
                        loop.run_until_complete(self._send_to_all_connections(data))

                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"Error in broadcast worker: {e}")

        finally:
            loop.close()

    # 实际发送：先复制一份连接列表再遍历（避免迭代中被修改）；
    # 发送失败的连接会在此处被断开并移除。
    async def _send_to_all_connections(self, data: dict[str, Any]):
        """Send data to all active WebSocket connections"""
        with self._connections_lock:
            connections = list(self.active_connections)
        if not connections:
            return

        disconnected = []
        for connection in connections:
            try:
                await connection.send_json(data)
            except Exception as e:
                logger.error(f"Error sending data to WebSocket: {e}")
                disconnected.append(connection)

        for connection in disconnected:
            self.disconnect(connection)

    # 线程安全的入队接口：供各功能模块的工作线程调用（切勿在这些线程里 await）。
    # 无连接或未运行时直接丢弃，队列满也只告警，绝不阻塞控制循环。
    def broadcast_joint_data_sync(self, data: dict[str, Any]):
        """Thread-safe method to queue data for broadcasting"""
        if self.is_running and self.active_connections:
            try:
                self.broadcast_queue.put_nowait(data)
            except queue.Full:
                logger.warning("Broadcast queue is full, dropping data")

    # 通知前端“作业列表变了”（提交 / 结束 / 删除），让 React Query 重新拉取；
    # 没有客户端连接时静默跳过 —— 前端挂载时本来就会拉一次，漏推可自愈。
    def notify_jobs_changed(self) -> None:
        """Push a 'jobs_changed' event to all WS clients so they refetch.

        Called from JobRegistry on submit / watchdog finalisation / delete.
        Skipped silently if no clients are connected — the frontend does an
        initial fetch on mount, so a missed broadcast is self-healing.
        """
        if self.is_running and self.active_connections:
            with contextlib.suppress(queue.Full):
                self.broadcast_queue.put_nowait({"type": "jobs_changed", "timestamp": time.time()})

    # 由 JobRegistry 看门狗以约 1Hz 推送运行中作业的进度快照，
    # 仪表盘进度条因此无需轮询 /jobs（更不必轮询每次都要打 HF API 的 /jobs/hub）。
    def notify_job_progress(self, snapshots: list[dict]) -> None:
        """Push a 'job_progress' event with per-running-job snapshots.

        Fired from the JobRegistry watchdog (~1Hz) while jobs are running so
        the dashboard's progress bar updates live without refetching /jobs
        (let alone /jobs/hub, which hits the HF API on every call).
        """
        if self.is_running and self.active_connections:
            with contextlib.suppress(queue.Full):
                self.broadcast_queue.put_nowait(
                    {"type": "job_progress", "jobs": snapshots, "timestamp": time.time()}
                )


# 全局广播管理器：功能模块通过参数拿到它（如 handle_start_teleoperation(request, manager)），
# 作业注册表则通过回调挂到它上面。
manager = ConnectionManager()
# 作业状态变化 / 进度更新 → 主动推给前端，而不是等前端轮询。
job_registry.set_on_change(manager.notify_jobs_changed)
job_registry.set_on_progress(manager.notify_job_progress)


# ============================================================================
# HARDWARE LEASE
#
# Exactly one of calibration / teleoperation / recording / inference may own
# the serial bus. Every start endpoint below takes the lease first; stop
# endpoints release it, the watchdog releases it when the task ends by itself,
# and `/emergency-stop` pre-empts it unconditionally.
# ============================================================================

# ----------------------------------------------------------------------------
# 硬件租约（HARDWARE LEASE）
#
# 标定 / 遥操作 / 录制 / 推理 四者中，同一时刻只允许一个占用串口总线：
#   - 下面每个 start 路由都先 acquire，拿不到就返回 409（响应里带上当前持有者）；
#   - 每个 stop 路由显式 release；
#   - 看门狗（startup 时启动）在任务自然结束或 TTL 过期时兜底释放；
#   - /emergency-stop 无条件抢占并停掉全部任务。
# ----------------------------------------------------------------------------
hardware_lease = get_hardware_lease_manager()


# 供租约看门狗回调：判断某任务对应的功能模块此刻是否真的还在跑。
# 这样即使浏览器标签页被关掉（不再发心跳），租约也不会被永久占住。
def _hardware_task_active(task: str) -> bool:
    """Report whether the module that owns `task` is still running."""
    if task == "recording":
        return bool(_record.recording_active)
    if task == "teleoperation":
        from . import teleoperate as _teleoperate

        return bool(_teleoperate.teleoperation_active)
    if task == "inference":
        from . import rollout as _rollout

        return bool(_rollout.inference_active)
    if task == "calibration":
        return bool(calibration_manager.get_status().calibration_active)
    return False


# 统一的任务停止入口：按任务名分发到各模块的 stop 函数。
# 单个任务失败不影响其它任务，且本函数自身绝不抛异常。
def _hardware_stop_task(task: str) -> dict[str, Any]:
    """Run the stop path for one task; never raises."""
    try:
        if task == "recording":
            return handle_stop_recording()
        if task == "teleoperation":
            return handle_stop_teleoperation()
        if task == "inference":
            return handle_stop_inference()
        if task == "calibration":
            return calibration_manager.stop_calibration_process()
    except Exception as exc:  # a failing task must not block the others
        logger.exception("Failed to stop %s", task)
        return {"success": False, "message": str(exc)}
    return {"success": False, "message": f"Unknown hardware task {task!r}"}


# 看门狗在租约失效（TTL 到期 / 任务已结束）时的回调：先记警告，再确保任务真的停下（fail-safe）。
def _hardware_stop_hook(task: str, reason: str) -> None:
    """Called by the lease watchdog when a lease expires (fail safe)."""
    logger.warning("Hardware lease for %s lost (%s); stopping the task", task, reason)
    _hardware_stop_task(task)


# 解析“谁在申请租约”：X-LeLab-Client 是浏览器/设备的稳定 id，缺失时退回来源 IP。
# X-LeLab-Role: observer 表示只读会话 —— 它只是标签，真正拒绝控制权的是租约管理器本身。
def _client_identity(request: Request) -> tuple[str, str]:
    """Owner label and role for a lease request.

    Clients may send `X-LeLab-Client` (a stable browser/device id) and
    `X-LeLab-Role: observer` for read-only sessions. Header values are only
    used as labels — they never grant control by themselves, because an
    observer role is refused outright by the lease manager.
    """
    owner = (request.headers.get("x-lelab-client") or "").strip()
    if not owner and request.client is not None:
        owner = request.client.host or ""
    role = (request.headers.get("x-lelab-role") or "operator").strip().lower()
    return owner or "unknown-client", role or "operator"


# 为 task 申请租约：环境变量关闭租约时返回 None；已被占用则抛 HTTPException(409) 并附上持有者信息。
def _acquire_hardware_lease(request: Request, task: str) -> HardwareLease | None:
    """Take the lease for `task`, or raise the matching HTTPException."""
    if not lease_required_from_env():
        return None
    owner, role = _client_identity(request)
    try:
        return hardware_lease.acquire(task, owner=owner, role=role)
    except HardwareLeaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": str(exc), "holder": exc.holder, "task": task},
        ) from exc


# start 处理函数返回 success=False（例如设备打不开）时，把刚拿到的租约立刻还回去。
def _release_lease_if_failed(lease: HardwareLease | None, result: Any) -> None:
    """Give the lease back when a start handler refused to start."""
    if lease is None:
        return
    failed = isinstance(result, dict) and result.get("success") is False
    if failed:
        hardware_lease.release(token=lease.token, reason="start-failed")


# 显式申请租约的请求体：支持 force 抢占、ttl_seconds 自定义有效期、role 区分操作者/观察者。
class HardwareLeaseRequest(BaseModel):
    task: str
    owner: str | None = None
    note: str | None = None
    ttl_seconds: float | None = None
    role: str = "operator"
    force: bool = False


# 续租（心跳）请求体：可用 token，也可用 task + X-LeLab-Client 头，这样浏览器无需持有 token。
class HardwareLeaseRenewRequest(BaseModel):
    token: str | None = None
    task: str | None = None
    ttl_seconds: float | None = None


# 释放租约请求体：按 token 释放，或按 task 强制释放（运维 / 接管场景）。
class HardwareLeaseReleaseRequest(BaseModel):
    token: str | None = None
    task: str | None = None
    owner: str | None = None
    reason: str = "released"
    force: bool = False


# 紧急停止请求体：只带一个原因字符串，用于日志与状态展示。
class EmergencyStopRequest(BaseModel):
    reason: str = "emergency-stop"


# 查询当前租约：持有者、剩余 TTL、最近一次释放原因。
@app.get("/hardware/lease")
def get_hardware_lease():
    """Current lease holder, remaining TTL and the last release reason."""
    return hardware_lease.status()


# 显式抢占 / 申请硬件（运维页操作、Windows 端接管时使用）。
@app.post("/hardware/lease/acquire")
def acquire_hardware_lease(body: HardwareLeaseRequest):
    """Explicitly claim the hardware (used by operators and Windows takeover)."""
    try:
        lease = hardware_lease.acquire(
            body.task,
            owner=body.owner or "api-client",
            note=body.note,
            ttl_seconds=body.ttl_seconds,
            role=body.role,
            force=body.force,
        )
    except HardwareLeaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": str(exc), "holder": exc.holder},
        ) from exc
    return {"success": True, "lease": lease.to_dict(include_token=True)}


# 心跳续租：维持当前会话对硬件的所有权，避免 TTL 到期被看门狗释放。
@app.post("/hardware/lease/renew")
def renew_hardware_lease(body: HardwareLeaseRenewRequest, http_request: Request):
    """Heartbeat: keep an active session's lease alive.

    Accepts the secret token, or the owner identity from the `X-LeLab-Client`
    header plus the task — so the browser can heartbeat without ever seeing
    the token.
    """
    owner, _role = _client_identity(http_request)
    try:
        lease = hardware_lease.renew(
            body.token,
            task=body.task,
            owner=owner,
            ttl_seconds=body.ttl_seconds,
        )
    except HardwareLeaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": str(exc), "holder": exc.holder},
        ) from exc
    return {"success": True, "lease": lease.to_dict()}


# 释放租约：按 token 或按 task（清理、接管）。
@app.post("/hardware/lease/release")
def release_hardware_lease(body: HardwareLeaseReleaseRequest):
    """Release the lease by token, or by task (operator takeover/cleanup)."""
    released = hardware_lease.release(
        token=body.token,
        task=body.task,
        owner=body.owner,
        reason=body.reason,
        force=body.force,
    )
    return {"success": released, "lease": hardware_lease.status()}


# 最高优先级停止：先无条件抢掉租约（防止停止过程中被新的启动请求抢走硬件），
# 再依次停止所有硬件任务，最后回传租约状态与各任务的停止结果。
# 请求体可省略，省略时按默认原因 "emergency-stop" 处理。
@app.post("/emergency-stop")
def emergency_stop(body: EmergencyStopRequest | None = None):
    """Highest-priority stop: pre-empt the lease and stop every task."""
    reason = (body.reason if body is not None else None) or "emergency-stop"
    preempted = hardware_lease.preempt(reason=reason)
    stopped = {task: _hardware_stop_task(task) for task in HARDWARE_TASKS}
    return {
        "success": True,
        "reason": reason,
        "preempted": preempted.to_dict() if preempted else None,
        "stopped": stopped,
        "lease": hardware_lease.status(),
    }


# 列出标定配置目录下的全部 JSON 配置（leader=主臂 / follower=从臂）。
@app.get("/get-configs")
def get_configs():
    # Get all available calibration configs
    leader_configs = [os.path.basename(f) for f in glob.glob(os.path.join(LEADER_CONFIG_PATH, "*.json"))]
    follower_configs = [os.path.basename(f) for f in glob.glob(os.path.join(FOLLOWER_CONFIG_PATH, "*.json"))]

    return {"leader_configs": leader_configs, "follower_configs": follower_configs}


# 启动遥操作（需先取串口租约；启动失败立即归还租约）。
@app.post("/move-arm")
def teleoperate_arm(request: TeleoperateRequest, http_request: Request):
    """Start teleoperation of the robot arm"""
    lease = _acquire_hardware_lease(http_request, "teleoperation")
    result = handle_start_teleoperation(request, manager)
    _release_lease_if_failed(lease, result)
    return result


# 停止遥操作并释放租约。
@app.post("/stop-teleoperation")
def stop_teleoperation():
    """Stop the current teleoperation session"""
    result = handle_stop_teleoperation()
    hardware_lease.release(task="teleoperation")
    return result


# 查询遥操作当前状态。
@app.get("/teleoperation-status")
def teleoperation_status():
    """Get the current teleoperation status"""
    return handle_teleoperation_status()


# 读取机器人当前关节角度（前端仪表显示用）。
@app.get("/joint-positions")
def get_joint_positions():
    """Get current robot joint positions"""
    return handle_get_joint_positions()


# 启动推理（本地 rollout 或远程 policy server）。
# 关键前置动作：先释放服务端相机预览 —— 预览持有独占的 OpenCV/V4L2 句柄，
# 不释放会让 robot_client 报 "Failed to open OpenCVCamera(n)"；
# 与录制路径的处理方式保持一致。之后再取硬件租约并分流到对应处理函数。
@app.post("/start-inference")
def start_inference(request: InferenceRequest, http_request: Request):
    # Camera previews hold an exclusive OpenCV/V4L2 handle. The browser drops
    # its streams when the modal flips to "submitting", but the server-side
    # MJPEG generator (and any other open tab) keeps the device busy — which
    # makes the rollout/robot_client fail with "Failed to open OpenCVCamera(n)".
    # Release them first, exactly like the recording path does.
    preview_release = _release_camera_previews()
    if preview_release["remaining"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "推理启动被阻止：仍有相机预览未释放设备，请关闭其它预览页面后重试。"
                f"(requested={preview_release['requested']}, remaining={preview_release['remaining']})"
            ),
        )
    lease = _acquire_hardware_lease(http_request, "inference")
    if request.target == "remote":
        result = handle_start_remote_inference(request)
    else:
        result = handle_start_inference(request)
    _release_lease_if_failed(lease, result)
    if not result.get("success"):
        raise HTTPException(
            status_code=result.get("status_code", 500),
            detail=result.get("message", "Failed to start inference"),
        )
    return result


# 停止推理：按“远程是否活跃”自动分流，随后释放租约。
@app.post("/stop-inference")
def stop_inference():
    result = handle_stop_remote_inference() if remote_inference_active() else handle_stop_inference()
    hardware_lease.release(task="inference")
    if not result.get("success"):
        raise HTTPException(
            status_code=result.get("status_code", 500),
            detail=result.get("message", "Failed to stop inference"),
        )
    return result


# 推理状态：远程推理进程存在时优先返回它的状态，否则返回本地 rollout 状态。
@app.get("/inference-status")
def inference_status():
    if remote_status_owns_route():
        return handle_remote_inference_status()
    return handle_inference_status()


# 查询训练机上由 LeLab 启动的 PolicyServer 是否仍然存活。
@app.get("/inference/remote/server-status")
def remote_policy_server_status():
    """Whether the train-side PolicyServer started by LeLab is still alive."""
    return handle_remote_policy_server_status()


# 停止训练机上的 PolicyServer。
@app.post("/inference/remote/server/stop")
def remote_policy_server_stop():
    result = handle_stop_remote_policy_server()
    if not result.get("success"):
        raise HTTPException(
            status_code=result.get("status_code", 500),
            detail=result.get("message", "Failed to stop the remote PolicyServer"),
        )
    return result


# 健康检查：用于探活与部署自检。
@app.get("/health")
def health_check():
    """Simple health check endpoint to verify server is running"""
    return {"status": "ok", "message": "FastAPI server is running"}


# 查询本机 HF CLI 是否已登录，并返回用户信息。
@app.get("/hf-auth-status")
def hf_auth_status():
    """Check whether the local HF CLI is authenticated and return user info."""
    return handle_hf_auth_status()


# 手动粘贴 token 登录的请求体。
class HfLoginBody(BaseModel):
    token: str


# 保存粘贴的 HF token（先经 whoami 校验，无效返回 401）。
@app.post("/hf-auth/login")
def hf_auth_login(body: HfLoginBody):
    """Persist a pasted HF token (validated against whoami) for this user."""
    try:
        return handle_hf_login(body.token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


# 清除已保存的 HF token（磁盘上其它登录信息保持不动）。
@app.post("/hf-auth/logout")
def hf_auth_logout():
    """Forget the stored HF token (kept on disk otherwise)."""
    try:
        return handle_hf_logout()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# 数据集总览：Hub 上的 + 本地缓存的，每条带 source 字段（local / hub / both）。
@app.get("/datasets")
def datasets_list():
    """List datasets available to the user — Hub-owned + local cache.

    Each entry carries a `source` field: "local", "hub", or "both".
    """
    return dataset_browser.list_all_datasets()


# 汇总某个数据集的同步状态：
#   正在传输 → syncing（附进度、已传字节、速率、ETA）；否则取最近一次历史结果
#   （completed→synced / failed→failed / cancelled→cancelled），都没有则 not-synced。
def _dataset_sync_state(
    repo_id: str | None,
    *,
    active: dict[str, Any],
    manager: Any,
) -> dict[str, Any]:
    """Merge the live transfer status with the last known result."""
    if not repo_id:
        return {"state": "unknown"}
    if active.get("repo_id") == repo_id and active.get("phase") in ACTIVE_SYNC_PHASES:
        return {
            "state": "syncing",
            "phase": active.get("phase"),
            "progress": active.get("progress"),
            "transferred": active.get("transferred"),
            "total": active.get("total"),
            "rate_bytes_per_second": active.get("rate_bytes_per_second"),
            "eta_seconds": active.get("eta_seconds"),
        }
    last = manager.history(repo_id)
    if isinstance(last, dict) and last:
        phase = last.get("phase")
        state = {
            "completed": "synced",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(phase, "unknown")
        return {
            "state": state,
            "phase": phase,
            "synced_at": last.get("finished"),
            "remote_path": last.get("remote_path"),
            "manifest_id": last.get("id"),
            "error": last.get("error"),
        }
    # A dry run leaves its own record in the global status without starting a
    # transfer; anything not actively transferring is "not synced" here.
    return {"state": "not-synced"}


# 当前正在录制的 repo_id；录制模块没暴露具体名字时返回 "*"，表示“任何数据集都别动”。
def _active_recording_repo() -> str | None:
    """Repo id currently being recorded, when the recorder exposes one."""
    if not _record.recording_active:
        return None
    config = getattr(_record, "recording_config", None)
    for attribute in ("repo_id", "dataset_repo_id", "dataset_name", "name"):
        value = getattr(config, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "*"


# 找出正在读取该数据集的训练作业 id（含多数据集组合的成员匹配）。
# 数据集清理 / 移动 / 封存前用它来拒绝危险操作；查询失败不影响后续流程。
def _training_job_using(repo_id: str) -> str | None:
    """Id of a running job that reads this dataset, if any."""
    try:
        records = job_registry.list(limit=50)
    except Exception:  # registry must never block dataset housekeeping
        logger.exception("Could not list jobs while checking dataset usage")
        return None
    for record in records:
        if record.state != "running":
            continue
        effective = getattr(record.config, "dataset_repo_id", None)
        members = getattr(record, "dataset_repo_ids", None) or []
        if repo_id == effective or repo_id in members:
            return record.id
    return None


# 破坏性 / 传输类操作的统一护栏，命中任意一条即 409：
#   1. 该数据集正在录制；2. 有训练作业正在读它；3. 它正在被同步到训练机。
# action 仅用于拼错误信息（trash / delete / sync / seal ...）。
def _ensure_dataset_mutable(repo_id: str, action: str) -> None:
    """Refuse destructive/transfer actions while the dataset is in use."""
    recording = _active_recording_repo()
    if recording is not None and recording in {repo_id, "*"}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} {repo_id}: a recording session is active.",
        )
    job_id = _training_job_using(repo_id)
    if job_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} {repo_id}: job {job_id} is training on it.",
        )
    sync = get_dataset_sync_manager().status()
    if sync.get("repo_id") == repo_id and sync.get("phase") in ACTIVE_SYNC_PHASES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} {repo_id}: a transfer for it is {sync.get('phase')}.",
        )


# 已封存的数据集拒绝一切修改操作（封存 = 逐文件 SHA-256 清单，见 sealing.py），必须先解除封存。
def _ensure_dataset_unsealed(repo_id: str, action: str) -> None:
    """Refuse mutating actions on a sealed (封存) dataset."""
    try:
        seal = seal_status(repo_id)
    except (FileNotFoundError, ValueError):
        return
    if seal.get("sealed"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {action} {repo_id}: the dataset is sealed (封存). "
                "Unseal it first if the data really has to change."
            ),
        )


# 组合数据集正被训练作业读取时，不允许把它移入回收站。
def _ensure_composite_unused(repo_id: str) -> None:
    """Refuse to trash a composite while a training job is reading it."""
    job_id = _training_job_using(repo_id)
    if job_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot trash {repo_id}: job {job_id} is training on it.",
        )


# 调用 dataset_storage 的检查函数，并把 ValueError / FileNotFoundError 统一映射成 400 / 404。
def _dataset_path_or_404(repo_id: str, inspector: Any) -> dict[str, Any]:
    """Run a dataset_storage helper with the standard error mapping."""
    try:
        return inspector(repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# 解析本地数据集目录，同时挡住路径穿越（400）与不存在（404）；
# 数据可视化相关接口用它拿到 Path（而不是 repo_id 字符串）。
def _local_dataset_path_or_404(repo_id: str) -> Path:
    """Resolve a local dataset directory, mapping traversal/missing to 4xx."""
    from .dataset_storage import lerobot_dataset_root, resolve_dataset_path

    try:
        return resolve_dataset_path(repo_id, lerobot_dataset_root())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"本地没有数据集 {repo_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# 只列本地磁盘上的数据集（绝不访问 Hub），并为每条附带同步状态。
@app.get("/datasets/local")
def local_datasets_list():
    """List only on-disk datasets; this endpoint never contacts the Hub."""
    sync_manager = get_dataset_sync_manager()
    active = sync_manager.status()
    items: list[dict[str, Any]] = []
    for item in dataset_browser.list_local_datasets():
        repo_id = item.get("repo_id")
        entry = {**item, "source": "local"}
        entry["sync"] = _dataset_sync_state(repo_id, active=active, manager=sync_manager)
        items.append(entry)
    return items


# 列出被移入 <root>/.trash 的数据集（可恢复），最新的在前。
@app.get("/datasets/local/trash")
def list_local_dataset_trash():
    """Datasets moved to `<root>/.trash`, newest first (restorable)."""
    return {"entries": list_trash()}


# 把剩余磁盘空间映射成仪表盘 / 录制共用的告警等级：
# <5% critical（禁止开录）、<10% danger、<20% warning，其余 normal。
def _storage_warning_level(total_bytes: int | None, free_bytes: int | None) -> str:
    """Map free capacity to the dashboard/recording safety bands."""
    if not total_bytes or free_bytes is None:
        return "unknown"
    free_percent = (free_bytes / total_bytes) * 100
    if free_percent < 5:
        return "critical"
    if free_percent < 10:
        return "danger"
    if free_percent < 20:
        return "warning"
    return "normal"


# Jetson 仪表盘的宿主机快照（CPU / GPU / 内存 / 温度 / 磁盘 / 设备）。
# 在保留 psutil 原始字段的同时，补一层稳定的短别名，方便脚本直接消费。
@app.get("/system/status")
def get_system_status():
    """Return a failure-tolerant host snapshot for the Jetson dashboard."""
    snapshot = system_snapshot()
    platform_info = snapshot.get("platform", {})
    cpu = snapshot.get("cpu", {})
    load = cpu.get("load_average", {})
    memory = snapshot.get("memory", {})
    disk = snapshot.get("disk", {})

    # Keep the rich raw fields while exposing stable, concise aliases used by
    # the web dashboard. This also makes the endpoint easy to consume from
    # scripts without knowing psutil's naming conventions.
    snapshot["host"] = platform_info.get("hostname")
    snapshot["cpu"] = {
        **cpu,
        "count": cpu.get("logical_count"),
        "load_1m": load.get("one_minute"),
    }
    snapshot["memory"] = {
        **memory,
        "total": memory.get("total_bytes"),
        "used": memory.get("used_bytes"),
        "available": memory.get("available_bytes"),
    }
    snapshot["disk"] = {
        **disk,
        "total": disk.get("total_bytes"),
        "used": disk.get("used_bytes"),
        "free": disk.get("free_bytes"),
    }
    return snapshot


# 存储总览：磁盘容量、数据集总占用、告警等级，以及“现在还允不允许开录”。
# 由 /ws/storage-status 复用，因此这里就直接返回前端的最终结构。
@app.get("/storage/summary")
def get_storage_summary():
    """Return local dataset usage and the recording capacity safety state."""
    summary = storage_summary()
    disk = summary.get("disk", {})
    dataset_storage = summary.get("dataset_storage", {})
    total = disk.get("total_bytes")
    free = disk.get("free_bytes")
    return {
        **summary,
        "total_bytes": total,
        "used_bytes": disk.get("used_bytes"),
        "free_bytes": free,
        "percent_used": disk.get("percent"),
        "warning_level": _storage_warning_level(total, free),
        "recording_allowed": _storage_warning_level(total, free) != "critical",
        "total_dataset_bytes": dataset_storage.get("size_bytes", 0),
        "datasets": summary.get("dataset_items", summary.get("datasets", [])),
    }


# 单个本地数据集的详情（容量 / 文件 / 相机等），全程不访问 HuggingFace Hub。
@app.get("/datasets/local/{repo_id:path}/storage")
def get_local_dataset_storage(repo_id: str):
    """Inspect a local LeRobot dataset without contacting Hugging Face Hub."""
    try:
        return dataset_detail(repo_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# 列表页用的数据集摘要：帧数 / 回合数 / 体积 / 相机 / finalize 状态。
@app.get("/datasets/local/{repo_id:path}/summary")
def get_local_dataset_summary(repo_id: str):
    """List-friendly dataset facts: counts, sizes, cameras, finalize state."""
    return _dataset_path_or_404(repo_id, dataset_summary)


# 特征 schema（类型 / 形状 / 名称 / 存储模板）与 Parquet 列信息。
@app.get("/datasets/local/{repo_id:path}/schema")
def get_local_dataset_schema(repo_id: str):
    """Feature schema (types/shapes/names/storage) and Parquet columns."""
    detail = _dataset_path_or_404(repo_id, dataset_detail)
    return {
        "repo_id": detail["repo_id"],
        "format": detail["format"],
        "features": detail["format"]["features"],
        "parquet_schema": detail["parquet_schema"],
        "metadata_error": detail["metadata_error"],
    }


# 返回受限深度的文件树，外加 meta/info.json 里的路径模板。
@app.get("/datasets/local/{repo_id:path}/files")
def get_local_dataset_files(repo_id: str):
    """Bounded file tree plus the path templates from meta/info.json."""
    detail = _dataset_path_or_404(repo_id, dataset_detail)
    return {
        "repo_id": detail["repo_id"],
        "path": detail["path"],
        "storage_templates": detail["format"]["storage_templates"],
        "file_tree": detail["file_tree"],
        "storage": detail["storage"],
    }


# 结构完整性检查（只核对文件与元数据，不解码视频）。
@app.get("/datasets/local/{repo_id:path}/integrity")
def get_local_dataset_integrity(repo_id: str):
    """Structural integrity checks (no video decoding)."""
    return _dataset_path_or_404(repo_id, dataset_integrity)


# ---------------------------------------------------------------- dataset viewer


# 数据可视化页用的逐回合元数据（只读）。
@app.get("/datasets/local/{repo_id:path}/episodes")
def get_dataset_episodes(repo_id: str):
    """Per-episode metadata for the visualisation page (read-only)."""
    from .dataset_viewer import list_episodes

    # `_dataset_path_or_404` passes the repo id string; the viewer needs a Path.
    return list_episodes(_local_dataset_path_or_404(repo_id))


# 串流某一回合某个相机的视频；浏览器需要 Range 支持，因此用 FileResponse 返回 mp4。
@app.get("/datasets/local/{repo_id:path}/episodes/{episode}/video/{camera}")
def get_episode_video(repo_id: str, episode: int, camera: str):
    """Stream one recorded camera video (browsers need Range support)."""
    from .dataset_viewer import video_path

    dataset_path = _local_dataset_path_or_404(repo_id)
    target = video_path(dataset_path, episode, camera)
    if target is None:
        raise HTTPException(status_code=404, detail=f"数据集 {repo_id} 第 {episode} 集没有相机 {camera} 的视频")
    return FileResponse(target, media_type="video/mp4")


# 某一回合的 observation.state / action 降采样时序数据，供前端画曲线。
@app.get("/datasets/local/{repo_id:path}/episodes/{episode}/timeseries")
def get_episode_timeseries(repo_id: str, episode: int):
    """Down-sampled observation.state / action series for one episode."""
    from .dataset_viewer import episode_timeseries

    dataset_path = _local_dataset_path_or_404(repo_id)
    return episode_timeseries(dataset_path, episode)


# 同步请求体：dry_run=true 时只做校验、不真正传输。
class DatasetSyncRequest(BaseModel):
    dry_run: bool = False


# 移入回收站请求体：可附一个原因，便于事后追溯。
class DatasetTrashRequest(BaseModel):
    reason: str | None = None


# 把数据集（断点续传地）同步到训练机的暂存目录；202 表示任务已在后台运行。
@app.post("/datasets/local/{repo_id:path}/sync-train", status_code=202)
def sync_local_dataset_to_train(repo_id: str, body: DatasetSyncRequest | None = None):
    """Upload this dataset to the training server's staging area (resumable)."""
    _ensure_dataset_mutable(repo_id, "sync")
    manager = get_dataset_sync_manager()
    try:
        status = manager.start(repo_id, dry_run=bool(body and body.dry_run))
    except DatasetSyncError as exc:
        message = str(exc)
        status_code = 409 if "already running" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    return {"success": True, "sync": status}


# 单个数据集的同步情况：进行中的实时状态 + 最近一次历史结果。
@app.get("/datasets/local/{repo_id:path}/sync-status")
def local_dataset_sync_status(repo_id: str):
    """Live transfer state for one dataset."""
    manager = get_dataset_sync_manager()
    active = manager.status()
    return {
        "repo_id": repo_id,
        "active": active.get("repo_id") == repo_id
        and active.get("phase") in ACTIVE_SYNC_PHASES,
        "sync": active if active.get("repo_id") == repo_id else None,
        "last": manager.history(repo_id),
        "state": _dataset_sync_state(repo_id, active=active, manager=manager),
    }


# 仪表盘视图：当前唯一活跃的传输（如果有）+ 最近历史记录。
@app.get("/datasets/sync-status")
def datasets_sync_status():
    """Dashboard view: the one active transfer (if any) plus recent history."""
    manager = get_dataset_sync_manager()
    return {"active": manager.status(), "history": manager.history()}


# 取消正在进行的传输；训练机上已传的暂存内容会保留，便于下次续传。
@app.post("/datasets/sync-cancel")
def datasets_sync_cancel():
    """Request cancellation of the running transfer (remote staging is kept)."""
    return {"success": True, "sync": get_dataset_sync_manager().cancel()}


# 把数据集移到 <root>/.trash（软删除，绝不物理抹除）；先做“未被占用”“未封存”检查。
@app.post("/datasets/local/{repo_id:path}/trash")
def trash_local_dataset(repo_id: str, body: DatasetTrashRequest | None = None):
    """Move a dataset to `<root>/.trash`; never a permanent delete."""
    _ensure_dataset_mutable(repo_id, "trash")
    _ensure_dataset_unsealed(repo_id, "move to trash")
    try:
        record = trash_dataset(repo_id)
    except SealError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record["reason"] = (body.reason if body is not None else None) or "user-request"
    return {"success": True, "trashed": record}


# 封存接口的可选参数（目前只有 reason，预留给后续审计信息）。
class SealRequest(BaseModel):
    """Optional knobs for the sealing endpoint (currently only a reason)."""

    reason: str | None = None


# 封存数据集：后台逐文件计算 SHA-256 清单（meta/.lelab-sealed.json），
# 之后所有写路径（回收站 / 删除 / 续录 / 修复）都会拒绝改动它。
@app.post("/datasets/local/{repo_id:path}/seal", status_code=202)
def seal_local_dataset(repo_id: str, body: SealRequest | None = None):
    """Seal a finalized dataset: hash every file and forbid mutation."""
    del body  # reserved for future audit metadata
    _ensure_dataset_mutable(repo_id, "seal")
    if _active_recording_repo() in {repo_id, "*"}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot seal {repo_id}: a recording session is active.",
        )
    try:
        status = get_seal_manager().start(repo_id)
    except SealError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True, "seal": status}


# 查询封存进度，或返回已完成数据集的封存记录。
@app.get("/datasets/local/{repo_id:path}/seal")
def local_dataset_seal_status(repo_id: str):
    """Live sealing progress, or the recorded seal for a finished dataset."""
    return {"repo_id": repo_id, "seal": get_seal_manager().status(repo_id)}


# 取消进行中的封存计算，数据集保持未封存状态。
@app.post("/datasets/local/{repo_id:path}/seal/cancel")
def cancel_local_dataset_seal(repo_id: str):
    """Abort an in-flight sealing pass; the dataset stays unsealed."""
    return {"success": True, "seal": get_seal_manager().cancel(repo_id)}


# 解除封存，使数据集重新可以修改。
@app.post("/datasets/local/{repo_id:path}/unseal")
def unseal_local_dataset(repo_id: str, body: SealRequest | None = None):
    """Remove the seal so the dataset can be modified again."""
    del body
    _ensure_dataset_mutable(repo_id, "unseal")
    try:
        result = unseal_dataset(repo_id)
    except SealError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"success": True, **result}


# 重新计算哈希并与封存清单比对，检测数据是否被改动（只读，不改任何文件）。
@app.post("/datasets/local/{repo_id:path}/seal/verify")
def verify_local_dataset_seal(repo_id: str):
    """Re-hash the dataset and compare it with the recorded seal (read-only)."""
    try:
        return verify_seal(repo_id)
    except SealError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# 从回收站恢复该数据集（默认恢复最新的一份；也可用 entry 指定具体条目）。
@app.post("/datasets/local/{repo_id:path}/restore")
def restore_local_dataset(repo_id: str, entry: str | None = None):
    """Restore the newest trashed copy of this dataset."""
    try:
        return {"success": True, "restored": restore_dataset(repo_id, entry=entry)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# 不带 local/ 前缀的别名路由，对应对外文档中的 /datasets/{dataset_id}/summary 形式。
# 字面量路由先注册，所以 /datasets/local/... 永远不会落到这些通配路由上。
# Aliases without the `local/` prefix, matching the documented API surface
# (`/datasets/{dataset_id}/summary`). Literal routes are registered first, so
# `/datasets/local/...` never reaches these.
@app.get("/datasets/{repo_id:path}/summary")
def get_dataset_summary_alias(repo_id: str):
    return get_local_dataset_summary(repo_id)


@app.get("/datasets/{repo_id:path}/schema")
def get_dataset_schema_alias(repo_id: str):
    return get_local_dataset_schema(repo_id)


@app.get("/datasets/{repo_id:path}/files")
def get_dataset_files_alias(repo_id: str):
    return get_local_dataset_files(repo_id)


@app.get("/datasets/{repo_id:path}/integrity")
def get_dataset_integrity_alias(repo_id: str):
    return get_local_dataset_integrity(repo_id)


# 多数据集组合的兼容性检查请求：成员 repo_ids + 采样权重模式（frames / equal / custom）。
class DatasetCompatibilityRequest(BaseModel):
    repo_ids: list[str]
    weights_mode: Literal["frames", "equal", "custom"] = "frames"
    weights: list[float] | None = None


# 组合数据集构建请求（字段与兼容性检查完全一致，语义上表示“确认开始构建”）。
class CompositeBuildRequest(DatasetCompatibilityRequest):
    pass


# 以组合标识（composite hash key）操作构建任务的请求体。
class CompositeKeyRequest(BaseModel):
    key: str


# 删除远端组合数据集的请求体。
class CompositeTrashRequest(BaseModel):
    repo_id: str


# 恢复远端组合数据集的请求体（需指定回收站条目）。
class CompositeRestoreRequest(BaseModel):
    repo_id: str
    entry: str


# 生成组合计划（兼容性校验 + 确定性的 local/composite_<hash16> 标识）；
# 参数或兼容性错误统一转成 400。
def _composite_plan_or_400(body: DatasetCompatibilityRequest):
    from .composites import CompositeError, plan_composite

    try:
        plan = plan_composite(
            body.repo_ids, weights_mode=body.weights_mode, weights=body.weights
        )
    except CompositeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return plan


# 兼容性报告：成员数据集的 fps / 相机名 / 维度 / codebase 版本 / 存储格式是否一致，
# 以及训练机上是否已经存在同一个组合（cached / remote_state）。
@app.post("/datasets/compatibility")
def datasets_compatibility(body: DatasetCompatibilityRequest):
    """Compatibility report for a candidate multi-dataset selection."""
    from .composites import CompositeError, remote_status

    plan = _composite_plan_or_400(body)
    cached: bool | None = None
    remote_state: str | None = None
    remote_error: str | None = None
    if plan.ok and plan.composite_repo_id:
        try:
            status = remote_status(plan.composite_repo_id)
            remote_state = str(status.get("state"))
            cached = remote_state == "ready"
        except CompositeError as exc:
            remote_state = "unknown"
            remote_error = str(exc)
    report = plan.to_report(cached=cached, remote_state=remote_state)
    if remote_error:
        report["remote_error"] = remote_error
    return report


# 在训练机上开始构建组合数据集；远端已就绪时直接复用（cached=True），
# 否则通过 SSH 启动后台构建任务。
@app.post("/datasets/composite", status_code=202)
def start_composite_build(body: CompositeBuildRequest):
    """Start (or reuse) a composite build on the training server."""
    from .composites import (
        CompositeError,
        get_composite_manager,
        remote_status,
        train_server_config,
    )

    plan = _composite_plan_or_400(body)
    if not plan.ok:
        messages = "；".join(blocker["message"] for blocker in plan.blockers[:4])
        raise HTTPException(status_code=409, detail=f"数据集不能组合：{messages}")
    if plan.composite_repo_id is None:
        raise HTTPException(status_code=400, detail="组合数据集计划不完整")
    try:
        config = train_server_config()
        status = remote_status(plan.composite_repo_id, config=config)
    except CompositeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if status.get("state") == "ready":
        return {
            "success": True,
            "cached": True,
            "composite": plan.to_report(cached=True, remote_state="ready"),
            "build": {"composite_hash": plan.composite_hash, "state": "ready", "progress": 1.0},
        }
    try:
        build = get_composite_manager().start(plan, runner="ssh_remote", config=config)
    except CompositeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "success": True,
        "cached": False,
        "composite": plan.to_report(cached=False, remote_state="missing"),
        "build": build,
    }


# 组合构建进度 + 远端产物状态。
@app.get("/datasets/composite/status")
def composite_build_status(key: str):
    """Progress for an in-flight composite build, plus the remote state."""
    from .composites import (
        CompositeError,
        get_composite_manager,
        is_composite_repo_id,
        remote_status,
    )

    build = get_composite_manager().status(key)
    remote: dict[str, Any] | None = None
    if is_composite_repo_id(key):
        try:
            remote = remote_status(key)
        except CompositeError as exc:
            remote = {"state": "unknown", "error": str(exc)}
    return {"build": build, "remote": remote}


# 取消进行中的组合构建。
@app.post("/datasets/composite/cancel")
def cancel_composite_build(body: CompositeKeyRequest):
    from .composites import get_composite_manager

    return {"success": True, "build": get_composite_manager().cancel(body.key)}


# 列出训练机上的组合数据集 —— 它们是一等数据集，可以直接拿去训练。
@app.get("/datasets/composites")
def list_composites():
    """Composite datasets stored on the training server (first-class datasets)."""
    from .composites import CompositeError, list_remote_composites

    try:
        items = list_remote_composites()
    except CompositeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"composites": items}


# 列出训练机上已删除（回收站）的组合数据集。
@app.get("/datasets/composites/trash")
def list_composites_trash():
    from .composites import CompositeError, list_remote_composite_trash

    try:
        items = list_remote_composite_trash()
    except CompositeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"trash": items}


# 删除远端组合数据集（有训练作业在读它时拒绝）。
@app.post("/datasets/composites/trash")
def trash_composite(body: CompositeTrashRequest):
    from .composites import CompositeError, trash_remote_composite

    _ensure_composite_unused(body.repo_id)
    try:
        return {"success": True, "trashed": trash_remote_composite(body.repo_id)}
    except CompositeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# 从回收站恢复远端组合数据集。
@app.post("/datasets/composites/restore")
def restore_composite(body: CompositeRestoreRequest):
    from .composites import CompositeError, restore_remote_composite

    try:
        return {
            "success": True,
            "restored": restore_remote_composite(body.repo_id, body.entry),
        }
    except CompositeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# 自检：确认 WebSocket 路由存在，并告知前端应连接的地址。
@app.get("/ws-test")
def websocket_test():
    """Test endpoint to verify WebSocket support"""
    return {"websocket_endpoint": "/ws/joint-data", "status": "available"}


# 关节数据下行通道：连接由 ConnectionManager 登记，之后由广播线程推送数据；
# 本协程只负责保活（1 秒超时读，用于及时发现断开）与退出时的清理。
@app.websocket("/ws/joint-data")
async def websocket_endpoint(websocket: WebSocket):
    logger.info("🔗 New WebSocket connection attempt")
    try:
        await manager.connect(websocket)
        logger.info("✅ WebSocket connection established")

        while True:
            # Keep the connection alive and wait for messages
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                # Handle any incoming messages if needed
                logger.debug(f"Received WebSocket message: {data}")
            except TimeoutError:
                # No message received, continue
                pass
            except WebSocketDisconnect:
                logger.info("🔌 WebSocket client disconnected")
                break

            # Small delay to prevent excessive CPU usage
            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        logger.info("🔌 WebSocket disconnected normally")
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
    finally:
        manager.disconnect(websocket)
        logger.info("🧹 WebSocket connection cleaned up")


# 通用“定时推送快照”循环：每隔 interval 秒给一个 WS 客户端推一次 builder() 的结果。
# builder 通过 asyncio.to_thread 在线程里执行 —— psutil / tegrastats / 磁盘遍历都是阻塞调用，
# 直接跑在事件循环里会拖住其它 WebSocket（包括机器人的关节数据流）。
async def _push_status_loop(
    websocket: WebSocket,
    builder: Any,
    *,
    interval: float,
) -> None:
    """Push a fresh snapshot to one dashboard client every `interval` seconds.

    The snapshot builder runs in a worker thread: `psutil`, `tegrastats` and
    disk walks are blocking, and running them on the event loop would stall
    every other websocket (including the robot's joint stream).
    """
    await websocket.accept()
    try:
        while True:
            snapshot = await asyncio.to_thread(builder)
            await websocket.send_json(snapshot)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(websocket.receive_text(), timeout=interval)
    except WebSocketDisconnect:
        logger.debug("Status websocket disconnected")
    except Exception as exc:  # pragma: no cover - transport errors vary
        logger.debug("Status websocket closed: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


# 每秒一次的系统状态推送（CPU / GPU / 内存 / 温度 / 磁盘 / 设备）。
@app.websocket("/ws/system-status")
async def websocket_system_status(websocket: WebSocket):
    """Once-per-second host snapshot (CPU/GPU/RAM/temp/disk/devices)."""
    await _push_status_loop(websocket, get_system_status, interval=1.0)


# 每 2 秒一次的存储容量 / 数据集占用推送。
@app.websocket("/ws/storage-status")
async def websocket_storage_status(websocket: WebSocket):
    """Capacity/dataset-usage stream for the storage page."""
    await _push_status_loop(websocket, get_storage_summary, interval=2.0)


# 开始录制。三重前置检查，任一不过就拒绝启动：
#   1. 磁盘剩余 < 5% → 507（录制会迅速撑爆磁盘）；
#   2. 服务端相机预览必须先释放设备 → 否则 409；
#   3. 取硬件租约；启动失败立即归还。
@app.post("/start-recording")
def start_recording(request: RecordingRequest, http_request: Request):
    """Start a dataset recording session"""
    capacity = dataset_disk_capacity()
    warning_level = _storage_warning_level(capacity.get("total_bytes"), capacity.get("free_bytes"))
    if warning_level == "critical":
        raise HTTPException(
            status_code=507,
            detail="Recording blocked: less than 5% free space remains on the dataset filesystem.",
        )
    preview_release = _release_camera_previews()
    if preview_release["remaining"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "Recording blocked because one or more camera previews did not release their devices. "
                "Close other preview pages and try again."
            ),
        )
    lease = _acquire_hardware_lease(http_request, "recording")
    result = handle_start_recording(request)
    _release_lease_if_failed(lease, result)
    return result


# 停止录制并释放租约。
@app.post("/stop-recording")
def stop_recording():
    """Stop the current recording session"""
    result = handle_stop_recording()
    hardware_lease.release(task="recording")
    return result


# 查询录制状态（回合数、当前回合等）。
@app.get("/recording-status")
def recording_status():
    """Get the current recording status"""
    return handle_recording_status()


# 录制过程中的实时相机预览：以 multipart/x-mixed-replace（MJPEG）流返回，
# 前端直接把它塞进 <img src> 即可；仅录制期间有效，录制结束生成器自行退出。
@app.get("/camera-feed/{cam_key}")
def camera_feed(cam_key: str):
    """Live MJPEG preview of one configured camera during an active recording.

    Browsers render `multipart/x-mixed-replace` directly in an <img>, so the
    frontend just points an <img> at this URL. Only valid while a session is
    active; the generator ends itself when recording stops.
    """
    if not _record.recording_active:
        raise HTTPException(status_code=409, detail="No active recording session")
    return StreamingResponse(
        _record.camera_feed_frames(cam_key),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# 提前结束当前回合、进入下一回合（替代键盘右方向键）。
@app.post("/recording-exit-early")
def recording_exit_early():
    """Skip to next episode (replaces right arrow key)"""
    return handle_exit_early()


# 重录当前回合（替代键盘左方向键）。
@app.post("/recording-rerecord-episode")
def recording_rerecord_episode():
    """Re-record current episode (replaces left arrow key)"""
    return handle_rerecord_episode()


# 把数据集上传到 HuggingFace Hub。
@app.post("/upload-dataset")
def upload_dataset(request: UploadRequest):
    """Upload dataset to HuggingFace Hub"""
    return handle_upload_dataset(request)


# 查询某个已保存数据集的信息。
@app.post("/dataset-info")
def get_dataset_info(request: DatasetInfoRequest):
    """Get information about a saved dataset"""
    return handle_get_dataset_info(request)


# 从本地磁盘彻底删除数据集目录（不可恢复）；删除前检查是否正在使用 / 已被封存。
@app.post("/delete-dataset")
def delete_dataset(request: DatasetInfoRequest):
    """Remove a recorded dataset directory from local disk."""
    _ensure_dataset_mutable(request.dataset_repo_id, "delete")
    _ensure_dataset_unsealed(request.dataset_repo_id, "permanently delete")
    return handle_delete_dataset(request)


# ============================================================================
# JOB ENDPOINTS
# ============================================================================


# 提交训练作业。这里手工解析 JSON，是为了兼容旧请求体形状
# （见 StartTrainingBody.from_legacy）；runner 启动失败会带上可操作的原因返回 409。
@app.post("/jobs/training", status_code=201)
async def create_training_job(req: Request):
    raw = await req.json()
    body = StartTrainingBody.from_legacy(raw)
    try:
        record = job_registry.start(body.config, body.target)
    except JobAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=f"Job already running: {exc}") from exc
    except ValueError as exc:
        # e.g. "flavor is required when runner is hf_cloud"
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Runner start failures (dataset not synced, SSH unreachable, remote
        # command rejected, ...) carry the actionable message; surface it as a
        # 409 instead of letting FastAPI turn it into a bare 500.
        logger.warning("Training job start failed: %s", exc)
        raise HTTPException(status_code=409, detail=f"启动失败：{exc}") from exc
    return record


# 导入外部模型（本地目录或 HF 仓库）的请求体。
class ImportModelRequest(BaseModel):
    source: str
    name: str | None = None


# 把外部模型注册成“伪作业”，便于在界面上统一查看与推理。
@app.post("/jobs/import", status_code=201)
def import_model(body: ImportModelRequest):
    """Register an external model (local dir or HF repo) as a pseudo-job."""
    try:
        return job_registry.register_imported(body.source, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# 列出最近的作业记录（默认 10 条）。
@app.get("/jobs")
def list_jobs(limit: int = 10):
    return {"jobs": job_registry.list(limit=limit)}


# 列出该用户的 HF Cloud Jobs 以及其上传的 LeRobot 模型仓库。
# 未配置 token 时返回 200 + 空列表，让前端直接渲染未登录空状态而不是报错。
# 必须声明在 /jobs/{job_id} 之前，否则 "hub" 会被当成 job_id。
@app.get("/jobs/hub")
def list_hub_jobs():
    """List the user's HF Cloud compute Jobs and their uploaded LeRobot model
    repos on huggingface.co.

    Returns 200 with empty lists when no token is configured so the frontend
    can render an unauthenticated empty state without surfacing an error.

    Declared before `/jobs/{job_id}` so FastAPI's first-match routing doesn't
    treat "hub" as a job id.
    """
    info = cached_whoami()
    if info is None:
        return {"authenticated": False, "jobs": [], "models": []}
    api = shared_hf_api()

    authors: list[str] = []
    if info.get("name"):
        authors.append(info["name"])
    for o in info.get("orgs", []) or []:
        if isinstance(o, dict) and o.get("name"):
            authors.append(o["name"])

    try:
        jobs = api.list_jobs()
    except Exception as exc:
        logger.warning("list_jobs failed: %s", exc)
        jobs = []

    seen_models: set[str] = set()
    models: list[dict] = []
    for author in authors:
        try:
            for m in api.list_models(author=author, filter="LeRobot", limit=200):
                if m.id in seen_models:
                    continue
                seen_models.add(m.id)
                models.append(
                    {
                        "repo_id": m.id,
                        "last_modified": m.last_modified.isoformat() if m.last_modified else None,
                        "private": bool(getattr(m, "private", False)),
                    }
                )
        except Exception as exc:
            logger.warning("list_models(%s) failed: %s", author, exc)
    models.sort(key=lambda m: m["last_modified"] or "", reverse=True)

    return {
        "authenticated": True,
        "jobs": [
            {
                "id": ji.id,
                "created_at": ji.created_at.isoformat() if ji.created_at else None,
                "docker_image": ji.docker_image,
                "space_id": ji.space_id,
                "flavor": ji.flavor,
                "status": ({"stage": ji.status.stage, "message": ji.status.message} if ji.status else None),
                "owner": ji.owner.name if ji.owner else None,
                "url": ji.url,
            }
            for ji in jobs
        ],
        "models": models,
    }


# 列出“本实例启动、但本地注册表已丢失”的远端作业（例如进程 reload 之后）。
@app.get("/jobs/orphans")
def list_orphan_jobs():
    """Remote runs started by this instance that the local registry lost."""
    try:
        return {"jobs": job_registry.orphan_jobs()}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# 认领孤儿作业：把它重新纳入本地注册表，恢复可管理状态。
@app.post("/jobs/orphans/{job_id}/adopt", status_code=201)
def adopt_orphan_job(job_id: str):
    """Take an orphan into the local registry so it can be managed again."""
    try:
        return job_registry.adopt_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"远端没有作业 {job_id!r}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# 查询单个作业详情。
@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    try:
        return job_registry.get(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found") from exc


# 增量拉取作业日志：读空队列，只返回上次之后新到的行。
@app.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str):
    try:
        logs = job_registry.drain_logs(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found") from exc
    return {"logs": logs}


# 返回作业在磁盘上的完整日志；顺带把实时队列读空，避免前端重复显示。
@app.get("/jobs/{job_id}/log-file")
def get_job_log_file(job_id: str):
    """Return the entire on-disk log file for a job. Drains the live queue too
    so the next /logs poll returns only lines that arrived after this call."""
    try:
        logs = job_registry.read_persisted_logs(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found") from exc
    # Best-effort drain so the frontend doesn't double-display.
    with contextlib.suppress(JobNotFoundError):
        job_registry.drain_logs(job_id)
    return {"logs": logs}


# 从作业的 log.jsonl 重建 loss / lr / grad-norm 序列，
# 用于页面刷新、路由切换或 lelab 重启后恢复监控曲线。
@app.get("/jobs/{job_id}/metrics-history")
def get_job_metrics_history(job_id: str):
    """Return the per-step loss/lr/grad-norm series reconstructed from the
    job's log.jsonl. Used to seed the monitoring charts so curves persist
    across page reloads, navigation, and lelab restarts."""
    try:
        points = job_registry.read_metrics_history(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found") from exc
    return {"points": points}


# 列出该作业已保存的 checkpoint（按 step 升序）。
@app.get("/jobs/{job_id}/checkpoints")
def get_job_checkpoints(job_id: str):
    """List the checkpoints saved for this job, ascending by step."""
    try:
        return {"checkpoints": job_registry.list_checkpoints(job_id)}
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found") from exc


# 取出 checkpoint 中与界面相关的配置：策略类型、各相机的分辨率、是否需要任务描述。
@app.get("/jobs/{job_id}/checkpoints/{step}/policy-config")
def get_checkpoint_policy_config(job_id: str, step: int):
    """Return the UX-relevant slice of a checkpoint's pretrained_model config:
    policy_type, image_features (per-camera height/width), and requires_task."""
    try:
        return job_registry.get_policy_config_summary(job_id, step)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# 停止作业（本地进程 / SSH 远端 / HF 云端由各自的 runner 实现）。
@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: str):
    try:
        return job_registry.stop(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found") from exc
    except JobNotRunningError as exc:
        raise HTTPException(status_code=409, detail=f"Job {job_id!r} is not running") from exc


# 删除作业记录；ssh_remote 的作业会进远端 jobs/.trash，而不是被直接抹掉。
@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, purge_remote: bool = True):
    """Delete a job record; ssh_remote runs go to the remote `jobs/.trash`."""
    from .runners.ssh_ops import RemoteJobNotOursError

    try:
        job_registry.delete_job(job_id, purge_remote=purge_remote)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found") from exc
    except JobNotRunningError as exc:
        raise HTTPException(status_code=409, detail=f"Job {job_id!r} is running; stop it first") from exc
    except RemoteJobNotOursError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# 返回 HF Jobs 硬件规格目录 + 登录状态，供“训练目标”卡片使用。
# flavors 与 whoami 都在进程内缓存，登录时会让 whoami 缓存失效。
@app.get("/jobs/runners/hardware")
def get_runners_hardware():
    """Return HF Jobs flavor catalog + auth state for the TargetCard.

    Both the flavors list and the whoami result are cached in-process to
    keep this endpoint cheap (it can be re-fetched whenever auth state
    changes). The whoami cache is invalidated on login.
    """
    info = cached_whoami()
    if info is None or not info.get("name"):
        return {"authenticated": False, "username": None, "flavors": []}
    username: str = info["name"]
    api = shared_hf_api()

    now = time.time()
    if _flavors_cache["data"] is None or now - _flavors_cache["fetched_at"] > _FLAVOR_CACHE_TTL_SECONDS:
        try:
            hw_list = api.list_jobs_hardware()
        except Exception as exc:
            logger.warning("list_jobs_hardware failed: %s", exc)
            return {"authenticated": True, "username": username, "flavors": []}
        _flavors_cache["data"] = [
            {
                "name": h.name,
                "pretty_name": h.pretty_name,
                "cpu": h.cpu,
                "ram": h.ram,
                "accelerator": h.accelerator,
                "unit_cost_usd": h.unit_cost_usd,
                "unit_label": h.unit_label,
            }
            for h in hw_list
        ]
        _flavors_cache["fetched_at"] = now

    return {
        "authenticated": True,
        "username": username,
        "flavors": _flavors_cache["data"],
    }


# ============================================================================
# SYSTEM ENDPOINTS
# ============================================================================


# 检测“机器上有 NVIDIA GPU，但 PyTorch 是 CPU 版”这一常见问题（issue #30）。
@app.get("/system/cuda-status")
def get_cuda_status():
    """Report whether an NVIDIA GPU is present but PyTorch is CPU-only (issue #30)."""
    return handle_get_cuda_status()


# 训练服务器连接信息的脱敏视图（含同步状态与推理策略端口），不会发起任何网络请求。
def _train_server_payload() -> dict[str, Any]:
    """Non-secret view of the SSH training target (never touches the network)."""
    from .utils.servers import train_policy_endpoint

    policy_host, policy_port = train_policy_endpoint()
    base: dict[str, Any] = {
        "policy_endpoint": {"host": policy_host, "port": policy_port},
        "sync": get_dataset_sync_manager().status(),
    }
    try:
        config = TrainServerConfig.from_env()
    except DatasetSyncError as exc:
        return {**base, "configured": False, "error": str(exc)}
    return {
        **base,
        "configured": True,
        "endpoint": config.endpoint,
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "root": config.root,
        "ssh_key": str(config.ssh_key) if config.ssh_key else None,
        "jobs_root": f"{config.root}/jobs",
        "datasets_root": f"{config.root}/datasets",
    }


# 训练服务器配置：数据集、作业、兜底推理流量都发往这里。
@app.get("/system/train-server")
def get_train_server_status():
    """Where the Jetson sends datasets, jobs and fallback inference traffic."""
    return _train_server_payload()


# 训练机控制面快照：GPU、28081 端口、作业、会话、磁盘。
# 只走一次 SSH 采集并做秒级缓存；查询通道永远不是推理端口。
@app.get("/system/train-server/status")
def get_train_server_live_status(force: bool = False):
    """Read-only control-plane snapshot: GPUs, port 28081, jobs, sessions, disk.

    Collected over one SSH call (TTL-cached for a couple of seconds) — the
    query channel is never the inference port.
    """
    payload = train_server_status(force=force)
    # The remote marker is the long-term source of truth, but jobs started
    # before markers existed (or after a registry reload) are still ours: the
    # local registry knows their ids, so mark them here without another SSH.
    try:
        known = job_registry.known_job_ids()
    except Exception:  # pragma: no cover - defensive
        known = set()
    for job in payload.get("jobs", []):
        if job.get("id") in known:
            job["ours"] = True
    return payload


# 清理远端中间 checkpoint 的请求体（默认 dry_run，只预演不删除）。
class CheckpointPruneRequest(BaseModel):
    keep_n: int = 3
    job_ids: list[str] | None = None
    dry_run: bool = True


# 删除指定 checkpoint step 的请求体。
class CheckpointDeleteRequest(BaseModel):
    job_id: str
    steps: list[int]


# 把远端 checkpoint 回传到本机的请求体（step 省略时取最新）。
class ModelPullRequest(BaseModel):
    job_id: str
    step: int | None = None


# 列出已回传到本机（Jetson）的模型及回收站内容，供 /models 页面使用。
@app.get("/models/local")
def list_local_models():
    """Models pulled to this Jetson (plus the trash) for the /models page."""
    from .model_store import keep_local_models, list_local_models as _list, list_trashed_models

    return {
        "keep": keep_local_models(),
        "models": _list(),
        "trash": list_trashed_models(),
    }


# 远端模型库：本系统的作业 checkpoint + 暂存/预训练权重。
# 每条带 local / local_step，界面据此置灰已在本机的权重，其余提供单步下载。
@app.get("/system/train-server/models")
def list_train_server_models(force: bool = False):
    """Remote model library: our job checkpoints + staged/pretrained weights.

    Each entry carries ``local``/``local_step`` so the UI can grey out what is
    already on this Jetson (and offer a single-step download for the rest).
    """
    from .model_store import list_local_models as _local_models

    payload = train_server_status(force=force)
    if not payload.get("ssh_ok"):
        return {
            "ssh_ok": False,
            "error": payload.get("error"),
            "library": [],
            "weights": [],
        }
    try:
        known = job_registry.known_job_ids()
    except Exception:  # pragma: no cover - defensive
        known = set()
    local_by_job = {model["job_id"]: model for model in _local_models()}
    library = []
    for job in payload.get("jobs", []):
        ours = job.get("id") in known or job.get("ours")
        if not ours or not job.get("checkpoints"):
            continue
        local = local_by_job.get(job["id"])
        library.append(
            {
                "job_id": job["id"],
                "state": job.get("state"),
                "checkpoints": job.get("checkpoints", []),
                "total_mb": job.get("checkpoint_mb", 0),
                "local": local is not None,
                "local_step": (local or {}).get("step"),
                "local_size_mb": (local or {}).get("size_mb"),
                "local_state": job.get("pull_state") if isinstance(job, dict) else None,
            }
        )
    library.sort(key=lambda item: item["job_id"], reverse=True)
    return {
        "ssh_ok": True,
        "error": None,
        "endpoint": payload.get("endpoint"),
        "keep": (payload.get("usage") or {}).get("jobs_gb"),
        "library": library,
        "weights": payload.get("staged_weights", []),
    }


# 模型回传的当前进度。
@app.get("/models/pull-status")
def model_pull_status():
    from .model_store import pull_state

    return pull_state()


# 从训练机下载某个 checkpoint（不指定 step 则取最新的）。
# 同一时刻只允许一个回传任务在跑；实际传输放在后台线程，接口立刻返回。
@app.post("/models/pull", status_code=202)
def pull_remote_model(body: ModelPullRequest):
    """Download one checkpoint step (or the newest) from the train server."""
    import threading

    from .model_store import pull_model, pull_state
    from .remote_sync import TrainServerConfig

    payload = train_server_status(force=True)
    if not payload.get("ssh_ok"):
        raise HTTPException(status_code=502, detail=f"训练服务器不可达：{payload.get('error')}")
    job = next((item for item in payload.get("jobs", []) if item.get("id") == body.job_id), None)
    if job is None or not job.get("checkpoints"):
        raise HTTPException(status_code=404, detail=f"作业 {body.job_id} 在训练机上没有 checkpoint")
    steps = [int(ckpt["step"]) for ckpt in job["checkpoints"]]
    step = int(body.step) if body.step is not None else max(steps)
    if step not in steps:
        raise HTTPException(status_code=404, detail=f"作业 {body.job_id} 上没有 step {step}")
    if pull_state().get("phase") in {"transferring", "verifying"}:
        raise HTTPException(status_code=409, detail="已有模型正在下载，请等它完成")
    config = TrainServerConfig.from_env()
    remote_dir = (
        f"{config.root.rstrip('/')}/jobs/{body.job_id}/run/checkpoints/{step:06d}/pretrained_model"
    )

    # 后台线程：真正执行下载，完成后把模型注册成“伪作业”，
    # 这样推理页面可以像选择训练产物一样选择它。
    def worker() -> None:
        try:
            pull_model(config, job_id=body.job_id, remote_pretrained_dir=remote_dir, step=step)
            with contextlib.suppress(Exception):
                job_registry.register_imported(
                    str(__import__("lelab.model_store", fromlist=["model_root"]).model_root()
                        / body.job_id),
                    name=f"{body.job_id} (local)",
                )
        except Exception:
            logger.exception("Manual model pull failed for %s", body.job_id)

    threading.Thread(target=worker, name=f"pull-{body.job_id}", daemon=True).start()
    return {"job_id": body.job_id, "step": step, "phase": "transferring"}


# 把本机模型移入回收站（软删除）。
@app.post("/models/local/{job_id}/trash")
def trash_local_model(job_id: str):
    from .model_store import trash_model

    try:
        return {"trash_name": trash_model(job_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# 从回收站恢复本机模型。
@app.post("/models/local/trash/{trash_name}/restore")
def restore_local_model(trash_name: str):
    from .model_store import restore_model

    try:
        return {"job_id": restore_model(trash_name)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# 彻底删除回收站中的模型（不可恢复）。
@app.delete("/models/local/trash/{trash_name}")
def purge_local_model(trash_name: str):
    from .model_store import purge_model

    try:
        purge_model(trash_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"purged": trash_name}


# 从训练机快照里取出目标作业并校验归属：不存在 → 404；
# 不是本系统启动的 → 403（防止误删别人的权重）。
def _checkpoint_job_or_404(payload: dict, job_id: str) -> dict:
    job = next((item for item in payload.get("jobs", []) if item.get("id") == job_id), None)
    if job is None:
        raise HTTPException(status_code=404, detail=f"训练机上没有作业 {job_id}")
    if not job.get("ours"):
        raise HTTPException(status_code=403, detail=f"作业 {job_id} 不是本系统启动的，拒绝删除")
    return job


# /models 页用的权重清单：各作业的 checkpoint、磁盘占用与可回收空间。
@app.get("/system/train-server/checkpoints")
def list_train_server_checkpoints(force: bool = False):
    """Weights inventory for the /models page: per-job checkpoints + disk."""
    from .jobs import default_keep_checkpoints
    from .train_server import prune_plan

    payload = train_server_status(force=force)
    try:
        known = job_registry.known_job_ids()
    except Exception:  # pragma: no cover - defensive
        known = set()
    for job in payload.get("jobs", []):
        if job.get("id") in known:
            job["ours"] = True
    keep_n = default_keep_checkpoints()
    plan = prune_plan(payload.get("jobs", []), keep_n=keep_n)
    reclaimable_mb = sum(item["size_mb"] for item in plan)
    return {
        "ssh_ok": payload.get("ssh_ok", False),
        "error": payload.get("error"),
        "disk": payload.get("disk"),
        "usage": payload.get("usage"),
        "keep_n": keep_n,
        "reclaimable_mb": reclaimable_mb,
        "reclaimable": plan,
        "jobs": [
            {
                "id": job["id"],
                "ours": job.get("ours", False),
                "state": job.get("state"),
                "checkpoint_mb": job.get("checkpoint_mb", 0),
                "checkpoints": job.get("checkpoints", []),
            }
            for job in payload.get("jobs", [])
        ],
    }


# 按 keep_n 直接删除（不进回收站）多余的中间 checkpoint；默认 dry_run，只预演不落刀。
@app.post("/system/train-server/checkpoints/prune")
def prune_train_server_checkpoints(body: CheckpointPruneRequest):
    """Hard-delete intermediate checkpoints beyond ``keep_n`` (dry-run by default)."""
    from .train_server import apply_checkpoint_plan, invalidate_train_server_status, prune_plan

    payload = train_server_status(force=True)
    if not payload.get("ssh_ok"):
        raise HTTPException(status_code=502, detail=f"训练服务器不可达：{payload.get('error')}")
    try:
        known = job_registry.known_job_ids()
    except Exception:  # pragma: no cover - defensive
        known = set()
    for job in payload.get("jobs", []):
        if job.get("id") in known:
            job["ours"] = True
    plan = prune_plan(payload.get("jobs", []), keep_n=body.keep_n, job_ids=body.job_ids)
    if body.dry_run:
        return {"dry_run": True, "keep_n": body.keep_n, "would_delete": plan,
                "reclaimable_mb": sum(item["size_mb"] for item in plan)}
    if not plan:
        return {"dry_run": False, "deleted": [], "reclaimable_mb": 0}
    config = TrainServerConfig.from_env()
    results = apply_checkpoint_plan(config, mode="prune", keep_n=body.keep_n, job_ids=body.job_ids)
    invalidate_train_server_status()
    deleted = [item for item in results if item["action"] == "deleted"]
    return {
        "dry_run": False,
        "deleted": deleted,
        "freed_mb": sum(item["size_mb"] for item in deleted),
    }


# 直接删除（不进回收站）指定作业的指定 checkpoint step。
# 会保护最新的那个 step —— 续训和回传都还需要它。
@app.delete("/system/train-server/checkpoints")
def delete_train_server_checkpoints(body: CheckpointDeleteRequest):
    """Hard-delete specific checkpoint steps of one of our jobs."""
    from .train_server import apply_checkpoint_plan, invalidate_train_server_status

    if not body.steps:
        raise HTTPException(status_code=400, detail="steps 不能为空")
    payload = train_server_status(force=True)
    if not payload.get("ssh_ok"):
        raise HTTPException(status_code=502, detail=f"训练服务器不可达：{payload.get('error')}")
    job = _checkpoint_job_or_404(payload, body.job_id)
    available = {int(ckpt["step"]) for ckpt in job.get("checkpoints", [])}
    missing = sorted(set(body.steps) - available)
    if missing:
        raise HTTPException(status_code=404, detail=f"作业 {body.job_id} 上没有这些 step：{missing}")
    newest = max(available) if available else None
    if newest is not None and newest in set(body.steps):
        raise HTTPException(
            status_code=409,
            detail=f"step {newest} 是该作业最新的 checkpoint，保留它才能续训/回传",
        )
    config = TrainServerConfig.from_env()
    results = apply_checkpoint_plan(
        config, mode="delete", targets=list(body.steps), job_ids=[body.job_id]
    )
    invalidate_train_server_status()
    deleted = [item for item in results if item["action"] == "deleted"]
    return {"deleted": deleted, "freed_mb": sum(item["size_mb"] for item in deleted)}


# 训练服务器连通性探测的请求体（超时秒数）。
class TrainServerProbeRequest(BaseModel):
    timeout_seconds: float = 8.0


# 跑一次 ssh <host> true（BatchMode）验证免密可达性，供“测试连接”按钮使用。
@app.post("/system/train-server/probe")
def probe_train_server(body: TrainServerProbeRequest | None = None):
    """Run one `ssh <host> true` (BatchMode) to verify key-based reachability."""
    payload = _train_server_payload()
    if not payload.get("configured"):
        raise HTTPException(status_code=400, detail=payload.get("error", "Train server not configured"))
    config = TrainServerConfig.from_env()
    timeout = max(2.0, min(30.0, float(body.timeout_seconds if body is not None else 8.0)))
    argv = [*ssh_base_command(config), config.endpoint, "true"]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "reachable": False,
            "endpoint": config.endpoint,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "detail": str(exc),
        }
    return {
        "reachable": completed.returncode == 0,
        "endpoint": config.endpoint,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "returncode": completed.returncode,
        "detail": (completed.stdout or "").strip()[-500:],
    }


# 检查 LeRobot 训练依赖（accelerate）是否可导入。
@app.get("/system/training-extra")
def get_training_extra():
    """Return whether the LeRobot training extra (accelerate) is importable."""
    return handle_get_training_extra()


# 后台执行 pip install accelerate；已在安装中则直接返回（no-op）。
@app.post("/system/training-extra/install")
def install_training_extra():
    """Spawn `pip install accelerate` as a background subprocess. No-op if already running."""
    return handle_install_training_extra()


# 返回安装状态及待取日志行（读取即清空队列）。
@app.get("/system/training-extra/install-status")
def install_training_extra_status():
    """Return current install state plus any pending log lines (drained on read)."""
    return handle_install_training_extra_status()


# 检查 wandb 包在当前 lelab 进程中是否可导入。
@app.get("/system/wandb-extra")
def get_wandb_extra():
    """Return whether the `wandb` package is importable in this lelab process."""
    return handle_get_wandb_extra()


# 后台执行 pip install wandb；已在安装中则直接返回（no-op）。
@app.post("/system/wandb-extra/install")
def install_wandb_extra():
    """Spawn `pip install wandb` as a background subprocess. No-op if already running."""
    return handle_install_wandb_extra()


# 返回 wandb 安装状态及待取日志行（读取即清空队列）。
@app.get("/system/wandb-extra/install-status")
def install_wandb_extra_status():
    """Return current wandb install state plus any pending log lines (drained on read)."""
    return handle_install_wandb_extra_status()


# 某个策略所需的可选依赖（如 smolvla / pi0 需要 transformers，diffusion 需要 diffusers）
# 是否可导入；核心策略恒为可用。
@app.get("/system/policy-extra/{policy_type}")
def get_policy_extra(policy_type: str):
    """Whether the optional LeRobot extra a policy needs (e.g. transformers for
    smolvla/pi0, diffusers for diffusion) is importable. Core policies report available."""
    return handle_get_policy_extra(policy_type)


# 后台执行 pip install lerobot[<该策略的 extra>]。
@app.post("/system/policy-extra/{policy_type}/install")
def install_policy_extra(policy_type: str):
    """Spawn `pip install lerobot[<extra>]` for the policy's extra in the background."""
    return handle_install_policy_extra(policy_type)


# 返回该策略依赖的安装状态及待取日志行（读取即清空队列）。
@app.get("/system/policy-extra/{policy_type}/install-status")
def install_policy_extra_status(policy_type: str):
    """Return the policy extra's install state plus any pending log lines (drained on read)."""
    return handle_install_policy_extra_status(policy_type)


# 检查 GitHub 上是否有更新的 LeLab commit（带缓存，失败时静默）。
@app.get("/system/update-check")
def update_check():
    """Report whether a newer LeLab commit exists on GitHub (cached, silent on failure)."""
    return handle_update_check()


# 在进程内执行 pip 升级；升级完成后需要用户手动重启 lelab。
@app.post("/system/update")
def run_update():
    """Run the pip upgrade in-process; the user must restart lelab afterwards."""
    return handle_run_update()


# 回放由内嵌的 lerobot/visualize_dataset Space 渲染，后端不需要任何额外路由。
# Replay is rendered by the embedded lerobot/visualize_dataset Space; no backend routes needed.


# ============================================================================
# Calibration endpoints
# 开始网页逐步标定（先取串口租约；启动失败立即归还）。
@app.post("/start-calibration")
def start_calibration(request: CalibrationRequest, http_request: Request):
    """Start calibration process"""
    lease = _acquire_hardware_lease(http_request, "calibration")
    result = calibration_manager.start_calibration(request)
    _release_lease_if_failed(lease, result)
    return result


# 停止标定并释放租约。
@app.post("/stop-calibration")
def stop_calibration():
    """Stop calibration process"""
    result = calibration_manager.stop_calibration_process()
    hardware_lease.release(task="calibration")
    return result


# 查询标定状态（含当前步骤）；把 dataclass 转成可序列化的字典返回。
@app.get("/calibration-status")
def calibration_status():
    """Get current calibration status"""
    from dataclasses import asdict

    status = calibration_manager.get_status()
    return asdict(status)


# 前端点击“下一步”：通知 CalibrationManager 当前步骤完成（内部用 threading.Event 唤醒等待中的标定流程）。
@app.post("/complete-calibration-step")
def complete_calibration_step():
    """Complete the current calibration step"""
    return calibration_manager.complete_step()


# 列出某类设备（robot=从臂 / teleop=主臂）的标定配置文件，含体积与修改时间。
# 注意 device_type 与端口接口里的 leader/follower 是两套词汇，不要混用。
@app.get("/calibration-configs/{device_type}")
def get_calibration_configs(device_type: str):
    """Get all calibration config files for a specific device type"""
    try:
        if device_type == "robot":
            config_path = FOLLOWER_CONFIG_PATH
        elif device_type == "teleop":
            config_path = LEADER_CONFIG_PATH
        else:
            return {"success": False, "message": "Invalid device type"}

        # Get all JSON files in the config directory
        configs = []
        if os.path.exists(config_path):
            for file in os.listdir(config_path):
                if file.endswith(".json"):
                    config_name = os.path.splitext(file)[0]
                    file_path = os.path.join(config_path, file)
                    file_size = os.path.getsize(file_path)
                    modified_time = os.path.getmtime(file_path)

                    configs.append(
                        {
                            "name": config_name,
                            "filename": file,
                            "size": file_size,
                            "modified": modified_time,
                        }
                    )

        return {"success": True, "configs": configs, "device_type": device_type}

    except Exception as e:
        logger.error(f"Error getting calibration configs: {e}")
        return {"success": False, "message": str(e)}


# 删除一个标定配置文件。
# config_name 会被拼进文件名，所以先用 is_valid_robot_name 挡掉路径穿越字符（/ \ ..）；
# FastAPI 的路径参数已经挡住字面 "/"，但挡不住 "\" 和 ".."，属于纵深防御。
@app.delete("/calibration-configs/{device_type}/{config_name}")
def delete_calibration_config(device_type: str, config_name: str):
    """Delete a calibration config file"""
    try:
        if device_type == "robot":
            config_path = FOLLOWER_CONFIG_PATH
        elif device_type == "teleop":
            config_path = LEADER_CONFIG_PATH
        else:
            return {"success": False, "message": "Invalid device type"}

        # config_name is interpolated into a filename, so reject path-traversal
        # characters (/, \, ..) before touching the filesystem. Defense-in-depth:
        # FastAPI path params already block a literal "/", but not "\" or "..".
        # Reuses the same guard already applied to robot-record deletes.
        if not is_valid_robot_name(config_name):
            return {"success": False, "message": "Invalid configuration name"}

        # Construct the file path
        filename = f"{config_name}.json"
        file_path = os.path.join(config_path, filename)

        # Check if file exists
        if not os.path.exists(file_path):
            return {"success": False, "message": "Configuration file not found"}

        # Delete the file
        os.remove(file_path)
        logger.info(f"Deleted calibration config: {file_path}")

        return {
            "success": True,
            "message": f"Configuration '{config_name}' deleted successfully",
        }

    except Exception as e:
        logger.error(f"Error deleting calibration config: {e}")
        return {"success": False, "message": str(e)}


# ============================================================================
# PORT DETECTION ENDPOINTS
# ============================================================================


# 列出当前可用的串口（前端下拉框用）。
@app.get("/available-ports")
def get_available_ports():
    """Get all available serial ports"""
    try:
        ports = find_available_ports()
        return {"status": "success", "ports": ports}
    except Exception as e:
        logger.error(f"Error getting available ports: {e}")
        return {"status": "error", "message": str(e)}


# 这段脚本在全新的 Python 子进程里执行（原因见 _avfoundation_cameras_in_cv2_order）：
# 复刻 OpenCV 在 macOS 上的设备枚举顺序 —— video + muxed 设备按 uniqueID 排序
# （对应 cap_avfoundation_mac.mm），这样返回的下标与 cv2.VideoCapture 实际打开的设备一致。
# Runs in a fresh Python — see _avfoundation_cameras_in_cv2_order for why.
# Mirrors OpenCV's macOS enumeration: video + muxed devices sorted by
# uniqueID (cap_avfoundation_mac.mm), so the returned index matches what
# cv2.VideoCapture will open.
_AVF_ENUM_SCRIPT = """
import json, objc
from Foundation import NSBundle
bundle = NSBundle.bundleWithPath_("/System/Library/Frameworks/AVFoundation.framework")
bundle.load()
types = []
for name in (
    "AVCaptureDeviceTypeBuiltInWideAngleCamera",
    "AVCaptureDeviceTypeExternalUnknown",   # macOS < 14
    "AVCaptureDeviceTypeExternal",          # macOS >= 14
    "AVCaptureDeviceTypeContinuityCamera",  # macOS >= 14
    "AVCaptureDeviceTypeDeskViewCamera",    # macOS >= 13
):
    loaded = {}
    try:
        objc.loadBundleVariables(bundle, loaded, [(name, b"@")])
    except objc.error:
        continue
    if loaded.get(name) is not None:
        types.append(loaded[name])
cls = objc.lookUpClass("AVCaptureDeviceDiscoverySession")
devs = []
for mt in ("vide", "muxx"):
    devs.extend(cls.discoverySessionWithDeviceTypes_mediaType_position_(types, mt, 0).devices() or [])
devs.sort(key=lambda d: d.uniqueID())
print(json.dumps([
    {"index": i, "name": str(d.localizedName()), "unique_id": str(d.uniqueID())}
    for i, d in enumerate(devs)
]))
"""


# macOS 相机枚举（放在子进程里做）：AVFoundation 的进程内设备缓存不会随 USB 热插拔刷新。
# 无论是已废弃的 +devicesWithMediaType: 还是长生命周期的 AVCaptureDeviceDiscoverySession 都会过期，
# 因为插拔通知经 NSNotificationCenter 投递，需要线程里有活跃的 NSRunLoop，而 uvicorn worker 没有。
# 新起一个子进程会让 AVFoundation 重新初始化，从而读到 IOKit 的实时设备状态。
def _avfoundation_cameras_in_cv2_order() -> list[dict[str, Any]]:
    """Enumerate macOS cameras in a fresh Python subprocess.

    AVFoundation's in-process device cache doesn't refresh on USB
    hotplug. Both the deprecated ``+devicesWithMediaType:`` and a
    long-lived ``AVCaptureDeviceDiscoverySession`` go stale, because
    device-connection notifications are delivered via
    ``NSNotificationCenter`` on a thread that needs an active
    ``NSRunLoop`` — uvicorn workers don't run one. A fresh subprocess
    re-initializes AVFoundation, which reads IOKit's live device state
    at startup.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", _AVF_ENUM_SCRIPT],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("AVFoundation enumeration subprocess failed: %s", e)
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.warning("AVFoundation enumeration returned invalid JSON: %s", e)
        return []


# 兜底枚举方案：逐个试探 cv2 下标 0-9，只能给出占位名称。
def _generic_cv2_cameras(backend) -> list[dict[str, Any]]:
    """Last-resort enumeration: probe cv2 indices with placeholder names."""
    import cv2

    cameras: list[dict[str, Any]] = []
    for i in range(10):
        cap = cv2.VideoCapture(i, backend)
        opened = cap.isOpened()
        cap.release()
        if opened:
            cameras.append({"index": i, "name": f"Camera {i}", "available": True})
    return cameras


# Windows 相机枚举：用 pygrabber 拿到 DirectShow 的真实设备名。
# pygrabber 列出的顺序与 cv2 的 DSHOW 后端（录制固定使用的后端）索引顺序一致，
# 因此返回的下标就是 cv2.VideoCapture(i, CAP_DSHOW) 会打开的设备；
# 真实名字让前端能和浏览器的 MediaDeviceInfo.label 对应起来做实时预览。
# pygrabber 不可用时退化为占位名称。
def _windows_cameras() -> list[dict[str, Any]]:
    """Enumerate Windows cameras with their real DirectShow names.

    pygrabber lists DirectShow video devices in the same order cv2's DSHOW
    backend indexes them (which recording is pinned to), so the returned index
    matches what ``cv2.VideoCapture(i, CAP_DSHOW)`` opens. The real names let the
    frontend match each index to the browser's ``MediaDeviceInfo.label`` for the
    live preview. Falls back to generic names if pygrabber is unavailable.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph

        names = FilterGraph().get_input_devices()
    except Exception as e:  # ImportError, or a COM/DirectShow failure
        logger.warning("pygrabber unavailable; using generic camera names: %s", e)
        import cv2

        return _generic_cv2_cameras(cv2.CAP_DSHOW)
    return [{"index": i, "name": name, "available": True} for i, name in enumerate(names)]


# 从 sysfs 读取 /dev/video{index} 的真实名字（Linux，无需额外依赖）。
def _v4l2_camera_name(index: int) -> str | None:
    """Real camera name for /dev/video{index} from sysfs (Linux, no deps)."""
    try:
        with open(f"/sys/class/video4linux/video{index}/name", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


# Linux 相机枚举：不打开设备（避免与正在预览的进程抢设备），只读 sysfs。
# UVC 相机通常暴露两个节点（video 与 metadata）；用稳定的 video-index0 链接挑出采集节点，
# 没有 /dev/v4l 链接时退回读 sysfs 的 per-interface index。
# 因此在预览已经占用设备时，这个接口依然可靠。
def _linux_cameras() -> list[dict[str, Any]]:
    """Enumerate Linux capture nodes without opening busy cameras.

    UVC cameras normally expose two nodes (video and metadata). The stable
    ``video-index0`` links identify the capture node. Falling back to sysfs's
    per-interface ``index`` keeps this working on systems without /dev/v4l
    links. This remains reliable while a preview already owns the device.
    """
    indices: set[int] = set()
    for link_dir in (Path("/dev/v4l/by-id"), Path("/dev/v4l/by-path")):
        if not link_dir.is_dir():
            continue
        for link in link_dir.glob("*-video-index0"):
            with contextlib.suppress(OSError, ValueError):
                name = link.resolve(strict=True).name
                if name.startswith("video"):
                    indices.add(int(name.removeprefix("video")))

    if not indices:
        for node in Path("/sys/class/video4linux").glob("video*"):
            with contextlib.suppress(OSError, ValueError):
                if (node / "index").read_text(encoding="utf-8").strip() == "0":
                    indices.add(int(node.name.removeprefix("video")))

    return [
        {
            "index": index,
            "name": _v4l2_camera_name(index) or f"Camera {index}",
            "available": True,
        }
        for index in sorted(indices)
    ]


# 服务端相机预览的 MJPEG 帧生成器（不经过浏览器 getUserMedia）。
# 典型场景：从另一台电脑通过局域网 HTTP 打开 LeLab —— 此时 getUserMedia 会被判定为
# 非安全上下文而失败，或者更糟：预览到的是客户端自己的摄像头。
# 这里把采集放在 API 主机上，并在浏览器关闭 MJPEG 响应后立即释放设备。
async def _standalone_camera_preview_frames(
    request: Request, camera_index: int, fps: float = 10.0
):
    """Stream a server-attached camera without involving the browser camera API.

    LeLab is commonly opened from a different computer over plain LAN HTTP. In
    that setup ``getUserMedia`` would either be blocked as an insecure context
    or, worse, preview a camera attached to the client instead of the Jetson.
    This generator keeps capture on the API host and releases the device as soon
    as the browser closes the MJPEG response.
    """
    import cv2

    session = _register_camera_preview()
    backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
    capture = None
    try:
        # The route-level check can race with a recording request after the
        # StreamingResponse is created but before this generator starts.
        if session.stop_requested.is_set() or _record.recording_active:
            return
        capture = await asyncio.to_thread(cv2.VideoCapture, camera_index, backend)
        if session.stop_requested.is_set() or _record.recording_active:
            return
        if not capture.isOpened():
            logger.warning("camera-preview: unable to open camera index %s", camera_index)
            return
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        capture.set(cv2.CAP_PROP_FPS, fps)
        if sys.platform.startswith("linux"):
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        interval = 1.0 / max(fps, 1.0)
        while not session.stop_requested.is_set() and not await request.is_disconnected():
            started = time.monotonic()
            ok, frame = await asyncio.to_thread(capture.read)
            if not ok:
                await asyncio.sleep(interval)
                continue
            encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if encoded:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        try:
            if capture is not None:
                # StreamingResponse cancels the generator when an <img> is
                # removed. A cleanup await can be cancelled too, leaving a
                # stale preview session behind. OpenCV release is short and
                # must finish before recording is allowed to claim the device.
                capture.release()
        finally:
            _finish_camera_preview(session)
            logger.info("camera-preview: released camera index %s", camera_index)


# 列出相机，下标顺序与 cv2 录制时使用的顺序一致，并附带真实设备名，
# 便于前端与浏览器的 MediaDeviceInfo.label 匹配做实时预览：
#   macOS   → AVFoundation localizedName（PyObjC 子进程）
#   Windows → DirectShow FriendlyName（pygrabber；录制固定 DSHOW）
#   Linux   → sysfs 中的 v4l2 设备名
# 拿不到真实名字时前端无法匹配，会显示 “No browser match”（issue #12、#16）。
@app.get("/available-cameras")
def get_available_cameras():
    """List cameras with the same index ordering cv2 will use to record.

    Each platform enumerates in the order its cv2 backend indexes devices, and
    pairs each index with the device's real name so the frontend can match it to
    the browser's ``MediaDeviceInfo.label`` for the live preview:
      - macOS: AVFoundation ``localizedName`` (via a PyObjC subprocess);
      - Windows: DirectShow FriendlyName (via pygrabber; recording pinned DSHOW);
      - Linux: the v4l2 device name from sysfs.
    Without real names the frontend can't match a camera and shows "No browser
    match" with an empty device_id (issues #12, #16).
    """
    try:
        import platform

        system = platform.system()

        if system == "Darwin":
            cameras = _avfoundation_cameras_in_cv2_order()
            for cam in cameras:
                cam["available"] = True
            return {"status": "success", "cameras": cameras}
        if system == "Windows":
            return {"status": "success", "cameras": _windows_cameras()}
        if system == "Linux":
            return {"status": "success", "cameras": _linux_cameras()}

        import cv2

        return {"status": "success", "cameras": _generic_cv2_cameras(cv2.CAP_ANY)}
    except ImportError:
        logger.warning("OpenCV not available for camera detection")
        return {"status": "success", "cameras": []}
    except Exception as e:
        logger.error(f"Error detecting cameras: {e}")
        return {"status": "error", "message": str(e), "cameras": []}


# 预览物理接在 API 主机上的相机（MJPEG）；录制或推理期间禁止预览，避免抢占设备。
@app.get("/camera-preview/{camera_index}")
def camera_preview(camera_index: int, request: Request):
    """MJPEG preview for a camera physically attached to the API host."""
    if camera_index < 0 or camera_index > 63:
        raise HTTPException(status_code=400, detail="Invalid camera index")
    if _record.recording_active:
        raise HTTPException(status_code=409, detail="Camera preview is unavailable during recording")
    inference_status = (
        handle_remote_inference_status() if remote_status_owns_route() else handle_inference_status()
    )
    if inference_status.get("inference_active"):
        raise HTTPException(status_code=409, detail="Camera preview is unavailable during inference")
    return StreamingResponse(
        _standalone_camera_preview_frames(request, camera_index),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


# 机械臂角色：leader=主臂（遥操作手柄）/ follower=从臂（执行臂）。
RobotSideLiteral = Literal["leader", "follower"]


# 端口检测请求体：默认检测从臂。
class PortDetectionBody(BaseModel):
    robot_type: RobotSideLiteral = "follower"


# “拔线后检测端口”请求体：传入拔线前的端口快照用于对比。
class PortDisconnectBody(BaseModel):
    ports_before: list[str]


# 保存机器人串口的请求体。
class SaveRobotPortBody(BaseModel):
    robot_type: RobotSideLiteral
    port: str


# 保存机器人标定配置名的请求体。
class SaveRobotConfigBody(BaseModel):
    robot_type: RobotSideLiteral
    config_name: str


# 记录当前端口快照，供随后的 /detect-port-after-disconnect 做差集判断。
@app.post("/start-port-detection")
def start_port_detection(body: PortDetectionBody):
    """Snapshot available ports so the follow-up /detect-port-after-disconnect
    call can diff them."""
    result = find_robot_port(body.robot_type)
    return {"status": "success", "data": result}


# 最长阻塞 15 秒，等待 ports_before 中的某个端口消失，从而确定刚拔下的是哪块板子。
@app.post("/detect-port-after-disconnect")
def detect_port_after_disconnect_endpoint(body: PortDisconnectBody):
    """Block up to 15s waiting for one port from `ports_before` to disappear."""
    try:
        detected_port = detect_port_after_disconnect(body.ports_before)
    except OSError as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    return {"status": "success", "port": detected_port}


# 保存机器人串口，下次自动带出。
@app.post("/save-robot-port")
def save_robot_port_endpoint(body: SaveRobotPortBody):
    """Save a robot port for future use"""
    save_robot_port(body.robot_type, body.port)
    return {"status": "success", "message": f"Port {body.port} saved for {body.robot_type}"}


# 读取已保存的串口与默认串口。
@app.get("/robot-port/{robot_type}")
def get_robot_port(robot_type: RobotSideLiteral):
    """Get the saved port for a robot type"""
    saved_port = get_saved_robot_port(robot_type)
    default_port = get_default_robot_port(robot_type)
    return {"status": "success", "saved_port": saved_port, "default_port": default_port}


# 保存某类设备最近选用的标定配置名。
@app.post("/save-robot-config")
def save_robot_config_endpoint(body: SaveRobotConfigBody):
    """Save a robot configuration for future use"""
    if not config.save_robot_config(body.robot_type, body.config_name):
        raise HTTPException(status_code=500, detail="Failed to save configuration")
    return {"status": "success", "message": f"Configuration saved for {body.robot_type}"}


# 读取已保存 / 默认的标定配置（默认值从 available_configs 里挑选）。
@app.get("/robot-config/{robot_type}")
def get_robot_config(robot_type: RobotSideLiteral, available_configs: str = ""):
    """Get the saved configuration for a robot type"""
    available_configs_list = [c.strip() for c in available_configs.split(",") if c.strip()]
    saved_config = config.get_saved_robot_config(robot_type)
    default_config = config.get_default_robot_config(robot_type, available_configs_list)
    return {"status": "success", "saved_config": saved_config, "default_config": default_config}


# ============================================================================
# ---------------------------- 机器人记录（具名机器人） ----------------------------
# Robot config records (named robots)


# 给返回前端的记录附加 is_clean（配置是否完整）与诊断详情。
def _record_with_clean(record: dict) -> dict:
    """Attach `is_clean` to a record for API responses."""
    diagnostics = robot_record_diagnostics(record)
    return {
        **record,
        "is_clean": diagnostics["complete"],
        "configuration": diagnostics,
    }


# 列出所有已保存的机器人记录。
@app.get("/robots")
def get_robots():
    """List all saved robot records."""
    try:
        records = [_record_with_clean(r) for r in list_robot_records()]
        return {"status": "success", "robots": records}
    except Exception as e:
        logger.error(f"Error listing robots: {e}")
        return {"status": "error", "message": str(e), "robots": []}


# 按名字读取单个机器人记录（先校验名字合法性，防止路径穿越）。
@app.get("/robots/{name}")
def get_robot(name: str):
    """Get a single robot record by name."""
    if not is_valid_robot_name(name):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid robot name"})
    record = get_robot_record(name)
    if record is None:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Robot not found"})
    return {"status": "success", "robot": _record_with_clean(record)}


# 新增或更新机器人记录：
#   ?create=true → “添加机器人”：同名已存在返回 409，否则先建空记录再合并请求体；
#   不带 create  → “打补丁”（例如标定结果回写）：把请求体合并进已有记录；
#                  记录不存在时静默成功（兼容“标定过程中记录被删掉”的边界情况）。
@app.post("/robots/{name}")
def upsert_robot(name: str, data: dict, create: bool = False):
    """
    Upsert a robot record.

    - `?create=true` is the "Add Robot" path: returns 409 if a record with that
      name already exists; otherwise creates with empty fields then merges body.
    - Without `?create=true` is the "patch" path (e.g., calibration write-back):
      merges body into existing record. If no record exists, no-ops and returns
      success — see deletion-during-calibration edge case in the spec.
    """
    if not is_valid_robot_name(name):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid robot name"})
    try:
        if create:
            if get_robot_record(name) is not None:
                return JSONResponse(
                    status_code=409,
                    content={"status": "error", "message": "A robot with this name already exists"},
                )
            save_robot_record(name, data or {}, allow_create=True)
        else:
            save_robot_record(name, data or {}, allow_create=False)
        record = get_robot_record(name)
        if record is None:
            return {"status": "success", "robot": None}
        return {"status": "success", "robot": _record_with_clean(record)}
    except Exception as e:
        logger.error(f"Error upserting robot {name}: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


# 删除机器人记录。
@app.delete("/robots/{name}")
def delete_robot(name: str):
    """Delete a robot record."""
    if not is_valid_robot_name(name):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid robot name"})
    if delete_robot_record(name):
        return {"status": "success"}
    return JSONResponse(status_code=404, content={"status": "error", "message": "Robot not found"})


# 启动钩子：打印 CUDA 环境诊断；把“任务是否还在跑”与“租约失效后怎么停”两个回调
# 交给租约看门狗，并启动看门狗（1 秒一次），使租约在没有任何 HTTP 请求时也能自洽。
@app.on_event("startup")
def startup_event():
    """One-time startup diagnostics surfaced in the server terminal."""
    warn_if_cuda_mismatch()
    # Keep the hardware lease honest even when no HTTP request arrives: TTL
    # expiry and finished tasks must release the serial bus on their own.
    hardware_lease.set_activity_probe(_hardware_task_active)
    hardware_lease.set_stop_hook(_hardware_stop_hook)
    hardware_lease.start_watchdog(interval=1.0)


# 关闭钩子：停看门狗、抢占式释放租约、停广播线程，确保串口与线程都被让出来。
@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources when FastAPI shuts down"""
    logger.info("🔄 FastAPI shutting down, cleaning up...")

    hardware_lease.stop_watchdog()
    hardware_lease.preempt(reason="server-shutdown")

    # Stop any active recording - handled by recording module cleanup

    if manager:
        manager.stop_broadcast_thread()
    logger.info("✅ Cleanup completed")


# 判断 Accept 头是否真的想要 text/html（q 值 > 0）。
# 浏览器导航会给 text/html 正的 q 值 → 返回 SPA 外壳；
# 而 text/html;q=0 是明确拒绝，不能算命中（简单子串匹配会误判）。
# */*（curl、XHR、API 客户端）刻意不算“想要 HTML”。
def _accepts_html(accept: str) -> bool:
    """Whether an Accept header explicitly wants text/html (quality > 0).

    Browser navigations list `text/html` with a positive quality value, so
    they get the SPA shell. A `text/html;q=0` entry is an explicit refusal and
    must not count — a plain substring check would wrongly treat it as a yes.
    `*/*` (curl, XHR, API clients) is deliberately not treated as wanting HTML.
    """
    for part in accept.split(","):
        media_type, _, params = part.strip().partition(";")
        if media_type.strip().lower() != "text/html":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        return quality > 0
    return False


# 支持前端单页路由的静态文件服务：找不到文件时回退到 index.html。
# /recording、/calibration 这类路径只存在于浏览器路由里，磁盘上并没有对应文件，
# 直接刷新或深链会拿到 404（{"detail":"Not Found"}）导致页面白屏。
# 因此这里在 404 且请求方接受 HTML 时返回 index.html，交给前端路由渲染；
# API 拼错、XHR、curl 仍然收到 JSON 404。
class SPAStaticFiles(StaticFiles):
    """StaticFiles that serves index.html for unknown client-side routes.

    The frontend is a single-page app: routes like /recording and /calibration
    exist only in the browser's router, not as files on disk. A hard reload or
    deep link to one of those URLs asks the server for a file that isn't there;
    plain StaticFiles answers 404 ({"detail":"Not Found"}), so the page breaks.

    Here we fall back to index.html on 404 so the SPA boots and its router
    renders the route. Only requests that accept HTML (i.e. browser navigations)
    get the fallback — API typos, XHR, and curl still receive a JSON 404.
    """

    # 先按常规查找文件；仅当 404 且 Accept 含 text/html 时回退到 index.html。
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and _accepts_html(Headers(scope=scope).get("accept", "")):
                return await super().get_response("index.html", scope)
            raise


# 把构建好的前端挂到 /。必须最后挂载，否则它会抢在所有 API 路由之前。
# Serve the built frontend at /. Must be mounted last so API routes win.
if FRONTEND_DIST.exists():
    app.mount("/", SPAStaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    logger.warning(
        f"frontend/dist not found at {FRONTEND_DIST}; run `npm run build` in frontend/ or use `lelab --dev`."
    )
