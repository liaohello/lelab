#!/usr/bin/env python3
"""Host-side teleop frame driver for the offline Humble closed loop.

Runs on the upper computer (Python 3.12) and speaks the same shared contract that
the car process speaks, over the real frame stream socket.  The leader input is a
synthetic sample stream: the SO-101 USB reader is deliberately not involved, so
this can run with the car powered off.

Exit code is non-zero when any assertion fails, and the JSON report says which.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "app_ros_mobile"))

from lelab.hybrid_channel import HybridProtocolError, command_v1  # noqa: E402
from lelab.teleop_frame_v1 import frame_v1  # noqa: E402
from lelab.teleop_stream import StreamClient, StreamError, token_digest  # noqa: E402

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
STATE = Path(os.environ.get("LROS_STATE_DIR", REPO / "work/ros2_collection/local_humble/state"))


def read_state(name: str) -> dict:
    path = STATE / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def board_outputs(settle_s: float = 1.2) -> int:
    """The board's output count, once two consecutive reads agree.

    The board writes its state from its own callback, so a single read can land mid-write
    of a count that a message just changed.  Every check below compares two of these, and
    a flaky read would look exactly like an extra message reaching the servos.
    """
    # The board writes its state on its own timer, so a single read can precede the
    # message that is already on the wire.  Two equal reads a quarter second apart are the
    # quiet point; without that, the stop's own hold frame looks like an extra emission.
    quiet = 0
    previous = read_state("sim_board_owner.json").get("outputs_received", 0)
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        time.sleep(0.1)
        current = read_state("sim_board_owner.json").get("outputs_received", 0)
        quiet = quiet + 1 if current == previous else 0
        previous = current
        if quiet >= 3:
            return current
    return previous


def settle_counts(stable_s: float = 0.5, timeout_s: float = 3.0) -> tuple[int, dict, dict]:
    """Board message count plus the control process counters, once they stop moving.

    The board writes when a message lands and the control process writes once per tick, so
    either side can be one step behind the other for a few tens of milliseconds.  Reading
    until both are quiet is what makes the per-frame accounting below a measurement rather
    than a race: a mismatch here would otherwise be indistinguishable from a real extra or
    missing servo message.
    """
    deadline = time.monotonic() + timeout_s
    quiet_since = None
    previous = None
    while time.monotonic() < deadline:
        board = read_state("sim_board_owner.json")
        control = read_state("car_control_status.json")
        current = (board.get("outputs_received", 0),
                   control.get("publish_count", 0),
                   (control.get("counters", {}).get("frames_emitted", 0),
                    control.get("counters", {}).get("hold_frames", 0),
                    control.get("stop_count", 0)))
        if current == previous:
            if quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= stable_s:
                return current[0], current[1], current[2]
        else:
            quiet_since = None
            previous = current
        time.sleep(0.1)
    return previous[0], previous[1], previous[2]


def build_frame(*, session_id, epoch, bundle, frame_seq, sample_seq, window_id,
                stream_epoch, positions, gripper_rad, ttl_ms=150, clutch=True,
                arm_seq=None, gripper_seq=None, leader_rad=None, pre_clip_rad=None):
    target = {"joint_names": ARM_JOINTS, "positions_rad": list(positions)}
    if leader_rad is not None:
        target["leader_rad"] = list(leader_rad)
    if pre_clip_rad is not None:
        target["pre_clip_rad"] = list(pre_clip_rad)
    arm = command_v1(session_id=session_id, session_epoch=epoch, channel="arm",
                     source="so101_leader", seq=arm_seq or frame_seq,
                     sample_seq=sample_seq, ttl_ms=ttl_ms, calibration_bundle_hash=bundle,
                     target=target, clutch=clutch)
    gripper = command_v1(session_id=session_id, session_epoch=epoch, channel="gripper",
                         source="so101_leader", seq=gripper_seq or frame_seq,
                         sample_seq=sample_seq, ttl_ms=ttl_ms,
                         calibration_bundle_hash=bundle,
                         target={"joint_name": "r_joint", "position_rad": gripper_rad},
                         clutch=clutch)
    return frame_v1(session_id=session_id, session_epoch=epoch, calibration_bundle_hash=bundle,
                    frame_seq=frame_seq, stream_epoch=stream_epoch, sample_seq=sample_seq,
                    freshness_window_id=window_id,
                    commands={"arm": arm, "gripper": gripper})


class Driver:
    def __init__(self, args):
        self.args = args
        marker = json.loads(Path(args.marker).read_text(encoding="utf-8"))
        self.marker = marker
        self.session_id = marker["session_id"]
        self.epoch = str(marker["session_epoch"])
        self.bundle = marker["calibration_bundle_hash"]
        self.client = StreamClient(port=marker["stream_port"], timeout_s=1.0)
        self.receipts: list[dict] = []
        self.checks: list[dict] = []
        self.samples: list[dict] = []
        self.sent_at: dict[int, float] = {}
        self.rtt_ms: list[float] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}  {detail}")

    def connect(self):
        welcome = self.client.connect(lease_token=self.marker["lease_token"],
                                      session_id=self.session_id, session_epoch=self.epoch)
        self.welcome = welcome
        return welcome

    def pump_receipts(self) -> list[dict]:
        try:
            self.client.pump(0.0)
        except StreamError:
            pass
        got = self.client.drain_receipts()
        now = time.monotonic()
        for receipt in got:
            submitted = self.sent_at.get(receipt.get("frame_seq"))
            if submitted is not None:
                self.rtt_ms.append((now - submitted) * 1000.0)
        self.receipts.extend(got)
        return got

    def await_receipt(self, frame_seq: int, timeout_s: float = 3.0) -> dict | None:
        """Poll the socket for this frame's receipt instead of guessing a sleep."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.pump_receipts()
            matching = [r for r in self.receipts if r.get("frame_seq") == frame_seq]
            if matching:
                return matching[-1]
            time.sleep(0.02)
        return None

    LEADER_LIMITS = {"joint1": (-2.052507, 2.086018), "joint2": (-2.052507, 1.931032),
                     "joint3": (-2.094395, 2.052507), "joint4": (-1.675516, 2.010619),
                     "joint5": (-1.759292, 1.717404)}

    def leader_sample(self, frame_seq: int) -> tuple[list, list]:
        """A simulated leader wave on six distinct axes, clipped the way the sender clips.

        joint5 is deliberately pushed past its calibrated limit: the recorded row then
        carries a pre-clip value that differs from the sent target, so the clipping is
        visible in the data instead of being asserted about it.
        """
        base = 0.05 * (frame_seq % 20)
        leader = [base + 0.02 * index for index in range(5)]
        leader[4] += 1.0
        sent = []
        for name, value in zip(ARM_JOINTS, leader):
            low, high = self.LEADER_LIMITS[name]
            sent.append(min(max(value, low), high))
        return leader, sent

    def send_at(self, hz: float, seconds: float, start_seq: int = 1) -> int:
        """Paced sender: one slot per period, skipped slots do not borrow the future.

        Advancing the schedule on a skipped slot would push the next deadline ahead of
        real time, and the sender would then sleep until it catches up - which looks
        like a stalled control loop but is only a pacing bug.
        """
        period = 1.0 / hz
        start = time.monotonic()
        deadline = start + seconds
        sent = 0
        frame_seq = start_seq
        tick = 0
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            target = start + tick * period
            tick += 1
            if now < target:
                time.sleep(target - now)
            window = self.client.current_window
            if window is None:
                self.check("window_issued", False, "the control side never issued a window")
                return sent
            remaining = float(window.get("window_remaining_ms") or 0.0)
            if remaining < self.args.window_margin_ms:
                # Attaching a nearly-closed window would only produce a refusal, so
                # the sender waits for the next one and counts the pause instead.
                self.skips = getattr(self, "skips", 0) + 1
                self.pump_receipts()
                continue
            try:
                leader, targets = self.leader_sample(frame_seq)
                frame = build_frame(session_id=self.session_id, epoch=self.epoch,
                                    bundle=self.bundle, frame_seq=frame_seq,
                                    sample_seq=frame_seq, window_id=window["window_id"],
                                    stream_epoch=self.client.stream_epoch,
                                    positions=targets, leader_rad=leader,
                                    pre_clip_rad=leader,
                                    gripper_rad=0.02 * (frame_seq % 10))
            except HybridProtocolError as error:
                self.check("frame_build", False, str(error))
                return sent
            self.client.submit_frame(frame)
            self.sent_at[frame_seq] = time.monotonic()
            self.samples.append({"frame_seq": frame_seq, "at": self.sent_at[frame_seq]})
            sent += 1
            frame_seq += 1
            self.pump_receipts()
        self.pump_receipts()
        return sent

    # -- phases ------------------------------------------------------------
    def phase_preview(self) -> None:
        outputs_before = board_outputs()
        publish_before = read_state("car_control_status.json").get("publish_count", -1)
        stops_before = read_state("car_control_status.json").get("stop_count", -1)
        sent = self.send_at(self.args.hz, self.args.duration_s)
        after_count = board_outputs()
        self.check("preview_frames_submitted", sent >= max(1, int(self.args.hz * self.args.duration_s * 0.6)),
                   f"submitted {sent} frames")
        self.check("preview_zero_board_outputs", after_count == outputs_before,
                   f"board outputs {outputs_before} -> {after_count}")
        after_control = read_state("car_control_status.json")
        self.check("preview_publish_count_zero",
                   after_control.get("publish_count", -1) - publish_before == 0
                   and after_control.get("stop_count", -1) - stops_before == 0,
                   "the control process published nothing, holds included, while the "
                   "licence kept outputs off")
        refusals = [r for r in self.receipts if r.get("state") == "refused"]
        reasons = sorted({f"{r.get('reason')}: {str(r.get('detail', ''))[:60]}" for r in refusals})
        self.check("preview_refusal_reason_is_licence",
                   any("licence" in reason for reason in reasons),
                   f"reasons seen: {reasons[:3]}")

    def phase_follow(self) -> None:
        # Both counters are cumulative for their own process, and either process may
        # outlive this script, so the phase measures deltas from its own start.
        control_before = read_state("car_control_status.json")
        board_before, publish_before, (emitted_before, holds_before, stops_before) = settle_counts()
        board_positions_before = read_state("sim_board_owner.json").get("positions_applied", 0)
        counters_before = control_before.get("counters", {})
        sent = self.send_at(self.args.hz, self.args.duration_s)
        board_outputs, publish_count, (emitted_total, holds_total, stops_total) = settle_counts()
        board = read_state("sim_board_owner.json")
        control = read_state("car_control_status.json")
        admitted = [r for r in self.receipts if r.get("state") == "admitted"]
        counters = control.get("counters", {})
        emitted = emitted_total - emitted_before
        # A hold frame is a publication too, and it is not an admitted frame: the two
        # counters have to be reconciled rather than waved through with a tolerance.
        holds = holds_total - holds_before
        stops = stops_total - stops_before
        outputs = board_outputs - board_before
        published = publish_count - publish_before
        positions = board.get("positions_applied", 0) - board_positions_before
        self.check("follow_frames_admitted", len(admitted) >= max(1, sent * 0.8),
                   f"{len(admitted)} of {sent} frames admitted")
        self.check("follow_board_messages_match_driver_publishes", outputs == published,
                   f"board received {outputs}, the driver published {published}")
        self.check("follow_one_board_message_per_frame",
                   emitted > 0 and published == emitted + stops,
                   f"this phase: {emitted} frames emitted + {stops} stop/hold publishes "
                   f"= {published} driver messages")
        ids = (board.get("last_command") or {}).get("ids")
        self.check("follow_single_six_axis_write", ids == [1, 2, 3, 4, 5, 10],
                   f"last board command ids: {ids}")
        applied_ratio = positions / max(1, outputs)
        self.check("follow_six_positions_per_message", 5.9 <= applied_ratio <= 6.1,
                   f"{applied_ratio:.2f} positions per message")
        elapsed = max(0.001, time.monotonic() - (self.samples[0]["at"] if self.samples else time.monotonic()))
        hz = emitted / elapsed
        self.check("follow_effective_hz_close_to_target", hz >= self.args.hz * 0.7,
                   f"effective emit {hz:.2f} Hz against a {self.args.hz} Hz target")
        ages = control.get("feedback", {}).get("ages_ms", {})
        self.check("feedback_age_within_gate",
                   bool(ages) and max(ages.values()) <= 500.0,
                   f"oldest axis age {max(ages.values()) if ages else 'n/a'} ms")
        if self.rtt_ms:
            ordered = sorted(self.rtt_ms)
            self.check("round_trip_within_budget", ordered[int(len(ordered) * 0.95)] <= 150.0,
                       f"submit->receipt p50 {statistics.median(ordered):.1f} ms, "
                       f"p95 {ordered[int(len(ordered) * 0.95)]:.1f} ms over {len(ordered)} receipts")
        remaining = [float(r["deadline_in_ms"]) for r in admitted if "deadline_in_ms" in r]
        if remaining:
            self.check("lifetime_left_at_admission_reported", True,
                       f"median usable lifetime after admission {statistics.median(remaining):.1f} ms")

    def phase_faults(self) -> None:
        window = self.client.current_window
        # window_id stays out of the shared base: each fault below names a different
        # window on purpose.
        base = dict(session_id=self.session_id, epoch=self.epoch, bundle=self.bundle,
                    stream_epoch=self.client.stream_epoch)
        live_window_id = window["window_id"]
        board_before = board_outputs()
        control_before = read_state("car_control_status.json").get("counters", {})

        # Each fault is submitted and awaited before the next: the sender is
        # latest-wins by design, so back-to-back submissions would supersede each
        # other and the refusals would never be observed.
        bad = build_frame(**base, frame_seq=9001, sample_seq=9001, window_id=live_window_id,
                          positions=[0.1] * 5, gripper_rad=0.02)
        bad["commands"]["gripper"]["target"]["position_rad"] = float("inf")
        self.client.submit_frame(bad)
        receipt = self.await_receipt(9001)
        self.check("fault_nonfinite_gripper_refused_whole_frame",
                   bool(receipt) and receipt.get("state") == "refused",
                   f"receipt: {receipt}")

        # 2. an expired window must be refused, not revalidated
        window = self.client.current_window
        stale = build_frame(**base, frame_seq=9002, sample_seq=9002, positions=[0.1] * 5,
                            gripper_rad=0.02, window_id=(window or {}).get("window_id", 0) - 1000)
        self.client.submit_frame(stale)
        receipt = self.await_receipt(9002)
        self.check("fault_stale_window_refused",
                   bool(receipt) and receipt.get("reason") in ("stale_window", "window_expired"),
                   f"reason: {receipt.get('reason') if receipt else 'none'}")

        # 3. a TTL above the car gate's ceiling must be refused
        window = self.client.current_window
        long_ttl = build_frame(**base, frame_seq=9003, sample_seq=9003,
                               window_id=(window or {}).get("window_id"), positions=[0.1] * 5,
                               gripper_rad=0.02)
        for channel in ("arm", "gripper"):
            long_ttl["commands"][channel]["ttl_ms"] = 500
        self.client.submit_frame(long_ttl)
        receipt = self.await_receipt(9003)
        self.check("fault_ttl_ceiling_refused",
                   bool(receipt) and "ttl" in str(receipt.get("reason", "")),
                   f"reason: {receipt.get('reason') if receipt else 'none'}")

        # a refused frame consumes nothing: the next valid frame still works
        window = self.client.current_window
        good = build_frame(**base, frame_seq=9004, sample_seq=9004,
                           window_id=(window or {}).get("window_id"), positions=[0.1] * 5,
                           gripper_rad=0.02)
        self.client.submit_frame(good)
        receipt = self.await_receipt(9004)
        self.check("fault_valid_frame_still_admitted_after_refusals",
                   bool(receipt) and receipt.get("state") == "admitted",
                   f"receipt: {receipt}")

        # 4. emergency stop must latch and refuse the frames that follow it
        self.client.send({"type": "preempt", "action": "estop", "reason": "acceptance_rehearsal"})
        estop_deadline = time.monotonic() + 3.0
        estop_receipt = None
        while time.monotonic() < estop_deadline and estop_receipt is None:
            self.pump_receipts()
            stopped = [r for r in reversed(self.receipts) if r.get("state") == "stopped"]
            estop_receipt = stopped[-1] if stopped else None
            time.sleep(0.02)
        self.check("fault_estop_receipt", bool(estop_receipt), f"receipt: {estop_receipt}")
        deadline = time.monotonic() + 3.0
        after_estop = {}
        while time.monotonic() < deadline:
            after_estop = read_state("car_control_status.json")
            if after_estop.get("latched_fault") == "estop":
                break
            time.sleep(0.1)
        self.check("fault_estop_disables_outputs", not after_estop.get("outputs_enabled", True),
                   f"driver outputs off after the stop: {after_estop.get('outputs_enabled')}")
        # The stop itself publishes one hold message, and the board counts it on its own
        # thread.  Settle first, then measure: what must not move is the count across the
        # frames sent after the stop, not the stop's own hold.
        board_before_estop_frames = board_outputs()
        sent_after = self.send_at(self.args.hz, 1.0, start_seq=9100)
        board_after_estop = board_outputs()
        self.check("fault_estop_stops_emission",
                   board_after_estop == board_before_estop_frames,
                   f"board outputs stayed at {board_after_estop} across {sent_after} frames "
                   f"sent after the stop")
        latched = read_state("car_control_status.json").get("latched_fault")
        self.check("fault_estop_latched", latched == "estop", f"latched_fault={latched}")
        self.pump_receipts()
        refused_after = [r for r in self.receipts if r.get("frame_seq", 0) >= 9100]
        self.check("fault_estop_frames_refused",
                   bool(refused_after) and all("fault_latched" in str(r.get("reason"))
                                               for r in refused_after),
                   f"{len(refused_after)} receipts after the stop, reasons: "
                   f"{sorted({str(r.get('reason')) for r in refused_after})}")

        # 5. a licence that dies mid-follow must fail closed without waiting for a TTL
        control = read_state("car_control_status.json")
        self.check("fault_refusals_recorded_without_side_effects",
                   control.get("counters", {}).get("frames_emitted", 0)
                   <= control_before.get("frames_emitted", 0) + sent_after,
                   "no emissions were attributed to the refused frames")
        return

    def report(self) -> dict:
        return {
            "phase": self.args.phase,
            "data_identity": "simulated_offline",
            "training_eligible": False,
            "hardware_effect": "not_tested_no_serial_port_reachable",
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "target_hz": self.args.hz,
            "sender_window_skips": getattr(self, "skips", 0),
            "duration_s": self.args.duration_s,
            "checks": self.checks,
            "failed": [check["name"] for check in self.checks if not check["passed"]],
            "stream_stats": self.client.stats(),
            "car_control": read_state("car_control_status.json"),
            "sim_board": read_state("sim_board_owner.json"),
            "receipt_sample": self.receipts[-5:],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True,
                        choices=["preview", "follow", "faults", "stop"])
    parser.add_argument("--hz", type=float, default=10.0)
    parser.add_argument("--duration-s", type=float, default=8.0)
    parser.add_argument("--marker", default=str(REPO / "work/ros2_collection/local_humble/run/offline_stack.json"))
    parser.add_argument("--report", default="")
    parser.add_argument("--window-margin-ms", type=float, default=60.0)
    args = parser.parse_args()
    driver = Driver(args)
    if args.phase == "stop":
        # A stop is already latched when the disarm comes here after the fault rehearsal,
        # and a latched process refuses a new hello by design.  That is the disarmed state,
        # not a failure to pause, so say so instead of raising through the launcher.
        if not driver.marker.get("lease_token"):
            driver.check("stop_no_lease_to_pause", True,
                         "the session marker holds no lease token: the car has already "
                         "given control back, so there is nothing to pause")
            document = driver.report()
            if args.report:
                Path(args.report).parent.mkdir(parents=True, exist_ok=True)
                Path(args.report).write_text(json.dumps(document, indent=2, sort_keys=True,
                                                        default=str) + "\n", encoding="utf-8")
            print("nothing to pause: no lease recorded")
            return 0
        try:
            driver.connect()
        except StreamError as error:
            refusal = f"{error.reason} {error.detail}"
            # Every one of these is the disarmed state seen from the sender's side: a
            # latched stop, an expired licence, or an agent that revoked it.  In all of
            # them there is nothing to pause, and the launcher's own revoke step is what
            # actually ends the permission.
            if not any(token in refusal for token in
                       ("fault_latched", "no_licence", "not_authorised", "heartbeat_expired")):
                raise
            driver.check("stop_nothing_to_pause", True,
                         f"the control process refuses a new hello ({refusal[:90]}); "
                         "nothing left to pause")
            document = driver.report()
            if args.report:
                Path(args.report).parent.mkdir(parents=True, exist_ok=True)
                Path(args.report).write_text(json.dumps(document, indent=2, sort_keys=True,
                                                        default=str) + "\n", encoding="utf-8")
            print("nothing to pause:", refusal)
            return 0
    else:
        driver.connect()
    print(f"connected, stream_epoch={driver.client.stream_epoch}, "
          f"window={driver.client.current_window}")
    driver.client.start_writer()
    if args.phase == "preview":
        driver.phase_preview()
    elif args.phase == "follow":
        driver.phase_follow()
    elif args.phase == "faults":
        driver.phase_faults()
    elif args.phase == "stop":
        driver.client.send({"type": "preempt", "action": "pause_all", "reason": "acceptance_end"})
        time.sleep(0.5)
        driver.check("stop_pause_receipt",
                     not read_state("car_control_status.json").get("outputs_enabled", True),
                     "the control process stopped emitting after pause_all")
    driver.client.close()
    document = driver.report()
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
                                     encoding="utf-8")
    return 1 if document["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
