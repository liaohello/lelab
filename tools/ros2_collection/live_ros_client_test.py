#!/usr/bin/env python3
"""T4 real ROS 2 client tests against the live upper DDS bridge.

Runs inside the lelab-humble-dev container with ROS Humble + the amd64
lelab_mobile_interfaces overlay sourced. These are real rclpy service/action
clients calling the bridge's ~/session_control, ~/clear_estop and ~/home_arm,
which forward to the upper FastAPI app and then the car agent. No actuator
topic is published; the DDS command-path tests only exercise auth rejection.

Usage: live_ros_client_test.py <output.json> [bridge_socket]
"""
from __future__ import annotations

import json
import socket
import sys
import time

import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node

from lelab_mobile_interfaces.srv import ClearEstop, SessionControl
from lelab_mobile_interfaces.action import HomeArm

NODE = "lelab_t4_ros_client"
SC = "/lelab_upper_dds_bridge/session_control"
CE = "/lelab_upper_dds_bridge/clear_estop"
HA = "/lelab_upper_dds_bridge/home_arm"
OWNER = "t4-ros-client"


def spin_future(node, future, timeout=8.0):
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
    if not future.done():
        raise TimeoutError("future_timeout")
    return future.result()


class Client(Node):
    def __init__(self):
        super().__init__(NODE)
        self.sc = self.create_client(SessionControl, SC)
        self.ce = self.create_client(ClearEstop, CE)
        self.ha = ActionClient(self, HomeArm, HA)

    def wait_ready(self, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sc.service_is_ready() and self.ce.service_is_ready() \
                    and self.ha.wait_for_server(timeout_sec=0.5):
                return True
            time.sleep(0.2)
        return False

    def session(self, action, sid="", epoch=0, owner="", reason="", ttl=0.0,
                request_json=""):
        req = SessionControl.Request()
        req.action = action
        req.session_id = sid
        req.session_epoch = int(epoch)
        req.owner = owner
        req.reason = reason
        req.ttl_s = float(ttl)
        req.request_json = request_json
        resp = spin_future(self, self.sc.call_async(req))
        try:
            doc = json.loads(resp.response_json) if resp.response_json else {}
        except ValueError:
            doc = {"_raw": resp.response_json[:200]}
        return {"success": bool(resp.success), "state": resp.state,
                "detail": resp.detail, "doc": doc}

    def clear(self, sid="", epoch=0, owner="", request_id="t4-clear"):
        req = ClearEstop.Request()
        req.request_id = request_id
        req.owner = owner
        req.session_id = sid
        req.session_epoch = int(epoch)
        resp = spin_future(self, self.ce.call_async(req))
        return {"success": bool(resp.success), "confirmed_state": resp.confirmed_state,
                "detail": resp.detail, "motion_granted": bool(resp.motion_granted)}

    def home(self, sid, epoch, request_id="t4-home", timeout=20.0):
        goal = HomeArm.Goal()
        goal.session_id = sid
        goal.session_epoch = int(epoch)
        goal.request_id = request_id
        gh_future = self.ha.send_goal_async(goal)
        gh = spin_future(self, gh_future, timeout=timeout)
        if not gh.accepted:
            return {"accepted": False, "success": False, "state": "rejected",
                    "detail": "goal_not_accepted"}
        result_future = gh.get_result_async()
        wrapped = spin_future(self, result_future, timeout=timeout)
        res = wrapped.result
        return {"accepted": True, "success": bool(res.success), "state": res.state,
                "detail": res.detail, "status_code": int(wrapped.status)}


def socket_op(sock_path, request):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(4.0)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0]) if buf else {"ok": False, "error": "empty"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    finally:
        s.close()


def main():
    out_path = sys.argv[1]
    sock_path = sys.argv[2] if len(sys.argv) > 2 else "/work/out/dds_r1/live_bridge.sock"
    rclpy.init()
    node = Client()
    results = []
    meta = {"node": NODE, "started_at": time.time(), "service_sc": SC,
            "service_ce": CE, "action_ha": HA, "owner": OWNER}

    def record(tid, passed, detail, extra=None, status=None):
        if status is None:
            status = "pass" if passed else "fail"
        entry = {"id": tid, "status": status, "passed": bool(passed),
                 "detail": str(detail)[:600]}
        if extra is not None:
            entry["response"] = extra
        results.append(entry)
        print(f"[{status.upper()}] {tid}: {str(detail)[:160]}")

    try:
        if not node.wait_ready():
            record("services_discoverable", False, "bridge services/action not ready")
            raise SystemExit(write(out_path, meta, results, False))
        record("services_discoverable", True, "session_control/clear_estop/home_arm ready")

        snap = socket_op(sock_path, {"op": "snapshot"})
        key_id = (snap.get("bridge") or {}).get("auth_key_id") or ""
        meta["bridge_auth_key_id"] = key_id
        meta["bridge_command_enabled"] = (snap.get("bridge") or {}).get("command_enabled")
        meta["bridge_feedback_count_before"] = (snap.get("counts") or {}).get("feedback")

        # 1. status (no output)
        r = node.session("status")
        record("status_no_output", r["success"], f"state={r['state']}", r)
        baseline_state = r["state"]

        # --- leader-independent rejection paths (no session required) ---
        r = node.session("bogus_action")
        record("unknown_action_rejected",
               not r["success"] and r["detail"].startswith("unknown_action"),
               f"detail={r['detail']}", r)

        r = node.session("status", request_json="{not-valid-json")
        record("invalid_request_json_rejected",
               not r["success"] and r["detail"] == "request_json_invalid",
               f"detail={r['detail']}", r)

        # ClearEstop is the unbound recovery path; it never grants motion.
        r = node.clear(owner=OWNER)
        record("clear_estop_motion_granted_false", r["motion_granted"] is False,
               f"success={r['success']} confirmed={r['confirmed_state']} "
               f"motion_granted={r['motion_granted']}", r)

        # DDS command auth: wrong key id
        r = socket_op(sock_path, {"op": "dds_hello", "auth_key_id": "deadbeefdeadbeef",
                                  "transport": "dds_v1", "session_id": "t4",
                                  "session_epoch": 1, "calibration_bundle_hash": "x",
                                  "sender_id": "t4", "transport_epoch": "t4",
                                  "auth_ticket": "bad"})
        record("dds_hello_wrong_key_rejected",
               r.get("ok") is False and "auth_key_unknown" in str(r.get("error")),
               f"error={r.get('error')}", r)

        # DDS command auth: correct key id, wrong ticket
        r = socket_op(sock_path, {"op": "dds_frame", "auth_key_id": key_id,
                                  "auth_ticket": "bad-ticket", "sender_id": "t4",
                                  "transport_epoch": "t4", "frame": {}})
        record("dds_frame_wrong_ticket_rejected",
               r.get("ok") is False and "auth_failed" in str(r.get("error")),
               f"error={r.get('error')}", r)

        # --- session lifecycle (gated on the leader hardware being present) ---
        r = node.session("acquire", owner=OWNER, ttl=120.0,
                         request_json=json.dumps({"client_id": OWNER}))
        sid = r["doc"].get("session_id") or ""
        epoch = r["doc"].get("session_epoch") or 0
        acquired = r["success"] and bool(sid) and int(epoch) > 0
        if acquired:
            record("acquire_session", True, f"state={r['state']} sid={sid} epoch={epoch}", r)
            r = node.session("renew", sid=sid, epoch=epoch, owner=OWNER)
            record("renew_correct_identity", r["success"], f"state={r['state']}", r)
            r = node.session("release", sid=sid, epoch=int(epoch) + 999, owner=OWNER)
            record("release_wrong_epoch_rejected", not r["success"],
                   f"success={r['success']} detail={r['detail']}", r)
            r = node.session("renew", sid=sid, epoch=int(epoch) + 999, owner=OWNER)
            record("renew_wrong_epoch_rejected", not r["success"],
                   f"success={r['success']} detail={r['detail']}", r)
            r = node.home(sid, epoch)
            record("home_arm_without_permission_rejected", r.get("success") is False,
                   f"accepted={r.get('accepted')} state={r.get('state')} "
                   f"detail={r.get('detail')}", r)
            r = node.session("release", sid=sid, epoch=epoch, owner=OWNER, reason="t4_done")
            record("release_correct", r["success"], f"state={r['state']}", r)
            r = node.session("status")
            record("status_after_release", r["success"] and r["state"] == baseline_state,
                   f"state={r['state']} baseline={baseline_state}", r)
        else:
            leader_missing = "leader_serial_missing" in r["detail"]
            record("acquire_fail_closed_without_leader", leader_missing,
                   f"detail={r['detail']}", r,
                   status="pass" if leader_missing else "fail")
            meta["session_lifecycle_blocker"] = r["detail"]
            for tid in ("renew_correct_identity", "release_wrong_epoch_rejected",
                        "renew_wrong_epoch_rejected", "home_arm_without_permission_rejected",
                        "release_correct", "status_after_release"):
                record(tid, False, "blocked: leader serial absent; acquire gate refused",
                       status="blocked")

        snap2 = socket_op(sock_path, {"op": "snapshot"})
        meta["bridge_commands_published"] = (snap2.get("counts") or {}).get("commands")
        meta["bridge_commands_rejected"] = (snap2.get("counts") or {}).get("commands_rejected")
        meta["bridge_feedback_count_after"] = (snap2.get("counts") or {}).get("feedback")
        record("bridge_published_zero_command_frames",
               meta["bridge_commands_published"] == 0,
               f"commands={meta['bridge_commands_published']}")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        record("client_exception", False, f"{type(exc).__name__}:{exc}")
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    all_pass = all(item["passed"] for item in results if item["status"] != "blocked")
    write(out_path, meta, results, all_pass)
    return 0 if all_pass else 1


def write(out_path, meta, results, all_pass):
    meta["ended_at"] = time.time()
    document = {"meta": meta, "all_pass": bool(all_pass),
                "pass_count": sum(1 for r in results if r["status"] == "pass"),
                "fail_count": sum(1 for r in results if r["status"] == "fail"),
                "blocked_count": sum(1 for r in results if r["status"] == "blocked"),
                "results": results}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2, sort_keys=True)
    print(f"\n{document['pass_count']} passed, {document['fail_count']} failed, "
          f"{document['blocked_count']} blocked -> {out_path}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())