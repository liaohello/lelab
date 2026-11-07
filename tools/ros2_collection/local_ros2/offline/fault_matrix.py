#!/usr/bin/env python3
"""OFFLINE ONLY - the live fault matrix against the production agent.

Every case here is driven through the interfaces the car exposes - the agent's HTTP routes
and the frame stream - because the point is what a real operator or a real upper computer
would experience.  The unit tests cover the same rules in isolation; this proves they hold
with the agent, the licence bridge and the control process all running as separate
processes.

Nothing here can prove a physical stop: there is no serial bus in this container.  What is
proven is that no new target is accepted or published, and that a lost authority fails
closed within its own bound.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
REPO = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "app_ros_mobile"))

from agent_session import call, wait_for_feedback, write_marker  # noqa: E402
from lelab.teleop_stream import StreamClient, StreamError  # noqa: E402

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]


class Matrix:
    def __init__(self, args):
        self.args = args
        self.marker_path = pathlib.Path(args.marker)
        self.marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        self.checks: list[dict] = []
        self.token = None
        self.epoch = None
        self.session_id = None

    def check(self, name, passed, detail=""):
        self.checks.append({"name": name, "passed": bool(passed), "detail": str(detail)[:300]})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}  {str(detail)[:170]}")

    def control(self):
        try:
            return json.loads(pathlib.Path(self.args.control_status).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def wait_for(self, predicate, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def lease(self, ttl_s=300.0):
        acquired = call(self.args.agent_url, "/control/acquire",
                        {"client_id": self.args.client_id, "ttl_s": ttl_s})
        self.token = acquired["token"]
        self.epoch = acquired["session_epoch"]
        self.session_id = f"offline-faults-{self.args.client_id}"
        bundle = self.args.bundle
        call(self.args.agent_url, "/hybrid/begin_epoch",
             {"session_id": self.session_id, "session_epoch": self.epoch,
              "calibration_bundle_hash": bundle, "channels": ["arm", "gripper"]},
             token=self.token)
        self.marker.update({"lease_token": self.token, "session_id": self.session_id,
                            "session_epoch": self.epoch, "client_id": self.args.client_id,
                            "calibration_bundle_hash": bundle})
        write_marker(self.marker_path, self.marker)
        return bundle

    def hello(self, *, token=None, session_id=None, session_epoch=None):
        client = StreamClient(port=self.args.stream_port, timeout_s=1.0)
        try:
            client.connect(lease_token=token if token is not None else self.token,
                           session_id=session_id or self.session_id,
                           session_epoch=str(session_epoch if session_epoch is not None else self.epoch))
            result = ("ok", client.stream_epoch)
        except StreamError as error:
            result = (f"refused:{error.reason}", error.detail)
        finally:
            client.close()
        return result

    def enable(self, bundle, **overrides):
        body = {"session_id": self.session_id, "session_epoch": self.epoch,
                "calibration_bundle_hash": bundle}
        body.update(overrides)
        return call(self.args.agent_url, "/hybrid/enable_output", body, token=self.token)

    # -- the cases ---------------------------------------------------------
    def run(self):
        bundle = self.lease()
        self.mark("lease_and_epoch_bound", True, f"epoch={self.epoch}")

        # The bridge publishes on its own heartbeat, so the mirror is given one interval
        # before the matrix calls the absence of a licence a failure.
        authorised = self.wait_for(lambda: self.control().get("licence", {}).get("authorised"))
        licence = (self.control().get("licence", {}).get("licence") or {})
        self.check("mirror_carries_the_agents_token_digest",
                   authorised and licence.get("outputs_enabled") is False
                   and len(str(licence.get("token_sha256") or "")) >= 16,
                   {"generation": licence.get("control_generation"),
                    "outputs": licence.get("outputs_enabled"),
                    "token_digest": str(licence.get("token_sha256"))[:12]})

        ok, detail = self.hello(token="a-token-the-agent-never-issued")
        self.check("hello_refused_for_a_foreign_token",
                   ok.startswith("refused") and "token" in f"{ok} {detail}", f"{ok} {detail}")
        ok, detail = self.hello(session_id="someone-elses-session")
        self.check("hello_refused_for_another_session", ok.startswith("refused") and "licensed" in str(detail),
                   f"{ok} {detail}")
        ok, detail = self.hello(session_epoch=self.epoch + 1)
        self.check("hello_refused_for_another_epoch", ok.startswith("refused"), f"{ok} {detail}")

        try:
            self.enable(bundle, session_id="offline-wrong-session")
            self.check("enable_refused_for_a_mismatched_identity", False, "accepted")
        except RuntimeError as error:
            self.check("enable_refused_for_a_mismatched_identity",
                       "hybrid_identity_mismatch" in str(error), str(error)[:120])

        # Enabling is refused until six readbacks are in hand, and the board needs a
        # moment after a stack start to produce a full scan.
        self.check("readback_complete_before_enable",
                   wait_for_feedback(self.args.agent_url).get("available") is True)
        self.enable(bundle)
        self.check("enable_grants_output_permission",
                   self.wait_for(lambda: ((self.control().get("licence", {}).get("licence") or {})
                                           .get("outputs_enabled") is True)))

        before = self.control().get("counters", {}).get("frames_emitted", 0)
        submitted = self.send_frames(20)
        after_enable = self.control().get("counters", {}).get("frames_emitted", 0)
        self.check("frames_emit_under_the_agents_licence",
                   submitted > 0 and after_enable > before,
                   f"{submitted} submitted, emitted {before} -> {after_enable}")
        # Every later "nothing moved" check is only meaningful against emission that actually
        # happened, so the baseline is stated here rather than assumed.
        self.emitting_baseline = after_enable

        call(self.args.agent_url, "/control/estop", {"reason": "fault_matrix"}, token=self.token)
        self.check("agent_estop_withdraws_the_permission",
                   self.wait_for(lambda: ((self.control().get("licence", {}).get("licence") or {})
                                          .get("outputs_enabled") is False)),
                   "the mirrored output permission went false")
        estop_baseline = self.control().get("counters", {}).get("frames_emitted", 0)
        self.send_frames(15)
        self.check("agent_estop_stops_emission",
                   self.control().get("counters", {}).get("frames_emitted", 0) == estop_baseline
                   and self.control().get("outputs_enabled") is False,
                   f"{estop_baseline} -> {self.control().get('counters', {}).get('frames_emitted')}"
                   f", driver outputs={self.control().get('outputs_enabled')}")

        call(self.args.agent_url, "/control/renew", {"token": self.token}, token=self.token)
        after_renew = self.control().get("counters", {}).get("frames_emitted", 0)
        self.send_frames(10)
        self.check("renew_does_not_resume_motion",
                   self.control().get("counters", {}).get("frames_emitted", 0) == after_renew,
                   f"still {self.control().get('counters', {}).get('frames_emitted')}")

        try:
            self.enable(bundle)
            self.check("enable_refused_while_the_stop_is_latched", False, "accepted")
        except RuntimeError as error:
            self.check("enable_refused_while_the_stop_is_latched",
                       "estop" in str(error) or "trusted_feedback" in str(error), str(error)[:120])

        call(self.args.agent_url, "/control/clear_estop", {"token": self.token}, token=self.token)
        self.check("cleared_stop_still_has_no_permission",
                   ((self.control().get("licence", {}).get("licence") or {})
                    .get("outputs_enabled") is False))
        self.check("readback_complete_after_clear",
                   wait_for_feedback(self.args.agent_url).get("available") is True)
        self.enable(bundle)
        resumed_before = self.control().get("counters", {}).get("frames_emitted", 0)
        self.send_frames(20)
        self.check("motion_resumes_only_after_clear_and_enable",
                   self.control().get("counters", {}).get("frames_emitted", 0) > resumed_before,
                   f"{resumed_before} -> {self.control().get('counters', {}).get('frames_emitted')}")

        self.expiring_lease()
        if self.args.kill_agent:
            self.agent_sigkill()

    def expiring_lease(self):
        """A lease that runs out must take the permission with it, without a revoke message."""
        # The lease is exclusive by design, so the matrix hands its own back before a
        # second client can hold one.
        call(self.args.agent_url, "/control/release", {"token": self.token}, token=self.token)
        short = call(self.args.agent_url, "/control/acquire",
                     {"client_id": f"{self.args.client_id}-short", "ttl_s": 5.0})
        # The agent clamps a ttl to at least 5 s, so this waits out the shortest lease it
        # will accept rather than pretending a shorter one exists.
        bundle = self.args.bundle
        call(self.args.agent_url, "/hybrid/begin_epoch",
             {"session_id": "offline-short-lease", "session_epoch": short["session_epoch"],
              "calibration_bundle_hash": bundle, "channels": ["arm", "gripper"]},
             token=short["token"])
        # Binding a new epoch re-creates the readback store under the new calibration
        # identity, so the next enable has to wait for a full scan.  That is the gate
        # working, not a flake: enabling on an empty store would grant motion on nothing.
        self.check("short_lease_readback_ready",
                   wait_for_feedback(self.args.agent_url).get("available") is True)
        call(self.args.agent_url, "/hybrid/enable_output",
             {"session_id": "offline-short-lease", "session_epoch": short["session_epoch"],
              "calibration_bundle_hash": bundle}, token=short["token"])
        self.check("short_lease_grants_permission",
                   self.wait_for(lambda: ((self.control().get("licence", {}).get("licence") or {})
                                          .get("outputs_enabled") is True)))
        deadline = time.monotonic() + 12.0
        lost = self.wait_for(lambda: self.control().get("licence", {}).get("authorised") is False,
                            timeout=deadline - time.monotonic())
        state = self.control()
        # An explicit revoke is the wanted answer: it means the bridge noticed the lease
        # had gone, rather than the control process merely timing out on a stale mirror.
        self.check("expired_lease_revokes_the_mirror",
                   lost and state.get("licence", {}).get("revoked_reason")
                   in ("car_lease_released", "heartbeat_expired"),
                   {"revoked": state.get("licence", {}).get("revoked_reason"),
                    "outputs_enabled": state.get("outputs_enabled")})
        self.check("expired_lease_stops_emission", state.get("outputs_enabled") is False,
                   state.get("outputs_enabled"))
        try:
            call(self.args.agent_url, "/control/release", {"token": short["token"]},
                 token=short["token"])
        except RuntimeError as error:
            # The lease was the thing under test and it has already run out; a release
            # refusal on an expired token is the expected tail of that case, not a failure.
            self.check("expired_lease_release_refused_as_expected",
                       "control_expired" in str(error), str(error)[:120])
        self.token, self.epoch, self.session_id = None, None, None

    def agent_sigkill(self):
        """The agent dying mid-session must not leave the control process emitting."""
        # A licence has to be live first, or the case would only observe the revocation the
        # previous case already caused.
        bundle = self.lease()
        self.check("sigkill_readback_ready",
                   wait_for_feedback(self.args.agent_url).get("available") is True)
        self.enable(bundle)
        if not self.wait_for(lambda: self.control().get("licence", {}).get("authorised")):
            self.check("sigkill_licence_live_before_kill", False, self.control().get("licence"))
            return
        self.check("sigkill_licence_live_before_kill", True,
                   self.control().get("licence", {}).get("licence", {}).get("control_generation"))
        subprocess.run(["docker", "exec", self.args.container, "bash", "-lc",
                        f"python3 {self.args.ops} stop-agent --state-dir {self.args.container_state} "
                        "--timeout-s 0.1 >/dev/null 2>&1; "
                        f"for p in $(ls /proc | grep -E '^[0-9]+$'); do "
                        f"c=$(tr '\\0' ' ' < /proc/$p/cmdline 2>/dev/null); "
                        f"case \"$c\" in *lelab_mobile_agent.agent*) kill -9 $p 2>/dev/null;; esac; done"],
                       capture_output=True, text=True)
        gone = self.wait_for(lambda: self.control().get("licence", {}).get("authorised") is False,
                            timeout=3.0)
        state = self.control()
        self.check("killed_agent_leaves_no_live_licence",
                   gone and state.get("licence", {}).get("revoked_reason") in
                   ("ipc_eof", "heartbeat_expired"),
                   {"revoked": state.get("licence", {}).get("revoked_reason"),
                    "outputs_enabled": state.get("outputs_enabled"),
                    "hardware_effect": "not_tested_no_serial_port_reachable"})

    def mark(self, name, passed, detail=""):
        self.check(name, passed, detail)

    def send_frames(self, count: int) -> int:
        from lelab.hybrid_channel import command_v1
        from lelab.teleop_frame_v1 import frame_v1

        client = StreamClient(port=self.args.stream_port, timeout_s=1.0)
        client.connect(lease_token=self.token, session_id=self.session_id,
                       session_epoch=str(self.epoch))
        client.start_writer()
        # The welcome carries the window that was open at connect time, but only the
        # periodic push carries the remaining lifetime the sender paces on, so wait for a
        # window that actually says how much of itself is left.
        deadline = time.monotonic() + 3.0
        while (client.current_window or {}).get("window_remaining_ms") is None \
                and time.monotonic() < deadline:
            client.pump(0.05)
            time.sleep(0.02)
        sent = 0
        for tick in range(1, count + 1):
            window = client.current_window
            if window is None or float(window.get("window_remaining_ms") or 0) < 60.0:
                time.sleep(0.05)
                continue
            common = dict(session_id=self.session_id, session_epoch=str(self.epoch),
                          source="so101_leader", sample_seq=tick, ttl_ms=150,
                          calibration_bundle_hash=self.args.bundle, clutch=True)
            arm = command_v1(channel="arm", seq=tick,
                             target={"joint_names": ARM_JOINTS,
                                     "positions_rad": [0.02 * tick] * 5}, **common)
            gripper = command_v1(channel="gripper", seq=tick,
                                 target={"joint_name": "r_joint", "position_rad": 0.02}, **common)
            client.submit_frame(frame_v1(session_id=self.session_id,
                                         session_epoch=str(self.epoch),
                                         calibration_bundle_hash=self.args.bundle,
                                         frame_seq=tick, stream_epoch=client.stream_epoch,
                                         sample_seq=tick,
                                         freshness_window_id=window["window_id"],
                                         commands={"arm": arm, "gripper": gripper}))
            sent += 1
            time.sleep(0.1)
        time.sleep(0.4)
        client.close()
        return sent

    def report(self):
        return {"data_identity": "simulated_offline", "training_eligible": False,
                "hardware_effect": "not_tested_no_serial_port_reachable",
                "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "checks": self.checks,
                "failed": [item["name"] for item in self.checks if not item["passed"]]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default="http://127.0.0.1:18130")
    parser.add_argument("--stream-port", type=int, default=18122)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--control-status", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--client-id", default="offline-faults")
    parser.add_argument("--container", default="lelab-humble-dev")
    parser.add_argument("--ops", default="/work/lelab/tools/ros2_collection/local_ros2/offline/container_ops.py")
    parser.add_argument("--container-state", default="/work/out/local_humble/docker/state")
    parser.add_argument("--kill-agent", action="store_true",
                        help="SIGKILL the agent to prove the mirror fails closed (needs a restart)")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    matrix = Matrix(args)
    try:
        matrix.run()
    except Exception as error:  # noqa: BLE001 - the report must survive a refused call
        matrix.check("matrix_completed", False, f"{type(error).__name__}: {error}")
    else:
        matrix.check("matrix_completed", True)
    document = matrix.report()
    path = pathlib.Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8")
    print(f"report: {path}  failed: {document['failed'] or 'none'}")
    return 1 if document["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
