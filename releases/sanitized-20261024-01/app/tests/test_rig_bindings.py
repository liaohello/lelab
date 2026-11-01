"""Role inventory to robot-record binding tests (no hardware access)."""

from __future__ import annotations

import pytest

from lelab import hardware_inventory as hi, rig_bindings as rb
from lelab.utils import config as cfg

BY_ID = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_{serial}-if00"


def _port(serial: str):
    return {
        "id": BY_ID.format(serial=serial),
        "by_id": BY_ID.format(serial=serial),
        "device": f"/dev/ttyACM{serial[-1]}",
        "source": "by-id",
        "serial": serial,
        "vid": "1a86",
        "pid": "55d3",
        "manufacturer": None,
        "product": "USB Single Serial",
        "usb_path": f"usb-3-{serial[-1]}",
        "present": True,
        "role": "unassigned",
        "note": "",
    }


@pytest.fixture()
def saved_roles(tmp_path, monkeypatch: pytest.MonkeyPatch):
    ports = [_port("111"), _port("222"), _port("333"), _port("444")]
    monkeypatch.setattr(cfg, "HARDWARE_INVENTORY_PATH", str(tmp_path / "hardware"))
    monkeypatch.setattr(hi, "list_serial_ports", lambda: ports)
    hi.save_inventory(
        {
            ports[0]["id"]: "left_follower",
            ports[3]["id"]: "right_follower",
            ports[1]["id"]: "left_leader",
            ports[2]["id"]: "right_leader",
        }
    )
    return {
        "left_follower": BY_ID.format(serial="111"),
        "right_follower": BY_ID.format(serial="444"),
        "left_leader": BY_ID.format(serial="222"),
        "right_leader": BY_ID.format(serial="333"),
    }


@pytest.fixture()
def fake_records(monkeypatch: pytest.MonkeyPatch):
    records = {
        "left_rig": {
            "name": "left_rig",
            "leader_port": "/dev/lelab-left-leader",
            "follower_port": "/dev/lelab-left-follower",
            "leader_config": "left_leader.json",
            "follower_config": "left_follower.json",
            "cameras": [],
        },
        "right_rig": {
            "name": "right_rig",
            "leader_port": "/dev/lelab-right-leader",
            "follower_port": "/dev/lelab-right-follower",
            "leader_config": "right_leader.json",
            "follower_config": "right_follower.json",
            "cameras": [],
        },
    }
    writes: list[str] = []

    def get(name: str):
        record = records.get(name)
        return dict(record) if record is not None else None

    def save(name: str, data: dict, allow_create: bool = True) -> bool:
        assert allow_create is False
        if name not in records:
            return False
        records[name].update(data)
        writes.append(name)
        return True

    monkeypatch.setattr(cfg, "get_robot_record", get)
    monkeypatch.setattr(cfg, "save_robot_record", save)
    return {"records": records, "writes": writes}


def test_describe_proposes_actual_ports_from_saved_roles(
    saved_roles, fake_records
) -> None:
    result = rb.describe_rig_bindings()

    assert result["can_apply"] is True
    assert result["missing_roles"] == []
    left, right = result["bindings"]
    assert left["changes"] == {
        "follower_port": saved_roles["left_follower"],
        "leader_port": saved_roles["left_leader"],
    }
    assert right["changes"] == {
        "follower_port": saved_roles["right_follower"],
        "leader_port": saved_roles["right_leader"],
    }


def test_apply_updates_only_ports_and_is_idempotent(
    saved_roles, fake_records
) -> None:
    result = rb.apply_rig_bindings()

    assert result["updated"] == ["left_rig", "right_rig"]
    records = fake_records["records"]
    assert records["left_rig"]["follower_port"] == saved_roles["left_follower"]
    assert records["left_rig"]["leader_port"] == saved_roles["left_leader"]
    assert records["right_rig"]["follower_port"] == saved_roles["right_follower"]
    assert records["right_rig"]["leader_port"] == saved_roles["right_leader"]
    assert records["left_rig"]["follower_config"] == "left_follower.json"
    assert records["right_rig"]["leader_config"] == "right_leader.json"

    again = rb.apply_rig_bindings()
    assert again["updated"] == []
    assert set(again["unchanged"]) == {"left_rig", "right_rig"}
    assert fake_records["writes"] == ["left_rig", "right_rig"]


def test_apply_requires_all_roles(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ports = [_port("111"), _port("222")]
    monkeypatch.setattr(cfg, "HARDWARE_INVENTORY_PATH", str(tmp_path / "hardware"))
    monkeypatch.setattr(hi, "list_serial_ports", lambda: ports)
    hi.save_inventory({ports[0]["id"]: "left_follower"})

    with pytest.raises(ValueError, match="save all four"):
        rb.apply_rig_bindings()


def test_apply_reports_missing_robot_records(
    saved_roles, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg, "get_robot_record", lambda _name: None)
    monkeypatch.setattr(
        cfg,
        "save_robot_record",
        lambda *_args, **_kwargs: pytest.fail("must not create a missing record"),
    )

    result = rb.describe_rig_bindings()
    assert result["can_apply"] is False
    assert [binding["status"] for binding in result["bindings"]] == [
        "record_missing",
        "record_missing",
    ]

    with pytest.raises(ValueError, match="robot records are missing"):
        rb.apply_rig_bindings()


def test_invalid_record_name_is_rejected(saved_roles, fake_records) -> None:
    with pytest.raises(ValueError, match="invalid robot record name"):
        rb.describe_rig_bindings(left_robot="../bad")
