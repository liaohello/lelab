#!/usr/bin/env python3
"""OFFLINE ONLY: drive the real production web page through a two-episode recording.

This is a browser session against the built React bundle and the production hybrid
routes, with the simulated device boundary behind it. Every state change is initiated
by a click; reads use the page's own same-origin fetch so the assertions are about the
server's answer and not about the DOM label.

Screenshots, console messages and a step report are written to --out-dir. Nothing here
is hardware evidence, and the produced dataset stays simulated_offline / not
training-eligible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import time
import urllib.parse
import urllib.request

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By

PHASE_SECONDS = 2.0
EPISODE_NAME = {"observe": "观察", "grasp": "抓取"}


def quote_repo(repo_id: str) -> str:
    """The page encodes each repo path segment; the reader has to match it."""
    return "/".join(urllib.parse.quote(part) for part in repo_id.split("/"))


class Closure:
    def __init__(self, base_url: str, driver_url: str, out_dir: pathlib.Path, *,
                 datasets_root: pathlib.Path | None = None,
                 agent_url: str = "", car_doc: str = "") -> None:
        self.base = base_url.rstrip("/")
        self.out = out_dir
        self.datasets_root = datasets_root
        self.agent_url = agent_url.rstrip("/")
        self.car_doc = car_doc
        self.report: dict = {"base_url": self.base, "steps": [], "checks": [],
                            "screenshots": [], "console_errors": [], "data_identity_expected":
                            "simulated_offline"}
        options = Options()
        options.add_argument("-headless")
        # geckodriver only keeps browser console records when the driver asks for them.
        options.set_preference("driver.logging", "error")
        self.driver = webdriver.Remote(command_executor=driver_url, options=options)
        self.driver.set_window_size(1600, 1400)
        self.driver.timeouts.implicit_wait = 0
        self.out.mkdir(parents=True, exist_ok=True)

    # -- primitives --------------------------------------------------------
    def button(self, text: str):
        return self.driver.find_element(
            By.XPATH, f"//button[normalize-space()='{text}']")

    def enabled(self, text: str) -> bool:
        try:
            return self.button(text).is_enabled()
        except WebDriverException:
            return False

    def click(self, text: str, timeout: float = 30.0) -> None:
        self.wait_until(lambda: self.enabled(text), f"button enabled: {text}", timeout)
        self.button(text).click()

    def wait_until(self, probe, label: str, timeout: float = 40.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = probe()
            except WebDriverException as exc:
                last = f"driver:{exc.__class__.__name__}"
            else:
                if last:
                    return last
            time.sleep(0.2)
        raise TimeoutException(f"timed out waiting for {label} (last={last!r})")

    def read(self, path: str) -> dict:
        """Same-origin read through the page itself, so the assertion is the server's."""
        script = """
        const done = arguments[arguments.length - 1];
        fetch(arguments[0], {headers: {'Content-Type': 'application/json'}})
          .then(r => r.json().then(v => done({status: r.status, body: v})))
          .catch(e => done({error: String(e)}));
        """
        return self.driver.execute_async_script(script, f"{self.base}{path}")

    def post(self, path: str, body: dict) -> dict:
        script = """
        const done = arguments[arguments.length - 1];
        fetch(arguments[0], {method: 'POST', headers: {'Content-Type': 'application/json'},
                             body: JSON.stringify(arguments[1])})
          .then(r => r.json().then(v => done({status: r.status, body: v})))
          .catch(e => done({error: String(e)}));
        """
        return self.driver.execute_async_script(script, f"{self.base}{path}", body)

    def shot(self, name: str, full: bool = True) -> str:
        path = self.out / f"{name}.png"
        self.driver.save_screenshot(str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.report["screenshots"].append({"name": name, "file": path.name,
                                           "sha256": digest, "bytes": path.stat().st_size})
        return digest

    def step(self, name: str, **detail) -> None:
        self.report["steps"].append({"name": name, "at": time.time(), **detail})
        print(f"STEP {name}: {json.dumps(detail, ensure_ascii=False)[:220]}", flush=True)

    def check(self, name: str, ok, detail=None) -> None:
        self.report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}: {json.dumps(detail, ensure_ascii=False, default=str)[:240]}",
              flush=True)

    # -- the flow ----------------------------------------------------------
    def open(self) -> dict:
        self.driver.get(f"{self.base}/ros-mobile")
        self.wait_until(lambda: self.driver.find_elements(By.CSS_SELECTOR,
                          '[data-testid="hybrid-panel"]'), "hybrid panel")
        self.shot("00-open")
        return self.read("/ros-mobile/hybrid/status")["body"]

    def select_profile(self, profile: str) -> None:
        select = self.driver.find_element(By.CSS_SELECTOR, "select[aria-label='联合 profile']")
        from selenium.webdriver.support.ui import Select
        Select(select).select_by_visible_text(profile)
        self.step("profile_selected", profile=profile)
        self.shot("01-profile")

    def status(self) -> dict:
        return self.read("/ros-mobile/hybrid/status")["body"]

    def arm_state(self, status: dict) -> str | None:
        """The facade reports the manipulator state under channels.arm.state."""
        return (((status.get("channels") or {}).get("arm") or {}).get("state"))

    def wait_status(self, predicate, label: str, timeout: float = 40.0) -> dict:
        """Wait for the server's own facade status, and keep the whole snapshot."""
        return self.wait_until(
            lambda: (lambda current: current if predicate(current) else None)(self.status()),
            label, timeout)

    def recording(self) -> dict:
        return self.read("/ros-mobile/hybrid/recording/status")["body"]

    def identity(self) -> dict:
        status = self.status()
        return {"session_id": status["session_id"], "session_epoch": status["session_epoch"]}

    def frame_counts(self) -> dict:
        return ((self.status().get("frame_link") or {}).get("counts") or {})

    def car_emitted(self) -> int:
        """Frames the car's own control process published, read from its status file.

        The facade's link counters disappear when a fault drops the socket, which is
        exactly the moment an emergency-stop claim has to be checked, so the car's
        counter is the witness here and the page's counter is only a bonus.
        """
        if not self.car_doc:
            return 0
        try:
            document = json.loads(pathlib.Path(self.car_doc).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AssertionError(f"car status document unreadable: {exc}") from exc
        return int((document.get("counters") or {}).get("frames_emitted") or 0)

    def follow_from(self, baseline: dict, label: str) -> dict:
        """Preview, align, arm, engage, then wait for the car to admit new frames."""
        self.click("无输出预览")
        self.wait_status(lambda s: self.arm_state(s) == "preview", "preview state")
        self.click("对齐")
        self.wait_status(lambda s: self.arm_state(s) == "aligned", "aligned state")
        self.click("机械臂待命")
        self.wait_status(lambda s: self.arm_state(s) == "armed", "armed state")
        self.click("接合离合")
        self.wait_status(lambda s: self.arm_state(s) == "following", "following state")

        def grown() -> dict | None:
            counts = self.frame_counts()
            if counts.get("admitted", 0) > baseline.get("admitted", 0):
                return counts
            return None

        counts = self.wait_until(grown, f"frames admitted after {label}", 30.0)
        self.shot(f"02-following-{label}")
        return {"baseline": baseline, "counts": counts,
                "refusals": (self.status().get("frame_link") or {}).get("refusals")}

    def run(self, episodes: int, negative: bool = False) -> dict:
        initial = self.open()
        self.step("opened", state=initial.get("state"), transport=initial.get("transport"))
        self.check("page_uses_frame_transport", initial.get("transport") == "frame_stream_v1",
                   {"transport": initial.get("transport")})
        self.select_profile("simulated_hybrid_manual_v1")

        self.click("获取联合会话")
        status = self.wait_status(lambda s: s.get("state") == "ready", "ready session")
        identity = self.identity()
        self.step("session_acquired", **identity)
        self.shot("03-session-ready")
        self.check("session_marked_simulated", status["simulated"] is True,
                   {"simulated": status["simulated"], "profile": status["profile"]})

        self.follow_from({}, "initial")
        self.click("开始联合录制")
        started = self.wait_until(lambda: self.recording()["status"] == "active",
                                  "recording session active")
        recording = self.recording()
        car_id = recording.get("car_recording_id")
        self.step("recording_started", recording_status=started, car_recording_id=car_id)
        self.shot("04-recording-started")
        self.check("car_recording_id_from_agent", isinstance(car_id, str) and bool(car_id), car_id)

        for index in range(1, episodes + 1):
            tag = f"ep{index}"
            self.click("开始联合回合")
            self.wait_until(lambda: self.recording()["episode_id"], f"{tag} episode open")
            episode_id = self.recording()["episode_id"]
            self.step("episode_started", episode_id=episode_id)
            for phase in ("observe", "grasp"):
                self.post_phase(phase)
                self.follow_from({}, f"{tag}-{phase}")
                time.sleep(PHASE_SECONDS)
                counts = self.status()["frame_link"]["counts"]
                self.step("phase_followed", episode_id=episode_id, phase=phase,
                          counts=counts)
                self.shot(f"05-{tag}-{phase}")
            self.click("暂停并结束联合回合")
            self.wait_until(lambda: self.recording()["episode_id"] in (None, ""),
                            f"{tag} episode closed")
            closed = self.recording()["episodes"][-1]
            self.check(f"{tag}_paused_before_wrapup", closed.get("paused_before_wrapup") is True,
                       {"episode_id": closed["episode_id"],
                        "phases": [p["phase"] for p in closed.get("phases") or []]})
            self.shot(f"06-{tag}-ended")

        self.click("暂停并停止联合录制")
        self.wait_until(lambda: self.recording()["status"] == "stopped", "recording stopped")
        stopped = self.recording()
        self.step("recording_stopped", car_recording_id=stopped.get("car_recording_id"),
                  episodes=len(stopped.get("episodes") or []))
        self.shot("07-recording-stopped")
        self.check("stopped_last_error_clean", stopped.get("last_error") is None,
                   {"last_error": stopped.get("last_error"),
                    "phase_timeline_complete": stopped.get("phase_timeline_complete")})

        # Sync and export are sub-tasks of the same session: the production page only
        # enables them while the hybrid session is held, so the review runs here.
        review = self.review(car_id)
        flows = (self.negatives(car_id, str(review.get("repo_id") or ""))
                 if negative else {})
        self.click("释放联合会话")
        released = self.wait_status(
            lambda s: s.get("state") in ("disconnected", "estop"), "session released")
        self.step("released", state=released["state"],
                  leases=released["leases"], diagnostics=released["diagnostics"])
        self.shot("08-released")
        return {"car_recording_id": car_id, "episodes": episodes, "review": review,
                "flows": flows, "final": self.status(), "recording": stopped}

    def post_phase(self, phase: str) -> None:
        label = f"阶段 {phase}"
        self.click(label)
        self.wait_until(lambda: (self.recording()["phases"] or [{}])[-1].get("phase") == phase,
                        f"phase {phase} recorded")
        self.step("phase_set", phase=phase)

    # -- data review section ----------------------------------------------
    def testid(self, name: str):
        found = self.driver.find_elements(By.CSS_SELECTOR, f'[data-testid="{name}"]')
        return found[0] if found else None

    def xpath_text(self, xpath: str):
        found = self.driver.find_elements(By.XPATH, xpath)
        return found[0] if found else None

    def select_session(self, car_recording_id: str) -> None:
        from selenium.webdriver.support.ui import Select
        self.click("刷新 session")
        select = self.driver.find_element(By.CSS_SELECTOR, "select[aria-label='选择 session']")
        self.wait_until(
            lambda: any(car_recording_id in (option.text or "")
                        for option in select.find_elements(By.TAG_NAME, "option")),
            "recorded session in the list", 60.0)
        option = next(o for o in select.find_elements(By.TAG_NAME, "option")
                      if car_recording_id in (o.text or ""))
        Select(select).select_by_visible_text(option.text)
        option.click()
        self.step("session_selected", label=option.text)

    def review(self, car_recording_id: str) -> dict:
        out: dict = {"car_recording_id": car_recording_id}
        self.select_session(car_recording_id)
        self.shot("10-session-selected")

        self.click("同步原始数据", timeout=90.0)
        synced = self.wait_until(lambda: self.testid("sync-result"), "sync result", 120.0)
        out["sync"] = synced.text[:300]
        self.step("synced", text=out["sync"])
        self.shot("11-synced")
        self.check("sync_reports_a_manifest_digest", "sha256" in out["sync"]
                   and "未提供" not in out["sync"], out["sync"])

        # The ordinary training-oriented entry must still refuse this session, now that
        # it is synced and verified: what it needs is the hardware claim, not the digest.
        refused = self.post("/ros-mobile/export", {"session_id": car_recording_id})
        out["normal_export_api"] = {"status": refused.get("status"),
                                    "body": str(refused.get("body"))[:300]}
        self.check("normal_export_still_refuses_simulated_after_sync",
                   refused.get("status", 200) >= 400, out["normal_export_api"])

        self.click("受限模拟 6D 导出", timeout=90.0)
        reported = self.wait_until(lambda: self.testid("export-report"),
                                   "restricted export report", 240.0)
        out["export_report_text"] = reported.text[:500]
        self.step("restricted_exported", text=out["export_report_text"])
        self.shot("12-restricted-export")
        self.check("restricted_export_produced_six_dimension_schema",
                   "(6 维)" in reported.text, {"text": reported.text[:200]})

        repo_match = re.search(r"repo_id\s+(\S+)\s*·", out["export_report_text"])
        repo_id = repo_match.group(1) if repo_match else ""
        out["repo_id"] = repo_id
        self.check("export_report_names_repo_id", bool(repo_id), repo_id)
        if not repo_id:
            return out

        listing = self.read(f"/datasets/local/{quote_repo(repo_id)}/episodes")
        out["episodes_api"] = str(listing.get("body"))[:400]
        episodes = (listing.get("body") or {}).get("episodes") or []
        self.check("dataset_lists_two_episodes_with_phases",
                   len(episodes) == 2 and all(e.get("phases") for e in episodes),
                   [{"length": e.get("length"), "phases": len(e.get("phases") or [])}
                    for e in episodes])

        from selenium.webdriver.support.ui import Select
        field = self.driver.find_element(By.CSS_SELECTOR, "input[aria-label='数据集 repo_id']")
        field.clear()
        field.send_keys(repo_id)
        self.click("读取分集")
        episode_select = self.driver.find_element(By.CSS_SELECTOR,
                                                  "select[aria-label='选择 episode']")
        self.wait_until(lambda: len(episode_select.find_elements(By.TAG_NAME, "option")) > 1,
                        "episodes in the page selector", 60.0)
        Select(episode_select).select_by_index(1)
        self.step("episode_selected", label=Select(episode_select).first_selected_option.text)
        phase_select = self.driver.find_element(By.CSS_SELECTOR, "select[aria-label='选择阶段']")
        options = phase_select.find_elements(By.TAG_NAME, "option")
        if len(options) > 1:
            Select(phase_select).select_by_index(1)
            self.step("phase_selected", label=options[1].text)
        self.click("读取曲线")
        self.wait_until(lambda: self.driver.find_elements(
            By.XPATH, "//*[contains(text(),'rad')]"), "curve axis labels", 60.0)
        self.shot("13-curves")

        series = self.read(f"/datasets/local/{quote_repo(repo_id)}/episodes/0/timeseries")
        body = series.get("body") or {}
        out["timeseries_keys"] = sorted(body)[:12]
        out["series_summary"] = {
            name: {"points": len(rows),
                   "first": rows[0] if rows else None,
                   "last": rows[-1] if rows else None}
            for name, rows in (body.get("series") or {}).items() if isinstance(rows, list)}
        self.check("curves_carry_real_joint_names",
                   bool((body.get("features") or body.get("names") or body.get("series"))),
                   {"keys": out["timeseries_keys"]})
        return out

    # -- negative flows: visible error, cancel, estop recovery ----------------
    def agent_read(self, path: str) -> dict:
        """Ask the car directly, so a page claim has an independent second witness."""
        with urllib.request.urlopen(f"{self.agent_url}{path}", timeout=8) as response:
            return json.loads(response.read())

    def dataset_hashes(self, repo_id: str) -> dict[str, str]:
        root = self.datasets_root / repo_id
        if not root.is_dir():
            return {}
        return {str(item.relative_to(root)): hashlib.sha256(item.read_bytes()).hexdigest()
                for item in sorted(root.rglob("*")) if item.is_file()}

    def _no_episodes_listed(self) -> bool:
        from selenium.webdriver.support.ui import Select
        found = self.driver.find_elements(By.CSS_SELECTOR, "select[aria-label='选择 episode']")
        return bool(found) and len(Select(found[0]).all_options) <= 1

    def page_error_and_dismiss(self, repo_id: str, car_recording_id: str) -> dict:
        """A refusal from the back end has to reach the operator as one readable sentence.

        The export entry refuses a session whose own recording ended in error, and the page
        renders that refusal; an unknown dataset repo simply cannot be read at all, because
        the button stays disabled rather than plotting invented numbers.
        """
        from selenium.webdriver.support.ui import Select
        out: dict = {}
        out["normal_export_button_enabled"] = self.enabled("导出 LeRobot")
        blocked = self.testid("export-blocked")
        out["normal_export_button_note"] = blocked.text[:200] if blocked else None

        listing = self.read("/ros-mobile/sessions")["body"]
        refused = next((item.get("session_id") for item in (listing or {}).get("sessions", [])
                        if item.get("status") == "error"), "")
        out["refused_session_chosen"] = refused
        if refused:
            select = self.driver.find_element(By.CSS_SELECTOR,
                                              "select[aria-label='选择 session']")
            option = next((o for o in select.find_elements(By.TAG_NAME, "option")
                           if refused in (o.text or "")), None)
            if option is not None:
                Select(select).select_by_visible_text(option.text)
                option.click()
                self.click("受限模拟 6D 导出", timeout=60.0)
                alert = self.wait_until(lambda: self.testid("data-review-error"),
                                        "the refusal rendered in the page", 60.0)
                out["alert"] = alert.text[:300]
                self.shot("14-error")
                self.check("error_rendered_as_a_readable_sentence",
                           bool(alert.text.strip()) and not re.search(
                               r"Traceback|Error:|HTTP Error|0x[0-9a-f]+", alert.text),
                           out["alert"])
                self.click("关闭错误")
                self.wait_until(lambda: self.testid("data-review-error") is None,
                                "the alert dismissed", 30.0)
                self.shot("15-error-dismissed")
                self.check("error_alert_can_be_dismissed", True, out["alert"])
            self.select_session(car_recording_id)

        field = self.driver.find_element(By.CSS_SELECTOR, "input[aria-label='数据集 repo_id']")
        field.clear()
        field.send_keys("lelab/this-dataset-does-not-exist")
        if self.enabled("读取分集"):
            self.click("读取分集")
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and self.testid("phase-curves"):
            time.sleep(0.5)
        out["read_button_enabled_for_unknown_dataset"] = self.enabled("读取分集")
        self.check("unknown_dataset_is_never_plotted", self.testid("phase-curves") is None,
                   {"curves_rendered": self.testid("phase-curves") is not None,
                    "episode_select_enabled": self.enabled("读取曲线"),
                    "note": "an unknown repo must not leave a curve on the page"})
        self.shot("15b-unknown-dataset")
        field.clear()
        field.send_keys(repo_id)
        return out
        return out

    def cancel_export(self, car_recording_id: str, repo_id: str) -> dict:
        """Cancel a running export worker and prove the published dataset survives it."""
        out: dict = {"repo_id": repo_id}
        before = self.dataset_hashes(repo_id)
        out["files_before_cancel"] = len(before)
        backup = self.out / "dataset-before-cancel"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(self.datasets_root / repo_id, backup)

        self.click("受限模拟 6D 导出", timeout=60.0)
        self.wait_until(lambda: self.enabled("取消导出"), "the cancel button", 60.0)
        self.shot("16-export-running")
        self.click("取消导出")
        job_line = self.wait_until(
            lambda: self.xpath_text("//p[contains(text(),'导出任务')]"), "the job line", 30.0)
        match = re.search(r"导出任务\s+(\S+)\s+·", job_line.text)
        job_id = match.group(1) if match else ""
        out["job_id"] = job_id
        snapshot = self.wait_until(
            lambda: (lambda answer: answer if (answer.get("body") or {}).get("status")
                     != "running" else None)(
                self.read(f"/ros-mobile/export/status/{job_id}")),
            "the worker to exit", 90.0)["body"]
        out["job_status"] = snapshot.get("status")
        out["job_error"] = str(snapshot.get("error"))[:200]
        out["worker_pid"] = snapshot.get("pid")
        self.check("cancel_ends_the_export_job", snapshot.get("status") == "canceled",
                   {"status": snapshot.get("status"), "error": out["job_error"]})
        gone = not out["worker_pid"] or not pathlib.Path(
            f"/proc/{int(out['worker_pid'])}").exists()
        out["worker_process_gone"] = gone
        self.check("canceled_worker_is_not_left_running", gone, {"pid": out["worker_pid"]})

        after = self.dataset_hashes(repo_id)
        out["dataset_touched_by_canceled_job"] = sorted(
            {key for key in set(after) | set(before) if after.get(key) != before.get(key)})[:12]
        if after != before:
            partial = self.datasets_root / f"{repo_id}-canceled-attempt"
            if partial.parent.is_dir() and partial.exists():
                shutil.rmtree(partial)
            shutil.move(str(self.datasets_root / repo_id), str(partial))
            shutil.copytree(backup, self.datasets_root / repo_id)
            out["restored_from_snapshot"] = str(partial.relative_to(self.datasets_root))
        restored = self.dataset_hashes(repo_id)
        self.check("published_dataset_still_the_exported_one", restored == before,
                   {"files": len(restored)})
        listing = self.read(f"/datasets/local/{quote_repo(repo_id)}/episodes")
        episodes = (listing.get("body") or {}).get("episodes") or []
        self.check("page_still_lists_two_episodes_after_cancel",
                   len(episodes) == 2 and all(e.get("phases") for e in episodes),
                   [{"length": e.get("length"), "phases": len(e.get("phases") or [])}
                    for e in episodes])
        self.shot("17-export-canceled")
        return out

    def resume_via_page(self, label: str) -> dict:
        """Recover the way the page actually allows, and record which path it needed."""
        try:
            self.follow_from({}, f"{label}-reengage")
            return {"path": "re-engage in place"}
        except (TimeoutException, WebDriverException) as exc:
            note = f"{type(exc).__name__}: {str(exc)[:180]}"
        self.click("释放联合会话")
        self.wait_status(lambda s: s.get("state") in ("disconnected", "fault"),
                         "the faulted session let go", 40.0)
        self.click("获取联合会话")
        self.wait_status(lambda s: s.get("state") == "ready", "a fresh ready session", 60.0)
        self.follow_from({}, f"{label}-after-reacquire")
        return {"path": "release, then acquire a new session",
                "why_re_engage_alone_failed": note}

    def estop_and_recover(self) -> dict:
        """Stop, clear, and re-engage by hand: clearing an estop must not resume motion."""
        if not self.car_doc:
            raise AssertionError("estop checks need --car-doc: without the car's own "
                                 "counter there is no witness that publication stopped")
        out: dict = {}
        self.follow_from({}, "pre-estop")
        running = self.car_emitted()
        time.sleep(1.0)
        advancing = self.car_emitted()
        self.check("car_publishes_before_the_estop", advancing > running,
                   {"before": running, "after": advancing})

        self.click("急停")
        stopped = self.wait_status(lambda s: s.get("state") == "estop", "estop state", 30.0)
        out["state_after_estop"] = stopped.get("state")
        out["arm_state_after_estop"] = self.arm_state(stopped)
        out["outputs_after_estop"] = stopped.get("outputs_enabled")
        out["last_error_after_estop"] = stopped.get("last_error")
        self.shot("18-estop")
        at_stop = self.car_emitted()
        time.sleep(2.0)
        frozen = self.car_emitted()
        out["emitted_at_estop"] = at_stop
        out["emitted_two_seconds_later"] = frozen
        self.check("estop_stops_car_publication", frozen == at_stop,
                   {"at_estop": at_stop, "two_seconds_later": frozen})
        if self.agent_url:
            car = self.agent_read("/status")
            licence = car.get("licence") or {}
            out["car_frame_stream_connected"] = licence.get("frame_stream_connected")
            out["car_frame_outputs_enabled"] = licence.get("frame_outputs_enabled")
            self.check("car_drops_the_frame_stream",
                       licence.get("frame_outputs_enabled") is not True,
                       {"frame_outputs_enabled": licence.get("frame_outputs_enabled"),
                        "frame_stream_connected": licence.get("frame_stream_connected")})

        self.click("清除急停")
        cleared = None
        try:
            cleared = self.wait_status(lambda s: s.get("state") != "estop",
                                       "estop cleared", 10.0)
        except TimeoutException:
            cleared = self.status()
            panel = self.testid("hybrid-error")
            out["clear_estop_refused_with"] = (panel.text[:220] if panel else None)
            out["clear_estop_http"] = str(cleared.get("last_error"))[:200]
        out["state_after_clear"] = cleared.get("state")
        out["cleared_reached_a_non_estop_state"] = cleared.get("state") != "estop"
        out["arm_state_after_clear"] = self.arm_state(cleared)
        at_clear = self.car_emitted()
        time.sleep(3.0)
        later = self.car_emitted()
        out["emitted_after_clear"] = later
        self.shot("19-estop-cleared-no-auto-engage")
        self.check("clearing_estop_does_not_resume_by_itself",
                   later == at_clear and self.arm_state(self.status()) != "following",
                   {"at_clear": at_clear, "three_seconds_later": later,
                    "arm": self.arm_state(self.status()), "state": self.status()["state"],
                    "last_error": self.status().get("last_error")})

        out["recovery"] = self.resume_via_page("post-estop")
        resumed = self.car_emitted()
        out["emitted_after_manual_reengage"] = resumed
        self.shot("20-reengaged")
        self.check("explicit_reengage_resumes_car_publication", resumed > later,
                   {"after_clear": later, "after_manual_reengage": resumed,
                    "recovery_path": out["recovery"]["path"]})
        return out

    def flows_only(self, car_recording_id: str, repo_id: str) -> dict:
        """Re-run the negative flows against data that is already recorded and synced."""
        self.open()
        self.select_profile("simulated_hybrid_manual_v1")
        self.click("获取联合会话")
        self.wait_status(lambda s: s.get("state") == "ready", "ready session", 60.0)
        self.select_session(car_recording_id)
        flows: dict = {"page_error": self.page_error_and_dismiss(repo_id,
                                                car_recording_id)}
        if repo_id and self.datasets_root:
            flows["cancel_export"] = self.cancel_export(car_recording_id, repo_id)
        flows["estop_recovery"] = self.estop_and_recover()
        self.click("释放联合会话")
        self.wait_status(lambda s: s.get("state") in ("disconnected", "fault", "estop"),
                         "session released", 60.0)
        self.report["final"] = self.status()
        return flows

    def negatives(self, car_recording_id: str, repo_id: str) -> dict:
        flows: dict = {"page_error": self.page_error_and_dismiss(repo_id,
                                                car_recording_id)}
        if repo_id and self.datasets_root:
            flows["cancel_export"] = self.cancel_export(car_recording_id, repo_id)
        flows["estop_recovery"] = self.estop_and_recover()
        return flows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18288")
    parser.add_argument("--driver-url", default="http://127.0.0.1:4444")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--negative", action="store_true",
                        help="also click the error, cancel and estop-recovery flows")
    parser.add_argument("--datasets-root", default="",
                        help="HF_LEROBOT_HOME of the server under test; needed to cancel safely")
    parser.add_argument("--agent-url", default="",
                        help="car agent URL, read directly as a second witness")
    parser.add_argument("--car-doc", default="",
                        help="path to car_control_status.json, the car's own counter file")
    parser.add_argument("--flows-only", action="store_true",
                        help="skip the recording and replay only the negative flows")
    parser.add_argument("--car-recording", default="")
    parser.add_argument("--repo", default="")
    args = parser.parse_args()
    session = Closure(args.base_url, args.driver_url, pathlib.Path(args.out_dir),
                      datasets_root=pathlib.Path(args.datasets_root) if args.datasets_root
                      else None,
                      agent_url=args.agent_url, car_doc=args.car_doc)
    failed = 0
    try:
        if args.flows_only:
            session.report["flows"] = session.flows_only(args.car_recording, args.repo)
        else:
            result = session.run(args.episodes, negative=args.negative)
            session.report["recording"] = result
        try:
            entries = session.driver.get_log("browser")
        except Exception as exc:  # noqa: BLE001 - console capture is auxiliary
            session.report["console_log_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            entries = []
        for entry in entries:
            if entry.get("level") in ("SEVERE", "ERROR"):
                session.report["console_errors"].append(entry["message"][:300])
    except Exception as exc:  # noqa: BLE001 - the report is the deliverable
        session.report["error"] = {"type": type(exc).__name__, "detail": str(exc)[:500]}
        failed = 1
        try:
            session.shot("99-error")
        except Exception:  # noqa: BLE001
            pass
    finally:
        session.report["failed"] = [c["name"] for c in session.report["checks"] if not c["ok"]]
        (session.out / "web-closure.json").write_text(
            json.dumps(session.report, indent=2, default=str) + "\n", encoding="utf-8")
        try:
            session.driver.quit()
        except Exception as exc:  # noqa: BLE001 - snap geckodriver owns the process
            print(f"driver teardown note: {type(exc).__name__}: {exc}", flush=True)
    return 1 if failed or session.report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
