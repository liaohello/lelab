"""Publish sensor_msgs/Joy from the receiver's js device, without the `joy` package.

The vendor image ships `ros-humble-joy`, but it is built against jammy while this
container's apt sources point at noble, so any later apt install can silently remove
it.  This node is the replacement: it reuses the same read-only js reader the bench
uses, so `/joy` keeps its meaning (raw axes / 32767, raw buttons) and the control
process needs no change.

It publishes only while the device is actually connected: a stale value must never
look like a live stick.  The receiver's own failsafe (axes drop to zero when the
transmitter dies) still reaches us as a normal axis event.
"""
from __future__ import annotations

import argparse

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy
from sensor_msgs.msg import Joy

from .linux_gamepad import LinuxJoystick

DEFAULT_PORT = "platform-xhci-hcd.1-usb-0:2:1.0-joystick"


class JoySource(Node):
    def __init__(self, port: str, rate_hz: float):
        super().__init__("lelab_joy_source")
        self.reader = LinuxJoystick(port)
        self.publisher = self.create_publisher(
            Joy, "/joy",
            QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT,
                       history=QoSHistoryPolicy.KEEP_LAST, depth=1))
        self.generation = None
        self.lost = 0
        self.create_timer(1.0 / rate_hz, self._tick)

    def _tick(self):
        now = self.get_clock().now().nanoseconds
        self.reader.poll(now)
        status = self.reader.status()
        if status["generation"] != self.generation:
            self.generation = status["generation"]
            self.get_logger().info(
                f"js device generation {self.generation} "
                f"({'connected' if status['connected'] else 'disconnected'})")
        if not status["connected"]:
            self.lost += 1
            return
        message = Joy()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = "joy"
        message.axes = [value / 32767.0 for value in status["axes_raw"]]
        message.buttons = [int(value) for value in status["buttons_raw"]]
        self.publisher.publish(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--rate", type=float, default=20.0)
    args = parser.parse_args()
    rclpy.init()
    node = JoySource(args.port, args.rate)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.reader.close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
