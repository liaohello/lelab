import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Download, Link2, RefreshCw, Save, Usb } from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * Read-only serial-port inventory for identifying physical arm roles.
 * The operator unplugs one cable at a time; vanished ports stay on screen so
 * the corresponding role can be assigned before plugging the arm back in.
 */

type Role =
  | "unassigned"
  | "left_follower"
  | "right_follower"
  | "left_leader"
  | "right_leader"
  | "spare";

interface Port {
  id: string;
  by_id: string | null;
  device: string | null;
  source: string | null;
  serial: string | null;
  vid: string | null;
  pid: string | null;
  manufacturer: string | null;
  product: string | null;
  usb_path: string | null;
  present: boolean;
  role: Role;
  note: string;
  last_seen?: string | null;
}

interface InventoryResponse {
  status: string;
  ports: Port[];
  saved_at: string | null;
  latest_path: string;
  required_roles: Role[];
  missing_roles: Role[];
  complete: boolean;
  counts: { total: number; present: number; assigned_required: number };
}

interface InventoryEvent {
  time: string;
  kind: "insert" | "remove";
  label: string;
  reconnected: boolean;
}

interface RigPortBinding {
  role: Role;
  serial: string | null;
  current: string;
  proposed: string | null;
}

interface RigBinding {
  side: "left" | "right";
  robot_name: string;
  record_exists: boolean;
  leader_port: RigPortBinding;
  follower_port: RigPortBinding;
  configs: { leader: string; follower: string };
  calibration: {
    complete: boolean;
    issues: { code: string; message: string }[];
    warnings?: { code: string; message: string }[];
  } | null;
  changes: Record<string, string>;
  status: string;
}

interface RigBindingsResponse {
  status: string;
  left_robot: string;
  right_robot: string;
  inventory_saved_at: string | null;
  inventory_complete: boolean;
  missing_roles: Role[];
  can_apply: boolean;
  bindings: RigBinding[];
}

const ROLE_OPTIONS: { value: Role; label: string }[] = [
  { value: "unassigned", label: "未分配" },
  { value: "left_follower", label: "左从臂（执行）" },
  { value: "right_follower", label: "右从臂（执行）" },
  { value: "left_leader", label: "左主臂（遥操作）" },
  { value: "right_leader", label: "右主臂（遥操作）" },
  { value: "spare", label: "备用" },
];
const ROLE_VALUES = new Set<Role>(ROLE_OPTIONS.map((option) => option.value));
const REQUIRED_ROLES: Role[] = [
  "left_follower",
  "right_follower",
  "left_leader",
  "right_leader",
];

const normalizeRole = (value: string | null | undefined): Role =>
  value && ROLE_VALUES.has(value as Role) ? (value as Role) : "unassigned";

const portLabel = (port: Port): string =>
  port.serial || port.by_id || port.device || port.id;

const roleLabel = (role: Role): string =>
  ROLE_OPTIONS.find((option) => option.value === role)?.label ?? role;

const BINDING_STATUS_LABELS: Record<string, string> = {
  ready: "待应用",
  unchanged: "端口一致",
  record_missing: "机器人记录不存在",
  roles_incomplete: "角色不完整",
};

const bindingStatusLabel = (status: string): string =>
  BINDING_STATUS_LABELS[status] ?? status;

const HardwareInventory: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [ports, setPorts] = useState<Port[]>([]);
  const [roles, setRoles] = useState<Record<string, Role>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [events, setEvents] = useState<InventoryEvent[]>([]);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [latestPath, setLatestPath] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const [rigBindings, setRigBindings] = useState<RigBindingsResponse | null>(null);
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const [bindingsApplying, setBindingsApplying] = useState(false);
  const [bindingMessage, setBindingMessage] = useState<string | null>(null);
  const [bindingError, setBindingError] = useState<string | null>(null);

  const portsRef = useRef<Port[]>([]);
  const firstFetchRef = useRef(true);
  const inFlightRef = useRef(false);

  const loadBindings = useCallback(async () => {
    setBindingsLoading(true);
    setBindingError(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/hardware/rig-bindings`);
      const payload = (await response.json().catch(() => ({}))) as
        | RigBindingsResponse
        | { detail?: string };
      if (!response.ok || !("bindings" in payload)) {
        throw new Error(
          "detail" in payload && payload.detail
            ? payload.detail
            : "读取双臂角色绑定失败"
        );
      }
      setRigBindings(payload);
    } catch (exc) {
      setBindingError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBindingsLoading(false);
    }
  }, [baseUrl, fetchWithHeaders]);

  const refresh = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setLoading(true);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/hardware/inventory`);
      const payload = (await response.json().catch(() => ({}))) as
        | InventoryResponse
        | { detail?: string };
      if (!response.ok) {
        throw new Error(
          "detail" in payload && payload.detail ? payload.detail : "读取硬件信息失败"
        );
      }
      if (!("ports" in payload)) {
        throw new Error("硬件信息响应格式不正确");
      }

      const incoming = payload.ports;
      const previous = portsRef.current;
      const previousById = new Map(previous.map((port) => [port.id, port]));
      const incomingIds = new Set(incoming.map((port) => port.id));
      const merged = [...incoming];
      for (const port of previous) {
        if (!incomingIds.has(port.id)) {
          merged.push({ ...port, present: false });
        }
      }

      if (!firstFetchRef.current) {
        const now = new Date().toLocaleTimeString();
        const changes: InventoryEvent[] = [];
        for (const port of incoming) {
          const old = previousById.get(port.id);
          if (!old || !old.present) {
            changes.push({
              time: now,
              kind: "insert",
              label: portLabel(port),
              reconnected: Boolean(old),
            });
          }
        }
        for (const port of previous) {
          if (port.present && !incomingIds.has(port.id)) {
            changes.push({
              time: now,
              kind: "remove",
              label: portLabel(port),
              reconnected: false,
            });
          }
        }
        if (changes.length) {
          setEvents((current) => [...changes, ...current].slice(0, 12));
        }
      }
      firstFetchRef.current = false;
      portsRef.current = merged;
      setPorts(merged);

      setRoles((current) => {
        const next = { ...current };
        for (const port of merged) {
          if (!(port.id in next)) {
            next[port.id] = normalizeRole(port.role);
          }
        }
        return next;
      });
      setNotes((current) => {
        const next = { ...current };
        for (const port of merged) {
          if (!(port.id in next)) {
            next[port.id] = port.note ?? "";
          }
        }
        return next;
      });

      setSavedAt(payload.saved_at ?? null);
      setLatestPath(payload.latest_path ?? "");
      setInitialized(true);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      inFlightRef.current = false;
      setLoading(false);
    }
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    void refresh();
    if (!autoRefresh) return;
    const timer = window.setInterval(() => {
      void refresh();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [autoRefresh, refresh]);

  useEffect(() => {
    void loadBindings();
  }, [loadBindings]);

  const effectiveRoles = useMemo(() => {
    const result: Record<string, Role> = {};
    for (const port of ports) {
      result[port.id] = roles[port.id] ?? normalizeRole(port.role);
    }
    return result;
  }, [ports, roles]);

  const effectiveNotes = useMemo(() => {
    const result: Record<string, string> = {};
    for (const port of ports) {
      result[port.id] = notes[port.id] ?? port.note ?? "";
    }
    return result;
  }, [ports, notes]);

  const presentCount = ports.filter((port) => port.present).length;
  const assignedRequired = REQUIRED_ROLES.filter((role) =>
    ports.some((port) => port.present && effectiveRoles[port.id] === role)
  );
  const missingRoles = REQUIRED_ROLES.filter((role) => !assignedRequired.includes(role));
  const complete = missingRoles.length === 0 && presentCount > 0;

  const changeRole = (port: Port, role: Role) => {
    setError(null);
    setMessage(null);
    if (role !== "unassigned" && role !== "spare") {
      const owner = ports.find(
        (other) => other.id !== port.id && effectiveRoles[other.id] === role
      );
      if (owner) {
        setError(
          `角色“${roleLabel(role)}”` +
            `已分配给 ${portLabel(owner)}，请先取消原分配。`
        );
        return;
      }
    }
    setRoles((current) => ({ ...current, [port.id]: role }));
    setDirty(true);
  };

  const changeNote = (port: Port, note: string) => {
    setNotes((current) => ({ ...current, [port.id]: note }));
    setDirty(true);
    setMessage(null);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/hardware/inventory/save`, {
        method: "POST",
        body: JSON.stringify({ roles: effectiveRoles, notes: effectiveNotes }),
      });
      const payload = (await response.json().catch(() => ({}))) as {
        detail?: string;
        saved_path?: string;
        saved_at?: string;
        latest_path?: string;
        complete?: boolean;
        missing_roles?: Role[];
      };
      if (!response.ok) {
        throw new Error(payload.detail ?? "保存硬件信息失败");
      }
      setDirty(false);
      setSavedAt(payload.saved_at ?? null);
      setLatestPath(payload.latest_path ?? latestPath);
      const missing = payload.missing_roles ?? [];
      setMessage(
        payload.complete
          ? `已保存：${payload.saved_path}（四个必需角色齐全）`
          : `已保存：${payload.saved_path}（仍缺：${missing.map(roleLabel).join("、") || "未连接端口"}）`
      );
      await refresh();
      await loadBindings();
      setBindingMessage(
        payload.complete ? "角色已保存，可应用到双臂记录。" : "角色已保存；补齐四个角色后才能应用。"
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  const applyBindings = async () => {
    setBindingsApplying(true);
    setBindingError(null);
    setBindingMessage(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/hardware/rig-bindings/apply`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      const payload = (await response.json().catch(() => ({}))) as {
        detail?: string;
        updated?: string[];
        unchanged?: string[];
      };
      if (!response.ok) {
        throw new Error(payload.detail ?? "应用角色端口失败");
      }
      const updated = payload.updated ?? [];
      const unchanged = payload.unchanged ?? [];
      setBindingMessage(
        updated.length
          ? `已更新记录：${updated.join("、")}`
          : `端口无需更新：${unchanged.join("、")}`
      );
      await Promise.all([loadBindings(), refresh()]);
    } catch (exc) {
      setBindingError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBindingsApplying(false);
    }
  };

  const download = async () => {
    setDownloading(true);
    setError(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/hardware/inventory/latest`);
      if (!response.ok) {
        const payload = (await response.json().catch(() => ({}))) as { detail?: string };
        throw new Error(payload.detail ?? "尚无已保存的硬件信息");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "hardware-inventory-latest.json";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="min-h-screen bg-black p-6 text-gray-100">
      <div className="mx-auto max-w-7xl space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold">
              <Usb className="h-6 w-6 text-blue-400" />
              硬件角色确认
            </h1>
            <p className="mt-1 max-w-3xl text-sm text-gray-400">
              只读枚举 <span className="font-mono">/dev/serial/by-id</span> 与 sysfs，
              不打开串口、不写标定、不改变力矩。用于在四台机械臂之间确认左右
              follower / leader。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" onClick={() => void refresh()} disabled={loading}>
              <RefreshCw className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
              刷新
            </Button>
            <Button onClick={() => void save()} disabled={saving || !ports.length}>
              <Save className="mr-1 h-4 w-4" />
              {saving ? "保存中…" : "保存硬件信息"}
            </Button>
            <Button
              variant="outline"
              onClick={() => void download()}
              disabled={downloading || !latestPath}
            >
              <Download className="mr-1 h-4 w-4" />
              下载 JSON
            </Button>
          </div>
        </div>

        <div className="rounded-lg border border-gray-800 bg-gray-950 p-4">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge className="bg-emerald-950 text-emerald-300">
              已连接 {presentCount}
            </Badge>
            <Badge className="bg-blue-950 text-blue-300">
              必需角色 {assignedRequired.length}/4
            </Badge>
            {complete ? (
              <Badge className="bg-emerald-600 text-white">角色齐全</Badge>
            ) : (
              <Badge className="bg-amber-950 text-amber-300">
                缺少：{missingRoles.map(roleLabel).join("、") || "未连接端口"}
              </Badge>
            )}
            {dirty ? (
              <Badge className="bg-amber-600 text-white">未保存修改</Badge>
            ) : savedAt ? (
              <span className="text-xs text-gray-500">已保存：{savedAt}</span>
            ) : (
              <span className="text-xs text-gray-500">尚未保存</span>
            )}
            <label className="ml-auto flex items-center gap-2 text-xs text-gray-400">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(event) => setAutoRefresh(event.target.checked)}
              />
              自动刷新（1.5 秒）
            </label>
          </div>
          <div className="mt-2 break-all font-mono text-xs text-gray-500">
            保存位置：{latestPath || "—"}
          </div>
        </div>

        {error ? (
          <div className="rounded border border-red-800 bg-red-950 p-3 text-sm text-red-200">
            {error}
          </div>
        ) : null}
        {message ? (
          <div className="break-all rounded border border-emerald-800 bg-emerald-950 p-3 text-sm text-emerald-200">
            {message}
          </div>
        ) : null}

        <div className="rounded-lg border border-gray-800 bg-gray-950 p-4 text-sm text-gray-400">
          <div className="mb-2 font-medium text-gray-200">操作步骤</div>
          <ol className="list-decimal space-y-1 pl-5">
            <li>确认四根 USB 串口都出现在下面，且为“已连接”。</li>
            <li>一次只拔一根线；对应行会变成“未连接”，右侧事件区会记录“拔出”。</li>
            <li>按你刚刚拔下的物理臂，在该行选择角色；然后把线插回，确认恢复“已连接”。</li>
            <li>四个必需角色都分配后点“保存硬件信息”。leader 仅遥操作/采集需要。</li>
          </ol>
        </div>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="space-y-3">
            {ports.map((port) => {
              const role = effectiveRoles[port.id] ?? "unassigned";
              return (
                <div
                  key={port.id}
                  className={`rounded-lg border p-4 ${
                    port.present
                      ? "border-gray-800 bg-gray-950"
                      : "border-amber-900/70 bg-amber-950/10"
                  }`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`h-2.5 w-2.5 rounded-full ${
                            port.present ? "bg-emerald-400" : "bg-amber-500"
                          }`}
                        />
                        <span className="font-mono text-sm text-gray-100">
                          {port.serial || "未知序列号"}
                        </span>
                        <Badge
                          className={
                            port.present
                              ? "bg-emerald-950 text-emerald-300"
                              : "bg-amber-950 text-amber-300"
                          }
                        >
                          {port.present ? "已连接" : "未连接"}
                        </Badge>
                        {port.source === "payload" ? (
                          <Badge className="bg-gray-800 text-gray-300">离线记录</Badge>
                        ) : null}
                      </div>
                      <div className="mt-1 break-all font-mono text-xs text-gray-500">
                        {port.by_id || port.id}
                      </div>
                      <div className="mt-1 break-all text-xs text-gray-500">
                        {port.device || "—"} · VID:PID {port.vid || "—"}:{port.pid || "—"} ·{" "}
                        {port.product || "—"}
                        {port.usb_path ? ` · ${port.usb_path}` : ""}
                      </div>
                      {port.manufacturer ? (
                        <div className="mt-1 text-xs text-gray-600">
                          {port.manufacturer}
                        </div>
                      ) : null}
                      {port.last_seen ? (
                        <div className="mt-1 text-xs text-gray-600">
                          上次保存：{port.last_seen}
                        </div>
                      ) : null}
                    </div>
                    <select
                      value={role}
                      onChange={(event) =>
                        changeRole(port, event.target.value as Role)
                      }
                      className="min-w-[180px] rounded border border-gray-700 bg-black px-2 py-1.5 text-sm text-gray-100"
                    >
                      {ROLE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <span className="text-xs text-gray-500">备注</span>
                    <input
                      value={effectiveNotes[port.id] ?? ""}
                      onChange={(event) => changeNote(port, event.target.value)}
                      maxLength={200}
                      placeholder="如 my_robot / 左臂序列号"
                      className="min-w-[240px] flex-1 rounded border border-gray-800 bg-black px-2 py-1 font-mono text-xs text-gray-100"
                    />
                  </div>
                </div>
              );
            })}

            {!initialized && loading ? (
              <div className="rounded border border-gray-800 bg-gray-950 p-4 text-sm text-gray-500">
                正在读取串口…
              </div>
            ) : null}
            {initialized && !ports.length ? (
              <div className="rounded border border-gray-800 bg-gray-950 p-4 text-sm text-gray-400">
                未发现串口。请确认 USB 线已连接；枚举不需要 dialout 权限。
              </div>
            ) : null}
          </div>

          <div className="h-fit rounded-lg border border-gray-800 bg-gray-950 p-4">
            <div className="text-sm font-medium text-gray-200">插拔事件</div>
            <div className="mt-2 space-y-1 font-mono text-xs">
              {events.length ? (
                events.map((event, index) => (
                  <div
                    key={`${event.time}-${event.kind}-${index}`}
                    className={
                      event.kind === "remove" ? "text-amber-300" : "text-emerald-300"
                    }
                  >
                    [{event.time}] {event.kind === "remove" ? "拔出" : "插入"}{" "}
                    {event.label}
                    {event.reconnected ? "（重新连接）" : ""}
                  </div>
                ))
              ) : (
                <div className="text-gray-500">等待插拔…</div>
              )}
            </div>
            <div className="mt-4 border-t border-gray-900 pt-3 text-xs text-gray-500">
              拔掉的串口会留在列表中，方便先选角色再插回。本页不会自动标定或下发动作。
            </div>
          </div>
        </div>

        <div className="rounded-lg border border-gray-800 bg-gray-950 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="flex items-center gap-2 text-lg font-medium">
                <Link2 className="h-5 w-5 text-blue-400" />
                应用到双臂记录
              </h2>
              <p className="mt-1 text-sm text-gray-400">
                把已保存的左右 follower / leader 串口端口写入
                <span className="font-mono"> left_rig </span>和
                <span className="font-mono"> right_rig </span>机器人记录；
                保留现有标定配置，不打开硬件。
              </p>
            </div>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                onClick={() => void loadBindings()}
                disabled={bindingsLoading}
              >
                <RefreshCw className={`mr-1 h-4 w-4 ${bindingsLoading ? "animate-spin" : ""}`} />
                重新检查
              </Button>
              <Button
                onClick={() => void applyBindings()}
                disabled={bindingsApplying || !rigBindings?.can_apply}
              >
                {bindingsApplying ? "应用中…" : "应用角色端口"}
              </Button>
            </div>
          </div>

          {bindingError ? (
            <div className="mt-3 rounded border border-red-800 bg-red-950 p-3 text-sm text-red-200">
              {bindingError}
            </div>
          ) : null}
          {bindingMessage ? (
            <div className="mt-3 rounded border border-emerald-800 bg-emerald-950 p-3 text-sm text-emerald-200">
              {bindingMessage}
            </div>
          ) : null}

          {!rigBindings ? (
            <div className="mt-3 text-sm text-gray-500">
              {bindingsLoading ? "正在检查…" : "尚未取得角色绑定。"}
            </div>
          ) : (
            <>
              <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
                <Badge className="bg-blue-950 text-blue-300">
                  已保存角色：{rigBindings.inventory_saved_at || "无"}
                </Badge>
                {rigBindings.missing_roles.length ? (
                  <Badge className="bg-amber-950 text-amber-300">
                    缺少：{rigBindings.missing_roles.map(roleLabel).join("、")}
                  </Badge>
                ) : (
                  <Badge className="bg-emerald-950 text-emerald-300">四角色完整</Badge>
                )}
              </div>
              <div className="mt-3 grid gap-3 lg:grid-cols-2">
                {rigBindings.bindings.map((binding) => (
                  <div
                    key={binding.robot_name}
                    className="rounded border border-gray-800 bg-black p-3"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-mono text-sm text-gray-100">
                        {binding.robot_name}
                      </div>
                      <Badge
                        className={
                          binding.status === "unchanged"
                            ? "bg-emerald-950 text-emerald-300"
                            : binding.status === "ready"
                              ? "bg-blue-950 text-blue-300"
                              : "bg-amber-950 text-amber-300"
                        }
                      >
                        {bindingStatusLabel(binding.status)}
                      </Badge>
                    </div>
                    <div className="mt-3 space-y-2 text-xs">
                      {(["follower", "leader"] as const).map((kind) => {
                        const item =
                          kind === "follower" ? binding.follower_port : binding.leader_port;
                        return (
                          <div key={kind} className="rounded bg-gray-950 p-2">
                            <div className="text-gray-300">
                              {kind === "follower" ? "从臂" : "主臂"} · {roleLabel(item.role)} ·{" "}
                              {item.serial || "未知"}
                            </div>
                            <div className="mt-1 text-gray-500">
                              当前：<span className="break-all font-mono">{item.current || "—"}</span>
                            </div>
                            <div className={item.proposed === item.current ? "text-gray-500" : "text-emerald-300"}>
                              建议：<span className="break-all font-mono">{item.proposed || "—"}</span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                    <div className="mt-3 border-t border-gray-800 pt-3 text-xs">
                      <div className="text-gray-300">
                        标定：
                        {binding.calibration?.complete ? (
                          <span className="text-emerald-300">主从臂均已就绪</span>
                        ) : (
                          <span className="text-amber-300">仍有缺失</span>
                        )}
                      </div>
                      <div className="mt-1 text-gray-500">
                        配置名：从臂 {binding.configs.follower || "—"} · 主臂{" "}
                        {binding.configs.leader || "—"}
                      </div>
                      {binding.calibration?.issues.length ? (
                        <ul className="mt-2 space-y-1 text-red-300">
                          {binding.calibration.issues.map((issue) => (
                            <li key={issue.code}>{issue.message}</li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default HardwareInventory;
