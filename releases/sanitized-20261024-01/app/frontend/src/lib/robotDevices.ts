// Per-device (leader / follower) state read out of a saved robot record.
// Calibration-file presence, port presence and hardware validation are three
// different facts and are never collapsed into "ready to use".

import type { RobotRecord } from "@/hooks/useRobots";

export type DeviceKind = "leader" | "follower";

export type DeviceState =
  | "port_missing"
  | "port_disconnected"
  | "uncalibrated"
  | "needs_check"
  | "calibrated";

export interface DeviceStatus {
  kind: DeviceKind;
  label: string;
  port: string;
  connected: boolean | null;
  calibrationName: string;
  calibrationExists: boolean;
  calibrationPath: string;
  issues: string[];
  state: DeviceState;
}

export const STATE_LABEL: Record<DeviceState, string> = {
  port_missing: "端口未配置",
  port_disconnected: "端口未连接",
  uncalibrated: "未标定",
  needs_check: "需要检查",
  calibrated: "标定文件存在",
};

export const STATE_CLASS: Record<DeviceState, string> = {
  port_missing: "text-red-300",
  port_disconnected: "text-amber-300",
  uncalibrated: "text-amber-300",
  needs_check: "text-amber-300",
  calibrated: "text-emerald-300",
};

const readCheck = (
  record: RobotRecord | undefined,
  key: string
): { exists?: boolean; path?: string; configured?: boolean; value?: string } => {
  const checks = record?.configuration?.checks as
    | Record<string, { exists?: boolean; path?: string }>
    | undefined;
  return checks?.[key] ?? {};
};

const warnsAbout = (record: RobotRecord | undefined, code: string): boolean =>
  Boolean(record?.configuration?.warnings?.some((warning) => warning.code === code));

function deviceFor(record: RobotRecord | undefined, kind: DeviceKind): DeviceStatus {
  const portField = kind === "leader" ? "leader_port" : "follower_port";
  const calibField = `${kind}_calibration`;
  const port = (record?.[portField] as string) ?? "";
  const check = readCheck(record, calibField);
  const calibrationExists = Boolean(check.exists);
  const issues = (record?.configuration?.issues ?? [])
    .filter((issue) => issue.code.startsWith(kind))
    .map((issue) => issue.message);

  let state: DeviceState;
  if (!port) state = "port_missing";
  else if (warnsAbout(record, `${portField}_disconnected`)) state = "port_disconnected";
  else if (!calibrationExists) state = "uncalibrated";
  else if (record && record.configuration && !record.configuration.complete)
    state = "needs_check";
  else state = "calibrated";

  return {
    kind,
    label: kind === "leader" ? "示教臂" : "工作臂",
    port,
    connected: port ? !warnsAbout(record, `${portField}_disconnected`) : null,
    calibrationName: (record?.[`${kind}_config`] as string) ?? "",
    calibrationExists,
    calibrationPath: check.path ?? "",
    issues,
    state,
  };
}

export function deviceStatuses(record?: RobotRecord | null): DeviceStatus[] {
  return [deviceFor(record ?? undefined, "leader"), deviceFor(record ?? undefined, "follower")];
}
