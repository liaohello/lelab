import { useEffect, useRef, useState } from "react";
import { useApi } from "@/contexts/ApiContext";

/** Decode off-screen before swapping pixels; one bounded request at a time.
 * Each mount carries its own viewer identity so two pages never consume one
 * shared frame-progress sequence (the backend refuses anonymous fresh polls). */
export default function StableCameraPreview({ active, sourceFresh }: {
  active: boolean; sourceFresh: boolean;
}) {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [picture, setPicture] = useState<{
    url: string;
    seq: number;
    sourceStampNs: number;
    at: number;
  } | null>(null);
  const [now, setNow] = useState(Date.now());
  const [error, setError] = useState<string | null>(null);
  const shown = useRef<string | null>(null);
  const viewerId = useRef(
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? `viewer-${crypto.randomUUID()}`
      : `viewer-${Date.now()}-${Math.random().toString(36).slice(2)}`,
  );
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!active) {
      setPicture(null);
      if (shown.current) URL.revokeObjectURL(shown.current);
      shown.current = null;
      return;
    }
    let stopped = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    let previousStamp = -1;
    let previousSeq = -1;
    const pull = async () => {
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 1500);
      let pendingUrl: string | null = null;
      try {
        const response = await fetchWithHeaders(`${baseUrl}/ros-mobile/preview.jpg?fresh=true`, {
          signal: controller.signal,
          cache: "no-store",
          headers: { "X-LeLab-Viewer": viewerId.current },
        });
        if (response.status === 409) {
          // The camera did not produce a newer frame for this viewer. Keep the
          // last decoded picture, mark it stale, and keep polling for recovery.
          if (!stopped) setError("相机帧未前进（可能断流）");
          return;
        }
        if (!response.ok) throw new Error(`camera_http_${response.status}`);
        const sequence = response.headers.get("X-LeLab-Frame-Seq");
        const stamp = response.headers.get("X-LeLab-Source-Stamp-Ns");
        if (!sequence || !stamp || !/^\d+$/.test(sequence) || !/^\d+$/.test(stamp)) {
          throw new Error("camera_frame_identity_missing");
        }
        const seq = Number(sequence); const sourceStamp = Number(stamp);
        if (sourceStamp <= previousStamp || (seq === previousSeq && previousSeq >= 0)) {
          throw new Error("camera_frame_did_not_advance");
        }
        pendingUrl = URL.createObjectURL(await response.blob());
        const decoded = new Image();
        decoded.src = pendingUrl;
        await decoded.decode();
        if (stopped) return;
        previousStamp = sourceStamp; previousSeq = seq;
        const previous = shown.current;
        shown.current = pendingUrl;
        setPicture({ url: pendingUrl, seq, sourceStampNs: sourceStamp, at: Date.now() });
        pendingUrl = null;
        setError(null);
        // React commits the already decoded resource before releasing the previous one.
        if (previous) window.setTimeout(() => URL.revokeObjectURL(previous), 500);
      } catch (failure) {
        if (!stopped) {
          const name = (failure as any)?.name;
          setError(name === "AbortError" ? "相机请求超时" : String(failure));
        }
      } finally {
        window.clearTimeout(timeout);
        if (pendingUrl) URL.revokeObjectURL(pendingUrl);
        if (!stopped) timer = window.setTimeout(pull, 200);
      }
    };
    void pull();
    return () => {
      stopped = true;
      controller?.abort();
      if (timer) window.clearTimeout(timer);
      if (shown.current) URL.revokeObjectURL(shown.current);
      shown.current = null;
    };
  }, [active, baseUrl, fetchWithHeaders]);

  const stale = !sourceFresh || !picture || now - picture.at > 1500;
  return <div className="relative h-full w-full" data-testid="stable-camera-preview"
    data-frame-seq={picture?.seq ?? ""} data-source-stamp-ns={picture?.sourceStampNs ?? ""}
    data-rendered-at={picture?.at ?? ""} data-stale={stale}>
    {picture && <img src={picture.url} alt="LanderPi 腕部实时画面" className="h-full w-full object-contain" />}
    {stale && <div className="absolute inset-0 flex items-center justify-center bg-black/65 p-4 text-center text-sm text-amber-200">
      {picture ? "画面过期；保留上一帧，仅供参考" : "等待首张有效画面"}
      {error && <span className="ml-2 text-xs">{error}</span>}
    </div>}
  </div>;
}
