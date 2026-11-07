import React, { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { JointUpdate } from "@/hooks/useRealTimeJoints";

export interface JointSample extends JointUpdate {
  receivedAt: number;
}

// URDF joint names as the server broadcasts them, with the lerobot motor name each
// one comes from. The page labels both so a curve can be traced back to the joint
// the calibration file describes.
const JOINTS = [
  { key: "Rotation", motor: "shoulder_pan", label: "肩转" },
  { key: "Pitch", motor: "shoulder_lift", label: "肩抬" },
  { key: "Elbow", motor: "elbow_flex", label: "肘弯" },
  { key: "Wrist_Pitch", motor: "wrist_flex", label: "腕弯" },
  { key: "Wrist_Roll", motor: "wrist_roll", label: "腕转" },
  { key: "Jaw", motor: "gripper", label: "夹爪" },
];

const MEASURED_COLOR = "#38bdf8";
const TARGET_COLOR = "#f59e0b";
const WINDOW_S = 12;

const toDegrees = (radians: number) => (radians * 180) / Math.PI;

interface JointTrackPanelProps {
  samples: JointSample[];
}

/**
 * Leader target against follower measurement, joint by joint, live.
 *
 * This is the view that makes teleoperation checkable rather than felt: a dashed
 * line that moves and a solid line that never catches up means the arm is not
 * following, and the delta readout says by how much. Both curves come from the same
 * server timestamp, so a stall shows up as a flat pair instead of a fake lag.
 */
const JointTrackPanel: React.FC<JointTrackPanelProps> = ({ samples }) => {
  const latest = samples[samples.length - 1];

  const charts = useMemo(() => {
    if (!latest) return [];
    const newest = latest.receivedAt;
    return JOINTS.map(({ key }) => ({
      key,
      rows: samples.map((sample) => ({
        t: Number(((sample.receivedAt - newest) / 1000).toFixed(2)),
        // A side that failed to read carries null, so the curve breaks instead of
        // drawing a zero that never happened.
        measured: sample.joints?.[key] ?? null,
        target: sample.target?.[key] ?? sample.leader?.[key] ?? null,
      })),
    }));
  }, [samples, latest]);

  return (
    <Card className="border-slate-800 bg-slate-900/70">
      <CardHeader className="p-4 pb-2">
        <CardTitle className="text-base">主臂目标 vs 从臂实测</CardTitle>
        <CardDescription className="text-slate-400">
          实线=从臂实测，虚线=主臂读数（同一帧下发给从臂的目标）。最近 {WINDOW_S} 秒。
        </CardDescription>
      </CardHeader>
      <CardContent className="p-4 pt-0">
        {!latest ? (
          <p className="text-sm text-slate-500">
            等待 /ws/joint-data 的关节帧——开始遥操作后这里才会滚动。
          </p>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {charts.map(({ key, rows }) => {
                const joint = JOINTS.find((item) => item.key === key)!;
                const measured = latest.joints?.[key] ?? null;
                const target = latest.target?.[key] ?? latest.leader?.[key] ?? null;
                const error = target === null || measured === null ? null : target - measured;
                return (
                  <div key={key} className="rounded border border-slate-800 bg-slate-950/60 p-2">
                    <div className="flex items-baseline justify-between text-xs">
                      <span className="font-medium text-slate-200">
                        {joint.label} <span className="text-slate-500">{joint.motor}</span>
                      </span>
                      <span className="font-mono text-slate-400">
                        实测 {measured === null ? "缺" : toDegrees(measured).toFixed(1)}°
                        {target !== null && (
                          <span className="text-amber-400">
                            {" "}
                            / 目标 {toDegrees(target).toFixed(1)}°
                          </span>
                        )}
                      </span>
                    </div>
                    <div className="h-24">
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: -28 }}>
                          <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                          <XAxis
                            dataKey="t"
                            stroke="#64748b"
                            tick={{ fontSize: 9 }}
                            domain={[-WINDOW_S, 0]}
                            tickFormatter={(value) => `${Math.round(value)}s`}
                          />
                          <YAxis stroke="#64748b" tick={{ fontSize: 9 }} width={44} tickFormatter={(value) => toDegrees(value).toFixed(0)} />
                          <ChartTooltip
                            contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 11 }}
                            formatter={(value: any, name: any) => [
                              value === null || value === undefined ? "—" : `${toDegrees(Number(value)).toFixed(1)}°`,
                              name,
                            ]}
                            labelFormatter={(value) => `${value}s`}
                          />
                          <Line
                            type="monotone"
                            dataKey="measured"
                            name="实测"
                            stroke={MEASURED_COLOR}
                            strokeWidth={1.6}
                            dot={false}
                            isAnimationActive={false}
                          />
                          <Line
                            type="monotone"
                            dataKey="target"
                            name="主臂目标"
                            stroke={TARGET_COLOR}
                            strokeDasharray="4 3"
                            strokeWidth={1.2}
                            dot={false}
                            isAnimationActive={false}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                    <div className="mt-1 flex items-center justify-between text-[11px]">
                      <span className="text-slate-500">跟随误差</span>
                      <span
                        className={`font-mono ${
                          error === null
                            ? "text-slate-500"
                            : Math.abs(toDegrees(error)) > 10
                              ? "text-red-400"
                              : "text-green-400"
                        }`}
                      >
                        {error === null ? "无读数" : `${toDegrees(error).toFixed(1)}°`}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="mt-2 flex items-center gap-2 text-[11px] text-slate-500">
              <LegendIcon color={MEASURED_COLOR} label="从臂实测" />
              <LegendIcon color={TARGET_COLOR} label="主臂目标（虚线）" dashed />
              <span className="ml-auto font-mono">
                {samples.length} 帧 · 最新 {new Date(latest.receivedAt).toLocaleTimeString()}
                {" · 从臂 "}
                <StatusWord value={latest.follower_status} missing={latest.follower_missing} />
                {" · 主臂 "}
                <StatusWord value={latest.leader_status} missing={latest.leader_missing} />
              </span>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
};

/** Says which side is missing data rather than letting a gap pass for a pose. */
const StatusWord: React.FC<{ value?: string; missing?: string[] }> = ({ value, missing }) => {
  const label =
    !value || value === "measured"
      ? "实测"
      : value === "read_failed"
        ? "读数失败"
        : `残缺${missing && missing.length ? `（缺 ${missing.slice(0, 3).join(", ")}${missing.length > 3 ? "…" : ""}）` : ""}`;
  const tone = !value || value === "measured" ? "text-green-400" : "text-red-400";
  return <span className={tone}>{label}</span>;
};

const LegendIcon: React.FC<{ color: string; label: string; dashed?: boolean }> = ({ color, label, dashed }) => (
  <span className="flex items-center gap-1">
    <span
      className="inline-block h-0 w-6 border-t-2"
      style={{ borderColor: color, borderStyle: dashed ? "dashed" : "solid" }}
    />
    {label}
  </span>
);

export default JointTrackPanel;
