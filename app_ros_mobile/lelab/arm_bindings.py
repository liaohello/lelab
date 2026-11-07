"""Which calibration was measured on which physical arm port.

Every SO-101 arm on this bench uses motor ids 1..6 under the same six joint
names, so a calibration from one arm loads onto another without any complaint
from lerobot: ``FeetechMotorsBus.write_calibration`` iterates the calibration by
*motor name* and writes through the bus's own name→id map, never comparing the
``id`` stored in each entry. The angles that come back are smooth, plausible and
belong to a different arm.

The only stable identity signal is the USB adapter: the ``/dev/serial/by-id``
path is what distinguishes these arms. So when the calibration wizard finishes
measuring a port, this module records the role (leader/follower), the calibration
file name and a content hash for that exact path. Starting desktop
teleoperation then refuses the combinations that contradict that evidence, and
reports the ones it simply has no evidence for.

Read-only for anything but ``record``: no serial traffic, no board writes.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .mobile_teleop import canonical_sha256
from .utils import config as _config

# Same vocabulary the calibration wizard uses for the two sides of a rig.
DEVICE_TYPE_TO_ROLE = {"teleop": "leader", "robot": "follower"}
SCHEMA_VERSION = 1


def bindings_path() -> Path:
    """Resolved on every call: ``LELAB_ARM_BINDINGS`` overrides the table, and the
    default follows ``HF_LEROBOT_HOME`` like the rest of the persisted state.
    """
    override = os.getenv("LELAB_ARM_BINDINGS")
    if override:
        return Path(override).expanduser()
    home = Path(os.getenv("HF_LEROBOT_HOME", "~/.cache/huggingface/lerobot")).expanduser()
    return home / "lelab" / "arm_bindings.json"


def config_name(value: str) -> str:
    """One spelling for a calibration file name: ``left_rig``, ``left_rig.json`` and a
    full path must all compare equal, and all point at the same file on disk."""
    name = os.path.splitext(os.path.basename(str(value or "")))[0]
    return f"{name}.json" if name else ""


def load(path: Path | None = None) -> dict[str, Any]:
    """The whole binding table, or an empty table if nothing has been recorded yet."""
    file = path or bindings_path()
    if not file.is_file():
        return {"schema_version": SCHEMA_VERSION, "ports": {}}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # A damaged table must not block teleoperation: the operator's own
        # calibration files are the source of truth, this is supporting evidence.
        return {"schema_version": SCHEMA_VERSION, "ports": {}, "unreadable": str(file), "error": str(exc)}
    if not isinstance(data, dict) or not isinstance(data.get("ports"), dict):
        return {"schema_version": SCHEMA_VERSION, "ports": {}, "unreadable": str(file)}
    return data


def record(port: str, device_type: str, config_file: str, calibration: dict[str, Any],
           path: Path | None = None) -> dict[str, Any]:
    """Bind <port> to the calibration just measured on it."""
    role = DEVICE_TYPE_TO_ROLE.get(device_type)
    if role is None:
        raise ValueError(f"unknown device_type {device_type!r}")
    if not port:
        raise ValueError("a port is required to record a binding")
    file = path or bindings_path()
    table = load(file)
    entry = {
        "role": role,
        "config": config_name(config_file),
        "content_sha256": canonical_sha256(calibration or {}),
        "ids": {name: body["id"] for name, body in (calibration or {}).items()
                if isinstance(body, dict) and body.get("id") is not None},
        "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Keyed by the path the wizard was given. A /dev/serial/by-id name is what stays
    # stable across replugs, so the workbench passes those; a bare /dev/ttyACM0 binds
    # under its own key and simply shows up as "no recorded measurement" later.
    table["ports"][port] = entry
    table["schema_version"] = SCHEMA_VERSION
    _config._atomic_write_text(str(file), json.dumps(table, indent=2, sort_keys=True))
    return entry


def describe(port: str, path: Path | None = None) -> dict[str, Any] | None:
    return load(path)["ports"].get(port)


# The two sides live in different lerobot calibration trees; a robot record names
# the file in each ("left_rig.json"), a teleoperation request names the same file
# possibly with a sub-path. Read through the config module so a redirected
# HF_LEROBOT_HOME (tests, a second bench) is honoured instead of a stale snapshot.
def config_dir(role: str) -> str | None:
    if role == "leader":
        return _config.LEADER_CONFIG_PATH
    if role == "follower":
        return _config.FOLLOWER_CONFIG_PATH
    return None


def calibration_path(role: str, config_file: str) -> str | None:
    """Absolute path of a side's calibration file, or None for an unsafe name.

    ``..`` and ``/`` are refused: this feeds a read-only endpoint, so a config
    name must not be able to walk outside the calibration tree.
    """
    directory = config_dir(role)
    name = os.path.splitext(str(config_file or ""))[0]
    if directory is None or not name or any(bad in name for bad in ("/", "\\", "..")):
        return None
    return os.path.join(directory, f"{name}.json")


def read_calibration(role: str, config_file: str, cal_path: str | None = None) -> dict[str, Any] | None:
    """Parsed calibration contents, or None when absent, unsafe or unparseable.

    ``cal_path`` overrides the tree lookup: the start path hands over the exact file
    lerobot resolved for the device, so the guard reads what is about to be written.
    """
    path = cal_path or calibration_path(role, config_file)
    if path is None or not os.path.isfile(path):
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def side(port: str, role: str, config_file: str, bus: Any | None = None,
         cal_path: str | None = None) -> dict[str, Any]:
    """Assemble one ``check_pair`` side from what the caller already knows.

    ``bus`` is the lerobot bus the request would drive; its motor ids are what the
    calibration has to agree with. A bus without a motor table (a test double, a
    driver that has not built one yet) contributes no ids, so the id comparison is
    skipped rather than inventing ids the hardware never asked for.
    """
    motors = getattr(bus, "motors", None) or {}
    return {
        "port": port or "",
        "role": role,
        "config": config_name(cal_path or config_file),
        "calibration": read_calibration(role, config_file, cal_path),
        "bus_ids": {name: motor.id for name, motor in motors.items()},
    }


def check_pair(leader: dict[str, Any], follower: dict[str, Any],
               path: Path | None = None) -> dict[str, Any]:
    """Compare one teleoperation request against the measured bindings.

    Each side is ``{"port", "role", "config", "calibration", "bus_ids"}``: the port
    the operator picked, which side it is being used as, the calibration file name
    selected for it, that file's parsed contents and the motor ids the bus will
    actually poll. ``refusals`` are contradictions with recorded evidence — an arm
    driven with another arm's numbers, or one port claimed by both arms.
    ``warnings`` are cases with no evidence either way (calibrated before this table
    existed, or the file edited by hand afterwards) and never block a working bench.
    """
    refusals: list[str] = []
    warnings: list[str] = []
    # Imported here because mobile_leader_reader pulls in fcntl/termios at module
    # level; this guard's id comparison is pure Python, but the desktop app also
    # runs on Windows, where importing it at the top would break every route.
    from .mobile_leader_reader import calibration_id_mismatches

    if leader["port"] and leader["port"] == follower["port"]:
        refusals.append(f"leader and follower name the same port: {leader['port']}")

    ports = load(path)["ports"]
    for label, requested in (("leader", leader), ("follower", follower)):
        port, calibration = requested["port"], requested["calibration"]
        role = requested.get("role") or label
        if not calibration:
            warnings.append(f"{label} calibration file could not be read for {port or 'an unset port'}")
            continue
        bus_ids = requested.get("bus_ids") or {}
        # A bus without a motor table contributes no ids; comparing against nothing
        # would flag every motor as unpolled, so skip the check instead of inventing
        # a refusal.
        disagreements = calibration_id_mismatches(calibration, bus_ids) if bus_ids else []
        if disagreements:
            refusals.append(f"{label} calibration does not match the motors on {port}: "
                            + ", ".join(disagreements[:6]))

        entry = ports.get(port)
        if entry is None:
            warnings.append(f"{port or label} has no recorded measurement on this host: this app cannot "
                            "prove the selected calibration belongs to this arm")
            continue
        if entry.get("role") != role:
            refusals.append(
                f"{label} port {port} was last calibrated as the {entry.get('role')}; "
                f"{entry.get('config')} was measured on the other side of that rig"
            )
        elif requested["config"] and entry.get("config") != requested["config"]:
            refusals.append(
                f"{label} port {port} was measured as {entry.get('config')}, but {requested['config']} "
                "is selected for it"
            )
        elif entry.get("content_sha256") and entry["content_sha256"] != canonical_sha256(calibration):
            warnings.append(
                f"{label} calibration on {port} changed since it was measured "
                f"(recorded {entry['content_sha256'][:12]}, now {canonical_sha256(calibration)[:12]}); "
                "re-check the arm before enabling motion"
            )

    return {"refusals": refusals, "warnings": warnings, "ready": not refusals,
            "ports": {wanted["port"]: ports.get(wanted["port"])
                      for wanted in (leader, follower) if wanted["port"]}}


class ArmIdentityError(RuntimeError):
    """The requested pair contradicts what was measured — nothing may be written."""


def check_desktop_pair(leader_port: str, follower_port: str, leader_device, follower_device,
                       leader_config: str = "", follower_config: str = "") -> dict[str, Any]:
    """Check one desktop leader/follower request and raise on a contradiction.

    Takes the two lerobot devices rather than a hand-assembled pair: each device knows the
    exact calibration file it is about to load (``calibration_fpath``) and the motor ids it
    will poll, which is the only comparison that means anything here. Call this after
    constructing the devices and before the first ``bus.connect()``, so a refusal never
    opens a port or writes a board. Both the teleoperation and the recording entry points
    go through here — a hole in one of them would let the same wrong calibration through.
    """
    verdict = check_pair(
        side(leader_port, "leader", leader_config, getattr(leader_device, "bus", None),
             str(getattr(leader_device, "calibration_fpath", "") or "") or None),
        side(follower_port, "follower", follower_config, getattr(follower_device, "bus", None),
             str(getattr(follower_device, "calibration_fpath", "") or "") or None),
    )
    if verdict["refusals"]:
        raise ArmIdentityError(
            "Refusing to start teleoperation — " + "; ".join(verdict["refusals"])
            + ". Re-run the calibration for the arm you plugged in, or pick the port and "
            "calibration that belong together."
        )
    return verdict


def workbench_inventory() -> dict[str, Any]:
    """What this host can prove about the arms plugged in right now.

    Read-only: it lists serial paths, the calibration files on disk and the binding
    table. It does not open a port, so the *hardware's* motor ids are not part of the
    answer — the start path compares those against the calibration.
    """
    from .mobile_leader_reader import list_leader_ports
    from .utils.config import find_available_ports

    adapters = {row["by_id"]: row for row in list_leader_ports()}
    ports = []
    for path in find_available_ports():
        # Without /dev/serial/by-id the port list falls back to every TTY the OS
        # exposes, including the motherboard's /dev/ttyS0..31 — nodes that exist
        # whether or not anything is attached. An SO-101 is always a USB adapter, so
        # those would be listed as arms the operator could "pick". COM ports on
        # Windows are kept: the exclusion names Linux built-in UARTs only.
        if re.match(r"^/dev/ttyS\d+$", path):
            continue
        adapter = adapters.get(path) or {}
        ports.append({
            "port": path,
            "connected": os.path.exists(path),
            "device": adapter.get("device", ""),
            "serial": adapter.get("serial", ""),
            "binding": describe(path),
        })

    bound = load()["ports"]
    calibrations = {}
    for role in ("leader", "follower"):
        directory = config_dir(role)
        rows = []
        for name in sorted(os.listdir(directory)) if directory and os.path.isdir(directory) else []:
            if not name.endswith(".json"):
                continue
            contents = read_calibration(role, name)
            rows.append({
                "config": name,
                "readable": contents is not None,
                "path": calibration_path(role, name),
                "ids": {motor: body["id"] for motor, body in (contents or {}).items()
                        if isinstance(body, dict) and body.get("id") is not None},
                "content_sha256": canonical_sha256(contents) if contents else "",
                # Which physical ports this file was measured on, so picking a file
                # shows the arms it belongs to rather than a bare name.
                "bound_ports": [port for port, entry in bound.items()
                                if entry.get("role") == role and entry.get("config") == config_name(name)],
            })
        calibrations[role] = rows

    return {"bindings_file": str(bindings_path()), "ports": ports, "calibrations": calibrations,
            "note": "paths and files read from this host only; no port was opened"}
