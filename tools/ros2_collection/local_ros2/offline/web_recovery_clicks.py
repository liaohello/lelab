#!/usr/bin/env python3
"""OFFLINE ONLY: click the operator's way out of an emergency stop, twice, in the page.

This is the acceptance the plan asked for in the browser, against the production React
bundle and the production hybrid routes, with only the device edge simulated:

    急停 → 清除急停 → 释放联合会话 → 获取联合会话 → 无输出预览 → 对齐
    → 机械臂待命 → 接合离合

and it is asserted at each step where motion actually is or is not allowed:

* clearing never grants movement - the car publishes nothing until an explicit engage;
* 机械臂待命 still publishes nothing; only 接合离合 does;
* the car-side latch and its clearance are read from the agent and the control process,
  not from what the page's pill says.

Screenshots and a JSON report are written to --out-dir. Nothing here is a physical stop:
"stopped" means the software stopped admitting and emitting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import time
import urllib.request

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import Select

CAR_DOC = None  # set from --car-doc
ARM = ("无输出预览", "对齐", "机械臂待命", "接合离合")
ARM_STATE = {"无输出预览": "preview", "对齐": "aligned", "机械臂待命": "armed",
             "接合离合": "following"}


class Recovery:
    def __init__(self, base_url: str, driver_url: str, agent_url: str, car_doc: str,
                 out: pathlib.Path, profile: str) -> None:
        self.base = base_url.rstrip("/")
        self.agent = agent_url.rstrip("/")
        self.car_doc = pathlib.Path(car_doc)
        self.out = out
        self.profile = profile
        self.out.mkdir(parents=True, exist_ok=True)
        self.report = {"base_url": self.base, "agent_url": self.agent,
                       "data_identity_expected": "simulated_offline",
                       "steps": [], "checks": [], "screenshots": [], "rounds": []}
        options = Options()
        options.add_argument("-headless")
        self.driver = webdriver.Remote(command_executor=driver_url, options=options)
        self.driver.set_window_size(1600, 1400)
        self.driver.timeouts.implicit_wait = 0

    # -- primitives ---------------------------------------------------------
    def button(self, text: str):
        return self.driver.find_element(By.XPATH, f"//button[normalize-space()='{text}']")

    def enabled(self, text: str) -> bool:
        try:
            return self.button(text).is_enabled()
        except WebDriverException:
            return False

    def click(self, text: str, timeout: float = 40.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.enabled(text):
                self.button(text).click()
                return
            time.sleep(0.2)
        raise TimeoutException(f"button never became clickable: {text}")

    def wait(self, probe, label: str, timeout: float = 40.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = probe()
            except WebDriverException as exc:
                last = f"driver:{type(exc).__name__}"
            else:
                if last:
                    return last
            time.sleep(0.2)
        raise TimeoutException(f"timed out waiting for {label} (last={last!r})")

    def status(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/ros-mobile/hybrid/status", timeout=10) as r:
            return json.loads(r.read())

    def car(self) -> dict:
        with urllib.request.urlopen(f"{self.agent}/status", timeout=10) as r:
            answer = json.loads(r.read())
        control = answer.get("control") or {}
        licence = answer.get("licence") or {}
        document = {}
        try:
            document = json.loads(self.car_doc.read_text())
        except (OSError, ValueError):
            document = {}
        return {"control_active": control.get("active"),
                "agent_estop_latched": control.get("estop_latched"),
                "frame_outputs_requested": licence.get("frame_outputs_requested"),
                "frame_outputs_enabled": licence.get("frame_outputs_enabled"),
                "frame_stream_connected": licence.get("frame_stream_connected"),
                "process_latched_fault": document.get("latched_fault"),
                "process_outputs_enabled": document.get("outputs_enabled"),
                "process_reengage_required": document.get("reengage_required"),
                "emitted": (document.get("counters") or {}).get("frames_emitted"),
                "queue_depth": (document.get("admission") or {}).get("queue_depth")}

    def shot(self, name: str) -> None:
        path = self.out / f"{name}.png"
        self.driver.save_screenshot(str(path))
        self.report["screenshots"].append(
            {"name": name, "file": path.name,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    def check(self, name: str, ok, detail=None) -> bool:
        self.report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}: "
              f"{json.dumps(detail, ensure_ascii=False, default=str)[:230]}", flush=True)
        return bool(ok)

    def step(self, name: str, **detail) -> None:
        self.report["steps"].append({"name": name, "at": time.strftime("%H:%M:%S"), **detail})

    def arm_state(self, status: dict) -> str | None:
        return (((status.get("channels") or {}).get("arm") or {}).get("state"))

    def wait_arm(self, expected: str, timeout: float = 40.0) -> dict:
        return self.wait(lambda: (lambda s: s if self.arm_state(s) == expected else None)(
            self.status()), f"arm state {expected}", timeout)

    # -- the flow -----------------------------------------------------------
    def open_and_acquire(self) -> dict:
        self.driver.get(f"{self.base}/ros-mobile")
        self.wait(lambda: self.driver.find_elements(
            By.CSS_SELECTOR, '[data-testid="hybrid-panel"]'), "the panel", 60.0)
        select = self.driver.find_element(By.CSS_SELECTOR, "select[aria-label='联合 profile']")
        Select(select).select_by_visible_text(self.profile)
        self.click("获取联合会话")
        ready = self.wait(lambda: (lambda s: s if s.get("state") == "ready" else None)(
            self.status()), "a ready session", 90.0)
        self.shot(f"acquire-{self.round_number}")
        return ready

    def engage_to_following(self) -> None:
        for label in ARM:
            self.click(label)
            self.wait_arm(ARM_STATE[label])
            if label == "机械臂待命":
                # The last step before motion: armed must still produce nothing.
                armed = self.car()
                time.sleep(2.0)
                self.check(f"round{self.round_number}_arming_alone_produces_nothing",
                           self.car()["emitted"] == armed["emitted"]
                           and armed["frame_outputs_enabled"] is not True,
                           {"emitted_at_arm": armed["emitted"],
                            "emitted_two_seconds_later": self.car()["emitted"],
                            "frame_outputs_enabled": armed["frame_outputs_enabled"]})
                self.shot(f"armed-no-output-{self.round_number}")
        self.wait_arm("following")

    def one_round(self) -> dict:
        record: dict = {"round": self.round_number}
        self.open_and_acquire()
        self.engage_to_following()
        moving = self.car()
        time.sleep(1.0)
        flowing = self.car()
        self.check(f"round{self.round_number}_publishing_before_the_stop",
                   flowing["emitted"] > moving["emitted"],
                   {"emitted": (moving["emitted"], flowing["emitted"])})

        started = time.monotonic()
        self.click("急停")
        self.wait(lambda: (lambda s: s if s.get("state") == "estop" else None)(self.status()),
                  "the estop state", 30.0)
        record["estop_state_after_s"] = round(time.monotonic() - started, 3)
        self.shot(f"estop-{self.round_number}")
        latched = None
        waited = time.monotonic()
        while time.monotonic() - waited < 5.0:
            view = self.car()
            if view["agent_estop_latched"] is True and view["process_latched_fault"] not in (None,):
                latched = view
                break
            time.sleep(0.1)
        record["car_latched_after_s"] = (round(time.monotonic() - waited, 3) if latched else None)
        self.check(f"round{self.round_number}_car_side_latched_after_the_page_stop",
                   latched is not None,
                   {"agent_estop_latched": (latched or {}).get("agent_estop_latched"),
                    "process_latched_fault": (latched or {}).get("process_latched_fault"),
                    "frame_outputs_enabled": (latched or {}).get("frame_outputs_enabled")})
        at_stop = self.car()["emitted"]
        time.sleep(2.0)
        self.check(f"round{self.round_number}_stop_holds_publication",
                   self.car()["emitted"] == at_stop,
                   {"emitted_at_stop": at_stop, "later": self.car()["emitted"]})

        self.click("清除急停")
        cleared = self.wait(
            lambda: (lambda s: s if s.get("state") != "estop" else None)(self.status()),
            "the page to leave estop", 40.0)
        record["state_after_clear"] = cleared.get("state")
        record["last_error_after_clear"] = cleared.get("last_error")
        self.shot(f"cleared-{self.round_number}")
        unlatched = None
        waited = time.monotonic()
        while time.monotonic() - waited < 8.0:
            view = self.car()
            if view["agent_estop_latched"] is not True and view["process_latched_fault"] is None:
                unlatched = view
                break
            time.sleep(0.1)
        record["car_clear_waited_s"] = round(time.monotonic() - waited, 3)
        self.check(f"round{self.round_number}_both_car_layers_report_the_clear",
                   unlatched is not None,
                   {"agent_estop_latched": (unlatched or {}).get("agent_estop_latched"),
                    "process_latched_fault": (unlatched or {}).get("process_latched_fault"),
                    "reengage_required": (unlatched or {}).get("process_reengage_required")})
        self.check(f"round{self.round_number}_clear_granted_no_motion",
                   self.arm_state(cleared) != "following"
                   and self.car()["frame_outputs_enabled"] is not True,
                   {"arm": self.arm_state(cleared),
                    "frame_outputs_enabled": self.car()["frame_outputs_enabled"]})

        released_before = self.car()["emitted"]
        self.click("释放联合会话")
        self.wait(lambda: (lambda s: s if s.get("state") == "disconnected" else None)(
            self.status()), "the session released", 40.0)
        time.sleep(2.0)
        self.check(f"round{self.round_number}_release_leaves_nothing_emitting",
                   self.car()["emitted"] == released_before,
                   {"emitted": (released_before, self.car()["emitted"]),
                    "control_active": self.car()["control_active"]})
        return record

    def run(self, rounds: int) -> int:
        failures = 0
        for index in range(1, rounds + 1):
            self.round_number = index
            print(f"--- recovery round {index}", flush=True)
            try:
                self.report["rounds"].append(self.one_round())
            except Exception as exc:  # noqa: BLE001 - the report is the deliverable
                failures += 1
                self.report.setdefault("round_errors", {})[str(index)] = (
                    f"{type(exc).__name__}: {str(exc)[:300]}")
                print(f"ERROR round {index}: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
                try:
                    self.shot(f"round{index}-error")
                except Exception:  # noqa: BLE001
                    pass
                break
        try:
            self.report["final_status"] = self.status()
            self.report["final_car"] = self.car()
        except Exception as exc:  # noqa: BLE001
            self.report["final_read_error"] = f"{type(exc).__name__}:{str(exc)[:160]}"
        self.report["failed"] = [c["name"] for c in self.report["checks"] if not c["ok"]]
        (self.out / "web-recovery.json").write_text(
            json.dumps(self.report, indent=2, default=str) + "\n", encoding="utf-8")
        try:
            self.driver.quit()
        except Exception as exc:  # noqa: BLE001 - geckodriver owns the process
            print(f"driver teardown note: {type(exc).__name__}", flush=True)
        return 1 if failures or self.report["failed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18288")
    parser.add_argument("--driver-url", default="http://127.0.0.1:4444")
    parser.add_argument("--agent-url", default="http://127.0.0.1:18131")
    parser.add_argument("--car-doc", required=True)
    parser.add_argument("--profile", default="simulated_hybrid_manual_v1")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    return Recovery(args.base_url, args.driver_url, args.agent_url, args.car_doc,
                    pathlib.Path(args.out_dir), args.profile).run(args.rounds)


if __name__ == "__main__":
    raise SystemExit(main())
