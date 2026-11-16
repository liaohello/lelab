import { Activity, Database, Gauge, Settings2 } from "lucide-react";
import { Link, useLocation } from "react-router-dom";

const tabs = [
  { key: "control", to: "/landerpi", label: "采集控制", icon: Activity },
  { key: "calibration", to: "/landerpi/calibration", label: "标定", icon: Settings2 },
  { key: "data", to: "/landerpi/data", label: "数据处理", icon: Database },
  { key: "diagnostics", to: "/landerpi/diagnostics", label: "诊断", icon: Gauge },
] as const;

export default function LanderpiNav() {
  const { pathname } = useLocation();
  return (
    <header className="border-b border-slate-800 bg-slate-950/95">
      <div className="mx-auto flex max-w-[1440px] flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-6">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-sky-500 font-semibold text-slate-950">LP</div>
          <div>
            <p className="text-sm font-semibold text-slate-100">LanderPi 操作台</p>
            <p className="text-[11px] text-slate-500">采集优先 · 诊断信息按需展开</p>
          </div>
        </div>
        <nav className="flex flex-wrap items-center gap-1 rounded-lg border border-slate-800 bg-slate-900/70 p-1" aria-label="LanderPi 功能入口">
          {tabs.map(({ key, to, label, icon: Icon }) => {
            const active = key === "control"
              ? pathname === "/landerpi" || pathname === "/landerpi/teleop"
              : pathname === to;
            return (
              <Link
                key={key}
                to={to}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition ${
                  active ? "bg-sky-500 text-slate-950" : "text-slate-400 hover:bg-slate-800 hover:text-slate-100"
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                {label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
