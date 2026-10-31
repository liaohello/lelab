"""SSH control-plane parsing for the train server (no real SSH)."""

from __future__ import annotations

import json

import pytest

from lelab import train_server
from lelab.remote_sync import TrainServerConfig

INSTANCE = "jetson-controller-jetson"

SAMPLE = """##GPUS
0, NVIDIA GeForce RTX 4090, 24564, 812, 3, 41, GPU-aaaa
1, NVIDIA GeForce RTX 4090, 24564, 20, 0, 38, GPU-bbbb
##APPS
GPU-aaaa, 2190790, /usr/bin/sunshine, 384
GPU-bbbb, 4242, python, 3800
##LISTEN
0.0.0.0:22
127.0.0.1:8000
127.0.0.1:28081\tusers:(("python",pid=4243,fd=5))
##JOBS
act_smoke_2026-09-19_15-46-36\t4242\t\t1\t3\t630\t 50:210 100:210 150:210\t{"kind": "train", "job_id": "act_smoke_2026-09-19_15-46-36", "instance_id": "jetson-controller-jetson", "gpu": 1}
someone_else_job\t777\t\t1\t0\t0\t\t{"kind": "train", "job_id": "someone_else_job", "instance_id": "other-box"}
old_job\t99\t0\t0\t1\t210\t 5:210\t{"kind": "train", "job_id": "old_job", "instance_id": "jetson-controller-jetson"}
##SESSIONS
2026-09-19_16-10-01\t4243\t1\t{"kind": "policy_server", "stamp": "2026-09-19_16-10-01", "instance_id": "jetson-controller-jetson", "port": 28081, "gpu": 1, "policy_ref": "l-e-n/act_so101@root"}\tPolicyServer started on 127.0.0.1:28081\x01Client ipv4:127.0.0.1 connected and ready\x01Action chunk #0 generated
##DISK
10066329600 4194304000 5872025600
##SIZES
36864
1024
##END
"""


def test_parse_status_maps_everything() -> None:
    payload = train_server.parse_status(SAMPLE, instance_id=INSTANCE, policy_port=28081)

    assert payload["ssh_ok"] is True
    assert [gpu["index"] for gpu in payload["gpus"]] == [0, 1]
    assert payload["gpus"][0]["mem_free_mb"] == 24564 - 812
    # The listener pid matches our session pid → the port is ours, not foreign.
    assert payload["policy_port"] == {"port": 28081, "state": "ours", "pid": 4243}

    ours = next(job for job in payload["jobs"] if job["id"].startswith("act_smoke"))
    assert ours["ours"] is True
    assert ours["state"] == "running"
    assert ours["mem_mb"] == 3800  # measured from nvidia-smi, not the request
    assert [ckpt["step"] for ckpt in ours["checkpoints"]] == [50, 100, 150]
    assert ours["checkpoint_mb"] == 630

    foreign = next(job for job in payload["jobs"] if job["id"] == "someone_else_job")
    assert foreign["ours"] is False

    finished = next(job for job in payload["jobs"] if job["id"] == "old_job")
    assert finished["state"] == "exited"
    assert finished["exit_code"] == 0

    session = payload["sessions"][0]
    assert session["ours"] is True
    assert session["state"] == "running"
    assert session["gpu"] == 1
    assert session["client_connected"] is True
    assert session["last_action"] is True

    assert payload["disk"]["total_gb"] == 9600.0
    assert payload["disk"]["free_percent"] == 58.3
    assert payload["usage"]["jobs_gb"] == 36.0

    app_pids = {app["pid"]: app["ours"] for app in payload["compute_apps"]}
    assert app_pids[4242] is True
    assert app_pids[2190790] is False
    # Processes are attributed to the GPU nvidia-smi reported, not to every card.
    assert payload["compute_apps"][1]["gpu_index"] == 1
    assert payload["gpus"][0]["foreign_procs"] == 1
    assert payload["gpus"][1]["foreign_procs"] == 0
    assert payload["gpus"][1]["our_jobs"] == [{"pid": 4242, "mem_mb": 3800}]


def test_parse_status_tolerates_missing_sections() -> None:
    payload = train_server.parse_status("##GPUS\n", instance_id=INSTANCE)
    assert payload["gpus"] == []
    assert payload["jobs"] == []
    assert payload["disk"]["free_percent"] is None


def test_status_cache_reuses_and_invalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_run_script(config, script, *args, **kwargs):
        calls.append(script[:20])
        return 0, SAMPLE

    monkeypatch.setattr(train_server.ssh_ops, "run_script", fake_run_script)
    monkeypatch.setattr(
        train_server.TrainServerConfig,
        "from_env",
        classmethod(
            lambda cls, env=None: TrainServerConfig(
                host="192.0.2.10", port=22, user="trainer", root="~/Desktop/lelab_server"
            )
        ),
    )

    cache = train_server.TrainServerStatusCache(ttl_seconds=60)
    first = cache.get()
    second = cache.get()
    assert first is second
    assert len(calls) == 1

    cache.get(force=True)
    assert len(calls) == 2

    cache.invalidate()
    cache.get()
    assert len(calls) == 3


def test_status_reports_ssh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_run_script(config, script, *args, **kwargs):
        return 255, "ssh: connect to host 192.0.2.10 port 22: Connection timed out"

    monkeypatch.setattr(train_server.ssh_ops, "run_script", failing_run_script)
    monkeypatch.setattr(
        train_server.TrainServerConfig,
        "from_env",
        classmethod(
            lambda cls, env=None: TrainServerConfig(
                host="192.0.2.10", port=22, user="trainer", root="~/Desktop/lelab_server"
            )
        ),
    )

    payload = train_server.TrainServerStatusCache().get()
    assert payload["ssh_ok"] is False
    assert "255" in payload["error"]
    assert payload["gpus"] == []


def test_instance_id_defaults_to_role() -> None:
    assert train_server.instance_id_from_env({"LELAB_ROLE": "windows-standby"}) == "windows-standby-local"
    assert train_server.instance_id_from_env({"LELAB_INSTANCE_ID": "custom"}) == "custom"


def test_status_endpoint_is_registered() -> None:
    from lelab.server import app

    assert "/system/train-server/status" in {route.path for route in app.routes}


def test_marker_json_is_serialisable_in_payload() -> None:
    payload = train_server.parse_status(SAMPLE, instance_id=INSTANCE)
    json.dumps(payload)  # must not raise: the route returns it as-is


def test_prune_plan_keeps_newest_and_skips_foreign() -> None:
    jobs = [
        {
            "id": "ours",
            "ours": True,
            "checkpoints": [
                {"step": 100, "size_mb": 210},
                {"step": 200, "size_mb": 211},
                {"step": 300, "size_mb": 212},
                {"step": 400, "size_mb": 213},
            ],
        },
        {
            "id": "someone_else",
            "ours": False,
            "checkpoints": [{"step": 1, "size_mb": 999}, {"step": 2, "size_mb": 999}],
        },
        {"id": "tiny", "ours": True, "checkpoints": [{"step": 5, "size_mb": 210}]},
    ]

    plan = train_server.prune_plan(jobs, keep_n=3)

    assert plan == [{"job_id": "ours", "step": 100, "size_mb": 210}]


def test_prune_plan_can_filter_jobs() -> None:
    jobs = [
        {
            "id": "a",
            "ours": True,
            "checkpoints": [{"step": 1, "size_mb": 10}, {"step": 2, "size_mb": 10}],
        },
        {
            "id": "b",
            "ours": True,
            "checkpoints": [{"step": 1, "size_mb": 20}, {"step": 2, "size_mb": 20}],
        },
    ]
    assert [item["job_id"] for item in train_server.prune_plan(jobs, keep_n=1)] == ["a", "b"]
    assert [item["job_id"] for item in train_server.prune_plan(jobs, keep_n=1, job_ids=["b"])] == ["b"]


def test_default_keep_checkpoints_is_configurable() -> None:
    from lelab.jobs import default_keep_checkpoints

    assert default_keep_checkpoints({}) == 3
    assert default_keep_checkpoints({"LELAB_TRAIN_KEEP_CHECKPOINTS": "5"}) == 5
    assert default_keep_checkpoints({"LELAB_TRAIN_KEEP_CHECKPOINTS": "0"}) == 1
    assert default_keep_checkpoints({"LELAB_TRAIN_KEEP_CHECKPOINTS": "junk"}) == 3
