"""Strict, versioned rig calibration validation; never probes devices."""
from __future__ import annotations

import math
from typing import Any

from .follower_calibration_asset import (invalidate_mappings_for_follower,
                                         read_mapping_invalidation,
                                         validate_follower_asset_v2)
from .mobile_teleop import CalibrationStore, canonical_sha256

KINDS = ("base", "follower", "leader", "mapping", "gamepad", "camera")
JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
ROLES = {"base": "mobile_base", "follower": "landerpi_follower", "leader": "so101_leader",
         "mapping": "leader_to_landerpi", "gamepad": "gamepad", "camera": "camera"}


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _finite_tree(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _finite_tree(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_finite_tree(v) for v in value)
    return value is None or isinstance(value, (str, bool, int))


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _ordered_pair(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(_number(x) for x in value) and value[0] < value[1]


def validate_asset(kind: str, asset: dict[str, Any]) -> list[str]:
    """Return reasons; a valid document is still not proof of physical calibration.

    Schema v1 stays the readable format for every kind.  Schema v2 exists only for
    the server-built follower asset, whose ``measured`` flag is written by
    ``follower_calibration_asset.build_follower_asset`` after a car-side job.
    """
    if kind not in KINDS or not isinstance(asset, dict):
        return ["unknown_kind_or_invalid_asset"]
    errors = []
    version = asset.get("schema_version")
    if (type(version) is not int or version not in (1, 2) or asset.get("kind") != kind):
        return ["schema_or_kind_mismatch"]
    if version == 2:
        if kind != "follower":
            # v2 exists only for the server-built follower asset.
            return ["schema_or_kind_mismatch"]
        return validate_follower_asset_v2(asset)
    if asset.get("role") != ROLES[kind]:
        errors.append("role_mismatch")
    if not _text(asset.get("identity")):
        errors.append("identity_missing")
    if asset.get("measured") is not True or CalibrationStore.unmeasured_paths(asset):
        errors.append("unmeasured_example")
    if not _finite_tree(asset):
        errors.append("nonfinite_or_non_json_value")
    if kind == "base":
        geometry = asset.get("wheel_geometry")
        if (not isinstance(geometry, dict) or not _number(geometry.get("wheel_radius_m")) or
            geometry["wheel_radius_m"] <= 0 or not _number(geometry.get("track_width_m")) or
            geometry["track_width_m"] <= 0 or not _number(geometry.get("wheelbase_m")) or geometry["wheelbase_m"] <= 0):
            errors.append("wheel_geometry_invalid")
    elif kind == "follower":
        if asset.get("joint_order") != JOINTS:
            errors.append("follower_joint_order_mismatch")
        limits = asset.get("limits_rad")
        if not isinstance(limits, dict) or set(limits) != set(JOINTS) or not all(_ordered_pair(v) for v in limits.values()):
            errors.append("follower_limits_invalid")
    elif kind == "leader":
        order, device = asset.get("joint_order"), asset.get("device_identity")
        if (not isinstance(order, list) or len(order) != 6 or not all(_text(x) for x in order) or len(set(order)) != 6):
            errors.append("leader_joint_order_invalid")
        if (not isinstance(device, dict) or not _text(device.get("serial")) or
            not isinstance(device.get("usb_by_id"), str) or not device["usb_by_id"].startswith("/dev/serial/by-id/")):
            errors.append("leader_usb_binding_invalid")
    elif kind == "mapping":
        bound, joints = asset.get("bound_identities"), asset.get("joints")
        if (asset.get("joint_order") != JOINTS or not isinstance(bound, dict) or
            not all(_text(bound.get(x)) for x in ("leader", "follower"))):
            errors.append("mapping_joints_or_binding_missing")
        if (not isinstance(joints, list) or len(joints) != 5 or
            any(not isinstance(j, dict) for j in joints) or
            [j.get("follower") for j in joints] != JOINTS[:5] or
            any(not _text(j.get("leader")) or j.get("direction") not in (-1, 1) or
                isinstance(j.get("direction"), bool) or not _number(j.get("scale")) or j["scale"] <= 0 for j in joints)):
            errors.append("mapping_joints_invalid")
        grip = asset.get("gripper")
        if (not isinstance(grip, dict) or grip.get("follower") != "r_joint" or
            grip.get("direction") not in (-1, 1) or isinstance(grip.get("direction"), bool) or
            not _number(grip.get("scale")) or grip["scale"] <= 0):
            errors.append("mapping_gripper_invalid")
    elif kind == "gamepad":
        axes = asset.get("axes")
        button = asset.get("deadman_button")
        if (not isinstance(button, int) or isinstance(button, bool) or button < 0 or
            not isinstance(axes, dict) or set(axes) != {"vx", "vy", "wz"}):
            errors.append("gamepad_mapping_invalid")
        else:
            indexes = []
            for axis in axes.values():
                if (not isinstance(axis, dict) or type(axis.get("index")) is not int or axis["index"] < 0 or
                    not all(_number(axis.get(k)) for k in ("min", "center", "max", "deadzone")) or
                    not axis["min"] < axis["center"] < axis["max"] or not 0 <= axis["deadzone"] < 1):
                    errors.append("gamepad_axis_invalid")
                    break
                indexes.append(axis["index"])
            if len(indexes) == 3 and len(set(indexes)) != 3:
                errors.append("gamepad_axis_duplicate")
    elif kind == "camera":
        intrinsics, extrinsics = asset.get("intrinsics"), asset.get("extrinsics")
        if (not isinstance(intrinsics, dict) or type(intrinsics.get("width")) is not int or intrinsics["width"] <= 0 or
            type(intrinsics.get("height")) is not int or intrinsics["height"] <= 0 or
            not isinstance(intrinsics.get("matrix"), list) or len(intrinsics["matrix"]) != 9 or
            not all(_number(x) for x in intrinsics["matrix"]) or intrinsics["matrix"][0] <= 0 or intrinsics["matrix"][4] <= 0):
            errors.append("camera_intrinsics_invalid")
        if (not isinstance(extrinsics, dict) or not _text(extrinsics.get("parent_frame")) or
            not _text(extrinsics.get("child_frame")) or
            not isinstance(extrinsics.get("translation_m"), list) or len(extrinsics["translation_m"]) != 3 or
            not all(_number(x) for x in extrinsics["translation_m"]) or
            not isinstance(extrinsics.get("quaternion_xyzw"), list) or len(extrinsics["quaternion_xyzw"]) != 4 or
            not all(_number(x) for x in extrinsics["quaternion_xyzw"]) or
            not math.isclose(sum(x*x for x in extrinsics["quaternion_xyzw"]), 1.0, rel_tol=1e-3, abs_tol=1e-3)):
            errors.append("camera_extrinsics_invalid")
    return errors


def validate_bundle(bundle: dict[str, Any], assets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    errors = []
    if (not isinstance(bundle, dict) or type(bundle.get("schema_version")) is not int or
        bundle.get("schema_version") != 1 or bundle.get("kind") != "rig_bundle" or not _finite_tree(bundle)):
        return {"ready": False, "reasons": ["bundle_schema_mismatch"], "bundle_hash": None}
    refs = bundle.get("assets")
    if not isinstance(refs, dict) or set(refs) != set(KINDS) or not isinstance(assets, dict):
        return {"ready": False, "reasons": ["bundle_assets_incomplete"], "bundle_hash": None}
    for kind in KINDS:
        asset = assets.get(kind)
        if not isinstance(asset, dict):
            errors.append(f"{kind}:missing")
            continue
        reasons = validate_asset(kind, asset)
        errors.extend(f"{kind}:{e}" for e in reasons)
        unsigned = {k: v for k, v in asset.items() if k not in ("content_sha256", "version")}
        content_hash = canonical_sha256(unsigned) if _finite_tree(unsigned) else None
        if (not content_hash or asset.get("content_sha256") != content_hash or
            asset.get("version") != content_hash[:12]):
            errors.append(f"{kind}:content_version_mismatch")
        ref = refs.get(kind)
        digest = canonical_sha256(asset) if _finite_tree(asset) else None
        if not isinstance(ref, dict) or ref.get("identity") != asset.get("identity") or ref.get("sha256") != digest:
            errors.append(f"{kind}:identity_or_hash_mismatch")
    mapping_asset = assets.get("mapping")
    mapping = mapping_asset.get("bound_identities") if isinstance(mapping_asset, dict) else None
    mapping = mapping if isinstance(mapping, dict) else {}
    for kind in ("leader", "follower"):
        asset = assets.get(kind)
        if not isinstance(asset, dict) or mapping.get(kind) != asset.get("identity"):
            errors.append(f"mapping:{kind}_binding_mismatch")
    leader = assets.get("leader")
    joints = mapping_asset.get("joints") if isinstance(mapping_asset, dict) else None
    order = leader.get("joint_order") if isinstance(leader, dict) else None
    if (isinstance(joints, list) and len(joints) == 5 and all(isinstance(j, dict) for j in joints)
        and isinstance(order, list) and len(order) == 6 and [j.get("leader") for j in joints] != order[:5]):
        errors.append("mapping:leader_joint_order_mismatch")
    return {"ready": not errors, "reasons": errors, "bundle_hash": canonical_sha256(bundle) if not errors else None}


class HybridCalibrationStore(CalibrationStore):
    """Reuse the existing atomic storage; validate before writing a hybrid asset."""

    KINDS = KINDS
    SUPPORTED_VERSIONS = (1, 2)

    def save(self, kind: str, identity: str, payload: dict[str, Any]):
        version = payload.get("schema_version", 1)
        candidate = {**payload, "schema_version": version, "kind": kind, "identity": identity}
        errors = validate_asset(kind, candidate)
        if errors:
            raise ValueError(", ".join(errors))
        record = super().save(kind, identity, candidate, schema_version=version)
        if kind == "mapping":
            # A rewritten mapping is a new pairing; the old invalidation no longer applies.
            self.clear_invalidation("mapping", identity)
        return record

    def save_validated(self, kind: str, identity: str, payload: dict[str, Any]):
        return self.save(kind, identity, payload)

    def load(self, kind: str, identity: str):
        marker = self.invalidation(kind, identity) if kind == "mapping" else None
        if marker is not None:
            raise ValueError(
                f"{kind} calibration invalidated by {marker.get('reason')}"
                f" (follower_version={marker.get('follower_version')}); "
                f"{marker.get('action_required', 're_pair_required')}")
        record = super().load(kind, identity)
        errors = validate_asset(kind, record.data)
        if record.data.get("identity") != identity or record.version != record.sha256[:12]:
            errors.append("identity_or_version_mismatch")
        if errors:
            raise ValueError(", ".join(errors))
        return record

    def invalidate(self, kind: str, identity: str, *, reason: str,
                   follower_identity: str | None = None,
                   follower_version: str | None = None) -> dict[str, Any]:
        path = self._path(kind, identity)
        if not path.is_file():
            return {"invalidated": False, "reason": "missing"}
        markers = invalidate_mappings_for_follower(
            self.root, follower_identity=follower_identity or identity,
            follower_version=follower_version or "")
        return {"invalidated": bool(markers), "markers": markers}

    def invalidation(self, kind: str, identity: str) -> dict[str, Any] | None:
        return read_mapping_invalidation(self.root, identity)

    def clear_invalidation(self, kind: str, identity: str) -> None:
        marker = self.root / kind / f"{identity.replace('/', '_')}.invalidated.json"
        if marker.is_file():
            marker.unlink()
