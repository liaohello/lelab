#!/usr/bin/env python3
"""OFFLINE ONLY: image freeze, exact-frame binding and an upper-host disappearance.

Three things the panel claims about the picture it shows, checked against the car:

* the JPEG it renders carries the same frame sequence it says it does, and a request for
  a frame the car no longer has is refused rather than answered with a newer image;
* when the simulated camera is stopped, the page says the picture is stale and stops
  claiming a new frame; it keeps displaying the last one, which is why the claim matters;
* when the upper host itself stops answering, the car's window ages out and the lease
  expires on its own, and nothing re-engages when the host comes back.

The camera stop/start and the process freeze belong to this task's isolated instance.
None of this is physical stop evidence: the frame stream stopping is not the arm being
commanded to stop, and the hold policy of the real board is untested here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import time
import urllib.request

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By

PHASES = ("live", "frozen", "recovered", "dropped")


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip()


OPS = "/work/lelab/tools/ros2_collection/local_ros2/offline/container_ops.py"


def container_exec(container: str, inner: str) -> str:
    """Run one command inside this task's own isolated container, nothing else."""
    return run(["sg", "docker", "-c",
                f"docker exec {container} bash -lc "
                f'"source /opt/ros/humble/setup.bash; python3 {OPS} {inner}"'])


def find_upper_pid(port: int) -> int:
    """The process that owns the web port - not the ones that merely mention the script.

    Scanning command lines matched the shell that launched the server as well, so the
    owner is taken from the listening socket's own record instead.
    """
    listing = run(["ss", "-tlnp", f"sport = :{port}"])
    pids = sorted({int(match) for match in re.findall(r"pid=(\d+)", listing)})
    if len(pids) != 1:
        raise RuntimeError(f"expected exactly one owner of port {port}, found {pids}")
    pid = pids[0]
    line = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore") \
        .replace("\0", " ")
    if "python" not in line:
        raise RuntimeError(f"port {port} is owned by something that is not the harness: {line[:120]}")
    return pid


class Checks:
    def __init__(self, base_url: str, driver_url: str, out: pathlib.Path, *,
                 container: str = "", state_dir: str = "", agent_url: str = "",
                 profile: str = "simulated_hybrid_manual_v1") -> None:
        self.base = base_url.rstrip("/")
        self.out = out
        self.container = container
        self.state_dir = state_dir
        self.agent_url = agent_url.rstrip("/")
        self.profile = profile
        self.report = {"base_url": self.base, "container": container,
                       "agent_url": self.agent_url, "phases": [], "checks": [],
                       "screenshots": [], "simulated_identity": True}
        options = Options()
        options.add_argument("-headless")
        self.driver = webdriver.Remote(command_executor=driver_url, options=options)
        self.driver.set_window_size(1600, 1400)
        # A frozen upper host is one of the phases; the page must survive the wait.
        self.driver.timeouts.script = 90.0

    def car(self, path: str = "/status") -> dict:
        with urllib.request.urlopen(f"{self.agent_url}{path}", timeout=10) as answer:
            return json.loads(answer.read())

    def camera(self, action: str) -> str:
        """Stop or start this instance's own simulated camera, by recorded pid."""
        if not self.container:
            raise RuntimeError("camera phases need --container")
        inner = f"{action}-camera --state-dir {self.state_dir}"
        if action == "start":
            inner += (" --script /work/lelab/tools/ros2_collection/local_ros2/offline/"
                      "sim_camera.py --arg=--rate-hz --arg=15")
        return container_exec(self.container, inner)

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception as exc:  # noqa: BLE001 - geckodriver owns the process
            self.report.setdefault("teardown_note", f"{type(exc).__name__}")

    def get(self, path: str) -> dict:
        script = """
        const done = arguments[arguments.length - 1];
        fetch(arguments[0]).then(r => r.text().then(t => {
          const headers = {};
          for (const pair of r.headers.entries()) headers[pair[0]] = pair[1];
          done({status: r.status, headers, bytes: t.length, body: t.slice(0, 200)});
        })).catch(e => done({error: String(e)}));
        """
        return self.driver.execute_async_script(script, f"{self.base}{path}")

    def json_get(self, path: str) -> dict:
        script = """
        const done = arguments[arguments.length - 1];
        fetch(arguments[0]).then(r => r.json().then(v => done({status: r.status, body: v})))
          .catch(e => done({error: String(e)}));
        """
        return self.driver.execute_async_script(script, f"{self.base}{path}")["body"]

    def check(self, name: str, ok, detail=None) -> None:
        self.report["checks"].append({"name": name, "ok": bool(ok), "phase": self.phase,
                                      "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} [{self.phase}] {name}: "
              f"{json.dumps(detail, ensure_ascii=False, default=str)[:240]}", flush=True)

    def shot(self, name: str) -> None:
        path = self.out.parent / f"{name}.png"
        self.driver.save_screenshot(str(path))
        self.report["screenshots"].append(
            {"name": name, "file": path.name,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    def open_panel(self) -> dict:
        self.driver.get(f"{self.base}/ros-mobile")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.driver.find_elements(By.CSS_SELECTOR, '[data-testid="hybrid-video"]'):
                break
            time.sleep(0.3)
        return self.json_get("/ros-mobile/hybrid/status")

    def video_gate(self) -> str | None:
        found = self.driver.find_elements(By.CSS_SELECTOR, '[data-testid="video-gate"]')
        return found[0].text if found else None

    def click(self, text: str, timeout: float = 40.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.driver.find_elements(By.XPATH, f"//button[normalize-space()='{text}']")
            if found and found[0].is_enabled():
                found[0].click()
                return
            time.sleep(0.2)
        raise RuntimeError(f"button never became clickable: {text}")

    def wait_car(self, predicate, label: str, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            answer = self.car()
            if predicate(answer):
                return answer
            time.sleep(0.3)
        raise RuntimeError(f"timed out waiting for the car: {label}")

    def establish_following(self, profile: str) -> dict:
        """Acquire, preview, align, arm and engage through the page, as an operator would.

        Each step is skipped when the server already reports that state, so a later phase
        can call this without fighting the buttons it has disabled.
        """
        from selenium.webdriver.support.ui import Select
        self.open_panel()
        status = self.json_get("/ros-mobile/hybrid/status")
        if status.get("state") != "ready":
            select = self.driver.find_element(
                By.CSS_SELECTOR, "select[aria-label='联合 profile']")
            options = [option.text for option in select.find_elements(By.TAG_NAME, "option")]
            if profile not in options:
                raise RuntimeError(f"profile {profile!r} not offered: {options}")
            Select(select).select_by_visible_text(profile)
            self.click("获取联合会话")
            self.wait_until_status(lambda s: s.get("state") == "ready", "ready session")
        for button, expected in (("无输出预览", "preview"), ("对齐", "aligned"),
                                 ("机械臂待命", "armed"), ("接合离合", "following")):
            current = (((self.json_get("/ros-mobile/hybrid/status")
                         .get("channels") or {}).get("arm") or {}).get("state"))
            if current == expected:
                continue
            self.click(button)
            self.wait_until_status(
                lambda s, expected=expected: (
                    ((s.get("channels") or {}).get("arm") or {}).get("state") == expected),
                f"arm state {expected}")
        return self.json_get("/ros-mobile/hybrid/status")

    def wait_until_status(self, predicate, label: str, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.json_get("/ros-mobile/hybrid/status")
            if predicate(last):
                return last
            time.sleep(0.3)
        # A bare "timed out" cannot be reviewed later; keep the server's own explanation.
        raise RuntimeError(f"timed out waiting for {label}: state={last.get('state')} "
                           f"arm={(last.get('channels') or {}).get('arm')} "
                           f"missing={last.get('missing_for_following')} "
                           f"last_error={last.get('last_error')} "
                           f"feedback={(last.get('feedback') or {}).get('reason')}")

    # -- phases ------------------------------------------------------------
    def phase_live(self) -> dict:
        self.phase = "live"
        self.establish_following(self.profile)
        status = self.open_panel()
        video = status["video"]
        self.check("panel_reports_a_fresh_frame", video.get("available") is True,
                   {"frame_seq": video.get("frame_seq"), "source": video.get("source")})
        current = video.get("frame_seq")
        exact = self.get(f"/ros-mobile/preview.jpg?frame_seq={current}")
        self.check("exact_frame_request_answered", exact.get("status") == 200,
                   {"status": exact.get("status"),
                    "header": exact.get("headers", {}).get("x-lelab-frame-seq")})
        self.check("jpeg_carries_the_requested_frame_header",
                   str(exact.get("headers", {}).get("x-lelab-frame-seq")) == str(current),
                   {"header": exact.get("headers", {}).get("x-lelab-frame-seq"),
                    "requested": current})
        stamp = exact.get("headers", {}).get("x-lelab-source-stamp-ns")
        self.check("jpeg_carries_a_source_stamp_header", bool(stamp), stamp)

        time.sleep(1.0)
        later = self.json_get("/ros-mobile/hybrid/status")["video"]["frame_seq"]
        self.check("frames_advance_while_the_camera_runs",
                   isinstance(later, int) and isinstance(current, int) and later > current,
                   {"before": current, "after": later})
        stale = self.get(f"/ros-mobile/preview.jpg?frame_seq={max(0, (current or 1) - 50)}")
        self.check("a_frame_the_car_no_longer_has_is_refused",
                   stale.get("status", 200) >= 400,
                   {"status": stale.get("status"), "body": str(stale.get("body"))[:160]})
        self.shot("media-live")
        return {"frame_seq": current, "later": later}

    def phase_frozen(self, seconds: float = 6.0) -> dict:
        self.phase = "frozen"
        stopped = self.camera("stop")
        before = self.open_panel()["video"]
        deadline = time.monotonic() + seconds
        seen = []
        while time.monotonic() < deadline:
            status = self.json_get("/ros-mobile/hybrid/status")["video"]
            seen.append(status.get("frame_seq"))
            time.sleep(0.5)
        gate = self.video_gate()
        stale_says_so = bool(status.get("stale") or (status.get("reason") or "")
                             or not status.get("available"))
        self.check("frozen_camera_reports_no_new_frame",
                   status.get("available") is not True or status.get("stale") is True,
                   {"available": status.get("available"), "stale": status.get("stale"),
                    "reason": status.get("reason")})
        self.check("frozen_camera_does_not_claim_a_new_sequence",
                   len(set(filter(None, seen))) <= 1, {"observed": sorted(set(map(str, seen)))})
        self.check("panel_shows_the_stale_picture_warning",
                   gate is not None and ("过期" in gate or "无新" in gate or "不可用" in gate),
                   gate)
        self.shot("media-frozen")
        return {"camera_stop": stopped[:200], "before": before.get("frame_seq"),
                "observed": sorted(set(map(str, seen))), "gate": gate}

    def phase_recovered(self) -> dict:
        self.phase = "recovered"
        started = self.camera("start")
        deadline = time.monotonic() + 40
        first = None
        while time.monotonic() < deadline:
            video = self.json_get("/ros-mobile/hybrid/status")["video"]
            if video.get("available") is True and first is None:
                first = video.get("frame_seq")
            if first is not None and video.get("frame_seq") and video["frame_seq"] > first:
                self.check("camera_restart_resumes_the_sequence", True,
                           {"first": first, "now": video["frame_seq"]})
                self.shot("media-recovered")
                return {"camera_start": started[:200], "first": first,
                        "now": video["frame_seq"]}
            time.sleep(0.5)
        self.check("camera_restart_resumes_the_sequence", False,
                   {"first": first, "last": (video or {}).get("frame_seq")})
        self.shot("media-recovered-failed")
        return {"camera_start": started[:200], "first": first}

    def phase_dropped(self, freeze_seconds: float = 50.0) -> dict:
        """Freeze the upper host itself: the car must time out on its own, and must not
        resume when the host comes back."""
        self.phase = "dropped"
        pid = find_upper_pid(int(self.base.rsplit(":", 1)[1]))
        engaged = self.establish_following(self.profile)
        self.check("following_before_the_freeze",
                   (((engaged.get("channels") or {}).get("arm") or {}).get("state"))
                   == "following", engaged.get("state"))
        before = self.car()
        control_before = before.get("control") or {}
        self.check("car_holds_the_lease_before_the_freeze",
                   control_before.get("active") is True,
                   {"active": control_before.get("active"),
                    "expires_in_s": control_before.get("expires_in_s")})
        os.kill(pid, signal.SIGSTOP)
        events: list[dict] = []
        started_at = time.monotonic()
        deadline = started_at + freeze_seconds
        lost_at = None
        try:
            while time.monotonic() < deadline:
                answer = self.car()
                control = answer.get("control") or {}
                licence = answer.get("licence") or {}
                frame = ((licence.get("bridge") or {}).get("frame_process") or {})
                events.append({"age_s": round(time.monotonic() - started_at, 2),
                               "active": control.get("active"),
                               "expires_in_s": control.get("expires_in_s"),
                               "frame_outputs_enabled": licence.get("frame_outputs_enabled"),
                               "frame_process_outputs": frame.get("outputs_enabled"),
                               "queue_depth": frame.get("queue_depth"),
                               "ticks": frame.get("ticks")})
                if control.get("active") is not True and frame.get(
                        "outputs_enabled") is not True:
                    lost_at = time.monotonic()
                    break
                time.sleep(1.0)
        finally:
            os.kill(pid, signal.SIGCONT)
        out = {"upper_pid": pid, "lease_lost_after_s":
               (round(lost_at - started_at, 2) if lost_at else None),
               "car_events_tail": events[-4:]}
        self.check("car_lease_expires_while_the_upper_host_is_frozen", lost_at is not None,
                   {"events": len(events), "last": events[-1] if events else None})
        last = events[-1] if events else {}
        self.check("frozen_upper_host_stops_the_emitter",
                   last.get("frame_process_outputs") is not True
                   and (last.get("queue_depth") or 0) == 0, last)
        # The agent's own mirror of the frame-output permission is a separate field, and
        # it stayed true while the emitter said otherwise. Recorded, not asserted away:
        # whoever reads /status must know which of the two words the truth is.
        out["agent_frame_outputs_mirror_during_freeze"] = last.get("frame_outputs_enabled")
        self.report["findings"] = (self.report.get("findings") or []) + [{
            "name": "car_lease_expired_but_agent_frame_outputs_mirror_stayed_true",
            "detail": {"agent_frame_outputs_enabled": last.get("frame_outputs_enabled"),
                       "control_process_outputs_enabled": last.get("frame_process_outputs"),
                       "control_active": last.get("active")}}]

        time.sleep(1.0)
        status = self.json_get("/ros-mobile/hybrid/status")
        out["state_after_host_returns"] = status.get("state")
        out["last_error_after_host_returns"] = status.get("last_error")
        out["arm_after_host_returns"] = ((status.get("channels") or {}).get("arm") or {}).get("state")
        self.shot("media-host-returned")
        self.check("the_host_notices_the_lease_is_gone",
                   status.get("state") in ("fault", "estop", "disconnected"),
                   {"state": status.get("state"), "last_error": status.get("last_error")})
        self.check("the_page_does_not_resume_following_by_itself",
                   out["arm_after_host_returns"] != "following",
                   out["arm_after_host_returns"])
        return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18288")
    parser.add_argument("--driver-url", default="http://127.0.0.1:4444")
    parser.add_argument("--out", required=True)
    parser.add_argument("--container", default="lelab-humble-facade-integration")
    parser.add_argument("--state-dir", default="/work/out/local_humble/docker/state")
    parser.add_argument("--agent-url", default="http://127.0.0.1:18131")
    parser.add_argument("--profile", default="simulated_hybrid_manual_v1")
    parser.add_argument("--only", choices=PHASES, action="append", default=None)
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    checks = Checks(args.base_url, args.driver_url, out, container=args.container,
                    state_dir=args.state_dir, agent_url=args.agent_url,
                    profile=args.profile)
    checks.phase = "init"
    existing = {}
    if out.exists():
        existing = json.loads(out.read_text())
    try:
        for phase in (args.only or list(PHASES)):
            result = getattr(checks, f"phase_{phase}")()
            checks.report["phases"].append({"name": phase, "result": result})
    except Exception as exc:  # noqa: BLE001 - the report is the deliverable
        checks.report["error"] = {"type": type(exc).__name__, "detail": str(exc)[:400]}
    finally:
        try:
            checks.click("释放联合会话", timeout=10.0)
            checks.wait_until_status(lambda s: s.get("state") in ("disconnected", "fault",
                                                                 "estop"),
                                     "the session released", 20.0)
            checks.report["released_after_checks"] = True
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            checks.report["release_note"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        checks.close()
        merged = {**existing, **checks.report}
        if "error" not in checks.report:
            # A re-run of one phase must not keep reporting the crash it replaced.
            merged.pop("error", None)
        merged["checks"] = (existing.get("checks") or []) + checks.report["checks"]
        merged["phases"] = (existing.get("phases") or []) + checks.report["phases"]
        merged["screenshots"] = (existing.get("screenshots") or []) + checks.report["screenshots"]
        merged["failed"] = [c["name"] for c in merged["checks"] if not c["ok"]]
        out.write_text(json.dumps(merged, indent=2, default=str) + "\n", encoding="utf-8")
    return 1 if checks.report.get("error") or any(not c["ok"] for c in checks.report["checks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
