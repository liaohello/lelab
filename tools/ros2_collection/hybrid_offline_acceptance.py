#!/usr/bin/env python3
"""Offline browser acceptance for the LanderPi hybrid workspace.

The real FastAPI routes, built Vite bundle, production sync/export code and a
real Firefox instance are used here.  Only the device/car transport edge is
replaced: the upper facade is the explicit `simulated_hybrid_manual_v1` fixture
and a local HTTP car serves one synthetic rosbag2 sqlite3 session for sync.

This is software acceptance only.  It does not open a serial port, camera,
gamepad, robot service, SSH connection or real actuator output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = ROOT / "app_ros_mobile"
TEST_ROOT = APP_ROOT / "tests"
sys.path[:0] = [str(APP_ROOT), str(TEST_ROOT), str(ROOT / "tools/ros2_collection")]

import t4_fixtures as fx  # noqa: E402
from webdriver_firefox import FirefoxDriver  # noqa: E402
from lelab.hybrid_fixtures import calibration_assets_fixture  # noqa: E402

SESSION_ID = "ros_browser_9dsim"


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _CarServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manifest: dict[str, Any], archive: bytes):
        super().__init__(address, _CarHandler)
        self.manifest = manifest
        self.archive = archive
        self.requests: list[dict[str, Any]] = []


class _CarHandler(BaseHTTPRequestHandler):
    server: _CarServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.server.requests.append({"method": "GET", "path": self.path})
        if self.path == "/status":
            self._json(200, {
                "ok": True, "agent": "offline-car-fixture", "read_only": True,
                "profile": "simulated_hybrid_manual_v1", "simulated": True,
                "control": {"active": False, "mode": "idle", "estop_latched": False},
                "recording": {"active": False, "status": "raw_ready"},
                "sensors": {}, "joints": {}, "topics": {"missing": []},
            })
            return
        if self.path == "/health":
            self._json(200, {"ok": True, "agent": "offline-car-fixture"})
            return
        if self.path == "/sessions":
            self._json(200, {"sessions": [{
                "session_id": SESSION_ID,
                "status": "raw_ready",
                "path": str(self.server.manifest.get("_session_dir", "")),
            }]})
            return
        if self.path == f"/sessions/{SESSION_ID}/manifest":
            self._json(200, {
                key: value for key, value in self.server.manifest.items()
                if not key.startswith("_")
            })
            return
        if self.path == f"/sessions/{SESSION_ID}/archive":
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(self.server.archive)))
            self.end_headers()
            self.wfile.write(self.server.archive)
            return
        self._json(404, {"error": "not_found", "path": self.path})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append({
            "method": "POST", "path": self.path,
            "body": json.loads(body) if body else None,
        })
        self._json(404, {"error": "not_found", "path": self.path})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wait_http(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                response.read()
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.15)
    raise RuntimeError(f"{url} did not become ready: {last_error}")


def _button_script(scope: str, text: str, allow_disabled: bool = False) -> tuple[str, list[Any]]:
    return (
        """
        const scope = document.querySelector(arguments[0]);
        if (!scope) return 'missing-scope';
        const wanted = arguments[1];
        const button = [...scope.querySelectorAll('button')].find(
          item => (item.innerText || '').includes(wanted)
        );
        if (!button) return 'missing-button';
        if (button.disabled && !arguments[2]) return 'disabled';
        button.click();
        return 'clicked';
        """,
        [scope, text, allow_disabled],
    )


def click_button(browser: FirefoxDriver, text: str, *, scope: str = "body",
                 enabled: bool = True, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    script, args = _button_script(scope, text, allow_disabled=not enabled)
    last = None
    while time.monotonic() < deadline:
        last = browser.execute(script, args)
        if last == "clicked":
            return
        time.sleep(0.2)
    raise RuntimeError(f"could not click {text!r} inside {scope}: {last}")


def set_field(browser: FirefoxDriver, selector: str, value: str) -> None:
    browser.execute(
        """
        const element = document.querySelector(arguments[0]);
        if (!element) return false;
        const prototype = element instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : element instanceof HTMLSelectElement
            ? HTMLSelectElement.prototype
            : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
        setter.call(element, arguments[1]);
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
        """,
        [selector, value],
    )


def select_by_label(browser: FirefoxDriver, label: str, value: str) -> None:
    browser.execute(
        """
        const element = document.querySelector(`[aria-label="${arguments[0]}"]`);
        if (!element) return false;
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
        setter.call(element, arguments[1]);
        element.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
        """,
        [label, value],
    )


def make_session(root: Path) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    assets = calibration_assets_fixture(measured_at_utc="2026-11-03T00:00:00Z")
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
            "identity": "offline_browser_9d_v1",
            "qualified": True,
            "not_hardware": True,
            "evidence_kind": "offline_integration_only",
        },
        "training_export": True,
    })
    built = fx.build_synced_session(
        root / "car", SESSION_ID, episodes=2, seconds=20.0, fps=10,
        meta=meta, manifest_overrides={"profile": "simulated_hybrid_manual_v1"},
    )
    manifest, archive = fx.package_session(built["session_dir"])
    manifest["_session_dir"] = str(built["session_dir"])
    return manifest, archive, assets["base"]


def run_browser(url: str, evidence: Path, assets: dict[str, Any], repo_id: str) -> dict[str, Any]:
    evidence.mkdir(parents=True, exist_ok=True)
    actions: list[str] = []
    screenshots: list[str] = []

    def shot(name: str) -> None:
        path = evidence / f"{name}.png"
        browser.screenshot(path)
        screenshots.append(str(path))

    def scroll_to(selector: str) -> None:
        browser.execute(
            """
            const node = document.querySelector(arguments[0]);
            if (node) node.scrollIntoView({block: 'start'});
            return Boolean(node);
            """,
            [selector],
        )
        time.sleep(0.3)

    with FirefoxDriver(evidence_dir=evidence) as browser:
        browser.navigate(url + "/ros-mobile")
        browser.wait_text("联合遥操作 · 网关控制", timeout_s=30)
        browser.wait_text("模拟能力声明", timeout_s=20)
        shot("01-simulated-ready")

        select_by_label(browser, "联合 profile", "simulated_hybrid_manual_v1")
        time.sleep(0.2)
        click_button(browser, "获取联合会话")
        actions.append("acquire")
        browser.wait_text("ready", timeout_s=20)

        # Calibration validation is available before a session; exercise both a
        # readable error and the successful path.
        set_field(browser, '[aria-label="标定身份"]', "offline-browser-base")
        set_field(browser, '[aria-label="标定 JSON"]', "{")
        click_button(browser, "校验标定")
        browser.wait_text("JSON 解析失败", timeout_s=10)
        click_button(browser, "关闭错误")
        set_field(browser, '[aria-label="标定 JSON"]', json.dumps(assets, ensure_ascii=False))
        click_button(browser, "校验标定")
        actions.append("calibration-validate")
        time.sleep(0.5)

        control_sequence = [
            "无输出预览", "对齐", "机械臂待命", "接合离合",
            "释放离合", "无输出预览", "对齐", "机械臂待命", "接合离合",
            "暂停全部运动", "锁定底盘",
        ]
        for label in control_sequence:
            click_button(browser, label)
            actions.append(label)
            time.sleep(0.35)

        click_button(browser, "开始联合录制")
        actions.append("start_session")
        time.sleep(0.4)
        for episode in range(2):
            click_button(browser, "开始联合回合")
            actions.append(f"episode-{episode}-start")
            time.sleep(0.3)
            click_button(browser, "阶段 observe")
            actions.append(f"episode-{episode}-observe")
            time.sleep(0.3)
            click_button(browser, "阶段 grasp")
            actions.append(f"episode-{episode}-grasp")
            time.sleep(0.3)
            click_button(browser, "暂停并结束联合回合")
            actions.append(f"episode-{episode}-end")
            time.sleep(0.5)
        click_button(browser, "暂停并停止联合录制")
        actions.append("stop_session")
        time.sleep(0.5)
        shot("02-recorded-two-episodes")

        review_scope = '[data-testid="hybrid-data-review"]'
        click_button(browser, "刷新 session", scope=review_scope)
        select_by_label(browser, "选择 session", SESSION_ID)
        click_button(browser, "同步原始数据", scope=review_scope)
        browser.wait_text("同步 raw_ready", timeout_s=30)
        actions.append("sync")

        # Let the real export worker start, then exercise cancellation and retry.
        click_button(browser, "导出 LeRobot", scope=review_scope)
        time.sleep(0.35)
        cancel_result = browser.execute(
            """
            const scope = document.querySelector(arguments[0]);
            const button = [...scope.querySelectorAll('button')].find(
              item => (item.innerText || '').includes('取消导出') && !item.disabled
            );
            if (!button) return false;
            button.click();
            return true;
            """,
            [review_scope],
        )
        actions.append("export-cancel-clicked" if cancel_result else "export-too-fast-for-cancel")
        if cancel_result:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = browser.execute(
                    "return document.body ? document.body.innerText : ''"
                )
                if "canceled" in str(state) or "已取消" in str(state):
                    break
                time.sleep(0.2)
            click_button(browser, "导出 LeRobot", scope=review_scope, timeout_s=30)
        browser.wait_text("阶段切片：", timeout_s=90)
        browser.wait_text("episodes", timeout_s=10)
        actions.append("export")
        # The export report and the phase curves both render inside the review
        # section below the fold; without scrolling, Firefox captures the same
        # viewport again and the screenshots cannot tell the two states apart.
        scroll_to(review_scope)
        shot("03-export-report")

        click_button(browser, "读取分集", scope=review_scope)
        time.sleep(0.4)
        select_by_label(browser, "选择 episode", "0")
        select_by_label(browser, "选择阶段", "1")
        click_button(browser, "读取曲线", scope=review_scope)
        curves_scope = '[data-testid="phase-curves"]'
        browser.find(curves_scope, timeout_s=30)
        actions.append("read-grasp-curves")
        scroll_to(curves_scope)
        shot("04-phase-curves")

        browser.navigate(url + "/dataset-viewer")
        browser.wait_text("数据可视化", timeout_s=30)
        browser.wait_text("阶段切片", timeout_s=30)
        click_button(browser, "grasp")
        time.sleep(0.5)
        video = browser.execute(
            "const v=document.querySelector('video'); return v ? v.getAttribute('src') : null;"
        )
        if not video or "#t=" not in str(video):
            raise RuntimeError(f"dataset viewer did not expose a bounded phase video: {video!r}")
        actions.append("dataset-viewer-grasp-video")
        shot("05-dataset-viewer-grasp")

        browser.navigate(url + "/ros-mobile")
        browser.wait_text("联合遥操作 · 网关控制", timeout_s=30)
        click_button(browser, "急停")
        actions.append("estop")
        browser.wait_text("estop", timeout_s=15)
        click_button(browser, "清除急停")
        actions.append("clear-estop")
        click_button(browser, "释放联合会话")
        actions.append("release")
        shot("06-estop-clear-release")
        browser_errors = browser.browser_errors()

    return {
        "actions": actions,
        "screenshots": screenshots,
        "browser_errors": browser_errors,
        "repo_id": repo_id,
        "phase_video": video,
        "driver_teardown": getattr(browser, "teardown_error", None),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    root = Path(args.root or f"/tmp/lelab-hybrid-offline-{stamp}").expanduser().resolve()
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    collection = root / "collection"
    datasets = root / "datasets"

    sixd = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "tests/test_t4_hybrid_recording.py::"
            "test_real_sqlite_sync_export_sdk_and_viewer_two_episodes",
            "-q", "-p", "no:cacheprovider",
        ],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    (evidence / "sixd-e2e.log").write_text(
        sixd.stdout + sixd.stderr, encoding="utf-8"
    )
    (evidence / "sixd-e2e.exit").write_text(str(sixd.returncode), encoding="utf-8")
    if sixd.returncode != 0:
        report = {
            "status": "failed",
            "root": str(root),
            "error": "six-dimensional locked-chassis production path failed",
            "exit": sixd.returncode,
            "log": str(evidence / "sixd-e2e.log"),
        }
        (evidence / "acceptance-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
        return 1

    manifest, archive, base_asset = make_session(root)
    car_port = free_port()
    car = _CarServer(("127.0.0.1", car_port), manifest, archive)
    car_thread = threading.Thread(target=car.serve_forever, daemon=True)
    car_thread.start()

    os.environ["LELAB_ROS_AGENT_URL"] = f"http://127.0.0.1:{car_port}"
    os.environ["LELAB_ROS_COLLECTION_ROOT"] = str(collection)
    os.environ["HF_LEROBOT_HOME"] = str(datasets)
    os.environ["LELAB_HYBRID_SIMULATED"] = "1"

    import uvicorn
    from lelab import ros_mobile
    from lelab.server import app

    web_port = args.port or free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=web_port, log_level="info",
        access_log=False, timeout_graceful_shutdown=2,
    ))
    web_thread = threading.Thread(target=server.run, daemon=True)
    web_thread.start()
    url = f"http://127.0.0.1:{web_port}"
    _wait_http(url + "/health")

    report: dict[str, Any] = {
        "status": "running",
        "root": str(root),
        "web_url": url,
        "car_url": f"http://127.0.0.1:{car_port}",
        "session_id": SESSION_ID,
        "simulation_identity": manifest["metadata"].get("simulation_identity"),
        "transport_substitutions": [
            "serial: none (not opened)",
            "camera: no frame endpoint used by hybrid fixture",
            "gamepad/leader: FixtureUpperLease + FixtureT1Driver",
            "car recorder transport: production ROS-free controller with InMemoryRecordingCarClient",
            "car archive/readback transport: local HTTP fixture serving a synthetic rosbag2 sqlite3 session",
        ],
        "sixd_locked_chassis": {
            "status": "passed",
            "log": str(evidence / "sixd-e2e.log"),
            "exit": 0,
        },
    }
    stage = "browser"
    try:
        repo_id = f"lelab/ros-mobile-{SESSION_ID.removeprefix('ros_').replace('_', '-').lower()}"
        report["browser"] = run_browser(url, evidence, base_asset, repo_id)
        stage = "capabilities"
        report["capabilities"] = ros_mobile.hybrid_capabilities()
        stage = "car_requests"
        report["car_requests"] = car.requests
        stage = "sync"
        report["sync"] = ros_mobile.sync_session(SESSION_ID)
        stage = "synced_manifest"
        synced = ros_mobile.UPPER_SESSIONS / SESSION_ID
        report["synced_manifest_sha256"] = _sha256_file(synced / "manifest.json")
        stage = "dataset_listing"
        report["dataset_root"] = str(datasets / repo_id)
        report["dataset_files"] = [
            str(path.relative_to(datasets)) for path in sorted((datasets / repo_id).rglob("*"))
            if path.is_file()
        ]
        stage = "sdk_readback"
        from lerobot.datasets import LeRobotDataset

        dataset = LeRobotDataset(repo_id=repo_id, root=datasets / repo_id)
        frame = dataset[0]
        report["sdk_readback"] = {
            "episodes": int(dataset.meta.total_episodes),
            "frames": int(dataset.meta.total_frames),
            "state_dims": int(frame["observation.state"].shape[-1]),
            "action_dims": int(frame["action"].shape[-1]),
            "source": frame.get("provenance") if isinstance(frame, dict) else None,
        }
        report["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        import traceback as _traceback

        report.update({
            "status": "failed",
            "failed_stage": stage,
            "error": f"{type(exc).__name__}: {exc}",
            "error_traceback": _traceback.format_exc(),
        })
        try:
            with FirefoxDriver(evidence_dir=evidence) as browser:
                browser.navigate(url + "/ros-mobile")
                browser.screenshot(evidence / "failure.png")
        except Exception:
            pass
    finally:
        server.should_exit = True
        web_thread.join(timeout=15)
        car.shutdown()
        car.server_close()
        car_thread.join(timeout=5)

    report_path = evidence / "acceptance-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not args.keep:
        print(f"evidence_root={root}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
