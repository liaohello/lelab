"""Jetson path on LeRobot 0.7: retired gRPC client is refused, server is Zenoh.

These tests use no SSH connection, camera, serial port or model. They check the
production guard and the generated server command, not hardware behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lelab import jetson_policy as jp
from lelab.utils.inference_070 import LEGACY_TOKENS


class _Stop(Exception):
    pass


def test_jetson_server_requires_explicit_deployment_and_semantics() -> None:
    config = jp.JetsonConfig(deployment="", semantics="")

    def never_called(*args, **kwargs):
        raise AssertionError("no SSH command may run without a deployment identity")

    with pytest.raises(jp.JetsonPolicyError, match="LELAB_JETSON_DEPLOYMENT"):
        jp.JetsonPolicyServer(config, ssh_runner=never_called).start()


def test_jetson_server_script_uses_the_070_entry_point() -> None:
    config = jp.JetsonConfig(
        deployment="jetson_so101",
        semantics="so101-degrees-gripper-percent-v1",
        policy_port=28081,
    )
    seen: dict[str, object] = {}

    def capture(script: str, *args: str, **kwargs):
        seen["script"] = script
        seen["args"] = args
        raise _Stop()

    with pytest.raises(_Stop):
        jp.JetsonPolicyServer(config, ssh_runner=capture).start()

    script = str(seen["script"])
    assert "lerobot.scripts.lerobot_policy_server" in script
    assert "--config_path=" in script
    assert '"[tcp/127.0.0.1:$port]"' in script
    assert not [token for token in LEGACY_TOKENS if token in script]
    assert '"deployment": "jetson_so101"' in str(seen["args"][-1])


def test_shadow_and_execute_clients_fail_before_any_device_access() -> None:
    config = jp.JetsonConfig()
    contract = SimpleNamespace(n_action_steps=1, chunk_size=1)
    source = SimpleNamespace(start=lambda: pytest.fail("source must not start"))
    gate = jp.ExecuteGate(
        calibration_verified=True,
        contract_verified=True,
        shadow_verified=True,
        stop_verified=True,
        site_authorized=True,
    )
    for mode in (jp.MODE_SHADOW, jp.MODE_EXECUTE):
        client = jp.JetsonShadowClient(
            config,
            contract,
            source,
            mode=mode,
            execute_gate=gate,
            action_sink=object(),  # type: ignore[arg-type]
        )  # type: ignore[arg-type]
        with pytest.raises(jp.JetsonPolicyError, match="retired 0.6 path"):
            client.start()


def test_observe_mode_remains_available_without_transport() -> None:
    config = jp.JetsonConfig()
    contract = SimpleNamespace(n_action_steps=1, chunk_size=1)
    started = []
    source = SimpleNamespace(start=lambda: started.append(True))

    def idle_loop(*args, **kwargs):
        client._stop.wait(timeout=0.5)

    client = jp.JetsonShadowClient(config, contract, source, mode=jp.MODE_OBSERVE)  # type: ignore[arg-type]
    client._observe_loop = idle_loop  # type: ignore[method-assign]
    client.start()
    assert started == [True]
    client.stop()
