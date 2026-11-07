"""T4 offline contracts: no ROS graph, network service, or hardware access."""
from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rosbags.rosbag2 import Writer
from rosbags.typesys import Stores, get_typestore

from lelab import dataset_viewer, ros_mobile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ros2_ws/src/lelab_mobile_agent'))
from lelab_mobile_agent.recorder import SessionRecorder  # noqa: E402


class LiveBag:
    def poll(self):
        return None


def test_phase_events_stay_in_one_episode_and_reject_bad_time(tmp_path):
    rec = SessionRecorder(tmp_path, {'recording': {'min_free_bytes': 0}}, None)
    rec.session_id = 's'
    rec.session_dir = tmp_path
    rec.bag_proc = LiveBag()
    (tmp_path / 'episodes').mkdir()
    rec.start_episode('e', 'task', 'manual', 100, {})
    rec.set_phase('observe', 101)
    with pytest.raises(ValueError):
        rec.set_phase('grasp', 100)
    rec.set_phase('grasp', 200)
    with pytest.raises(ValueError):
        rec.set_phase('grasp', 201)
    with pytest.raises(ValueError, match='manual success label'):
        rec.finish_episode(300, {'success': 'unverified'})
    rec.finish_episode(300, {'success': 'unknown'})
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    phase = [e for e in events if e['kind'].startswith('phase_')]
    assert [e['kind'] for e in phase] == ['phase_started', 'phase_ended', 'phase_started', 'phase_ended']
    assert [e['payload']['phase'] for e in phase] == ['observe', 'observe', 'grasp', 'grasp']
    assert len({e['event_id'] for e in phase}) == 4
    assert all(e['session_id'] == 's' and e['episode_id'] == 'e' and e['clock_domain'] == 'ros_time' for e in phase)
    record = json.loads((tmp_path / 'episodes/e.json').read_text())
    assert [(p['started_time_ns'], p['ended_time_ns']) for p in record['phases']] == [(101, 200), (200, 300)]
    with pytest.raises(RuntimeError):
        rec.set_phase('observe', 301)
    with pytest.raises(RuntimeError):
        rec.start_episode('e', 'task', 'manual', 400, {})


class Facade:
    def __init__(self):
        self.calls = []
        self.fail_pause = False
    def status(self):
        return {'recording': {'session_id': 's'}}
    def capabilities(self):
        return {'available': False}
    def set_arm_state(self, event):
        self.calls.append(event)
        if self.fail_pause and event == 'pause_all':
            raise ValueError('pause failed')
        return {'state': event}
    def recording(self, action, payload):
        self.calls.append((action, payload))
        return {'ok': True}
    def preview(self):
        return {'outputs_enabled': False}


def test_hybrid_routes_pause_before_close_and_never_on_phase(monkeypatch, tmp_path):
    """A phase boundary must not touch motion; closing must pause first."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import t4_fixtures as fx

    facade = fx.FakeFacade()
    car = fx.FakeCar()
    controller = ros_mobile.HybridRecordingController(
        facade, car, clock_ns=lambda: 1, wall_ns=lambda: 2, background=False)
    monkeypatch.setattr(ros_mobile, '_HYBRID_RECORDING', controller)
    app = FastAPI(); app.include_router(ros_mobile.router)
    client = TestClient(app)

    assert client.post('/ros-mobile/hybrid/recording',
                       json={'action': 'start_session'}).status_code == 200
    assert client.post('/ros-mobile/hybrid/recording',
                       json={'action': 'start_episode'}).status_code == 200
    episode_before = controller.episode_id
    session_opens = [c for c in car.calls if c[0] == '/recording/start_session']

    response = client.post('/ros-mobile/hybrid/recording',
                           json={'action': 'set_phase', 'phase': 'observe'})
    assert response.status_code == 200
    # No motion command, and the same episode continues.
    assert [c for c in facade.calls if c[0] == 'set_arm_state'] == []
    assert controller.episode_id == episode_before
    assert len([c for c in car.calls if c[0] == '/recording/start_session']) == len(session_opens)
    assert controller.motion_commands_sent == 0
    assert [e['phase'] for e in car.phase_events] == ['observe']

    facade.calls.clear()
    assert client.post('/ros-mobile/hybrid/recording',
                       json={'action': 'end_episode', 'success': 'success'}).status_code == 200
    assert facade.calls[0] == ('set_arm_state', 'pause_all')
    assert controller.paused_before_wrapup is True
    end_calls = [c for c in car.calls if c[0] == '/recording/end_episode']
    assert end_calls[-1][1].get('success') is True
    assert 'discarded' not in end_calls[-1][1]

    # A refused pause must not yield a saved episode.
    facade.pause_fails = True
    client.post('/ros-mobile/hybrid/recording', json={'action': 'start_episode'})
    response = client.post('/ros-mobile/hybrid/recording', json={'action': 'end_episode'})
    assert response.status_code == 503 and 'control_pause_failed' in response.text
    end_calls = [c for c in car.calls if c[0] == '/recording/end_episode']
    assert end_calls[-1][1].get('discarded') is True
    assert controller.status()['status'] == 'invalid'


def test_estop_returns_without_waiting_for_the_car_save(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import t4_fixtures as fx

    facade = fx.FakeFacade()
    car = fx.FakeCar()
    controller = ros_mobile.HybridRecordingController(
        facade, car, clock_ns=lambda: 1, wall_ns=lambda: 2, background=True)
    monkeypatch.setattr(ros_mobile, '_HYBRID_RECORDING', controller)
    app = FastAPI(); app.include_router(ros_mobile.router)
    client = TestClient(app)
    client.post('/ros-mobile/hybrid/recording', json={'action': 'start_session'})
    client.post('/ros-mobile/hybrid/recording', json={'action': 'start_episode'})
    response = client.post('/ros-mobile/hybrid/control',
                           json={'action': 'estop', 'reason': 'operator'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['estop'] is True and payload['outputs_enabled'] is False
    # The latch is immediate even though the car wrap-up is deferred.
    assert controller.status()['status'] == 'invalid'
    ended = [c for c in car.calls if c[0] == '/recording/end_episode']
    assert ended and ended[-1][1].get('discarded') is True


def _make_bag(path):
    store = get_typestore(Stores.ROS2_HUMBLE)
    typ = store.types
    with Writer(path, version=8) as writer:
        image = writer.add_connection('/camera', 'sensor_msgs/msg/Image', typestore=store)
        state = writer.add_connection('/joints', 'sensor_msgs/msg/JointState', typestore=store)
        event = writer.add_connection('/lelab/events', 'std_msgs/msg/String', typestore=store)
        for ep in range(2):
            start = 1_000_000_000 + ep * 2_000_000_000
            for frame in range(7):
                ts = start + frame * 100_000_000
                header = typ['std_msgs/msg/Header'](typ['builtin_interfaces/msg/Time'](ts // 10**9, ts % 10**9), '')
                im = typ['sensor_msgs/msg/Image'](header, 48, 64, 'rgb8', 0, 192, np.full(48*64*3, 20 + ep, dtype=np.uint8))
                js = typ['sensor_msgs/msg/JointState'](header, ['joint1','joint2','joint3','joint4','joint5','r_joint'], np.array([.1,.2,.3,.4,.5,.6]), np.array([], dtype=np.float64), np.array([], dtype=np.float64))
                writer.write(image, ts, store.serialize_cdr(im, image.msgtype))
                writer.write(state, ts, store.serialize_cdr(js, state.msgtype))
                for component, target in [('arm', [.11,.21,.31,.41,.51]), ('gripper', [.61])]:
                    obj = {'kind': 'command', 'component': component, 'time_ns': ts, 'target': target}
                    msg = typ['std_msgs/msg/String'](json.dumps(obj))
                    writer.write(event, ts, store.serialize_cdr(msg, event.msgtype))


def _package(tmp_path):
    car = tmp_path / 'car'; (car / 'raw').mkdir(parents=True)
    _make_bag(car / 'raw/bag')
    episodes = []
    for i in range(2):
        start = 1_000_000_000 + i * 2_000_000_000
        episodes.append({'episode_id': f'e{i}', 'task': 'manual', 'source': 'leader_arm', 'status': 'saved',
                         'started_time_ns': start, 'ended_time_ns': start + 600_000_000,
                         'evaluation': {'success': 'unknown'}, 'phases': [
                             {'phase': 'observe', 'started_time_ns': start, 'ended_time_ns': start + 300_000_000},
                             {'phase': 'grasp', 'started_time_ns': start + 300_000_000, 'ended_time_ns': start + 600_000_000}]})
    phase_events = []
    for episode in episodes:
        for phase in episode['phases']:
            for kind, key in [('phase_started', 'started_time_ns'), ('phase_ended', 'ended_time_ns')]:
                phase_events.append({'event_id': f"event-{len(phase_events)}", 'kind': kind,
                                     'session_id': 'ros_t4fixture', 'episode_id': episode['episode_id'],
                                     'time_ns': phase[key], 'clock_domain': 'ros_time',
                                     'payload': {'phase': phase['phase']}})
    (car / 'events.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in phase_events))
    files = [{'path': str(p.relative_to(car)), 'size': p.stat().st_size, 'sha256': ros_mobile._sha256(p)} for p in car.rglob('*') if p.is_file()]
    meta = {'source': 'hardware_teleop_arm_gripper', 'runtime_mode': 'hardware_teleop', 'training_export': True,
            'export_schema': ros_mobile.ARM_GRIPPER_SCHEMA, 'clock_domain': 'ros_time', 'camera_topic': '/camera',
            'record_joint_state_topic': '/joints', 'features': {'fps': 10, 'state': {'names': ['joint1','joint2','joint3','joint4','joint5','r_joint']}}}
    manifest = {'session_id': 'ros_t4fixture', 'status': 'raw_ready', 'ready_eligible': True, 'bag_exit_code': 0,
                'bag_readable': True, 'required_topics': ['/camera', '/joints', '/lelab/events'], 'files': files,
                'episodes': episodes, 'metadata': meta}
    (car / 'manifest.json').write_text(json.dumps(manifest))
    (car / 'READY').write_text(json.dumps({'manifest_sha256': ros_mobile._sha256(car / 'manifest.json')}))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for path in car.rglob('*'):
            if path.is_file(): tar.add(path, arcname=path.relative_to(car))
    return manifest, archive.getvalue()


def test_real_sqlite_sync_export_sdk_and_viewer_two_episodes(tmp_path, monkeypatch):
    manifest, archive = _package(tmp_path)
    upper = tmp_path / 'upper'; upper.mkdir()
    monkeypatch.setattr(ros_mobile, 'UPPER_SESSIONS', upper)
    monkeypatch.setattr(ros_mobile, 'UPPER_EXPORTS', tmp_path / 'exports')
    monkeypatch.setattr(ros_mobile, 'lerobot_dataset_root', lambda: tmp_path / 'datasets')
    monkeypatch.setattr(ros_mobile, '_http', lambda *a, **k: manifest)
    monkeypatch.setattr(ros_mobile.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(archive))
    assert ros_mobile.sync_session('ros_t4fixture')['status'] == 'raw_ready'
    assert ros_mobile.sync_session('ros_t4fixture')['status'] == 'already_synced'
    report = ros_mobile.export_session('ros_t4fixture')
    assert report['total_episodes'] == 2
    from lerobot.datasets import LeRobotDataset
    ds = LeRobotDataset(repo_id=report['repo_id'], root=Path(report['dataset_root']))
    assert tuple(ds[0]['action'].shape) == (6,)
    root = Path(report['dataset_root'])
    listing = dataset_viewer.list_episodes(root)
    videos = [dataset_viewer.video_path(root, ep['index'], 'wrist_real') for ep in listing['episodes']]
    assert len(videos) == 2 and videos[0] == videos[1], 'both episodes must reference the shared MP4'
    ranges = [ep['video_ranges']['wrist_real'] for ep in listing['episodes']]
    assert ranges[0]['to_s'] <= ranges[1]['from_s']
    for ep in listing['episodes']:
        assert [p['phase'] for p in ep['phases']] == ['observe', 'grasp']
        assert dataset_viewer.video_path(root, ep['index'], 'wrist_real').is_file()
        bounds = ep['video_ranges']['wrist_real']
        assert ep['phases'][0]['video_ranges']['wrist_real']['from_s'] == pytest.approx(bounds['from_s'])
        assert ep['phases'][1]['video_ranges']['wrist_real']['to_s'] <= bounds['to_s'] + 1e-6
        assert len(dataset_viewer.episode_timeseries(root, ep['index'])['phases']) == 2
    with pytest.raises(Exception, match='already exists'):
        ros_mobile.export_session('ros_t4fixture')


def test_hybrid_fails_closed_and_old_readonly_rejected(tmp_path, monkeypatch):
    manifest, _ = _package(tmp_path)
    sessions = tmp_path / 'upper'; session = sessions / 'ros_t4fixture'; session.mkdir(parents=True)
    (session / 'READY').write_text('{}')
    monkeypatch.setattr(ros_mobile, 'UPPER_SESSIONS', sessions)
    manifest['metadata']['export_schema'] = ros_mobile.HYBRID_SCHEMA
    (session / 'manifest.json').write_text(json.dumps(manifest))
    schema = ros_mobile._export_schema(manifest['metadata'])
    assert schema['dims'] == 9 and schema['state_joints'][-1] == 'r_joint'
    with pytest.raises(Exception, match="requires runtime_mode in \\['hardware_hybrid_manual'\\]"):
        ros_mobile._check_exportable('ros_t4fixture')
    manifest['metadata']['runtime_mode'] = 'hardware_hybrid_manual'
    (session / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(Exception, match='feedback_provenance'):
        ros_mobile._check_exportable('ros_t4fixture')
    manifest['metadata'] = {'source': 'hardware_readonly', 'training_export': False}
    (session / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(Exception, match='read-only'):
        ros_mobile._check_exportable('ros_t4fixture')


def test_invalid_phase_slice_is_rejected():
    episode = {'episode_id': 'e', 'started_time_ns': 0, 'ended_time_ns': 100,
               'phases': [{'phase': 'observe', 'started_time_ns': 0, 'ended_time_ns': 80},
                          {'phase': 'grasp', 'started_time_ns': 70, 'ended_time_ns': 100}]}
    with pytest.raises(ros_mobile.ExportRejected, match='invalid phase'):
        ros_mobile._phase_slices(episode, 0, 10, 10)


def test_facade_without_adapters_is_honestly_unavailable(monkeypatch):
    monkeypatch.setattr(ros_mobile, '_HYBRID_FACADE', None)
    app = FastAPI()
    app.include_router(ros_mobile.router)
    client = TestClient(app)
    response = client.get('/ros-mobile/hybrid/status')
    assert response.status_code == 200
    assert response.json()['outputs_enabled'] is False
    assert client.get('/ros-mobile/hybrid/capabilities').status_code == 200
    assert client.post('/ros-mobile/hybrid/session', json={'action': 'acquire', 'owner': 'test'}).status_code == 409
    assert client.post('/ros-mobile/hybrid/recording', json={'action': 'start_session'}).status_code == 409
    assert client.post('/ros-mobile/hybrid/calibration', json={'action': 'save', 'kind': 'base'}).status_code == 503


def test_phase_event_wrong_clock_and_missing_event_are_rejected(tmp_path):
    manifest, _ = _package(tmp_path)
    car = tmp_path / 'car'
    ros_mobile._verify_phase_events(car, 'ros_t4fixture', manifest['episodes'], 'ros_time')
    events = [json.loads(x) for x in (car / 'events.jsonl').read_text().splitlines()]
    events[0]['clock_domain'] = 'other_clock'
    (car / 'events.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in events))
    with pytest.raises(ros_mobile.ExportRejected, match='clock mismatch'):
        ros_mobile._verify_phase_events(car, 'ros_t4fixture', manifest['episodes'], 'ros_time')
    events.pop(0)
    (car / 'events.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in events))
    with pytest.raises(ros_mobile.ExportRejected, match='do not match'):
        ros_mobile._verify_phase_events(car, 'ros_t4fixture', manifest['episodes'], 'ros_time')


def test_six_dimensional_identity_and_unknown_schema_are_rejected(tmp_path, monkeypatch):
    manifest, _ = _package(tmp_path)
    session = tmp_path / 'upper' / 'ros_t4fixture'
    session.mkdir(parents=True)
    (session / 'READY').write_text('{}')
    monkeypatch.setattr(ros_mobile, 'UPPER_SESSIONS', session.parent)
    manifest['metadata']['features']['state']['names'][-1] = 'gripper_base_joint'
    (session / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(Exception, match='r_joint in order'):
        ros_mobile._check_exportable('ros_t4fixture')
    manifest['metadata']['export_schema'] = 'invented'
    (session / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(Exception, match='unknown export schema'):
        ros_mobile._check_exportable('ros_t4fixture')


def test_cross_phase_hold_and_missing_sample_are_rejected(tmp_path, monkeypatch):
    manifest, archive = _package(tmp_path)
    upper = tmp_path / 'upper'; upper.mkdir()
    monkeypatch.setattr(ros_mobile, 'UPPER_SESSIONS', upper)
    monkeypatch.setattr(ros_mobile, 'UPPER_EXPORTS', tmp_path / 'exports')
    monkeypatch.setattr(ros_mobile, 'lerobot_dataset_root', lambda: tmp_path / 'datasets')
    monkeypatch.setattr(ros_mobile, '_http', lambda *a, **k: manifest)
    monkeypatch.setattr(ros_mobile.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(archive))
    ros_mobile.sync_session('ros_t4fixture')
    read_bag = ros_mobile._read_bag
    def remove_boundary_sample(*args, **kwargs):
        data = read_bag(*args, **kwargs)
        boundary = args[1] + 300_000_000
        data['states'] = [x for x in data['states'] if x['stamp'] != boundary]
        return data
    monkeypatch.setattr(ros_mobile, '_read_bag', remove_boundary_sample)
    with pytest.raises(ros_mobile.ExportRejected, match='crosses phase boundary'):
        ros_mobile.export_session('ros_t4fixture')
    assert not (tmp_path / 'datasets/lelab/ros-mobile-t4fixture').exists()


def test_cancel_removes_only_staging_and_leaves_raw(monkeypatch, tmp_path):
    class Proc:
        pid = 999999
        returncode = None
        def wait(self, timeout):
            self.returncode = -15
    monkeypatch.setattr(ros_mobile.os, 'killpg', lambda *_: None)
    monkeypatch.setattr(ros_mobile, 'UPPER_EXPORTS', tmp_path / 'exports')
    staging = tmp_path / 'exports' / 's' / 'staging-1'
    staging.mkdir(parents=True)
    raw = tmp_path / 'raw'
    raw.write_text('preserve')
    job = {'status': 'running', 'proc': Proc(), 'session_id': 's'}
    monkeypatch.setattr(ros_mobile, 'EXPORT_JOBS', {'job': job})
    assert ros_mobile.export_cancel('job')['status'] == 'canceled'
    assert not staging.exists() and raw.read_text() == 'preserve'


def _controller(**facade_kwargs):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import t4_fixtures as fx

    facade_kwargs.setdefault('ready', True)
    facade = fx.FakeFacade(**facade_kwargs)
    car = fx.FakeCar()
    return ros_mobile.HybridRecordingController(
        facade, car, clock_ns=lambda: 1, wall_ns=lambda: 2, background=False), facade, car


def test_recording_needs_the_lease_and_an_open_episode():
    controller, _, _ = _controller(ready=False)
    with pytest.raises(ros_mobile.RecordingError, match='session_not_ready'):
        controller.dispatch('start_session', {})
    controller, _, _ = _controller()
    with pytest.raises(ros_mobile.RecordingError, match='recording_session_not_started'):
        controller.dispatch('start_episode', {})
    controller.dispatch('start_session', {})
    with pytest.raises(ros_mobile.RecordingError, match='episode_not_open'):
        controller.dispatch('set_phase', {'phase': 'observe'})


def test_a_second_session_or_episode_is_refused_not_silently_reused():
    controller, _, _ = _controller()
    controller.dispatch('start_session', {})
    with pytest.raises(ros_mobile.RecordingError, match='already_started'):
        controller.dispatch('start_session', {})
    controller.dispatch('start_episode', {})
    with pytest.raises(ros_mobile.RecordingError, match='episode_already_open'):
        controller.dispatch('start_episode', {})


def test_phase_repetition_and_unknown_phase_are_refused():
    controller, _, _ = _controller()
    controller.dispatch('start_session', {})
    controller.dispatch('start_episode', {})
    controller.dispatch('set_phase', {'phase': 'observe'})
    with pytest.raises(ros_mobile.RecordingError, match='phase_already_active'):
        controller.dispatch('set_phase', {'phase': 'observe'})
    with pytest.raises(ros_mobile.RecordingError, match='unknown_phase'):
        controller.dispatch('set_phase', {'phase': 'teleport'})


def test_phases_stay_ordered_on_the_car_clock():
    controller, facade, car = _controller()
    controller.dispatch('start_session', {})
    controller.dispatch('start_episode', {})
    controller.dispatch('set_phase', {'phase': 'observe'})
    controller.dispatch('set_phase', {'phase': 'grasp'})
    stampes = [e['time_ns'] for e in car.phase_events]
    assert stampes == sorted(stampes) and len(set(stampes)) == 2
    phases = controller.status()['phases']
    assert [p['phase'] for p in phases] == ['observe', 'grasp']
    assert phases[0]['ended_time_ns'] == phases[1]['started_time_ns']
    assert phases[1]['ended_time_ns'] is None
    # A phase change never reaches the arm-state API.
    assert [c for c in facade.calls if c[0] == 'set_arm_state'] == []


def test_a_car_without_the_phase_route_is_reported_and_blocks_export():
    controller, _, car = _controller()
    car.route_phase = False
    controller.dispatch('start_session', {})
    controller.dispatch('start_episode', {})
    with pytest.raises(ros_mobile.CarRouteUnavailable) as error:
        controller.dispatch('set_phase', {'phase': 'observe'})
    assert error.value.status_code == 503
    status = controller.status()
    assert status['phase_timeline_complete'] is False
    assert status['last_error'] == 'phase_timeline_incomplete'
    assert status['phases'][-1]['car_recorded'] is False


def test_a_car_without_a_clock_domain_cannot_start_recording():
    controller, _, car = _controller()
    car.clock_domain = None
    with pytest.raises(ros_mobile.RecordingError, match='clock_domain_missing'):
        controller.dispatch('start_session', {})
    assert controller.status()['status'] == 'invalid'


def test_stop_session_closes_an_open_episode_as_discarded():
    controller, facade, car = _controller()
    controller.dispatch('start_session', {})
    controller.dispatch('start_episode', {})
    controller.dispatch('set_phase', {'phase': 'observe'})
    facade.calls.clear()
    status = controller.dispatch('stop_session', {'reason': 'operator'})
    assert status['status'] == 'stopped'
    assert facade.calls[0] == ('set_arm_state', 'pause_all')
    ended = [c for c in car.calls if c[0] == '/recording/end_episode'][-1][1]
    assert ended['discarded'] is True and 'forced_by_stop' in ended['reason']


def test_an_unreadable_manual_result_is_refused():
    controller, _, _ = _controller()
    controller.dispatch('start_session', {})
    controller.dispatch('start_episode', {})
    with pytest.raises(ros_mobile.RecordingError, match='invalid_manual_result'):
        controller.dispatch('end_episode', {'success': 'maybe'})
    # The episode stays open so the operator can label it properly.
    assert controller.status()['episode_id'] is not None


def test_the_event_journal_keeps_session_epoch_and_never_claims_output():
    controller, _, _ = _controller()
    controller.dispatch('start_session', {})
    controller.dispatch('start_episode', {})
    controller.dispatch('set_phase', {'phase': 'observe'})
    controller.dispatch('end_episode', {'success': 'success'})
    status = controller.status()
    assert status['outputs_enabled'] is False
    assert status['lease_acquired'] is False
    assert status['motion_commands_sent'] == 0
    assert status['clock_epoch'] and status['session_epoch'] == 4242
    assert [e['kind'] for e in controller.events] == [
        'recording_session_started', 'episode_started', 'phase_started', 'episode_ended']
    assert all(e['session_epoch'] == 4242 and e['clock_epoch'] for e in controller.events)
