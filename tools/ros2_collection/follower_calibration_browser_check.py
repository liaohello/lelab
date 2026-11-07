"""Firefox check for the LanderPi follower calibration page, against the HTTP fixture.

SIMULATED.  Starts ``http_follower_calibration_fixture`` (the real car-side state
machine behind the frozen routes) plus a real uvicorn instance of the app in
hardware mode pointed at it, then drives ``/landerpi`` in Firefox.

What it proves (and what the acceptance asks for):
  * the gripper slider can actually be dragged and the car receives raw targets,
  * releasing the pointer stops new targets,
  * the mark buttons are disabled with a readable reason before a drag and become
    available once the feedback has settled,
  * the page reports "受控运动中：仅 ID10" instead of "执行输出关闭" while dragging.

Usage:
    python3 tools/ros2_collection/follower_calibration_browser_check.py \
        --out docs/evidence/landerpi-hardware-20261104/offline-calibration
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "ros2_collection"))

import http_follower_calibration_fixture as fixture  # noqa: E402
from webdriver_firefox import FirefoxDriver  # noqa: E402

APP_ROOT = ROOT / "app_ros_mobile"
VENV_PYTHON = APP_ROOT / ".venv" / "bin" / "python"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def _wait(predicate, *, timeout_s: float, interval_s: float = 0.1, label: str = ""):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    raise AssertionError(f"timeout waiting for {label or predicate}; last={last!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "docs" / "evidence"
                                             / "landerpi-hardware-20261104"
                                             / "offline-calibration"))
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    fixture_port = _free_port()
    app_port = _free_port()
    server = fixture.serve("127.0.0.1", fixture_port)
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixture_url = f"http://127.0.0.1:{fixture_port}"
    app_url = f"http://127.0.0.1:{app_port}"
    work = out / "fixture-run"
    work.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "LELAB_HYBRID_HARDWARE": "1",
        "LELAB_ROS_AGENT_URL": fixture_url,
        "LELAB_HYBRID_CALIBRATION_ROOT": str(work / "calibration"),
        "LELAB_ROS_COLLECTION_ROOT": str(work / "collection"),
        "PYTHONNOUSERSITE": "1",
    }
    log_path = out / "browser-check-uvicorn.log"
    log = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "uvicorn", "lelab.server:app",
         "--host", "127.0.0.1", "--port", str(app_port)],
        cwd=str(APP_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT, text=True)

    report: dict = {"fixture": fixture_url, "app": app_url, "checks": [],
                    "browser_errors": [], "passed": False}
    driver = None
    try:
        _wait(lambda: _probe(f"{app_url}/ros-mobile/hybrid/status"),
              timeout_s=60, label="app status")
        driver = FirefoxDriver(evidence_dir=out, headless=True)
        driver.navigate(f"{app_url}/landerpi")
        driver.wait_text("LanderPi 设置", timeout_s=30)
        driver.click_text("树莓派从臂", tag="button")
        time.sleep(0.5)

        # 1) Before any drag the mark buttons must be blocked with a reason.
        blocker = driver.text('[data-testid="mark-blocker"]')
        report["checks"].append({"name": "mark_blocked_before_drag",
                                 "detail": blocker, "ok": "先拖动滑轨" in blocker})

        # 2) Confirm both safety checkboxes and start the job.
        driver.execute(
            "const set=(sel,v)=>{const e=document.querySelector(sel);"
            "const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
            "setter.call(e,String(v));e.dispatchEvent(new Event('input',{bubbles:true}));};"
            "set('[data-testid=safe-min]',300);set('[data-testid=safe-max]',600);return true;")
        driver.execute(
            "const boxes=[...document.querySelectorAll('input[type=checkbox]')];"
            "boxes.forEach(b=>{if(!b.checked)b.click();});"
            "return boxes.length;")
        time.sleep(0.3)
        driver.click('[data-testid="start-calibration"]')
        _wait(lambda: driver.execute(
            "const el=document.querySelector('[data-testid=gripper-slider]');"
            "return el && !el.disabled;"), timeout_s=45, label="slider enabled")

        # 3) Drag: two positions, then release.
        before = len(_get_json(f"{fixture_url}/fixture/log")["commands"])
        driver.execute(
            "const el=document.querySelector('[data-testid=gripper-slider]');"
            "el.scrollIntoView({block:'center'});"
            "const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
            "window.__setSlider=(v)=>{setter.call(el,String(v));"
            "el.dispatchEvent(new Event('input',{bubbles:true}));};"
            "el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:1,isPrimary:true}));"
            "window.__setSlider(700);"
            "return el.value;")
        time.sleep(0.6)
        driver.execute("window.__setSlider(820); return document.querySelector('[data-testid=gripper-slider]').value;")
        time.sleep(0.6)
        _wait(lambda: driver.execute(
            "const e=document.querySelector('[data-testid=calibration-phase-debug]');"
            "return e?.dataset.phase === 'gripper_drag';"),
            timeout_s=5, label="UI gripper_drag phase")
        dragging_state = driver.text('[data-testid="gripper-state"]')
        motion_badge = driver.execute(
            "return document.body.innerText.includes('受控运动中：仅 ID10');")
        driver.execute("window.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:1}));"
                       "return true;")
        time.sleep(0.3)
        after_release = _get_json(f"{fixture_url}/fixture/log")["commands"]
        time.sleep(1.4)
        settled = _get_json(f"{fixture_url}/fixture/log")["commands"]

        sent = after_release[before:]
        report["checks"].extend([
            {"name": "slider_drag_sends_targets", "count": len(sent),
             "raws": [row["raw"] for row in sent],
             "ok": len(sent) >= 2},
            {"name": "slider_direction", "ok": bool(sent) and sent[-1]["raw"] >= sent[0]["raw"]},
            {"name": "only_id10", "ok": all(row["servo_id"] == 10 for row in sent)},
            {"name": "pointerup_stops_new_targets",
             "before": len(after_release), "after": len(settled),
             "ok": len(settled) == len(after_release)},
            {"name": "motion_badge_is_not_output_off", "ok": bool(motion_badge),
             "detail": motion_badge},
            {"name": "drag_state_text", "detail": dragging_state,
             "ok": "受控" in dragging_state or "发送" in dragging_state},
        ])

        # 4) After the settle window the mark buttons become usable.
        closed_enabled = _wait(lambda: driver.execute(
            "const b=document.querySelector('[data-testid=mark-closed]');"
            "return b && !b.disabled;"),
            timeout_s=5, label="mark button enabled")
        report["checks"].append({"name": "mark_enabled_after_settle",
                                 "ok": bool(closed_enabled)})
        if closed_enabled:
            driver.click('[data-testid="mark-closed"]')
            time.sleep(0.8)
            marked = _get_json(f"{fixture_url}/fixture/log")["job"]["gripper"]
            report["checks"].append({
                "name": "closed_mark_recorded",
                "rounds": marked.get("rounds_completed") if marked else None,
                "marks": len(marked.get("marks", [])) if marked else 0,
                "ok": bool(marked) and len(marked.get("marks", [])) == 1,
            })
            disabled_again = driver.execute(
                "const b=document.querySelector('[data-testid=mark-closed]');"
                "return b && b.disabled;")
            report["checks"].append({"name": "closed_mark_button_state",
                                     "ok": bool(disabled_again)})

        driver.screenshot(out / "browser-check.png")
        report["browser_errors"] = list(driver.browser_errors())
        report["checks"].append({"name": "no_browser_script_errors",
                                 "ok": not report["browser_errors"]})
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        if driver is not None:
            try:
                report["page_debug"] = driver.execute(
                    "const e=document.querySelector('[data-testid=calibration-phase-debug]');"
                    "return {phase:e?.dataset.phase ?? null,"
                    "targetPhase:e?.dataset.targetPhase ?? null,"
                    "text:document.body?.innerText?.slice(-4000) ?? ''};")
                driver.screenshot(out / "browser-check-failure.png")
                report["browser_errors"] = list(driver.browser_errors())
            except Exception:  # noqa: BLE001
                pass
    finally:
        if driver is not None and not args.keep_open:
            driver.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()
        fixture.STOP.set()
        server.shutdown()

    report["passed"] = bool(report["checks"]) and all(
        check.get("ok") for check in report["checks"]) and not report.get("error")
    (out / "browser-check.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    for check in report["checks"]:
        print(("PASS " if check.get("ok") else "FAIL ") + check["name"],
              {k: v for k, v in check.items() if k not in ("name", "ok")})
    if report.get("error"):
        print("ERROR:", report["error"])
    print("PASSED" if report["passed"] else "FAILED")
    return 0 if report["passed"] else 1


def _probe(url: str) -> bool:
    try:
        _get_json(url, timeout=2.0)
        return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    raise SystemExit(main())
