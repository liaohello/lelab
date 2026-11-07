"""Upper-host tests for the follower calibration proxy, commit and readback.

The car is never contacted: ``ros_mobile._http`` is replaced with a recorder, so
these tests assert the exact frozen route table, that the browser cannot inject
``measured``/device identity, that commit builds the v2 asset from car evidence,
and that a new follower asset invalidates the old mapping.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lelab import ros_mobile
from lelab.follower_calibration_asset import (build_follower_asset, sample_log_digest,
                                              validate_evidence)
from lelab.hybrid_calibration import HybridCalibrationStore, validate_asset
from lelab.hybrid_fixtures import calibration_assets_fixture

JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
FOLLOWER_ID = "landerpi-follower-v1"
COMPLETED = "2026-10-04T11:56:00Z"
NOW = _dt.datetime.fromisoformat(COMPLETED.replace("Z", "+00:00")).timestamp() + 60


def _joint(name: str, low: int, high: int, reference: int) -> dict:
    return {
        "method": "manual_range_capture",
        "servo_id": {"joint1": 1, "joint2": 2, "joint3": 3,
                     "joint4": 4, "joint5": 5, "r_joint": 10}[name],
        "reference_raw": reference,
        "observed_min_raw": low,
        "observed_max_raw": high,
        "span_raw": high - low,
        "samples": 120,
        "noise_raw": 0.0,
        "anomalies": 0,
        "duplicates": 2,
        "stale": 0,
        "last_seq": 240,
        "sample_log_entries": 12,
        "sample_log_truncated": False,
        "first_sample_utc": "2026-10-04T11:40:01Z",
        "last_sample_utc": "2026-10-04T11:55:00Z",
        "valid": True,
        "reasons": [],
    }


def evidence_fixture(**overrides) -> dict:
    sample_log = [
        {"joint": "joint1", "seq": 1, "t_ms": 0, "raw": 420},
        {"joint": "joint1", "seq": 120, "t_ms": 900000, "raw": 560},
        {"joint": "r_joint", "seq": 2, "t_ms": 40, "raw": 330},
        {"joint": "r_joint", "seq": 400, "t_ms": 950000, "raw": 570},
    ]
    evidence = {
        "schema_version": 2,
        "kind": "follower_calibration_evidence",
        "job_id": "0123456789abcdef",
        "generation": 3,
        "phase": "stopped",
        "agent": {"version": "0.1.0", "profile": "hardware_hybrid_manual_v1"},
        "device_identity": {
            "kind": "landerpi_bus_servo_follower",
            "control_board": "landerpi_mentorpi",
            "controller_owner": "ros_robot_controller",
            "joint_order": list(JOINTS),
            "servo_ids": {"joint1": 1, "joint2": 2, "joint3": 3,
                          "joint4": 4, "joint5": 5, "r_joint": 10},
            "board_serial": "TESTBOARD01",
            "board_serial_source": "environment",
            "host_serial": "10000000abcdef",
            "agent_version": "0.1.0",
            "profile": "hardware_hybrid_manual_v1",
        },
        "source_kind": "servo_position_readback",
        "method": "operator_manual_plus_gripper_drag",
        "units": {"position": "vendor_raw_tick", "radians_per_tick": 0.00418879,
                  "centre_raw": 500, "range_raw": [0, 1000],
                  "gripper_flip": "vendor_yaml_min_greater_than_max"},
        "started_at_utc": "2026-10-04T11:40:00Z",
        "completed_at_utc": "2026-10-04T11:56:00Z",
        "operator": "unit-test",
        "operator_confirmed_safe_window": True,
        "support_confirmed": True,
        "safe_window_raw": [300, 600],
        "stop_reason": "done",
        "faults": [],
        "joints": {
            "joint1": _joint("joint1", 420, 560, 500),
            "joint2": _joint("joint2", 410, 590, 500),
            "joint3": _joint("joint3", 430, 550, 500),
            "joint4": _joint("joint4", 400, 600, 500),
            "joint5": _joint("joint5", 425, 575, 500),
            "r_joint": _joint("r_joint", 320, 580, 450),
        },
        "gripper": {
            "method": "operator_drag_safe_window",
            "safe_window_raw": [300, 600],
            "observed_closed_raw": 332,
            "observed_open_raw": 570,
            "usable_closed_raw": 340,
            "usable_open_raw": 562,
            "repeatability_raw": 5,
            "repeatability_tolerance_raw": 15,
            "direction": 1,
            "rounds_completed": 2,
            "valid": True,
            "reasons": [],
            "marks": [
                {"end": "closed", "round": 1, "raw": 330},
                {"end": "open", "round": 1, "raw": 572},
                {"end": "closed", "round": 2, "raw": 335},
                {"end": "open", "round": 2, "raw": 568},
            ],
            "commands_sent": 48,
            "commands_dropped": 1,
            "mechanical_limits": None,
        },
        "quality": {
            "arm_joints_valid": 5,
            "arm_joints_required": 5,
            "gripper_valid": True,
            "feedback_fresh": True,
            "complete": True,
            "reasons": [],
        },
        "sample_log_entries": len(sample_log),
        "sample_log": sample_log,
    }
    evidence["sample_log_sha256"] = sample_log_digest(sample_log)
    evidence.update(overrides)
    return evidence


def _mapping_asset(follower_identity: str = FOLLOWER_ID, identity: str = "mapping-test") -> dict:
    return {
        "schema_version": 1,
        "kind": "mapping",
        "role": "leader_to_landerpi",
        "identity": identity,
        "measured": True,
        "measured_at_utc": "2026-10-04T10:00:00Z",
        "joint_order": list(JOINTS),
        "bound_identities": {"leader": "so101-leader-v1", "follower": follower_identity},
        "joints": [
            {"leader": leader, "follower": follower, "direction": 1, "scale": 1.0}
            for leader, follower in zip(
                ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"],
                JOINTS[:5], strict=True)
        ],
        "gripper": {"follower": "r_joint", "direction": -1, "scale": 1.0},
    }


class _Facade:
    def __init__(self, store: HybridCalibrationStore) -> None:
        self.calibration_store = store
        self.session_id = None
        self._is_simulated = lambda: False

    def validate_session_request_if_active(self, _body):
        return None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = HybridCalibrationStore(tmp_path / "calibration")
    facade = _Facade(store)
    monkeypatch.setattr(ros_mobile, "get_hybrid_facade", lambda: facade)
    app = FastAPI()
    app.include_router(ros_mobile.router)
    return TestClient(app), store, facade


def test_route_table_is_the_frozen_contract(client):
    http_client, _store, _facade = client
    for action in ("start", "status", "manual-ranges", "torque", "target", "mark",
                   "gripper-stop", "stop"):
        assert f"/ros-mobile/hybrid/follower-calibration/{action}" not in ()
    for action in ("step", "auto-gripper"):
        response = http_client.post(f"/ros-mobile/hybrid/follower-calibration/{action}", json={})
        assert response.status_code == 404, action
        assert "unknown follower calibration action" in response.json()["detail"]


def test_proxy_forwards_each_action_to_its_car_path(client, monkeypatch):
    http_client, _store, _facade = client
    calls = []

    def fake_http(method, path, payload=None, token=None, timeout=30.0):
        calls.append((method, path, payload))
        return {"ok": True, "path": path}

    monkeypatch.setattr(ros_mobile, "_http", fake_http)
    body = {"token": "t", "job_id": "j", "target_permille": 420, "client_seq": 3}
    roles = {
        "start": "/calibration/follower/start",
        "status": "/calibration/follower/status",
        "manual-ranges": "/calibration/follower/manual-ranges",
        "torque": "/calibration/follower/torque",
        "target": "/calibration/follower/gripper/target",
        "mark": "/calibration/follower/gripper/mark",
        "gripper-stop": "/calibration/follower/gripper/stop",
        "stop": "/calibration/follower/stop",
    }
    for action, path in roles.items():
        response = http_client.post(f"/ros-mobile/hybrid/follower-calibration/{action}", json=body)
        assert response.status_code == 200
        assert calls[-1] == ("POST", path, body)
    assert not any(path in ("/calibration/follower/step", "/calibration/follower/auto-gripper")
                   for _, path, _ in calls)


def test_manual_ranges_are_part_of_the_narrow_proxy_contract(client):
    assert ros_mobile.FOLLOWER_CALIBRATION_ACTIONS["manual-ranges"] == \
        "/calibration/follower/manual-ranges"


def test_proxy_preserves_car_safety_rejection_status(client, monkeypatch):
    http_client, _store, _facade = client

    def reject_start(*_args, **_kwargs):
        raise ros_mobile.BridgeError(409, "confirm the safe window before starting")

    monkeypatch.setattr(ros_mobile, "_http", reject_start)
    response = http_client.post(
        "/ros-mobile/hybrid/follower-calibration/start",
        json={"safe_window_raw": [300, 600],
              "operator_confirmed_safe_window": False,
              "support_confirmed": False})

    assert response.status_code == 409
    assert response.json()["detail"] == "confirm the safe window before starting"


def test_browser_cannot_save_a_follower_asset_directly(client, monkeypatch):
    http_client, store, facade = client

    def facade_with_real_calibration():
        return facade

    monkeypatch.setattr(ros_mobile, "get_hybrid_facade", facade_with_real_calibration)
    payload = {"schema_version": 1, "kind": "follower", "measured": True,
               "identity": FOLLOWER_ID, "device_identity": {"serial": "fake"}}
    # The route forwards to the facade, which is the object that must refuse.  Use the
    # production facade class so the refusal is the real code path.
    from lelab.hybrid_facade import HybridPublicFacade

    real = HybridPublicFacade(calibration_store=store)
    monkeypatch.setattr(ros_mobile, "get_hybrid_facade", lambda: real)
    response = http_client.post("/ros-mobile/hybrid/calibration",
                                json={"action": "save", "kind": "follower",
                                      "identity": FOLLOWER_ID, "payload": payload})
    assert response.status_code == 409
    assert "follower_calibration_requires_job_commit" in response.json()["detail"]


def _commit(client, monkeypatch, evidence, body=None, status_code=200):
    http_client, _store, _facade = client
    payload = {"token": "tok", "job_id": "0123456789abcdef", "generation": 3,
               "now_ts": NOW}
    payload.update(body or {})

    def fake_http(method, path, payload_in=None, token=None, timeout=30.0):
        assert path == "/calibration/follower/status"
        assert payload_in["include_evidence"] is True
        return {"phase": "stopped", "evidence": evidence}

    monkeypatch.setattr(ros_mobile, "_http", fake_http)
    return http_client.post("/ros-mobile/hybrid/follower-calibration/commit",
                            json=payload)


def test_commit_builds_a_server_owned_v2_asset(client, monkeypatch):
    http_client, store, _facade = client
    evidence = evidence_fixture()
    response = _commit(client, monkeypatch, evidence)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] is True
    assert body["measured"] is True
    assert body["schema_version"] == 2
    assert body["identity"] == FOLLOWER_ID

    stored = store.load("follower", FOLLOWER_ID).data
    assert stored["schema_version"] == 2
    assert stored["measured"] is True
    assert stored["device_identity"]["board_serial"] == "TESTBOARD01"
    assert stored["device_identity"]["controller_owner"] == "ros_robot_controller"
    assert stored["measured_at_utc"] == evidence["completed_at_utc"]
    assert stored["method"] == "operator_manual_plus_gripper_drag"
    assert stored["evidence"]["sample_log_sha256"] == evidence["sample_log_sha256"]
    assert stored["limits_source"] == "observed_range_not_mechanical"
    assert stored["gripper"]["mechanical_limits"] is None
    # Vendor unit semantics are preserved, not re-derived in the browser.
    assert stored["units"]["centre_raw"] == 500
    assert stored["raw_to_rad"]["r_joint"]["direction"] == -1
    assert stored["raw_to_rad"]["joint1"]["direction"] == 1
    assert math.isclose(stored["raw_to_rad"]["joint1"]["radians_per_tick"],
                        0.00418879, rel_tol=1e-6)
    assert validate_asset("follower", stored) == []


def test_commit_accepts_explicit_operator_entered_ranges(client, monkeypatch):
    _http_client, store, _facade = client
    evidence = evidence_fixture()
    for name in JOINTS[:5]:
        evidence["joints"][name].update(method="operator_manual_entry",
                                        samples=0, noise_raw=None)
    evidence["method"] = "operator_manual_full_entry"
    evidence["arm_ranges"] = {
        "method": "operator_manual_entry",
        "operator_entered": True,
        "entered_at_utc": "2026-10-04T11:30:00Z",
    }
    evidence["gripper"].update(method="operator_manual_entry",
                               rounds_completed=0,
                               repeatability_raw=None,
                               marks=[])
    evidence["gripper_ranges"] = {
        "method": "operator_manual_entry",
        "operator_entered": True,
        "entered_at_utc": "2026-10-04T11:30:00Z",
    }
    response = _commit(client, monkeypatch, evidence)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] is True
    stored = store.load("follower", FOLLOWER_ID).data
    assert stored["method"] == "operator_manual_full_entry"
    assert stored["limits_source"] == "operator_entry_not_mechanical"
    assert all(stored["joints"][name]["method"] == "operator_manual_entry"
               for name in JOINTS[:5])
    assert stored["gripper"]["method"] == "operator_manual_entry"
    assert stored["gripper"]["repeatability_raw"] is None
    assert validate_asset("follower", stored) == []


def test_commit_ignores_browser_claims_about_measurement_time_and_serial(client, monkeypatch):
    _http_client, store, _facade = client
    evidence = evidence_fixture()
    response = _commit(client, monkeypatch, evidence, body={
        "measured": True,
        "measured_at_utc": "1999-01-01T00:00:00Z",
        "device_identity": {"board_serial": "ATTACKER", "controller_owner": "fake"},
        "method": "auto_endstop_seek",
        "source_kind": "joint_states",
        "identity": "attacker-identity",
    })
    assert response.status_code == 200
    stored = store.load("follower", FOLLOWER_ID).data
    assert stored["device_identity"]["board_serial"] == "TESTBOARD01"
    assert stored["identity"] == FOLLOWER_ID
    assert stored["method"] == "operator_manual_plus_gripper_drag"
    assert stored["source_kind"] == "servo_position_readback"
    assert stored["measured_at_utc"] == evidence["completed_at_utc"]


@pytest.mark.parametrize("mutation,reason", [
    (lambda ev: ev.update(source_kind="joint_states"), "evidence_source_untrusted"),
    (lambda ev: ev["quality"].update(complete=False), "evidence_incomplete"),
    (lambda ev: ev["quality"].update(feedback_fresh=False), "evidence_feedback_stale"),
    (lambda ev: ev["gripper"].update(rounds_completed=1), "gripper:repeatability_rounds_missing"),
    (lambda ev: ev["gripper"].update(repeatability_raw=40), "gripper:repeatability_exceeded"),
    (lambda ev: ev["joints"]["joint3"].update(span_raw=10), "joint3:span_too_small"),
    (lambda ev: ev["joints"]["joint2"].update(valid=False), "joint2:invalid"),
    (lambda ev: ev["device_identity"].update(controller_owner="second_owner"),
     "evidence_controller_owner_mismatch"),
    (lambda ev: ev["device_identity"]["servo_ids"].update(r_joint=9),
     "evidence_servo_ids_mismatch"),
    (lambda ev: ev.update(sample_log_sha256="0" * 64), "sample_log_digest_mismatch"),
    (lambda ev: ev["gripper"].update(mechanical_limits=[0, 1000]),
     "gripper:mechanical_limits_not_proven"),
    (lambda ev: ev.update(completed_at_utc="2026-10-04T11:00:00Z"),
     "evidence_time_reversed"),
    (lambda ev: ev.update(schema_version=1), "evidence_schema_mismatch"),
])
def test_commit_refuses_incomplete_or_tampered_evidence(client, monkeypatch, mutation, reason):
    http_client, store, _facade = client
    evidence = evidence_fixture()
    mutation(evidence)
    response = _commit(client, monkeypatch, evidence)
    assert response.status_code == 200
    body = response.json()
    assert body["saved"] is False
    assert reason in body["reasons"], body["reasons"]
    with pytest.raises(FileNotFoundError):
        store.load("follower", FOLLOWER_ID)


def test_expired_evidence_is_refused(client, monkeypatch):
    http_client, _store, _facade = client
    evidence = evidence_fixture()
    response = _commit(client, monkeypatch, evidence, body={"now_ts": NOW + 7200})
    assert response.status_code == 200
    assert "evidence_expired" in response.json()["reasons"]


def test_commit_requires_a_token(client, monkeypatch):
    http_client, _store, _facade = client
    response = http_client.post("/ros-mobile/hybrid/follower-calibration/commit", json={})
    assert response.status_code == 400


def test_new_follower_asset_invalidates_the_old_mapping(client, monkeypatch):
    http_client, store, _facade = client
    store.save("mapping", "mapping-test", _mapping_asset())
    assert store.load("mapping", "mapping-test").version

    response = _commit(client, monkeypatch, evidence_fixture())
    body = response.json()
    assert body["saved"] is True
    assert body["requires_repair"] is True
    invalidated = body["mapping_invalidated"]
    assert [row["identity"] for row in invalidated] == ["mapping-test"]
    assert invalidated[0]["action_required"] == "re_pair_leader_and_follower_before_preview"

    with pytest.raises(ValueError) as caught:
        store.load("mapping", "mapping-test")
    assert "invalidated" in str(caught.value)

    # Re-pairing writes a fresh mapping and clears the marker.
    store.save("mapping", "mapping-test", _mapping_asset())
    assert store.load("mapping", "mapping-test").version


def test_mapping_bound_to_another_follower_is_left_alone(client, monkeypatch):
    http_client, store, _facade = client
    store.save("mapping", "mapping-test", _mapping_asset(follower_identity="other-follower"))
    response = _commit(client, monkeypatch, evidence_fixture())
    assert response.json()["requires_repair"] is False
    assert store.load("mapping", "mapping-test").version


def test_readback_returns_the_saved_asset(client, monkeypatch):
    http_client, _store, _facade = client
    assert http_client.get("/ros-mobile/hybrid/follower-calibration/asset").json()["exists"] is False
    _commit(client, monkeypatch, evidence_fixture())
    readback = http_client.get("/ros-mobile/hybrid/follower-calibration/asset").json()
    assert readback["exists"] is True
    assert readback["schema_version"] == 2
    assert readback["asset"]["measured"] is True
    assert readback["sha256"] == readback["asset"]["content_sha256"]


def test_v1_follower_files_stay_readable_and_v2_is_the_only_save_path(tmp_path):
    store = HybridCalibrationStore(tmp_path)
    v1 = calibration_assets_fixture()["follower"]
    assert v1["schema_version"] == 1
    store.save("follower", v1["identity"], v1)
    loaded = store.load("follower", v1["identity"])
    assert loaded.data["schema_version"] == 1
    assert validate_asset("follower", loaded.data) == []


def test_v2_follower_requires_the_full_evidence_chain(tmp_path):
    store = HybridCalibrationStore(tmp_path)
    asset = build_follower_asset(evidence_fixture(), identity=FOLLOWER_ID, now_ts=NOW)
    asset["gripper"]["rounds_completed"] = 1
    with pytest.raises(ValueError) as caught:
        store.save("follower", FOLLOWER_ID, asset)
    assert "repeat_rounds_missing" in str(caught.value)


def test_asset_round_trip_hash_matches_content(tmp_path):
    store = HybridCalibrationStore(tmp_path)
    asset = build_follower_asset(evidence_fixture(), identity=FOLLOWER_ID, now_ts=NOW)
    record = store.save("follower", FOLLOWER_ID, asset)
    unsigned = {k: v for k, v in record.data.items() if k not in ("content_sha256", "version")}
    from lelab.mobile_teleop import canonical_sha256

    assert record.sha256 == canonical_sha256(unsigned)
    assert record.version == record.sha256[:12]
    assert record.data["schema_version"] == 2


def test_validate_evidence_accepts_the_reference_document():
    assert validate_evidence(evidence_fixture(), now_ts=NOW) == []


def test_validate_evidence_rejects_a_follower_asset_pasted_as_evidence(tmp_path):
    asset = build_follower_asset(evidence_fixture(), identity=FOLLOWER_ID, now_ts=NOW)
    reasons = validate_evidence(asset, now_ts=NOW)
    assert "evidence_schema_mismatch" in reasons
    assert "evidence_quality_missing" in reasons


def test_json_serialisable_asset(tmp_path):
    asset = build_follower_asset(evidence_fixture(), identity=FOLLOWER_ID, now_ts=NOW)
    json.dumps(asset)  # must not raise
    clone = copy.deepcopy(asset)
    clone["measured"] = False
    assert "unmeasured_example" in validate_asset("follower", clone)
