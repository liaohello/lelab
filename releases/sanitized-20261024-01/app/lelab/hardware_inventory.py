"""Read-only serial-port inventory used to identify physical arm roles.

The page works by having the operator unplug one USB cable at a time: the
matching ``/dev/serial/by-id`` entry disappears, the operator assigns the
physical arm's role, then plugs it back in.  Enumeration only reads sysfs; it
never opens a serial port, writes calibration, or changes torque.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .__version__ import __version__
from .utils import config

logger = logging.getLogger(__name__)

ROLE_LABELS: dict[str, str] = {
    "left_follower": "左从臂（执行）",
    "right_follower": "右从臂（执行）",
    "left_leader": "左主臂（遥操作）",
    "right_leader": "右主臂（遥操作）",
    "spare": "备用",
    "unassigned": "未分配",
}
REQUIRED_ROLES: tuple[str, ...] = (
    "left_follower",
    "right_follower",
    "left_leader",
    "right_leader",
)
ROLE_VALUES = (*REQUIRED_ROLES, "spare", "unassigned")

_BY_ID_ROOT = Path("/dev/serial/by-id")
_TTY_ROOT = Path("/sys/class/tty")
_MAX_ROLE_ENTRIES = 32
_MAX_ID_LEN = 512
_MAX_NOTE_LEN = 200


def inventory_dir() -> Path:
    return Path(config.HARDWARE_INVENTORY_PATH).expanduser()


def latest_inventory_path() -> Path:
    return inventory_dir() / "hardware-inventory-latest.json"


def _read_attr(directory: Path, name: str) -> str | None:
    try:
        value = (directory / name).read_text().strip()
    except OSError:
        return None
    return value or None


def _sysfs_info(device_node: str | None) -> dict[str, Any]:
    """Read USB identity attributes without opening the tty."""

    if not device_node:
        return {}
    device_link = _TTY_ROOT / device_node / "device"
    try:
        start = device_link.resolve(strict=True)
    except OSError:
        return {}

    candidate: Path | None = start
    for _ in range(8):
        if candidate is None:
            break
        vid = _read_attr(candidate, "idVendor")
        pid = _read_attr(candidate, "idProduct")
        if vid and pid:
            busnum = _read_attr(candidate, "busnum")
            devpath = _read_attr(candidate, "devpath")
            usb_path = f"usb-{busnum}-{devpath}" if busnum and devpath else None
            return {
                "vid": vid.lower(),
                "pid": pid.lower(),
                "serial": _read_attr(candidate, "serial"),
                "manufacturer": _read_attr(candidate, "manufacturer"),
                "product": _read_attr(candidate, "product"),
                "usb_path": usb_path,
            }
        candidate = candidate.parent if candidate.parent != candidate else None
    return {}


def _port_entry(
    *,
    entry_id: str,
    by_id: str | None,
    device: str | None,
    source: str,
    info: dict[str, Any],
    present: bool,
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "by_id": by_id,
        "device": device,
        "source": source,
        "serial": info.get("serial"),
        "vid": info.get("vid"),
        "pid": info.get("pid"),
        "manufacturer": info.get("manufacturer"),
        "product": info.get("product"),
        "usb_path": info.get("usb_path"),
        "present": present,
        "role": "unassigned",
        "note": "",
    }


def _by_id_ports() -> list[dict[str, Any]]:
    try:
        entries = sorted(_BY_ID_ROOT.iterdir(), key=lambda path: path.name)
    except OSError:
        return []

    ports: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.is_symlink():
            continue
        device = os.path.realpath(entry)
        ports.append(
            _port_entry(
                entry_id=str(entry),
                by_id=str(entry),
                device=device,
                source="by-id",
                info=_sysfs_info(Path(device).name),
                present=Path(device).exists(),
            )
        )
    return ports


def _pyserial_ports() -> list[dict[str, Any]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    ports: list[dict[str, Any]] = []
    for port in list_ports.comports():
        device = getattr(port, "device", None)
        if not device:
            continue
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        # On Linux pyserial also lists onboard ttyS* devices; only USB adapters
        # are relevant here.
        if platform.system() == "Linux" and vid is None:
            continue
        ports.append(
            _port_entry(
                entry_id=device,
                by_id=None,
                device=device,
                source="pyserial",
                info={
                    "serial": getattr(port, "serial_number", None),
                    "vid": f"{vid:04x}" if vid is not None else None,
                    "pid": f"{pid:04x}" if pid is not None else None,
                    "manufacturer": getattr(port, "manufacturer", None),
                    "product": getattr(port, "product", None),
                    "usb_path": getattr(port, "location", None) or getattr(port, "hwid", None),
                },
                present=True,
            )
        )
    return sorted(ports, key=lambda item: item["id"])


def list_serial_ports() -> list[dict[str, Any]]:
    """Enumerate current serial adapters; never opens a device."""

    if platform.system() == "Linux":
        ports = _by_id_ports()
        if ports:
            return ports
    return _pyserial_ports()


def load_latest_inventory() -> dict[str, Any] | None:
    path = latest_inventory_path()
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read hardware inventory %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _saved_ports(latest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    saved: dict[str, dict[str, Any]] = {}
    if not latest:
        return saved
    for entry in latest.get("serial_ports", []):
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            saved[entry["id"]] = entry
    return saved


def merge_inventory_ports(
    current: list[dict[str, Any]], latest: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Overlay saved roles/notes and keep saved-but-unplugged ports visible."""

    saved = _saved_ports(latest)
    current_ids = {port["id"] for port in current}
    merged: list[dict[str, Any]] = []

    for port in current:
        old = saved.get(port["id"], {})
        merged.append(
            {
                **port,
                "role": old.get("role", "unassigned"),
                "note": old.get("note", ""),
                "last_seen": latest.get("saved_at") if latest else None,
            }
        )

    for entry_id, old in saved.items():
        if entry_id in current_ids:
            continue
        merged.append(
            {
                "id": entry_id,
                "by_id": old.get("by_id"),
                "device": old.get("device"),
                "source": old.get("source"),
                "serial": old.get("serial"),
                "vid": old.get("vid"),
                "pid": old.get("pid"),
                "manufacturer": old.get("manufacturer"),
                "product": old.get("product"),
                "usb_path": old.get("usb_path"),
                "present": False,
                "role": old.get("role", "unassigned"),
                "note": old.get("note", ""),
                "last_seen": (latest or {}).get("saved_at"),
            }
        )

    merged.sort(key=lambda item: item["id"])
    return merged


def _missing_roles(ports: list[dict[str, Any]]) -> list[str]:
    assigned = {
        port.get("role")
        for port in ports
        if port.get("present") and port.get("role") in REQUIRED_ROLES
    }
    return [role for role in REQUIRED_ROLES if role not in assigned]


def _counts(ports: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(ports),
        "present": sum(1 for port in ports if port.get("present")),
        "assigned_required": sum(
            1
            for port in ports
            if port.get("present") and port.get("role") in REQUIRED_ROLES
        ),
    }


def _public_ports(ports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "id",
        "by_id",
        "device",
        "source",
        "serial",
        "vid",
        "pid",
        "manufacturer",
        "product",
        "usb_path",
        "present",
        "role",
        "note",
        "last_seen",
    )
    return [{field: port.get(field) for field in fields} for port in ports]


def current_inventory_payload() -> dict[str, Any]:
    latest = load_latest_inventory()
    ports = merge_inventory_ports(list_serial_ports(), latest)
    missing = _missing_roles(ports)
    return {
        "status": "success",
        "ports": _public_ports(ports),
        "saved_at": latest.get("saved_at") if latest else None,
        "latest_path": str(latest_inventory_path()),
        "required_roles": list(REQUIRED_ROLES),
        "role_labels": ROLE_LABELS,
        "missing_roles": missing,
        "complete": not missing,
        "counts": _counts(ports),
    }


def _validate_payload(roles: dict[str, str], notes: dict[str, str]) -> None:
    if len(roles) > _MAX_ROLE_ENTRIES or len(notes) > _MAX_ROLE_ENTRIES:
        raise ValueError("too many serial-port entries")
    for entry_id, role in roles.items():
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("serial-port id must be a non-empty string")
        if len(entry_id) > _MAX_ID_LEN:
            raise ValueError(f"serial-port id is too long: {entry_id[:40]}...")
        if not isinstance(role, str) or role not in ROLE_VALUES:
            raise ValueError(
                f"unknown role {role!r}; expected one of {', '.join(ROLE_VALUES)}"
            )
    for entry_id, note in notes.items():
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("serial-port id must be a non-empty string")
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        if len(note) > _MAX_NOTE_LEN:
            raise ValueError(f"note is too long (max {_MAX_NOTE_LEN} characters)")


def _validate_role_conflicts(ports: list[dict[str, Any]]) -> None:
    seen: dict[str, str] = {}
    for port in ports:
        role = port.get("role")
        if role not in REQUIRED_ROLES:
            continue
        owner = seen.get(role)
        if owner is not None and owner != port["id"]:
            raise ValueError(f"role {role} is assigned to both {owner} and {port['id']}")
        seen[role] = port["id"]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def save_inventory(
    roles: dict[str, str], notes: dict[str, str] | None = None
) -> dict[str, Any]:
    """Merge assignments into the current inventory and write JSON files."""

    notes = notes or {}
    _validate_payload(roles, notes)

    latest = load_latest_inventory()
    ports = merge_inventory_ports(list_serial_ports(), latest)
    by_id = {port["id"]: port for port in ports}

    for entry_id, role in roles.items():
        if entry_id not in by_id:
            by_id[entry_id] = {
                "id": entry_id,
                "by_id": entry_id if entry_id.startswith("/dev/serial/by-id/") else None,
                "device": None,
                "source": "payload",
                "serial": None,
                "vid": None,
                "pid": None,
                "manufacturer": None,
                "product": None,
                "usb_path": None,
                "present": False,
                "role": "unassigned",
                "note": "",
                "last_seen": latest.get("saved_at") if latest else None,
            }
        by_id[entry_id]["role"] = role
    for entry_id, note in notes.items():
        if entry_id not in by_id:
            continue
        by_id[entry_id]["note"] = note

    ordered = sorted(by_id.values(), key=lambda item: item["id"])
    _validate_role_conflicts(ordered)

    saved_at = datetime.now(UTC).isoformat(timespec="seconds")
    missing = _missing_roles(ordered)
    document: dict[str, Any] = {
        "schema_version": 1,
        "saved_at": saved_at,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "app_version": __version__,
        "required_roles": list(REQUIRED_ROLES),
        "role_labels": ROLE_LABELS,
        "missing_roles": missing,
        "complete": not missing,
        "counts": _counts(ordered),
        "serial_ports": _public_ports(ordered),
        "roles": {
            port["id"]: port["role"]
            for port in ordered
            if port.get("role") not in (None, "unassigned")
        },
        "notes": {
            port["id"]: port["note"]
            for port in ordered
            if port.get("note")
        },
    }

    directory = inventory_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved_path = directory / f"hardware-inventory-{stamp}.json"
    suffix = 1
    while saved_path.exists():
        saved_path = directory / f"hardware-inventory-{stamp}-{suffix}.json"
        suffix += 1
    document["file"] = str(saved_path)
    _write_json_atomic(saved_path, document)
    _write_json_atomic(latest_inventory_path(), document)
    logger.info("Saved hardware inventory to %s", saved_path)

    return {
        "status": "success",
        "saved_path": str(saved_path),
        "latest_path": str(latest_inventory_path()),
        "saved_at": saved_at,
        "complete": not missing,
        "missing_roles": missing,
        "counts": _counts(ordered),
        "ports": _public_ports(ordered),
    }
