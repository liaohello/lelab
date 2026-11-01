import React, { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useToast } from "@/hooks/use-toast";
import {
  ArrowLeft,
  MoreHorizontal,
  RotateCcw,
  Square,
  SkipForward,
  Play,
  Volume2,
  VolumeX,
} from "lucide-react";
import {
  getMuted,
  setMuted as persistMuted,
  playRecordingStartCue,
  playResetStartCue,
  playAutoAdvanceWarning,
} from "@/lib/recordingAudio";
import { useApi } from "@/contexts/ApiContext";
import UrdfViewer from "@/components/UrdfViewer";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

interface ArmPair {
  leader_port: string;
  follower_port: string;
  leader_config: string;
  follower_config: string;
}

interface RecordingConfig {
  leader_port?: string;
  follower_port?: string;
  leader_config?: string;
  follower_config?: string;
  bimanual?: { left: ArmPair; right: ArmPair };
  cameras?: Record<string, Record<string, unknown>>;
  dataset_repo_id: string;
  single_task: string;
  num_episodes: number;
  episode_time_s: number;
  reset_time_s: number;
  fps: number;
  video: boolean;
  push_to_hub: boolean;
  resume: boolean;
  streaming_encoding: boolean;
}

type Phase =
  | "preparing"
  | "recording"
  | "resetting"
  | "saving"
  | "stopping"
  | "finalizing"
  | "verifying"
  | "completed"
  | "error";

interface JointSnapshot {
  mode: "bimanual" | "single";
  phase: string;
  sequence?: number;
  age_ms?: number;
  stale?: boolean;
  recorded?: boolean;
  frame_index?: number | null;
  episode_index?: number | null;
  timestamp?: number;
  errors?: string[];
  arms?: Record<
    string,
    { joints_rad: Record<string, number>; joints_deg: Record<string, number> }
  >;
  actions?: {
    raw_target?: Record<string, Record<string, number>>;
    sent_target?: Record<string, Record<string, number>>;
  };
}

interface BackendStatus {
  recording_active: boolean;
  current_phase: string;
  mode?: "bimanual" | "single";
  joint_snapshot?: JointSnapshot;
  camera_health?: Array<{
    name: string;
    connected: boolean;
    age_ms: number | null;
    stale: boolean;
    error: string | null;
  }>;
  finalize_state?: {
    state: string;
    verified?: boolean;
    error?: string;
    saved_episodes?: number;
    verification?: Record<string, unknown>;
  };
  saved_episode_details?: Array<{
    episode_index: number;
    frames: number;
    save_seconds: number;
  }>;
  action_semantics?: string;
  sync?: string;
  diagnostics?: { path?: string; error?: string | null };
  current_episode?: number;
  total_episodes?: number;
  saved_episodes?: number;
  phase_elapsed_seconds?: number;
  phase_time_limit_s?: number;
  session_elapsed_seconds?: number;
  session_ended?: boolean;
  dataset_repo_id?: string;
  error?: string;
  cameras?: string[]; // Names of the cameras configured for this session
  available_controls: {
    stop_recording: boolean;
    exit_early: boolean;
    save_episode?: boolean;
    finish_and_save?: boolean;
    rerecord_episode: boolean;
  };
}

const Recording = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { baseUrl, wsBaseUrl, fetchWithHeaders } = useApi();

  // Get recording config from navigation state
  const recordingConfig = location.state?.recordingConfig as RecordingConfig;
  // The bimanual setup page calls /start-recording itself, then opens this page
  // with sessionStarted=true. A page load alone must never connect the arms.
  const startedExternally = Boolean(location.state?.sessionStarted);

  // Backend status state - this is the single source of truth
  const [backendStatus, setBackendStatus] = useState<BackendStatus | null>(
    null
  );
  const [recordingSessionStarted, setRecordingSessionStarted] = useState(false);

  const [optimisticPhase, setOptimisticPhase] = useState<Phase | null>(null);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const [muted, setMutedState] = useState<boolean>(() => getMuted());
  const prevRealPhaseRef = useRef<Phase | null>(null);
  // Bumps on each re-record so the auto-advance warning re-fires for the same episode number.
  const [rerecordTick, setRerecordTick] = useState(0);
  const warningFiredForPhaseRef = useRef<{ phase: Phase | null; episode: number | null; tick: number }>({ phase: null, episode: null, tick: 0 });
  // Guards against React StrictMode double-invocation of the start effect.
  const startInitiatedRef = useRef(false);

  // --- Camera preview layout -------------------------------------------------
  // Aspect ratio (width / height) of the configured cameras, used to size the
  // preview windows without letterboxing. Falls back to 4:3 if unknown.
  const cameraAspect = useMemo(() => {
    const cams = (recordingConfig as unknown as { cameras?: unknown })?.cameras;
    const list = Array.isArray(cams)
      ? cams
      : cams && typeof cams === "object"
      ? Object.values(cams as Record<string, unknown>)
      : [];
    const first = list[0] as { width?: number; height?: number } | undefined;
    if (first?.width && first?.height) return first.width / first.height;
    return 4 / 3;
  }, [recordingConfig]);

  // Measure the space left for the camera windows (via ResizeObserver, so it
  // re-fits on any viewport change) to size them as large as possible while
  // keeping the whole page within one screen (no scroll).
  const [cameraArea, setCameraArea] = useState({ w: 0, h: 0 });
  const cameraAreaObserver = useRef<ResizeObserver | null>(null);
  const cameraAreaRef = useCallback((node: HTMLDivElement | null) => {
    cameraAreaObserver.current?.disconnect();
    cameraAreaObserver.current = null;
    if (node) {
      const ro = new ResizeObserver((entries) => {
        const r = entries[0].contentRect;
        setCameraArea({ w: r.width, h: r.height });
      });
      ro.observe(node);
      cameraAreaObserver.current = ro;
    }
  }, []);

  // Pick the column count and per-window pixel size that maximizes the video
  // area within the measured space, given the camera count and aspect ratio.
  const cameraCount = backendStatus?.cameras?.length ?? 0;
  const cameraWindow = useMemo(() => {
    const { w, h } = cameraArea;
    if (!cameraCount || w <= 0 || h <= 0) return { width: 0, height: 0 };
    const gap = 12; // matches the grid's gap-3
    let best = { width: 0, height: 0, area: -1 };
    for (let cols = 1; cols <= cameraCount; cols++) {
      const rows = Math.ceil(cameraCount / cols);
      const cellW = (w - gap * (cols - 1)) / cols;
      const cellH = (h - gap * (rows - 1)) / rows;
      if (cellW <= 0 || cellH <= 0) continue;
      const width = Math.min(cellW, cellH * cameraAspect);
      const height = width / cameraAspect;
      const area = width * height;
      if (area > best.area) best = { width, height, area };
    }
    return { width: Math.floor(best.width), height: Math.floor(best.height) };
  }, [cameraArea, cameraCount, cameraAspect]);

  const toggleMute = useCallback(() => {
    setMutedState((prev) => {
      const next = !prev;
      persistMuted(next);
      return next;
    });
  }, []);

  // Redirect if no config provided
  useEffect(() => {
    if (!recordingConfig) {
      toast({
        title: "No Configuration",
        description: "Please start recording from the main page.",
        variant: "destructive",
      });
      navigate("/");
    }
  }, [recordingConfig, navigate, toast]);

  // Start recording session when component loads. The ref guard prevents
  // React StrictMode (and any future re-renders) from firing /start-recording
  // twice — the second call returns 409 and bounces the user home.
  useEffect(() => {
    if (recordingConfig && !startInitiatedRef.current) {
      startInitiatedRef.current = true;
      if (startedExternally) {
        setRecordingSessionStarted(true);
      } else {
        startRecordingSession();
      }
    }
    // startRecordingSession is intentionally omitted: re-running this effect
    // on its identity change would re-fire /start-recording.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordingConfig, startedExternally]);

  // Refs so the poll interval below stays stable and reads the latest values
  // without tearing itself down on every state change.
  const optimisticPhaseRef = useRef(optimisticPhase);
  optimisticPhaseRef.current = optimisticPhase;
  const rerecordTickRef = useRef(rerecordTick);
  rerecordTickRef.current = rerecordTick;

  // Poll backend status continuously to stay in sync
  useEffect(() => {
    if (!recordingSessionStarted) return;

    const pollStatus = async () => {
      try {
        const response = await fetchWithHeaders(
          `${baseUrl}/recording-status`
        );
        if (!response.ok) return;
        const status = await response.json();
        setBackendStatus(status);

        const currentOptimistic = optimisticPhaseRef.current;
        if (currentOptimistic && status.current_phase === currentOptimistic) {
          setOptimisticPhase(null);
        }

        const real = status.current_phase as Phase;
        const prev = prevRealPhaseRef.current;
        if (prev !== real) {
          if (real === "recording" && prev !== null) {
            playRecordingStartCue();
          } else if (real === "resetting") {
            playResetStartCue();
          }
          prevRealPhaseRef.current = real;
          warningFiredForPhaseRef.current = { phase: null, episode: null, tick: 0 };
        }

        const elapsed = status.phase_elapsed_seconds || 0;
        const limit = status.phase_time_limit_s || 0;
        const inFinalThreeSeconds = limit > 3 && elapsed >= limit - 3;
        const ep = status.current_episode ?? null;
        const tick = rerecordTickRef.current;
        const warned = warningFiredForPhaseRef.current;
        if (
          inFinalThreeSeconds &&
          currentOptimistic === null &&
          (warned.phase !== real ||
            warned.episode !== ep ||
            warned.tick !== tick)
        ) {
          playAutoAdvanceWarning();
          warningFiredForPhaseRef.current = { phase: real, episode: ep, tick };
        }

        if (!status.recording_active && status.session_ended) {
          // session_ended only becomes true after the worker has finalized and
          // verified the writer. Do not navigate while it is still finalizing.
          if (status.current_phase === "error") {
            const saved = status.saved_episodes || 0;
            toast({
              title: saved > 0 ? "Recording Interrupted" : "Recording Failed",
              description:
                saved > 0
                  ? `${saved} episode(s) were saved before the session failed: ${status.error || "unknown error"}`
                  : status.error || "The recording session failed to start.",
              variant: "destructive",
            });
            if (saved === 0) {
              navigate("/");
              return;
            }
          }
          const datasetInfo = {
            dataset_repo_id:
              status.dataset_repo_id || recordingConfig.dataset_repo_id,
            single_task: recordingConfig.single_task,
            num_episodes: recordingConfig.num_episodes,
            saved_episodes: status.saved_episodes || 0,
            session_elapsed_seconds: status.session_elapsed_seconds || 0,
            finalize: status.finalize_state,
            mode: status.mode,
            diagnostics: status.diagnostics,
          };
          navigate("/upload", { state: { datasetInfo } });
        }
      } catch (error) {
        console.error("Error polling recording status:", error);
      }
    };

    pollStatus();
    const statusInterval = setInterval(pollStatus, 1000);
    return () => clearInterval(statusInterval);
  }, [recordingSessionStarted, recordingConfig, navigate, baseUrl, fetchWithHeaders, toast]);

  const formatTime = (seconds: number): string => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, "0")}:${secs
      .toString()
      .padStart(2, "0")}`;
  };

  const startRecordingSession = async () => {
    try {
      const response = await fetchWithHeaders(`${baseUrl}/start-recording`, {
        method: "POST",
        body: JSON.stringify(recordingConfig),
      });

      const data = await response.json();

      if (response.ok && data.success !== false) {
        setRecordingSessionStarted(true);
        toast({
          title: "Recording Started",
          description: `Started recording ${recordingConfig.num_episodes} episodes`,
        });
      } else {
        toast({
          title: "Error Starting Recording",
          description:
            data.detail ||
            data.message ||
            "Failed to start recording session.",
          variant: "destructive",
        });
        navigate("/");
      }
    } catch (error) {
      toast({
        title: "Connection Error",
        description: "Could not connect to the backend server.",
        variant: "destructive",
      });
      navigate("/");
    }
  };

  const handleExitEarly = useCallback(async () => {
    if (!backendStatus?.available_controls.exit_early) return;
    if (optimisticPhase !== null) return;

    const realPhase = backendStatus.current_phase as Phase;
    const next: Phase | null =
      realPhase === "recording" ? "resetting" :
      realPhase === "resetting" ? "recording" : null;

    if (!next) return;

    setOptimisticPhase(next);

    try {
      const response = await fetchWithHeaders(
        `${baseUrl}/recording-exit-early`,
        { method: "POST" }
      );
      if (!response.ok) {
        const data = await response.json();
        setOptimisticPhase(null);
        toast({
          title: "Error",
          description: data.message,
          variant: "destructive",
        });
      }
    } catch (error) {
      setOptimisticPhase(null);
      toast({
        title: "Connection Error",
        description: "Could not connect to the backend server.",
        variant: "destructive",
      });
    }
  }, [backendStatus, optimisticPhase, baseUrl, fetchWithHeaders, toast]);

  const handleRerecordEpisode = useCallback(async () => {
    if (!backendStatus?.available_controls.rerecord_episode) return;

    try {
      const response = await fetchWithHeaders(
        `${baseUrl}/recording-rerecord-episode`,
        {
          method: "POST",
        }
      );
      const data = await response.json();

      if (response.ok) {
        setRerecordTick((t) => t + 1);
        toast({
          title: "Re-recording Episode",
          description: `Episode ${backendStatus.current_episode} will be re-recorded.`,
        });
      } else {
        toast({
          title: "Error",
          description: data.message,
          variant: "destructive",
        });
      }
    } catch (error) {
      toast({
        title: "Connection Error",
        description: "Could not connect to the backend server.",
        variant: "destructive",
      });
    }
  }, [backendStatus, baseUrl, fetchWithHeaders, toast]);

  const handleStopRecording = useCallback(async () => {
    if (!backendStatus?.available_controls.stop_recording) return;
    try {
      await fetchWithHeaders(`${baseUrl}/stop-recording`, {
        method: "POST",
      });

      toast({
        title: "Stopping recording",
        description: "Finalizing dataset…",
      });
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to stop recording.",
        variant: "destructive",
      });
    }
  }, [backendStatus, baseUrl, fetchWithHeaders, toast]);

  const requestStopRecording = useCallback(() => {
    if (!backendStatus?.available_controls.stop_recording) return;
    setShowStopConfirm(true);
  }, [backendStatus]);

  const confirmStopRecording = useCallback(async () => {
    setShowStopConfirm(false);
    await handleStopRecording();
  }, [handleStopRecording]);

  const handlersRef = useRef({
    handleExitEarly,
    handleRerecordEpisode,
    requestStopRecording,
    showStopConfirm,
  });
  useEffect(() => {
    handlersRef.current = {
      handleExitEarly,
      handleRerecordEpisode,
      requestStopRecording,
      showStopConfirm,
    };
  });

  const sessionReady = recordingSessionStarted && backendStatus !== null;

  useEffect(() => {
    if (!sessionReady) return;

    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        return;
      }
      if (e.key === " " || e.code === "Space" || e.key === "ArrowRight") {
        e.preventDefault();
        handlersRef.current.handleExitEarly();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        handlersRef.current.handleRerecordEpisode();
      } else if (e.key === "Escape") {
        if (handlersRef.current.showStopConfirm) return;
        handlersRef.current.requestStopRecording();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sessionReady]);

  if (!recordingConfig) {
    return (
      <div className="min-h-screen bg-black text-white flex items-center justify-center">
        <div className="text-center">
          <p className="text-lg">No recording configuration found.</p>
          <Button onClick={() => navigate("/")} className="mt-4">
            Return to Home
          </Button>
        </div>
      </div>
    );
  }

  // Show loading state while waiting for backend status
  if (!backendStatus) {
    return (
      <div className="min-h-screen bg-black text-white flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-red-500 mx-auto mb-4"></div>
          <p className="text-lg">Connecting to recording session...</p>
        </div>
      </div>
    );
  }

  const realPhase = backendStatus.current_phase as Phase;
  const currentPhase: Phase = optimisticPhase ?? realPhase;
  const currentEpisode = backendStatus.current_episode ?? 1;
  const totalEpisodes =
    backendStatus.total_episodes ?? recordingConfig.num_episodes;

  const phaseElapsedTime = optimisticPhase
    ? 0
    : backendStatus.phase_elapsed_seconds || 0;
  const phaseTimeLimit =
    currentPhase === "recording"
      ? recordingConfig.episode_time_s
      : currentPhase === "resetting"
      ? recordingConfig.reset_time_s
      : backendStatus.phase_time_limit_s || 0;

  const sessionElapsedTime = backendStatus.session_elapsed_seconds || 0;

  const isBimanual =
    backendStatus.mode === "bimanual" || Boolean(recordingConfig.bimanual);
  const snapshot = backendStatus.joint_snapshot;
  const finalizeState = backendStatus.finalize_state;

  const getStatusText = () => {
    if (currentPhase === "recording") return `RECORDING EPISODE ${currentEpisode}`;
    if (currentPhase === "resetting") return "RESET — GET READY";
    if (currentPhase === "saving") return "SAVING EPISODE";
    if (currentPhase === "stopping") return "STOPPING — SAVING CURRENT SEGMENT";
    if (currentPhase === "finalizing") return "FINALIZING DATASET";
    if (currentPhase === "verifying") return "VERIFYING DATASET";
    if (currentPhase === "preparing") return "PREPARING SESSION";
    if (currentPhase === "error") return "SESSION FAILED";
    return "SESSION COMPLETE";
  };

  const phaseColor =
    currentPhase === "recording"
      ? { dot: "bg-red-500", pill: "bg-red-500/15 text-red-300", timer: "text-green-400", bar: "bg-green-500", button: "bg-green-500 hover:bg-green-600" }
      : currentPhase === "resetting"
      ? { dot: "bg-orange-500", pill: "bg-orange-500/15 text-orange-300", timer: "text-orange-400", bar: "bg-orange-500", button: "bg-orange-500 hover:bg-orange-600" }
      : currentPhase === "finalizing" || currentPhase === "verifying" || currentPhase === "saving" || currentPhase === "stopping"
      ? { dot: "bg-amber-500", pill: "bg-amber-500/15 text-amber-300", timer: "text-amber-400", bar: "bg-amber-500", button: "bg-amber-600" }
      : currentPhase === "error"
      ? { dot: "bg-red-600", pill: "bg-red-600/15 text-red-300", timer: "text-red-400", bar: "bg-red-600", button: "bg-red-700" }
      : { dot: "bg-gray-500", pill: "bg-gray-500/15 text-gray-300", timer: "text-gray-400", bar: "bg-gray-500", button: "bg-gray-500" };

  const primaryLabel =
    currentPhase === "recording"
      ? isBimanual
        ? "保存本集并继续"
        : "End Episode"
      : currentPhase === "resetting"
      ? isBimanual
        ? "进入下一集"
        : "Start Next Episode"
      : currentPhase === "saving" ||
        currentPhase === "stopping" ||
        currentPhase === "finalizing" ||
        currentPhase === "verifying"
      ? "处理中…"
      : "Advance";

  const PrimaryIcon = currentPhase === "recording" ? SkipForward : Play;
  const cameraHealth = backendStatus.camera_health ?? [];

  return (
    <div className="h-screen bg-black text-white p-6 flex flex-col overflow-hidden">
      <div className="max-w-7xl w-full mx-auto flex-1 min-h-0 flex flex-col">
        <div className="mb-3 flex-shrink-0">
          <Button
            onClick={() => navigate("/")}
            variant="outline"
            className="border-gray-500 hover:border-gray-200 text-gray-300 hover:text-white"
          >
            <ArrowLeft className="w-4 h-4 mr-2" />
            Back to Home
          </Button>
        </div>

        <div className="bg-gray-900 rounded-lg border border-gray-700 p-6 flex-1 min-h-0 flex flex-col justify-center">
          <div className="flex justify-end items-center gap-4 mb-3 flex-shrink-0 text-sm text-gray-400">
            <span aria-label={`Episode ${currentEpisode} of ${totalEpisodes}`}>
              Episode <span className="text-white font-semibold">{currentEpisode}</span> / {totalEpisodes}
            </span>
            <span className="font-mono" aria-label={`Total session time ${formatTime(sessionElapsedTime)}`}>
              {formatTime(sessionElapsedTime)}
            </span>
            <Button
              variant="ghost"
              size="icon"
              onClick={toggleMute}
              aria-label={muted ? "Unmute" : "Mute"}
              className="h-8 w-8 text-gray-400 hover:text-white hover:bg-gray-800"
            >
              {muted ? <VolumeX className="w-5 h-5" /> : <Volume2 className="w-5 h-5" />}
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-gray-400 hover:text-white hover:bg-gray-800"
                  aria-label="More actions"
                >
                  <MoreHorizontal className="w-5 h-5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="end"
                onCloseAutoFocus={(e) => e.preventDefault()}
                className="bg-gray-900 border-gray-700 text-white"
              >
                <DropdownMenuItem
                  onClick={handleRerecordEpisode}
                  disabled={!backendStatus.available_controls.rerecord_episode}
                  className="focus:bg-gray-800 focus:text-white"
                >
                  <RotateCcw className="w-4 h-4 mr-2" />
                  {isBimanual ? "丢弃本集并重录" : "Re-record episode"}
                </DropdownMenuItem>
                <DropdownMenuItem
                  onClick={requestStopRecording}
                  disabled={!backendStatus.available_controls.stop_recording}
                  className="text-red-400 focus:bg-gray-800 focus:text-red-300"
                >
                  <Square className="w-4 h-4 mr-2" />
                  {isBimanual ? "结束并保存" : "Stop recording"}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>

          <div className="text-center mb-3 flex-shrink-0">
            <div
              role="status"
              aria-live="polite"
              className={`inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-bold tracking-widest ${phaseColor.pill}`}
            >
              <span className={`w-2 h-2 rounded-full ${phaseColor.dot} ${currentPhase !== "completed" ? "animate-pulse" : ""}`} />
              {getStatusText()}
            </div>
          </div>

          {isBimanual && currentPhase !== "completed" && (
            <div className="mb-3 grid flex-shrink-0 gap-3 md:grid-cols-2">
              {(["left", "right"] as const).map((side) => {
                const arm = snapshot?.arms?.[side];
                const errors = (snapshot?.errors || []).filter((message) =>
                  message.includes(side)
                );
                return (
                  <section
                    key={side}
                    className="rounded-lg border border-gray-700 bg-gray-950/70 p-3"
                  >
                    <header className="mb-2 flex items-center justify-between text-sm">
                      <span className="font-semibold text-white">
                        {side === "left" ? "左工作臂" : "右工作臂"}
                      </span>
                      <span
                        className={
                          !snapshot
                            ? "text-gray-500"
                            : snapshot.stale
                            ? "text-amber-300"
                            : "text-green-300"
                        }
                      >
                        {!snapshot
                          ? "等待同一周期快照"
                          : snapshot.stale
                          ? "快照陈旧"
                          : `同一周期 #${snapshot.sequence}`}
                      </span>
                    </header>
                    {snapshot ? (
                      <>
                        <div className="h-40">
                          <UrdfViewer arm={side} />
                        </div>
                        <div className="mt-2 grid grid-cols-3 gap-1 font-mono text-[11px] text-gray-300">
                          {arm &&
                            Object.entries(arm.joints_deg).map(([name, value]) => (
                              <div key={name} className="truncate">
                                {name}: {value.toFixed(1)}°
                              </div>
                            ))}
                        </div>
                        <p className="mt-1 text-[11px] text-gray-500">
                          {snapshot.recorded
                            ? `帧 #${snapshot.frame_index ?? "?"} · episode ${snapshot.episode_index ?? "?"}`
                            : "复位阶段（不入库）"}
                          {typeof snapshot.age_ms === "number"
                            ? ` · 快照年龄 ${Math.round(snapshot.age_ms)} ms`
                            : ""}
                        </p>
                        {errors.length > 0 && (
                          <p className="mt-1 text-[11px] text-red-300">
                            缺少数据：{errors.join(", ")}（未补零）
                          </p>
                        )}
                      </>
                    ) : (
                      <div className="flex h-40 items-center justify-center rounded border border-dashed border-gray-700 text-sm text-gray-500">
                        尚未收到该臂/相机的同周期数据，不显示零姿态
                      </div>
                    )}
                  </section>
                );
              })}
            </div>
          )}

          {finalizeState && finalizeState.state !== "pending" && (
            <div
              className={`mb-3 flex-shrink-0 rounded-lg border p-3 text-sm ${
                finalizeState.state === "completed"
                  ? "border-green-700 bg-green-950/40 text-green-200"
                  : finalizeState.state === "failed"
                  ? "border-red-700 bg-red-950/40 text-red-200"
                  : "border-amber-700 bg-amber-950/40 text-amber-200"
              }`}
              role="status"
            >
              {finalizeState.state === "finalizing" &&
                "正在停止新帧、编码视频并写出 parquet/meta 收尾文件…"}
              {finalizeState.state === "verifying" &&
                "收尾完成，正在重新打开数据集核对 12 维特征、episode 索引与视频解码…"}
              {finalizeState.state === "completed" &&
                `收尾并校验完成：${finalizeState.saved_episodes ?? 0} 集已保存。`}
              {finalizeState.state === "failed" &&
                `收尾失败：${finalizeState.error || "unknown error"}`}
              {finalizeState.state !== "completed" &&
                finalizeState.state !== "failed" &&
                finalizeState.state !== "finalizing" &&
                finalizeState.state !== "verifying" &&
                `收尾状态：${finalizeState.state}`}
            </div>
          )}

          {cameraHealth.length > 0 && (
            <div className="mb-2 flex flex-shrink-0 flex-wrap justify-center gap-2 text-xs">
              {cameraHealth.map((camera) => (
                <span
                  key={camera.name}
                  className={`rounded-full border px-2 py-0.5 ${
                    camera.connected && !camera.stale
                      ? "border-green-700 text-green-300"
                      : "border-amber-700 text-amber-300"
                  }`}
                >
                  {camera.name}:{" "}
                  {!camera.connected
                    ? camera.error || "未连接"
                    : camera.stale
                    ? "陈旧"
                    : camera.age_ms === null
                    ? "等待首帧"
                    : `${Math.round(camera.age_ms)} ms`}
                </span>
              ))}
            </div>
          )}

          {/* Camera windows for the cameras configured for this session. Shown
              from the preparing phase so the slots are laid out immediately;
              each fills with its live feed once the robot is connected and
              recording/resetting. This is the flexible region: it absorbs the
              space left after the fixed chrome, and cameraWindow sizes each feed
              as large as fits so the whole page stays within one screen. */}
          {backendStatus.cameras &&
            backendStatus.cameras.length > 0 &&
            currentPhase !== "completed" && (
              <div
                ref={cameraAreaRef}
                className="flex-1 min-h-0 flex flex-wrap gap-3 justify-center content-center overflow-hidden mb-3"
              >
                {backendStatus.cameras.map((name) => (
                  <CameraFeed
                    key={name}
                    baseUrl={baseUrl}
                    name={name}
                    live={
                      currentPhase === "recording" ||
                      currentPhase === "resetting" ||
                      currentPhase === "saving" ||
                      currentPhase === "stopping"
                    }
                    width={cameraWindow.width}
                    height={cameraWindow.height}
                  />
                ))}
              </div>
            )}

          <div className="text-center mb-3 flex-shrink-0">
            <div className={`text-7xl font-mono font-bold leading-none ${phaseColor.timer}`}>
              {formatTime(phaseElapsedTime)}
            </div>
            <div className="text-sm text-gray-500 mt-2">
              / {formatTime(phaseTimeLimit)}
            </div>
          </div>

          <div className="w-full bg-gray-800 rounded-full h-1.5 mb-4 flex-shrink-0">
            <div
              className={`h-1.5 rounded-full transition-all duration-500 ${phaseColor.bar}`}
              style={{
                width: `${Math.min((phaseElapsedTime / phaseTimeLimit) * 100, 100)}%`,
              }}
            />
          </div>

          <Button
            onClick={handleExitEarly}
            disabled={
              !backendStatus.available_controls.exit_early ||
              optimisticPhase !== null ||
              currentPhase === "completed"
            }
            className={`w-full flex-shrink-0 text-white font-semibold py-6 text-lg disabled:opacity-50 ${phaseColor.button}`}
          >
            <PrimaryIcon className="w-5 h-5 mr-2" />
            {primaryLabel}
            {currentPhase !== "completed" &&
              currentPhase !== "finalizing" &&
              currentPhase !== "verifying" && (
                <span className="ml-3 px-2 py-0.5 rounded text-xs font-mono bg-black/30 text-white/70">
                  SPACE / →
                </span>
              )}
          </Button>

          {currentPhase === "completed" && (
            <p className="text-center text-sm text-gray-400 mt-6">
              Recording complete — redirecting to upload…
            </p>
          )}
        </div>
      </div>

      <AlertDialog open={showStopConfirm} onOpenChange={setShowStopConfirm}>
        <AlertDialogContent className="bg-gray-900 border-gray-700 text-white">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {isBimanual ? "结束并保存本次采集？" : "Stop recording?"}
            </AlertDialogTitle>
            <AlertDialogDescription className="text-gray-400">
              {isBimanual
                ? "当前非空有效集会先保存，随后编码视频、写出元数据并重新打开数据集校验；完成前会显示收尾状态。之前保存的集不会丢失。"
                : "Saved episodes are kept. The session will end and you'll be taken to the upload page."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="bg-gray-800 border-gray-700 text-white hover:bg-gray-700">
              Keep recording
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmStopRecording}
              className="bg-red-500 hover:bg-red-600 text-white"
            >
              {isBimanual ? "结束并保存" : "Stop"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};

interface CameraFeedProps {
  baseUrl: string;
  name: string;
  // False during the preparing phase: show an empty slot until the camera is
  // connected and streaming. True once recording/resetting, when frames flow.
  live: boolean;
  // Pixel size computed by the parent to fit the available space. The box is
  // sized to the camera's aspect ratio, so the video fills it without letterbox.
  width: number;
  height: number;
}

// Renders one recording camera's window at an explicit size. During preparing it
// shows an empty placeholder; once live it plays the backend MJPEG stream. The
// browser renders a `multipart/x-mixed-replace` response natively in an <img>,
// so we just point it at /camera-feed/{name}. If the stream errors before frames
// flow (camera still warming up), retry with a cache-busting key after a delay.
const CameraFeed: React.FC<CameraFeedProps> = ({
  baseUrl,
  name,
  live,
  width,
  height,
}) => {
  const [reloadKey, setReloadKey] = useState(0);
  const [hasError, setHasError] = useState(false);
  const retryRef = useRef<number | null>(null);

  const src = `${baseUrl}/camera-feed/${encodeURIComponent(name)}?k=${reloadKey}`;

  useEffect(() => {
    return () => {
      if (retryRef.current) window.clearTimeout(retryRef.current);
    };
  }, []);

  const handleError = useCallback(() => {
    setHasError(true);
    if (retryRef.current) window.clearTimeout(retryRef.current);
    retryRef.current = window.setTimeout(() => {
      setHasError(false);
      setReloadKey((k) => k + 1);
    }, 1500);
  }, []);

  // 0 before the first measurement; skip rendering a zero-size box.
  if (width <= 0 || height <= 0) return null;

  return (
    <div
      style={{ width, height }}
      className="relative bg-gray-900 rounded-lg border border-gray-700 overflow-hidden flex items-center justify-center"
    >
      {!live ? (
        <span className="text-gray-500 text-sm">Getting ready…</span>
      ) : hasError ? (
        <span className="text-gray-500 text-sm">Connecting feed…</span>
      ) : (
        <img
          src={src}
          alt={`${name} live feed`}
          onError={handleError}
          className="w-full h-full object-cover"
        />
      )}
      <span className="absolute bottom-2 left-2 px-2 py-0.5 rounded bg-black/60 text-sm text-gray-200">
        {name}
      </span>
    </div>
  );
};

export default Recording;
