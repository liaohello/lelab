import { useEffect, useRef, useState } from "react";
import { useApi } from "@/contexts/ApiContext";

export type StreamStatus = "connecting" | "open" | "closed";

interface StatusStreamOptions<T> {
  /** WebSocket path, e.g. "/ws/system-status". */
  path: string;
  onMessage: (payload: T) => void;
  enabled?: boolean;
  maxBackoffMs?: number;
}

/**
 * Subscribe to one of the backend's status WebSockets.
 *
 * The backend pushes a fresh snapshot every second, which is far cheaper than
 * polling `psutil`/`tegrastats` from the browser. The stream reconnects with
 * exponential backoff and reports its state so callers can keep a slower
 * polling fallback while the socket is down.
 */
export const useStatusStream = <T,>({
  path,
  onMessage,
  enabled = true,
  maxBackoffMs = 10_000,
}: StatusStreamOptions<T>): StreamStatus => {
  const { wsBaseUrl } = useApi();
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;
  const [status, setStatus] = useState<StreamStatus>("closed");

  useEffect(() => {
    if (!enabled || !wsBaseUrl) {
      setStatus("closed");
      return;
    }
    let disposed = false;
    let socket: WebSocket | null = null;
    let retries = 0;
    let timer: number | undefined;

    const connect = () => {
      if (disposed) return;
      setStatus("connecting");
      try {
        socket = new WebSocket(`${wsBaseUrl}${path}`);
      } catch {
        scheduleReconnect();
        return;
      }
      socket.onopen = () => {
        retries = 0;
        setStatus("open");
      };
      socket.onmessage = (event) => {
        try {
          handlerRef.current(JSON.parse(event.data) as T);
        } catch {
          // A malformed frame is not worth tearing the stream down for.
        }
      };
      socket.onerror = () => {
        socket?.close();
      };
      socket.onclose = () => {
        if (disposed) return;
        setStatus("closed");
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (disposed) return;
      const delay = Math.min(maxBackoffMs, 1000 * 2 ** retries);
      retries += 1;
      timer = window.setTimeout(connect, delay);
    };

    connect();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
      socket?.close();
    };
  }, [enabled, maxBackoffMs, path, wsBaseUrl]);

  return status;
};
