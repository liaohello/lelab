"""Car-side raw session recording.

A session is one rosbag2/MCAP file plus an append-only event log.  Episodes
are logical boundaries inside the session; stopping an episode never deletes
raw material.  The source session is sealed by a manifest and READY marker,
then pulled by the upper host through the agent HTTP archive endpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXTRA_TOPICS = [
    "/lelab/contact/left_jaw_link",
    "/lelab/contact/right_jaw_link",
    "/lelab/contact/gripper_base_link",
]


@dataclass
class Episode:
    episode_id: str
    task: str
    source: str
    started_sim_ns: int
    started_wall: float
    initial_cube: list[float] | None = None
    initial_container: list[float] | None = None
    initial_joints: dict[str, float] = field(default_factory=dict)
    status: str = "recording"
    end_sim_ns: int | None = None
    end_wall: float | None = None
    evaluation: dict[str, Any] | None = None


class SessionRecorder:
    def __init__(self, car_root: str | Path, profile: dict[str, Any], logger):
        self.root = Path(car_root).expanduser() / "sessions"
        self.profile = profile
        self.logger = logger
        self.lock = threading.RLock()
        self.session_id: str | None = None
        self.session_dir: Path | None = None
        self.bag_dir: Path | None = None
        self.bag_proc: subprocess.Popen | None = None
        self.bag_log = None
        self.episodes: dict[str, Episode] = {}
        self.current_episode: str | None = None
        self.task = ""
        self.started_wall: float | None = None
        self.started_sim_ns: int | None = None
        self.error: str | None = None
        self.ready = False

    @property
    def active(self) -> bool:
        return self.bag_proc is not None and self.bag_proc.poll() is None

    def _event(self, kind: str, **payload):
        if not self.session_dir:
            return
        event = {"kind": kind, "wall_time": time.time(), **payload}
        with self.lock:
            with open(self.session_dir / "events.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def start_session(self, session_id: str, task: str, metadata: dict[str, Any], sim_ns: int) -> dict:
        with self.lock:
            if self.active:
                raise RuntimeError("a recording session is already active")
            self.session_id = session_id
            self.task = task
            self.started_wall = time.time()
            self.started_sim_ns = sim_ns
            self.session_dir = self.root / session_id
            self.session_dir.mkdir(parents=True, exist_ok=False)
            self.bag_dir = self.session_dir / "raw" / "bag"
            self.bag_dir.parent.mkdir(parents=True, exist_ok=True)
            (self.session_dir / "episodes").mkdir()
            topics = list(dict.fromkeys(self.profile["recording"]["topics"] + EXTRA_TOPICS))
            cmd = [
                "ros2", "bag", "record",
                "--output", str(self.bag_dir),
                "--storage", self.profile["recording"].get("raw_storage", "mcap"),
                "--topics", *topics,
            ]
            self.bag_log = open(self.session_dir / "recording.log", "w", encoding="utf-8")
            self.bag_log.write("$ " + " ".join(cmd) + "\n")
            self.bag_log.flush()
            self.bag_proc = subprocess.Popen(cmd, stdout=self.bag_log, stderr=subprocess.STDOUT, start_new_session=True)
            time.sleep(2.0)
            if self.bag_proc.poll() is not None:
                self.error = "rosbag recorder exited during startup"
                raise RuntimeError(self.error)
            session = {
                "schema_version": 1,
                "session_id": session_id,
                "task": task,
                "status": "recording",
                "started_wall": self.started_wall,
                "started_sim_ns": sim_ns,
                "topics": topics,
                "metadata": metadata,
            }
            (self.session_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
            self._event("session_start", sim_time_ns=sim_ns, session_id=session_id, task=task, topics=topics)
            return {"session_id": session_id, "path": str(self.session_dir), "topics": topics}

    def start_episode(self, episode_id: str, task: str, source: str, sim_ns: int, initial: dict) -> dict:
        with self.lock:
            if not self.active:
                raise RuntimeError("no active recording session")
            if self.current_episode is not None:
                raise RuntimeError("an episode is already open")
            ep = Episode(
                episode_id=episode_id,
                task=task,
                source=source,
                started_sim_ns=sim_ns,
                started_wall=time.time(),
                initial_cube=initial.get("cube"),
                initial_container=initial.get("container"),
                initial_joints=initial.get("joints", {}),
            )
            self.episodes[episode_id] = ep
            self.current_episode = episode_id
            event = self._event(
                "episode_start", sim_time_ns=sim_ns, session_id=self.session_id,
                episode_id=episode_id, task=task, source=source, initial=initial,
            )
            return event

    def finish_episode(self, sim_ns: int, evaluation: dict, discarded: bool = False, reason: str = "") -> dict:
        with self.lock:
            if self.current_episode is None:
                raise RuntimeError("no open episode")
            ep = self.episodes[self.current_episode]
            ep.end_sim_ns = sim_ns
            ep.end_wall = time.time()
            ep.status = "discarded" if discarded else "saved"
            ep.evaluation = evaluation
            event = self._event(
                "episode_end", sim_time_ns=sim_ns, session_id=self.session_id,
                episode_id=ep.episode_id, status=ep.status, reason=reason,
                evaluation=evaluation,
            )
            record = {
                "episode_id": ep.episode_id,
                "task": ep.task,
                "source": ep.source,
                "started_sim_ns": ep.started_sim_ns,
                "ended_sim_ns": ep.end_sim_ns,
                "status": ep.status,
                "initial": {
                    "cube": ep.initial_cube,
                    "container": ep.initial_container,
                    "joints": ep.initial_joints,
                },
                "evaluation": evaluation,
            }
            (self.session_dir / "episodes" / f"{ep.episode_id}.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
            self.current_episode = None
            return event

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def stop_session(self) -> dict:
        with self.lock:
            if not self.session_id or not self.session_dir:
                raise RuntimeError("no session to stop")
            session_dir = self.session_dir
            if self.bag_proc and self.bag_proc.poll() is None:
                try:
                    os.killpg(self.bag_proc.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    self.bag_proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.bag_proc.kill()
                    self.bag_proc.wait(timeout=5)
            if self.bag_log:
                self.bag_log.close()
            self._event("session_stop", session_id=self.session_id, sim_time_ns=None)
            session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
            session["status"] = "raw_ready" if self.error is None else "error"
            session["stopped_wall"] = time.time()
            (session_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
            files = []
            for path in sorted(p for p in session_dir.rglob("*") if p.is_file()):
                if path.name in {"manifest.json", "READY"} or path.name.endswith(".tar.gz"):
                    continue
                files.append({"path": str(path.relative_to(session_dir)), "size": path.stat().st_size, "sha256": self._sha256(path)})
            manifest = {
                "schema_version": 1,
                "session_id": self.session_id,
                "task": self.task,
                "status": "raw_ready" if self.error is None else "error",
                "profile": self.profile["profile"],
                "metadata": session.get("metadata", {}),
                "episodes": [
                    {
                        "episode_id": ep.episode_id,
                        "source": ep.source,
                        "status": ep.status,
                        "started_sim_ns": ep.started_sim_ns,
                        "ended_sim_ns": ep.end_sim_ns,
                        "evaluation": ep.evaluation,
                    }
                    for ep in self.episodes.values()
                ],
                "files": files,
                "error": self.error,
            }
            manifest_path = session_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            manifest_sha = self._sha256(manifest_path)
            if self.error is None:
                (session_dir / "READY").write_text(
                    json.dumps({"session_id": self.session_id, "manifest_sha256": manifest_sha}), encoding="utf-8"
                )
                self.ready = True
            result = {"session_id": self.session_id, "manifest_sha256": manifest_sha, "files": len(files), "status": manifest["status"]}
            self.bag_proc = None
            self.session_id = None
            self.session_dir = None
            return result

    def status(self) -> dict:
        with self.lock:
            return {
                "active": self.active,
                "session_id": self.session_id,
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "current_episode": self.current_episode,
                "episodes": [ep.__dict__ for ep in self.episodes.values()],
                "error": self.error,
                "ready": self.ready,
            }

    def list_sessions(self) -> list[dict]:
        out = []
        if not self.root.exists():
            return out
        for path in sorted(self.root.iterdir()):
            if not path.is_dir():
                continue
            session_file = path / "session.json"
            manifest_file = path / "manifest.json"
            info = {"session_id": path.name, "path": str(path)}
            if session_file.exists():
                try:
                    info.update(json.loads(session_file.read_text(encoding="utf-8")))
                except Exception:
                    info["status"] = "invalid_session_json"
            info["manifest"] = manifest_file.exists()
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
