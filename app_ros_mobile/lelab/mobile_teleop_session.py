"""Anti-jump teleoperation state machine for driving the car arm from an SO-101 leader.

Pure logic: the coordinator supplies the observed facts (leader health, calibration readiness,
both leases, follower feedback freshness, explicit clutch request). The machine never sends a
command by itself and refuses to enter `following` when any precondition is unmet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

STATES = ("disconnected", "connected", "calibrated", "preview", "aligned", "armed",
          "following", "paused", "fault", "estop")
LINEAR = ["disconnected", "connected", "calibrated", "preview", "aligned", "armed", "following"]
# Entering `following` requires a fresh alignment; these events invalidate any cached reference.
INVALIDATING_EVENTS = ("leader_reconnect", "car_reconnect", "estop_cleared", "calibration_changed",
                       "feedback_lost", "lease_lost", "fault_cleared")


@dataclass
class Gate:
    """Observed preconditions. `ok` is recomputed every poll; nothing is latched."""
    leader_connected: bool = False
    leader_healthy: bool = False
    calibration_ready: bool = False
    calibration_version: str | None = None
    upper_lease_valid: bool = False
    car_lease_valid: bool = False
    follower_feedback_valid: bool = False
    exclusive_control: bool = False
    chassis_locked: bool = True

    def missing(self, state: str) -> list[str]:
        out: list[str] = []
        if state in ("calibrated", "preview", "aligned", "armed", "following") and not self.leader_connected:
            out.append("leader_not_connected")
        if state in ("calibrated", "preview", "aligned", "armed", "following") and not self.leader_healthy:
            out.append("leader_unhealthy")
        if state in ("calibrated", "preview", "aligned", "armed", "following") and not self.calibration_ready:
            out.append("calibration_missing_or_mismatched")
        if state in ("preview", "aligned", "armed", "following") and not self.upper_lease_valid:
            out.append("upper_lease_invalid")
        if state in ("aligned", "armed", "following") and not self.car_lease_valid:
            out.append("car_lease_invalid")
        if state in ("aligned", "armed", "following") and not self.follower_feedback_valid:
            out.append("follower_feedback_missing_or_stale")
        if state in ("armed", "following") and not self.exclusive_control:
            out.append("control_not_exclusive")
        if state == "following" and not self.chassis_locked:
            out.append("chassis_must_stay_locked_in_phase_1")
        return out


@dataclass
class TeleopStateMachine:
    on_clear_targets: Callable[[str], None] | None = None
    state: str = "disconnected"
    clutch_engaged: bool = False
    aligned: bool = False
    alignment: dict[str, Any] | None = None
    deny_reasons: list[str] = field(default_factory=list)
    last_event: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers
    def _record(self, event: str, detail: str = "") -> None:
        self.history.append({"event": event, "state": self.state, "detail": detail,
                             "wall": time.time()})
        self.last_event = event

    def _clear(self, reason: str) -> None:
        self.aligned = False
        self.alignment = None
        self.clutch_engaged = False
        if self.on_clear_targets:
            self.on_clear_targets(reason)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "clutch_engaged": self.clutch_engaged,
            "aligned": self.aligned,
            "alignment": self.alignment,
            "deny_reasons": list(self.deny_reasons),
            "last_event": self.last_event,
            "history_count": len(self.history),
        }

    # ------------------------------------------------------------------ events
    def event(self, name: str, gate: Gate, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        self.deny_reasons = []
        handler = getattr(self, f"_on_{name}", None)
        if handler is None:
            self.deny_reasons = [f"unknown_event:{name}"]
            return self.snapshot()
        before = self.state
        blocked = handler(gate, detail or {})
        if blocked:
            self.deny_reasons = blocked
            self._record(name, "denied:" + ",".join(blocked))
        else:
            self._record(name, f"{before}->{self.state}")
        return self.snapshot()

    # ------------------------------------------------------------------ handlers
    def _on_leader_found(self, gate: Gate, detail: dict) -> list[str] | None:
        if gate.leader_connected:
            self.state = "connected"
            self._clear("leader_found")
            return None
        return ["leader_not_connected"]

    def _on_leader_lost(self, gate: Gate, detail: dict) -> list[str] | None:
        self.state = "disconnected"
        self._clear("leader_lost")
        return None

    def _on_calibration_ready(self, gate: Gate, detail: dict) -> list[str] | None:
        missing = gate.missing("calibrated")
        if missing:
            return missing
        if self.state in ("disconnected",):
            return ["leader_not_connected"]
        self.state = "calibrated"
        self._clear("calibration_changed")
        return None

    def _on_calibration_changed(self, gate: Gate, detail: dict) -> list[str] | None:
        self._clear("calibration_changed")
        if self.state in ("following", "armed", "aligned", "paused"):
            self.state = "calibrated"
        return None

    def _on_enter_preview(self, gate: Gate, detail: dict) -> list[str] | None:
        missing = gate.missing("preview")
        if missing:
            return missing
        if self.state not in ("calibrated", "preview", "aligned", "armed", "paused"):
            return [f"bad_transition:{self.state}->preview"]
        # paused has to be able to come back: 结束一集默认暂停, and the only way to re-establish a
        # new reference is preview -> align -> arm -> engage. Excluding it left the operator with a
        # session that could never record a second episode, while engage still refuses (the
        # alignment was cleared when the session parked), so this cannot resume motion by itself.
        self.state = "preview"
        return None

    def _on_align(self, gate: Gate, detail: dict) -> list[str] | None:
        missing = gate.missing("aligned")
        if missing:
            return missing
        if self.state not in ("preview", "aligned"):
            return [f"bad_transition:{self.state}->aligned"]
        reference = detail.get("reference") or {}
        leader_ref = reference.get("leader_rad")
        follower_ref = reference.get("follower_rad")
        gripper_ref = reference.get("gripper")
        if not leader_ref or not follower_ref:
            return ["alignment_reference_incomplete"]
        if not self._reference_continuous(reference):
            return ["alignment_reference_out_of_limits"]
        self.aligned = True
        self.alignment = {**reference, "wall": time.time(), "calibration_version": gate.calibration_version}
        self.state = "aligned"
        return None

    def _reference_continuous(self, reference: dict) -> bool:
        limits = reference.get("limits_rad") or {}
        follower_ref = reference.get("follower_rad") or {}
        for joint, value in follower_ref.items():
            pair = limits.get(joint)
            if pair is None:
                continue
            if not (float(pair[0]) <= float(value) <= float(pair[1])):
                return False
        return True

    def _on_arm(self, gate: Gate, detail: dict) -> list[str] | None:
        missing = gate.missing("armed")
        if missing:
            return missing
        if self.state != "aligned":
            return [f"bad_transition:{self.state}->armed"]
        self.state = "armed"
        return None

    def _on_engage(self, gate: Gate, detail: dict) -> list[str] | None:
        missing = gate.missing("following")
        if missing:
            return missing
        if self.state not in ("armed", "paused"):
            return [f"bad_transition:{self.state}->following"]
        if self.state == "paused":
            return ["realign_required_after_release"]
        if not self.aligned:
            return ["not_aligned"]
        self.clutch_engaged = True
        self.state = "following"
        return None

    def _on_release(self, gate: Gate, detail: dict) -> list[str] | None:
        """Clutch release stops following and forces a new reference before any restart."""
        self.clutch_engaged = False
        if self.state == "following":
            self.state = "paused"
            self._clear("clutch_released")
        return None

    def _on_pause(self, gate: Gate, detail: dict) -> list[str] | None:
        self.clutch_engaged = False
        if self.state in ("following", "armed"):
            self.state = "paused"
            self._clear("paused")
        return None

    def _on_reconnect(self, gate: Gate, detail: dict) -> list[str] | None:
        self._clear("reconnect")
        self.state = "connected" if gate.leader_connected else "disconnected"
        return None

    def _on_fault(self, gate: Gate, detail: dict) -> list[str] | None:
        self._clear("fault")
        self.state = "fault"
        return None

    def _on_fault_cleared(self, gate: Gate, detail: dict) -> list[str] | None:
        self._clear("fault_cleared")
        self.state = "preview" if gate.upper_lease_valid else "connected"
        return None

    def _on_estop(self, gate: Gate, detail: dict) -> list[str] | None:
        self._clear("estop")
        self.state = "estop"
        return None

    def _on_estop_cleared(self, gate: Gate, detail: dict) -> list[str] | None:
        self._clear("estop_cleared")
        # Recovery never resumes motion: alignment is gone, so the operator must re-align.
        self.state = "preview" if gate.leader_connected else "disconnected"
        return None

    def _on_invalidate(self, gate: Gate, detail: dict) -> list[str] | None:
        """A lost precondition parks the session; motion can only resume after a fresh align."""
        reason = str(detail.get("reason", "invalidated"))
        self._clear(reason)
        if self.state in ("armed", "following", "paused"):
            self.state = "paused"
        return None

    # ------------------------------------------------------------------ tick guard
    def guard_following(self, gate: Gate) -> tuple[bool, list[str]]:
        """Called by the control loop each cycle; any unmet gate drops out of following."""
        missing = gate.missing("following")
        if missing:
            self._clear("gate_lost:" + ",".join(missing))
            self.state = "paused" if self.state == "following" else self.state
            return False, missing
        return self.state == "following", ["state_not_following"] if self.state != "following" else []
