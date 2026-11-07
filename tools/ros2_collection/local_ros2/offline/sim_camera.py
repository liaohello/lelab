#!/usr/bin/env python3
"""OFFLINE ONLY - synthetic 640x400 wrist camera for the local Humble loop.

The hardware baseline stays 640x400 so the recorded schema matches the car, but the
pixels are generated: every frame carries its sequence number in a top bar of
vertical stripes and in the header frame_id, and the image is tagged
``simulated_offline``.  Nothing here opens a video device (none is mapped into the
runtime in the first place).
"""
from __future__ import annotations

import argparse
import signal

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CameraInfo, Image

WIDTH = 640
HEIGHT = 400
DATA_IDENTITY = "simulated_offline"


def pattern(index: int) -> bytes:
    canvas = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    canvas[:, :, 0] = 8 + (index * 3) % 40          # slow blue drift
    canvas[40:, 40:] = ((index * 7) % 200, (index * 11) % 200, (index * 13) % 200)
    # Top bar: one stripe per binary digit of the frame index, i.e. a readable
    # watermark that a reviewer or a test can decode back to the sequence number.
    for bit in range(12):
        if index & (1 << bit):
            canvas[0:32, 20 + bit * 30:20 + bit * 30 + 20] = 255
    return canvas.tobytes()


class SimCamera(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("lelab_sim_camera")
        self.index = 0
        self.rate_hz = args.rate_hz
        qos = QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT,
                         history=QoSHistoryPolicy.KEEP_LAST, depth=1)
        self.image_pub = self.create_publisher(Image, args.image_topic, qos)
        self.info_pub = self.create_publisher(CameraInfo, args.camera_info_topic, qos)
        self.create_timer(1.0 / self.rate_hz, self._publish)

    def _publish(self) -> None:
        stamp = self.get_clock().now().to_msg()
        message = Image()
        message.header.stamp = stamp
        message.header.frame_id = f"{DATA_IDENTITY}:{self.index}"
        message.height = HEIGHT
        message.width = WIDTH
        message.encoding = "rgb8"
        message.is_bigendian = 0
        message.step = WIDTH * 3
        message.data = pattern(self.index)
        self.image_pub.publish(message)
        info = CameraInfo()
        info.header = message.header
        info.height = HEIGHT
        info.width = WIDTH
        info.distortion_model = "plumedbot"
        info.d = [0.0, 0.0, 0.0, 0.0, 0.0]
        focal = 500.0
        info.k = [focal, 0.0, WIDTH / 2.0, 0.0, focal, HEIGHT / 2.0, 0.0, 0.0, 1.0]
        self.info_pub.publish(info)
        self.index += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-topic",
                        default="/lelab_offline/ascamera/camera_publisher/rgb0/image")
    parser.add_argument("--camera-info-topic",
                        default="/lelab_offline/ascamera/camera_publisher/rgb0/camera_info")
    parser.add_argument("--rate-hz", type=float, default=15.0)
    args = parser.parse_args()
    rclpy.init()
    node = SimCamera(args)
    print(f'{{"started": true, "data_identity": "{DATA_IDENTITY}", '
          f'"width": {WIDTH}, "height": {HEIGHT}, "rate_hz": {args.rate_hz}}}', flush=True)
    signal.signal(signal.SIGTERM, lambda *_: rclpy.shutdown())
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, RuntimeError):
        pass
    finally:
        node.destroy_node()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
