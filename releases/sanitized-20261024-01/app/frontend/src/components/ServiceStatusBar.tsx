import { useEffect, useState } from "react";
import { useApi } from "@/contexts/ApiContext";
import { fetchTeleopReadiness } from "@/lib/teleopReadiness";

/**
 * Always-visible statement of which backend this page is talking to, who holds
 * the serial bus right now, and which follower motion limits that backend will
 * actually inject.  Two services on one workstation can carry different
 * environment overrides, so guessing from the previous page is unsafe.
 */

const REFRESH_MS = 10_000;

interface LeaseSnapshot {
  state: string;
  task: string | null;
  owner: string | null;
}

const formatLimits = (safety: Record<string, number | null> | undefined): string => {
  if (!safety) return "读取中…";
  return Object.entries(safety)
    .map(([name, value]) => `${name}=${value === null ? "关闭" : value}`)
    .join(" · ");
};

const ServiceStatusBar = () => {
  const { baseUrl, clientId, fetchWithHeaders } = useApi();
  const [limits, setLimits] = useState("读取中…");
  const [lease, setLease] = useState<LeaseSnapshot | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [readiness, leaseResponse] = await Promise.all([
        fetchTeleopReadiness(fetchWithHeaders, baseUrl),
        fetchWithHeaders(`${baseUrl}/hardware/lease`).catch(() => null),
      ]);
      if (cancelled) return;
      const safety = readiness?.sides?.left?.identity?.safety;
      if (safety) setLimits(formatLimits(safety));
      if (leaseResponse?.ok) setLease((await leaseResponse.json()) as LeaseSnapshot);
    };
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [baseUrl, fetchWithHeaders]);

  const host = baseUrl.replace(/^https?:\/\//, "");
  const held = lease?.state === "held";
  const occupant = !held
    ? "空闲"
    : lease?.owner === clientId
      ? `${lease.task} · 本浏览器`
      : `${lease.task} · 其他客户端`;

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-gray-900 bg-black px-4 py-1 font-mono text-[11px] text-gray-500">
      <span>服务 {host}</span>
      <span className={held ? "text-amber-300" : ""}>占用 {occupant}</span>
      <span>生效限幅 {limits}</span>
    </div>
  );
};

export default ServiceStatusBar;
