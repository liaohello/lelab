"""Explicit opt-in frame-stream composition for the existing hybrid facade.

The serial and car leases, calibration, router, and car gateway remain authoritative.
The status poller only caches observations; the follow worker owns the mapper.
This module does not open a device at import.
"""
from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any

from .hybrid_calibration import HybridCalibrationStore
from .hybrid_channel import TrustedFeedbackGate
from .hybrid_frame_link import FrameLink
from .hybrid_home_plan import HOME_RAW, plan_home_step, raw_to_rad
from .hybrid_hardware import (
    ARM_JOINTS, CarControlLease, CarHttpOutputDriver, HardwareCompositionError,
    HardwareObserver, HardwarePreviewFacade, UpperLeaderLease, _calibration_root,
    _http_json,
)


class StreamHardwareObserver(HardwareObserver):
    """Publish the latest age-carrying observation from one background poller."""

    FEEDBACK_MAX_AGE_S = 1.0

    def __init__(self, *args: Any, poll_period_s: float = 0.10,
                 simulated_source: bool = False, trial_max_step_rad_s: float | None = None,
                 filter_alpha_override: float | None = None,
                 deadband_rad_override: float | None = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.poll_period_s = max(0.05, float(poll_period_s))
        self.simulated_source = bool(simulated_source)
        self.trial_max_step_rad_s = trial_max_step_rad_s
        self.filter_alpha_override = filter_alpha_override
        self.deadband_rad_override = deadband_rad_override
        self._cache_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._cached_facts: dict[str, Any] | None = None
        self._cached_car: dict[str, Any] | None = None
        self._cache_started_at: float | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._preview_sequence: int | None = None
        self._preview_value: dict[str, Any] | None = None
        self._aligned_layers: dict[str, dict[str, Any]] | None = None
        self._display_mapper = None
        self.poll_error: str | None = None
        self.poll_count = 0
        self.poll_error_count = 0
        self.last_poll_duration_s: float | None = None

    def _preview(self, leader: dict[str, Any], follower: dict[str, float],
                 layers: dict[str, dict[str, Any]]) -> dict[str, Any]:
        sequence = leader.get("reads")
        if (isinstance(sequence, int) and not isinstance(sequence, bool)
                and sequence == self._preview_sequence and self._preview_value is not None):
            return copy.deepcopy(self._preview_value)
        result = super()._preview(leader, follower, layers)
        if result.get("available") and isinstance(sequence, int) and not isinstance(sequence, bool):
            self._preview_sequence = sequence
            self._preview_value = copy.deepcopy(result)
        return result

    def _poll(self, *, timeout_s: float | None = None,
              total_timeout_s: float | None = None) -> None:
        """Poll once, optionally bounding lock wait and HTTP in one budget."""
        started = time.monotonic()
        deadline = (None if total_timeout_s is None
                    else started + max(0.0, total_timeout_s))
        if total_timeout_s is None:
            self._poll_lock.acquire()
        elif not self._poll_lock.acquire(timeout=max(0.0, total_timeout_s)):
            raise HardwareCompositionError("observer_poll_lock_timeout")
        try:
            request_timeout = self.timeout_s if timeout_s is None else timeout_s
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HardwareCompositionError("observer_poll_timeout")
                request_timeout = min(request_timeout, remaining)
            car = _http_json(self.agent_url, "/status", timeout=request_timeout)
            if bool(car.get("simulated")) != self.simulated_source:
                raise HardwareCompositionError("frame_simulation_identity_mismatch")
            facts = super().snapshot(car_status=car, network_elapsed_s=time.monotonic() - started,
                                     advance_preview=False)
            if self.simulated_source:
                facts["simulated"] = True
                for device in facts.get("devices", {}).values():
                    device["simulated"] = True
                facts["feedback"]["data_identity"] = "simulated_offline"
            with self._cache_lock:
                self._cached_car = car
                self._cached_facts = facts
                # Include the whole request and conversion in the cache age.
                self._cache_started_at = started
                self.last_poll_duration_s = round(time.monotonic() - started, 3)
                self.poll_count += 1
                self.poll_error = None
        finally:
            self._poll_lock.release()

    def start_cache(self) -> None:
        if self._poll_thread is not None:
            return
        self._poll()
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="hybrid-frame-observer",
                                             daemon=True)
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        while not self._poll_stop.wait(self.poll_period_s):
            try:
                self._poll()
            except Exception as exc:  # noqa: BLE001 - stale data is rejected below
                self.poll_error = f"{type(exc).__name__}:{exc}"
                self.poll_error_count += 1

    def stop_cache(self) -> None:
        self._poll_stop.set()
        thread = self._poll_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.timeout_s + 0.2)
            if thread.is_alive():
                raise HardwareCompositionError("observer_poller_did_not_stop")
        self._poll_thread = None
        with self._cache_lock:
            self._cached_car = self._cached_facts = None
            self._cache_started_at = None
        self._preview_sequence = None
        self._preview_value = None
        self._aligned_layers = None
        self._display_mapper = None

    def car_snapshot(self) -> tuple[dict[str, Any] | None, float | None]:
        with self._cache_lock:
            car = copy.deepcopy(self._cached_car)
            started = self._cache_started_at
        return car, None if started is None else max(0.0, time.monotonic() - started)

    def cache_state(self) -> dict[str, Any]:
        """How current the observation cache is, and what the last poll cost.

        ``lease_lost`` and ``stale`` look identical from the outside; this is what
        tells them apart without guessing from the last known state.
        """
        with self._cache_lock:
            started = self._cache_started_at
            return {"running": self._poll_thread is not None,
                    "poll_period_s": self.poll_period_s,
                    "polls": self.poll_count,
                    "cache_age_s": (round(max(0.0, time.monotonic() - started), 3)
                                    if started is not None else None),
                    "last_poll_duration_s": self.last_poll_duration_s,
                    "last_error": self.poll_error}

    def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        if self._poll_thread is None:
            return super().snapshot(**kwargs)
        with self._cache_lock:
            facts = copy.deepcopy(self._cached_facts)
            started = self._cache_started_at
        if facts is None or started is None:
            raise HardwareCompositionError("frame_observation_missing")
        elapsed = max(0.0, time.monotonic() - started)
        leader = facts.get("leader") or {}
        age = leader.get("age_s")
        if not isinstance(age, (int, float)) or isinstance(age, bool) or age + elapsed > 0.5:
            leader["healthy"] = False
            leader["reason"] = "leader_cached_sample_stale"
            facts["leader_healthy"] = False
        else:
            leader["age_s"] = float(age) + elapsed
        if elapsed > self.FEEDBACK_MAX_AGE_S:
            facts["feedback_valid"] = False
            facts["exclusive_control"] = False
        if self._preview_value is not None:
            facts["preview"] = copy.deepcopy(self._preview_value)
        elif (self._display_mapper is not None and self._aligned_layers is not None
              and facts.get("leader_healthy") and facts.get("feedback_valid")):
            # Display calculations must not advance the control worker's mapper.
            # A fresh copy of the alignment-time mapper gives a bounded, explicitly
            # non-emitting estimate; the live follow mapper remains untouched.
            facts["preview"] = super()._preview(
                leader, (facts.get("feedback") or {}).get("positions_rad") or {},
                self._aligned_layers, mapper_override=copy.deepcopy(self._display_mapper))
            facts["preview"]["preview_mapper"] = "independent_alignment_snapshot"
        return facts

    def fresh_snapshot(self, *, max_cache_age_s: float = 0.20,
                       timeout_s: float = 0.35) -> dict[str, Any]:
        """Refresh a late cache once before returning it to the control worker.

        The background poller normally runs at 10 Hz. A scheduler or HTTP hiccup
        must not turn an otherwise measurable 1.0-second feedback age into a hard
        fault while the car can still provide a fresh scan. The retry is bounded,
        and the caller's normal 1.0-second freshness gate remains unchanged.
        """
        with self._cache_lock:
            started = self._cache_started_at
        cache_age_s = (None if started is None
                       else max(0.0, time.monotonic() - started))
        if cache_age_s is None or cache_age_s > max_cache_age_s:
            if self._poll_thread is None:
                self._poll(timeout_s=timeout_s,
                           total_timeout_s=timeout_s)
            else:
                try:
                    self._poll(timeout_s=timeout_s,
                               total_timeout_s=timeout_s)
                except Exception as exc:  # noqa: BLE001 - the normal gate still decides
                    self.poll_error = f"{type(exc).__name__}:{exc}"
                    self.poll_error_count += 1
        return self.snapshot()

    def capture_reference(self) -> dict[str, Any]:
        if self._poll_thread is not None:
            self._poll()  # do not align against an older cached leader sample
        reference = super().capture_reference()
        if self.trial_max_step_rad_s is not None:
            # A runtime cap can only tighten the saved mapping. The car's independent
            # trial guard remains authoritative and the calibration asset is unchanged.
            self.mapper.max_step_rad_s = min(self.mapper.max_step_rad_s,
                                            self.trial_max_step_rad_s)
        if self.filter_alpha_override is not None:
            self.mapper.filter_alpha = min(
                self.mapper.filter_alpha, self.filter_alpha_override)
        if self.deadband_rad_override is not None:
            self.mapper.deadband_rad = max(
                self.mapper.deadband_rad, self.deadband_rad_override)
        self._preview_sequence = None
        self._preview_value = None
        self._aligned_layers = self._load_layers()
        self._display_mapper = copy.deepcopy(self.mapper)
        if self._poll_thread is not None:
            self._poll()  # align must expose the new reference before returning
        return reference

    def map_sample(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Advance the mapper only in the follow worker, once per leader sample."""
        if self._aligned_layers is None:
            raise HardwareCompositionError("frame_mapping_not_aligned")
        leader = facts.get("leader") or {}
        follower = (facts.get("feedback") or {}).get("positions_rad") or {}
        return self._preview(leader, follower, self._aligned_layers)

    def follower_asset(self) -> dict[str, Any]:
        """Return the saved B-layer asset used for raw-to-radian conversion."""
        layers = self._load_layers()
        asset = layers.get("follower") if isinstance(layers, dict) else None
        if not isinstance(asset, dict):
            raise HardwareCompositionError("follower_calibration_missing")
        return copy.deepcopy(asset)


class FrameCarControlLease(CarControlLease):
    """Use the existing car lease and a fresh copy of its bound epoch."""

    observer: StreamHardwareObserver | None = None
    # The freshness window the observation cache is allowed to drift, and the shortest
    # gap between direct ``/status`` probes. Both are the same 0.5 s the snapshot gate
    # already uses; neither is a safety budget and neither is widened here.
    CACHE_MAX_AGE_S = 1.0
    PROBE_MIN_INTERVAL_S = 0.5
    PROBE_TIMEOUT_S = 1.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._probe_lock = threading.Lock()
        self._last_probe_mono: float | None = None
        self._last_probe_ok = False
        self.last_probe_source: str | None = None

    def _assess(self, car: dict[str, Any], session: dict[str, Any], source: str) -> bool:
        control = car.get("control") or {}
        hybrid = car.get("hybrid") or {}
        licence = car.get("licence") or {}
        if control.get("active") is not True:
            self.last_reason = "car_control_inactive"
        elif licence.get("output_transport") != "frame_stream":
            self.last_reason = "car_frame_transport_missing"
        elif hybrid.get("session_id") != session.get("session_id"):
            self.last_reason = "car_session_mismatch"
        elif str(hybrid.get("session_epoch")) != str(session.get("session_epoch")):
            self.last_reason = "car_epoch_mismatch"
        elif hybrid.get("calibration_bundle_hash") != session.get("calibration_bundle_hash"):
            self.last_reason = "car_bundle_mismatch"
        else:
            self.last_reason = "car_lease_held"
            self.last_valid_at_mono = time.monotonic()
            self.last_probe_source = source
            return True
        self.last_probe_source = source
        return False

    def _probe(self, session: dict[str, Any]) -> bool:
        """Assess the car directly, at most once per freshness window."""
        with self._probe_lock:
            now = time.monotonic()
            if (self._last_probe_mono is not None
                    and now - self._last_probe_mono < self.PROBE_MIN_INTERVAL_S):
                # Reuse the verdict of the probe that just ran; the reason it produced
                # is still the current one.
                self.last_probe_source = "probe_recent"
                return self._last_probe_ok
            status = self._live_status(self.PROBE_TIMEOUT_S)
            if status is None:
                self._last_probe_ok = False
                self.last_probe_source = "probe_failed"
                return False
            ok = self._assess(status, session, "probe")
            self._last_probe_ok = ok
            self._last_probe_mono = time.monotonic()
            return ok

    def valid(self, token: str) -> bool:
        ok, session, _remaining = self._local_state(token)
        if not ok:
            return False
        if self.observer is None or self.observer._poll_thread is None:
            status = self._live_status()
            if status is None:
                return False
            return self._assess(status, session, "direct")
        car, age = self.observer.car_snapshot()
        if isinstance(car, dict) and age is not None and age <= self.CACHE_MAX_AGE_S:
            return self._assess(car, session, "cache")
        # A stale cache means the poller was slow, not that the car took the lease away.
        # Ask the car before parking the session; the observation freshness gate still
        # blocks any following motion until the cache is live again.
        return self._probe(session)

    def _driver_available_before_epoch(self, status: dict[str, Any]) -> bool:
        if super()._driver_available_before_epoch(status):
            return True
        # A freshly started frame agent has a readback subscriber but does not
        # bind it to its gateway until begin_epoch. Verify the declared channel
        # and sample identities here; the normal acquire path then requires the
        # bound driver and fully trusted, calibrated feedback before returning.
        if (status.get("licence") or {}).get("output_transport") != "frame_stream":
            return False
        feedback = (status.get("control") or {}).get("follower_feedback") or {}
        samples = feedback.get("joint_samples") or {}
        ids = {"joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4, "joint5": 5, "r_joint": 10}
        return all(isinstance(samples.get(name), dict)
                   and samples[name].get("servo_id") == servo_id
                   and samples[name].get("source_kind") == "servo_position_readback"
                   and isinstance(samples[name].get("age_s"), (int, float))
                   and not isinstance(samples[name].get("age_s"), bool)
                   and 0 <= samples[name]["age_s"] <= StreamHardwareObserver.FEEDBACK_MAX_AGE_S
                   for name, servo_id in ids.items())

    def acquire(self, request: dict) -> str:
        status = _http_json(self.agent_url, "/status", timeout=self.timeout_s)
        licence = status.get("licence") or {}
        if licence.get("output_transport") != "frame_stream":
            raise HardwareCompositionError("frame_transport_not_available")
        return super().acquire(request)


class FrameStreamDriver(CarHttpOutputDriver):
    """Enable through the agent, then submit only complete frames over its stream."""

    # The emitter verifies per-ID torque inside its own tick after the permission
    # arrives. Wait long enough for that bounded state machine, but never forever.
    EMITTER_ENABLE_TIMEOUT_S = 5.0

    def __init__(self, *args: Any, host: str, port: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.host, self.port = host, port
        self.link: FrameLink | None = None
        self._needs_rebind = False

    def capabilities(self) -> dict[str, Any]:
        return {**super().capabilities(), "driver": "frame_stream_v1",
                "transport": "frame_stream_v1", "frame_protocol_version": 1}

    def enable(self, session: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.lease.token
        if not token or not isinstance(session, dict):
            raise HardwareCompositionError("car_control_lease_missing")
        if self._needs_rebind:
            # Agent stop revokes all gateway channel permits. Recreate the
            # identical lease-bound epoch before a new explicit engagement;
            # the car clears its old queue and the new stream gets its own epoch.
            rebound = _http_json(self.agent_url, "/hybrid/begin_epoch", method="POST",
                body={**session, "channels": ["arm", "gripper"]}, token=token,
                timeout=self.timeout_s)
            if (rebound.get("driver_bound") is not True or
                    rebound.get("session_id") != session.get("session_id") or
                    str(rebound.get("session_epoch")) != str(session.get("session_epoch")) or
                    rebound.get("calibration_bundle_hash") != session.get("calibration_bundle_hash")):
                raise HardwareCompositionError("frame_epoch_rebind_not_confirmed")
            # Rebinding invalidates the old calibrated readback. Wait for a new
            # complete scan instead of enabling against the preceding epoch.
            deadline = time.monotonic() + 1.5
            while True:
                status = _http_json(self.agent_url, "/status", timeout=self.timeout_s)
                feedback = (status.get("control") or {}).get("follower_feedback") or {}
                if feedback.get("available") is True and self.lease._driver_available_before_epoch(status):
                    break
                if time.monotonic() >= deadline:
                    raise HardwareCompositionError("frame_rebind_feedback_timeout")
                time.sleep(0.05)
            self._needs_rebind = False
        link = FrameLink(host=self.host, port=self.port, session_id=session["session_id"],
                         session_epoch=session["session_epoch"],
                         bundle_hash=session["calibration_bundle_hash"], lease_token=token)
        try:
            link.attach_lease(token)
            welcome = link.engage()
            if welcome.get("frame_protocol_version") != 1:
                raise HardwareCompositionError("frame_protocol_version_mismatch")
            result = super().enable(session)
            if result.get("emitter") != "car_control_process":
                raise HardwareCompositionError("frame_emitter_not_confirmed")
            self._await_emitter_ready()
            self.link = link
            return result
        except BaseException:
            link.client.close()
            self.enabled = False
            try:
                self.lease.stop("frame_enable_rollback")
            except Exception:
                pass
            raise

    def _await_emitter_ready(self) -> None:
        """The agent's flag is permission; the emitter still has to report enabled.

        Homing loads and verifies torque inside the control process after the
        permission arrives. A frame sent before that would be refused as an emitter
        failure, so enabling is only complete once the process itself says so.
        """
        deadline = time.monotonic() + self.EMITTER_ENABLE_TIMEOUT_S
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                status = _http_json(self.agent_url, "/status", timeout=1.0)
            except Exception as exc:  # noqa: BLE001 - a failed read is retried, bounded
                last = {"reason": f"status_read_failed:{type(exc).__name__}"}
                time.sleep(0.1)
                continue
            process = ((((status.get("licence") or {}).get("bridge") or {})
                        .get("frame_process")) or {})
            last = dict(process)
            if process.get("outputs_enabled") is True:
                if (process.get("homing_motion") is None
                        or process.get("homing_torque_state") == "verified"):
                    return
                if process.get("homing_torque_state") == "failed":
                    raise HardwareCompositionError(
                        "frame_emitter_enable_failed:homing_torque_unverified:" +
                        str(process.get("homing_torque_error")))
            time.sleep(0.1)
        raise HardwareCompositionError(
            "frame_emitter_enable_timeout:" +
            str(last.get("homing_torque_state") or last.get("reason") or "unknown"))

    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        raise HardwareCompositionError("single_channel_submit_forbidden_in_frame_mode")

    def abort(self) -> None:
        link = self.link
        self.enabled = False
        if link is not None:
            link.engaged = False
            link.client.abort()

    def stop(self, reason: str) -> dict[str, Any]:
        link, self.link = self.link, None
        self._needs_rebind = True
        if link is not None:
            try:
                link.disengage(reason)
            finally:
                link.client.close()
        return super().stop(reason)


class FrameStreamHardwareFacade(HardwarePreviewFacade):
    """Keep the production state machine while replacing only the follow transport."""

    # The vendor servo has a measured deadband around ten ticks and can also
    # back-drive a few ticks between feedback scans. Fourteen ticks per 0.4 s
    # crosses the deadband while leaving six ticks of margin under the car's
    # independent twenty-tick hard step bound.
    # Keep five ticks of feedback-skew margin below the car's twenty-tick hard bound.
    HOME_MAX_STEP_RAW = 15
    # The STS servo has a measured deadband around four ticks. Five ticks keeps
    # completion tied to observed position while allowing the measured bias.
    HOME_TOLERANCE_RAW = 5
    HOME_SETTLE_TOLERANCE_RAW = 2
    HOME_STEP_INTERVAL_S = 0.4
    # Keep the car-side 300 ms output watchdog fed while waiting for the next
    # measured scan; the repeated frame carries the same pending target.
    HOME_REPEAT_INTERVAL_S = 0.1
    HOME_SETTLE_TIMEOUT_S = 2.5
    # A commanded segment that moves away from its target by more than half the
    # vendor's ~10-tick measured deadband is a reversal, not scan noise.
    HOME_REVERSE_TOLERANCE_RAW = 5
    # Per-scan movement below the settle tolerance does not count as a direction.
    HOME_MOTION_NOISE_RAW = 2
    # Three sign changes of >= HOME_MOTION_NOISE_RAW within one segment is an
    # oscillation: the axis is not converging even if each single scan moves.
    HOME_OSCILLATION_REVERSALS = 3
    # Non-selected axes are compared with the task-start scan, never with the
    # continuously refreshed hold target. The bound is the documented ~10-tick
    # deadband; ordinary back-drive stays inside it, real drift does not.
    HOME_DRIFT_TOLERANCE_RAW = 10
    # Commands and the six-servo feedback scan share one serial transaction.
    # Five Hz leaves enough bus time for the scan to remain fresh.
    FOLLOW_HZ = 5.0
    FEEDBACK_MAX_AGE_S = StreamHardwareObserver.FEEDBACK_MAX_AGE_S
    # One missed feedback scan is normally repaired by the next ~220 ms frame.
    # Wait for that new scan without emitting the rejected target. This is a
    # recovery wait, not a relaxation of the 500 ms freshness gate.
    FEEDBACK_RECOVERY_WINDOW_S = 0.25
    FEEDBACK_RETRY_POLL_S = 0.05

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._home_stop = threading.Event()
        self._home_thread: threading.Thread | None = None
        self._home_generation = 0
        self._home_asset: dict[str, Any] | None = None
        self._home_targets: dict[str, int] = dict(HOME_RAW)
        self._home_feedback_owner: int | None = None
        self._home_feedback_generation: int | None = None
        self._home_start_raw: dict[str, int] = {}
        self._home_task: dict[str, Any] = {
            "state": "idle",
            "generation": 0,
            "scope": "full_pose",
            "session_id": None,
            "session_epoch": None,
            "calibration_bundle_hash": None,
            "targets_raw": dict(HOME_RAW),
            "tolerance_raw": self.HOME_TOLERANCE_RAW,
            "max_step_raw": self.HOME_MAX_STEP_RAW,
            "current_joint": None,
            "observed_raw": {},
            "step_target_raw": None,
            "remaining_raw": None,
            "progress_pct": 0.0,
            "feedback_age_s": None,
            "feedback_sample_seq": None,
            "feedback_publisher_instance": None,
            "feedback_publisher_generation": None,
            "outputs_enabled": False,
            "started_mono_ns": None,
            "updated_mono_ns": None,
            "last_error": None,
        }

    def _home_set(self, **updates: Any) -> dict[str, Any]:
        with self.lock:
            self._home_task.update(updates)
            self._home_task["updated_mono_ns"] = time.monotonic_ns()
            return copy.deepcopy(self._home_task)

    def home_status(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self._home_task)

    @staticmethod
    def _feedback_sample_seq(feedback: dict[str, Any]) -> int | None:
        samples = feedback.get("joint_samples") or {}
        values = []
        for name in (*ARM_JOINTS, "r_joint"):
            sample = samples.get(name) if isinstance(samples, dict) else None
            value = sample.get("sample_seq") if isinstance(sample, dict) else None
            if type(value) is not int:
                return None
            values.append(value)
        return max(values) if len(set(values)) == 1 and values else None

    @staticmethod
    def _feedback_age(feedback: dict[str, Any]) -> float | None:
        oldest = feedback.get("oldest_age_s")
        if isinstance(oldest, (int, float)) and not isinstance(oldest, bool):
            return float(oldest)
        ages = [
            float(sample["age_s"])
            for sample in (feedback.get("samples") or [])
            if isinstance(sample, dict)
            and isinstance(sample.get("age_s"), (int, float))
            and not isinstance(sample.get("age_s"), bool)
        ]
        return max(ages) if ages else None

    def _home_enable(self, session: dict[str, Any]) -> None:
        driver = self.driver
        router = self.router
        if not isinstance(driver, FrameStreamDriver) or router is None:
            raise HardwareCompositionError("home_frame_driver_unavailable")
        driver.enable(session)
        router.enable_outputs()
        router.grant("arm", "gripper")

    def _home_disable(self, reason: str) -> None:
        router = self.router
        if router is not None:
            try:
                router.stop(reason)
            except Exception:
                pass

    def _home_fail(self, reason: str) -> None:
        self._home_disable(reason)
        with self.lock:
            self._home_task.update({
                "state": "fault",
                "current_joint": None,
                "outputs_enabled": False,
                "last_error": str(reason),
            })
            self.arm_state = "idle"
            self._alignment = None
            self.state = "fault"
            self.last_error = str(reason)
            self._diag_note("home_fault", reason=str(reason)[:200])

    def _home_complete(self) -> None:
        self._home_disable("homing_complete")
        with self.lock:
            self._home_task.update({
                "state": "complete",
                "current_joint": None,
                "step_target_raw": None,
                "remaining_raw": 0,
                "progress_pct": 100.0,
                "outputs_enabled": False,
                "last_error": None,
            })
            self.arm_state = "idle"
            self._alignment = None
            self.last_error = None
            self._diag_note("home_complete", observed_raw=self._home_task.get("observed_raw"))

    def _home_cancelled(self) -> None:
        self._home_disable("homing_cancelled")
        with self.lock:
            self._home_task.update({
                "state": "cancelled",
                "current_joint": None,
                "outputs_enabled": False,
            })
            self.arm_state = "idle"
            self._alignment = None
            self._diag_note("home_cancelled")

    @staticmethod
    def _home_target_radians(observed: dict[str, int], asset: dict[str, Any],
                             active: str, target_raw: int) -> dict[str, float]:
        conversion = asset.get("raw_to_rad") or {}
        targets = {
            name: raw_to_rad(int(observed[name]), conversion[name])
            for name in (*ARM_JOINTS, "r_joint")
        }
        targets[active] = raw_to_rad(int(target_raw), conversion[active])
        return targets

    def _home_send(self, link: FrameLink, observed: dict[str, int],
                   asset: dict[str, Any], active: str, target_raw: int,
                   sample_seq: int) -> dict[str, Any]:
        targets_rad = self._home_target_radians(observed, asset, active, target_raw)
        return link.send_snapshot(
            arm_positions=[float(targets_rad[name]) for name in ARM_JOINTS],
            gripper_rad=float(targets_rad["r_joint"]),
            sample_seq=sample_seq,
            leader_rad=[float(observed[name]) for name in ARM_JOINTS],
            pre_clip_rad=[float(targets_rad[name]) for name in ARM_JOINTS],
            clutch=True)

    def start_home(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start an explicitly requested staged raw-pose task in this session."""
        with self.lock:
            self._validate_optional_identity(request)
            if self.state != "ready":
                raise HardwareCompositionError("session_not_ready")
            if self._home_thread is not None and self._home_thread.is_alive():
                raise HardwareCompositionError("homing_already_running")
            if self.arm_state == "following":
                raise HardwareCompositionError("pause_follow_before_homing")
            self._stop_follow_worker()
            facts = self.observer.fresh_snapshot()
            feedback = facts.get("feedback") or {}
            gate = TrustedFeedbackGate.gate(
                feedback, calibration_hash=self.bundle_hash or "",
                now_ns=self.clock_ns(), max_age_ns=500_000_000)
            if gate.get("trusted") is not True:
                raise HardwareCompositionError(
                    "homing_feedback_untrusted:" + ",".join(gate.get("reasons") or []))
            feedback_owner = feedback.get("publisher_instance")
            feedback_generation = feedback.get("publisher_generation")
            if type(feedback_owner) is not int or feedback_owner <= 0:
                # Without a publisher instance a restart is indistinguishable from a
                # sequence gap; refuse rather than fall back to the old heuristic.
                raise HardwareCompositionError("homing_feedback_publisher_missing")
            if type(feedback_generation) is not int or feedback_generation < 0:
                raise HardwareCompositionError("homing_feedback_publisher_missing")
            only_joint = request.get("only_joint")
            if only_joint is not None and (
                    not isinstance(only_joint, str)
                    or only_joint not in (*ARM_JOINTS, "r_joint")):
                raise HardwareCompositionError("homing_invalid_only_joint")
            targets = dict(HOME_RAW)
            if only_joint is not None:
                samples = feedback.get("joint_samples") or {}
                try:
                    targets = {
                        name: int(samples[name]["raw_position"])
                        for name in (*ARM_JOINTS, "r_joint")
                    }
                except (KeyError, TypeError, ValueError):
                    raise HardwareCompositionError(
                        "homing_single_axis_feedback_missing") from None
                targets[only_joint] = HOME_RAW[only_joint]
            asset = self.observer.follower_asset()
            plan = plan_home_step(
                feedback, asset, bundle_hash=self.bundle_hash or "",
                max_step_raw=self.HOME_MAX_STEP_RAW,
                tolerance_raw=self.HOME_TOLERANCE_RAW,
                targets_raw=targets)
            self._home_generation += 1
            generation = self._home_generation
            session = {
                "session_id": self.session_id,
                "session_epoch": self.session_epoch,
                "calibration_bundle_hash": self.bundle_hash,
            }
            self._home_stop = threading.Event()
            self._home_asset = asset
            self._home_targets = dict(targets)
            self._home_feedback_owner = feedback_owner
            self._home_feedback_generation = feedback_generation
            self._home_start_raw = {name: int(value) for name, value in
                                    (plan.get("observed_raw") or {}).items()}
            self._home_task = {
                **self._home_task,
                "state": "starting",
                "scope": "single_axis" if only_joint is not None else "full_pose",
                "only_joint": only_joint,
                "generation": generation,
                **session,
                "targets_raw": dict(targets),
                "tolerance_raw": self.HOME_TOLERANCE_RAW,
                "max_step_raw": self.HOME_MAX_STEP_RAW,
                "current_joint": plan.get("joint"),
                "observed_raw": plan.get("observed_raw") or {},
                "step_target_raw": plan.get("step_target_raw"),
                "remaining_raw": plan.get("remaining_raw"),
                "progress_pct": 0.0,
                "feedback_age_s": self._feedback_age(feedback),
                "feedback_sample_seq": self._feedback_sample_seq(feedback),
                "feedback_publisher_instance": feedback_owner,
                "feedback_publisher_generation": feedback_generation,
                "outputs_enabled": False,
                "started_mono_ns": time.monotonic_ns(),
                "updated_mono_ns": time.monotonic_ns(),
                "last_error": None,
            }
            if plan.get("complete") is True:
                self._home_task.update({
                    "state": "complete", "remaining_raw": 0,
                    "progress_pct": 100.0, "current_joint": None,
                    "step_target_raw": None,
                })
                self.arm_state = "idle"
                self._alignment = None
                return self.status()
            try:
                self._home_enable(session)
            except Exception:
                self._home_task.update({
                    "state": "fault", "outputs_enabled": False,
                    "last_error": "homing_output_enable_failed",
                })
                raise
            self.arm_state = "homing"
            self._alignment = None
            self._home_task["outputs_enabled"] = True
            self._home_task["state"] = "moving"
            self._home_thread = threading.Thread(
                target=self._home_worker_loop,
                args=(self._home_stop, generation),
                name="lelab-home-worker",
                daemon=True,
            )
            self._home_thread.start()
        return self.status()

    def cancel_home(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._validate_optional_identity(request)
            thread = self._home_thread
            if thread is None or not thread.is_alive():
                if self._home_task.get("state") in {"complete", "cancelled"}:
                    return self.status()
                raise HardwareCompositionError("homing_not_running")
            self._home_stop.set()
        thread.join(timeout=3.0)
        if thread.is_alive():
            self._home_fail("homing_cancel_timeout")
            raise HardwareCompositionError("homing_cancel_timeout")
        self._home_cancelled()
        return self.status()

    def _home_worker_loop(self, stop: threading.Event, generation: int) -> None:
        pending: dict[str, Any] | None = None
        initial_remaining: int | None = None
        try:
            while not stop.wait(0.05):
                with self.lock:
                    if (generation != self._home_generation
                            or self.arm_state != "homing" or self.state != "ready"):
                        return
                    router = self.router
                    driver = self.driver
                    if (router is None or not router.outputs_enabled
                            or not isinstance(driver, FrameStreamDriver)
                            or not driver.enabled or driver.link is None):
                        raise HardwareCompositionError("homing_output_not_ready")
                    link = driver.link
                if self.upper.valid(self.upper_token) is not True:
                    raise HardwareCompositionError("homing_upper_lease_lost")
                if self.car.valid(self.car_token) is not True:
                    raise HardwareCompositionError("homing_car_lease_lost")
                facts = self.observer.fresh_snapshot()
                feedback = facts.get("feedback") or {}
                gate = TrustedFeedbackGate.gate(
                    feedback, calibration_hash=self.bundle_hash or "",
                    now_ns=self.clock_ns(), max_age_ns=500_000_000)
                if gate.get("trusted") is not True:
                    raise HardwareCompositionError(
                        "homing_feedback_untrusted:" + ",".join(gate.get("reasons") or []))
                owner_now = feedback.get("publisher_instance")
                generation_now = feedback.get("publisher_generation")
                if (owner_now != self._home_feedback_owner
                        or generation_now != self._home_feedback_generation):
                    # The publisher is a new software instance. Its sequence space is
                    # unrelated to the pending segment, so waiting for the number to
                    # catch up would silently mix two generations. Fail at once; the
                    # car side has already revoked output on its own.
                    raise HardwareCompositionError(
                        f"homing_feedback_owner_changed:"
                        f"{self._home_feedback_owner}->{owner_now}:gen{generation_now}")
                sample_seq = self._feedback_sample_seq(feedback)
                if sample_seq is None:
                    raise HardwareCompositionError("homing_feedback_sequence_missing")
                receipts = link.collect()
                if link.last_error:
                    raise HardwareCompositionError("homing_stream_error:" + link.last_error)
                for receipt in receipts:
                    if receipt.get("state") == "refused":
                        raise HardwareCompositionError(
                            "homing_frame_refused:" + str(receipt.get("reason")) + ":" +
                            str(receipt.get("detail") or ""))
                with self.lock:
                    if generation != self._home_generation or self.arm_state != "homing":
                        return
                    asset = copy.deepcopy(self._home_asset or {})
                    only_joint = self._home_task.get("only_joint")
                if not isinstance(asset, dict) or not asset:
                    raise HardwareCompositionError("homing_calibration_missing")
                targets = dict(self._home_targets)
                if only_joint is not None:
                    samples = feedback.get("joint_samples") or {}
                    for name in (*ARM_JOINTS, "r_joint"):
                        raw = (samples.get(name) or {}).get("raw_position")
                        # The agent relays JSON numbers, so an integral raw arrives as
                        # a float; accept only values that are exactly integral.
                        if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                                or not float(raw).is_integer()):
                            raise HardwareCompositionError(
                                "homing_single_axis_feedback_missing")
                        if name != only_joint:
                            targets[name] = int(raw)
                plan = plan_home_step(
                    feedback, asset, bundle_hash=self.bundle_hash or "",
                    max_step_raw=self.HOME_MAX_STEP_RAW,
                    tolerance_raw=self.HOME_TOLERANCE_RAW,
                    targets_raw=targets)
                observed = plan.get("observed_raw") or {}
                if only_joint is not None:
                    # Non-selected axes are compared with the task-start scan, not with
                    # the refreshed hold target, so a slow back-drive cannot hide behind
                    # the target following the measurement.
                    for name in (*ARM_JOINTS, "r_joint"):
                        if name == only_joint:
                            continue
                        baseline = self._home_start_raw.get(name)
                        current_raw = observed.get(name)
                        if type(baseline) is not int or type(current_raw) is not int:
                            raise HardwareCompositionError("homing_drift_baseline_missing:" + name)
                        drift = int(current_raw) - baseline
                        if abs(drift) > self.HOME_DRIFT_TOLERANCE_RAW:
                            raise HardwareCompositionError(
                                f"homing_unselected_drift:{name}:start={baseline}:"
                                f"current={current_raw}")
                if plan.get("complete") is True:
                    self._home_set(
                        state="complete", observed_raw=observed,
                        progress_pct=100.0, current_joint=None,
                        step_target_raw=None, remaining_raw=0,
                        feedback_age_s=self._feedback_age(feedback),
                        feedback_sample_seq=sample_seq)
                    self._home_complete()
                    return
                if initial_remaining is None:
                    initial_remaining = max(1, sum(abs(int(value) - int(targets[name]))
                                                   for name, value in observed.items()))
                if pending is not None:
                    pending_current = observed.get(pending["joint"])
                    if type(pending_current) is not int:
                        raise HardwareCompositionError("homing_current_position_missing")
                    if sample_seq > int(pending["sample_seq"]):
                        direction = int(pending["direction"])
                        signed = (int(pending_current) - int(pending["start_raw"])) * direction
                        pending_error = abs(int(pending_current) - int(pending["target_raw"]))
                        step_delta = int(pending_current) - int(pending["last_raw"])
                        if abs(step_delta) > self.HOME_MOTION_NOISE_RAW:
                            sign = 1 if step_delta > 0 else -1
                            if (pending["move_sign"] is not None
                                    and sign != pending["move_sign"]):
                                pending["reversals"] += 1
                            pending["move_sign"] = sign
                        pending["last_raw"] = int(pending_current)
                        if signed > int(pending.get("best_signed_raw") or 0):
                            pending["best_signed_raw"] = signed
                            pending["last_progress_mono"] = time.monotonic()
                        if signed < -self.HOME_REVERSE_TOLERANCE_RAW:
                            raise HardwareCompositionError(
                                f"homing_reversed:{pending['joint']}:"
                                f"current={pending_current}:start={pending['start_raw']}:"
                                f"target={pending['target_raw']}")
                        if pending["reversals"] >= self.HOME_OSCILLATION_REVERSALS:
                            raise HardwareCompositionError(
                                f"homing_oscillating:{pending['joint']}:"
                                f"reversals={pending['reversals']}")
                        elapsed = time.monotonic() - pending["sent_mono"]
                        if ((signed > 0 or pending_error <= self.HOME_SETTLE_TOLERANCE_RAW)
                                and elapsed >= self.HOME_STEP_INTERVAL_S):
                            # Measured movement toward the target (or arrival) proves
                            # the segment executed. Discard it and replan from this
                            # fresh raw position below.
                            pending = None
                        elif (time.monotonic() - pending["last_progress_mono"]
                              > self.HOME_SETTLE_TIMEOUT_S):
                            raise HardwareCompositionError(
                                f"homing_no_progress:{pending['joint']}:"
                                f"current={pending_current}:start={pending['start_raw']}:"
                                f"target={pending['target_raw']}")
                    if pending is not None:
                        # Repeating the exact same pending target keeps the car-side
                        # watchdog alive while waiting for a new measured scan. It is
                        # not a new segment and does not advance the target.
                        pending_error = abs(int(pending_current) - int(pending["target_raw"]))
                        if (pending_error > self.HOME_SETTLE_TOLERANCE_RAW
                                and time.monotonic() - pending["last_send_mono"]
                                >= self.HOME_REPEAT_INTERVAL_S):
                            self._home_send(
                                link, observed, asset, pending["joint"],
                                int(pending["target_raw"]), sample_seq)
                            pending["last_send_mono"] = time.monotonic()
                        self._home_set(
                            current_joint=pending["joint"],
                            step_target_raw=pending["target_raw"],
                            observed_raw=observed,
                            remaining_raw=int(targets[pending["joint"]]) - int(pending_current),
                            feedback_age_s=self._feedback_age(feedback),
                            feedback_sample_seq=sample_seq)
                        continue
                current = observed.get(plan["joint"])
                if type(current) is not int:
                    raise HardwareCompositionError("homing_current_position_missing")
                remaining_now = sum(abs(int(value) - int(targets[name]))
                                    for name, value in observed.items())
                progress = round(100.0 * (1.0 - remaining_now / max(1, initial_remaining)), 2)
                step_joint = plan["joint"]
                with self.lock:
                    if (generation != self._home_generation or self.arm_state != "homing"
                            or self.driver.link is not link):
                        return
                    sent = self._home_send(
                        link, observed, asset, step_joint,
                        int(plan["step_target_raw"]), sample_seq)
                if sent.get("sent") is True:
                    step_target_raw = int(plan["step_target_raw"])
                    pending = {
                        "joint": step_joint,
                        "target_raw": step_target_raw,
                        "start_raw": int(current),
                        "direction": 1 if step_target_raw > int(current) else -1,
                        "sample_seq": sample_seq,
                        "sent_mono": time.monotonic(),
                        "last_send_mono": time.monotonic(),
                        "last_progress_mono": time.monotonic(),
                        "best_signed_raw": 0,
                        "last_raw": int(current),
                        "move_sign": None,
                        "reversals": 0,
                    }
                    self._home_set(
                        state="moving", current_joint=step_joint,
                        step_target_raw=int(plan["step_target_raw"]),
                        observed_raw=observed,
                        remaining_raw=int(targets[step_joint]) - int(current),
                        progress_pct=progress,
                    feedback_age_s=self._feedback_age(feedback),
                        feedback_sample_seq=sample_seq)
        except Exception as exc:  # noqa: BLE001 - any uncertain home loop parks output
            if not stop.is_set():
                self._home_fail(f"home_worker_failed:{type(exc).__name__}:{exc}")

    def _stop_home_worker(self) -> None:
        self._home_stop.set()
        thread = self._home_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._home_thread = None

    def _phase_process(self) -> dict[str, Any]:
        status = _http_json(self.car.agent_url, "/status", timeout=1.0)
        hybrid = status.get("hybrid") or {}
        if ((status.get("control") or {}).get("active") is not True or
                hybrid.get("session_id") != self.session_id or
                str(hybrid.get("session_epoch")) != str(self.session_epoch) or
                hybrid.get("calibration_bundle_hash") != self.bundle_hash):
            raise HardwareCompositionError("phase_control_identity_mismatch")
        process = ((status.get("licence") or {}).get("bridge") or {}).get("frame_process")
        if (not isinstance(process, dict) or isinstance(process.get("age_s"), bool) or
                not isinstance(process.get("age_s"), (int, float))):
            raise HardwareCompositionError("phase_control_confirmation_missing")
        if not 0 <= process["age_s"] <= 0.8:
            raise HardwareCompositionError("phase_control_confirmation_stale")
        return process

    def prepare_recording_phase(self) -> None:
        """Stop new output and require a later, quiescent control-process sample."""
        try:
            baseline = self._phase_process().get("ticks")
        except Exception:
            self.set_arm_state("pause_all")
            raise
        if not isinstance(baseline, int):
            self.set_arm_state("pause_all")
            raise HardwareCompositionError("phase_control_tick_missing")
        self.set_arm_state("pause_all")
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            process = self._phase_process()
            if (isinstance(process.get("ticks"), int) and process["ticks"] > baseline and
                    process.get("outputs_enabled") is False and process.get("queue_depth") == 0):
                return
            time.sleep(0.05)
        raise HardwareCompositionError("phase_control_stop_unconfirmed")

    def confirm_recording_phase(self, *, recording_id: str, episode_id: str,
                                phase: str, clock_epoch: str) -> None:
        """Wait for the control process itself to report the new context."""
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            process = self._phase_process()
            context = process.get("context") or {}
            if (process.get("outputs_enabled") is False and process.get("queue_depth") == 0 and
                    context.get("session_id") == recording_id and
                    str(context.get("session_epoch")) == str(self.session_epoch) and
                    context.get("episode_id") == episode_id and context.get("phase") == phase and
                    context.get("clock_epoch") == clock_epoch and
                    context.get("calibration_bundle_hash") == self.bundle_hash and
                    type(context.get("context_seq")) is int):
                return
            time.sleep(0.05)
        raise HardwareCompositionError("phase_context_unconfirmed")

    def acquire_session(self, request: dict[str, Any]) -> dict[str, Any]:
        result = super().acquire_session(request)
        try:
            self.observer.start_cache()
        except Exception:
            super().release_session("frame_observer_start_failed", self.session_id, self.session_epoch)
            raise
        return result

    def release_session(self, reason: str, session_id=None, session_epoch=None) -> dict[str, Any]:
        # Authenticate before touching the active worker or its observation cache.
        if session_id is not None or session_epoch is not None:
            with self.lock:
                self.validate_session_request({"session_id": session_id,
                                               "session_epoch": session_epoch})
        self._follow_stop.set()
        self._stop_home_worker()
        try:
            return super().release_session(reason, session_id, session_epoch)
        finally:
            self.observer.stop_cache()

    def _car_answer(self, path: str, body: dict, token: str | None,
                    timeout_s: float = 2.0) -> dict:
        url = getattr(self.driver, "agent_url", None)
        if not url:
            return {"error": "no_agent_url"}
        try:
            return _http_json(url, path, method="POST", body=body, token=token,
                              timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - an unconfirmed car is reported, not hidden
            return {"error": f"{type(exc).__name__}:{str(exc)[:160]}"}

    def _car_estop_state(self) -> dict:
        url = getattr(self.driver, "agent_url", None)
        if not url:
            return {}
        try:
            status = _http_json(url, "/status", timeout=1.5)
        except Exception as exc:  # noqa: BLE001
            return {"car_state_error": f"{type(exc).__name__}:{str(exc)[:120]}"}
        control = status.get("control") or {}
        document = (status.get("licence") or {}).get("bridge") or {}
        frame = document.get("frame_process") or {}
        age = frame.get("age_s")
        # The emitter reports for itself. A missing or stale report is not evidence of a
        # clear state - absence has to read as unknown, never as "no fault".
        witness = ("fresh" if frame and isinstance(age, (int, float)) and age <= 2.0
                   else ("stale" if frame else "absent"))
        return {"agent_estop_latched": control.get("estop_latched"),
                "agent_frame_outputs_enabled": (status.get("licence") or {}).get(
                    "frame_outputs_enabled"),
                "process_outputs_enabled": frame.get("outputs_enabled"),
                "process_latched_fault": frame.get("latched_fault"),
                "process_reengage_required": frame.get("reengage_required"),
                "process_witness": witness,
                "clear_still_queued": document.get("clear_pending"),
                "process_age_s": age, "queue_depth": frame.get("queue_depth")}

    @staticmethod
    def _emitter_clear_seen(view: dict) -> bool:
        """True only when the emitter itself says, freshly, that it holds no latched stop."""
        return (view.get("process_witness") == "fresh"
                and view.get("process_latched_fault") is None
                and view.get("clear_still_queued") is not True)

    def emergency_stop(self, reason="estop"):
        """Stop here, and make the two car layers latch: the agent owns the permission and
        the control process owns the emission. A closed local socket is neither of those.

        Each stage is reported separately. Without a fresh answer from the car the stage is
        ``unconfirmed`` - it is never written as stopped, and nothing here is a claim about
        a physical stop.
        """
        started = time.monotonic()
        self._follow_stop.set()
        self._home_stop.set()
        if self._home_task.get("state") in {"starting", "moving"}:
            # The emergency path must not wait for the facade lock; this small
            # dictionary update is advisory status only and cannot grant motion.
            self._home_task.update({
                "state": "stopped",
                "current_joint": None,
                "outputs_enabled": False,
                "last_error": "operator_estop",
                "updated_mono_ns": time.monotonic_ns(),
            })
        stage: dict = {"requested_reason": str(reason or "estop")}
        link = self.driver.link if isinstance(self.driver, FrameStreamDriver) else None
        if link is not None:
            link.stop(str(reason))            # preempt ahead of anything still queued
            stage["car_preempt_send_failed"] = link.last_error and str(
                link.last_error).startswith("estop_send_failed:")
            self.driver.abort()
        else:
            stage["car_preempt_send_failed"] = None
            stage["detail_no_link"] = "no frame link was open; nothing was queued to stop"
        stage["local_stopped_s"] = round(time.monotonic() - started, 3)
        answer = self._car_answer("/control/estop", {"reason": f"frame_facade:{reason}"},
                                  self.car_token)
        stage["agent_latch"] = ("confirmed" if answer.get("estop") is True else
                                ("unconfirmed" if "error" not in answer else "failed"))
        if "error" in answer:
            stage["agent_latch_error"] = answer["error"]
        deadline = time.monotonic() + 2.0
        # When no preempt could go out, the emitter never sees this stop and its own latch
        # stays whatever it was - waiting for it would only add latency to a real stop.
        wait_for_emitter = link is not None and stage["car_preempt_send_failed"] is False
        seen = self._car_estop_state()
        while time.monotonic() < deadline and (
                seen.get("agent_estop_latched") is not True
                or (wait_for_emitter and seen.get("process_latched_fault") is None)):
            time.sleep(0.1)
            seen = self._car_estop_state()
        stage["process_latch"] = (
            "unknown" if seen.get("process_witness") != "fresh" else
            ("latched" if seen.get("process_latched_fault") else "clear"))
        stage.update(seen)
        stage["confirmed_s"] = round(time.monotonic() - started, 3)
        self._stop_stage = stage
        router = self.router
        if router is not None:
            router.outputs_enabled = False
            router.permits.revoke("base", "arm", "gripper")
            router.queue.clear()
        out = super().emergency_stop(reason)
        if isinstance(out, dict):
            out["car_stop"] = stage
        return out

    def _car_clear_estop(self, owner: str | None = None) -> dict:
        """Lift the car-side stop. Never grants motion: outputs stay disabled and the next
        session still has to bind an epoch and be explicitly enabled."""
        token = self.car_token
        # A fresh upper process has no acquired car lease after its old token expired,
        # but the browser's stable client id is still the identity that raised the
        # stop. Pass it through the existing prior-owner recovery branch so an operator
        # can lift the latch without creating an anonymous clear path.
        client_id = owner or getattr(self.car, "acquired_client_id", None)

        def clear(candidate: str | None, clear_token: str | None) -> dict:
            body = {"client_id": candidate}
            answer = self._car_answer("/control/clear_estop", body, clear_token)
            if "error" in answer:
                # The lease is gone, so no token can work any more; the agent only
                # accepts the prior owner in that case. Retry without the dead token.
                answer = self._car_answer("/control/clear_estop", body, None)
            return answer

        answer = clear(client_id, token)
        if "error" in answer:
            # A new upper process has no local owner, and the browser identity can
            # differ from the car client on a reused/manual session. The agent's own
            # bounded event record identifies the client that latched the current
            # stop, so use that recorded identity through the existing prior-owner
            # branch. This does not add a tokenless motion path.
            recorded_owner = self._car_estop_owner_hint()
            if recorded_owner and recorded_owner != client_id:
                client_id = recorded_owner
                answer = clear(recorded_owner, token)
        if "error" in answer and token is None:
            # A web stop may be raised after its token expired. Acquire a short-lived
            # control identity that grants no output, use it only to clear the latch,
            # and release it immediately. This uses the same agent control lease.
            temporary_owner = client_id or "operator-clear-estop"
            acquired = self._car_answer(
                "/control/acquire",
                {"client_id": temporary_owner, "ttl_s": 30, "recovery": True},
                None,
            )
            temporary_token = acquired.get("token")
            if isinstance(temporary_token, str) and temporary_token:
                try:
                    answer = clear(temporary_owner, temporary_token)
                finally:
                    self._car_answer("/control/release", {}, temporary_token)
        if "error" in answer:
            return {"car_clear": "failed", "car_clear_error": answer["error"]}
        deadline = time.monotonic() + 2.0
        seen = self._car_estop_state()
        # Both car layers have to answer. The agent lifting its latch only proves the
        # permission authority agreed; the emitter holds a latch of its own and refuses
        # to admit or emit until it has seen a clearance newer than what it latched on.
        while time.monotonic() < deadline and (
                seen.get("agent_estop_latched") is True
                or not self._emitter_clear_seen(seen)):
            time.sleep(0.1)
            seen = self._car_estop_state()
        agent_clear = (answer.get("estop") is False
                       and seen.get("agent_estop_latched") is not True)
        if not agent_clear:
            state = "failed"
        elif not self._emitter_clear_seen(seen):
            state = "pending_or_failed"
        else:
            state = "confirmed"
        return {"car_clear": state,
                "authorised_by": answer.get("authorised_by"),
                "clear_estop_sent": answer.get("clear_estop_sent"),
                "announcements": answer.get("announcements"), **seen}

    def _car_estop_owner_hint(self) -> str | None:
        """Return the car's recorded client of the current latch.

        This is recovery evidence exposed by the same loopback-only agent that owns
        the latch. It does not grant output permission; the clear route still verifies
        the identity against ``estop_owner_client``.
        """
        url = getattr(self.driver, "agent_url", None)
        if not url:
            return None
        try:
            status = _http_json(url, "/status?full=1", timeout=1.5)
        except Exception:  # noqa: BLE001 - recovery simply fails closed
            return None
        events = status.get("events")
        if not isinstance(events, list):
            return None
        latest_estop = None
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if isinstance(event, dict) and event.get("kind") == "estop":
                latest_estop = index
                break
        if latest_estop is None:
            return None
        for event in reversed(events[:latest_estop]):
            if not isinstance(event, dict) or event.get("kind") != "control_acquired":
                continue
            client_id = event.get("client_id")
            if isinstance(client_id, str) and client_id:
                return client_id
        return None

    def clear_estop_unbound(self, owner: str | None = None):
        """Clear the local facade and the car-side latch as one explicit action."""
        with self.lock:
            if (self.session_id is not None or self.upper_token is not None
                    or self.car_token is not None):
                raise RuntimeError("session_identity_required")
        car_clear = self._car_clear_estop(owner)
        out = super().clear_estop_unbound()
        if isinstance(out, dict):
            out["car_clear"] = car_clear
        return out

    def _follow_fault_detail(self, reason: str, exc: BaseException,
                             facts: dict[str, Any], gate: dict[str, Any],
                             link: Any) -> dict[str, Any]:
        """Capture the first protected failure before any stop changes the view."""
        feedback = facts.get("feedback") if isinstance(facts, dict) else {}
        feedback = feedback if isinstance(feedback, dict) else {}
        process = facts.get("frame_process") if isinstance(facts, dict) else {}
        process = process if isinstance(process, dict) else {}
        samples = []
        for sample in feedback.get("samples") or []:
            if not isinstance(sample, dict):
                continue
            samples.append({
                name: sample.get(name) for name in (
                    "name", "servo_id", "sample_seq", "age_s", "age_ns",
                    "received_mono_ns", "sample_time_ns", "clock_domain",
                    "valid", "error", "source_kind", "raw_position")
            })
        cache_state = getattr(self.observer, "cache_state", None)
        try:
            cache = cache_state() if callable(cache_state) else None
        except Exception as cache_exc:  # noqa: BLE001 - preserve the primary failure
            cache = {"error": f"{type(cache_exc).__name__}:{cache_exc}"}
        return {
            "reason": reason,
            "exception_type": type(exc).__name__,
            "clock_ns": self.clock_ns(),
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "calibration_bundle_hash": self.bundle_hash,
            "feedback": {
                "valid": feedback.get("valid"),
                "trusted": feedback.get("trusted"),
                "scan_span_s": feedback.get("scan_span_s"),
                "publisher_instance": feedback.get("publisher_instance"),
                "publisher_generation": feedback.get("publisher_generation"),
                "samples": samples,
            },
            "gate": copy.deepcopy(gate),
            "observation_cache": cache,
            "frame_process": {
                key: process.get(key) for key in (
                    "ticks", "outputs_enabled", "latched_fault", "paused",
                    "last_refusal", "queue_depth", "age_s",
                    "effective_emit_hz")
            },
            "frame_link": (link.state()
                           if link is not None and callable(getattr(link, "state", None))
                           else None),
        }

    @classmethod
    def _feedback_gate_for(cls, facts: dict[str, Any], calibration_hash: str,
                           now_ns: int) -> dict[str, Any]:
        return TrustedFeedbackGate.gate(
            facts.get("feedback"), calibration_hash=calibration_hash,
            now_ns=now_ns, max_age_ns=int(cls.FEEDBACK_MAX_AGE_S * 1_000_000_000))

    @staticmethod
    def _transient_feedback_gap(facts: dict[str, Any],
                                gate: dict[str, Any]) -> bool:
        reasons = gate.get("reasons") or []
        if not reasons:
            return False
        allowed = {"sample_invalid", "stale_or_invalid_timestamp"}
        if any(str(reason).split(":", 1)[-1] not in allowed for reason in reasons):
            return False
        samples = (facts.get("feedback") or {}).get("samples") or []
        if len(samples) != 6:
            return False
        for sample in samples:
            error = str(sample.get("error") or "")
            age = sample.get("age_s")
            if ("sample_stale" not in error and "feedback_stale" not in error
                    and not (isinstance(age, (int, float))
                             and age >= self.FEEDBACK_MAX_AGE_S)):
                return False
        return True

    def _recover_transient_feedback(self, facts: dict[str, Any],
                                    gate: dict[str, Any],
                                    stop: threading.Event):
        """Wait for a new scan when a rejection is only the scan-age boundary.

        The stale facts are never passed to the mapper or frame sender. If the
        recovery window ends without a trusted fresh scan, the caller still
        faults and stops the session.
        """
        if not self._transient_feedback_gap(facts, gate):
            return facts, gate
        deadline = time.monotonic() + self.FEEDBACK_RECOVERY_WINDOW_S
        while time.monotonic() < deadline and not stop.wait(self.FEEDBACK_RETRY_POLL_S):
            try:
                candidate = self.observer.fresh_snapshot(
                    max_cache_age_s=0.05, timeout_s=0.15)
            except Exception:  # noqa: BLE001 - the final gate is authoritative
                continue
            candidate_gate = self._feedback_gate_for(
                candidate, self.bundle_hash or "", self.clock_ns())
            if candidate_gate.get("trusted") is True:
                return candidate, candidate_gate
            facts, gate = candidate, candidate_gate
            if not self._transient_feedback_gap(facts, gate):
                break
        return facts, gate



    def _follow_worker_loop(self, stop: threading.Event) -> None:
        last_sample = -1
        last_admission = time.monotonic()
        admitted_once = False
        next_tick = last_admission
        period = 1.0 / max(1.0, float(self.FOLLOW_HZ))
        while not stop.is_set():
            next_tick += period
            facts: dict[str, Any] = {}
            gate: dict[str, Any] = {}
            link = None
            try:
                with self.lock:
                    if stop.is_set() or self.state != "ready" or self.arm_state != "following":
                        break
                    upper_ok = self.upper.valid(self.upper_token)
                    car_ok = self.car.valid(self.car_token)
                    if not upper_ok or not car_ok:
                        raise HardwareCompositionError(
                            "frame_lease_lost:" + self._lease_reason(upper_ok, car_ok))
                    if self.router is None or not self.router.outputs_enabled:
                        raise HardwareCompositionError("frame_router_not_enabled")
                    permits = self.router.permits.snapshot(self.session_epoch).get("granted", {})
                    if not all(permits.get(name) for name in ("arm", "gripper")):
                        raise HardwareCompositionError("frame_channel_not_permitted")
                    driver = self.driver
                    if not isinstance(driver, FrameStreamDriver) or not driver.enabled or driver.link is None:
                        raise HardwareCompositionError("frame_driver_not_enabled")
                    link = driver.link
                facts = self.observer.fresh_snapshot()
                gate = self._feedback_gate_for(
                    facts, self.bundle_hash or "", self.clock_ns())
                facts, gate = self._recover_transient_feedback(facts, gate, stop)
                if not gate.get("trusted"):
                    raise HardwareCompositionError("follow_feedback_untrusted:" +
                                                   ",".join(gate.get("reasons") or []))
                leader = facts.get("leader") or {}
                if not leader.get("healthy"):
                    raise HardwareCompositionError("leader_unhealthy_or_stale")
                receipts = link.collect()
                if link.last_error:
                    raise HardwareCompositionError("frame_stream_error:" + link.last_error)
                for receipt in receipts:
                    if receipt.get("state") == "admitted":
                        last_admission = time.monotonic()
                        admitted_once = True
                    elif receipt.get("state") == "refused":
                        reason = str(receipt.get("reason"))
                        detail = str(receipt.get("detail") or "")
                        # The agent's newly enabled permission takes one IPC
                        # heartbeat to reach the control process. This bounded
                        # startup refusal grants no output and never refreshes the
                        # admission deadline.
                        if (admitted_once or reason != "channel_refused" or
                                "licence_outputs_disabled" not in detail):
                            raise HardwareCompositionError("frame_refused:" + reason + ":" + detail)
                if time.monotonic() - last_admission > 0.5:
                    raise HardwareCompositionError("frame_admission_timeout")
                preview = self.observer.map_sample(facts)
                facts["preview"] = preview
                targets = preview.get("targets")
                gripper = preview.get("gripper") or {}
                sequence = leader.get("reads")
                if (preview.get("available") is not True or not isinstance(targets, list)
                        or not isinstance(sequence, int) or isinstance(sequence, bool)):
                    raise HardwareCompositionError("frame_preview_or_sample_missing")
                by_joint = {item.get("joint"): item for item in targets if isinstance(item, dict)}
                if set(by_joint) != set(ARM_JOINTS) or not isinstance(gripper.get("target_rad"), (int, float)):
                    raise HardwareCompositionError("frame_targets_incomplete")
                if sequence > last_sample:
                    leader_rad = leader.get("positions_rad") or {}
                    names = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
                    with self.lock:
                        if stop.is_set() or self.arm_state != "following" or driver.link is not link:
                            break
                        result = link.send_snapshot(
                            arm_positions=[float(by_joint[name]["target_rad"]) for name in ARM_JOINTS],
                            gripper_rad=float(gripper["target_rad"]), sample_seq=sequence,
                            leader_rad=[float(leader_rad[name]) for name in names],
                            pre_clip_rad=[float(by_joint[name]["pre_clip_rad"])
                                          for name in ARM_JOINTS], clutch=True)
                    if result.get("sent"):
                        last_sample = sequence
                        self._follow_commands += 1
                with self.lock:
                    self._facts, self._feedback_gate = facts, gate
            except Exception as exc:  # noqa: BLE001 - an uncertain stream parks the session
                if not stop.is_set():
                    reason = f"frame_follow_failed:{type(exc).__name__}:{exc}"
                    self._follow_fail(
                        reason,
                        self._follow_fault_detail(reason, exc, facts, gate, link))
                break
            if stop.wait(max(0.0, next_tick - time.monotonic())):
                break
            if time.monotonic() > next_tick + period:
                next_tick = time.monotonic()  # never replay missed ticks

    def status(self) -> dict[str, Any]:
        out = super().status()
        follow_runtime = out.setdefault("follow_runtime", {})
        if isinstance(follow_runtime, dict):
            follow_runtime["rate_hz"] = self.FOLLOW_HZ
        driver = self.driver
        out["transport"] = "frame_stream_v1"
        out["frame_link"] = driver.link.state() if isinstance(driver, FrameStreamDriver) and driver.link else None
        out["stop_stage"] = getattr(self, "_stop_stage", None)
        out["car_clear"] = getattr(self, "_car_clear_last", None)
        homing = self.home_status()
        process = out.get("frame_process") or {}
        # The car emitter is the only place that can prove a per-ID torque load; the
        # upper side relays its requested/verified/failed rows without re-labelling.
        homing["torque"] = {
            "state": process.get("homing_torque_state"),
            "enabled": process.get("homing_torque_enabled"),
            "results": process.get("homing_torque_results"),
            "error": process.get("homing_torque_error"),
        }
        out["homing"] = homing
        return out


def build_stream_hardware_facade(agent_url: str) -> FrameStreamHardwareFacade:
    """Build an inert opt-in composition; hardware opens only on session acquire."""
    store = HybridCalibrationStore(_calibration_root())
    identities = {
        "leader": os.environ.get("LELAB_HYBRID_LEADER_ID", "landerpi-so101-leader-v1"),
        "follower": os.environ.get("LELAB_HYBRID_FOLLOWER_ID", "landerpi-follower-v1"),
        "mapping": os.environ.get("LELAB_HYBRID_MAPPING_ID", "landerpi-so101-mapping-v1"),
    }
    upper = UpperLeaderLease(agent_url=agent_url, store=store, leader_identity=identities["leader"])
    expected_profile = os.environ.get("LELAB_HYBRID_CAR_PROFILE", "hardware_hybrid_manual_v1")
    car = FrameCarControlLease(agent_url, expected_profile=expected_profile)
    runtime_step_cap = (
        0.10 if expected_profile in {
            "hardware_hybrid_arm_trial_v1", "hardware_hybrid_arm2_trial_v1",
            "hardware_hybrid_arm3_trial_v1", "hardware_hybrid_arm4_trial_v1",
            "hardware_hybrid_arm5_trial_v1"}
        else 0.15 if expected_profile == "hardware_hybrid_follow_v1"
        else None)
    filter_alpha_override = (
        float(os.environ.get("LELAB_HYBRID_FILTER_ALPHA", "0.60"))
        if expected_profile == "hardware_hybrid_follow_v1" else None)
    deadband_rad_override = (
        float(os.environ.get("LELAB_HYBRID_DEADBAND_RAD", "0.004"))
        if expected_profile == "hardware_hybrid_follow_v1" else None)
    if filter_alpha_override is not None and not 0.05 <= filter_alpha_override <= 1.0:
        raise ValueError("LELAB_HYBRID_FILTER_ALPHA must be in [0.05, 1.0]")
    if deadband_rad_override is not None and not 0.0 <= deadband_rad_override <= 0.02:
        raise ValueError("LELAB_HYBRID_DEADBAND_RAD must be in [0.0, 0.02]")
    observer = StreamHardwareObserver(upper=upper, agent_url=agent_url, store=store,
        leader_identity=identities["leader"], follower_identity=identities["follower"],
        mapping_identity=identities["mapping"],
        trial_max_step_rad_s=runtime_step_cap,
        filter_alpha_override=filter_alpha_override,
        deadband_rad_override=deadband_rad_override)
    car.observer = observer
    facade = FrameStreamHardwareFacade(upper=upper, car=car, observer=observer, calibration_store=store)
    facade.bind_driver(FrameStreamDriver(car, observer, agent_url, host="127.0.0.1",
        port=int(os.environ.get("LELAB_HYBRID_FRAME_PORT", "18122"))))
    facade.hardware_identities = identities
    return facade
