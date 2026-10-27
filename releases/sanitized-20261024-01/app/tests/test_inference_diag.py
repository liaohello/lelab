"""Pure tests for cloud-inference log diagnostics (no hardware, no network)."""

from __future__ import annotations

import json

from lelab.utils.inference_diag import build_report, main, parse_client_log, parse_server_log

_CLIENT_LOG = """\
INFO 2026-09-19 21:04:00 t_client.py:499 {'actions_per_chunk': 50,
 'chunk_size_threshold': 0.5,
 'fps': 30,
DEBUG 2026-09-19 21:04:02 t_client.py:447 QUEUE SIZE: 0 (Must go: True)
DEBUG 2026-09-19 21:04:02 t_client.py:211 Observation serialization time: 0.002500s
DEBUG 2026-09-19 21:04:02 t_client.py:490 Control loop (ms): 210.00
DEBUG 2026-09-19 21:04:02 t_client.py:447 QUEUE SIZE: 25 (Must go: False)
DEBUG 2026-09-19 21:04:02 t_client.py:490 Control loop (ms): 180.00
DEBUG 2026-09-19 21:04:03 t_client.py:490 Control loop (ms): 2.60
DEBUG 2026-09-19 21:04:03 my_robot SOFollower read state: 1.1ms
DEBUG 2026-09-19 21:04:03 t_client.py:490 Control loop (ms): 2.70
DEBUG 2026-09-19 21:04:03 t_client.py:290 Received actions on device: cpu
INFO 2026-09-19 21:04:03 t_client.py:530 [PERF] rate=29.8Hz queue min/avg/max=13/22.4/25 sends=4 drop=3 rtt_ms n=4 p50=310 p90=420 must_go=1
WARNING 2026-09-19 21:04:04 ts/utils.py:123 Relative goal position magnitude had to be clamped to be safe.
{   'shoulder_lift': {   'original goal_pos': -103.15703582763672,
                         'safe goal_pos': -95.72527472527473}}
WARNING 2026-09-19 21:04:05 ts/utils.py:173 Goal position exceeded the configured startup-position workspace and was clamped.
{   'elbow_flex': {   'original goal_pos': 72.9277572631836,
                      'safe goal_pos': 80.48351648351648,
                      'startup origin_pos': 92.48351648351648},
    'shoulder_lift': {   'original goal_pos': -90.11990356445312,
                         'safe goal_pos': -90.24175824175825,
                         'startup origin_pos': -102.24175824175825}}
"""

_SERVER_LOG = """\
INFO 2026-09-19 21:04:03 y_server.py:224 Running inference for observation #0 (must_go: True)
INFO 2026-09-19 21:04:03 y_server.py:359 Preprocessing and inference took 0.6000s, action shape: torch.Size([1, 50, 6])
INFO 2026-09-19 21:04:03 y_server.py:242 Action chunk #0 generated | Total time: 842.71ms
INFO 2026-09-19 21:04:04 y_server.py:224 Running inference for observation #50 (must_go: False)
INFO 2026-09-19 21:04:04 y_server.py:359 Preprocessing and inference took 0.0180s, action shape: torch.Size([1, 50, 6])
INFO 2026-09-19 21:04:04 y_server.py:242 Action chunk #50 generated | Total time: 116.15ms
INFO 2026-09-19 21:04:05 y_server.py:242 Action chunk #100 generated | Total time: 196.89ms
"""


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_client_log_reports_loop_queue_and_clamps(tmp_path) -> None:
    path = _write(tmp_path, "robot_client_1.log", _CLIENT_LOG)

    stats = parse_client_log(path)

    assert stats["window_s"] == 5.0
    assert stats["config"] == {"actions_per_chunk": 50, "fps": 30, "chunk_size_threshold": 0.5}
    loop = stats["control_loop"]
    assert loop["iterations"] == 4
    assert loop["with_observation_ms"] == {
        "count": 2,
        "min": 180.0,
        "p50": 180.0,
        "p90": 210.0,
        "p99": 210.0,
        "max": 210.0,
        "mean": 195.0,
    }
    assert loop["without_observation_ms"]["count"] == 2

    queue = stats["action_queue"]
    assert queue["samples"] == 2
    assert queue["min"] == 0
    assert queue["max"] == 25
    assert queue["zero_samples"] == 1
    assert queue["zero_samples_after_fill"] == 0
    assert queue["must_go_true"] == 1

    assert stats["client_io"]["chunks_received"] == 1
    assert stats["client_io"]["serialize_ms"]["p50"] == 2.5
    assert stats["client_io"]["joint_read_ms"]["count"] == 1

    perf = stats["perf"]
    assert len(perf["windows"]) == 1
    assert perf["rate_hz"]["p50"] == 29.8
    assert perf["rtt_p50_ms"]["p50"] == 310.0
    assert perf["drops"] == 3
    assert perf["sends"] == 4

    clamps = stats["clamps"]
    assert clamps["total"] == 3
    assert clamps["by_kind"] == {"relative": 1, "workspace": 2}
    assert clamps["details"]["workspace:elbow_flex"]["magnitude_deg"]["max"] == 7.56
    assert clamps["details"]["relative:shoulder_lift"]["dominant_safe_value"] == -95.725


def test_workspace_clamp_is_marked_pinned_only_when_repeated(tmp_path) -> None:
    single = _write(tmp_path, "robot_client_single.log", _CLIENT_LOG)
    assert parse_client_log(single)["clamps"]["details"]["workspace:elbow_flex"]["saturated"] is False

    repeats = "".join(
        f"WARNING 2026-09-19 21:04:0{index} ts/utils.py:173 Goal position exceeded the configured "
        "startup-position workspace and was clamped.\n"
        f"{{   'elbow_flex': {{   'original goal_pos': {60.0 + index:.1f},\n"
        "                      'safe goal_pos': 80.48351648351648,\n"
        "                      'startup origin_pos': 92.48351648351648}}\n"
        for index in range(6)
    )
    path = _write(tmp_path, "robot_client_repeat.log", repeats)

    detail = parse_client_log(path)["clamps"]["details"]["workspace:elbow_flex"]

    assert detail["saturated"] is True
    assert detail["dominant_safe_value_hits"] == 6


def test_parse_server_log_separates_cold_start(tmp_path) -> None:
    path = _write(tmp_path, "policy_server.log", _SERVER_LOG)

    stats = parse_server_log(path)

    assert stats["chunks"] == 3
    assert stats["cold_start_ms"] == 842.71
    assert stats["chunk_total_ms_warm"]["count"] == 2
    assert stats["chunk_total_ms_warm"]["max"] == 196.89
    assert stats["gpu_inference_ms"]["max"] == 600.0
    assert stats["chunk_gap_s"]["max"] == 1.0
    assert stats["must_go_runs"] == 1


def test_findings_flag_stalls_and_saturated_limits(tmp_path) -> None:
    client_log = _write(tmp_path, "robot_client_findings.log", _CLIENT_LOG)
    server_log = _write(tmp_path, "policy_server_findings.log", _SERVER_LOG)

    report = build_report(client_log, server_log)
    codes = {finding["code"] for finding in report["findings"]}

    assert "control_loop_blocked_by_observation" in codes
    assert "inference_budget_ok" in codes
    # A single clamp event is not a saturation problem.
    assert "safety_limits_saturated" not in codes


def test_findings_flag_queue_starvation(tmp_path) -> None:
    text = _CLIENT_LOG.replace(
        "DEBUG 2026-09-19 21:04:03 t_client.py:490 Control loop (ms): 2.60",
        "DEBUG 2026-09-19 21:04:03 t_client.py:447 QUEUE SIZE: 0 (Must go: True)\n"
        "DEBUG 2026-09-19 21:04:03 t_client.py:490 Control loop (ms): 2.60",
    )
    path = _write(tmp_path, "robot_client_starved.log", text)

    report = build_report(path, None)
    codes = {finding["code"] for finding in report["findings"]}

    assert "action_queue_starvation" in codes


def test_findings_report_client_round_trip(tmp_path) -> None:
    path = _write(tmp_path, "robot_client_perf.log", _CLIENT_LOG)

    report = build_report(path, None)
    findings = {finding["code"]: finding for finding in report["findings"]}

    assert findings["cloud_round_trip_ok"]["message"].endswith("(typical p90 420ms).")
    assert "observation_upload_backlog" not in findings


def test_cli_emits_json(tmp_path, capsys) -> None:
    client_log = _write(tmp_path, "robot_client_cli.log", _CLIENT_LOG)

    exit_code = main(["--client-log", str(client_log), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["client"]["path"] == str(client_log)
    assert payload["server"] is None


def test_cli_rejects_missing_logs(tmp_path) -> None:
    assert main(["--client-log", str(tmp_path / "missing.log")]) == 2
