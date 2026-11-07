"""TeleopSessionCoordinator: one session that owns the leader serial lease, the car control
lease, the anti-jump state machine, the mapper and the recording subtask.

The browser only drives these methods; the real-time loop and every safety decision live here.
Preview mode never touches an actuator: it computes and reports the mapped demand only.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .hardware_lease import HardwareLeaseError, get_hardware_lease_manager
from .mobile_leader_reader import (LeaderConfig, SO101LeaderReader, build_bus,
                                   calibration_id_mismatches)
from .mobile_teleop import CalibrationStore, TeleopMapper, default_store_root
from .mobile_teleop_session import Gate, TeleopStateMachine

UPPER_LEASE_TASK = "teleoperation"
LEADER_SERIAL_TASK = "teleoperation"


class BridgeError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class CoordinatorConfig:
    agent_url: str = "http://127.0.0.1:8110"
    control_ttl_s: float = 30.0
    lease_ttl_s: float = 60.0
    command_hz: float = 20.0
    request_timeout_s: float = 3.0
    leader: dict[str, Any] = field(default_factory=dict)


class MobileTeleopCoordinator:
    def __init__(self, config: CoordinatorConfig, profile: dict[str, Any],
                 store: CalibrationStore | None = None,
                 leader_reader: SO101LeaderReader | None = None,
                 transport: Callable[[str, str, dict, str | None], dict] | None = None,
                 lease_manager: Any | None = None):
        self.config = config
        self.profile = profile
        self.limits = profile.get("limits", {}).get("joint_positions_rad", {})
        self.store = store or CalibrationStore(default_store_root())
        self.leader = leader_reader
        self._leader_open_binding: tuple[str, str] | None = None
        self.mapper_cfg = profile.get("teleop", {}).get("mapping", {})
        self.mapper = TeleopMapper(self.mapper_cfg, self.limits) if self.mapper_cfg else None
        self.machine = TeleopStateMachine(on_clear_targets=self._on_clear_targets)
        self._transport = transport or self._http
        self.lock = threading.RLock()
        self.leases = lease_manager or get_hardware_lease_manager()
        self.token: str | None = None
        self.upper_token: str | None = None
        self.calibration_version: str | None = None
        self.seq = -1
        self.session_owner: str | None = None
        self.follow_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop_generation = 0
        self.last_error: str | None = None
        self.recording_session: str | None = None
        self.recording_episode: str | None = None
        self.preview: dict[str, Any] = {}
        self.identities: dict[str, str] = {}
        self.last_gate: dict[str, Any] | None = None
        self.last_gate_blockers: list[str] = []
        self.last_gate_at: float | None = None
        self.last_car_exclusivity: dict[str, Any] | None = None
        self.last_car_hold_policy: str | None = None

    # ------------------------------------------------------------------ lease rollback hook
    def _on_clear_targets(self, reason: str) -> None:
        """Any state that invalidates the reference must also drop the filter and target memory."""
        if self.mapper is not None:
            self.mapper.reset(reason)
        self.last_sent = None
        self.last_error = reason

    # ------------------------------------------------------------------ car gateway client
    def _http(self, path: str, method: str = "GET", payload: dict | None = None,
              token: str | None = None) -> dict:
        data = json.dumps(payload or {}).encode()
        request = urllib.request.Request(self.config.agent_url + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("X-LeLab-Control-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_s) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(detail)
                detail = f"{parsed.get('error')}: {parsed.get('detail')}"
            except json.JSONDecodeError:
                pass
            raise BridgeError(exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BridgeError(502, f"car gateway unreachable: {exc}") from exc
        return json.loads(body or b"{}")

    # ------------------------------------------------------------------ leases
    def acquire(self, owner: str, leader_identity: str, follower_identity: str,
                mapping_identity: str) -> dict[str, Any]:
        """Take the upper serial lease and the car control lease, rolling back on partial failure."""
        if owner is None or not owner.strip():
            raise BridgeError(400, "owner is required")
        with self.lock:
            if self.token:
                raise BridgeError(409, "a teleop session is already active")
            self.identities = {"leader": leader_identity, "follower": follower_identity,
                               "mapping": mapping_identity}
            readiness = self.store.readiness(leader_identity, follower_identity, mapping_identity)
            if not readiness["ready"]:
                raise BridgeError(409, "calibration not ready: " + "; ".join(readiness["reasons"]))
            # Without a live leader reading the session could only ever sit in `disconnected`
            # while still holding both leases, so require it before anything is acquired.
            if self.leader is None:
                raise BridgeError(409, "leader reader is not configured; open the leader serial first")
            if isinstance(self.leader, SO101LeaderReader):
                leader_record = self.store.load("leader", leader_identity)
                if self._leader_open_binding != (leader_identity, leader_record.version):
                    raise BridgeError(409, "leader calibration or identity changed; close and reopen the leader")
            leader_state = self.leader.snapshot()
            if not leader_state.get("connected") or not leader_state.get("healthy"):
                raise BridgeError(409, "leader not ready: " + str(leader_state.get("reason") or "leader_unreachable"))
            # The stored mapping layer is the only mapping truth: rebuilding the mapper here
            # keeps a stale profile copy from silently steering the arm.
            mapping_doc = self.store.load("mapping", mapping_identity).data
            follower_doc = self.store.load("follower", follower_identity).data
            limits = follower_doc.get("limits_rad") or self.limits
            # The follower layer owns the safe envelope; the mapping may only narrow it, and
            # TeleopMapper intersects both against the hardware limits.
            envelope = follower_doc.get("soft_limits_rad") or {}
            joints = []
            for cfg in mapping_doc.get("joints", []):
                entry = dict(cfg)
                soft = envelope.get(entry.get("follower"))
                if soft:
                    entry.setdefault("soft_min_rad", soft[0])
                    entry.setdefault("soft_max_rad", soft[1])
                joints.append(entry)
            mapping_doc = {**mapping_doc, "joints": joints}
            unbounded = [j.get("follower") for j in joints if j.get("follower") not in limits]
            if unbounded:
                raise BridgeError(409, f"follower calibration has no limits_rad for {unbounded}")
            if not mapping_doc.get("gripper"):
                raise BridgeError(409, "mapping calibration has no gripper section")
            self.mapper_cfg, self.limits = mapping_doc, limits
            self.mapper = TeleopMapper(mapping_doc, limits)
            self.mapper.reset("mapping reloaded from calibration store")
        lease = None
        try:
            lease = self.leases.acquire(
                UPPER_LEASE_TASK, owner=owner, role="operator", ttl_seconds=self.config.lease_ttl_s)
        except HardwareLeaseError as exc:
            raise BridgeError(exc.status_code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(500, f"upper lease failed: {exc}") from exc
        acquired = None
        try:
            status = self._transport("/status")
            if status.get("read_only"):
                raise BridgeError(409, "car agent is in readonly mode; deploy the teleop profile")
            if not (status.get("control") or {}).get("leader_arm_available",
                                                     status.get("leader_arm_available", False)):
                raise BridgeError(409, "car agent does not allow leader_arm mode")
            acquired = self._transport("/control/acquire", "POST",
                                  {"client_id": owner, "ttl_s": self.config.control_ttl_s})
            self._transport("/control/mode", "POST", {"mode": "leader_arm"}, acquired["token"])
        except BridgeError as exc:
            rollback_error = None
            if acquired and acquired.get("token"):
                try:
                    self._transport("/control/release", "POST", {}, acquired["token"])
                except BridgeError as release_exc:
                    rollback_error = release_exc.detail
            self.leases.release(token=lease.token, reason="car-acquire-failed")
            if rollback_error:
                raise BridgeError(exc.status, f"{exc.detail}; car lease release failed: {rollback_error}; "
                                             "wait for car lease expiry") from exc
            raise exc
        with self.lock:
            self.token = acquired["token"]
            self.upper_token = lease.token
            self.session_owner = owner
            self.seq = -1
            self.calibration_version = readiness["calibration_version"]
            self.machine.event("leader_found", self._gate())
            self.machine.event("calibration_ready", self._gate())
        return {"owner": owner, "car_token": self.token, "state": self.machine.state,
                "calibration_version": readiness["calibration_version"],
                "lease_expires_s": self.config.lease_ttl_s}

    def release(self, reason: str = "released") -> dict[str, Any]:
        self.stop_follow_loop()
        with self.lock:
            token, upper = self.token, self.upper_token
            self.token = None
            self.upper_token = None
            self.session_owner = None
        out: dict[str, Any] = {"released": True}
        if token:
            try:
                self._transport("/control/release", "POST", {}, token)
            except BridgeError as exc:
                out["car_release_error"] = exc.detail
        if upper:
            try:
                self.leases.release(token=upper, reason=reason)
            except Exception as exc:  # noqa: BLE001
                out["upper_release_error"] = str(exc)
        with self.lock:
            self.machine.event("leader_lost", Gate())
        return out

    def is_live(self) -> bool:
        """True while this process holds the session or is still streaming to the car.

        The lease watchdog asks this: the desktop SO-101 teleoperation flag says nothing about a
        mobile session, and reporting it as finished would revoke a live teleop lease.
        """
        with self.lock:
            return bool(self.upper_token) or self.follow_thread is not None

    def shutdown(self, reason: str = "server-exit") -> dict[str, Any]:
        """Upper-side exit: stop commanding, close the bag, drop both leases, release the port.

        The tunnel may already be dead, so the gateway calls are best effort — what actually
        protects the arm is the car's own arm-stream TTL and control-lease expiry.
        """
        out: dict[str, Any] = {}
        self.stop_follow_loop()
        with self.lock:
            episode, session = self.recording_episode, self.recording_session
        try:
            if episode:
                self.recording("end_episode", {})
            if session:
                self.recording("stop_session", {})
        except BridgeError as exc:
            out["recording_close_error"] = exc.detail
        try:
            out.update(self.release(reason=reason))
        finally:
            # The leader port comes back even when the car is unreachable. 上位机退出 must not
            # leave the arm claimed under flock + TIOCEXCL, or the next tool cannot open it without
            # restarting LeLab - the rehearsal found this by reading status after shutdown.
            out["leader"] = self.close_leader().get("leader")
        return out

    def heartbeat(self) -> dict[str, Any]:
        with self.lock:
            token, upper = self.token, self.upper_token
        if not token or not upper:
            raise BridgeError(409, "teleop session is not acquired")
        out = self._transport("/control/renew", "POST", {}, token)
        try:
            self.leases.renew(token=upper, ttl_seconds=self.config.lease_ttl_s)
        except Exception as exc:  # noqa: BLE001
            self.demote_to_paused(f"upper_lease_lost:{exc}")
            raise BridgeError(409, f"upper lease renew failed: {exc}") from exc
        return out

    # ------------------------------------------------------------------ leader device
    def open_leader(self, port: str, ids: dict[str, int] | None = None,
                    leader_identity: str | None = None) -> dict[str, Any]:
        with self.lock:
            # The wizard opens the serial first so the operator can watch live readings before
            # taking leases; acquiring then reuses that reader instead of forcing a close/re-open.
            reusing = self.leader is not None
            if reusing and str(self.leader.config.port) != str(port):
                raise BridgeError(409, f"leader reader already open on {self.leader.config.port}; "
                                       "release the serial before switching port")
            if reusing and self._leader_open_binding and leader_identity != self._leader_open_binding[0]:
                raise BridgeError(409, "leader identity changed; close and reopen the leader")
            if leader_identity:
                if not leader_identity.strip():
                    raise BridgeError(409, "leader identity is required to open the leader serial")
                self.identities = {**self.identities, "leader": leader_identity}
            if not reusing:
                cfg = LeaderConfig(port=port, ids=ids or LeaderConfig(port=port).ids,
                                   read_hz=float(self.config.leader.get("read_hz", 30.0)),
                                   stale_after_s=float(self.config.leader.get("stale_after_s", 0.5)))
                calibration = self._leader_calibration()
                # The calibration is used to normalise readings, so a mismatched one is worse than
                # none: it produces smooth, plausible angles from the wrong arm's offsets.
                mismatch = calibration_id_mismatches(calibration, cfg.ids)
                if mismatch:
                    raise BridgeError(409, "leader_calibration_does_not_match_this_arm: "
                                            + "; ".join(mismatch))
                record = self.store.load("leader", self.identities["leader"])
                device = record.data.get("device_identity") or {}
                expected_port, serial = device.get("usb_by_id"), device.get("serial")
                name = os.path.basename(port)
                port_serial = name.split("_")[-1].split("-")[0]
                if (not isinstance(expected_port, str) or not isinstance(serial, str) or not serial
                        or os.path.dirname(port) != "/dev/serial/by-id" or expected_port != port
                        or "USB_Single_Serial" not in name or port_serial != serial):
                    raise BridgeError(409, "leader_device_identity_mismatch: select the by-id port "
                                           "and bind the same USB serial in layer A")
                reader = SO101LeaderReader(cfg, bus_factory=lambda c: build_bus(c, calibration))
                try:
                    reader.open()
                except (RuntimeError, ValueError) as exc:
                    # A missing/partial leader calibration is a configuration refusal, not a crash:
                    # the reader must never fall back to uncalibrated ticks.
                    raise BridgeError(409, str(exc)) from exc
                reader.start()
                self.leader = reader
                self._leader_open_binding = (self.identities["leader"], record.version)
        return self.status()

    def close_leader(self) -> dict[str, Any]:
        with self.lock:
            reader, self.leader = self.leader, None
            self._leader_open_binding = None
        if reader:
            reader.close()
        with self.lock:
            self.machine.event("leader_lost", self._gate())
        return self.status()

    def _leader_calibration(self) -> dict[str, Any] | None:
        try:
            record = self.store.load("leader", self.identities.get("leader", ""))
        except (FileNotFoundError, ValueError):
            return None
        # Layer A references the calibration the existing SO-101 wizard already produced; the
        # bus needs those exact fields to report degrees and opening percent.
        return record.data.get("lerobot_calibration")

    def _status_snapshot(self) -> tuple[dict[str, Any], str | None]:
        """One gateway read; the follow loop reuses it for the gate and the preview."""
        try:
            return self._transport("/status"), None
        except BridgeError as exc:
            return {}, exc.detail

    # ------------------------------------------------------------------ gates
    def _gate(self, status: dict[str, Any] | None = None) -> Gate:
        """Snapshot both sides; a dropped tunnel or agent is reported as gates failing."""
        leader_snap = self.leader.snapshot() if self.leader else {}
        if status is None:
            try:
                status = self._transport("/status")
            except BridgeError:
                status = {}
        control = status.get("control") or {}
        # The deployed agent reports feedback and teleop settings inside the `control` block; the
        # top level is kept as a fallback so either shape is accepted. Reading only the top level
        # would leave every gate permanently closed on the real car.
        follower = status.get("follower_feedback") or control.get("follower_feedback") or {}
        teleop = status.get("teleop") or control.get("teleop") or {}
        lease_ok = False
        with self.lock:
            upper = self.upper_token
            token = self.token
            owner = self.session_owner
        if upper:
            try:
                state = self.leases.status()
                lease_ok = (state.get("state") == "held"
                            and state.get("task") == UPPER_LEASE_TASK
                            and state.get("owner") == owner
                            and float(state.get("expires_in") or 0.0) > 0.0)
            except Exception:  # noqa: BLE001
                lease_ok = False
        ready = False
        if self.identities:
            try:
                ready = self.store.readiness(self.identities.get("leader", ""),
                                             self.identities.get("follower", ""),
                                             self.identities.get("mapping", ""))["ready"]
            except Exception:  # noqa: BLE001
                ready = False
        gate = Gate(
            leader_connected=bool(leader_snap.get("connected")),
            leader_healthy=bool(leader_snap.get("healthy")),
            calibration_ready=ready,
            calibration_version=self.calibration_version,
            upper_lease_valid=lease_ok,
            car_lease_valid=bool(token) and bool(control.get("active"))
            and control.get("mode") == "leader_arm",
            follower_feedback_valid=bool(follower.get("available")),
            exclusive_control=bool(token) and control.get("client_id") == owner,
            chassis_locked=bool(teleop.get("chassis_locked", True)),
        )
        with self.lock:
            # Cached so /teleop/status can show what the car last reported without making its own
            # gateway call: the status poll must stay fast and local when the tunnel is stalled.
            self.last_gate = vars(gate)
            self.last_gate_blockers = gate.missing("following")
            self.last_gate_at = time.monotonic()
            # What the car itself concluded about exclusive access, reported on its own /status.
            self.last_car_exclusivity = teleop.get("exclusivity")
            self.last_car_hold_policy = teleop.get("hold_policy")
        return gate

    def readiness_report(self) -> dict[str, Any]:
        gate = self._gate()
        return {"state": self.machine.state, "missing_for_following": gate.missing("following"),
                "leader": self.leader.snapshot() if self.leader else {"connected": False},
                "gate": vars(gate)}

    # ------------------------------------------------------------------ preview (no actuator output)
    def preview_step(self, status: dict[str, Any] | None = None,
                     status_error: str | None = None) -> dict[str, Any]:
        """Compute the mapped demand and error against real feedback without sending anything."""
        if status is None:
            status, status_error = self._status_snapshot()
        if status_error:
            self.preview = {"error": status_error, "outputs_actuators": False}
            return self.preview
        gate = self._gate(status)
        leader = self.leader.snapshot() if self.leader else {}
        feedback = (status.get("follower_feedback") or (status.get("control") or {}).get("follower_feedback") or {})
        mapping = self.mapper_cfg
        out: dict[str, Any] = {"outputs_actuators": False, "state": self.machine.state,
                              "gate_missing": gate.missing("following")}
        if not leader.get("positions_rad") or not mapping:
            out["error"] = leader.get("reason") or "leader_unreachable"
            self.preview = out
            return out
        if self.mapper is None:
            out["error"] = "mapping_not_loaded"
            self.preview = out
            return out
        ref = self.machine.alignment or {}
        leader_ref = ref.get("leader_rad") or {}
        follower_ref = ref.get("follower_rad") or {}
        positions = dict(leader["positions_rad"])          # keyed by the leader joint names
        targets = {}
        clipped_joints: list[str] = []
        errors = {}
        missing_targets: list[str] = []
        leader_feedback = feedback.get("positions_rad") or {}
        for entry in mapping.get("joints", []):
            leader_joint = entry["leader"]
            follower = entry["follower"]
            if leader_joint not in positions:
                missing_targets.append(f"{follower}:{leader_joint}_unread")
                continue
            target = self.mapper.joint_target(
                follower, positions[leader_joint], leader_ref.get(leader_joint, positions[leader_joint]),
                follower_ref.get(follower, leader_feedback.get(follower, 0.0)), time.monotonic())
            targets[follower] = target
            if target["clipped"]:
                clipped_joints.append(follower)
            measured = leader_feedback.get(follower)
            if measured is not None:
                errors[follower] = round(target["target_rad"] - measured, 6)
        gripper = None
        if leader.get("gripper_percent") is not None:
            gripper = self.mapper.gripper_target(leader["gripper_percent"],
                                                 (ref.get("gripper") or {}).get("leader_percent",
                                                                               leader["gripper_percent"]),
                                                 (ref.get("gripper") or {}).get("pulse", 500.0))
        out.update({
            "leader_rad": positions,
            "leader_deg": leader.get("positions_deg"),
            "unmapped_joints": missing_targets,
            "gripper_percent": leader.get("gripper_percent"),
            "targets": targets,
            "gripper": gripper,
            "clipped_joints": clipped_joints,
            "follow_error_rad": errors,
            "feedback": {"available": feedback.get("available"), "reason": feedback.get("reason"),
                         "positions_rad": feedback.get("positions_rad"), "age_s": feedback.get("age_s"),
                         "missing_joints": feedback.get("missing_joints"), "source": feedback.get("source")},
            "sent_target_rad": (status.get("control") or {}).get("last_arm_targets"),
            # What the car itself concluded about our command stream (monotonic, car-side clock).
            "arm_stream": (status.get("teleop")
                           or (status.get("control") or {}).get("teleop") or {}).get("stream"),
            "calibration_version": getattr(self, "calibration_version", None),
            "aligned": self.machine.aligned,
        })
        with self.lock:
            self.preview = out
        return out

    # ------------------------------------------------------------------ alignment / control
    def align(self) -> dict[str, Any]:
        preview = self.preview_step()
        leader = preview.get("leader_rad") or {}
        feedback = (preview.get("feedback") or {}).get("positions_rad") or {}
        if not leader or not feedback:
            raise BridgeError(409, f"align needs leader and follower readings ({preview.get('error')})")
        reference = {"leader_rad": leader, "follower_rad": feedback,
                     "limits_rad": {k: list(v) for k, v in self.limits.items()},
                     "gripper": {"leader_percent": (preview.get("gripper") or {}).get("leader_percent", 0.0),
                                 "pulse": (preview.get("gripper") or {}).get("target_pulse", 500.0)}}
        with self.lock:
            snap = self.machine.event("align", self._gate(), {"reference": reference})
        if snap["deny_reasons"]:
            raise BridgeError(409, "align refused: " + ",".join(snap["deny_reasons"]))
        self.mapper.reset("aligned")
        return snap

    def set_state(self, event: str) -> dict[str, Any]:
        stopping = event in ("release", "pause", "estop", "fault", "leader_lost")
        if stopping:
            # Stopping must not wait for a remote status request or its timeout.
            self.stop_follow_loop()
        with self.lock:
            snap = self.machine.event(event, Gate() if stopping else self._gate())
        if snap["deny_reasons"] and event in ("enter_preview", "arm", "engage"):
            raise BridgeError(409, f"{event} refused: " + ",".join(snap["deny_reasons"]))
        if event == "engage":
            self.start_follow_loop()
        return snap

    def emergency_stop(self, reason: str = "operator-estop") -> dict[str, Any]:
        with self.lock:
            token = self.token
        self.stop_follow_loop()
        with self.lock:
            snap = self.machine.event("estop", Gate())
        out = {"state": snap["state"], "sent_estop": False}
        if token:
            try:
                self._transport("/control/estop", "POST", {"reason": reason}, token)
                out["sent_estop"] = True
            except BridgeError as exc:
                out["estop_error"] = exc.detail
        return out

    # ------------------------------------------------------------------ follow loop
    def start_follow_loop(self) -> None:
        with self.lock:
            if self.follow_thread is not None:
                return
            self._stop.clear()
            self._loop_generation += 1
            generation = self._loop_generation
            self.follow_thread = threading.Thread(target=self._follow_loop, args=(generation,),
                                                  daemon=True, name="mobile-teleop-follow")
            self.follow_thread.start()

    def stop_follow_loop(self) -> None:
        with self.lock:
            thread, self.follow_thread = self.follow_thread, None
        self._stop.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _follow_loop(self, generation: int) -> None:
        period = 1.0 / max(1.0, self.config.command_hz)
        while not self._stop.is_set():
            started = time.monotonic()
            with self.lock:
                # A loop that parked itself exits without joining its own thread, so it can still
                # be sleeping when the operator re-engages and a new loop starts. Only the newest
                # generation may send; two threads driving the arm at once is not acceptable.
                stale = generation != self._loop_generation
            if stale:
                return
            try:
                self.send_once()
            except BridgeError as exc:
                self.demote_to_paused(f"gateway_error:{exc.detail}")
            except Exception as exc:  # noqa: BLE001
                self.demote_to_paused(f"follow_error:{type(exc).__name__}:{exc}")
            self._stop.wait(max(0.0, period - (time.monotonic() - started)))

    def send_once(self) -> dict[str, Any] | None:
        # One car /status read per control cycle: at command_hz this is the difference between a
        # healthy tunnel and one that times out mid-session.
        status, status_error = self._status_snapshot()
        gate = self._gate(status)
        ok, reasons = self.machine.guard_following(gate)
        if not ok:
            if reasons and "state_not_following" not in reasons:
                # guard_following already parked the state machine and recorded the lost gates as
                # the reason (via _on_clear_targets); what was missing is the stream itself. A
                # parked session must stop polling the car, and resuming has to be an explicit
                # re-align + engage rather than this thread picking back up on its own.
                self.stop_follow_loop()
            return None
        preview = self.preview_step(status, status_error)
        if preview.get("error"):
            self.demote_to_paused(f"preview_error:{preview['error']}")
            return None
        targets = {j: t["target_rad"] for j, t in (preview.get("targets") or {}).items()}
        if not targets:
            return None
        arm_order = self.mapper_cfg.get("joint_order", list(targets))
        positions = [targets[j] for j in arm_order if j in targets]
        if len(positions) != 5:
            self.demote_to_paused("arm_target_incomplete")
            return None
        with self.lock:
            self.seq += 1
            seq = self.seq
            token = self.token
            version = getattr(self, "calibration_version", None)
        payload = {
            "session_id": token, "seq": seq, "source": "leader_arm", "mode": "leader_arm",
            "ttl_s": 2.0 / max(1.0, self.config.command_hz), "calibration_version": version,
            "clutch": "engaged", "leader_rad": preview.get("leader_rad"),
            "pre_clip_rad": [preview["targets"][j]["pre_clip_rad"] for j in arm_order if j in targets],
            "arm": {"positions": positions, "duration_s": round(1.0 / self.config.command_hz, 3)},
        }
        try:
            sent = self._transport("/control/arm", "POST", payload, token)
        except BridgeError as exc:
            self.demote_to_paused(f"arm_send_failed:{exc.detail}")
            return None
        gripper = preview.get("gripper") or {}
        if gripper.get("target_rad") is not None:
            self.seq += 1
            try:
                self._transport("/control/gripper", "POST", {
                "session_id": token, "seq": self.seq, "source": "leader_arm", "mode": "leader_arm",
                "ttl_s": payload["ttl_s"], "calibration_version": version, "clutch": "engaged",
                "gripper_percent": gripper.get("leader_percent"),
                "pre_clip_rad": [gripper.get("target_rad")],
                    "gripper": {"position": gripper["target_rad"],
                               "duration_s": round(1.0 / self.config.command_hz, 3)},
                }, token)
            except BridgeError as exc:
                self.demote_to_paused(f"gripper_send_failed:{exc.detail}")
                return None
        self.last_sent = {"seq": seq, "positions": positions, "wall": time.monotonic(),
                          "sent": sent.get("applied"), "clipped": sent.get("clipped")}
        return self.last_sent

    def demote_to_paused(self, reason: str) -> None:
        """Any lost precondition stops the stream and forces a fresh alignment next time."""
        self.stop_follow_loop()
        with self.lock:
            self.last_error = reason
            self.machine.event("invalidate", self._gate(), {"reason": reason})

    # ------------------------------------------------------------------ recording subtask
    def recording(self, action: str, payload: dict | None = None) -> dict[str, Any]:
        """Recording runs inside this teleop session and never takes a second lease."""
        with self.lock:
            token = self.token
        if not token:
            raise BridgeError(409, "teleop session is not acquired; recording must join it")
        routes = {
            "start_session": ("/recording/start_session", "POST"),
            "start_episode": ("/recording/start_episode", "POST"),
            "end_episode": ("/recording/end_episode", "POST"),
            "stop_session": ("/recording/stop_session", "POST"),
            "sessions": ("/sessions", "GET"),
        }
        route = routes.get(action)
        if route is None:
            raise BridgeError(400, f"unknown recording action {action!r}")
        if action == "end_episode" and self.machine.state == "following":
            # Stop before potentially slow or failed recorder I/O, even for API clients that did
            # not send a separate pause request. The next episode requires a fresh alignment.
            self.set_state("pause")
        out = self._transport(route[0], route[1], payload or {}, token if route[1] == "POST" else None)
        if action == "start_session":
            self.recording_session = out.get("session_id")
        elif action == "start_episode":
            self.recording_episode = out.get("episode_id")
        elif action == "end_episode":
            self.recording_episode = None
        elif action == "stop_session":
            self.recording_session = out.get("session_id")
        return out

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        with self.lock:
            gate, gate_at = self.last_gate, self.last_gate_at
            return {
                "state": self.machine.state,
                "snapshot": self.machine.snapshot(),
                "owner": self.session_owner,
                "car_token_active": bool(self.token),
                "upper_lease_active": bool(self.upper_token),
                "seq": self.seq,
                "identities": dict(self.identities),
                "calibration_version": getattr(self, "calibration_version", None),
                "leader": self.leader.snapshot() if self.leader else {"connected": False},
                "preview": self.preview,
                "last_sent": getattr(self, "last_sent", None),
                "last_error": self.last_error,
                "recording_session": self.recording_session,
                "recording_episode": self.recording_episode,
                # Reported from the last car status observation, not from the local profile: the
                # page must not present a config default as the car's actual chassis state.
                "gate": gate,
                "gate_age_s": None if gate_at is None else round(time.monotonic() - gate_at, 2),
                "missing_for_following": list(self.last_gate_blockers),
                "car_exclusivity": self.last_car_exclusivity,
                "car_hold_policy": self.last_car_hold_policy,
                "chassis_locked": None if gate is None else gate.get("chassis_locked"),
                "chassis_locked_profile": bool(self.profile.get("teleop", {}).get("chassis_locked", True)),
                "units": {"leader": "deg on the wire, rad after Feetech conversion",
                          "follower": "rad from /controller_manager/joint_states",
                          "gripper_leader": "0-100 percent", "gripper_follower": "rad (from pulses)"},
            }


_default = None


def peek_mobile_teleop_coordinator() -> MobileTeleopCoordinator | None:
    """The singleton only when a page already built one; shutdown must not create one."""
    return _default


def get_mobile_teleop_coordinator() -> MobileTeleopCoordinator:
    global _default
    if _default is None:
        import os
        from pathlib import Path
        profile_path = Path(os.environ.get("LELAB_MOBILE_TELEOP_PROFILE", ""))
        profile = json.loads(profile_path.read_text()) if profile_path.is_file() else {}
        _default = MobileTeleopCoordinator(
            CoordinatorConfig(agent_url=os.environ.get("LELAB_ROS_AGENT_URL", "http://127.0.0.1:8110")),
            profile)
    return _default


# ---------------------------------------------------------------------------
# T3 Wave A: hybrid session facade entry point for the /ros-mobile/hybrid/* routes.
# The default is deliberately adapter-free and fail-closed: no serial port, camera,
# gamepad, service or actuator is touched by importing or polling this factory.
# ---------------------------------------------------------------------------
_hybrid_facade = None
_hybrid_facade_lock = threading.Lock()


def get_hybrid_session_facade():
    """Return the process-wide hybrid facade (T4 route glue calls this name).

    With no adapters installed, status()/capabilities()/preview() are safe to read and
    acquire_session() refuses with a readable reason. Real adapters are injected only via
    configure_hybrid_facade() during an authorised integration wave.
    """
    global _hybrid_facade
    with _hybrid_facade_lock:
        if _hybrid_facade is None:
            from .hybrid_facade import build_public_facade
            _hybrid_facade = build_public_facade()
        return _hybrid_facade


def configure_hybrid_facade(*, upper=None, car=None, observer=None, driver=None,
                            calibration_store=None, clock_ns=None,
                            gamepad_deadzone=None, gamepad_timeout_ns=None,
                            replace: bool = True, facade=None):
    """Inject trusted server-side adapters. Never installs fixtures or fake feedback.

    Gamepad parameters come only from a verified `gamepad` calibration asset; leaving
    them unset keeps the gamepad input path explicitly unconfigured (fail closed).
    """
    global _hybrid_facade
    with _hybrid_facade_lock:
        if facade is not None:
            if not replace and _hybrid_facade is not None:
                return _hybrid_facade
            _hybrid_facade = facade
            return _hybrid_facade
        if replace or _hybrid_facade is None:
            from .hybrid_facade import build_public_facade
            kwargs = {"upper": upper, "car": car, "observer": observer,
                      "calibration_store": calibration_store}
            if clock_ns is not None:
                kwargs["clock_ns"] = clock_ns
            if gamepad_deadzone is not None:
                kwargs["gamepad_deadzone"] = gamepad_deadzone
                kwargs["gamepad_timeout_ns"] = gamepad_timeout_ns
            _hybrid_facade = build_public_facade(**kwargs)
        if driver is not None:
            _hybrid_facade.bind_driver(driver)
        return _hybrid_facade


class _HybridUpperLeaseAdapter:
    """Upper layer of the SAME HardwareLeaseManager used by legacy teleoperation."""

    def __init__(self, coordinator: MobileTeleopCoordinator):
        self.coordinator = coordinator
        self._token: str | None = None

    def acquire(self, request: dict) -> str:
        lease = self.coordinator.leases.acquire(
            UPPER_LEASE_TASK, owner=str(request.get("owner") or "hybrid-operator"),
            role="operator", ttl_seconds=self.coordinator.config.lease_ttl_s)
        self._token = lease.token
        return lease.token

    def valid(self, token: str) -> bool:
        status = self.coordinator.leases.status()
        return (token == self._token and status.get("state") == "held"
                and status.get("task") == UPPER_LEASE_TASK)

    def renew(self, token: str) -> None:
        if not self.coordinator.leases.renew(token=token):
            raise RuntimeError("upper_lease_renew_failed")

    def release(self, token: str, reason: str) -> None:
        self.coordinator.leases.release(token=token, reason=str(reason))
        self._token = None


class _HybridCarLeaseAdapter:
    """Car-side control lease via the coordinator's existing transport."""

    def __init__(self, coordinator: MobileTeleopCoordinator):
        self.coordinator = coordinator
        self._token: str | None = None

    def acquire(self, request: dict) -> str:
        result = self.coordinator._transport(
            "/control/acquire", "POST",
            {"client_id": str(request.get("owner") or "hybrid-operator"),
             "ttl_s": self.coordinator.config.control_ttl_s})
        token = result.get("token") if isinstance(result, dict) else None
        if not token:
            raise BridgeError(502, "car control lease not granted")
        self._token = token
        return token

    def valid(self, token: str) -> bool:
        return bool(token) and token == self._token

    def release(self, token: str, reason: str) -> None:
        self.coordinator._transport("/control/release", "POST", {}, token)
        self._token = None

    def stop(self, reason: str) -> None:
        # Stop precedes any network/recorder wrap-up; Wave A never binds this adapter.
        if self._token is not None:
            self.coordinator._transport("/control/mode", "POST", {"mode": "idle"}, self._token)


def hybrid_lease_adapters(coordinator: MobileTeleopCoordinator | None = None) -> dict:
    """Both lease layers for the hybrid facade, sharing the existing lease owner.

    Returned adapters are inert until acquire() is explicitly called by an authorised
    integration wave; this function only constructs them, it never contacts the car.
    """
    resolved = coordinator or get_mobile_teleop_coordinator()
    return {"upper": _HybridUpperLeaseAdapter(resolved),
            "car": _HybridCarLeaseAdapter(resolved)}
