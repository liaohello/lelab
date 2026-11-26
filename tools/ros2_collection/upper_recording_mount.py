#!/usr/bin/env python3
"""Expose the upper recording directory to MentorPi over the existing SSH login."""
from __future__ import annotations

import argparse
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path


REMOTE = r'''
set -eu
mount_dir=/tmp/lelab-upper-storage
command -v sshfs >/dev/null || { echo 'MentorPi needs sshfs' >&2; exit 2; }
test -c /dev/fuse || { echo 'MentorPi needs /dev/fuse' >&2; exit 2; }
if mountpoint -q "$mount_dir"; then echo 'Upper recording storage is already mounted' >&2; exit 2; fi
mkdir -p -m 700 "$mount_dir"
test ! -L "$mount_dir" && test -z "$(find "$mount_dir" -mindepth 1 -maxdepth 1 -print -quit)" || {
    echo 'Upper recording mount point must be an empty directory' >&2; exit 2;
}
exec sshfs -f -o passive,sshfs_sync,dir_cache=no,fsname=lelab-upper-recordings :/capture "$mount_dir"
'''


def serve_recordings(directory: Path) -> list[str]:
    sftp = "/usr/lib/openssh/sftp-server"
    if shutil.which("bwrap") is None or not Path(sftp).is_file():
        raise SystemExit("bwrap and the OpenSSH sftp-server are required on the upper computer")
    return [
        "bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--ro-bind", "/etc/passwd", "/etc/passwd",
        "--dev", "/dev",
        "--dir", "/capture", "--bind", str(directory), "/capture/sessions",
        "--chdir", "/capture", "--", sftp, "-d", "/capture", "-u", "0077",
        "-P", "symlink,hardlink,readlink",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi", required=True, help="existing SSH destination, for example pi@raspberrypi")
    parser.add_argument("--container", default="MentorPi")
    parser.add_argument("--sessions", required=True, type=Path, help="local upper/sessions directory")
    args = parser.parse_args()
    directory = args.sessions.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    server = serve_recordings(directory)
    remote = shlex.join(["docker", "exec", "-i", "-u", "0", args.container, "sh", "-c", REMOTE])
    client = ["ssh", "-T", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=5",
              "-oServerAliveInterval=3", "-oServerAliveCountMax=2", "--", args.pi, remote]

    processes = []
    try:
        left, right = socket.socketpair()
        with left, right:
            processes.append(subprocess.Popen(server, stdin=left, stdout=left))
            processes.append(subprocess.Popen(client, stdin=right, stdout=right))
        while all(proc.poll() is None for proc in processes):
            time.sleep(0.2)
        print("Upper recording mount disconnected", flush=True)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
