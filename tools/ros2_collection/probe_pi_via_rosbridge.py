#!/usr/bin/env python3
"""Read-only ROS 2 introspection over rosbridge (subscriptions and rosapi calls only)."""

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp


async def send(ws, payload):
    await ws.send_str(json.dumps(payload))


async def call(ws, service, args, timeout=8.0):
    id_ = f"{service.strip('/').replace('/', '_')}_{int(time.time() * 1000)}"
    await send(ws, {"op": "call_service", "service": service, "args": args, "id": id_})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.receive_str(), timeout=max(0.1, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        if msg.get("op") == "service_response" and msg.get("id") == id_:
            if not msg.get("result", True):
                raise RuntimeError(f"{service} rejected: {msg.get('values')}")
            return msg["values"]
    raise RuntimeError(f"{service} timed out")


async def subscribe(ws, topic, msg_type, duration, throttle_ms=None):
    id_ = f"probe_{topic.strip('/').replace('/', '_')}"
    out = {"topic": topic, "type": msg_type, "throttle_rate_ms": throttle_ms}
    payload = {"op": "subscribe", "id": id_, "topic": topic, "type": msg_type}
    if throttle_ms:
        payload["throttle_rate"] = int(throttle_ms)
    await send(ws, payload)
    received = 0
    stamps = []
    shapes = set()
    frame_id = None
    t_first = None
    end = time.time() + duration
    try:
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.receive_str(), timeout=max(0.1, end - time.time()))
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("op") != "publish" or msg.get("id") != id_:
                continue
            values = msg.get("msg") or {}
            now = time.monotonic()
            if t_first is None:
                t_first = now
            received += 1
            header = values.get("header") or {}
            if frame_id is None and header.get("frame_id"):
                frame_id = header["frame_id"]
            stamp = header.get("stamp")
            if isinstance(stamp, dict):
                stamps.append(int(stamp.get("sec", 0)) * 10 ** 9 + int(stamp.get("nanosec", 0)))
            if "width" in values:
                shapes.add((values.get("width"), values.get("height"), values.get("encoding")))
    finally:
        await send(ws, {"op": "unsubscribe", "id": id_})
    window = time.monotonic() - t_first if t_first else 0.0
    out.update({
        "messages": received,
        "window_s": round(window, 3),
        "hz": round(received / window, 3) if window > 0.5 else None,
        "frame_id": frame_id,
    })
    if stamps:
        out["header_stamp_min_ns"] = min(stamps)
        out["header_stamp_max_ns"] = max(stamps)
        out["header_stamp_first_iso"] = datetime.fromtimestamp(min(stamps) / 10 ** 9, tz=timezone.utc).isoformat()
    if shapes:
        out["image_shape_variants"] = [sorted(list(s)) for s in sorted(shapes, key=str)]
    return out


async def probe(url, duration, topics):
    result = {"url": url, "started_utc": datetime.now(timezone.utc).isoformat()}
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(url, max_msg_size=128 * 1024 * 1024) as ws:
            await ws.ping()
            result["websocket"] = "connected"
            result["topics"] = (await call(ws, "/rosapi/topics", {})).get("topics", [])
            result["topic_types"] = {}
            result["subscriptions"] = []
            for topic, mtype, throttle in topics:
                try:
                    got = await call(ws, "/rosapi/topic_type", {"topic": topic}, timeout=6.0)
                    types = got.get("types") or got.get("type")
                    result["topic_types"][topic] = types[0] if isinstance(types, list) and types else types
                except Exception as exc:  # noqa: BLE001
                    result["topic_types"][topic] = f"unresolved:{type(exc).__name__}"
                try:
                    result["subscriptions"].append(
                        await subscribe(ws, topic, mtype, duration, throttle)
                    )
                except Exception as exc:  # noqa: BLE001
                    result["subscriptions"].append(
                        {"topic": topic, "error": f"{type(exc).__name__}: {exc}"}
                    )
            for key, service in (("nodes", "/rosapi/nodes"), ("services", "/rosapi/services")):
                try:
                    got = await call(ws, service, {}, timeout=20.0)
                    result[key] = got.get(key, [])
                except Exception as exc:  # noqa: BLE001
                    result[key] = []
                    result[f"{key}_error"] = f"{type(exc).__name__}: {exc}"
            result["service_count"] = len(result["services"])
    result["finished_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://192.168.19.138:9090")
    ap.add_argument("--duration", type=float, default=8.0)
    ap.add_argument("--output", default="/tmp/pi_rosbridge_probe.json")
    ap.add_argument("--topic", action="append", default=None, help="topic:type; repeats")
    args = ap.parse_args()

    def parse(spec):
        parts = spec.split(":")
        throttle = int(parts[2]) if len(parts) > 2 and parts[2] else None
        return (parts[0], parts[1], throttle)

    topics = [parse(t) for t in args.topic] if args.topic else [
        ("/ascamera/camera_publisher/rgb0/image", "sensor_msgs/msg/Image", 200),
        ("/ascamera/camera_publisher/rgb0/camera_info", "sensor_msgs/msg/CameraInfo", None),
        ("/imu", "sensor_msgs/msg/Imu", None),
        ("/joint_states", "sensor_msgs/msg/JointState", None),
        ("/odom", "nav_msgs/msg/Odometry", None),
    ]
    out = asyncio.run(probe(args.url, args.duration, topics))
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2)
    print(f"topics={len(out['topics'])} nodes={len(out['nodes'])} services={out['service_count']}")
    for sub in out["subscriptions"]:
        print(json.dumps(sub, ensure_ascii=False))
    print(f"written={args.output}")


if __name__ == "__main__":
    main()
