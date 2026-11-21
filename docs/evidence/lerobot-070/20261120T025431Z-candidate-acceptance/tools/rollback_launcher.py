#!/usr/bin/env python3
"""Start whichever release `current` points at, and say which one that was.

The service is launched through the pointer, not through an absolute path: that
is the whole point of a `current`-style deploy. Before uvicorn starts, this
writes a boot report with the resolved release, the module files that were
actually imported and the frontend directory that will be served, so the drill
can compare "what the pointer says" with "what the process loaded".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    current = Path(os.environ["LELAB_ACCEPT_CURRENT"])
    subdir = os.environ.get("LELAB_ACCEPT_PROJECT", "app")
    release = current.resolve()
    project = release / "payload" / subdir
    lerobot_src = release / "payload" / "lerobot" / "src"

    # Put this release in front of the environment's editable installs, so the
    # pointer, not the environment, decides which code runs.
    sys.path.insert(0, str(lerobot_src))
    sys.path.insert(0, str(project))

    import lelab
    import lerobot
    from lelab.server import FRONTEND_DIST

    report = {
        "current": str(current),
        "current_resolved": str(release),
        "project": str(project),
        "lelab_file": str(Path(lelab.__file__).resolve()),
        "lerobot_file": str(Path(lerobot.__file__).resolve()),
        "frontend_dist": str(Path(FRONTEND_DIST).resolve()),
        "frontend_index_exists": (Path(FRONTEND_DIST) / "index.html").is_file(),
        "sys_path_head": sys.path[:4],
        "pid": os.getpid(),
    }
    target = os.environ.get("LELAB_ACCEPT_BOOT")
    if target:
        Path(target).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    from lelab.scripts import lelab as cli

    cli.BACKEND_PORT = int(os.environ.get("LELAB_ACCEPT_PORT", "8020"))
    cli.HOST = "127.0.0.1"
    cli.main(["--no-open", "--host", "127.0.0.1"])


if __name__ == "__main__":
    main()
