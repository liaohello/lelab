/**
 * LanderPi hybrid teleop — frontend contract layer.
 *
 * This module is intentionally pure (no React, no fetch). Every gate here is fail-closed:
 * a missing / unparsable field disables the dependent control instead of being read as "OK".
 * The browser never owns a serial port, a control loop or a safety loop; it only sends discrete
 * operator events and renders what the gateway reports.
 */

/** New profile for the joint base+arm+gripper session. Old read-only / locked profiles stay as-is. */
export const HYBRID_PROFILE = "hardware_hybrid_manual_v1";

export const CONTROL_ACTIONS = [
  "preview",
  "align",
  "arm",
  "engage",
  "release_clutch",
  "pause_all",
  "estop",
  "clear_estop",
  "enable_base",
  "lock_base",
] as const;

export const CALIBRATION_ACTIONS = ["validate", "save"] as const;
export const CALIBRATION_KINDS = ["base", "follower", "leader", "mapping", "gamepad", "camera"] as const;
export const RECORDING_ACTIONS = ["start_session", "start_episode", "set_phase", "end_episode", "stop_session"] as const;
export const DATA_ACTIONS = ["sync", "export"] as const;
export const MANUAL_PHASES = ["observe", "grasp"] as const;

const KNOWN_ACTION_SET = new Set<string>([
  ...CONTROL_ACTIONS,
  ...CALIBRATION_ACTIONS,
  ...RECORDING_ACTIONS,
  ...DATA_ACTIONS,
]);
const CONTROL_SET = new Set<string>(CONTROL_ACTIONS);
const CALIBRATION_SET = new Set<string>(CALIBRATION_ACTIONS);
const CALIBRATION_KIND_SET = new Set<string>(CALIBRATION_KINDS);
const RECORDING_SET = new Set<string>(RECORDING_ACTIONS);
const DATA_SET = new Set<string>(DATA_ACTIONS);

/** Actions that need an acquired session/epoch before they are sent. */
const NEEDS_SESSION = new Set<string>([
  "release",
  "preview",
  "align",
  "arm",
  "engage",
  "release_clutch",
  "enable_base",
  "lock_base",
  "clear_estop",
  "start_session",
  "start_episode",
  "set_phase",
  "end_episode",
  "stop_session",
]);

/** Session acquire/release are not control actions and are not in `capabilities.actions`. */
const SESSION_ACTIONS = new Set<string>(["acquire", "release"]);

/**
 * Stopping is allowed even when capabilities/session are unknown: refusing to send a stop is
 * worse than sending one the gateway may reject. It still never enables motion or bypasses limits.
 */
const BEST_EFFORT_STOP = new Set<string>(["pause_all", "estop"]);

/** Feedback sources that may be used for real following / training state. */
export const MEASURED_SOURCE = "servo_position_readback";
/** Sources that must never be presented as measured. */
export const NON_MEASURED_SOURCES = ["command_cache", "model_placeholder", "synthetic"] as const;

export const REQUIRED_FEEDBACK_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"] as const;

export const DEFAULT_STATUS_MAX_AGE_MS = 4000;
export const DEFAULT_SOURCE_AGE_MAX_S = 1.2;
export const DEFAULT_SEQ_HOLD_MAX_S = 2.0;
export const DEFAULT_JOINT_AGE_MAX_S = 0.5;
export const MAX_HYBRID_SESSION_EPOCH = (2 ** 52) - 1;

export type Trust = "measured" | "cache" | "placeholder" | "synthetic" | "unknown";

export interface CapabilityGate {
  ok: boolean;
  reason: string | null;
  actions: Set<string>;
  profiles: string[];
  simulated: boolean;
  contractVersion: number | null;
}

export interface SessionRef {
  session_id: string | null;
  session_epoch: number | null;
  valid: boolean;
}

export interface FeedbackRow {
  name: string;
  sourceKind: string | null;
  trust: Trust;
  positionRad: number | null;
  rawPosition: number | null;
  ageS: number | null;
  valid: boolean;
  error: string | null;
  calibrationHash: string | null;
}

export interface FeedbackGate {
  ok: boolean;
  reason: string | null;
  rows: FeedbackRow[];
  missing: string[];
  untrusted: string[];
  stale: string[];
  invalid: string[];
}

export interface VideoState {
  fresh: boolean;
  reason: string;
  source: string | null;
  seq: number | null;
  seqField: "frame_seq" | "preview_seq" | null;
  sourceAgeS: number | null;
  seqUnchangedS: number | null;
}

const EMPTY_GATE: CapabilityGate = {
  ok: false,
  reason: "未取得能力声明",
  actions: new Set<string>(),
  profiles: [],
  simulated: false,
  contractVersion: null,
};

function asObject(value: unknown): Record<string, any> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, any>)
    : null;
}

function finiteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

/** Readable error text. Never returns "[object Object]". */
export function reasonText(raw: unknown): string {
  if (raw === null || raw === undefined) return "未知错误";
  if (typeof raw === "string") return raw;
  if (typeof raw === "number" || typeof raw === "boolean") return String(raw);
  const record = asObject(raw);
  if (!record) return String(raw);
  const text =
    nonEmptyString(record.reason) ??
    nonEmptyString(record.message) ??
    nonEmptyString(record.detail) ??
    nonEmptyString(record.error);
  if (text) {
    const extra = nonEmptyString(record.detail);
    return extra && extra !== text ? `${text}：${extra}` : text;
  }
  try {
    return JSON.stringify(raw);
  } catch {
    return "无法解析的错误";
  }
}

/**
 * Capabilities must be explicit and recent. `available: false` (the car-side stub shape) is a
 * declared unavailability, not an invitation to guess. An unknown contract version is refused.
 */
export function gateCapabilities(
  caps: unknown,
  options: { nowMs: number; lastOkMs: number | null; maxAgeMs?: number }
): CapabilityGate {
  const maxAgeMs = options.maxAgeMs ?? DEFAULT_STATUS_MAX_AGE_MS;
  if (options.lastOkMs === null) return { ...EMPTY_GATE, reason: "能力声明尚未取得" };
  if (!(options.nowMs - options.lastOkMs <= maxAgeMs)) {
    return { ...EMPTY_GATE, reason: "能力声明已过期" };
  }
  const record = asObject(caps);
  if (!record) return { ...EMPTY_GATE, reason: "能力声明不是对象" };
  const simulated = record.simulated === true;
  if (record.available !== true && !simulated) {
    return { ...EMPTY_GATE, reason: reasonText(record.reason ?? "后端声明 hybrid 能力不可用") };
  }

  const declaredVersion =
    finiteNumber(record.schema_version) ??
    finiteNumber(record.contract_version) ??
    finiteNumber(record.protocol_version) ??
    finiteNumber(record.version);
  if (declaredVersion !== null && declaredVersion !== 1) {
    return { ...EMPTY_GATE, reason: `未知契约版本 ${declaredVersion}，已禁用控制` };
  }

  const rawActions = Array.isArray(record.actions) ? record.actions : null;
  if (rawActions === null) {
    // No allow-list means no control: a profile name alone is not a capability grant.
    return { ...EMPTY_GATE, reason: "能力声明未提供 actions 允许列表" };
  }
  const actions = new Set<string>();
  for (const item of rawActions) {
    if (typeof item === "string" && KNOWN_ACTION_SET.has(item)) actions.add(item);
  }
  if (actions.size === 0) {
    return { ...EMPTY_GATE, reason: "能力声明未允许任何已知 hybrid 动作" };
  }

  const profiles = Array.isArray(record.profiles)
    ? record.profiles.filter((item: unknown): item is string => typeof item === "string" && item.trim() !== "")
    : [];
  if (profiles.length === 0) {
    return { ...EMPTY_GATE, reason: "能力声明未提供 profiles 列表" };
  }

  return {
    ok: true,
    reason: null,
    actions,
    profiles,
    simulated,
    contractVersion: declaredVersion,
  };
}

/** The car/supervisor wire epoch is an unsigned integer below 2^52. */
function wireEpoch(value: unknown): number | null {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    && value <= MAX_HYBRID_SESSION_EPOCH
    ? value
    : null;
}

/** Session identity in the shape every hybrid request must carry. */
export function sessionRef(status: unknown): SessionRef {
  const record = asObject(status);
  const id = nonEmptyString(record?.session_id);
  const epoch = wireEpoch(record?.session_epoch);
  return { session_id: id, session_epoch: epoch, valid: id !== null && epoch !== null };
}

/** True when a reply belongs to a different session/epoch than the one the page holds. */
export function isForeignSession(reply: unknown, current: SessionRef): boolean {
  const record = asObject(reply);
  if (!record) return false;
  const replyId = nonEmptyString(record.session_id);
  const rawEpoch = record.session_epoch;
  const hasEpoch = rawEpoch !== null && rawEpoch !== undefined;
  if (replyId === null && !hasEpoch) return false;
  if (!current.valid) return hasEpoch && wireEpoch(rawEpoch) === null;
  if (replyId !== null && replyId !== current.session_id) return true;
  if (hasEpoch) {
    const replyEpoch = wireEpoch(rawEpoch);
    if (replyEpoch === null || replyEpoch !== current.session_epoch) return true;
  }
  return false;
}

export interface ActionGateInput {
  name: string;
  gate: CapabilityGate;
  session: SessionRef;
  online: boolean;
  activeProfile?: string | null;
}

/** Whether one operator action may be sent right now. */
export function actionAllowed({ name, gate, session, online, activeProfile = null }: ActionGateInput): boolean {
  if (!online) return false;
  if (BEST_EFFORT_STOP.has(name)) return true;
  if (!gate.ok) return false;

  // Acquire needs a usable hybrid capability but no prior session; release needs the session.
  if (SESSION_ACTIONS.has(name)) return name === "acquire" ? true : session.valid;
  // A declared profile is not permission to drive whichever profile happens to be live.
  if (!activeProfile || !gate.profiles.includes(activeProfile)) return false;
  // The backend rejects calibration writes while the single hybrid lease is active.
  if (name === "save" && session.valid) return false;

  // Calibration assets, recording and data jobs are separate top-level capabilities.
  if ((CALIBRATION_SET.has(name) || RECORDING_SET.has(name) || DATA_SET.has(name)) && !gate.actions.has(name)) {
    return false;
  }
  // Recording and archive jobs are sub-tasks of the same session; they never open a lease.
  if ((RECORDING_SET.has(name) || DATA_SET.has(name)) && !session.valid) return false;

  if (CONTROL_SET.has(name) && !gate.actions.has(name)) return false;
  if (NEEDS_SESSION.has(name) && !session.valid) return false;
  return true;
}

export function isKnownControlAction(name: string): boolean {
  return CONTROL_SET.has(name);
}

export function isKnownCalibrationAction(name: string): boolean {
  return CALIBRATION_SET.has(name);
}

export function isKnownCalibrationKind(kind: string): boolean {
  return CALIBRATION_KIND_SET.has(kind);
}

export function isKnownRecordingAction(name: string): boolean {
  return RECORDING_SET.has(name);
}

/** Body for a control event: session context + action only. Never carries motion samples. */
export function controlBody(action: string, session: SessionRef): Record<string, unknown> {
  return { action, session_id: session.session_id, session_epoch: session.session_epoch };
}

/** Body for a phase switch: it must not smuggle any motion/actuator field. */
export function phaseBody(phase: string, session: SessionRef): Record<string, unknown> {
  return { action: "set_phase", phase, session_id: session.session_id, session_epoch: session.session_epoch };
}

/** End-of-episode is always "stop first, then close the episode". */
export function endEpisodeSequence(): ["pause_all", "end_episode"] {
  return ["pause_all", "end_episode"];
}

/** Closing a recorder session follows the same stop-before-wrap-up rule. */
export function stopSessionSequence(): ["pause_all", "stop_session"] {
  return ["pause_all", "stop_session"];
}

function trustOf(sourceKind: string | null, simulated: boolean): Trust {
  if (sourceKind === MEASURED_SOURCE) return "measured";
  if (sourceKind === "command_cache") return "cache";
  if (sourceKind === "model_placeholder") return "placeholder";
  if (sourceKind === "synthetic") return simulated ? "synthetic" : "synthetic";
  return "unknown";
}

const TRUST_LABEL: Record<Trust, string> = {
  measured: "总线实测",
  cache: "指令缓存",
  placeholder: "模型占位",
  synthetic: "合成（仅排练）",
  unknown: "来源未知",
};

export function trustLabel(trust: Trust): string {
  return TRUST_LABEL[trust];
}

function collectFeedbackSamples(feedback: unknown): {
  samples: Record<string, any>;
  envelope: Record<string, any> | null;
} {
  const record = asObject(feedback);
  if (!record) return { samples: {}, envelope: null };
  // The formal T3 snapshot is a list of individually timestamped servo samples.
  if (Array.isArray(record.samples)) {
    const samples: Record<string, any> = {};
    for (const item of record.samples) {
      const sample = asObject(item);
      const name = nonEmptyString(sample?.name);
      if (sample && name) samples[name] = sample;
    }
    return { samples, envelope: record };
  }
  const nested = asObject(record.joints) ?? asObject(record.samples) ?? asObject(record.positions_rad) ?? null;
  if (nested) return { samples: nested, envelope: record };
  // Tolerate a flat {joint1: {...}} map, but only when it actually contains sample-like entries.
  const flat: Record<string, any> = {};
  for (const [key, value] of Object.entries(record)) {
    const sample = asObject(value);
    if (sample && ("source_kind" in sample || "position_rad" in sample || "raw_position" in sample)) {
      flat[key] = sample;
    }
  }
  return { samples: flat, envelope: record };
}

/**
 * Build one row per required joint. Missing samples produce rows with `trust: "unknown"` rather
 * than being silently omitted, so the UI can show exactly what is missing.
 */
export function feedbackRows(feedback: unknown, simulated = false): FeedbackRow[] {
  const { samples, envelope } = collectFeedbackSamples(feedback);
  return REQUIRED_FEEDBACK_JOINTS.map((name) => {
    const sample = asObject(samples[name]);
    if (!sample) {
      return {
        name,
        sourceKind: null,
        trust: "unknown" as Trust,
        positionRad: null,
        rawPosition: null,
        ageS: null,
        valid: false,
        error: "缺失",
        calibrationHash: null,
      };
    }
    const sourceKind = nonEmptyString(sample.source_kind) ?? nonEmptyString(envelope?.source_kind);
    const ageNs = finiteNumber(sample.age_ns);
    const ageS = finiteNumber(sample.age_s) ?? (ageNs === null ? null : ageNs / 1e9);
    const sampleValid = typeof sample.valid === "boolean"
      ? sample.valid
      : envelope?.valid === true || envelope?.trusted === true;
    const trust = trustOf(sourceKind, simulated);
    return {
      name,
      sourceKind,
      trust,
      positionRad: finiteNumber(sample.position_rad),
      rawPosition: finiteNumber(sample.raw_position),
      ageS,
      valid: sampleValid,
      error: nonEmptyString(sample.error),
      calibrationHash:
        nonEmptyString(sample.calibration_hash) ??
        nonEmptyString(envelope?.calibration_hash),
    };
  });
}

/**
 * All six joints must be present, measured, valid and fresh. A single missing/cached/stale joint
 * blocks real following and 9-D export; the UI shows which one and why.
 */
export function gateFeedback(
  feedback: unknown,
  options: { simulated?: boolean; maxAgeS?: number; expectedCalibrationHash?: string | null } = {}
): FeedbackGate {
  const maxAgeS = options.maxAgeS ?? DEFAULT_JOINT_AGE_MAX_S;
  const rows = feedbackRows(feedback, options.simulated === true);
  const missing: string[] = [];
  const untrusted: string[] = [];
  const stale: string[] = [];
  const invalid: string[] = [];

  for (const row of rows) {
    if (row.sourceKind === null) {
      missing.push(row.name);
      continue;
    }
    if (row.trust !== "measured") untrusted.push(`${row.name}:${row.trust}`);
    if (row.ageS === null || row.ageS < 0 || row.ageS > maxAgeS) stale.push(row.name);
    if (!row.valid || row.error !== null) invalid.push(row.name);
    if (row.positionRad === null) invalid.push(row.name);
    if (options.expectedCalibrationHash) {
      if (row.calibrationHash === null) {
        untrusted.push(`${row.name}:calibration_hash_missing`);
      } else if (row.calibrationHash !== options.expectedCalibrationHash) {
        untrusted.push(`${row.name}:calibration_mismatch`);
      }
    }
  }

  if (missing.length > 0) {
    return { ok: false, reason: `缺少反馈：${missing.join(", ")}`, rows, missing, untrusted, stale, invalid };
  }
  if (untrusted.length > 0) {
    return { ok: false, reason: `反馈来源不可用于跟随：${untrusted.join(", ")}`, rows, missing, untrusted, stale, invalid };
  }
  if (invalid.length > 0) {
    return { ok: false, reason: `反馈无效：${[...new Set(invalid)].join(", ")}`, rows, missing, untrusted, stale, invalid };
  }
  if (stale.length > 0) {
    return { ok: false, reason: `反馈过期：${stale.join(", ")}`, rows, missing, untrusted, stale, invalid };
  }
  return { ok: true, reason: null, rows, missing, untrusted, stale, invalid };
}

/** Video sequence: prefer the contract name, accept the car's current `preview_seq` as an alias. */
export function readVideoSeq(video: unknown): { value: number | null; field: "frame_seq" | "preview_seq" | null } {
  const record = asObject(video);
  if (!record) return { value: null, field: null };
  const frame = finiteNumber(record.frame_seq);
  if (frame !== null) return { value: frame, field: "frame_seq" };
  const preview = finiteNumber(record.preview_seq);
  if (preview !== null) return { value: preview, field: "preview_seq" };
  return { value: null, field: null };
}

export interface VideoEvaluationInput {
  video: unknown;
  nowMs: number;
  lastOkMs: number | null;
  lastSeq: number | null;
  lastSeqChangedMs: number | null;
  statusMaxAgeMs?: number;
  sourceAgeMaxS?: number;
  seqHoldMaxS?: number;
}

/**
 * HTTP 200 + a JPEG body is not a fresh frame. Freshness needs an explicit valid/non-stale flag,
 * a source identity, a numeric sequence and a bounded source age; a sequence that has not moved
 * for longer than `seqHoldMaxS` is stale even if the last poll succeeded.
 */
export function evaluateVideo(input: VideoEvaluationInput): VideoState {
  const statusMaxAgeMs = input.statusMaxAgeMs ?? DEFAULT_STATUS_MAX_AGE_MS;
  const sourceAgeMaxS = input.sourceAgeMaxS ?? DEFAULT_SOURCE_AGE_MAX_S;
  const seqHoldMaxS = input.seqHoldMaxS ?? DEFAULT_SEQ_HOLD_MAX_S;

  const record = asObject(input.video);
  const empty: VideoState = {
    fresh: false,
    reason: "未取得视频状态",
    source: null,
    seq: null,
    seqField: null,
    sourceAgeS: null,
    seqUnchangedS: null,
  };
  if (!record) return empty;

  const source = nonEmptyString(record.source);
  const { value: seq, field } = readVideoSeq(record);
  const sourceAgeS = finiteNumber(record.age_s);
  const seqUnchangedS =
    seq !== null && input.lastSeq !== null && seq === input.lastSeq && input.lastSeqChangedMs !== null
      ? Math.max(0, (input.nowMs - input.lastSeqChangedMs) / 1000)
      : seq !== null
        ? 0
        : null;

  const state: VideoState = { fresh: false, reason: "", source, seq, seqField: field, sourceAgeS, seqUnchangedS };

  if (input.lastOkMs === null || !(input.nowMs - input.lastOkMs <= statusMaxAgeMs)) {
    return { ...state, reason: "状态轮询已过期" };
  }
  if (record.valid !== true) return { ...state, reason: "来源未声明有效" };
  if (record.stale !== false) return { ...state, reason: "来源标记过期或未声明" };
  if (source === null) return { ...state, reason: "缺少视频来源标识" };
  if (seq === null) return { ...state, reason: "缺少 frame_seq/preview_seq，无法判断是否为新画面" };
  if (sourceAgeS === null || sourceAgeS < 0 || sourceAgeS > sourceAgeMaxS) {
    return { ...state, reason: "源帧年龄超限" };
  }
  if (seqUnchangedS !== null && seqUnchangedS > seqHoldMaxS) {
    return { ...state, reason: `帧序号已 ${seqUnchangedS.toFixed(1)} s 未变化（可能为缓存画面）` };
  }
  return { ...state, fresh: true, reason: "来源帧在更新" };
}

/** Label for an explicit rehearsal/模拟 capability declaration. */
export function simulationLabel(gate: CapabilityGate): string | null {
  return gate.simulated ? "模拟能力声明（仅用于无硬件联调，不可作为实机验收）" : null;
}

/** The only profiles the hybrid panel may acquire: exactly what the backend declares. */
export function selectableProfiles(gate: CapabilityGate): string[] {
  if (!gate.ok) return [];
  // Never guess `hardware_hybrid_manual_v1` when the list is missing: that is an integration
  // gap to report, not a capability grant.
  return gate.profiles.filter((profile) => profile === HYBRID_PROFILE || gate.simulated);
}

/** Parse the calibration draft. Returns a readable error instead of throwing into render. */
export function parseCalibrationDraft(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  try {
    const value = JSON.parse(text);
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return { ok: false, error: "标定内容必须是 JSON 对象" };
    }
    return { ok: true, value };
  } catch (error: any) {
    return { ok: false, error: `JSON 解析失败：${error?.message ?? "格式错误"}` };
  }
}
