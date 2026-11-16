import React, { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Database, Download, Loader2, RefreshCw, Upload } from "lucide-react";
import { Link } from "react-router-dom";
import LanderpiNav from "@/components/landerpi/LanderpiNav";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";

const button = "rounded-lg border border-slate-700 px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-35";

function text(value: unknown, fallback = "—"): string {
  return typeof value === "string" && value ? value : fallback;
}

export default function LanderpiData() {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [sessions, setSessions] = useState<any[]>([]);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sync, setSync] = useState<any>(null);
  const [job, setJob] = useState<any>(null);
  const [report, setReport] = useState<any>(null);

  const request = useCallback(<T,>(path: string, body?: unknown) =>
    apiRequest<T>(baseUrl, fetchWithHeaders, path, {
      method: body === undefined ? "GET" : "POST",
      body,
    }), [baseUrl, fetchWithHeaders]);

  const load = useCallback(async () => {
    setBusy("刷新采集记录");
    try {
      const data = await request<any>("/ros-mobile/sessions");
      const list = Array.isArray(data?.sessions) ? data.sessions : [];
      setSessions(list);
      if (!selected && list.length > 0) setSelected(list[list.length - 1].session_id);
      setError(null);
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
    } finally {
      setBusy(null);
    }
  }, [request, selected]);

  useEffect(() => { void load(); }, [load]);

  const doSync = async () => {
    if (!selected) return;
    setBusy("同步原始数据");
    setError(null);
    try {
      setSync(await request<any>("/ros-mobile/sync", { session_id: selected }));
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
    } finally {
      setBusy(null);
    }
  };

  const doExport = async () => {
    if (!selected) return;
    setBusy("导出硬件 6D");
    setError(null);
    setReport(null);
    try {
      const started = await request<any>("/ros-mobile/export", { session_id: selected });
      setJob(started);
      for (let attempt = 0; attempt < 240; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const next = await request<any>(`/ros-mobile/export/status/${encodeURIComponent(started.job_id)}`);
        setJob(next);
        if (next?.status !== "running") {
          if (next?.status === "completed") setReport(next.report ?? {});
          if (next?.status === "failed") setError(next?.error || "导出失败");
          break;
        }
      }
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <LanderpiNav />
      <main className="mx-auto max-w-6xl space-y-4 px-4 py-5 sm:px-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <Database className="h-5 w-5 text-emerald-300" />
              <h1 className="text-xl font-semibold">数据处理</h1>
            </div>
            <p className="mt-1 max-w-3xl text-sm text-slate-400">
              对已经结束的真实采集执行同步、硬件 6D 导出和回看。这个页面不申请硬件控制权，不会让机械臂运动。
            </p>
          </div>
          <Link to="/dataset-viewer" className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800">
            打开数据集查看器
          </Link>
        </div>

        {error && (
          <div className="flex items-start gap-2 rounded-xl border border-red-800 bg-red-950/40 p-3 text-sm text-red-100">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="break-all">{error}</span>
            <button type="button" className="ml-auto shrink-0 text-xs underline" onClick={() => setError(null)}>关闭</button>
          </div>
        )}

        <section className="rounded-xl border border-slate-800 bg-slate-900/65 p-4">
          <div className="flex flex-wrap items-end gap-2">
            <label className="min-w-[320px] flex-1 text-xs text-slate-400">
              已结束的采集 session
              <select
                value={selected}
                onChange={(event) => setSelected(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
              >
                <option value="">选择 session</option>
                {sessions.slice().reverse().map((item) => (
                  <option key={item.session_id} value={item.session_id}>
                    {item.session_id} · {text(item.status, "unknown")}{item.upper?.ready ? " · 已同步" : ""}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" className={button} disabled={!!busy} onClick={() => void load()}>
              <RefreshCw className="mr-1 inline h-4 w-4" />刷新记录
            </button>
            <button type="button" className={button} disabled={!selected || !!busy} onClick={() => void doSync()}>
              <Upload className="mr-1 inline h-4 w-4" />同步原始数据
            </button>
            <button type="button" className="rounded-lg bg-emerald-600 px-3 py-2 text-sm font-medium text-slate-950 disabled:opacity-40" disabled={!selected || !!busy || job?.status === "running"} onClick={() => void doExport()}>
              <Download className="mr-1 inline h-4 w-4" />导出硬件 6D
            </button>
            {busy && <span className="flex items-center gap-1 text-xs text-slate-400"><Loader2 className="h-3.5 w-3.5 animate-spin" />{busy}</span>}
          </div>
          <p className="mt-2 text-xs text-slate-500">
            导出由后端校验来源、六轴实测、标定绑定、阶段完整性和数据身份；页面不补零、不补行，也不会自动切到 simulated_offline。
          </p>
        </section>

        {sync && (
          <section className="rounded-xl border border-emerald-800/70 bg-emerald-950/25 p-4 text-sm">
            <p className="font-medium text-emerald-200">同步已完成</p>
            <p className="mt-1 break-all font-mono text-xs text-emerald-300/80">{text(sync.path)}</p>
            <p className="mt-1 text-xs text-emerald-300/70">清单校验：{text(sync.status)} · SHA256 {text(sync.manifest_sha256).slice(0, 24)}…</p>
          </section>
        )}

        {job && (
          <section className="rounded-xl border border-slate-800 bg-slate-900/65 p-4 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="font-medium">导出任务</p>
                <p className="mt-1 break-all font-mono text-xs text-slate-400">{job.job_id}</p>
              </div>
              <span className="rounded-full border border-slate-700 px-2 py-1 text-xs text-slate-300">{text(job.status)}</span>
            </div>
            {job.error && <p className="mt-2 break-all text-xs text-red-300">{job.error}</p>}
          </section>
        )}

        {report && (
          <section className="rounded-xl border border-emerald-800/70 bg-emerald-950/25 p-4">
            <h2 className="font-semibold text-emerald-100">硬件 6D 导出结果</h2>
            <div className="mt-3 grid gap-2 text-sm sm:grid-cols-3">
              <div><p className="text-xs text-emerald-300/60">数据集</p><p className="mt-1 break-all font-mono text-emerald-100">{text(report.repo_id)}</p></div>
              <div><p className="text-xs text-emerald-300/60">集数 / 帧数</p><p className="mt-1 text-emerald-100">{report.total_episodes ?? "—"} / {report.total_frames ?? "—"}</p></div>
              <div><p className="text-xs text-emerald-300/60">schema</p><p className="mt-1 text-emerald-100">{report.export_schema?.kind ?? "—"} · {report.export_schema?.dims ?? "—"} 维</p></div>
            </div>
            <p className="mt-3 break-all font-mono text-xs text-emerald-300/80">{text(report.dataset_root)}</p>
          </section>
        )}
      </main>
    </div>
  );
}
