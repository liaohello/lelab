"""Serial-port role inventory tests: read-only enumeration and file output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lelab import hardware_inventory as hi
from lelab.utils import config as cfg

BY_ID = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_{serial}-if00"


def _port(serial: str, *, present: bool = True, role: str = "unassigned", note: str = ""):
    return {
        "id": BY_ID.format(serial=serial),
        "by_id": BY_ID.format(serial=serial),
        "device": f"/dev/ttyACM{serial[-1]}",
        "source": "by-id",
        "serial": serial,
        "vid": "1a86",
        "pid": "7523",
        "manufacturer": "QinHeng Electronics",
        "product": "USB Single Serial",
        "usb_path": f"usb-1-{serial[-1]}",
        "present": present,
        "role": role,
        "note": note,
    }


@pytest.fixture()
def inventory_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hardware"
    monkeypatch.setattr(cfg, "HARDWARE_INVENTORY_PATH", str(root))
    return root


def test_sysfs_identity_is_read_without_opening_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tty_root = tmp_path / "sys-tty"
    node = tty_root / "ttyACM9"
    node.mkdir(parents=True)
    usb_device = tmp_path / "usb" / "1-2"
    usb_device.mkdir(parents=True)
    (usb_device / "idVendor").write_text("1a86\n")
    (usb_device / "idProduct").write_text("7523\n")
    (usb_device / "serial").write_text("5B91045575\n")
    (usb_device / "manufacturer").write_text("QinHeng Electronics\n")
    (usb_device / "product").write_text("USB Single Serial\n")
    (usb_device / "busnum").write_text("1\n")
    (usb_device / "devpath").write_text("2\n")
    interface = usb_device / "1-2:1.0"
    interface.mkdir()
    (node / "device").symlink_to(interface, target_is_directory=True)

    monkeypatch.setattr(hi, "_TTY_ROOT", tty_root)

    assert hi._sysfs_info("ttyACM9") == {
        "vid": "1a86",
        "pid": "7523",
        "serial": "5B91045575",
        "manufacturer": "QinHeng Electronics",
        "product": "USB Single Serial",
        "usb_path": "usb-1-2",
    }


def test_by_id_enumeration_uses_stable_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    by_id_root = tmp_path / "by-id"
    by_id_root.mkdir()
    device = tmp_path / "ttyACM9"
    device.write_text("")
    (by_id_root / "usb-1a86_USB_Single_Serial_ABC123-if00").symlink_to(device)

    sysfs_root = tmp_path / "tty"
    node = sysfs_root / "ttyACM9"
    node.mkdir(parents=True)
    usb_device = tmp_path / "usbdev"
    usb_device.mkdir()
    (usb_device / "idVendor").write_text("1a86")
    (usb_device / "idProduct").write_text("7523")
    (usb_device / "serial").write_text("ABC123")
    (node / "device").symlink_to(usb_device, target_is_directory=True)

    monkeypatch.setattr(hi, "_BY_ID_ROOT", by_id_root)
    monkeypatch.setattr(hi, "_TTY_ROOT", sysfs_root)

    ports = hi.list_serial_ports()

    assert len(ports) == 1
    assert ports[0]["serial"] == "ABC123"
    assert ports[0]["by_id"].endswith("usb-1a86_USB_Single_Serial_ABC123-if00")
    assert ports[0]["present"] is True
    assert ports[0]["role"] == "unassigned"


def test_merge_keeps_unplugged_saved_port_and_role(inventory_home: Path) -> None:
    latest = {
        "saved_at": "2026-10-31T08:00:00+00:00",
        "serial_ports": [
            _port("111", role="left_follower", note="left arm"),
            _port("222", role="right_follower", present=False),
        ],
    }
    merged = hi.merge_inventory_ports([_port("111")], latest)

    by_serial = {port["serial"]: port for port in merged}
    assert by_serial["111"]["present"] is True
    assert by_serial["111"]["role"] == "left_follower"
    assert by_serial["111"]["note"] == "left arm"
    assert by_serial["222"]["present"] is False
    assert by_serial["222"]["role"] == "right_follower"


def test_save_writes_complete_file_and_latest(
    inventory_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = [_port("111"), _port("222")]
    monkeypatch.setattr(hi, "list_serial_ports", lambda: ports)

    result = hi.save_inventory(
        {
            ports[0]["id"]: "left_follower",
            ports[1]["id"]: "right_follower",
        },
        {ports[0]["id"]: "left arm"},
    )

    assert result["complete"] is False
    assert set(result["missing_roles"]) == {"left_leader", "right_leader"}
    saved_path = Path(result["saved_path"])
    latest_path = Path(result["latest_path"])
    assert saved_path.is_file()
    assert latest_path.is_file()
    assert saved_path.name.startswith("hardware-inventory-")

    document = json.loads(saved_path.read_text())
    assert document["schema_version"] == 1
    assert document["serial_ports"][0]["role"] == "left_follower"
    assert document["serial_ports"][0]["note"] == "left arm"
    assert document["role_labels"]["left_follower"] == "左从臂（执行）"
    assert document["file"] == str(saved_path)
    assert document["hostname"]
    assert json.loads(latest_path.read_text())["saved_at"] == document["saved_at"]


def test_save_keeps_assignment_for_unplugged_port(
    inventory_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hi, "list_serial_ports", lambda: [_port("111")])

    result = hi.save_inventory(
        {
            _port("111")["id"]: "left_follower",
            _port("222")["id"]: "right_follower",
        }
    )

    saved = {port["id"]: port for port in result["ports"]}
    assert saved[_port("111")["id"]]["present"] is True
    assert saved[_port("222")["id"]]["present"] is False
    assert saved[_port("222")["id"]]["role"] == "right_follower"


def test_duplicate_required_role_is_rejected(
    inventory_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = [_port("111"), _port("222")]
    monkeypatch.setattr(hi, "list_serial_ports", lambda: ports)

    with pytest.raises(ValueError, match="assigned to both"):
        hi.save_inventory(
            {
                ports[0]["id"]: "left_follower",
                ports[1]["id"]: "left_follower",
            }
        )


def test_unknown_role_is_rejected(inventory_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _port("111")
    monkeypatch.setattr(hi, "list_serial_ports", lambda: [port])

    with pytest.raises(ValueError, match="unknown role"):
        hi.save_inventory({port["id"]: "writer"})


def test_inventory_http_round_trip(
    client: TestClient, inventory_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = [_port("111"), _port("222")]
    monkeypatch.setattr(hi, "list_serial_ports", lambda: ports)

    listed = client.get("/hardware/inventory")
    assert listed.status_code == 200
    assert len(listed.json()["ports"]) == 2

    saved = client.post(
        "/hardware/inventory/save",
        json={
            "roles": {
                ports[0]["id"]: "left_follower",
                ports[1]["id"]: "right_follower",
            },
            "notes": {ports[0]["id"]: "left arm"},
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["status"] == "success"
    assert body["complete"] is False
    assert Path(body["saved_path"]).is_file()

    downloaded = client.get("/hardware/inventory/latest")
    assert downloaded.status_code == 200
    assert downloaded.headers["cache-control"] == "no-store"
    assert downloaded.json()["serial_ports"][0]["role"] == "left_follower"


def test_inventory_http_rejects_duplicate_role(
    client: TestClient, inventory_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = [_port("111"), _port("222")]
    monkeypatch.setattr(hi, "list_serial_ports", lambda: ports)

    response = client.post(
        "/hardware/inventory/save",
        json={
            "roles": {
                ports[0]["id"]: "left_leader",
                ports[1]["id"]: "left_leader",
            }
        },
    )

    assert response.status_code == 400
    assert "assigned to both" in response.json()["detail"]
