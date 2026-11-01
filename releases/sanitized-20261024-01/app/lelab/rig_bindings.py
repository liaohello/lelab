"""Apply saved serial roles to the existing left/right rig records.

This is a configuration-only helper: it never opens a port, writes calibration,
or changes torque.  The serial roles come from the read-only hardware
inventory, while the robot records remain the source of calibration bindings.
"""

from __future__ import annotations

import logging
from typing import Any

from .hardware_inventory import REQUIRED_ROLES, current_inventory_payload
from .utils import config

logger = logging.getLogger(__name__)

DEFAULT_LEFT_ROBOT = "left_rig"
DEFAULT_RIGHT_ROBOT = "right_rig"
SIDES = ("left", "right")


def _role_ports(ports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for port in ports:
        role = port.get("role")
        if role not in REQUIRED_ROLES or not port.get("present"):
            continue
        if role in bindings:
            raise ValueError(f"role {role} is bound to more than one present port")
        bindings[role] = port
    return bindings


def describe_rig_bindings(
    left_robot: str = DEFAULT_LEFT_ROBOT,
    right_robot: str = DEFAULT_RIGHT_ROBOT,
) -> dict[str, Any]:
    """Compare saved role ports with the two named robot records."""

    names = {"left": left_robot, "right": right_robot}
    for name in names.values():
        if not config.is_valid_robot_name(name):
            raise ValueError(f"invalid robot record name: {name!r}")

    inventory = current_inventory_payload()
    role_ports = _role_ports(inventory["ports"])
    missing_roles = [role for role in REQUIRED_ROLES if role not in role_ports]
    bindings: list[dict[str, Any]] = []

    for side in SIDES:
        robot_name = names[side]
        record = config.get_robot_record(robot_name)
        follower_role = f"{side}_follower"
        leader_role = f"{side}_leader"
        proposed_follower = role_ports.get(follower_role, {}).get("by_id")
        proposed_leader = role_ports.get(leader_role, {}).get("by_id")
        current_follower = (record or {}).get("follower_port", "")
        current_leader = (record or {}).get("leader_port", "")

        changes: dict[str, str] = {}
        if proposed_follower and current_follower != proposed_follower:
            changes["follower_port"] = proposed_follower
        if proposed_leader and current_leader != proposed_leader:
            changes["leader_port"] = proposed_leader

        status = "ready"
        if record is None:
            status = "record_missing"
        elif follower_role not in role_ports or leader_role not in role_ports:
            status = "roles_incomplete"
        elif not changes:
            status = "unchanged"

        bindings.append(
            {
                "side": side,
                "robot_name": robot_name,
                "record_exists": record is not None,
                "leader_role": leader_role,
                "follower_role": follower_role,
                "leader_port": {
                    "role": leader_role,
                    "serial": role_ports.get(leader_role, {}).get("serial"),
                    "current": current_leader,
                    "proposed": proposed_leader,
                },
                "follower_port": {
                    "role": follower_role,
                    "serial": role_ports.get(follower_role, {}).get("serial"),
                    "current": current_follower,
                    "proposed": proposed_follower,
                },
                "configs": {
                    "leader": (record or {}).get("leader_config", ""),
                    "follower": (record or {}).get("follower_config", ""),
                },
                "calibration": (
                    config.robot_record_diagnostics(record)
                    if record is not None
                    else None
                ),
                "changes": changes,
                "status": status,
            }
        )

    can_apply = not missing_roles and all(binding["record_exists"] for binding in bindings)
    return {
        "status": "success",
        "left_robot": left_robot,
        "right_robot": right_robot,
        "inventory_saved_at": inventory.get("saved_at"),
        "inventory_complete": inventory.get("complete", False),
        "missing_roles": missing_roles,
        "can_apply": can_apply,
        "bindings": bindings,
    }


def apply_rig_bindings(
    left_robot: str = DEFAULT_LEFT_ROBOT,
    right_robot: str = DEFAULT_RIGHT_ROBOT,
) -> dict[str, Any]:
    """Persist proposed ports into the two robot records, preserving configs."""

    described = describe_rig_bindings(left_robot, right_robot)
    if described["missing_roles"]:
        raise ValueError("save all four required serial roles first")
    if not described["can_apply"]:
        raise ValueError(
            "robot records are missing: "
            + ", ".join(
                binding["robot_name"]
                for binding in described["bindings"]
                if not binding["record_exists"]
            )
        )

    updated: list[str] = []
    unchanged: list[str] = []
    for binding in described["bindings"]:
        if not binding["changes"]:
            unchanged.append(binding["robot_name"])
            continue
        payload = {
            "leader_port": binding["leader_port"]["proposed"],
            "follower_port": binding["follower_port"]["proposed"],
        }
        if not config.save_robot_record(
            binding["robot_name"], payload, allow_create=False
        ):
            raise RuntimeError(
                f"could not update robot record {binding['robot_name']!r}"
            )
        updated.append(binding["robot_name"])

    logger.info("Applied hardware role ports to robot records: %s", updated)
    return {
        "status": "success",
        "updated": updated,
        "unchanged": unchanged,
        "result": describe_rig_bindings(left_robot, right_robot),
    }
