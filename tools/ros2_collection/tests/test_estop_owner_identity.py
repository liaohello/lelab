"""Execute the actual agent method bodies without importing ROS or opening a port."""
from __future__ import annotations

import ast
from collections import deque
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]


class ApiError(Exception):
    pass


def agent_methods():
    path = ROOT / "ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MobileAgent")
    wanted = {"_estop_owner_from_events", "estop", "clear_estop", "acquire"}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert len(methods) == len(wanted)
    scope = {"time": time, "ApiError": ApiError,
             "Empty": SimpleNamespace, "Bool": SimpleNamespace,
             "Twist": SimpleNamespace, "String": SimpleNamespace}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), scope)
    return type("AgentMethodHarness", (), {n.name: scope[n.name] for n in methods})


@pytest.fixture
def agent():
    node = agent_methods()()
    node.lock = threading.RLock()
    node.events = deque(maxlen=300)
    node.control_client = "original-operator"
    node.control_token = None
    node.control_expires = 0
    node.estop_latched = False
    node.estop_owner_client = None
    node.estop_requested_at = None
    node.frame_outputs_enabled = False
    node.control_mode = "idle"
    node.expert_cancel = threading.Event()
    node.heartbeat_pub = node.estop_pub = node.reset_pub = None
    node.licence_bridge = None
    node._refuse_read_only = lambda _: None
    node._announce = lambda *_: False
    node._zero_base_now = lambda: None
    node._bridge_wake = lambda _: None
    node._emit = lambda kind, **kw: node.events.append({"kind": kind, **kw})

    def refused(_token):
        raise ApiError("invalid control token")

    node._check_token = refused
    return node


def test_repeated_stop_after_expiry_preserves_first_owner_and_time(agent):
    agent.estop(None)
    original_time = agent.estop_requested_at
    agent.control_client = None
    agent.estop(None, "repeated-after-expiry")
    assert agent.estop_owner_client == "original-operator"
    assert agent.estop_requested_at == original_time
    assert agent.events[-1]["repeated"] is True
    out = agent.clear_estop(None, "original-operator")
    assert out["authorised_by"] == "prior_owner_recovery"
    assert out["motion_granted"] is False
    assert agent.frame_outputs_enabled is False


def test_legacy_overwritten_owner_recovers_only_from_uncleared_history(agent):
    agent.events.extend([
        {"kind": "control_acquired", "client_id": "original-operator"},
        {"kind": "estop"}, {"kind": "estop"},
    ])
    agent.estop_latched = True
    agent.estop_owner_client = None
    out = agent.clear_estop(None, "original-operator")
    assert out["authorised_by"] == "prior_owner_recovery"
    assert not agent.estop_latched
    assert not agent.frame_outputs_enabled


@pytest.mark.parametrize("client", [None, "unrelated-client"])
def test_legacy_history_does_not_grant_anonymous_or_wrong_client_clear(agent, client):
    agent.events.extend([{"kind": "control_acquired", "client_id": "original-operator"},
                         {"kind": "estop"}])
    agent.estop_latched = True
    with pytest.raises(ApiError):
        agent.clear_estop(None, client)
    assert agent.estop_latched


@pytest.mark.parametrize("barrier", ["estop_cleared", "control_released", "control_expired"])
def test_old_or_released_owner_history_cannot_clear_new_stop(agent, barrier):
    agent.events.extend([{"kind": "control_acquired", "client_id": "original-operator"},
                         {"kind": barrier}, {"kind": "estop"}])
    agent.estop_latched = True
    with pytest.raises(ApiError):
        agent.clear_estop(None, "original-operator")
    assert agent.estop_latched


def test_real_acquire_refuses_estop_so_temporary_token_cannot_be_recovery(agent):
    agent.estop_latched = True
    with pytest.raises(ApiError, match="clear emergency stop first"):
        agent.acquire("original-operator")


@pytest.mark.parametrize("client", ["unrelated-client", ""])
def test_recovery_acquire_cannot_bypass_original_owner(agent, client):
    agent.estop_latched = True
    agent.estop_owner_client = "original-operator"
    with pytest.raises(ApiError, match="original operator"):
        agent.acquire(client, recovery=True)


def test_ownerless_recovery_acquire_does_not_issue_a_clear_token(agent):
    agent.estop_latched = True
    agent.estop_owner_client = None
    with pytest.raises(ApiError, match="original operator"):
        agent.acquire("any-client", recovery=True)
