import { Info } from "lucide-react";
import LanderpiNav from "@/components/landerpi/LanderpiNav";
import RosMobile from "@/pages/RosMobile";

export default function LanderpiDiagnostics() {
  return (
    <div className="min-h-screen bg-slate-950">
      <LanderpiNav />
      <div className="mx-auto max-w-[1440px] px-4 pt-4 sm:px-6">
        <div className="flex items-start gap-2 rounded-xl border border-amber-800/70 bg-amber-950/25 p-3 text-sm text-amber-100">
          <Info className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            这里是诊断与旧入口，保留原始状态、话题、旧只读面板和故障细节。日常采集请回到“采集控制”，不要同时从多个入口申请同一份控制权。
          </p>
        </div>
      </div>
      <RosMobile />
    </div>
  );
}
