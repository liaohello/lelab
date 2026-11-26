import { apiRequest } from "@/lib/apiClient";

export type CaptureAttempt = {
  attempt_id: string;
  episode_id: string;
  state: string;
  phase: string | null;
  result: string;
  started_time_ns: number | null;
  ended_time_ns: number | null;
  pause_reason?: string | null;
  interrupt_reason?: string | null;
  failure_reason?: string | null;
  note?: string;
  export_eligible?: boolean;
  eligibility_reasons?: string[];
};

export type CaptureTask = {
  task_id: string;
  name: string;
  description: string;
  fps: number;
  target_episodes: number | null;
  mode: "new" | "append";
  append_to: { repo_id: string } | null;
  stage: string;
  saved_count: number;
  attempt_count: number;
  attempts: CaptureAttempt[];
  review_pending: boolean;
  base_capture_raw_only?: boolean;
  awaiting_reset: boolean;
  car_recording_id: string | null;
  pipeline: {
    stage: string;
    job_id?: string;
    repo_id?: string;
    dataset_root?: string;
    total_episodes?: number;
    total_frames?: number;
    error?: string;
    sync_path?: string;
  };
  interrupt_reason?: string | null;
};

export type CaptureStatus = {
  source: string;
  operator_stage: string;
  recording_active: boolean;
  elapsed_s: number | null;
  control: any;
  control_error: { reason: string; detail: string } | null;
  recording: any;
  task: CaptureTask | null;
  attempt: CaptureAttempt | null;
  blocking: { code: string; message: string }[];
  server_wall_ns: number;
};

export type CaptureResponse = {
  request_id: string;
  state: "completed" | "failed" | "processing";
  action: string;
  result?: any;
  error?: { reason: string; detail: string; status_code?: number };
  capture: CaptureStatus;
  replayed?: boolean;
};

export type Fetcher = (url: string, options?: RequestInit) => Promise<Response>;

export function newRequestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `req-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export async function captureStatus(baseUrl: string, fetcher: Fetcher): Promise<CaptureStatus> {
  return apiRequest<CaptureStatus>(baseUrl, fetcher, "/ros-mobile/hybrid/capture/status");
}

export async function captureAction(
  baseUrl: string,
  fetcher: Fetcher,
  action: string,
  payload: Record<string, unknown> = {},
  timeoutMs = 30000,
): Promise<CaptureResponse> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await apiRequest<CaptureResponse>(
      baseUrl, fetcher, `/ros-mobile/hybrid/capture/${action}`,
      { method: "POST", body: { request_id: newRequestId(), ...payload }, signal: controller.signal },
    );
  } finally {
    window.clearTimeout(timer);
  }
}

export type ErrorText = { title: string; next: string };

const REASON_TEXT: Array<[string, string, string]> = [
  ["camera_frame_stale", "相机画面过期", "输出已停止，REC 被阻止。检查相机连接（诊断页），画面恢复新鲜后再开始本集。"],
  ["feedback_untrusted", "六路实测反馈不满足安全门限", "输出已停止。检查从臂反馈年龄与标定绑定，不要继续发送目标。"],
  ["engage_target_not_continuous", "主从臂目标与当前姿态偏差过大", "把主臂调整到接近从臂的姿态，或先检查当前映射，再接合。"],
  ["control_session_changed", "控制会话已换代", "旧集已标记中断并保留证据。先收尾旧采集，再开始新数据集；不会自动续动。"],
  ["service_restarted", "服务重启，旧录制已中断", "旧集保留在车端。点击收尾旧采集后再开始新数据集。"],
  ["review_pending", "还有其他集待审核", "输出已停止。先保存 / 丢弃 / 重录待审核的集，再结束数据集。"],
  ["episode_paused_before_follow", "本集已打开，但跟随未接合", "输出保持关闭。点击恢复跟随会重新对齐并显式接合；不会自动续动。"],
  ["grasp_paused", "抓取边界已记录，跟随已暂停", "输出保持关闭。点击恢复跟随重新对齐并接合，或结束本集。"],
  ["observe_paused", "观察边界已记录，跟随已暂停", "点击恢复跟随，或结束本集。"],
  ["attempt_paused", "本集输出已暂停", "输出保持关闭。恢复跟随需要显式点击；也可以直接结束本集。"],
  ["task_interrupted", "旧采集已中断", "先收尾旧采集（车端原始包保留），再开始新数据集。"],
  ["estop_latched", "急停已锁存", "确认现场安全后清除急停；清除不会自动恢复运动，需要重新获取控制并显式开始。"],
  ["append_incompatible", "追加数据集兼容性检查未通过", "robot_type / 特征 / 关节顺序 / 单位 / 相机 / 数据身份不一致。改为新建数据集，或选择兼容的数据集。"],
  ["append_dataset_unreadable", "无法读取要追加的数据集", "确认数据集 repo_id 与本地路径存在，再重试。"],
  ["dataset_not_open", "当前没有打开的数据集", "先开始数据集，再开始本集。"],
  ["task_already_open", "已有一个采集任务", "先结束或收尾当前数据集，再开始新的。"],
  ["episode_already_open", "本集已经开始", "不要重复开始；页面会按服务端状态显示 REC。"],
  ["no_open_episode", "当前没有打开的集", "先点击 REC 开始本集。"],
  ["reset_not_acknowledged", "复位步骤未确认", "先点击准备下一集，确认复位完成。"],
  ["recover_requires_new_session", "当前会话与旧任务仍一致", "正常结束数据集即可；只有故障换代会话后才需要收尾旧采集。"],
  ["export_failed", "LeRobot 导出失败", "原始数据已同步保留。查看数据页的拒绝原因，修复后重试同步导出。"],
  ["export_timeout", "导出超时", "原始数据已保留。到数据页查看任务状态并重试。"],
  ["car_route_unavailable", "车端缺少所需录制接口", "输出已停止。确认车端 agent 版本与部署一致。"],
  ["leader_serial_missing", "没有检测到 SO-101 主臂", "检查上位机 USB 与主臂连接后重新获取控制。"],
  ["session_not_ready", "控制会话未就绪", "重新获取控制权；页面会自动完成预览与对齐。"],
  ["frame_admission_timeout", "跟随帧未在时限内获得车端确认", "输出已停止。释放故障会话并重新获取控制。"],
  ["upper_recording_storage_unavailable", "上位机录制存储未连接", "连接上位机录制目录后再开始数据集。"],
  ["upper_recording_disk_low", "上位机录制磁盘空间不足", "释放上位机录制目录所在磁盘的空间后再开始数据集。"],
  ["disk_low", "车端磁盘不足", "清理车端空间后再开始数据集；原始包不会被写入。"],
  ["device-bound feedback-limit", "反馈范围版本尚未生效", "输出未开启。到“标定 / 映射 / 范围”保存一次允许范围，再在诊断页完成一次显式接合使版本生效，然后回来开始数据集。"],
  ["feedback_limits_reenable_required", "反馈范围版本与驱动加载不一致", "输出未开启。重新获取控制并显式接合一次，使驱动加载最新保存的范围版本。"],
];

export function reasonText(raw: unknown): ErrorText & { detail: string } {
  const detail = typeof raw === "string" ? raw : raw == null ? "" : JSON.stringify(raw);
  const jointRange = /frame_emitter_feedback_out_of_range:(\w+):raw=([^:\s]+):allowed=(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)/.exec(detail);
  if (jointRange) {
    const [, joint, position, min, max] = jointRange;
    return {
      title: `从臂 ${joint} 超出接合范围`,
      next: `当前读数 ${position}，允许 ${min} 至 ${max}。检查从臂姿态，读数回到范围后再接合。`,
      detail,
    };
  }
  for (const [needle, title, next] of REASON_TEXT) {
    if (detail.includes(needle)) return { title, next, detail };
  }
  return {
    title: detail ? "操作没有完成" : "服务端返回了空错误",
    next: "输出状态以顶部状态条为准；查看详情后按当前步骤按钮继续，不要重复点击。",
    detail,
  };
}

export function formatElapsed(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "00:00";
  const total = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}
