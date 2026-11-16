"""Read-only camera inventory and stable-name binding.

Cameras are identified by their ``/dev/v4l/by-id`` links rather than the
mutable ``/dev/videoN`` number. The page lets an operator view one camera at a
time and assign the dataset name (for example ``left_wrist``).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .utils import config

logger = logging.getLogger(__name__)

DEFAULT_CAMERA_KEYS = (
    "front",
    "left_wrist",
    "right_wrist",
    "up",
    "right",
    "unassigned",
)
_CAMERA_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_MAX_CAMERAS = 16
_MAX_LABEL = 60
_BY_ID_ROOT = Path("/dev/v4l/by-id")
_BY_PATH_ROOT = Path("/dev/v4l/by-path")
_SYSFS_ROOT = Path("/sys/class/video4linux")


def inventory_dir() -> Path:
    return Path(config.CAMERA_INVENTORY_PATH).expanduser()


def latest_inventory_path() -> Path:
    return inventory_dir() / "camera-inventory-latest.json"


def _read_attr(directory: Path, name: str) -> str | None:
    try:
        value = (directory / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _from_link(link: Path, root_kind: str) -> dict[str, Any] | None:
    try:
        target = link.resolve(strict=True)
    except OSError:
        return None
    name = target.name
    if not name.startswith("video"):
        return None
    try:
        index = int(name.removeprefix("video"))
    except ValueError:
        return None
    device = Path("/dev") / name
    return {
        "id": str(link),
        "stable_source": root_kind,
        "by_id": str(link) if root_kind == "by-id" else None,
        "by_path": str(link) if root_kind == "by-path" else None,
        "device": str(device),
        "index": index,
        "name": _read_attr(_SYSFS_ROOT / name, "name") or f"Camera {index}",
        "present": device.exists(),
        "camera_key": "unassigned",
        "label": "",
        "confirmed_at": None,
        "note": "",
    }


def _fallback_sysfs_ports() -> list[dict[str, Any]]:
    ports = []
    for node in sorted(_SYSFS_ROOT.glob("video*"), key=lambda item: item.name):
        capture_index = _read_attr(node, "index")
        if capture_index != "0":
            continue
        try:
            index = int(node.name.removeprefix("video"))
        except ValueError:
            continue
        device = Path("/dev") / node.name
        ports.append(
            {
                "id": str(device),
                "stable_source": "sysfs",
                "by_id": None,
                "by_path": None,
                "device": str(device),
                "index": index,
                "name": _read_attr(node, "name") or f"Camera {index}",
                "present": device.exists(),
                "camera_key": "unassigned",
                "label": "",
                "confirmed_at": None,
                "note": "",
            }
        )
    return ports


def list_camera_ports() -> list[dict[str, Any]]:
    """Enumerate capture devices without opening ``VideoCapture``."""

    if platform.system() != "Linux":
        return []

    seen_indexes: set[int] = set()
    ports: list[dict[str, Any]] = []
    for root, kind in ((_BY_ID_ROOT, "by-id"), (_BY_PATH_ROOT, "by-path")):
        if not root.is_dir():
            continue
        for link in sorted(root.glob("*-video-index0"), key=lambda item: item.name):
            entry = _from_link(link, kind)
            if entry is None or entry["index"] in seen_indexes:
                continue
            seen_indexes.add(entry["index"])
            ports.append(entry)

    if not ports:
        ports = _fallback_sysfs_ports()

    return sorted(ports, key=lambda item: (item["index"], item["id"]))


def load_latest_inventory() -> dict[str, Any] | None:
    path = latest_inventory_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read camera inventory %s: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _saved_ports(latest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    saved: dict[str, dict[str, Any]] = {}
    if not latest:
        return saved
    for entry in latest.get("cameras", []):
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            saved[entry["id"]] = entry
    return saved


def merge_inventory_cameras(
    current: list[dict[str, Any]], latest: dict[str, Any] | None
) -> list[dict[str, Any]]:
    saved = _saved_ports(latest)
    current_ids = {port["id"] for port in current}
    merged = []
    for port in current:
        old = saved.get(port["id"], {})
        merged.append(
            {
                **port,
                "camera_key": old.get("camera_key", "unassigned"),
                "label": old.get("label", ""),
                # A confirmation belongs to the physical device: if it is
                # unplugged the operator has to confirm the picture again.
                "confirmed_at": old.get("confirmed_at") if port["present"] else None,
                "note": old.get("note", ""),
            }
        )
    for camera_id, old in saved.items():
        if camera_id in current_ids:
            continue
        merged.append(
            {
                "id": camera_id,
                "stable_source": old.get("stable_source"),
                "by_id": old.get("by_id"),
                "by_path": old.get("by_path"),
                "device": old.get("device"),
                "index": old.get("index"),
                "name": old.get("name"),
                "present": False,
                "camera_key": old.get("camera_key", "unassigned"),
                "label": old.get("label", ""),
                "confirmed_at": None,
                "note": old.get("note", ""),
            }
        )
    return sorted(merged, key=lambda item: item["id"])


def _valid_key(value: str) -> bool:
    return isinstance(value, str) and bool(_CAMERA_NAME_RE.fullmatch(value))


def _validate_payload(assignments: dict[str, str]) -> None:
    if len(assignments) > _MAX_CAMERAS:
        raise ValueError("too many camera assignments")
    for camera_id, key in assignments.items():
        if not isinstance(camera_id, str) or not camera_id:
            raise ValueError("camera id must be a non-empty string")
        if not _valid_key(key):
            raise ValueError(
                f"camera key {key!r} must start with a letter and contain only letters, digits or underscores"
            )
    assigned_keys = [key for key in assignments.values() if key != "unassigned"]
    duplicates = sorted({key for key in assigned_keys if assigned_keys.count(key) > 1})
    if duplicates:
        raise ValueError("duplicate camera keys: " + ", ".join(duplicates))


def current_inventory_payload() -> dict[str, Any]:
    latest = load_latest_inventory()
    cameras = merge_inventory_cameras(list_camera_ports(), latest)
    assigned = {
        camera["camera_key"]
        for camera in cameras
        if camera.get("present") and camera.get("camera_key") != "unassigned"
    }
    return {
        "status": "success",
        "cameras": cameras,
        "saved_at": latest.get("saved_at") if latest else None,
        "latest_path": str(latest_inventory_path()),
        "recommended_keys": list(DEFAULT_CAMERA_KEYS),
        "missing_recommended": [
            key for key in ("front", "left_wrist", "right_wrist") if key not in assigned
        ],
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def save_inventory(
    assignments: dict[str, str],
    notes: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    confirmations: dict[str, bool] | None = None,
) -> dict[str, Any]:
    notes = notes or {}
    _validate_payload(assignments)

    latest = load_latest_inventory()
    cameras = merge_inventory_cameras(list_camera_ports(), latest)
    by_id = {camera["id"]: camera for camera in cameras}
    for camera_id, key in assignments.items():
        if camera_id not in by_id:
            raise ValueError(f"unknown camera id: {camera_id}")
        by_id[camera_id]["camera_key"] = key
    for camera_id, note in notes.items():
        if camera_id not in by_id:
            continue
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError("camera notes must be strings of at most 200 characters")
        by_id[camera_id]["note"] = note
    for camera_id, label in (labels or {}).items():
        if camera_id not in by_id:
            raise ValueError(f"unknown camera id: {camera_id}")
        if not isinstance(label, str) or len(label.strip()) > _MAX_LABEL:
            raise ValueError(f"camera labels must be strings of at most {_MAX_LABEL} characters")
        by_id[camera_id]["label"] = label.strip()

    saved_at = datetime.now(UTC).isoformat(timespec="seconds")
    for camera_id, confirmed in (confirmations or {}).items():
        if camera_id not in by_id:
            raise ValueError(f"unknown camera id: {camera_id}")
        camera = by_id[camera_id]
        if not confirmed:
            camera["confirmed_at"] = None
            continue
        if not camera.get("present"):
            raise ValueError(f"{camera_id} is not connected; its picture cannot be confirmed")
        if camera.get("camera_key", "unassigned") == "unassigned":
            raise ValueError(f"assign a dataset field to {camera_id} before confirming it")
        camera["confirmed_at"] = saved_at

    ordered = sorted(by_id.values(), key=lambda item: item["id"])
    assigned = {
        camera["camera_key"]
        for camera in ordered
        if camera.get("present") and camera["camera_key"] != "unassigned"
    }
    document = {
        "schema_version": 1,
        "saved_at": saved_at,
        "recommended_keys": list(DEFAULT_CAMERA_KEYS),
        "missing_recommended": [
            key for key in ("front", "left_wrist", "right_wrist") if key not in assigned
        ],
        "cameras": ordered,
    }

    directory = inventory_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved_path = directory / f"camera-inventory-{stamp}.json"
    suffix = 1
    while saved_path.exists():
        saved_path = directory / f"camera-inventory-{stamp}-{suffix}.json"
        suffix += 1
    document["file"] = str(saved_path)
    _write_json_atomic(saved_path, document)
    _write_json_atomic(latest_inventory_path(), document)
    return {
        "status": "success",
        "saved_path": str(saved_path),
        "latest_path": str(latest_inventory_path()),
        "saved_at": saved_at,
        "cameras": ordered,
        "missing_recommended": document["missing_recommended"],
    }


def collection_binding() -> dict[str, Any]:
    """Describe the stable camera bindings a recording session should use.

    ``source`` prefers the ``/dev/v4l/by-id`` link so a re-plugged device keeps
    its dataset field instead of silently following a mutable ``/dev/videoN``
    index.  A binding counts as ready only when the device is present and the
    operator has confirmed its picture.
    """

    payload = current_inventory_payload()
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    unconfirmed: list[dict[str, Any]] = []
    for camera in payload["cameras"]:
        key = camera.get("camera_key", "unassigned")
        if key == "unassigned":
            continue
        summary = {
            "camera_key": key,
            "label": camera.get("label") or key,
            "name": camera.get("name"),
            "id": camera["id"],
            "device": camera.get("device"),
            "index": camera.get("index"),
            "confirmed_at": camera.get("confirmed_at"),
        }
        if not camera.get("present"):
            missing.append(summary)
            continue
        if not camera.get("confirmed_at"):
            unconfirmed.append(summary)
        rows.append(
            {
                **summary,
                "source": camera.get("by_id")
                or camera.get("by_path")
                or camera.get("device"),
                "ready": bool(camera.get("confirmed_at")),
            }
        )
    return {
        "status": "success",
        "saved_at": payload.get("saved_at"),
        "cameras": rows,
        "missing": missing,
        "unconfirmed": unconfirmed,
        "ready": bool(rows) and not missing and not unconfirmed,
    }
