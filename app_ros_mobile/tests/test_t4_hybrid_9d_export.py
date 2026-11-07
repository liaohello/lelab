"""T4 Wave A: 9D hybrid export proven through the production sync and export path.

Every case here builds a real rosbag2 **sqlite3** bag plus the car's
``session.json`` / ``events.jsonl`` / ``manifest.json`` / ``READY`` layout, pushes it
through the production ``sync_session`` verifier and the production
``export_session``, then reads the result back with the local LeRobot SDK and the
dataset viewer.  Nothing is mocked except the two HTTP calls that carry the
archive, and no hardware, service or large model is touched.

The rejection cases exist because a 9D row is only meaningful when it is a
*command* plus a *measured* state: a missing channel, an expired command, a
command-echo "state" or an unverified base estimate must fail loudly instead of
producing a plausible-looking training row.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import t4_fixtures as fx  # noqa: E402

from lelab import dataset_viewer, ros_mobile  # noqa: E402
from lelab.hybrid_export_schema import STATE_9D_FIELDS, ACTION_9D_FIELDS  # noqa: E402

SESSION_ID = "ros_t4hybrid"


def _prepare(tmp_path: Path, monkeypatch, **session_kwargs):
    """Build the car-side session, then publish it through the production sync."""
    car = tmp_path / "car"
    car.mkdir(parents=True, exist_ok=True)
    built = fx.build_synced_session(car, SESSION_ID, **session_kwargs)
    manifest, archive = fx.package_session(built["session_dir"])
    upper = tmp_path / "upper" / "sessions"
    upper.mkdir(parents=True)
    monkeypatch.setattr(ros_mobile, "UPPER_SESSIONS", upper)
    monkeypatch.setattr(ros_mobile, "UPPER_EXPORTS", tmp_path / "upper" / "exports")
    monkeypatch.setattr(ros_mobile, "lerobot_dataset_root", lambda: tmp_path / "datasets")
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: manifest)
    monkeypatch.setattr(ros_mobile.urllib.request, "urlopen", lambda *a, **k: _Bytes(archive))
    return built


class _Bytes:
    def __init__(self, data: bytes):
        import io

        self._stream = io.BytesIO(data)

    def __enter__(self):
        return self._stream

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self._stream.read(*args)


def test_two_short_episodes_export_and_read_back_as_nine_dimensions(tmp_path, monkeypatch):
    built = _prepare(tmp_path, monkeypatch, episodes=2, seconds=1.5)

    assert ros_mobile.sync_session(SESSION_ID)["status"] == "raw_ready"
    # A second pull of the same verified session is refused rather than duplicated.
    assert ros_mobile.sync_session(SESSION_ID)["status"] == "already_synced"

    report = ros_mobile.export_session(SESSION_ID)
    assert report["status"] == "validated"
    assert report["export_schema"] == {"kind": "mobile_hybrid_manual_9d_v1", "dims": 9,
                                       "chassis_included": True}
    assert report["total_episodes"] == 2 and report["total_frames"] == 30
    assert report["feedback_provenance"]["joint_source_kind"] == "servo_position_readback"
    assert report["base_velocity_source"] == "wheel_odometry_estimate"
    assert report["success_labeling"] == "manual_or_unknown_no_simulator_ground_truth"

    # Both episodes carry the manual observe/grasp split with its source spelled out.
    for episode in report["episodes"]:
        assert [s["phase"] for s in episode["phases"]] == ["observe", "grasp"]
        assert all(s["frame_count"] > 0 for s in episode["phases"])
        assert all("car clock" in s["source"] for s in episode["phases"])
    labels = [e["success"] for e in report["episodes"]]
    assert labels == ["unknown", "success"], "manual labels survive; nothing is invented"

    dataset_root = Path(report["dataset_root"])
    from lerobot.datasets import LeRobotDataset

    dataset = LeRobotDataset(repo_id=report["repo_id"], root=dataset_root)
    assert int(dataset.meta.total_episodes) == 2
    assert len(dataset) == 30
    sample = dataset[0]
    assert tuple(sample["action"].shape) == (9,)
    assert tuple(sample["observation.state"].shape) == (9,)
    # Action is the command, state is the measurement: the fixture makes them differ.
    action = [round(float(v), 3) for v in sample["action"].tolist()]
    state = [round(float(v), 3) for v in sample["observation.state"].tolist()]
    assert action == [0.05, 0.0, 0.02, 0.1, -0.2, 0.3, -0.4, 0.5, 0.25]
    assert state == [0.05, 0.0, 0.02, 0.0, -0.2, 0.3, -0.4, 0.0, 0.25]

    info = json.loads((dataset_root / "meta" / "info.json").read_text())
    assert info["features"]["observation.state"]["names"] == list(STATE_9D_FIELDS)
    assert info["features"]["action"]["names"] == list(ACTION_9D_FIELDS)

    # The viewer locates video and curves by episode and phase, with the slice source.
    listing = dataset_viewer.list_episodes(dataset_root)
    assert len(listing["episodes"]) == 2
    videos = [dataset_viewer.video_path(dataset_root, ep["index"], "wrist_real")
              for ep in listing["episodes"]]
    assert videos[0] == videos[1] and videos[0].is_file(), "both episodes share one mp4"
    ranges = [ep["video_ranges"]["wrist_real"] for ep in listing["episodes"]]
    assert ranges[0]["to_s"] <= ranges[1]["from_s"]
    for episode in listing["episodes"]:
        assert episode["phase_source"] and "car events.jsonl" in episode["phase_source"]
        assert [p["phase"] for p in episode["phases"]] == ["observe", "grasp"]
        for phase in episode["phases"]:
            assert "car events.jsonl" in phase["source"]
            assert phase["frame_from"] < phase["frame_to_exclusive"]

    grasp = dataset_viewer.episode_phase_slice(dataset_root, 0, "grasp")
    assert grasp["available"] is True
    assert grasp["frame_from"] == 8 and grasp["frame_to_exclusive"] == 15
    assert grasp["interval"] == "half-open [frame_from, frame_to_exclusive)"
    assert grasp["video_ranges"]["wrist_real"]["from_s"] == pytest.approx(0.8)
    assert grasp["car_time_ns"]["started_time_ns"] == built["phases"][0]["phases"][1]["started_time_ns"]

    series = dataset_viewer.episode_timeseries(dataset_root, 0, phase="grasp")
    assert series["points"] == 7 and series["phase_slice"]["phase"] == "grasp"
    assert "slice_source" in series
    missing = dataset_viewer.episode_phase_slice(dataset_root, 0, "transport")
    assert missing["available"] is False and missing["reason"] == "phase_not_in_episode"
    assert missing["available_phases"] == ["observe", "grasp"]

    # Re-export is refused so a finished dataset is never silently replaced.
    with pytest.raises(Exception, match="already exists"):
        ros_mobile.export_session(SESSION_ID)


def test_a_tampered_file_fails_the_production_sync_verifier(tmp_path, monkeypatch):
    built = _prepare(tmp_path, monkeypatch)
    manifest, archive = fx.package_session(built["session_dir"])
    upper = tmp_path / "upper" / "sessions"
    monkeypatch.setattr(ros_mobile, "UPPER_SESSIONS", upper)
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: manifest)
    monkeypatch.setattr(ros_mobile.urllib.request, "urlopen", lambda *a, **k: _Bytes(archive))
    ros_mobile.sync_session(SESSION_ID)

    session = upper / SESSION_ID
    target = session / "episodes" / "ep_0000.json"
    target.write_text(target.read_text().replace('"unknown"', '"success"'))
    with pytest.raises(ros_mobile.BridgeError, match="sha256 mismatch"):
        ros_mobile._verify_synced_session(session, manifest)


def test_a_missing_action_channel_is_refused_instead_of_zero_filled(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, frame_kwargs={"skip_channel": "base"})
    ros_mobile.sync_session(SESSION_ID)
    with pytest.raises(ros_mobile.ExportRejected, match="no base command stream"):
        ros_mobile.export_session(SESSION_ID)


def test_an_expired_command_rejects_the_episode(tmp_path, monkeypatch):
    # Frames are 100 ms apart but a command is only issued every fifth frame, so a
    # 1 ms ttl has long expired by the next frame: the hold must not be extended.
    _prepare(tmp_path, monkeypatch,
             frame_kwargs={"base_ttl_ms": 1, "command_interval_frames": 5})
    ros_mobile.sync_session(SESSION_ID)
    with pytest.raises(ros_mobile.ExportRejected, match="base command expired"):
        ros_mobile.export_session(SESSION_ID)


def test_a_command_without_ttl_is_not_held_open_ended(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, frame_kwargs={"arm_ttl_ms": None})
    ros_mobile.sync_session(SESSION_ID)
    with pytest.raises(ros_mobile.ExportRejected, match="arm command declares no ttl_ms"):
        ros_mobile.export_session(SESSION_ID)


def test_zero_base_covariance_is_kept_as_declared_evidence(tmp_path, monkeypatch):
    """A zero variance is finite and non-negative, so it is recorded, not "fixed"."""
    _prepare(tmp_path / "zero", monkeypatch, frame_kwargs={"base_cov": (0.0, 0.0, 0.0)})
    ros_mobile.sync_session(SESSION_ID)
    report = ros_mobile.export_session(SESSION_ID)
    assert report["episodes"][0]["frames_detail"][0]["base_covariance_diag"] == [0.0, 0.0, 0.0]


def test_a_negative_base_covariance_rejects_the_episode(tmp_path, monkeypatch):
    _prepare(tmp_path / "negative", monkeypatch,
             frame_kwargs={"base_cov": (-1.0, 1e-4, 1e-4)})
    ros_mobile.sync_session(SESSION_ID)
    with pytest.raises(ros_mobile.ExportRejected, match="covariance is not finite and non-negative"):
        ros_mobile.export_session(SESSION_ID)


def test_forbidden_state_sources_are_refused_by_the_gate(tmp_path, monkeypatch):
    for kind in ("command_cache", "command_integrated_estimate", "model_placeholder", "synthetic"):
        meta = fx.hybrid_metadata(provenance={
            "joint_source_kind": kind, "base_source_kind": "wheel_odometry_estimate",
            "calibration_hash": "sha256:abc", "base_estimate_has_covariance": True,
            "max_state_age_s": 0.2,
        })
        _prepare(tmp_path / kind, monkeypatch, meta=meta)
        with pytest.raises(Exception, match="command echo|not "):
            ros_mobile._check_exportable(SESSION_ID)


def test_a_missing_observe_phase_is_refused(tmp_path, monkeypatch):
    def only_grasp(start, end, step):
        return [{"phase": "grasp", "started_time_ns": start, "ended_time_ns": end}]

    _prepare(tmp_path, monkeypatch, phases_for_episode=only_grasp)
    ros_mobile.sync_session(SESSION_ID)
    with pytest.raises(Exception, match="no observe phase"):
        ros_mobile.export_session(SESSION_ID)


def test_phase_events_that_do_not_match_the_manifest_are_refused(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, break_phase_events=True)
    ros_mobile.sync_session(SESSION_ID)
    with pytest.raises(ros_mobile.ExportRejected, match="do not match"):
        ros_mobile.export_session(SESSION_ID)


def test_a_read_only_profile_is_still_refused(tmp_path, monkeypatch):
    meta = fx.hybrid_metadata(training_export=False)
    _prepare(tmp_path, monkeypatch, meta=meta)
    ros_mobile.sync_session(SESSION_ID)
    with pytest.raises(Exception, match="training_export=false"):
        ros_mobile._check_exportable(SESSION_ID)


def test_an_unlabelled_episode_stays_exportable_and_a_failure_is_dropped(tmp_path, monkeypatch):
    meta = fx.hybrid_metadata()
    built = _prepare(tmp_path, monkeypatch, episodes=2, meta=meta)
    manifest = json.loads((built["session_dir"] / "manifest.json").read_text())
    manifest["episodes"][1]["evaluation"] = {"success": "failure"}
    manifest["episodes"][0]["evaluation"] = {"success": "unknown"}
    built["session_dir"].joinpath("manifest.json").write_text(json.dumps(manifest))
    # Re-sign READY for the modified manifest, as the car would.
    (built["session_dir"] / "READY").write_text(json.dumps({
        "manifest_sha256": ros_mobile._sha256(built["session_dir"] / "manifest.json")}))
    archive_manifest, archive = fx.package_session(built["session_dir"])
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: archive_manifest)
    monkeypatch.setattr(ros_mobile.urllib.request, "urlopen", lambda *a, **k: _Bytes(archive))
    ros_mobile.sync_session(SESSION_ID)
    report = ros_mobile.export_session(SESSION_ID)
    assert report["total_episodes"] == 1
    assert report["episodes"][0]["success"] == "unknown"


def test_explicit_simulation_identity_exports_without_claiming_hardware(tmp_path, monkeypatch):
    provenance = {
        "joint_source_kind": "simulated_servo_readback",
        "base_source_kind": "simulated_wheel_odometry_estimate",
        "calibration_hash": "sha256:offline-simulation",
        "base_estimate_has_covariance": True,
        "max_state_age_s": 0.2,
        "joint_topic": fx.STATE_TOPIC,
        "base_topic": fx.BASE_TOPIC,
        "scan_span_ms": 1.5,
    }
    meta = fx.hybrid_metadata(provenance=provenance, runtime_mode="simulated_hybrid_manual")
    meta.update({
        "source": "simulated_offline",
        "simulation_identity": {
            "identity": "offline-export-test",
            "qualified": True,
            "not_hardware": True,
            "evidence_kind": "offline_integration_only",
        },
    })
    _prepare(
        tmp_path,
        monkeypatch,
        meta=meta,
        manifest_overrides={"profile": "simulated_hybrid_manual_v1"},
    )
    ros_mobile.sync_session(SESSION_ID)
    report = ros_mobile.export_session(SESSION_ID)
    assert report["data_identity"] == "simulated_offline"
    assert report["training_eligible"] is False
    assert report["simulation_identity"]["identity"] == "offline-export-test"
    assert report["feedback_provenance"]["joint_source_kind"] == "simulated_servo_readback"
    assert report["feedback_provenance"]["base_source_kind"] == "simulated_wheel_odometry_estimate"


def test_simulated_sources_without_an_identity_are_refused(tmp_path, monkeypatch):
    provenance = {
        "joint_source_kind": "simulated_servo_readback",
        "base_source_kind": "simulated_wheel_odometry_estimate",
        "calibration_hash": "sha256:offline-simulation",
        "base_estimate_has_covariance": True,
        "max_state_age_s": 0.2,
    }
    meta = fx.hybrid_metadata(provenance=provenance, runtime_mode="simulated_hybrid_manual")
    meta["source"] = "simulated_offline"
    _prepare(tmp_path, monkeypatch, meta=meta)
    ros_mobile.sync_session(SESSION_ID)
    with pytest.raises(Exception, match="simulation_identity"):
        ros_mobile.export_session(SESSION_ID)
