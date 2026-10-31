"""Car-side raw session recording with explicit integrity gates.

A session is one rosbag2/MCAP file plus an append-only event log. Episodes are
logical boundaries; stopping an episode never deletes raw material.

READY is only written when the recorder stopped normally, the bag process
exited successfully, the bag is readable by the local rosbag CLI, required
streams were present, and every file was hashed. Any other outcome retains the
partial raw files and a manifest with status=error but never writes READY.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Episode:
    episode_id: str
    task: str
    source: str
    started_time_ns: int
    started_wall: float
    initial: dict[str, Any] = field(default_factory=dict)
    status: str = "recording"
    ended_time_ns: int | None = None
    end_wall: float | None = None
    evaluation: dict[str, Any] | None = None


class SessionRecorder:
    def __init__(self, car_root: str | Path, profile: dict[str, Any], logger):
        self.root = Path(car_root).expanduser() / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile = profile
        self.logger = logger
        self.lock = threading.RLock()
        recording = profile.get("recording", {})
        self.required_topics = list(recording.get("required_topics", []))
        self.min_free_bytes = int(recording.get("min_free_bytes", 512 * 1024 * 1024))
        self.stream_stale_s = float(recording.get("stream_stale_s", 1.5))
        self.clock_domain = (
            profile.get("runtime", {}).get("clock_domain")
            or recording.get("clock_domain")
            or "ros_time"
        )
        self.session_id: str | None = None
        self.last_session_id: str | None = None
        self.session_dir: Path | None = None
        self.bag_dir: Path | None = None
        self.bag_proc: subprocess.Popen | None = None
        self.bag_log = None
        self.episodes: dict[str, Episode] = {}
        self.current_episode: str | None = None
        self.task = ""
        self.started_wall: float | None = None
        self.started_time_ns: int | None = None
        self.error: str | None = None
        self.error_detail: str | None = None
        self.ready = False
        self.status_value = "idle"
        self.bag_exit_code: int | None = None
        self.bag_readable: bool | None = None
        self.stop_reason: str | None = None
        self.stream_missing_since: float | None = None
        self.stream_gap_count = 0
        self.stream_gap_topics: list[str] = []
        self._health_error_written = False
        self._stopping = False

    @property
    def active(self) -> bool:
        return self.session_id is not None and self.bag_proc is not None and self.bag_proc.poll() is None

    def _event(self, kind: str, **payload):
        if not self.session_dir:
            return
        event = {"kind": kind, "wall_time": time.time(), "clock_domain": self.clock_domain, **payload}
        with self.lock:
            with open(self.session_dir / "events.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def _set_error(self, reason: str, detail: str = "", *, stop_bag: bool = False):
        if self.error is None:
            self.error = reason
            self.error_detail = detail
            self.status_value = "error"
            self._event("recording_error", reason=reason, detail=detail)
        if stop_bag and self.bag_proc is not None and self.bag_proc.poll() is None:
            try:
                os.killpg(self.bag_proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def _disk_free(self) -> int:
        return shutil.disk_usage(self.root).free

    def _startup_check(self, timeout_s: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.bag_proc is None:
                return False
            if self.bag_proc.poll() is not None:
                self.bag_exit_code = self.bag_proc.returncode
                return False
            if self.bag_dir is not None and self.bag_dir.is_dir():
                if any(p.is_file() and p.stat().st_size > 0 for p in self.bag_dir.iterdir()):
                    return True
            time.sleep(0.2)
        return False

    def start_session(self, session_id: str, task: str, metadata: dict[str, Any], time_ns: int) -> dict:
        with self.lock:
            if self.active:
                raise RuntimeError("a recording session is already active")
            self.session_id = session_id
            self.last_session_id = session_id
            self.task = task
            self.started_wall = time.time()
            self.started_time_ns = time_ns
            self.error = None
            self.error_detail = None
            self.ready = False
            self.status_value = "recording"
            self.bag_exit_code = None
            self.bag_readable = None
            self.stop_reason = None
            self.stream_missing_since = None
            self.stream_gap_count = 0
            self.stream_gap_topics = []
            self._health_error_written = False
            self._stopping = False
            self.episodes = {}
            self.current_episode = None
            self.session_dir = self.root / session_id
            self.session_dir.mkdir(parents=True, exist_ok=False)
            self.bag_dir = self.session_dir / "raw" / "bag"
            self.bag_dir.parent.mkdir(parents=True, exist_ok=True)
            (self.session_dir / "episodes").mkdir()
            if self._disk_free() < self.min_free_bytes:
                self._set_error("disk_low", f"free={self._disk_free()} required={self.min_free_bytes}")
                raise RuntimeError(self.error)
            topics = list(dict.fromkeys(
                self.profile["recording"].get("topics", [])
                + self.profile["recording"].get("extra_topics", [])
            ))
            if not topics:
                self._set_error("no_topics", "profile recording.topics is empty")
                raise RuntimeError(self.error)
            cmd = [
                "ros2", "bag", "record",
                "--output", str(self.bag_dir),
                "--storage", self.profile["recording"].get("raw_storage", "mcap"),
                "--topics", *topics,
            ]
            self.bag_log = open(self.session_dir / "recording.log", "w", encoding="utf-8")
            self.bag_log.write("$ " + " ".join(cmd) + "\n")
            self.bag_log.write(f"clock_domain={self.clock_domain} required={self.required_topics}\n")
            self.bag_log.flush()
            self.bag_proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=self.bag_log,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            if not self._startup_check():
                if self.bag_proc.poll() is None:
                    self._set_error("recorder_startup_timeout", "bag directory or file not created", stop_bag=True)
                else:
                    self._set_error("recorder_startup_failed", f"exit={self.bag_proc.returncode}")
                raise RuntimeError(self.error)
            session = {
                "schema_version": 1,
                "session_id": session_id,
                "task": task,
                "status": "recording",
                "started_wall": self.started_wall,
                "started_time_ns": time_ns,
                "clock_domain": self.clock_domain,
                "topics": topics,
                "required_topics": self.required_topics,
                "metadata": metadata,
            }
            (self.session_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
            self._event("session_start", time_ns=time_ns, session_id=session_id, task=task,
                        topics=topics, required_topics=self.required_topics)
            return {"session_id": session_id, "path": str(self.session_dir), "topics": topics, "clock_domain": self.clock_domain}

    def start_episode(self, episode_id: str, task: str, source: str, time_ns: int, initial: dict) -> dict:
        with self.lock:
            if not self.active:
                raise RuntimeError("no active recording session")
            if self.current_episode is not None:
                raise RuntimeError("an episode is already open")
            ep = Episode(episode_id=episode_id, task=task, source=source,
                         started_time_ns=time_ns, started_wall=time.time(), initial=initial)
            self.episodes[episode_id] = ep
            self.current_episode = episode_id
            return self._event("episode_start", time_ns=time_ns, session_id=self.session_id,
                               episode_id=episode_id, task=task, source=source, initial=initial)

    def finish_episode(self, time_ns: int, evaluation: dict, discarded: bool = False, reason: str = "") -> dict:
        with self.lock:
            if self.current_episode is None:
                raise RuntimeError("no open episode")
            ep = self.episodes[self.current_episode]
            ep.ended_time_ns = time_ns
            ep.end_wall = time.time()
            ep.status = "discarded" if discarded else "saved"
            ep.evaluation = evaluation
            event = self._event("episode_end", time_ns=time_ns, session_id=self.session_id,
                                episode_id=ep.episode_id, status=ep.status, reason=reason, evaluation=evaluation)
            record = {
                "episode_id": ep.episode_id, "task": ep.task, "source": ep.source,
                "started_time_ns": ep.started_time_ns, "ended_time_ns": ep.ended_time_ns,
                "started_wall": ep.started_wall, "end_wall": ep.end_wall,
                "status": ep.status, "initial": ep.initial, "evaluation": evaluation,
            }
            (self.session_dir / "episodes" / f"{ep.episode_id}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            self.current_episode = None
            return event

    def poll_health(self, required_ages: dict[str, float | None] | None = None) -> dict:
        with self.lock:
            if self.session_id is None:
                return {"active": False}
            if self.bag_proc is not None and self.bag_proc.poll() is not None and not self._stopping:
                self.bag_exit_code = self.bag_proc.returncode
                self._set_error("bag_exited_early", f"exit={self.bag_exit_code}")
            free = self._disk_free()
            if free < self.min_free_bytes:
                self._set_error("disk_low", f"free={free} required={self.min_free_bytes}", stop_bag=True)
            if required_ages:
                missing = [
                    t for t in self.required_topics
                    if required_ages.get(t) is None or required_ages[t] > self.stream_stale_s
                ]
                now = time.monotonic()
                if missing:
                    if self.stream_missing_since is None:
                        self.stream_missing_since = now
                        self.stream_gap_topics = missing
                    if now - self.stream_missing_since > self.stream_stale_s and not self._health_error_written:
                        self.stream_gap_count += 1
                        self._health_error_written = True
                        self._set_error("required_stream_missing", ",".join(missing))
                        self._event("stream_gap", topics=missing, age_s=required_ages)
                else:
                    self.stream_missing_since = None
                    self.stream_gap_topics = []
            return {"active": self.active, "error": self.error, "disk_free": free}

    def _bag_info(self) -> tuple[bool, str]:
        if not self.bag_dir or not self.bag_dir.is_dir():
            return False, "bag directory missing"
        try:
            proc = subprocess.run(["ros2", "bag", "info", str(self.bag_dir)],
                                  capture_output=True, text=True, timeout=20)
        except Exception as exc:  # noqa: BLE001
            return False, f"ros2 bag info failed: {exc}"
        output = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0 and "Files:" in output and "Messages:" in output
        return ok, output[-2000:]

    def stop_session(self, reason: str = "normal", time_ns: int | None = None) -> dict:
        with self.lock:
            if not self.session_id or not self.session_dir:
                raise RuntimeError("no session to stop")
            session_dir = self.session_dir
            self._stopping = True
            self.stop_reason = reason
            if self.current_episode is not None:
                ep = self.episodes[self.current_episode]
                evaluation = {"status": "interrupted", "reason": "episode_open_at_stop", "success": "unknown"}
                self._event("episode_end", time_ns=None, session_id=self.session_id,
                            episode_id=ep.episode_id, status="interrupted",
                            reason="episode_open_at_stop", evaluation=evaluation)
                ep.status = "interrupted"
                ep.ended_time_ns = None
                ep.end_wall = time.time()
                ep.evaluation = evaluation
                (session_dir / "episodes" / f"{ep.episode_id}.json").write_text(json.dumps({
                    "episode_id": ep.episode_id, "task": ep.task, "source": ep.source,
                    "started_time_ns": ep.started_time_ns, "ended_time_ns": None,
                    "started_wall": ep.started_wall, "end_wall": ep.end_wall,
                    "status": "interrupted", "initial": ep.initial, "evaluation": evaluation,
                }, indent=2), encoding="utf-8")
                self.current_episode = None
                self._set_error("episode_open_at_stop", "session stopped with an open episode; raw retained")
            if self.bag_proc and self.bag_proc.poll() is None:
                try:
                    os.killpg(self.bag_proc.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    self.bag_proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.bag_proc.kill()
                    try:
                        self.bag_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._set_error("bag_stop_timeout", "recorder could not be stopped")
            self.bag_exit_code = self.bag_proc.returncode if self.bag_proc else None
            if self.bag_proc is not None and self.bag_exit_code not in (0, None):
                self._set_error("bag_exit_nonzero", f"exit={self.bag_exit_code}")
            if self.bag_log:
                self.bag_log.close()
                self.bag_log = None
            self.bag_readable, bag_info = self._bag_info()
            if not self.bag_readable:
                self._set_error("bag_not_readable", bag_info)
            if self.error is None and self.bag_exit_code != 0:
                self._set_error("bag_exit_nonzero", f"exit={self.bag_exit_code}")
            self._event("session_stop", time_ns=time_ns, session_id=self.session_id, reason=reason,
                        bag_exit_code=self.bag_exit_code, bag_readable=self.bag_readable,
                        error=self.error, error_detail=self.error_detail)
            session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
            normal = self.error is None and self.bag_exit_code == 0 and self.bag_readable
            session.update({
                "status": "raw_ready" if normal else "error",
                "stopped_wall": time.time(),
                "stop_reason": reason,
                "bag_exit_code": self.bag_exit_code,
                "bag_readable": self.bag_readable,
                "error": self.error,
                "error_detail": self.error_detail,
                "stream_gap_count": self.stream_gap_count,
                "stream_gap_topics": self.stream_gap_topics,
            })
            (session_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
            files = []
            for path in sorted(p for p in session_dir.rglob("*") if p.is_file()):
                if path.name in {"manifest.json", "READY"} or path.name.endswith(".tar.gz"):
                    continue
                files.append({"path": str(path.relative_to(session_dir)), "size": path.stat().st_size, "sha256": self._sha256(path)})
            manifest = {
                "schema_version": 2,
                "session_id": self.session_id,
                "task": self.task,
                "status": "raw_ready" if normal else "error",
                "profile": self.profile.get("profile"),
                "profile_sha256": session.get("metadata", {}).get("profile_sha256"),
                "metadata": session.get("metadata", {}),
                "clock_domain": self.clock_domain,
                "started_time_ns": self.started_time_ns,
                "started_wall": self.started_wall,
                "stopped_wall": session["stopped_wall"],
                "stop_reason": reason,
                "bag_exit_code": self.bag_exit_code,
                "bag_readable": self.bag_readable,
                "required_topics": self.required_topics,
                "stream_gap_count": self.stream_gap_count,
                "stream_gap_topics": self.stream_gap_topics,
                "episodes": [{
                    "episode_id": ep.episode_id, "source": ep.source, "status": ep.status,
                    "started_time_ns": ep.started_time_ns, "ended_time_ns": ep.ended_time_ns,
                    "started_wall": ep.started_wall, "end_wall": ep.end_wall,
                    "evaluation": ep.evaluation,
                } for ep in self.episodes.values()],
                "files": files,
                "error": self.error,
                "error_detail": self.error_detail,
                "ready_eligible": normal,
            }
            manifest_path = session_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            manifest_sha = self._sha256(manifest_path)
            if normal:
                (session_dir / "READY").write_text(json.dumps({
                    "session_id": self.session_id, "manifest_sha256": manifest_sha,
                    "bag_exit_code": self.bag_exit_code, "bag_readable": True,
                    "clock_domain": self.clock_domain, "verified_files": len(files),
                }), encoding="utf-8")
                self.ready = True
            else:
                self.ready = False
            result = {"session_id": self.session_id, "manifest_sha256": manifest_sha,
                      "files": len(files), "status": manifest["status"], "error": self.error}
            self.status_value = manifest["status"]
            self.bag_proc = None
            self.session_id = None
            self.session_dir = None
            self._stopping = False
            return result

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def status(self) -> dict:
        with self.lock:
            return {
                "active": self.active,
                "session_id": self.session_id,
                "last_session_id": self.last_session_id,
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "current_episode": self.current_episode,
                "episodes": [ep.__dict__ for ep in self.episodes.values()],
                "error": self.error, "error_detail": self.error_detail,
                "ready": self.ready, "status": self.status_value,
                "bag_exit_code": self.bag_exit_code, "bag_readable": self.bag_readable,
                "clock_domain": self.clock_domain, "disk_free_bytes": self._disk_free(),
                "stream_gap_count": self.stream_gap_count, "stream_gap_topics": self.stream_gap_topics,
                "required_topics": self.required_topics,
            }

    def list_sessions(self) -> list[dict]:
        out = []
        if not self.root.exists():
            return out
        for path in sorted(self.root.iterdir()):
            if not path.is_dir():
                continue
            info = {"session_id": path.name, "path": str(path)}
            try:
                info.update(json.loads((path / "session.json").read_text(encoding="utf-8")))
            except Exception:
                info["status"] = "invalid_session_json"
            info["manifest"] = (path / "manifest.json").exists()
            info["ready"] = (path / "READY").exists()
            out.append(info)
        return out

    def manifest(self, session_id: str) -> dict:
        path = self.root / session_id / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(session_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def archive(self, session_id: str, fileobj):
        session_dir = self.root / session_id
        if not session_dir.is_dir():
            raise FileNotFoundError(session_id)
        with tarfile.open(fileobj=fileobj, mode="w|gz") as tar:
            for path in sorted(session_dir.rglob("*")):
                if path.is_file() and not path.name.endswith(".tar.gz"):
                    tar.add(path, arcname=str(path.relative_to(session_dir)))
