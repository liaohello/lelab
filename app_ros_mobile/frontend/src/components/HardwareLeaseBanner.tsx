import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { OctagonAlert, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApi } from "@/contexts/ApiContext";
import {
  emergencyStop,
  getHardwareLease,
  renewHardwareLease,
} from "@/lib/systemStorageApi";

const LEASE_POLL_MS = 2_000;
const LEASE_QUERY_KEY = "hardware-lease";

const TASK_LABELS: Record<string, string> = {
  calibration: "校准",
  teleoperation: "遥操作",
  recording: "录制",
  inference: "推理",
};

/**
 * Global hardware-control indicator.
 *
 * Only one task may own the SO-101 serial bus. The banner shows who holds the
 * lease, keeps it alive while this browser is the owner, and always offers the
 * emergency stop — the one action that must work regardless of ownership.
 */
const HardwareLeaseBanner = () => {
  const { baseUrl, clientId, fetchWithHeaders } = useApi();
  const queryClient = useQueryClient();
  const lastRenewAttempt = useRef(0);

  const leaseQuery = useQuery({
    queryKey: [LEASE_QUERY_KEY, baseUrl],
    queryFn: ({ signal }) => getHardwareLease(baseUrl, fetchWithHeaders, signal),
    refetchInterval: LEASE_POLL_MS,
    retry: 1,
  });

  const lease = leaseQuery.data;
  const held = lease?.state === "held";
  const ownedByThisBrowser = held && lease?.owner === clientId;
  const refetchLease = useRef(leaseQuery.refetch);
  refetchLease.current = leaseQuery.refetch;

  // Heartbeat at ~1/3 of the TTL so a page reload or a slow tab still holds
  // the lease, while a closed tab loses it within one TTL.
  useEffect(() => {
    if (!held || !lease?.task || !ownedByThisBrowser) return;
    const ttl = lease.ttl_seconds || 60;
    const expiresIn = lease.expires_in ?? ttl;
    if (expiresIn > ttl / 2) return;
    const now = Date.now();
    if (now - lastRenewAttempt.current < 5_000) return;
    lastRenewAttempt.current = now;
    void renewHardwareLease(baseUrl, fetchWithHeaders, { task: lease.task })
      .catch(() => undefined)
      .then(() => refetchLease.current());
  }, [
    baseUrl,
    clientId,
    fetchWithHeaders,
    held,
    lease?.expires_in,
    lease?.task,
    lease?.ttl_seconds,
    ownedByThisBrowser,
  ]);

  const handleEmergencyStop = async () => {
    try {
      await emergencyStop(baseUrl, fetchWithHeaders, `web:${clientId}`);
    } finally {
      void queryClient.invalidateQueries({ queryKey: [LEASE_QUERY_KEY] });
      void leaseQuery.refetch();
    }
  };

  if (leaseQuery.isError || (!held && !lease?.last_release)) return null;

  return (
    <div
      className={
        held
          ? "flex flex-wrap items-center justify-center gap-3 border-b border-amber-500/40 bg-amber-500/10 px-4 py-1.5 text-xs text-amber-200"
          : "flex flex-wrap items-center justify-center gap-3 border-b border-slate-800 bg-slate-900/60 px-4 py-1.5 text-xs text-slate-400"
      }
    >
      <span className="inline-flex items-center gap-1.5">
        {held ? <ShieldCheck className="h-3.5 w-3.5" /> : <ShieldCheck className="h-3.5 w-3.5" />}
        {held ? (
          <>
            硬件控制锁：
            <strong className="font-semibold text-amber-100">
              {TASK_LABELS[lease?.task ?? ""] ?? lease?.task}
            </strong>
            <span className="text-amber-200/80">
              {lease?.owner}
              {ownedByThisBrowser ? "（本机）" : ""} · 剩余 {Math.max(0, Math.round(lease?.expires_in ?? 0))}s
            </span>
          </>
        ) : (
          <>硬件空闲{lease?.last_release ? ` · 上次：${TASK_LABELS[lease.last_release.task] ?? lease.last_release.task}（${lease.last_release.reason}）` : ""}</>
        )}
      </span>
      {held && (
        <Button
          size="sm"
          variant="destructive"
          className="h-6 px-2 text-[11px]"
          onClick={() => void handleEmergencyStop()}
        >
          <OctagonAlert className="h-3.5 w-3.5" />
          紧急停止
        </Button>
      )}
    </div>
  );
};

export default HardwareLeaseBanner;
