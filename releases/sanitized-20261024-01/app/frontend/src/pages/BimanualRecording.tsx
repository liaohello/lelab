import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AlertTriangle, CheckCircle2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import LandingTopBar from "@/components/landing/LandingTopBar";
import CameraConfiguration, { CameraConfig } from "@/components/recording/CameraConfiguration";
import { useApi } from "@/contexts/ApiContext";
import { useRobots } from "@/hooks/useRobots";
import { useToast } from "@/hooks/use-toast";

const PROFILE_KEY = "lelab.localBimanualProfile.v2";

type ArmPair = {
  leader_port: string;
  follower_port: string;
  leader_config: string;
  follower_config: string;
};

type ValidationDevice = {
  side: string;
  role: string;
  port: string;
  calibration: { file: string; sha256: string; role?: string; side?: string };
};

type ValidationResult = {
  success: boolean;
  error?: string;
  warnings?: string[];
  rig?: { devices: Record<string, ValidationDevice> };
  cameras?: Array<{ name: string; camera_index?: number }>;
};

const readProfile = () => {
  try {
    return JSON.parse(localStorage.getItem(PROFILE_KEY) || "{}");
  } catch {
    return {};
  }
};

const pairFromRecord = (record: {
  leader_port: string;
  follower_port: string;
  leader_config: string;
  follower_config: string;
}): ArmPair => ({
  leader_port: record.leader_port,
  follower_port: record.follower_port,
  leader_config: record.leader_config,
  follower_config: record.follower_config,
});

const BimanualRecording: React.FC = () => {
  const { records, availableNames, isLoading } = useRobots();
  const { baseUrl, fetchWithHeaders } = useApi();
  const navigate = useNavigate();
  const { toast } = useToast();
  const profile = readProfile();

  const [leftName, setLeftName] = useState<string>(profile.left || "");
  const [rightName, setRightName] = useState<string>(profile.right || "");
  const [cameras, setCameras] = useState<CameraConfig[]>(profile.cameras || []);
  const [showCameras, setShowCameras] = useState(false);
  const [dataset, setDataset] = useState(profile.dataset || "so101_bimanual");
  const [task, setTask] = useState(profile.task || "");
  const [episodes, setEpisodes] = useState(profile.episodes || 5);
  const [duration, setDuration] = useState(profile.duration || 60);
  const [fps, setFps] = useState(profile.fps || 30);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [starting, setStarting] = useState(false);
  const releaseStreamsRef = useRef<(() => void) | null>(null);

  const left = records[leftName];
  const right = records[rightName];
  const rig =
    left && right && leftName && rightName && leftName !== rightName
      ? { left: pairFromRecord(left), right: pairFromRecord(right) }
      : null;

  useEffect(() => {
    localStorage.setItem(
      PROFILE_KEY,
      JSON.stringify({
        left: leftName,
        right: rightName,
        cameras,
        dataset,
        task,
        episodes,
        duration,
        fps,
      })
    );
  }, [leftName, rightName, cameras, dataset, task, episodes, duration, fps]);

  useEffect(() => () => releaseStreamsRef.current?.(), []);

  const cameraPayload = useCallback(
    () =>
      Object.fromEntries(
        cameras.map((camera) => [
          camera.name,
          {
            type: camera.type,
            camera_index: camera.camera_index,
            width: camera.width,
            height: camera.height,
            fps: camera.fps,
            backend: camera.backend,
            fourcc: camera.fourcc,
          },
        ])
      ),
    [cameras]
  );

  const validateRig = useCallback(async (): Promise<ValidationResult | null> => {
    if (!rig) return null;
    setValidating(true);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/bimanual/validate`, {
        method: "POST",
        body: JSON.stringify({ rig, cameras: cameraPayload() }),
      });
      const data = (await response.json()) as ValidationResult;
      setValidation(data);
      return data;
    } catch (error) {
      toast({
        title: "核验失败",
        description: `无法连接后端：${String(error)}`,
        variant: "destructive",
      });
      return null;
    } finally {
      setValidating(false);
    }
  }, [baseUrl, cameraPayload, fetchWithHeaders, rig, toast]);

  useEffect(() => {
    setValidation(null);
  }, [leftName, rightName, cameras]);

  const cameraNames = cameras.map((camera) => camera.name);
  const duplicateCameraName = new Set(cameraNames).size !== cameraNames.length;
  const basicReady =
    Boolean(rig) &&
    Boolean(task.trim()) &&
    Boolean(dataset.trim()) &&
    cameras.length > 0 &&
    !duplicateCameraName;

  const start = async () => {
    if (!rig || !basicReady) return;
    if (duplicateCameraName) {
      toast({
        title: "相机名称重复",
        description: "每路相机必须使用唯一名称（例如 front、left_wrist、right_wrist）。",
        variant: "destructive",
      });
      return;
    }
    setStarting(true);
    try {
      const checked = await validateRig();
      if (!checked?.success) {
        toast({
          title: "配置未通过核验",
          description: checked?.error || "四个设备、标定文件或相机绑定不完整。",
          variant: "destructive",
        });
        return;
      }
      releaseStreamsRef.current?.();
      const recordingConfig = {
        bimanual: rig,
        dataset_repo_id: `local/${dataset.trim()}`,
        single_task: task.trim(),
        num_episodes: episodes,
        episode_time_s: duration,
        reset_time_s: 10,
        fps,
        video: true,
        push_to_hub: false,
        resume: false,
        streaming_encoding: true,
        cameras: cameraPayload(),
      };
      const response = await fetchWithHeaders(`${baseUrl}/start-recording`, {
        method: "POST",
        body: JSON.stringify(recordingConfig),
      });
      const data = await response.json();
      if (!response.ok || data.success === false) {
        toast({
          title: "无法开始双臂采集",
          description: data.detail || data.message || "后端拒绝了采集请求。",
          variant: "destructive",
        });
        return;
      }
      navigate("/recording", {
        state: { recordingConfig, sessionStarted: true },
      });
    } catch (error) {
      toast({
        title: "启动失败",
        description: `无法连接后端：${String(error)}`,
        variant: "destructive",
      });
    } finally {
      setStarting(false);
    }
  };

  const devices = validation?.rig?.devices ? Object.values(validation.rig.devices) : [];

  return (
    <div className="min-h-screen bg-black text-white">
      <LandingTopBar />
      <main className="mx-auto max-w-6xl space-y-6 p-6">
        <div>
          <Link to="/" className="text-sm text-gray-400 hover:text-white">
            ← 返回工作台
          </Link>
          <h1 className="mt-4 text-2xl font-semibold">本机双臂采集</h1>
          <p className="mt-2 max-w-3xl text-gray-400">
            选择左右两组已保存的 leader/follower 设备。四台机械臂各自使用本地标定文件；
            采集时左右臂与相机写入同一个 LeRobot 数据集。页面加载不会连接机械臂或打开相机。
          </p>
        </div>

        <section className="grid gap-4 md:grid-cols-2">
          {(["left", "right"] as const).map((side) => {
            const name = side === "left" ? leftName : rightName;
            const record = records[name];
            const setter = side === "left" ? setLeftName : setRightName;
            return (
              <div key={side} className="space-y-3 rounded-xl border border-gray-700 bg-gray-900 p-5">
                <Label htmlFor={`${side}-pair`}>
                  {side === "left" ? "左侧机械臂组" : "右侧机械臂组"}
                </Label>
                <select
                  id={`${side}-pair`}
                  value={name}
                  disabled={isLoading}
                  onChange={(event) => setter(event.target.value)}
                  className="w-full rounded-md border border-gray-600 bg-gray-800 p-2 text-white"
                >
                  <option value="">选择已保存的机械臂组</option>
                  {availableNames.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
                {record ? (
                  <div className="space-y-1 text-sm text-gray-300">
                    <p>示教臂：{record.leader_port || "未配置"}</p>
                    <p>工作臂：{record.follower_port || "未配置"}</p>
                    <p>示教标定：{record.leader_config || "未绑定"}</p>
                    <p>工作标定：{record.follower_config || "未绑定"}</p>
                    <Button
                      variant="outline"
                      className="mt-2"
                      onClick={() => navigate("/calibration", { state: { robot_name: name } })}
                    >
                      查看或逐台标定
                    </Button>
                  </div>
                ) : (
                  <p className="text-sm text-gray-500">尚未选择机械臂组。</p>
                )}
              </div>
            );
          })}
        </section>

        {leftName && rightName && leftName === rightName ? (
          <p className="text-amber-300" role="alert">
            左右两组不能选择同一条设备记录。
          </p>
        ) : null}

        <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-900 p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold">四设备与标定核验</h2>
              <p className="text-sm text-gray-400">
                只读检查：四个独立串口、左右独立标定文件、角色目录与相机唯一性。
              </p>
            </div>
            <Button variant="outline" onClick={validateRig} disabled={!rig || validating}>
              <RefreshCw className={`mr-2 h-4 w-4 ${validating ? "animate-spin" : ""}`} />
              核验配置
            </Button>
          </div>
          {validation && !validation.success ? (
            <div className="flex items-start gap-2 rounded-lg border border-red-700 bg-red-950/40 p-3 text-red-200">
              <AlertTriangle className="mt-0.5 h-4 w-4" />
              <div>
                <p className="font-medium">配置错误</p>
                <p className="text-sm">{validation.error}</p>
              </div>
            </div>
          ) : null}
          {validation?.success ? (
            <div className="grid gap-3 md:grid-cols-2">
              {devices.map((device) => (
                <div
                  key={`${device.side}-${device.role}`}
                  className="rounded-lg border border-gray-700 bg-gray-950 p-3 text-sm"
                >
                  <p className="flex items-center gap-2 font-medium text-green-300">
                    <CheckCircle2 className="h-4 w-4" />
                    {device.side === "left" ? "左" : "右"}
                    {device.role === "leader" ? "示教臂" : "工作臂"}
                  </p>
                  <p className="text-gray-400">端口：{device.port}</p>
                  <p className="break-all text-gray-400">标定：{device.calibration.file}</p>
                  <p className="font-mono text-xs text-gray-500">
                    sha256 {device.calibration.sha256.slice(0, 16)}…
                  </p>
                </div>
              ))}
            </div>
          ) : null}
          {validation?.warnings?.length ? (
            <ul className="list-disc pl-5 text-sm text-amber-300">
              {validation.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
        </section>

        <section className="grid gap-4 rounded-xl border border-gray-700 bg-gray-900 p-5 md:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="bi-dataset">数据集名称</Label>
            <Input
              id="bi-dataset"
              className="border-gray-600 bg-gray-800 text-white"
              value={dataset}
              onChange={(event) =>
                setDataset(event.target.value.replace(/[^A-Za-z0-9._-]/g, "_"))
              }
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="bi-task">任务描述</Label>
            <Input
              id="bi-task"
              className="border-gray-600 bg-gray-800 text-white"
              value={task}
              onChange={(event) => setTask(event.target.value)}
              placeholder="例如：左右臂协作折叠毛巾"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="bi-episodes">示范集数</Label>
            <Input
              id="bi-episodes"
              type="number"
              min={1}
              className="border-gray-600 bg-gray-800 text-white"
              value={episodes}
              onChange={(event) => setEpisodes(Math.max(1, Number(event.target.value)))}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="bi-duration">每集最长秒数</Label>
            <Input
              id="bi-duration"
              type="number"
              min={1}
              className="border-gray-600 bg-gray-800 text-white"
              value={duration}
              onChange={(event) => setDuration(Math.max(1, Number(event.target.value)))}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="bi-fps">目标采集帧率</Label>
            <Input
              id="bi-fps"
              type="number"
              min={1}
              max={120}
              className="border-gray-600 bg-gray-800 text-white"
              value={fps}
              onChange={(event) =>
                setFps(Math.min(120, Math.max(1, Number(event.target.value))))
              }
            />
          </div>
          <p className="self-end text-sm text-gray-400">
            到时自动保存并进入复位；也可保存本集、丢弃重录或结束并保存。采集阶段不加载策略模型。
            左右串口按左后右顺序读写，属于软件同步，不是硬件级同时采样。
          </p>
        </section>

        <section className="space-y-4 rounded-xl border border-gray-700 bg-gray-900 p-5">
          <div>
            <h2 className="font-semibold">采集相机</h2>
            <p className="text-sm text-gray-400">
              相机名称必须唯一（建议 front、left_wrist、right_wrist）。只有点击下方按钮才会扫描和预览设备。
            </p>
          </div>
          {showCameras ? (
            <CameraConfiguration
              cameras={cameras}
              onCamerasChange={setCameras}
              releaseStreamsRef={releaseStreamsRef}
            />
          ) : (
            <Button variant="outline" onClick={() => setShowCameras(true)}>
              配置并预览相机（已保存 {cameras.length} 路）
            </Button>
          )}
        </section>

        {!basicReady ? (
          <p className="text-amber-300" role="status">
            选择左右两组不同设备记录、填写任务并配置至少一路唯一名称的相机后才能开始。
          </p>
        ) : null}

        <Button
          size="lg"
          onClick={start}
          disabled={!basicReady || starting || validating}
          className="w-full bg-green-600 py-6 text-lg font-semibold text-white hover:bg-green-700"
        >
          {starting ? "正在连接并开始采集…" : "开始双臂采集"}
        </Button>
      </main>
    </div>
  );
};

export default BimanualRecording;
