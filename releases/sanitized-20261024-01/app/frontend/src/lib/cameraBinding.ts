// Stable camera bindings confirmed on /cameras.  Recording uses these so a
// re-plugged device keeps its dataset field instead of following a mutable
// /dev/videoN index.

export interface CameraBindingSummary {
  camera_key: string;
  label: string;
  name: string | null;
  id: string;
  device: string | null;
  index: number | null;
  confirmed_at: string | null;
}

export interface CameraBindingRow extends CameraBindingSummary {
  source: string | null;
  ready: boolean;
}

export interface CameraBindingResponse {
  status: string;
  saved_at: string | null;
  cameras: CameraBindingRow[];
  missing: CameraBindingSummary[];
  unconfirmed: CameraBindingSummary[];
  ready: boolean;
}

export async function fetchCameraBinding(
  fetchWithHeaders: (url: string, options?: RequestInit) => Promise<Response>,
  baseUrl: string
): Promise<CameraBindingResponse | null> {
  try {
    const response = await fetchWithHeaders(`${baseUrl}/cameras/collection-binding`);
    if (!response.ok) return null;
    return (await response.json()) as CameraBindingResponse;
  } catch {
    return null;
  }
}
