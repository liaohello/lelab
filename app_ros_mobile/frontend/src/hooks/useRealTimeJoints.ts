import { useEffect, useRef, useState, useCallback } from "react";
import { URDFViewerElement } from "@/lib/urdfViewerHelpers";
import { useApi } from "@/contexts/ApiContext";

export type ReadStatus = "measured" | "read_failed" | "partial";

export interface JointUpdate {
  type: "joint_update";
  // null when that side's read failed or came back incomplete: a missing arm is shown
  // as missing, never as a zero pose.
  joints: Record<string, number> | null;
  // The leader reading that produced this iteration's target. Absent from a server
  // built before the desktop teleop stream carried both arms, so consumers must
  // treat it as optional rather than assuming a live leader.
  leader?: Record<string, number> | null;
  follower_status?: ReadStatus;
  leader_status?: ReadStatus;
  follower_missing?: string[];
  leader_missing?: string[];
  /** A preview session reads and broadcasts both arms but writes nothing to the bus. */
  preview?: boolean;
  target_sent?: boolean;
  target?: Record<string, number> | null;
  clutch?: { engage_requested: boolean; engaged: boolean; aligned: boolean; refusal: string | null };
  timestamp: number;
}

interface UseRealTimeJointsProps {
  // The 3D viewer is optional: a page that only charts the stream (the /so101
  // workbench) subscribes through onFrame without mounting an URDF canvas.
  viewerRef?: React.RefObject<URDFViewerElement | null>;
  enabled?: boolean;
  websocketUrl?: string;
  onFrame?: (frame: JointUpdate) => void;
}

const INITIAL_RECONNECT_DELAY_MS = 1000;
const MAX_RECONNECT_DELAY_MS = 30000;

export const useRealTimeJoints = ({
  viewerRef,
  enabled = true,
  websocketUrl,
  onFrame,
}: UseRealTimeJointsProps) => {
  const { wsBaseUrl } = useApi();
  const finalWebSocketUrl = websocketUrl || `${wsBaseUrl}/ws/joint-data`;

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_DELAY_MS);
  const intentionallyClosedRef = useRef(false);
  const [isConnected, setIsConnected] = useState(false);
  // Held in a ref so a new chart callback identity does not tear down the socket.
  const onFrameRef = useRef(onFrame);
  useEffect(() => {
    onFrameRef.current = onFrame;
  }, [onFrame]);

  const updateJointValues = useCallback(
    (joints: Record<string, number>) => {
      const viewer = viewerRef?.current;
      if (!viewer || typeof viewer.setJointValue !== "function") return;
      Object.entries(joints).forEach(([jointName, value]) => {
        try {
          viewer.setJointValue(jointName, value);
        } catch (error) {
          console.warn(`Failed to set joint ${jointName}:`, error);
        }
      });
    },
    [viewerRef]
  );

  useEffect(() => {
    if (!enabled) return;

    intentionallyClosedRef.current = false;

    const connect = () => {
      if (intentionallyClosedRef.current) return;

      let ws: WebSocket;
      try {
        ws = new WebSocket(finalWebSocketUrl);
      } catch (error) {
        console.error("Failed to create WebSocket:", error);
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        setIsConnected(true);
        reconnectDelayRef.current = INITIAL_RECONNECT_DELAY_MS;
        if (reconnectTimeoutRef.current) {
          clearTimeout(reconnectTimeoutRef.current);
          reconnectTimeoutRef.current = null;
        }
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as JointUpdate;
          if (data.type === "joint_update") {
            // The frame is forwarded even when a side carries no numbers, so the page
            // can show "read failed" instead of freezing an old pose silently.
            if (data.joints) updateJointValues(data.joints);
            onFrameRef.current?.(data);
          }
        } catch (error) {
          console.error("Error parsing WebSocket message:", error);
        }
      };

      ws.onclose = (event) => {
        setIsConnected(false);
        wsRef.current = null;
        if (intentionallyClosedRef.current) return;
        if (event.code === 1000) return; // clean close
        scheduleReconnect();
      };

      ws.onerror = () => {
        setIsConnected(false);
      };
    };

    const scheduleReconnect = () => {
      if (reconnectTimeoutRef.current) return;
      const delay = reconnectDelayRef.current;
      reconnectDelayRef.current = Math.min(
        delay * 2,
        MAX_RECONNECT_DELAY_MS
      );
      reconnectTimeoutRef.current = setTimeout(() => {
        reconnectTimeoutRef.current = null;
        connect();
      }, delay);
    };

    connect();

    return () => {
      intentionallyClosedRef.current = true;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.close(1000);
        wsRef.current = null;
      }
      setIsConnected(false);
    };
  }, [enabled, finalWebSocketUrl, updateJointValues]);

  return { isConnected };
};
