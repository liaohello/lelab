"""Tests for lelab.arm_bindings — which calibration belongs to which physical arm.

Every SO-101 on this bench uses motor ids 1..6 under the same six joint names, so
lerobot will happily write the left arm's numbers onto the right arm: the bus
matches motors *by name* and never compares the ``id`` stored in each calibration
entry. These tests pin down what the app refuses when the recorded evidence
contradicts the request, and — just as important — what it must not refuse, so an
arm calibrated before this table existed still starts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
LEFT_LEADER = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00"
LEFT_FOLLOWER = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149339-if00"


def calibration(shift: int = 0) -> dict:
    """A complete six-motor calibration, distinct for each ``shift`` value."""
    return {
        name: {
            "id": index + 1,
            "drive_mode": 0,
            "homing_offset": -1000 + shift * 7 + index,
            "range_min": 800 + index,
            "range_max": 3200 - index - shift,
        }
        for index, name in enumerate(JOINTS)
    }


def bus_ids() -> dict[str, int]:
    return {name: index + 1 for index, name in enumerate(JOINTS)}


class Bench:
    """The two calibration trees and a binding table, all inside one tmp home."""

    def __init__(self, bindings: Path) -> None:
        from lelab import arm_bindings
        from lelab.utils import config as cfg

        self.ab = arm_bindings
        self.cfg = cfg
        self.bindings = bindings

    def write(self, role: str, name: str, payload: dict) -> None:
        directory = Path(self.cfg.LEADER_CONFIG_PATH if role == "leader" else self.cfg.FOLLOWER_CONFIG_PATH)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

    def measure(self, port: str, device_type: str, name: str, payload: dict) -> None:
        """What the calibration wizard does: save the file, then bind it to the port."""
        self.write(self.ab.DEVICE_TYPE_TO_ROLE[device_type], name, payload)
        self.ab.record(port, device_type, name, payload, path=self.bindings)

    def side(self, port: str, role: str, name: str, ids: dict[str, int] | None = None):
        """One side of a teleoperation request; ``ids`` is the motor table of the bus."""
        bus = None
        if ids is not None:
            bus = type("Bus", (), {"motors": {n: type("M", (), {"id": i})() for n, i in ids.items()}})
        return self.ab.side(port, role, name, bus)

    def check(self, leader: tuple, follower: tuple) -> dict:
        return self.ab.check_pair(self.side(*leader), self.side(*follower), path=self.bindings)


@pytest.fixture
def bench(tmp_lerobot_home, monkeypatch: pytest.MonkeyPatch) -> Bench:
    bindings = tmp_lerobot_home / "lelab" / "arm_bindings.json"
    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(bindings))
    return Bench(bindings)


def test_a_pair_measured_on_this_bench_is_accepted(bench: Bench) -> None:
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    bench.measure(LEFT_FOLLOWER, "robot", "left_rig", calibration(2))

    verdict = bench.check((LEFT_LEADER, "leader", "left_rig"), (LEFT_FOLLOWER, "follower", "left_rig"))

    assert verdict["refusals"] == []
    assert verdict["warnings"] == []
    assert verdict["ready"] is True


def test_swapping_the_two_ports_is_refused(bench: Bench) -> None:
    """The failure this exists for: the operator designates the follower's adapter as
    the leader. Both calibrations are complete and plausible, so nothing else complains."""
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    bench.measure(LEFT_FOLLOWER, "robot", "left_rig", calibration(2))

    verdict = bench.check((LEFT_FOLLOWER, "leader", "left_rig"), (LEFT_LEADER, "follower", "left_rig"))

    assert verdict["ready"] is False
    assert len(verdict["refusals"]) == 2
    assert any("was last calibrated as the follower" in r for r in verdict["refusals"])
    assert any("was last calibrated as the leader" in r for r in verdict["refusals"])


def test_selecting_another_rigs_file_for_a_bound_port_is_refused(bench: Bench) -> None:
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    bench.write("leader", "right_rig", calibration(5))

    verdict = bench.check((LEFT_LEADER, "leader", "right_rig"), ("", "follower", ""))

    assert verdict["ready"] is False
    assert any("was measured as left_rig.json, but right_rig.json is selected" in r
               for r in verdict["refusals"])


def test_an_unbound_port_warns_but_still_starts(bench: Bench) -> None:
    """Arms calibrated before this table existed must keep working: no evidence is not
    a contradiction."""
    bench.write("leader", "old_rig", calibration(1))
    bench.write("follower", "old_rig", calibration(2))

    verdict = bench.check((LEFT_LEADER, "leader", "old_rig"), (LEFT_FOLLOWER, "follower", "old_rig"))

    assert verdict["refusals"] == []
    assert verdict["ready"] is True
    assert len(verdict["warnings"]) == 2
    assert all("no recorded measurement" in w for w in verdict["warnings"])


def test_a_hand_edited_calibration_warns_instead_of_refusing(bench: Bench) -> None:
    """Tightening a range by hand is a normal thing to do on this bench. The app says the
    file no longer matches what was measured, but does not lock the operator out."""
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    edited = calibration(1)
    edited["wrist_roll"]["range_max"] = 3000
    bench.write("leader", "left_rig", edited)

    verdict = bench.check((LEFT_LEADER, "leader", "left_rig"), ("", "follower", ""))

    assert verdict["ready"] is True
    assert any("changed since it was measured" in w for w in verdict["warnings"])


def test_one_port_cannot_be_both_arms(bench: Bench) -> None:
    verdict = bench.check((LEFT_LEADER, "leader", "left_rig"), (LEFT_LEADER, "follower", "left_rig"))

    assert any("same port" in r for r in verdict["refusals"])


def test_ids_that_disagree_with_the_bus_are_refused(bench: Bench) -> None:
    """A servo re-ID'd after calibration: the file is complete and the joint names match,
    but one entry describes a motor this bus will never poll under that name."""
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    ids = bus_ids()
    ids["wrist_flex"] = 7

    verdict = bench.check((LEFT_LEADER, "leader", "left_rig", ids), ("", "follower", ""))

    assert verdict["ready"] is False
    assert any("wrist_flex" in r and "bus_id=7" in r for r in verdict["refusals"])


def test_a_bus_without_a_motor_table_skips_the_id_check(bench: Bench) -> None:
    """No ids to compare against is not evidence of a mismatch — the port was never
    opened, so nothing on the hardware has been contradicted."""
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))

    verdict = bench.check((LEFT_LEADER, "leader", "left_rig"), ("", "follower", ""))

    assert verdict["ready"] is True
    assert not any("does not match the motors" in r for r in verdict["refusals"])


def test_left_rig_and_left_rig_json_are_the_same_calibration(bench: Bench) -> None:
    """The robot record stores "left_rig.json" while a teleop request sends "left_rig"."""
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))

    verdict = bench.check((LEFT_LEADER, "leader", "left_rig.json"), ("", "follower", ""))

    assert verdict["refusals"] == []
    assert not any("was measured as" in r for r in verdict["refusals"])


def test_a_missing_calibration_file_warns(bench: Bench) -> None:
    verdict = bench.check((LEFT_LEADER, "leader", "never_written"), ("", "follower", ""))

    assert any("could not be read" in w for w in verdict["warnings"])


def test_a_calibration_name_cannot_escape_the_calibration_tree(bench: Bench) -> None:
    assert bench.ab.calibration_path("leader", "../../etc/passwd") is None
    assert bench.ab.read_calibration("leader", "../follower_rig") is None
    assert bench.ab.read_calibration("gripper", "left_rig") is None


def test_a_damaged_binding_table_does_not_block_teleoperation(bench: Bench) -> None:
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    bench.bindings.write_text("{not json", encoding="utf-8")

    verdict = bench.check((LEFT_LEADER, "leader", "left_rig"), ("", "follower", ""))

    assert verdict["ready"] is True
    assert any("no recorded measurement" in w for w in verdict["warnings"])


def test_recalibrating_a_port_replaces_the_previous_binding(bench: Bench) -> None:
    """Last measurement wins: an arm re-measured as the other side must be usable there."""
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    bench.measure(LEFT_LEADER, "robot", "left_rig", calibration(3))

    verdict = bench.check(("", "leader", ""), (LEFT_LEADER, "follower", "left_rig"))

    assert verdict["ready"] is True
    assert not any("was last calibrated as" in r for r in verdict["refusals"])


def test_record_refuses_an_unknown_device_type(bench: Bench) -> None:
    with pytest.raises(ValueError, match="device_type"):
        bench.ab.record(LEFT_LEADER, "gripper", "left_rig", calibration(1), path=bench.bindings)


def test_workbench_inventory_binds_ports_to_calibration_identity(bench: Bench, monkeypatch) -> None:
    from lelab import arm_bindings

    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    monkeypatch.setattr("lelab.utils.config.find_available_ports", lambda: [LEFT_LEADER, LEFT_FOLLOWER])
    monkeypatch.setattr(
        "lelab.mobile_leader_reader.list_leader_ports",
        lambda base="/dev/serial/by-id": [
            {"by_id": LEFT_LEADER, "device": "ttyACM0", "serial": "5B90149723"},
            {"by_id": LEFT_FOLLOWER, "device": "ttyACM1", "serial": "5B90149339"},
        ],
    )

    inventory = arm_bindings.workbench_inventory()

    assert inventory["ports"][0]["serial"] == "5B90149723"
    assert inventory["ports"][0]["device"] == "ttyACM0"
    assert inventory["ports"][0]["binding"]["role"] == "leader"
    assert inventory["ports"][0]["binding"]["config"] == "left_rig.json"
    # These by-id paths do not exist in the tmp home, so the page must be told they
    # are absent rather than silently listed as available arms.
    assert inventory["ports"][0]["connected"] is False

    rows = {row["config"]: row for row in inventory["calibrations"]["leader"]}
    assert rows["left_rig.json"]["ids"] == bus_ids()
    assert rows["left_rig.json"]["bound_ports"] == [LEFT_LEADER]
    assert rows["left_rig.json"]["readable"] is True
    assert inventory["calibrations"]["follower"] == []


def test_built_in_uart_nodes_are_not_offered_as_arms(bench: Bench, monkeypatch) -> None:
    """With no /dev/serial/by-id the port list degrades to every TTY the OS has, and
    /dev/ttyS0 exists whether or not anything is plugged in. Listing those as arms would
    let the operator "select" a motherboard port and then read a dead bus."""
    from lelab import arm_bindings

    monkeypatch.setattr("lelab.utils.config.find_available_ports",
                        lambda: ["/dev/ttyS0", "/dev/ttyS31", "/dev/ttyACM7", "COM3"])
    monkeypatch.setattr("lelab.mobile_leader_reader.list_leader_ports", lambda base="/dev/serial/by-id": [])

    listed = [row["port"] for row in arm_bindings.workbench_inventory()["ports"]]

    assert listed == ["/dev/ttyACM7", "COM3"]


def test_devices_endpoint_answers_with_the_table_and_session_state(client, bench: Bench, monkeypatch) -> None:
    """The contract the workbench page is built on: one read-only GET that says what is
    plugged in, what each port was measured as, and whether a session holds the arms."""
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    monkeypatch.setattr("lelab.utils.config.find_available_ports", lambda: [LEFT_LEADER])
    monkeypatch.setattr("lelab.mobile_leader_reader.list_leader_ports",
                        lambda base="/dev/serial/by-id": [
                            {"by_id": LEFT_LEADER, "device": "ttyACM0", "serial": "5B90149723"}])

    body = client.get("/so101/devices").json()

    assert body["status"] == "success"
    assert body["ports"][0]["binding"]["config"] == "left_rig.json"
    assert body["calibrations"]["leader"][0]["bound_ports"] == [LEFT_LEADER]
    assert body["lease"] is not None
    assert body["teleoperation"]["teleoperation_active"] is False
    assert "no port was opened" in body["note"]


def test_identity_endpoint_refuses_the_swapped_pair_over_http(client, bench: Bench) -> None:
    bench.measure(LEFT_LEADER, "teleop", "left_rig", calibration(1))
    bench.measure(LEFT_FOLLOWER, "robot", "left_rig", calibration(2))

    body = client.get(
        "/so101/identity",
        params={"leader_port": LEFT_FOLLOWER, "leader_config": "left_rig",
                "follower_port": LEFT_LEADER, "follower_config": "left_rig"},
    ).json()

    assert body["ready"] is False
    assert len(body["refusals"]) == 2
    assert body["ports"][LEFT_FOLLOWER]["role"] == "follower"
