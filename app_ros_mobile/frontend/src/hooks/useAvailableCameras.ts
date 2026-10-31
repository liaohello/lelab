import { useCallback, useEffect, useState } from "react";
import { useApi } from "@/contexts/ApiContext";

export interface AvailableCamera {
  index: number;
  name: string;
  deviceId: string;
  available: boolean;
}

interface UseAvailableCamerasOptions {
  /** When false, do nothing. Use to gate on modal open. */
  enabled?: boolean;
}

/**
 * Enumerates cameras attached to the API host. Preview capture also runs on
 * that host, so opening LeLab from Windows controls the Jetson cameras rather
 * than accidentally requesting Windows browser-camera permission.
 */
export function useAvailableCameras({
  enabled = true,
}: UseAvailableCamerasOptions = {}) {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [cameras, setCameras] = useState<AvailableCamera[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  const refresh = useCallback(async (): Promise<AvailableCamera[]> => {
    setIsLoading(true);
    try {
      const r = await fetchWithHeaders(`${baseUrl}/available-cameras`);
      if (!r.ok) {
        setCameras([]);
        return [];
      }
      const data = await r.json();
      const backendCams: {
        index: number;
        name?: string;
        available: boolean;
      }[] = data.cameras ?? [];

      const merged: AvailableCamera[] = backendCams.map((cam) => {
        const label = cam.name || `Camera ${cam.index}`;
        return {
          index: cam.index,
          name: label,
          deviceId: `server-camera-${cam.index}`,
          available: cam.available,
        };
      });
      setCameras(merged);
      return merged;
    } catch {
      setCameras([]);
      return [];
    } finally {
      setIsLoading(false);
    }
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    if (!enabled) return;
    refresh();
  }, [enabled, refresh]);

  return { cameras, isLoading, refresh };
}
