#!/usr/bin/env python3
"""Small dependency-free Firefox WebDriver client for integration evidence.

The system Firefox and geckodriver are already present in this workspace.  This
module starts both on a private port and speaks the W3C WebDriver wire protocol
directly, so browser acceptance does not depend on jsdom or a downloaded copy of
Playwright.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class BrowserError(RuntimeError):
    pass


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_geckodriver() -> str:
    for candidate in (shutil.which("geckodriver"), "/snap/bin/geckodriver"):
        if candidate and Path(candidate).exists():
            return candidate
    raise BrowserError("geckodriver is not installed")


class FirefoxDriver:
    def __init__(
        self,
        *,
        evidence_dir: Path,
        headless: bool = True,
        page_load_timeout_s: float = 30.0,
    ) -> None:
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.profile_dir = Path(tempfile.mkdtemp(prefix="lelab-firefox-"))
        self.log_handle = (self.evidence_dir / "geckodriver.log").open("wb")
        self.process = subprocess.Popen(
            [
                _find_geckodriver(),
                "--port",
                str(self.port),
                "--log",
                "debug",
            ],
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        (self.evidence_dir / "geckodriver.pid").write_text(str(self.process.pid) + "\n")
        self.session_id: str | None = None
        self._wait_ready()
        args = ["-headless"] if headless else []
        self._create_session(args)
        self.command("POST", f"/session/{self.session_id}/timeouts", {
            "pageLoad": int(page_load_timeout_s * 1000),
            "script": int(page_load_timeout_s * 1000),
            "implicit": 0,
        })
        self.execute(
            """
            window.__lelabBrowserErrors = [];
            window.addEventListener('error', event => {
              window.__lelabBrowserErrors.push(String(event.message || event.error || 'error'));
            });
            window.addEventListener('unhandledrejection', event => {
              window.__lelabBrowserErrors.push(String(event.reason || 'unhandled rejection'));
            });
            """
        )

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 20.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise BrowserError(
                    f"geckodriver exited early with {self.process.returncode}; "
                    f"see {self.evidence_dir / 'geckodriver.log'}"
                )
            try:
                self.command("GET", "/status")
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        raise BrowserError(f"geckodriver did not become ready: {last_error}")

    def _create_session(self, browser_args: list[str]) -> None:
        response = self.command(
            "POST",
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "firefox",
                        "acceptInsecureCerts": True,
                        "pageLoadStrategy": "normal",
                        "moz:firefoxOptions": {
                            "args": browser_args,
                            "prefs": {
                                "browser.tabs.warnOnClose": False,
                                "dom.disable_open_during_load": False,
                            },
                        },
                    }
                }
            },
        )
        self.session_id = response.get("sessionId")
        if not self.session_id:
            raise BrowserError(f"WebDriver did not return a session id: {response}")

    def command(self, method: str, path: str, body: Any | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise BrowserError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
        value = payload.get("value", payload)
        if isinstance(value, dict) and value.get("error"):
            raise BrowserError(
                f"{method} {path}: {value.get('error')}: {value.get('message')}"
            )
        return value

    def navigate(self, url: str) -> None:
        self.command("POST", f"/session/{self.session_id}/url", {"url": url})

    def current_url(self) -> str:
        return str(self.command("GET", f"/session/{self.session_id}/url"))

    def execute(self, script: str, args: list[Any] | None = None) -> Any:
        return self.command(
            "POST",
            f"/session/{self.session_id}/execute/sync",
            {"script": script, "args": args or []},
        )

    def find(self, css: str, *, timeout_s: float = 10.0) -> str:
        return self._find("css selector", css, timeout_s=timeout_s)

    def find_xpath(self, xpath: str, *, timeout_s: float = 10.0) -> str:
        return self._find("xpath", xpath, timeout_s=timeout_s)

    def _find(self, using: str, value: str, *, timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                result = self.command(
                    "POST",
                    f"/session/{self.session_id}/element",
                    {"using": using, "value": value},
                )
                element_id = result.get("element-6066-11e4-a52e-4f735466cecf")
                if element_id:
                    return str(element_id)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            time.sleep(0.1)
        raise BrowserError(f"element not found ({using}={value!r}): {last_error}")

    def click(self, css: str, *, timeout_s: float = 10.0) -> None:
        element_id = self.find(css, timeout_s=timeout_s)
        self.command("POST", f"/session/{self.session_id}/element/{element_id}/click", {})

    def click_text(self, text: str, *, tag: str = "*", timeout_s: float = 10.0) -> None:
        literal = text.replace("'", "\\'")
        element_id = self.find_xpath(
            f"//{tag}[contains(normalize-space(.), '{literal}')]",
            timeout_s=timeout_s,
        )
        self.command("POST", f"/session/{self.session_id}/element/{element_id}/click", {})

    def text(self, css: str, *, timeout_s: float = 10.0) -> str:
        element_id = self.find(css, timeout_s=timeout_s)
        return str(
            self.command(
                "GET", f"/session/{self.session_id}/element/{element_id}/text"
            )
        )

    def wait_text(self, text: str, *, timeout_s: float = 20.0) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            body = self.execute("return document.body ? document.body.innerText : ''")
            if text in str(body):
                return str(body)
            time.sleep(0.2)
        raise BrowserError(f"page never displayed {text!r}")

    def screenshot(self, path: Path) -> Path:
        encoded = self.command("GET", f"/session/{self.session_id}/screenshot")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(encoded))
        return target

    def browser_errors(self) -> list[str]:
        errors = self.execute("return window.__lelabBrowserErrors || []")
        return [str(item) for item in errors] if isinstance(errors, list) else []

    def close(self) -> None:
        if self.session_id:
            try:
                self.command("DELETE", f"/session/{self.session_id}")
            except Exception:
                pass
            self.session_id = None
        if self.process.poll() is None:
            # A snap-packaged geckodriver runs in its own user namespace, so even
            # our own child cannot always be signalled from outside it.  Losing the
            # driver process must never invalidate a completed browser run.
            try:
                os.killpg(os.getpgid(self.process.pid), 15)
            except OSError:
                try:
                    self.process.terminate()
                except OSError:
                    self.teardown_error = (
                        f"geckodriver_pid_{self.process.pid}_could_not_be_signalled"
                    )
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                except OSError:
                    self.teardown_error = (
                        f"geckodriver_pid_{self.process.pid}_left_running"
                    )
        self.log_handle.close()
        shutil.rmtree(self.profile_dir, ignore_errors=True)

    def __enter__(self) -> "FirefoxDriver":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--evidence-dir", default="docs/evidence/integration-current/browser-smoke")
    parser.add_argument("--screenshot", default="")
    args = parser.parse_args()
    directory = Path(args.evidence_dir)
    with FirefoxDriver(evidence_dir=directory) as browser:
        browser.navigate(args.url)
        target = Path(args.screenshot) if args.screenshot else directory / "page.png"
        browser.screenshot(target)
        print(json.dumps({
            "url": browser.current_url(),
            "title": browser.execute("return document.title"),
            "screenshot": str(target),
            "errors": browser.browser_errors(),
        }, ensure_ascii=False))
