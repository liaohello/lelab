import { BrowserRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { UrdfProvider } from "@/contexts/UrdfContext";
import { DragAndDropProvider } from "@/contexts/DragAndDropContext";
import { Toaster } from "@/components/ui/toaster";
import Landing from "@/pages/Landing";
import Teleoperation from "@/pages/Teleoperation";
import Calibration from "@/pages/Calibration";
import Recording from "@/pages/Recording";
import Training from "@/pages/Training";
import Inference from "@/pages/Inference";
import EditDataset from "@/pages/EditDataset";
import Upload from "@/pages/Upload";
import SystemStorage from "@/pages/SystemStorage";
import System from "@/pages/System";
import StoragePage from "@/pages/Storage";
import Models from "@/pages/Models";
import DatasetViewer from "@/pages/DatasetViewer";

import NotFound from "@/pages/NotFound";
import SingleTabGuard from "@/components/SingleTabGuard";
import TeleopStopNotice from "@/components/TeleopStopNotice";
import UpdateNotice from "@/components/UpdateNotice";
import HardwareLeaseBanner from "@/components/HardwareLeaseBanner";
import { TooltipProvider } from "@radix-ui/react-tooltip";
import { ApiProvider } from "./contexts/ApiContext";
import { HfAuthProvider } from "./contexts/HfAuthContext";

const queryClient = new QueryClient();

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ThemeProvider>
          <ApiProvider>
            <HfAuthProvider>
              <UrdfProvider>
                <DragAndDropProvider>
                  <BrowserRouter>
                    <SingleTabGuard>
                      <TeleopStopNotice />
                      <UpdateNotice />
                      <HardwareLeaseBanner />
                      <Routes>
                        <Route path="/" element={<Landing />} />
                        <Route path="/teleoperation" element={<Teleoperation />} />
                        <Route path="/recording" element={<Recording />} />
                        <Route path="/upload" element={<Upload />} />
                        <Route path="/training" element={<Training />} />
                        <Route path="/training/:jobId" element={<Training />} />
                        <Route path="/inference" element={<Inference />} />
                        <Route path="/calibration" element={<Calibration />} />
                        <Route path="/edit-dataset" element={<EditDataset />} />
                        <Route path="/system-storage" element={<SystemStorage />} />
                        <Route path="/system" element={<System />} />
                        <Route path="/storage" element={<StoragePage />} />
                        <Route path="/models" element={<Models />} />
                        <Route path="/dataset-viewer" element={<DatasetViewer />} />

                        <Route path="*" element={<NotFound />} />
                      </Routes>
                    </SingleTabGuard>
                    <Toaster />
                  </BrowserRouter>
                </DragAndDropProvider>
              </UrdfProvider>
            </HfAuthProvider>
          </ApiProvider>
        </ThemeProvider>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
