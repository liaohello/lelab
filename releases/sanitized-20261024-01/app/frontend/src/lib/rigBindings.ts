// Shared reading of the saved serial-role bindings for the two rig records.
// Pages use this instead of hard-coding a port or calibration path, so a
// different machine resolves to whatever the operator saved on /hardware.

export type RigSide = "left" | "right";

export interface RigPortBinding {
  role: string;
  serial: string | null;
  current: string;
  proposed: string | null;
}

export interface RigCalibrationCheck {
  exists?: boolean;
  path?: string;
}

export interface RigBinding {
  side: RigSide;
  robot_name: string;
  record_exists: boolean;
  leader_port: RigPortBinding;
  follower_port: RigPortBinding;
  configs: { leader: string; follower: string };
  calibration: {
    complete: boolean;
    issues: { code: string; message: string }[];
    warnings?: { code: string; message: string }[];
    checks?: {
      leader_calibration?: RigCalibrationCheck;
      follower_calibration?: RigCalibrationCheck;
    };
  } | null;
  changes: Record<string, string>;
  status: string;
}

export interface RigBindingsResponse {
  status: string;
  left_robot: string;
  right_robot: string;
  inventory_saved_at: string | null;
  inventory_complete: boolean;
  missing_roles: string[];
  can_apply: boolean;
  bindings: RigBinding[];
}

export interface FollowerTarget {
  side: RigSide;
  robotName: string;
  recordExists: boolean;
  port: string;
  portSource: "record" | "role" | "missing";
  serial: string | null;
  portDisconnected: boolean;
  calibrationPath: string;
  calibrationExists: boolean;
  issues: string[];
}

/** Resolve the follower a read-only or single-arm test should open. */
export function followerTarget(binding: RigBinding): FollowerTarget {
  const check = binding.calibration?.checks?.follower_calibration;
  const calibrationExists = Boolean(check?.exists && check.path);
  const fromRecord = binding.follower_port.current.trim();
  const fromRole = binding.follower_port.proposed?.trim() ?? "";
  const port = fromRecord || fromRole;
  return {
    side: binding.side,
    robotName: binding.robot_name,
    recordExists: binding.record_exists,
    port,
    portSource: fromRecord ? "record" : fromRole ? "role" : "missing",
    serial: binding.follower_port.serial,
    portDisconnected: (binding.calibration?.warnings ?? []).some(
      (warning) => warning.code === "follower_port_disconnected"
    ),
    calibrationPath: calibrationExists ? check?.path ?? "" : "",
    calibrationExists,
    issues: (binding.calibration?.issues ?? [])
      .filter((issue) => issue.code.startsWith("follower") || issue.code === "follower_config_missing")
      .map((issue) => issue.message),
  };
}

export async function fetchRigBindings(
  fetchWithHeaders: (url: string, options?: RequestInit) => Promise<Response>,
  baseUrl: string
): Promise<RigBindingsResponse> {
  const response = await fetchWithHeaders(`${baseUrl}/hardware/rig-bindings`);
  const payload = (await response.json().catch(() => ({}))) as
    | RigBindingsResponse
    | { detail?: string };
  if (!response.ok || !("bindings" in payload)) {
    throw new Error(
      "detail" in payload && payload.detail ? payload.detail : "读取双臂角色绑定失败"
    );
  }
  return payload;
}

export function targetsBySide(
  response: RigBindingsResponse
): Record<RigSide, FollowerTarget | null> {
  const empty: Record<RigSide, FollowerTarget | null> = { left: null, right: null };
  for (const binding of response.bindings) {
    empty[binding.side] = followerTarget(binding);
  }
  return empty;
}
