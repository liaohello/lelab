import LanderpiNav from "@/components/landerpi/LanderpiNav";
import LanderpiAdvancedSettings from "@/components/landerpi/LanderpiAdvancedSettings";
import LanderpiSetup from "@/pages/LanderpiSetup";

export default function LanderpiCalibration() {
  return (
    <div className="min-h-screen bg-slate-950">
      <LanderpiNav />
      <LanderpiSetup />
      <LanderpiAdvancedSettings />
    </div>
  );
}
