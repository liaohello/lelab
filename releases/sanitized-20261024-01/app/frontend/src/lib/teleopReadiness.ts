// Operator-confirmed single-arm teleoperation test evidence, shared by the test
// page and the bimanual collection prep page so both read one source of truth.

export type TeleopTestSide = "left" | "right";

export interface TeleopCalibrationFile {
  path: string;
  sha256: string | null;
}

export interface TeleopIdentity {
  side: TeleopTestSide;
  record: string;
  leader_port: string;
  follower_port: string;
  leader_calibration: TeleopCalibrationFile;
  follower_calibration: TeleopCalibrationFile;
  safety: Record<string, number | null>;
  record_complete: boolean;
  issues: { code: string; message: string }[];
  fingerprint: string;
}

export interface TeleopTestEntry {
  fingerprint: string;
  passed: boolean;
  note: string;
  recorded_at: string;
  summary?: { steps: number; clamped_steps: number; elapsed_seconds: number };
}

export type TeleopTestState = "passed" | "failed" | "stale" | "untested";

export interface TeleopSideReadiness {
  side: TeleopTestSide;
  record: string;
  available: boolean;
  reason: string | null;
  identity?: TeleopIdentity;
  test: { state: TeleopTestState; entry: TeleopTestEntry | null; reason?: string };
}

export interface TeleopReadinessResponse {
  sides: Record<TeleopTestSide, TeleopSideReadiness>;
  result_path: string;
}

export const TEST_STATE_LABEL: Record<TeleopTestState, string> = {
  passed: "遥操作测试通过",
  failed: "上次判定异常",
  stale: "结论已过期，需重测",
  untested: "尚未测试",
};

export const TEST_STATE_CLASS: Record<TeleopTestState, string> = {
  passed: "text-emerald-300",
  failed: "text-red-300",
  stale: "text-amber-300",
  untested: "text-gray-400",
};

export async function fetchTeleopReadiness(
  fetchWithHeaders: (url: string, options?: RequestInit) => Promise<Response>,
  baseUrl: string
): Promise<TeleopReadinessResponse | null> {
  try {
    const response = await fetchWithHeaders(`${baseUrl}/teleoperation/test/readiness`);
    if (!response.ok) return null;
    return (await response.json()) as TeleopReadinessResponse;
  } catch {
    return null;
  }
}
