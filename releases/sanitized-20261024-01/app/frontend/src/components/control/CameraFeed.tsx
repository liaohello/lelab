import React, { useState } from "react";
import { VideoOff } from "lucide-react";
import { useApi } from "@/contexts/ApiContext";

interface CameraFeedProps {
  /** cv2 index on the API host. */
  cameraIndex?: number;
  /** Optional caption shown under the feed. */
  label?: string;
}

/** Live MJPEG feed from a camera attached to the Jetson/API host. */
const CameraFeed: React.FC<CameraFeedProps> = ({ cameraIndex, label }) => {
  const { baseUrl } = useApi();
  const [hasError, setHasError] = useState(false);
  const showVideo = cameraIndex !== undefined && !hasError;

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-700 overflow-hidden">
      <div className="aspect-[4/3] bg-gray-800 relative">
        {showVideo ? (
          <img
            src={`${baseUrl}/camera-preview/${cameraIndex}`}
            onError={() => setHasError(true)}
            alt={label ? `${label} live preview` : "Camera live preview"}
            className="w-full h-full object-cover"
          />
        ) : (
          <div className="w-full h-full flex flex-col items-center justify-center">
            <VideoOff className="w-8 h-8 text-gray-500 mb-2" />
            <span className="text-gray-500 text-sm">
              {cameraIndex !== undefined ? "Preview failed" : "No camera selected"}
            </span>
          </div>
        )}
      </div>
      {label && (
        <div className="p-2 text-sm text-gray-300 truncate border-t border-gray-800">
          {label}
        </div>
      )}
    </div>
  );
};

export default CameraFeed;
