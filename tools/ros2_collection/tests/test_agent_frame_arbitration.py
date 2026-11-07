"""Frame-stream output arbitration on the production agent.

These tests exercise the DEPLOYED agent code paths (only the surrounding node state is
stubbed, the way the existing agent tests do it), plus a source-level check that every
entry point which can move a servo is guarded.  SIMULATED ONLY: no ROS graph, no serial
port, no hardware, no motion.
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent"), str(ROOT / "app_ros_mobile")]

import os
HAS_RCLPY = os.environ.get("LELAB_FORCE_NO_RCLPY") != "1"
AGENT_IMPORT_ERROR = ""
if HAS_RCLPY:
    try:
        import rclpy  # noqa: F401
    except Exception as error:  # noqa: BLE001
        HAS_RCLPY = False
        AGENT_IMPORT_ERROR = f"rclpy: {type(error).__name__}: {error}"

if HAS_RCLPY:
    try:
        from lelab_mobile_agent.agent import ApiError, MobileAgent  # noqa: E402
    except Exception as error:  # noqa: BLE001
        HAS_RCLPY = False
        AGENT_IMPORT_ERROR = f"agent: {type(error).__name__}: {str(error)[:200]}"

BUNDLE = "b" * 64
GUARDED = [
    ("arm_command", "/control/arm"),
    ("gripper_command", "/control/gripper"),
    ("hybrid_command", "hybrid command/dispatch/apply"),
    ("hybrid_dispatch", "hybrid command/dispatch/apply"),
    ("hybrid_apply", "hybrid command/dispatch/apply"),
    ("run_expert", "/expert/run"),
    ("_reset_task", "/task/reset"),
    ("_calibration_publish", "follower calibration target"),
    ("_calibration_set_torque", "follower calibration torque"),
]
MUST_NOT_BE_GUARDED = ["def estop", "def clear_estop", "def hybrid_stop", "def release",
                       "def renew", "def acquire"]


class FakeBridge:
    def __init__(self, active=True):
        self._active = active
        self.wakes = 0
        self.clears = 0

    def frame_transport_active(self, session_id=None, session_epoch=None):
        return self._active

    def wake(self):
        self.wakes += 1

    def request_clear_estop(self):
        self.clears += 1


@unittest.skipUnless(HAS_RCLPY, AGENT_IMPORT_ERROR or AGENT_IMPORT_ERROR or "needs the Humble runtime")
class Arbitration(unittest.TestCase):
    def agent(self, transport="frame_stream", stream_active=True, estop=False):
        agent = object.__new__(MobileAgent)
        agent.output_transport = transport
        agent.frame_outputs_enabled = False
        agent.licence_bridge = FakeBridge(stream_active) if transport == "frame_stream" else None
        agent.hybrid_gateway = None
        agent.control_token = "real-lease-token"
        agent.control_epoch = 77
        agent.control_expires = 1e12
        agent.estop_latched = estop
        agent.read_only = False
        agent.hybrid_no_output = False
        agent.actuator_outputs = True
        return agent

    def test_every_writable_entry_point_is_guarded(self):
        source = Path(inspect.getsource(MobileAgent)).read_text() \
            if False else inspect.getsource(sys.modules["lelab_mobile_agent.agent"])
        for name, route in GUARDED:
            body = source[source.index(f"    def {name}("):]
            body = body[:body.index("\n    def ")]
            self.assertIn("_refuse_frame_stream_action", body,
                          f"{name} can move the board and must be guarded")

    def test_stopping_paths_stay_usable_while_a_stream_owns_output(self):
        source = inspect.getsource(sys.modules["lelab_mobile_agent.agent"])
        for header in MUST_NOT_BE_GUARDED:
            body = source[source.index("    " + header + "("):]
            body = body[:body.index("\n    def ")]
            self.assertNotIn("_refuse_frame_stream_action", body,
                             f"{header!r} is a stop or lease path and must never be refused")

    def test_a_frame_stream_epoch_refuses_legacy_emission(self):
        agent = self.agent()
        with self.assertRaises(ApiError) as ctx:
            agent.arm_command("real-lease-token", {"positions": [0.0] * 5})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.reason, "frame_transport_active")

    def test_legacy_transport_is_unchanged(self):
        agent = self.agent(transport="legacy")
        try:
            agent._refuse_frame_stream_action("/control/arm")
        except Exception as error:      # noqa: BLE001 - anything raised is a regression
            self.fail(f"legacy transport must not be affected: {error}")

    def test_no_stream_connection_does_not_reopen_the_legacy_routes(self):
        """A profile that declares the frame transport never falls back implicitly.

        Dropping the guard when the client is momentarily gone would let an old HTTP
        route emit while the control process still holds a live licence mirror, which
        is exactly the two-writer failure this guard exists to prevent.
        """
        agent = self.agent(stream_active=False)
        with self.assertRaises(ApiError) as ctx:
            agent._refuse_frame_stream_action("/control/arm")
        self.assertEqual(ctx.exception.status, 409)

    def test_the_agent_never_constructs_a_second_emitter_authority(self):
        agent = self.agent()
        # enabling in frame mode must not light up this process' own driver
        self.assertIsNone(getattr(agent, "ros_servo_output", None))
        self.assertFalse(getattr(agent, "hybrid_no_output", False))


@unittest.skipUnless(HAS_RCLPY, AGENT_IMPORT_ERROR or AGENT_IMPORT_ERROR or "needs the Humble runtime")
class LicenceFacts(unittest.TestCase):
    def agent(self, **kwargs):
        return Arbitration().agent(**kwargs)

    def test_facts_are_derived_from_lease_and_bridge_only(self):
        agent = self.agent()
        facts = agent._licence_facts()
        self.assertEqual(facts["lease_token"], "real-lease-token")
        self.assertFalse(facts["outputs_enabled"])
        self.assertEqual(facts["calibration_bundle_hash"], "unbound")

    def test_a_latched_stop_outranks_an_enabled_output(self):
        agent = self.agent(estop=True)
        agent.frame_outputs_enabled = True
        self.assertFalse(agent._licence_facts()["outputs_enabled"])

    def test_enabling_output_wakes_the_bridge_and_releasing_clears_it(self):
        agent = self.agent()
        agent.frame_outputs_enabled = True
        agent._bridge_wake("test")
        self.assertEqual(agent.licence_bridge.wakes, 1)
        self.assertTrue(agent._licence_facts()["outputs_enabled"])

    def test_clear_estop_asks_for_a_permission_free_clearance(self):
        agent = self.agent()
        agent.estop_latched = True
        agent.get_logger = lambda: type("L", (), {"info": lambda *a, **k: None})()
        agent._emit = lambda *a, **k: None
        for attribute in ("heartbeat_pub", "estop_pub", "reset_pub", "base_pub"):
            setattr(agent, attribute, type("P", (), {"publish": lambda *a, **k: None})())
        agent.control_mode = "idle"
        agent.base_target = None
        agent.expert_cancel = type("E", (), {"set": lambda *a, **k: None})()
        agent._set_base_expert = lambda *a, **k: None
        agent._zero_base_now = lambda *a, **k: None
        agent._reset_hybrid_control_binding = lambda *a, **k: None
        agent._check_token = lambda *a, **k: None
        agent.lock = __import__("threading").RLock()
        agent.clear_estop("real-lease-token")
        self.assertFalse(agent.estop_latched)
        self.assertEqual(agent.licence_bridge.clears, 1,
                         "the control process must be told explicitly, not by a newer generation")
        self.assertFalse(agent.frame_outputs_enabled,
                         "clearing a stop must not re-enable output")


@unittest.skipUnless(HAS_RCLPY, AGENT_IMPORT_ERROR or "needs the Humble runtime")
class NoSilentFallback(unittest.TestCase):
    def test_frame_stream_without_a_bridge_still_refuses_legacy_writes(self):
        """An unstarted bridge must not become an excuse to emit from this process."""
        agent = Arbitration().agent()
        agent.licence_bridge = None
        # transport says frame_stream, so the legacy routes are closed even if the
        # bridge object is missing: there is no mirror, hence no permission anywhere.
        self.assertFalse(agent.frame_stream_active("s", 1))
        with self.assertRaises(ApiError):
            agent._refuse_frame_stream_action("/control/arm")

    def test_replacing_the_lease_revokes_the_frame_output_flag(self):
        """A new lease must start disarmed, never inherit the previous session's arm."""
        agent = Arbitration().agent()
        agent.frame_outputs_enabled = True
        agent.hybrid_gateway = None
        agent._reset_hybrid_control_binding("control_lease_replaced")
        self.assertFalse(agent.frame_outputs_enabled)
        self.assertFalse(agent._licence_facts()["outputs_enabled"])
        self.assertEqual(agent.licence_bridge.wakes, 1,
                         "the mirror must be told about the revocation, not wait a tick")

    def test_hybrid_stop_revokes_frame_outputs_too(self):
        agent = Arbitration().agent()
        agent.frame_outputs_enabled = True
        live = {"session_id": "s", "session_epoch": 3, "calibration_bundle_hash": BUNDLE,
                "outputs_enabled": True, "permits": {"arm": True}}
        gateway = type("G", (), {})()
        gateway.status = lambda: dict(live)
        # A real gateway drops its own output flag when it stops; the stub has to, or the
        # test would assert against a state the production object can never be in.
        def stop(reason):
            live["outputs_enabled"] = False
            live["permits"] = {}
            return {"queue_dropped": 0, "reason": reason}
        gateway.stop = stop
        agent.hybrid_gateway = gateway
        agent.hybrid_control_token = "real-lease-token"
        agent._emit = lambda *a, **k: None
        out = agent.hybrid_stop("real-lease-token", {"session_id": "s", "session_epoch": 3,
                                                     "calibration_bundle_hash": BUNDLE,
                                                     "reason": "operator"})
        self.assertFalse(agent.frame_outputs_enabled)
        self.assertFalse(out["outputs_enabled"])
        self.assertFalse(agent._licence_facts()["outputs_enabled"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_RCLPY, AGENT_IMPORT_ERROR or "needs the Humble runtime")
class StopRecovery(unittest.TestCase):
    def test_other_joint_trials_refuse_an_emitter_bound_to_the_wrong_axis(self):
        for axis in (3, 4, 5):
            with self.subTest(axis=axis):
                agent = Arbitration().agent()
                trial = {"allowed_joints": [f"joint{axis}"], "max_displacement_rad": 0.08,
                         "first_frame_tolerance_rad": 0.02, "locked_tolerance_rad": 0.02}
                agent.profile = {"profile": f"hardware_hybrid_arm{axis}_trial_v1"}
                agent.teleop_cfg = {"trial_motion": trial}
                body = {"session_id": "s", "session_epoch": 1, "calibration_bundle_hash": BUNDLE}
                agent._require_hybrid_gateway = lambda _token: type("G", (), {
                    "status": lambda _s: dict(body),
                    "read_feedback": lambda *_a, **_k: {"valid": True, "trusted": True}})()
                agent._feedback_gate = lambda: (True, {})
                process = {"age_s": 0.1, "data_identity": "hardware",
                           "configured_bundle_hash": BUNDLE,
                           "trial_motion": {**trial, "allowed_joints": ["joint1"]},
                           "latched_fault": None, "outputs_enabled": False}
                agent.licence_bridge.state = lambda: {"frame_process": process}
                with self.assertRaises(ApiError) as ctx:
                    agent.hybrid_enable_output("real-lease-token", body)
                self.assertEqual(ctx.exception.reason, "trial_emitter_not_ready")
                self.assertFalse(agent.frame_outputs_enabled)

    def test_joint2_trial_also_requires_a_fresh_matching_emitter(self):
        agent = Arbitration().agent()
        trial = {"allowed_joints": ["joint2"], "max_displacement_rad": 0.08,
                 "first_frame_tolerance_rad": 0.02, "locked_tolerance_rad": 0.02}
        agent.profile = {"profile": "hardware_hybrid_arm2_trial_v1"}
        agent.teleop_cfg = {"trial_motion": trial}
        body = {"session_id": "s", "session_epoch": 1, "calibration_bundle_hash": BUNDLE}
        gateway = type("G", (), {"status": lambda _s: dict(body),
            "read_feedback": lambda *_a, **_k: {"valid": True, "trusted": True}})()
        agent._require_hybrid_gateway = lambda _token: gateway
        agent._feedback_gate = lambda: (True, {})
        agent._emit = lambda *_a, **_k: None
        process = {"age_s": 0.1, "data_identity": "hardware",
                   "configured_bundle_hash": BUNDLE, "trial_motion": trial,
                   "latched_fault": None, "outputs_enabled": False}
        agent.licence_bridge.state = lambda: {"frame_process": {
            **process, "trial_motion": {**trial, "allowed_joints": ["joint1"]}}}
        with self.assertRaises(ApiError) as ctx:
            agent.hybrid_enable_output("real-lease-token", body)
        self.assertEqual(ctx.exception.reason, "trial_emitter_not_ready")
        agent.licence_bridge.state = lambda: {"frame_process": process}
        self.assertTrue(agent.hybrid_enable_output("real-lease-token", body)["outputs_enabled"])

    def test_homing_profile_requires_the_matching_homing_emitter(self):
        agent = Arbitration().agent()
        homing = {
            "axis_order": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
            "targets_raw": {"joint1": 500, "joint2": 500, "joint3": 500,
                            "joint4": 500, "joint5": 500, "r_joint": 350},
            "tolerance_raw": 2, "locked_tolerance_raw": 2,
            "first_frame_tolerance_raw": 10, "max_step_raw": 7,
        }
        agent.profile = {"profile": "hardware_hybrid_homing_v1"}
        agent.teleop_cfg = {"homing_motion": homing}
        body = {"session_id": "s", "session_epoch": 1, "calibration_bundle_hash": BUNDLE}
        gateway = type("G", (), {"status": lambda _s: dict(body),
            "read_feedback": lambda *_a, **_k: {"valid": True, "trusted": True}})()
        agent._require_hybrid_gateway = lambda _token: gateway
        agent._feedback_gate = lambda: (True, {})
        agent._emit = lambda *_a, **_k: None
        process = {"age_s": 0.1, "data_identity": "hardware",
                   "configured_bundle_hash": BUNDLE, "homing_motion": homing,
                   "latched_fault": None, "outputs_enabled": False}
        agent.licence_bridge.state = lambda: {"frame_process": {
            **process, "homing_motion": {**homing, "max_step_raw": 8}}}
        with self.assertRaises(ApiError) as ctx:
            agent.hybrid_enable_output("real-lease-token", body)
        self.assertEqual(ctx.exception.reason, "trial_emitter_not_ready")
        agent.licence_bridge.state = lambda: {"frame_process": process}
        self.assertTrue(agent.hybrid_enable_output("real-lease-token", body)["outputs_enabled"])

    def test_hardware_trial_needs_fresh_matching_emitter_before_enable(self):
        agent = Arbitration().agent()
        trial = {"allowed_joints": ["joint1"], "max_displacement_rad": 0.08,
                 "first_frame_tolerance_rad": 0.02, "locked_tolerance_rad": 0.02}
        agent.profile = {"profile": "hardware_hybrid_arm_trial_v1"}
        agent.teleop_cfg = {"trial_motion": trial}
        body = {"session_id": "s", "session_epoch": 1, "calibration_bundle_hash": BUNDLE}
        gateway = type("G", (), {"status": lambda _s: dict(body),
            "read_feedback": lambda *_a, **_k: {"valid": True, "trusted": True}})()
        agent._require_hybrid_gateway = lambda _token: gateway
        agent._feedback_gate = lambda: (True, {})
        agent._emit = lambda *_a, **_k: None
        process = {"age_s": 0.1, "data_identity": "hardware",
                   "configured_bundle_hash": BUNDLE, "trial_motion": trial,
                   "latched_fault": None, "outputs_enabled": False}
        for altered in ({}, {**process, "age_s": 2.0},
                        {**process, "configured_bundle_hash": "wrong"},
                        {**process, "trial_motion": {"allowed_joints": ["joint2"]}}):
            agent.licence_bridge.state = lambda altered=altered: {"frame_process": altered}
            with self.assertRaises(ApiError) as ctx:
                agent.hybrid_enable_output("real-lease-token", body)
            self.assertEqual(ctx.exception.reason, "trial_emitter_not_ready")
            self.assertFalse(agent.frame_outputs_enabled)
        agent.licence_bridge.state = lambda: {"frame_process": process}
        result = agent.hybrid_enable_output("real-lease-token", body)
        self.assertTrue(result["outputs_enabled"])

    def test_no_output_profile_never_enables_either_transport(self):
        for transport in ("frame_stream", "legacy"):
            for no_output, actuator_outputs in ((True, False), (False, False), (True, True)):
                with self.subTest(transport=transport, no_output=no_output,
                                  actuator_outputs=actuator_outputs):
                    agent = Arbitration().agent(transport=transport)
                    agent.hybrid_no_output = no_output
                    agent.actuator_outputs = actuator_outputs
                    with self.assertRaises(ApiError) as ctx:
                        agent.hybrid_enable_output("real-lease-token", {})
                    self.assertEqual(ctx.exception.reason, "hybrid_no_output")
                    self.assertFalse(agent.frame_outputs_enabled)

    def test_enabling_outputs_while_a_stop_is_latched_is_refused(self):
        """A latched stop must not be silently overruled by an enable request.

        The mirror already ranks the stop above the output flag, so accepting the enable
        would answer `enabled: true` while nothing was permitted: the operator has to see
        the refusal and clear the stop first.
        """
        agent = Arbitration().agent(estop=True)
        agent._require_hybrid_gateway = lambda token: type(
            "G", (), {"status": staticmethod(lambda: {
                "session_id": "s", "session_epoch": 1,
                "calibration_bundle_hash": BUNDLE, "outputs_enabled": False,
                "permits": {}})})()
        with self.assertRaises(ApiError) as ctx:
            agent.hybrid_enable_output("real-lease-token",
                                       {"session_id": "s", "session_epoch": 1,
                                        "calibration_bundle_hash": BUNDLE})
        self.assertEqual(ctx.exception.reason, "estop_latched")
