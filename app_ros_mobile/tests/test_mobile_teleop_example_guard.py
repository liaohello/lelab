"""A saved template must not read as a calibrated layer.

需求四 forbids silently using default values when the device or mapping does not match. The page's
"载入模板" puts the shipped example document straight into the editor, so the store has to know the
difference between "saved" and "measured" - otherwise load, save, and the whole calibration gate
passes on placeholder offsets, directions and soft limits.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from lelab.mobile_teleop import CalibrationStore

EXAMPLES = Path(__file__).resolve().parents[2] / "configs" / "mobile_teleop"
MARKERS = CalibrationStore.UNMEASURED_PREFIXES


def example(kind: str) -> dict:
    return json.loads((EXAMPLES / f"{kind}.example.json").read_text(encoding="utf-8"))


def measure(node):
    """What the operator is expected to do: drop the flag and rewrite the provenance note."""
    if isinstance(node, dict):
        out = {k: measure(v) for k, v in node.items() if k != CalibrationStore.UNMEASURED_FLAG}
        return out
    if isinstance(node, list):
        return [measure(v) for v in node]
    if isinstance(node, str) and node.strip().startswith(MARKERS):
        return "measured on the car 2026-11-03"
    return node


def save_three(store: CalibrationStore, docs: dict[str, dict]) -> None:
    docs["mapping"]["bound_identities"] = {"leader": "L", "follower": "F"}
    for kind, identity in (("leader", "L"), ("follower", "F"), ("mapping", "M")):
        store.save(kind, identity, docs[kind])


def test_the_shipped_templates_refuse_to_pass_readiness(tmp_path):
    store = CalibrationStore(tmp_path)
    save_three(store, {kind: example(kind) for kind in ("leader", "follower", "mapping")})
    report = store.readiness("L", "F", "M")
    assert not report["ready"], "an unedited template must never be treated as calibrated"
    blocked = {name for name in ("leader", "follower", "mapping")
               if any(r.startswith(f"{name} layer still holds shipped example values") for r in report["reasons"])}
    assert blocked == {"leader", "follower", "mapping"}, report["reasons"]


def test_only_the_markers_separate_the_template_from_a_ready_layer(tmp_path):
    """The same documents pass once the flags are dropped and the notes rewritten - the guard is
    about the marker, not about the template's shape."""
    store = CalibrationStore(tmp_path)
    save_three(store, {kind: measure(example(kind)) for kind in ("leader", "follower", "mapping")})
    report = store.readiness("L", "F", "M")
    assert report["reasons"] == [] and report["ready"], report["reasons"]
    assert report["calibration_version"].count("|") == 2


def test_the_scan_names_the_exact_json_path():
    payload = {"joints": [{"name": "shoulder_pan"}, {"name": "wrist_roll", "_example_not_measured": True}],
               "gripper": {"span_note": "example_not_measured: 5.0 pulses per 1 percent"}}
    assert CalibrationStore.unmeasured_paths(payload) == ["joints[1]", "gripper.span_note"]


def test_prose_that_describes_the_convention_is_not_a_marker():
    """A note written *about* the convention after measuring must not lock the operator out."""
    layer = {"_note": "every value that was marked example_not_measured has been replaced with a reading",
             "reference_pose_rad": [0.0, 0.8, -2.0, -1.2, 0.0]}
    assert CalibrationStore.unmeasured_paths(copy.deepcopy(layer)) == []
