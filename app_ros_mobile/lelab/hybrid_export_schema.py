"""Upper-host mirror of the car-side recording contract plus 9D export validation.

The car owns the append-only timeline (``events.jsonl``) and the raw sqlite3 bag.
This module owns the *export* rules: which 9D samples may become training rows,
which must be rejected, and how a manual phase boundary maps onto the exported
frame grid.

Design rules enforced here (plan sections 3.4 and 5):

* A 9D ``action`` is three chassis velocity **commands** plus five final arm
  targets plus one gripper target.  A command is only usable while its own
  ``ttl_ms`` is still valid at the frame timestamp and its ``seq`` is monotonic
  per (channel, source).  A missing or expired command is rejected -- it is
  never zero-filled.
* A 9D ``state`` is a **verified** base estimate plus six **measured** servo
  positions.  ``command_cache``, a command-integrated estimate, a model
  placeholder and synthetic values are not state.  Anything missing is rejected.
* ``driver_sent`` is a UART write, not mechanical execution.  ``unavailable``
  is a first-class answer for a missing ACK.
* No interpolation across an episode boundary or across a clock epoch, and no
  future state may be used to build a current observation.

The constants are duplicated from
``ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/recording_contract.py`` on
purpose: the car must import nothing from the web app, and
``tests/test_t4_contract_parity.py`` compares both sides with golden vectors so
drift is caught offline.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

PROTOCOL_VERSION = 1

PHASES: tuple[str, ...] = ("drive", "observe", "grasp", "transport", "release")
MANUAL_RESULTS: tuple[str, ...] = ("success", "failure", "unknown")
# ``grasp`` may be followed by manual transport/release; ``observe`` is the
# manipulation-approach segment.  Both are manual phase events, not detections.
MANIPULATION_PHASES: tuple[str, ...] = ("observe", "grasp", "transport", "release")

CONFIRMATION_LEVELS: tuple[str, ...] = (
    "requested", "gateway_accepted", "driver_sent",
    "controller_acknowledged", "execution_observed",
)

CHANNELS: tuple[str, ...] = ("base", "arm", "gripper")
COMMAND_SOURCES: tuple[str, ...] = ("gamepad", "so101_leader", "maintenance")
TRUSTED_JOINT_SOURCE = "servo_position_readback"
TRUSTED_BASE_SOURCES: tuple[str, ...] = ("wheel_odometry_estimate", "imu_fused_estimate")
FORBIDDEN_STATE_SOURCES: tuple[str, ...] = (
    "command_cache", "command_integrated_estimate", "model_placeholder", "synthetic",
)
SIMULATED_JOINT_SOURCE = "simulated_servo_readback"
SIMULATED_BASE_SOURCE = "simulated_wheel_odometry_estimate"
SIMULATED_DATA_SOURCE = "simulated_offline"

SCHEMA_ARM_GRIPPER_6DOF = "arm_gripper_6dof_phase1"
SCHEMA_MOBILE_HYBRID_9D = "mobile_hybrid_manual_9d_v1"
SCHEMA_NAMES: tuple[str, ...] = (SCHEMA_ARM_GRIPPER_6DOF, SCHEMA_MOBILE_HYBRID_9D)

STATE_JOINTS: tuple[str, ...] = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
ARM_JOINTS: tuple[str, ...] = STATE_JOINTS[:5]

ACTION_9D_FIELDS: tuple[str, ...] = (
    "base_vx_cmd", "base_vy_cmd", "base_wz_cmd",
    "joint1_target", "joint2_target", "joint3_target", "joint4_target", "joint5_target",
    "r_joint_target",
)
STATE_9D_FIELDS: tuple[str, ...] = (
    "base_vx_est", "base_vy_est", "base_wz_est",
    "joint1_measured", "joint2_measured", "joint3_measured", "joint4_measured",
    "joint5_measured", "r_joint_measured",
)
ACTION_6D_FIELDS: tuple[str, ...] = (
    "joint1_target", "joint2_target", "joint3_target", "joint4_target", "joint5_target",
    "r_joint_target",
)
STATE_6D_FIELDS: tuple[str, ...] = (
    "joint1_measured", "joint2_measured", "joint3_measured", "joint4_measured",
    "joint5_measured", "r_joint_measured",
)

STATE_9D_UNITS: tuple[str, ...] = ("m/s", "m/s", "rad/s", "rad", "rad", "rad", "rad", "rad", "rad")
ACTION_9D_UNITS: tuple[str, ...] = ("m/s", "m/s", "rad/s", "rad", "rad", "rad", "rad", "rad", "rad")

CONTRACT: dict[str, Any] = {
    "protocol_version": PROTOCOL_VERSION,
    "phases": list(PHASES),
    "manual_results": list(MANUAL_RESULTS),
    "event_kinds": [
        "session_start", "episode_start", "phase_started", "phase_ended",
        "episode_end", "session_stop", "command_status", "fault",
        "calibration_snapshot", "clock_jump", "recording_error", "estop",
        "control_paused", "hybrid_sample",
    ],
    "confirmation_levels": list(CONFIRMATION_LEVELS),
    "channels": list(CHANNELS),
    "command_sources": list(COMMAND_SOURCES),
    "trusted_joint_source": TRUSTED_JOINT_SOURCE,
    "trusted_base_sources": list(TRUSTED_BASE_SOURCES),
    "forbidden_state_sources": list(FORBIDDEN_STATE_SOURCES),
    "schema_names": list(SCHEMA_NAMES),
    "state_joints": list(STATE_JOINTS),
    "arm_joints": list(ARM_JOINTS),
    "action_9d_fields": list(ACTION_9D_FIELDS),
    "state_9d_fields": list(STATE_9D_FIELDS),
    "action_6d_fields": list(ACTION_6D_FIELDS),
    "state_6d_fields": list(STATE_6D_FIELDS),
}


class SampleRejected(ValueError):
    """A 9D row that may not be exported.  ``reason`` is a stable machine token."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason

    def as_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


def simulation_identity(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Validate an explicit offline identity; never infer simulation from a failed read.

    A simulation export is useful integration evidence, but it must not be
    indistinguishable from a hardware recording.  The caller supplies this
    identity in session metadata and the resulting dataset/report carries it.
    """
    if meta.get("source") != SIMULATED_DATA_SOURCE:
        return None
    identity = meta.get("simulation_identity")
    if not isinstance(identity, dict):
        raise SampleRejected(
            "simulation_identity_missing",
            f"source={meta.get('source')!r} requires an explicit simulation_identity",
        )
    name = identity.get("identity")
    if not isinstance(name, str) or not name.strip():
        raise SampleRejected("simulation_identity_missing", "identity must be a non-empty string")
    if identity.get("qualified") is not True or identity.get("not_hardware") is not True:
        raise SampleRejected(
            "simulation_identity_unqualified",
            "qualified=true and not_hardware=true are required",
        )
    return {
        "identity": name.strip(),
        "qualified": True,
        "not_hardware": True,
        "training_eligible": False,
        "evidence_kind": "offline_integration_only",
    }


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SampleRejected("missing_or_non_numeric", f"{field} is not numeric")
    out = float(value)
    if not math.isfinite(out):
        raise SampleRejected("non_finite", f"{field} is not finite")
    return out


def _int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SampleRejected("missing_or_non_integer", f"{field} is not an integer")
    return int(value)


def _require(mapping: dict[str, Any], fields: Iterable[str], what: str) -> None:
    missing = [f for f in fields if f not in mapping]
    if missing:
        raise SampleRejected("missing_field", f"{what} lacks {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Per-sample provenance
# ---------------------------------------------------------------------------
def check_command(
    command: dict[str, Any] | None, *,
    channel: str,
    source: str,
    frame_time_ns: int,
    last_seq: int | None,
) -> dict[str, Any]:
    """One base/arm/gripper command, valid at ``frame_time_ns``.

    ``last_seq`` is the previously accepted sequence for this (channel, source)
    pair. Sequence numbers are strictly increasing but may have gaps when the
    latest-wins queue intentionally drops an older target.
    """
    if channel not in CHANNELS:
        raise SampleRejected("unknown_channel", f"{channel!r}")
    if source not in COMMAND_SOURCES:
        raise SampleRejected("untrusted_command_source", f"{source!r}")
    if command is None:
        raise SampleRejected("missing_command", f"no {channel} command for this frame")
    if not isinstance(command, dict):
        raise SampleRejected("malformed_command", f"{channel} command is not an object")
    _require(command, ("seq", "issued_time_ns", "ttl_ms", "source"), f"{channel} command")
    if command["source"] != source:
        raise SampleRejected("command_source_mismatch",
                             f"{channel} command claims {command['source']!r}, expected {source!r}")
    seq = _int(command["seq"], "seq")
    if last_seq is not None:
        if seq == last_seq:
            raise SampleRejected("command_replayed", f"{channel} seq {seq} repeated")
        if seq < last_seq:
            raise SampleRejected("command_out_of_order", f"{channel} seq {seq} < {last_seq}")
    issued = _int(command["issued_time_ns"], "issued_time_ns")
    ttl_ms = _finite(command["ttl_ms"], "ttl_ms")
    if ttl_ms <= 0:
        raise SampleRejected("command_expired", f"{channel} ttl_ms={ttl_ms} is not positive")
    age_ns = frame_time_ns - issued
    if age_ns < 0:
        # A command issued after the frame it would explain cannot be its action.
        raise SampleRejected("command_from_future", f"{channel} command issued after frame")
    if age_ns > int(ttl_ms) * 1_000_000:
        raise SampleRejected("command_expired",
                             f"{channel} command age {age_ns}ns exceeds ttl {int(ttl_ms)}ms")
    return {"seq": seq, "issued_time_ns": issued, "ttl_ms": ttl_ms,
            "source": source, "age_ns": age_ns,
            "targets": [float(v) for v in command["target"]] if isinstance(command.get("target"), (list, tuple))
            else None}


def check_base_state(sample: dict[str, Any] | None, *, frame_time_ns: int,
                     calibration_hash: str, max_age_s: float,
                     allow_simulated: bool = False) -> dict[str, float]:
    """Verified chassis estimate.  Command integration is never a state."""
    if sample is None:
        raise SampleRejected("missing_base_estimate", "no base estimate for this frame")
    if not isinstance(sample, dict):
        raise SampleRejected("malformed_base_estimate", "base estimate is not an object")
    _require(sample, ("vx_m_s", "vy_m_s", "wz_rad_s", "source_kind", "sample_time_ns",
                      "covariance", "valid", "calibration_hash"), "base estimate")
    kind = sample["source_kind"]
    if kind in FORBIDDEN_STATE_SOURCES:
        raise SampleRejected("forbidden_state_source",
                             f"{kind!r} cannot be used as a training-state source")
    accepted_base_sources = (
        (*TRUSTED_BASE_SOURCES, SIMULATED_BASE_SOURCE)
        if allow_simulated else TRUSTED_BASE_SOURCES
    )
    if kind not in accepted_base_sources:
        raise SampleRejected("untrusted_base_estimate", f"{kind!r} is not a verified estimate")
    if sample["valid"] is not True:
        raise SampleRejected("invalid_base_estimate", "base estimate flagged invalid")
    if sample["calibration_hash"] != calibration_hash:
        raise SampleRejected("calibration_mismatch", "base calibration hash differs from session")
    covariance = sample["covariance"]
    if not isinstance(covariance, (list, tuple)) or len(covariance) != 3 \
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in covariance):
        raise SampleRejected("missing_covariance", "base estimate needs a 3-element covariance")
    if any(not math.isfinite(float(v)) or float(v) < 0 for v in covariance):
        raise SampleRejected("invalid_covariance", "covariance must be finite and non-negative")
    sample_time = _int(sample["sample_time_ns"], "sample_time_ns")
    age_ns = frame_time_ns - sample_time
    if age_ns < 0:
        raise SampleRejected("state_from_future", "base estimate is newer than the frame")
    age_s = age_ns / 1e9
    if age_s > max_age_s:
        raise SampleRejected("stale_base_estimate",
                             f"base estimate age {age_s:.3f}s exceeds {max_age_s}s")
    return {"base_vx_est": _finite(sample["vx_m_s"], "vx_m_s"),
            "base_vy_est": _finite(sample["vy_m_s"], "vy_m_s"),
            "base_wz_est": _finite(sample["wz_rad_s"], "wz_rad_s")}


def check_joint_state(joints: Sequence[dict[str, Any]] | None, *, frame_time_ns: int,
                      calibration_hash: str, max_age_s: float,
                      allow_simulated: bool = False) -> list[float]:
    """Six measured servo positions in canonical joint order."""
    if joints is None:
        raise SampleRejected("missing_joint_state", "no servo readback for this frame")
    if not isinstance(joints, (list, tuple)) or len(joints) != len(STATE_JOINTS):
        raise SampleRejected("wrong_joint_count",
                             f"expected {len(STATE_JOINTS)} measured joints, got "
                             f"{len(joints) if isinstance(joints, (list, tuple)) else type(joints).__name__}")
    out: list[float] = []
    for index, expected in enumerate(STATE_JOINTS):
        sample = joints[index]
        if not isinstance(sample, dict):
            raise SampleRejected("malformed_joint_sample", f"joint slot {index} is not an object")
        _require(sample, ("name", "position_rad", "raw_position", "source_kind",
                          "sample_time_ns", "valid", "calibration_hash"), "joint sample")
        if sample["name"] != expected:
            raise SampleRejected("wrong_joint_order",
                                 f"slot {index} is {sample['name']!r}, expected {expected!r}")
        kind = sample["source_kind"]
        if kind in FORBIDDEN_STATE_SOURCES:
            raise SampleRejected("forbidden_state_source",
                                 f"{expected} source {kind!r} is command echo, not measurement")
        accepted_joint_sources = (
            (TRUSTED_JOINT_SOURCE, SIMULATED_JOINT_SOURCE)
            if allow_simulated else (TRUSTED_JOINT_SOURCE,)
        )
        if kind not in accepted_joint_sources:
            raise SampleRejected("untrusted_joint_source",
                                 f"{expected} source {kind!r} is not measured readback")
        if sample["valid"] is not True or sample.get("error") not in (None, "", False):
            raise SampleRejected("invalid_joint_sample", f"{expected} is flagged invalid")
        if sample["calibration_hash"] != calibration_hash:
            raise SampleRejected("calibration_mismatch", f"{expected} calibration hash differs")
        raw = _finite(sample["raw_position"], "raw_position")
        if not -1000.0 <= raw <= 1000.0:
            raise SampleRejected("raw_position_out_of_range",
                                 f"{expected} raw_position={raw} outside -1000..1000")
        sample_time = _int(sample["sample_time_ns"], "sample_time_ns")
        age_ns = frame_time_ns - sample_time
        if age_ns < 0:
            raise SampleRejected("state_from_future", f"{expected} readback is newer than the frame")
        if age_ns / 1e9 > max_age_s:
            raise SampleRejected("stale_joint_state",
                                 f"{expected} readback age {age_ns / 1e9:.3f}s exceeds {max_age_s}s")
        out.append(_finite(sample["position_rad"], "position_rad"))
    return out


def build_9d_row(
    *,
    frame_time_ns: int,
    commands: dict[str, dict[str, Any] | None],
    base_estimate: dict[str, Any] | None,
    joints: Sequence[dict[str, Any]] | None,
    calibration_hash: str,
    max_state_age_s: float,
    last_seq: dict[tuple[str, str], int] | None = None,
    allow_simulated: bool = False,
) -> tuple[dict[str, Any], dict[tuple[str, str], int]]:
    """Build one 9D ``(action, state)`` row or raise :class:`SampleRejected`.

    ``commands`` is keyed by channel: ``base`` (gamepad), ``arm`` and ``gripper``
    (so101_leader).  Any missing channel rejects the row rather than zero-filling
    it, because a zero velocity is a real command, not an absence.
    """
    last = dict(last_seq or {})
    sources = {"base": "gamepad", "arm": "so101_leader", "gripper": "so101_leader"}
    normalized: dict[str, dict[str, Any]] = {}
    for channel, source in sources.items():
        normalized[channel] = check_command(
            commands.get(channel), channel=channel, source=source,
            frame_time_ns=frame_time_ns, last_seq=last.get((channel, source)),
        )
        last[(channel, source)] = normalized[channel]["seq"]

    base_fields = check_base_state(base_estimate, frame_time_ns=frame_time_ns,
                                   calibration_hash=calibration_hash,
                                   max_age_s=max_state_age_s,
                                   allow_simulated=allow_simulated)
    joint_fields = check_joint_state(joints, frame_time_ns=frame_time_ns,
                                     calibration_hash=calibration_hash,
                                     max_age_s=max_state_age_s,
                                     allow_simulated=allow_simulated)

    def _targets(channel: str) -> list[float]:
        values = normalized[channel].get("targets")
        return values if values is not None else []

    base_action = normalized["base"].get("targets")
    if base_action is None:
        raise SampleRejected("missing_field", "base command lacks vx/vy/wz targets")
    arm_action = _targets("arm")
    gripper_action = _targets("gripper")
    if len(arm_action) != 5:
        raise SampleRejected("wrong_action_shape", "arm command needs five joint targets")
    if len(gripper_action) != 1:
        raise SampleRejected("wrong_action_shape", "gripper command needs one target")
    if len(base_action) != 3:
        raise SampleRejected("wrong_action_shape", "base command needs vx/vy/wz targets")

    action = [_finite(v, "base command") for v in base_action] + \
             [_finite(v, "arm target") for v in arm_action] + \
             [_finite(v, "gripper target") for v in gripper_action]
    state = [base_fields["base_vx_est"], base_fields["base_vy_est"], base_fields["base_wz_est"]] + joint_fields
    if len(action) != 9 or len(state) != 9:
        raise SampleRejected("wrong_action_shape", "9D row must hold nine action and nine state values")
    row = {
        "time_ns": frame_time_ns,
        "action": action,
        "state": state,
        "action_fields": list(ACTION_9D_FIELDS),
        "state_fields": list(STATE_9D_FIELDS),
        "provenance": {
            "action_sources": {ch: normalized[ch]["source"] for ch in normalized},
            "action_seq": {ch: normalized[ch]["seq"] for ch in normalized},
            "action_age_ns": {ch: normalized[ch]["age_ns"] for ch in normalized},
            "state_joint_source": (
                SIMULATED_JOINT_SOURCE if allow_simulated else TRUSTED_JOINT_SOURCE
            ),
            "state_base_source": (base_estimate or {}).get("source_kind"),
            "calibration_hash": calibration_hash,
        },
    }
    if allow_simulated:
        row["provenance"]["data_identity"] = "simulated_offline"
        row["provenance"]["training_eligible"] = False
    if normalized["gripper"].get("note"):
        row["provenance"]["gripper_note"] = normalized["gripper"]["note"]
    return row, last


# ---------------------------------------------------------------------------
# Phase timelines
# ---------------------------------------------------------------------------
def check_phase_timeline(phases: Sequence[dict[str, Any]] | None, *,
                         episode_start_ns: int, episode_end_ns: int) -> list[dict[str, Any]]:
    """Manual phase boundaries must be monotonic and inside their own episode.

    A phase change carries no motion side effect, and re-stating the active phase
    is rejected instead of being silently merged.
    """
    out: list[dict[str, Any]] = []
    previous_end = episode_start_ns
    previous_phase: str | None = None
    for entry in phases or []:
        if not isinstance(entry, dict):
            raise SampleRejected("malformed_phase", "phase entry is not an object")
        name = entry.get("phase")
        if name not in PHASES:
            raise SampleRejected("unknown_phase", f"{name!r}")
        start = entry.get("started_time_ns")
        end = entry.get("ended_time_ns")
        if isinstance(start, bool) or not isinstance(start, int):
            raise SampleRejected("missing_phase_time", "phase start is not an integer")
        if isinstance(end, bool) or not isinstance(end, int):
            raise SampleRejected("unterminated_phase",
                                 f"{name} has no ended_time_ns; the episode did not close it")
        if start < previous_end:
            raise SampleRejected("phase_overlap", f"{name} starts before the previous phase ended")
        if end <= start:
            raise SampleRejected("empty_phase", f"{name} ends at or before it starts")
        if end > episode_end_ns:
            raise SampleRejected("phase_after_episode", f"{name} ends after the episode")
        if previous_phase == name:
            raise SampleRejected("phase_already_active", f"{name} repeated back to back")
        out.append({"phase": name, "started_time_ns": start, "ended_time_ns": end})
        previous_end = end
        previous_phase = name
    return out


def phase_slices(phases: Sequence[dict[str, Any]], *, aligned_start_ns: int,
                 frame_count: int, fps: float) -> list[dict[str, Any]]:
    """Map car-clock boundaries onto the exported frame grid, half-open.

    ``source`` is recorded explicitly so a downstream consumer can tell a real
    car-clock boundary from any later resampling.
    """
    if fps <= 0:
        raise SampleRejected("invalid_fps", f"fps={fps}")
    step_ns = 1e9 / fps
    slices: list[dict[str, Any]] = []
    for entry in phases:
        start, end = entry["started_time_ns"], entry["ended_time_ns"]
        first = min(frame_count, max(0, math.ceil((start - aligned_start_ns) / step_ns)))
        stop = min(frame_count, max(0, math.ceil((end - aligned_start_ns) / step_ns)))
        slices.append({
            "phase": entry["phase"],
            "started_time_ns": start,
            "ended_time_ns": end,
            "frame_from": first,
            "frame_to_exclusive": stop,
            "from_s": round(first / fps, 6),
            "to_s": round(stop / fps, 6),
            "frame_count": max(0, stop - first),
            "source": "car_events.jsonl.phase_started/phase_ended; car clock; half-open [from,to)",
        })
    return slices


def require_observe_grasp(slices: Sequence[dict[str, Any]], *, require_frames: bool = True) -> None:
    """A hybrid episode is only training-eligible with an observe and a grasp phase.

    ``transport``/``release`` are optional continuations of the grasp segment.
    ``require_frames=False`` is used for the pre-flight check, where the frame
    grid is not known yet.
    """
    names = [s["phase"] for s in slices]
    for required in ("observe", "grasp"):
        if required not in names:
            raise SampleRejected("missing_required_phase",
                                 f"episode has no {required} phase (found {names or 'none'})")
    if not require_frames:
        return
    for entry in slices:
        if entry["frame_count"] <= 0:
            raise SampleRejected("empty_phase_slice",
                                 f"phase {entry['phase']} covers no exported frame")


def manual_result(value: Any) -> str:
    """Manual success/failure/unknown only; never a simulator ground truth."""
    if value is None or value == "":
        return "unknown"
    if value is True or value == "success":
        return "success"
    if value is False or value in ("failure", "failed", "false"):
        return "failure"
    if value == "unknown":
        return "unknown"
    raise SampleRejected("invalid_manual_result", f"unreadable manual result {value!r}")


def command_status_view(status: dict[str, Any] | None) -> dict[str, Any]:
    """Confirmation levels with explicit ``unavailable``; never upgrades evidence."""
    raw = dict(status or {})
    out: dict[str, Any] = {}
    for level in CONFIRMATION_LEVELS:
        value = raw.get(level)
        out[level] = "unavailable" if value is None or value is False else value
    for level in CONFIRMATION_LEVELS:
        if raw.get(level) == "unavailable":
            out[level] = "unavailable"
    if out["execution_observed"] not in ("unavailable",):
        evidence = raw.get("execution_evidence")
        if not isinstance(evidence, dict) or evidence.get("source_kind") != TRUSTED_JOINT_SOURCE \
                or evidence.get("valid") is not True:
            raise SampleRejected("unproven_execution",
                                 "execution_observed needs measured readback; driver_sent is not executed")
    out["highest_confirmed"] = next(
        (level for level in reversed(CONFIRMATION_LEVELS)
         if out[level] not in ("unavailable", None, False)),
        "unavailable",
    )
    if out["highest_confirmed"] == "driver_sent":
        out["ack_note"] = "driver_sent is a UART write, not execution"
    return out


SIMULATED_6D_CONTRACT = "simulated_offline_6d_v1"
# The offline profile is named explicitly on purpose: this contract exists so the local
# Docker/Humble rig can exercise the real parse-export-readback chain, and the safest way
# to keep it from being reached by an accidentally-similar recording is to require the
# profile that declares it.
SIMULATED_6D_PROFILES = frozenset({"hardware_hybrid_manual_v1_offline6d"})
SIMULATED_6D_RUNTIME_MODES = frozenset({"simulated_hybrid_manual", "hybrid_manual_sim"})
SIMULATED_6D_ROW_TOPIC_SUFFIX = "/recording/teleop"


def simulated_6d_identity(meta: dict[str, Any]) -> dict[str, Any]:
    """Decide, fail closed, whether this session may use the offline simulated 6D contract.

    The contract exists because the six-dimensional exporter otherwise accepts only a
    `hardware_teleop` session with training export enabled: both of those claims would be
    lies about the local rig.  Nothing here re-labels the data.  Every check reads a fact
    the car already recorded, and the identity that comes out is the same
    `simulated_offline` / `training_eligible=false` pair the rows carry, so the derived
    dataset can never be mistaken for, or mixed into, hardware evidence.
    """
    def reject(reason: str, detail: str) -> None:
        raise SampleRejected(reason, detail)

    if meta.get("simulated") is not True:
        reject("simulated_6d_requires_declared_simulation",
               f"runtime declares simulated={meta.get('simulated')!r}; the offline contract "
               "is only for profiles that declare simulation")
    if meta.get("data_identity") != SIMULATED_DATA_SOURCE:
        reject("simulated_6d_identity_missing",
               f"session data_identity={meta.get('data_identity')!r}, expected "
               f"{SIMULATED_DATA_SOURCE!r}")
    if meta.get("profile") not in SIMULATED_6D_PROFILES:
        reject("simulated_6d_profile_not_offline",
               f"profile={meta.get('profile')!r} is not one of {sorted(SIMULATED_6D_PROFILES)}")
    if meta.get("runtime_mode") not in SIMULATED_6D_RUNTIME_MODES:
        reject("simulated_6d_runtime_mode",
               f"runtime_mode={meta.get('runtime_mode')!r} is not an offline simulated mode")
    if meta.get("export_schema") != SCHEMA_ARM_GRIPPER_6DOF:
        reject("simulated_6d_schema", f"export_schema={meta.get('export_schema')!r}")
    # A profile that permits training export is not an offline rig, and the contract must
    # not become a way to route around that.
    if meta.get("training_export") is not False:
        reject("simulated_6d_training_switch",
               f"training_export={meta.get('training_export')!r}; the offline profile must "
               "declare it false")
    provenance = meta.get("feedback_provenance") or {}
    if provenance.get("hardware_measurement") is True:
        reject("simulated_6d_claims_hardware_measurement",
               "the session records a hardware measurement, so it is not offline simulated "
               "evidence")
    if not meta.get("clock_domain") or not meta.get("clock_epoch"):
        reject("simulated_6d_clock_identity",
               f"clock_domain={meta.get('clock_domain')!r} clock_epoch={meta.get('clock_epoch')!r}")
    # A row's state is whatever the control process last read from the board, so the row
    # itself has to say how old that read was, and the contract needs a bound to check it
    # against.  Without both, a dataset could quietly be built from stale positions.
    max_state_age = provenance.get("max_state_age_s")
    if isinstance(max_state_age, bool) or not isinstance(max_state_age, (int, float)) \
            or not 0 < float(max_state_age) <= 0.5:
        reject("simulated_6d_state_age_unbounded",
               f"feedback_provenance.max_state_age_s={max_state_age!r}; the rows' own feedback "
               "age has nothing to be checked against")
    return {
        "max_state_age_s": float(max_state_age),
        "contract": SIMULATED_6D_CONTRACT,
        "data_identity": SIMULATED_DATA_SOURCE,
        "training_eligible": False,
        "evidence_kind": "offline_integration_only",
        # The rows are written by the control process: the action is the target it sent, and
        # the state is the readback it saw at that instant.  Neither is a hardware measure.
        "action_source": "car_side_sent_target_recorded_by_the_control_process",
        "state_source": "simulated_servo_readback_recorded_by_the_control_process",
        "sampling": "event_paced_teleop_rows",
        "clock_domain": meta.get("clock_domain"),
        "clock_epoch": meta.get("clock_epoch"),
        "profile": meta.get("profile"),
        "row_topic_suffix": SIMULATED_6D_ROW_TOPIC_SUFFIX,
    }
