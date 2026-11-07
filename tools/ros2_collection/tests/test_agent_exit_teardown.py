"""The agent's own exit path must stop the car first and leave no traceback behind.

需求六 covers "the upper computer disappears" - on the car that is a SIGTERM/SIGINT into this
process, and 2026-11-03's local rehearsal showed the exit was not clean: rclpy's signal handler had
already torn the context down, so the shutdown call in ``finally`` raised RCLError and printed a
traceback over the real exit status. Anything written after such a call would never run, so the
ordering here is tested, not assumed.
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent import agent as agent_module  # noqa: E402


class Ledger:
    """Records the calls the teardown makes, in the order the car would feel them."""

    def __init__(self, context_live: bool, session_raises: bool = False):
        self.events: list[str] = []
        self.context_live = context_live
        self.session_raises = session_raises

    def set(self) -> None:
        self.events.append("preview_stop")

    def stop_session(self) -> None:
        if self.session_raises:
            raise RuntimeError("recorder would not close")
        self.events.append("stop_session")

    def publish(self, _msg) -> None:
        self.events.append("chassis_zero")

    def shutdown(self) -> None:
        self.events.append("http_stop")

    def destroy_node(self) -> None:
        self.events.append("destroy_node")

    def ok(self) -> bool:
        return self.context_live

    def rcl_shutdown(self) -> None:
        if not self.context_live:
            raise RuntimeError("rcl_shutdown already called on the given context")
        self.events.append("rclpy_shutdown")


class FakeRclpy:
    def __init__(self, ledger: Ledger):
        self._ledger = ledger

    def ok(self) -> bool:
        return self._ledger.ok()

    def shutdown(self) -> None:
        self._ledger.rcl_shutdown()


class FakeSessions:
    def __init__(self, ledger: Ledger, active: bool = True):
        self._ledger = ledger
        self.active = active

    def stop_session(self) -> None:
        self._ledger.stop_session()


class TeardownTests(unittest.TestCase):
    def build(self, context_live=True, session_raises=False, active=True, base_pub=True):
        ledger = Ledger(context_live, session_raises)
        agent = type("Stub", (), {
            "_preview_stop": ledger,
            "sessions": FakeSessions(ledger, active),
            "base_pub": ledger if base_pub else None,
            "destroy_node": ledger.destroy_node,
        })()
        real_rclpy, real_twist = agent_module.rclpy, agent_module.Twist
        agent_module.rclpy = FakeRclpy(ledger)
        agent_module.Twist = lambda *a, **k: "twist"
        self.addCleanup(self._restore, real_rclpy, real_twist)
        return ledger, agent

    @staticmethod
    def _restore(rclpy, twist):
        agent_module.rclpy, agent_module.Twist = rclpy, twist

    def test_the_context_is_still_live_so_it_is_shut_down_last(self):
        ledger, agent = self.build(context_live=True)
        agent_module._teardown(agent, ledger)
        self.assertEqual(ledger.events,
                         ["preview_stop", "stop_session", "chassis_zero", "http_stop",
                          "destroy_node", "rclpy_shutdown"])

    def test_a_signal_handler_that_already_closed_the_context_raises_nothing(self):
        """The 2026-11-03 rehearsal exit: ok() is False, so shutdown() must not be reached."""
        ledger, agent = self.build(context_live=False)
        agent_module._teardown(agent, ledger)  # used to die with RCLError here
        self.assertNotIn("rclpy_shutdown", ledger.events)
        self.assertEqual(ledger.events[:4], ["preview_stop", "stop_session", "chassis_zero",
                                             "http_stop"])

    def test_a_session_that_will_not_close_still_gets_a_zeroed_chassis(self):
        ledger, agent = self.build(session_raises=True)
        agent_module._teardown(agent, ledger)
        self.assertIn("chassis_zero", ledger.events, "stopping the car is not optional")
        self.assertIn("http_stop", ledger.events)

    def test_the_context_decides_which_rclpy_error_counts_as_a_planned_stop(self):
        """Two different rclpy exceptions came out of planned SIGTERM stops on 2026-11-03."""
        from rclpy.executors import ExternalShutdownException

        class Live:
            ok = staticmethod(lambda: True)

        class Dead:
            ok = staticmethod(lambda: False)

        real = agent_module.rclpy
        self.addCleanup(setattr, agent_module, "rclpy", real)
        agent_module.rclpy = Live
        self.assertTrue(agent_module._is_planned_stop(KeyboardInterrupt()))
        self.assertTrue(agent_module._is_planned_stop(ExternalShutdownException()))
        self.assertFalse(agent_module._is_planned_stop(RuntimeError("publisher died")),
                         "a real fault must still leave a traceback and a non-zero exit")
        agent_module.rclpy = Dead
        self.assertTrue(agent_module._is_planned_stop(RuntimeError(
            "failed to initialize wait set: the given context is not valid")))

    def test_a_signal_stop_is_a_planned_exit_and_a_fault_is_not(self):
        from rclpy.executors import ExternalShutdownException

        self.assertIn(KeyboardInterrupt, agent_module.PLANNED_STOP_ERRORS)
        self.assertIn(ExternalShutdownException, agent_module.PLANNED_STOP_ERRORS)
        self.assertNotIn(RuntimeError, agent_module.PLANNED_STOP_ERRORS)
        main_source = inspect.getsource(agent_module.main)
        self.assertIn("_is_planned_stop(stop)", main_source,
                      "a planned stop must not be left to escape as a traceback")
        self.assertIn("_teardown(agent, server)", main_source)

    def test_no_control_lease_means_no_chassis_frame_is_published(self):
        ledger, agent = self.build(base_pub=False)
        agent_module._teardown(agent, ledger)
        self.assertNotIn("chassis_zero", ledger.events)


if __name__ == "__main__":
    unittest.main()
