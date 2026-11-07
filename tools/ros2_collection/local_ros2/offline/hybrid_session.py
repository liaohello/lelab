#!/usr/bin/env python3
"""OFFLINE ONLY - drive one production agent session from the upper-computer side.

Everything here goes through the interfaces the car would use in the field: the lease,
the epoch binding, the output permission, the recording session and the archive all come
from the agent's own HTTP routes, and the frames go out over the production
`FrameLink`/stream client.  Only the *devices* are simulated: the leader samples below
are synthetic, and the session is stamped `simulated_offline` end to end.

One session, two episodes, observe and grasp in each, with frames flowing the whole time
so the recorder captures real six-axis rows next to the phase boundaries it wrote itself.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid

REPO = pathlib.Path(__file__).resolve().parents[4]
for candidate in (REPO / "app_ros_mobile", REPO / "tools/ros2_collection/local_ros2/offline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from agent_session import call, write_marker          # noqa: E402
from lelab.hybrid_frame_link import CachedCarStatus, FrameLink  # noqa: E402
from lelab.teleop_stream import StreamError            # noqa: E402

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
# The same calibrated limits the loaded profile carries, so a sender-side clip matches the
# car's refusal instead of disagreeing with it.
LIMITS = {"joint1": (-2.052507, 2.086018), "joint2": (-2.052507, 1.931032),
          "joint3": (-2.094395, 2.052507), "joint4": (-1.675516, 2.010619),
          "joint5": (-1.759292, 1.717404)}


def leader_sample(tick: int) -> tuple[list, list]:
    """A simulated SO101 leader: six distinct axes, joint5 deliberately over its limit.

    The pre-clip value has to be visible in the recorded row, so one axis is driven past
    where the car may go.  A sender that only ever produced in-range numbers would make
    the clip untestable.
    """
    base = 0.05 * (tick % 20)
    leader = [base + 0.02 * index for index in range(5)]
    leader[4] += 1.0
    clipped = [min(max(value, LIMITS[name][0]), LIMITS[name][1])
               for name, value in zip(ARM_JOINTS, leader)]
    return leader, clipped


class Session:
    def __init__(self, args):
        self.args = args
        self.marker_path = pathlib.Path(args.marker)
        self.marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        self.checks: list[dict] = []
        self.events: list[dict] = []
        self.session_id = None
        self.clock_epoch = uuid.uuid4().hex
        self.control_epoch = None
        self.token = None

    def check(self, name, passed, detail=""):
        self.checks.append({"name": name, "passed": bool(passed), "detail": str(detail)[:400]})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}  {str(detail)[:200]}")

    def record(self, kind, **fields):
        self.events.append({"kind": kind, "at_mono": round(time.monotonic(), 3), **fields})

    # -- the lease and the epoch -------------------------------------------
    def arm(self):
        acquired = call(self.args.agent_url, "/control/acquire",
                        {"client_id": self.args.client_id, "ttl_s": 300.0})
        self.token = acquired["token"]
        self.control_epoch = acquired["session_epoch"]
        session = {"session_id": self.args.frame_session_id,
                   "session_epoch": self.control_epoch,
                   "calibration_bundle_hash": self.args.bundle}
        begun = call(self.args.agent_url, "/hybrid/begin_epoch",
                     {**session, "channels": ["arm", "gripper"]}, token=self.token)
        self.check("epoch_bound", begun["session_epoch"] == self.control_epoch, begun["session_id"])
        # A stop latched by an earlier run stays latched - that is the point of latching -
        # so re-engaging begins with the one thing production also requires: an explicit
        # clearance, which grants nothing, before the epoch binding and the enable below.
        control = {}
        try:
            control = json.loads(pathlib.Path(self.args.control_status).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            control = {}
        if control.get("latched_fault"):
            cleared = call(self.args.agent_url, "/control/clear_estop", {"token": self.token},
                           token=self.token)
            self.check("latched_stop_cleared_first", cleared.get("estop") is False,
                       {"was": control.get("latched_fault"),
                        "clear_sent": cleared.get("clear_estop_sent")})
            time.sleep(0.5)              # let the clearance reach the mirror
        ready = wait_for_feedback(self.args.agent_url)
        self.check("readback_complete", ready.get("available") is True, ready)
        enabled = call(self.args.agent_url, "/hybrid/enable_output", session, token=self.token)
        self.check("outputs_enabled_by_agent", enabled.get("outputs_enabled") is True,
                   f"emitter={enabled.get('emitter')}")
        self.check("frame_emitter_is_the_control_process",
                   enabled.get("emitter") == "car_control_process", enabled.get("emitter"))
        self.marker.update({"lease_token": self.token, "session_id": session["session_id"],
                            "session_epoch": self.control_epoch,
                            "calibration_bundle_hash": self.args.bundle,
                            "stream_port": self.args.stream_port,
                            "agent_url": self.args.agent_url,
                            "client_id": self.args.client_id,
                            "data_identity": "simulated_offline", "training_eligible": False})
        write_marker(self.marker_path, self.marker)

    def start_session(self):
        out = call(self.args.agent_url, "/recording/start_session", {
            "task": self.args.task,
            "session_epoch": self.control_epoch,
            "clock_epoch": self.clock_epoch,
            "calibration_bundle_hash": self.args.bundle,
            "export_schema": self.args.export_schema,
            "runtime_mode": self.args.runtime_mode,
            "profile": self.args.profile_name,
            "simulation": True,
        }, token=self.token)
        self.session_id = out["session_id"]
        self.check("session_started", bool(self.session_id), self.session_id)
        self.check("session_clock_declared", bool(out.get("clock_domain")),
                   out.get("clock_domain"))
        self.record("start_session", session_id=self.session_id,
                    clock_domain=out.get("clock_domain"))
        return out

    # -- one episode --------------------------------------------------------
    def episode(self, index):
        link = FrameLink(host="127.0.0.1", port=self.args.stream_port,
                         session_id=self.args.frame_session_id,
                         session_epoch=self.control_epoch, bundle_hash=self.args.bundle,
                         lease_token=self.token, ttl_ms=self.args.ttl_ms,
                         status_cache=CachedCarStatus(
                             probe=lambda: call(self.args.agent_url, "/status"),
                             period_s=0.2, max_age_s=1.0))
        link.attach_lease(self.token)
        started = {}
        try:
            link.engage()
            self.check(f"episode{index}_link_engaged", link.engaged, "hello accepted")
        except (StreamError, RuntimeError) as error:
            self.check(f"episode{index}_link_engaged", False, f"{type(error).__name__}: {error}")
            return started
        started_at = time.monotonic()
        episode = call(self.args.agent_url, "/recording/start_episode", {
            "source": "manual_hybrid", "task": self.args.task,
            "session_id": self.session_id, "session_epoch": self.control_epoch,
            "calibration_bundle_hash": self.args.bundle,
        }, token=self.token)
        episode_id = episode["episode_id"]
        started["episode_id"] = episode_id
        self.record("start_episode", episode_id=episode_id)
        for phase in ("observe", "grasp"):
            seconds = getattr(self.args, f"{phase}_s")
            phase_event = call(self.args.agent_url, "/recording/set_phase", {
                "phase": phase, "episode_id": episode_id, "clock_epoch": self.clock_epoch,
                "session_id": self.session_id, "session_epoch": self.control_epoch,
                "calibration_bundle_hash": self.args.bundle,
            }, token=self.token)
            self.check(f"episode{index}_phase_{phase}",
                       phase_event.get("payload", {}).get("phase") == phase,
                       phase_event.get("kind"))
            sent = self.send_frames(link, seconds)
            started.setdefault("frames", {})[phase] = sent
        # End of an episode: following stops and is confirmed stopped BEFORE the episode is
        # saved, so a saved episode can never contain targets that kept moving afterwards.
        link.disengage("episode_end")
        settled = wait_for_settled(self.args.control_status, timeout_s=2.0)
        self.check(f"episode{index}_following_paused",
                   settled.get("paused") is True or settled.get("outputs_enabled") is False,
                   {key: settled.get(key) for key in ("paused", "outputs_enabled")})
        emitted_at_pause = settled.get("counters", {}).get("frames_emitted")
        time.sleep(0.5)
        after = read_control(self.args.control_status)
        self.check(f"episode{index}_no_new_targets_after_pause",
                   after.get("counters", {}).get("frames_emitted") == emitted_at_pause,
                   f"{emitted_at_pause} -> {after.get('counters', {}).get('frames_emitted')}")
        ended = call(self.args.agent_url, "/recording/end_episode", {
            "discarded": False, "reason": "episode_complete",
            # No automatic success judgement offline: the chassis is locked and nobody
            # labelled the outcome, so the episode is reported as it really is.
            "note": "offline simulated session: success not assessed",
            "session_id": self.session_id, "session_epoch": self.control_epoch,
            "calibration_bundle_hash": self.args.bundle,
        }, token=self.token)
        started["ended"] = {"episode_id": ended.get("episode_id"),
                            "duration_s": round(time.monotonic() - started_at, 2),
                            "success": ended.get("evaluation", ended.get("success"))}
        self.record("end_episode", **started["ended"])
        self.check(f"episode{index}_ended", ended.get("episode_id") == episode_id, episode_id)
        link.status_cache.stop()
        return started

    def send_frames(self, link, seconds):
        """Paced sender for one phase: one snapshot per period, receipts collected."""
        period = 1.0 / self.args.hz
        deadline = time.monotonic() + seconds
        tick = 0
        counts = {"sent": 0, "skipped": 0}
        while time.monotonic() < deadline:
            target = deadline - (seconds - tick * period)
            now = time.monotonic()
            if now < target:
                time.sleep(max(0.0, target - now))
            tick += 1
            leader, clipped = leader_sample(tick)
            try:
                outcome = link.send_snapshot(arm_positions=clipped, gripper_rad=0.02 + 0.01 * (tick % 5),
                                             sample_seq=tick, leader_rad=leader,
                                             pre_clip_rad=leader)
            except RuntimeError as error:
                counts["skipped"] += 1
                self.record("send_error", detail=f"{type(error).__name__}: {error}")
                continue
            if outcome.get("sent"):
                counts["sent"] += 1
            else:
                counts["skipped"] += 1
            link.collect()
        link.collect()
        counts["admitted"] = link.counts["admitted"]
        counts["refused"] = link.counts["refused"]
        counts["refusals"] = dict(link.refusals)
        return counts

    def release(self):
        """Give the car back: revoke the output permission, then release the lease.

        Ending the recording is not the end of the authority.  A session that stops but
        keeps its lease would leave the next operator facing `control_held` and a live
        output permission nobody is holding a session for.
        """
        out = {}
        session = {"session_id": self.args.frame_session_id,
                   "session_epoch": self.control_epoch,
                   "calibration_bundle_hash": self.args.bundle}
        try:
            out["hybrid_stop"] = call(self.args.agent_url, "/hybrid/stop",
                                      {**session, "reason": "offline_session_finished"},
                                      token=self.token)
        except RuntimeError as error:
            out["hybrid_stop_error"] = str(error)[:160]
        try:
            out["released"] = call(self.args.agent_url, "/control/release",
                                   {"token": self.token}, token=self.token)
        except RuntimeError as error:
            out["release_error"] = str(error)[:160]
        self.marker["lease_token"] = ""
        write_marker(self.marker_path, self.marker)
        self.check("lease_released", out.get("released", {}).get("ok") is True,
                   out.get("release_error") or out.get("hybrid_stop_error") or "released")
        # The bridge revokes on its own heartbeat, so the mirror goes dark within about one
        # interval; the check waits for that instead of pretending the release is synchronous.
        deadline = time.monotonic() + 3.0
        after = {}
        while time.monotonic() < deadline:
            try:
                after = json.loads(pathlib.Path(self.args.control_status).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                after = {}
            licence_now = (after.get("licence") or {}).get("licence")
            if licence_now is None or licence_now.get("outputs_enabled") is False:
                break
            time.sleep(0.1)
        licence = (after.get("licence") or {}).get("licence")
        # No licence at all is the strongest form of "revoked", so both answers pass here;
        # what must not survive the release is a live permission.
        self.check("output_permission_revoked",
                   licence is None or licence.get("outputs_enabled") is False,
                   {"authorised": (after.get("licence") or {}).get("authorised"),
                    "outputs_enabled": None if licence is None else licence.get("outputs_enabled")})
        return out

    def stop(self):
        out = call(self.args.agent_url, "/recording/stop_session", {
            "reason": "offline_session_complete", "session_id": self.session_id,
            "session_epoch": self.control_epoch,
            "calibration_bundle_hash": self.args.bundle,
        }, token=self.token)
        self.check("session_stopped", out.get("stopped") is True or "session_id" in out,
                   json.dumps(out)[:160])
        return out

    def archive(self, destination: pathlib.Path):
        manifest = call(self.args.agent_url, f"/sessions/{self.session_id}/manifest")
        self.check("manifest_lists_two_episodes", len(manifest.get("episodes", {})) == self.args.episodes,
                   sorted(manifest.get("episodes", {})) if isinstance(manifest.get("episodes"), dict)
                   else manifest.get("episodes"))
        url = f"{self.args.agent_url}/sessions/{self.session_id}/archive"
        import urllib.request

        with urllib.request.urlopen(url, timeout=120) as response:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
        size = destination.stat().st_size
        self.check("archive_downloaded", size > 10_000, f"{size} bytes from the production archive route")
        return manifest, size


def read_control(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wait_for_settled(path, timeout_s):
    deadline = time.monotonic() + timeout_s
    document = read_control(path)
    while time.monotonic() < deadline:
        document = read_control(path)
        if document.get("paused") or document.get("outputs_enabled") is False:
            return document
        time.sleep(0.1)
    return document


def wait_for_feedback(agent_url, joints=6, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        status = call(agent_url, "/status")
        feedback = (status.get("control") or {}).get("follower_feedback") or {}
        last = {"available": feedback.get("available"),
                "samples": len(feedback.get("joint_samples") or {})}
        if feedback.get("available") and len(feedback.get("joint_samples") or {}) >= joints:
            return last
        time.sleep(0.2)
    return last


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default="http://127.0.0.1:18130")
    parser.add_argument("--stream-port", type=int, default=18122)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--control-status", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--client-id", default="offline-hybrid")
    parser.add_argument("--frame-session-id", dest="frame_session_id",
                        default="offline-hybrid-frames")
    parser.add_argument("--task", default="offline simulated observe and grasp")
    parser.add_argument("--profile-name", default="hardware_hybrid_manual_v1_offline6d")
    parser.add_argument("--export-schema", default="arm_gripper_6dof_phase1")
    parser.add_argument("--runtime-mode", default="simulated_hybrid_manual")
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--observe-s", type=float, default=3.0)
    parser.add_argument("--grasp-s", type=float, default=3.0)
    parser.add_argument("--hz", type=float, default=10.0)
    parser.add_argument("--ttl-ms", type=int, default=150)
    parser.add_argument("--archive-to", default="")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    session = Session(args)
    episode_reports = []
    try:
        session.arm()
        session.start_session()
        for index in range(1, args.episodes + 1):
            episode_reports.append(session.episode(index))
        session.stop()
        session.release()
        manifest = {}
        if args.archive_to:
            manifest, archive_bytes = session.archive(pathlib.Path(args.archive_to))
    except Exception as error:  # noqa: BLE001 - the report is the deliverable, even on failure
        session.check("session_completed", False, f"{type(error).__name__}: {error}")
        manifest = manifest if "manifest" in dir() else {}
        archive_bytes = 0
        sys.stderr.write(f"session failed: {type(error).__name__}: {error}\n")
    else:
        session.check("session_completed", True, "no exception on the production path")

    document = {
        "data_identity": "simulated_offline",
        "training_eligible": False,
        "hardware_effect": "not_tested_no_serial_port_reachable",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recording_session_id": session.session_id,
        "control_session_id": args.frame_session_id,
        "control_session_epoch": session.control_epoch,
        "clock_epoch": session.clock_epoch,
        "episodes": episode_reports,
        "checks": session.checks,
        "failed": [check["name"] for check in session.checks if not check["passed"]],
        "manifest": manifest,
        "events": session.events,
    }
    if args.archive_to:
        document["archive_bytes"] = archive_bytes
    report = pathlib.Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
                      encoding="utf-8")
    print(f"report: {report}  failed: {document['failed'] or 'none'}")
    return 1 if document["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
