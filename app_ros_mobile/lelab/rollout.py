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

"""Inference mode: drives the SO-101 follower with a trained policy.

Mirrors `app/teleoperating.py` in shape — single global session, mutex
with teleoperation/recording (the follower's serial bus can only be
opened once), `lerobot.scripts.lerobot_rollout` running as a subprocess
for clean cancellation. Hub-checkpoint refs are resolved to a local dir
via huggingface_hub.snapshot_download before we spawn the subprocess.

中文说明（本文件在推理链路中的角色）：这是「编排/进程管理层」，不是推理循环本身。
lelab API 收到启动请求后，本文件把策略引用解析成本地 pretrained_model 目录，组装
`python -m lerobot.scripts.lerobot_rollout --policy.path=... --robot...` 命令并 spawn 子进程。
真正的推理闭环发生在子进程内（见 lerobot/rollout strategy.run）：
    读取各相机图像 + 关节状态 → 按训练时的字段名构造观测 dict（经 processor 归一化）
    → policy.select_action()（ACT 内部动作队列逐弹，见 modeling_act.py）
    → 反归一化还原为真实关节目标 → robot.send_action() 发给从臂。
本文件只负责：子进程生命周期（启动/停止/状态查询）、stdout 日志泵与错误提取、
全局状态机（inference_active 等），并与遥操作/录制互斥（串口总线独占）。
远端推理（target="remote"，PolicyServer + 观测上传）由 lelab/remote_inference.py 负责，
本文件对该目标直接拒绝并引导走 /start-inference 的 remote 流程。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .safety import follower_safety_cli_args
from .utils.config import setup_follower_calibration_file
from .utils.policy_refs import HUB_ROOT_RE, HUB_TREE_RE

logger = logging.getLogger(__name__)


class InferenceRequest(BaseModel):
    """中文说明：前端「开始推理」表单的请求体（pydantic 模型）。
    必填：follower_port（从臂串口）、follower_config（从臂标定配置名）、policy_ref（策略引用）。
    可选：task（任务指令文本）、cameras（各相机配置，键为相机名，与训练时观测字段一致）、
    duration_s（推理时长）、target（local=本机推理 / remote=云端 GPU 推理，见 lelab.remote_inference）、
    policy_type 与 actions_per_chunk / fps / chunk_size_threshold / remote_policy_device / remote_gpu
    （后者为 remote 目标的异步推理调优参数，本地路径不使用）。
    """

    follower_port: str
    follower_config: str
    policy_ref: str  # opaque ref returned by /jobs/{id}/checkpoints 策略引用：本地路径或 Hub checkpoint 引用
    task: str = ""
    cameras: dict[str, dict[str, Any]] = {}
    duration_s: int = 60
    #: "local" runs the whole rollout on this machine; "remote" keeps the
    #: robot client here and puts the policy on the GPU training server
    #: (see lelab.remote_inference).
    target: str = "local"
    #: Policy family, required by the async robot client (--policy_type).
    policy_type: str | None = None
    #: Async-inference tuning (remote target only).
    actions_per_chunk: int = 50
    fps: int = 30
    chunk_size_threshold: float = 0.5
    remote_policy_device: str = "cuda"
    #: GPU index on the train server ("auto" is resolved by the caller).
    remote_gpu: int | None = None


# —— 全局推理状态机（中文说明）——
# inference_active：是否有推理子进程在跑（前端据此轮询/禁用按钮）；与遥操作、录制互斥。
# _inference_proc：子进程句柄；_inference_started_at / _inference_rollout_started_at：
#   进程启动时刻 / 控制主循环真正开始跑的时刻（二者之差 = 加载 policy + 连总线/相机的开销）。
# _inference_meta：本次推理的元信息（策略引用、时长、日志路径），退出时随状态接口返回。
inference_active: bool = False
_inference_proc: subprocess.Popen | None = None
_inference_started_at: float | None = None
_inference_rollout_started_at: float | None = None
_inference_meta: dict[str, Any] = {}
# Guards mutations to the globals above; held only for the short critical
# sections in start/stop/status.
_state_lock = threading.Lock()
_HUB_REF_RE = re.compile(r"^(?P<repo>[^@]+)@checkpoints/(?P<step_dir>\d+)$")
_HUB_ROOT_REF_RE = re.compile(r"^(?P<repo>[^@]+)@root$")
# lerobot prints this once per run, the moment its main control loop is
# about to take over from the setup phase. We watch stdout for it so the
# UI can present a "rollout time" separate from the multi-second policy
# load + bus connect + camera connect setup overhead.
_ROLLOUT_START_MARKER = "Rollout setup complete"


def _pump_stdout(proc: subprocess.Popen, log_handle) -> None:
    """Tee the subprocess's stdout to the log file and watch for the
    rollout-start marker.

    中文说明：后台守护线程，逐行读取子进程 stdout 并写入日志文件；一旦看到
    「Rollout setup complete」标记，就记下 _inference_rollout_started_at——
    它把「策略加载 + 连接机器人/相机」的启动开销与真正的控制主循环时间区分开，
    供状态接口分别汇报。线程结束时负责关闭日志句柄（此后所有权归本线程）。
    """
    global _inference_rollout_started_at
    try:
        for raw in iter(proc.stdout.readline, b""):
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            try:
                log_handle.write(line)
                log_handle.flush()
            except Exception:
                pass
            if _inference_rollout_started_at is None and _ROLLOUT_START_MARKER in line:
                _inference_rollout_started_at = time.time()
                logger.info(
                    "Inference rollout main loop started after %.1fs of setup",
                    _inference_rollout_started_at - (_inference_started_at or _inference_rollout_started_at),
                )
    except Exception as exc:
        logger.exception("Inference stdout pump failed: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            log_handle.close()


def _detect_device() -> str:
    """cuda → mps → cpu, picked once at start time."""
    try:
        import torch

        if torch.cuda.is_available():
            # Generic CUDA wheels can report ``is_available()`` on Jetson even
            # when they contain no kernels for the Orin GPU (sm_87). In that
            # state policy loading succeeds, but the first tensor operation
            # crashes with ``no kernel image is available``. Detect this from
            # the wheel's compiled arch list before launching the rollout.
            if platform.machine().lower() in {"aarch64", "arm64"}:
                major, minor = torch.cuda.get_device_capability(0)
                required_arch = f"sm_{major}{minor}"
                built_arches = set(torch.cuda.get_arch_list())
                if built_arches and required_arch not in built_arches:
                    logger.warning(
                        "CUDA is visible but this PyTorch build lacks %s (built=%s); "
                        "falling back to CPU for local inference",
                        required_arch,
                        sorted(built_arches),
                    )
                    return "cpu"
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _resolve_policy_path(policy_ref: str) -> str:
    """Turn a checkpoints API ref into a local path that lerobot accepts.

    Local refs are already absolute paths to a pretrained_model dir.
    Hub refs look like 'user/repo@checkpoints/<step_dir>' where
    <step_dir> is lerobot's zero-padded directory name (e.g. 000050) — we
    forward it verbatim into snapshot_download's allow_patterns and the
    resolved local path.
    A 'user/repo@root' ref means the whole repo IS the pretrained_model
    (no checkpoints sub-tree); the full repo is downloaded via
    snapshot_download and its root is returned directly.

    中文说明：把前端传来的「策略引用」规整为子进程可用的本地 pretrained_model 目录，
    该目录即 policy 的保存产物（config.json + model.safetensors + 归一化统计），可被
    Policy.from_pretrained 直接加载。三种形式：本地绝对路径（原样返回）、
    'user/repo@checkpoints/000050'（只下载该 step 的 pretrained_model 子树）、
    'user/repo@root'（整个仓库就是 pretrained_model，下载整仓取根目录）。
    """
    if Path(policy_ref).is_dir():
        return policy_ref
    from huggingface_hub import snapshot_download

    m = HUB_TREE_RE.match(policy_ref)
    if m:
        repo_id, step_dir = m.group("repo"), m.group("step_dir")
        local_root = snapshot_download(
            repo_id=repo_id,
            repo_type="model",
            allow_patterns=[f"checkpoints/{step_dir}/pretrained_model/*"],
        )
        return str(Path(local_root) / "checkpoints" / step_dir / "pretrained_model")
    m = HUB_ROOT_RE.match(policy_ref)
    if m:
        return snapshot_download(repo_id=m.group("repo"), repo_type="model")
    raise ValueError(f"Unrecognised policy ref: {policy_ref!r}")


def _read_policy_config(policy_path: str) -> dict[str, Any]:
    """Load pretrained_model/config.json if present."""
    config_path = Path(policy_path) / "config.json"
    if not config_path.is_file():
        return {}
    try:
        with open(config_path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read policy config at %s: %s", config_path, exc)
        return {}


def _rollout_inference_args(policy_path: str) -> list[str]:
    """Return extra lerobot-rollout flags for policies that reject sync inference.

    GR00T and other relative-action policies decode action chunks against the
    observation state at inference time. The default sync backend calls
    ``select_action`` per tick and can re-decode cached relative actions
    against newer states, so lerobot requires the RTC/chunked rollout path
    instead.

    GR00T also needs tuned RTC queue settings: the lerobot default
    ``queue_threshold=30`` makes the background thread replan while a 16-step
    chunk is still playing, which replaces the queue mid-motion and feels
    like one action then a full recalculation."""
    cfg = _read_policy_config(policy_path)
    policy_type = cfg.get("type")
    needs_rtc = bool(cfg.get("use_relative_actions")) or policy_type == "groot"
    if not needs_rtc:
        return []

    args = ["--inference.type=rtc"]

    n_action_steps = cfg.get("n_action_steps")
    if isinstance(n_action_steps, int) and n_action_steps > 0:
        args.append(f"--inference.rtc.execution_horizon={n_action_steps}")
    elif policy_type == "groot":
        args.append("--inference.rtc.execution_horizon=16")

    # Wait until the current chunk is consumed before replanning. GROOT docs
    # recommend keeping this at 0 (never > 5) for stable real-robot rollout.
    args.append("--inference.queue_threshold=0")
    return args


def _format_cameras_arg(cameras: dict[str, dict[str, Any]]) -> str:
    """Convert {name: {type, camera_index, width, height, fps}} into
    lerobot's CLI dict syntax. The frontend key `camera_index` is
    remapped to lerobot's `index_or_path`.

    On Linux, pin OpenCV cameras to V4L2 and default to MJPG. Without this,
    OpenCV commonly opens UVC cameras as YUYV: the current Jetson camera only
    supports 640x480 YUYV at 25 FPS, while MJPG supports the dataset's 30 FPS.
    Recording already applies the same V4L2/MJPG default in ``record.py``;
    inference must use an identical capture mode.

    中文说明：把前端相机表单 {相机名: {type, camera_index, width, height, fps}} 转成
    lerobot 的 --robot.cameras CLI 字典字符串；相机名会成为观测 dict 里的键
    （observation.images.<相机名>），必须与训练数据集一致，否则 policy 拿不到对应图像输入。
    """
    parts = []
    for name, cfg in cameras.items():
        remapped = {
            ("index_or_path" if k == "camera_index" else k): v for k, v in cfg.items() if v is not None
        }
        if remapped.get("type") == "opencv" and platform.system() == "Linux":
            remapped.setdefault("backend", 200)  # Cv2Backends.V4L2
            if remapped.get("backend") in (200, "V4L2"):
                remapped.setdefault("fourcc", "MJPG")
        body = ", ".join(f"{k}: {v}" for k, v in remapped.items())
        parts.append(f"{name}: {{{body}}}")
    return "{" + ", ".join(parts) + "}"


# Exception lines at the tail of a Python traceback look like
# "RuntimeError: ..." or "lerobot.errors.DeviceNotConnectedError: ...".
_EXC_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Timeout|Failure)\b")


def _extract_error_from_log(log_path: str | None) -> str | None:
    """Pull the meaningful error out of a failed rollout's log so the UI can
    show it directly instead of telling the user to open a file in the cache."""
    if not log_path:
        return None
    try:
        # Only the tail matters; avoid materializing a multi-MB verbose log.
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 64 * 1024))
            data = fh.read()
    except OSError:
        return None
    tail = data.decode("utf-8", errors="replace").splitlines()[-50:]
    # Prefer the last exception line + everything after it (the message body).
    exc_idx = next((i for i in range(len(tail) - 1, -1, -1) if _EXC_LINE_RE.match(tail[i])), None)
    if exc_idx is not None:
        snippet = "\n".join(tail[exc_idx:]).strip()
    else:
        non_empty = [ln for ln in tail if ln.strip()]
        snippet = "\n".join(non_empty[-6:]).strip()
    snippet = re.sub(r"\n\s*\n+", "\n", snippet)
    if len(snippet) > 500:
        snippet = snippet[:500].rstrip() + "…"
    return snippet or None


def _friendly_hint(error_text: str | None) -> str | None:
    """A plain-language, actionable headline for the common SO-101 failures."""
    if not error_text:
        return None
    low = error_text.lower()
    if "overload" in low or "torque_enable" in low:
        return (
            "A motor overloaded — usually the gripper holding an object too hard. Release the object / "
            "open the gripper and power-cycle the arm before trying again."
        )
    if "missing motor ids" in low or "motor check failed" in low:
        return (
            "A follower motor isn't responding (often the gripper, id 6). If a skill was holding an object "
            "it likely overloaded — remove it, power-cycle the arm, then try teleoperation first."
        )
    if "could not connect" in low or "failed to connect" in low or "not connected" in low:
        return "Couldn't connect to the arm — make sure it's plugged in, powered on, and on the right port."
    if "frame is too old" in low or "no frame" in low or "frame timeout" in low:
        return (
            "A camera can't keep up — frames are arriving too slowly. Lower its resolution/FPS, "
            "set FOURCC=MJPG, and close other heavy apps, then try again."
        )
    if "failed to set capture_" in low or "actual_width" in low or "actual_height" in low:
        return "A camera doesn't support the configured resolution — open camera settings and click Auto."
    if "failed to set fps" in low or "actual_fps" in low:
        return (
            "A camera opened in a mode that cannot provide the configured FPS. "
            "On Jetson, use V4L2 + MJPG (or lower the camera FPS) and try again."
        )
    if "permission" in low and ("port" in low or "com" in low):
        return "Couldn't open the serial port — close anything else using it, or run `lelab --stop`."
    if "relative-action" in low or "relative chunk actions" in low:
        return (
            "This policy was trained with relative actions and needs RTC (chunked) inference. "
            "Update lelab and try again — recent versions enable that automatically."
        )
    return None


# Errors that mean the policy actually ran and only shutdown/cleanup tripped —
# e.g. disabling torque on a gripper still holding an object. Connection-loss
# errors are deliberately excluded: a mid-run disconnect is a real failure.
_CLEANUP_MARKERS = ("overload", "torque_enable")


def _classify_outcome(rc: int | None, rollout_started: bool, error_text: str | None) -> str:
    """ok | ran_with_warning | failed.

    A non-zero exit *after* the rollout main loop started, where the error is a
    torque-disable/overload on shutdown, means the skill ran but a motor (usually
    the loaded gripper) complained during cleanup — that's a warning, not a
    failure, so the UI shouldn't call a working run "failed"."""
    if not rc:
        return "ok"
    low = (error_text or "").lower()
    if rollout_started and any(marker in low for marker in _CLEANUP_MARKERS):
        return "ran_with_warning"
    return "failed"


def handle_start_inference(request: InferenceRequest) -> dict[str, Any]:
    """Start a one-shot rollout subprocess. Returns a dict — the route
    layer turns it into a JSON response or HTTPException as appropriate.

    中文说明（启动链，本地推理）：
    1) 互斥检查：与遥操作/录制/已有推理互斥（从臂串口总线只能被一个进程打开），通过后立刻
       置 inference_active=True 占位，防止并发请求竞争。
    2) 准备：解析从臂标定文件名（供 --robot.id）、把 policy_ref 解析为本地 pretrained_model 目录。
    3) 组装子进程命令 `lerobot.scripts.lerobot_rollout`：--policy.path / --policy.device
       （自动探测 cuda/mps/cpu）/ --robot.type=so101_follower / --robot.port / --robot.id /
       --task / --duration / 安全限幅参数 / （按需）RTC 分块推理参数，以及 --robot.cameras
       （相机配置字典，键名与训练时观测字段一致）。
    4) Popen 启动子进程：policy 加载（from_pretrained）、机器人与相机连接、以及推理主循环
       （构造观测 dict → policy.select_action → 反归一化 → robot.send_action）都发生在子进程内；
       父进程向 stdin 写入一个换行以跳过标定确认的交互式 input()，避免挂起。
    5) 启动 stdout 泵线程写日志并监听主循环启动标记；成功后记录 _inference_proc /
       _inference_started_at / _inference_meta。任一步失败则回收日志句柄并复位 inference_active。
    注意：target="remote" 在此被拒绝——云端推理须走 lelab/remote_inference.py 的独立接口
    （其内部启动 gRPC PolicyServer，并由 robot_client 的 observation_sender 线程非阻塞上传观测）。
    """
    global inference_active, _inference_proc, _inference_started_at
    global _inference_rollout_started_at, _inference_meta

    # Mutex with teleop and recording: all three drive the same serial bus.
    from . import record as _record, teleoperate as _teleoperate

    with _state_lock:
        if request.target == "remote":
            return {
                "success": False,
                "status_code": 400,
                "message": "Remote inference must be started through /start-inference with target=remote",
            }
        if _teleoperate.teleoperation_active:
            return {
                "success": False,
                "status_code": 409,
                "message": "Teleoperation is currently active. Stop it first.",
            }
        if _record.recording_active:
            return {
                "success": False,
                "status_code": 409,
                "message": "Recording is currently active. Stop it first.",
            }
        if inference_active:
            return {
                "success": False,
                "status_code": 409,
                "message": "Inference is already active. Stop it first.",
            }
        # Claim the slot now so a concurrent caller losing the race sees us.
        inference_active = True

    # Opened partway through setup; the stdout pump thread takes ownership once
    # it starts. Tracked here so a failure before that hand-off can close it
    # instead of leaking the file handle (which on Windows also keeps the log
    # locked).
    log_handle = None
    try:
        # `setup_follower_calibration_file` returns the basename without the
        # .json extension. We need that stripped form for `--robot.id`,
        # because lerobot appends `.json` itself when constructing
        # `calibration_dir / f"{id}.json"`.
        follower_id = setup_follower_calibration_file(request.follower_config)
        policy_path = _resolve_policy_path(request.policy_ref)

        cmd = [
            sys.executable,
            "-m",
            "lerobot.scripts.lerobot_rollout",
            "--strategy.type=base",
            f"--policy.path={policy_path}",
            f"--policy.device={_detect_device()}",
            "--robot.type=so101_follower",
            f"--robot.port={request.follower_port}",
            f"--robot.id={follower_id}",
            f"--task={request.task}",
            f"--duration={request.duration_s}",
            *follower_safety_cli_args(),
            *_rollout_inference_args(policy_path),
        ]
        if request.cameras:
            cmd.append(f"--robot.cameras={_format_cameras_arg(request.cameras)}")

        log_dir = Path.home() / ".cache" / "huggingface" / "lerobot" / "inference_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{int(time.time())}.log"
        log_handle = log_path.open("w", buffering=1)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Feed a single newline into stdin so SOFollower.calibrate()'s
        # `input("Press ENTER to use the calibration file ...")` returns "" and
        # writes the existing calibration to the motors instead of hanging
        # forever waiting for an interactive operator. Subsequent input()
        # calls in the recalibration path get EOF and raise — which is fine,
        # because we never want to enter that path from the UI.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write(b"\n")
            proc.stdin.flush()
            proc.stdin.close()
        except Exception as exc:
            logger.warning("Failed to seed stdin for inference subprocess: %s", exc)
        threading.Thread(
            target=_pump_stdout,
            args=(proc, log_handle),
            name="inference-stdout-pump",
            daemon=True,
        ).start()
        log_handle = None  # pump thread owns and closes it from here on
    except Exception as exc:
        logger.exception("Failed to start inference")
        # Startup failed before the pump thread took over (most often Popen
        # itself). Release the slot and close the log file if we opened it before
        # failing, so the handle isn't leaked.
        if log_handle is not None:
            log_handle.close()
        with _state_lock:
            inference_active = False
        return {"success": False, "status_code": 500, "message": f"Failed to start inference: {exc}"}

    with _state_lock:
        _inference_proc = proc
        _inference_started_at = time.time()
        _inference_rollout_started_at = None
        _inference_meta = {
            "policy_ref": request.policy_ref,
            "duration_s": request.duration_s,
            "log_path": str(log_path),
        }
    logger.info("Inference started: pid=%s policy=%s", proc.pid, policy_path)
    return {"success": True, "message": "Inference started", "log_path": str(log_path)}


def handle_stop_inference() -> dict[str, Any]:
    """中文说明：停止推理并释放设备。先 terminate 子进程，等 5 秒不退出再 kill——
    SIGTERM/终止信号会让子进程内的 strategy.teardown 断开相机、关闭从臂串口并松开力矩；
    没有父进程侧的显式设备释放：GPU 显存、串口、相机都随子进程退出由操作系统回收。
    最后在锁内清空全部全局状态（inference_active / _inference_proc / 计时与元信息）。
    """
    global inference_active, _inference_proc, _inference_started_at
    global _inference_rollout_started_at, _inference_meta

    with _state_lock:
        if not inference_active or _inference_proc is None:
            return {"success": False, "status_code": 409, "message": "No inference is active"}
        proc = _inference_proc

    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Inference did not exit in 5s; killing")
            proc.kill()
            proc.wait()
    except Exception as exc:
        logger.exception("Stop inference: %s", exc)

    with _state_lock:
        inference_active = False
        _inference_proc = None
        _inference_started_at = None
        _inference_rollout_started_at = None
        _inference_meta = {}
    return {"success": True, "message": "Inference stopped"}


def handle_inference_status() -> dict[str, Any]:
    """中文说明：状态查询（前端轮询）。两种分支：
    - 懒清理：若子进程已自行退出（poll() 非 None），就地结束状态机，并按退出码分类结果
      （_classify_outcome：ok / ran_with_warning / failed），失败时从日志尾部提取真实异常
      （_extract_error_from_log）并附上一条可操作的中文/英文建议（_friendly_hint），
      前端无需让用户去翻缓存里的日志文件。
    - 运行中：返回 inference_active、总耗时 elapsed_s、控制主循环耗时 rollout_elapsed_s
      及元信息（策略引用、时长、日志路径）。
    """
    global inference_active, _inference_proc, _inference_started_at
    global _inference_rollout_started_at, _inference_meta

    # Finalise state lazily if the subprocess died on its own.
    with _state_lock:
        proc = _inference_proc
        if proc is not None and proc.poll() is not None:
            rc = proc.returncode
            logger.info("Inference subprocess exited rc=%s", rc)
            finished_meta = _inference_meta
            finished_started = _inference_started_at
            finished_rollout_started = _inference_rollout_started_at
            inference_active = False
            _inference_proc = None
            _inference_started_at = None
            _inference_rollout_started_at = None
            _inference_meta = {}
            # On failure, surface the real error from the log so the UI doesn't
            # have to send the user digging through the cache.
            error = _extract_error_from_log(finished_meta.get("log_path")) if rc else None
            outcome = _classify_outcome(rc, finished_rollout_started is not None, error)
            return {
                "inference_active": False,
                "exited": True,
                "exit_code": rc,
                "outcome": outcome,
                "error": error,
                "hint": _friendly_hint(error),
                "policy_ref": finished_meta.get("policy_ref"),
                "duration_s": finished_meta.get("duration_s"),
                "log_path": finished_meta.get("log_path"),
                "started_at": finished_started,
                "rollout_started_at": finished_rollout_started,
                "rollout_elapsed_s": 0,
                "elapsed_s": 0,
            }
        elapsed = (time.time() - _inference_started_at) if _inference_started_at else 0
        rollout_elapsed = time.time() - _inference_rollout_started_at if _inference_rollout_started_at else 0
        return {
            "inference_active": inference_active,
            "started_at": _inference_started_at,
            "rollout_started_at": _inference_rollout_started_at,
            "elapsed_s": elapsed,
            "rollout_elapsed_s": rollout_elapsed,
            "duration_s": _inference_meta.get("duration_s"),
            "policy_ref": _inference_meta.get("policy_ref"),
            "log_path": _inference_meta.get("log_path"),
        }
