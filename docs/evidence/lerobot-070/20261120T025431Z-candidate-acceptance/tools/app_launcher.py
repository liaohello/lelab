#!/usr/bin/env python3
"""Start the packaged LeLab CLI on a test port, without editing the package.

`lelab/scripts/lelab.py` hardcodes `BACKEND_PORT = 8000`, and this machine
already has an unrelated LeLab instance on 8000 (the operator's live app). The
acceptance therefore patches the module global in memory and calls the real
`main()`; nothing inside the unpacked tree changes, and the CLI still does its
own port check, frontend check and uvicorn configuration.

The package is imported from `PYTHONPATH`, which the harness sets to the unpacked
tree, so this file works the same whether the third-party dependencies come from
a purpose-built environment or from the shared one.
"""

from __future__ import annotations

import os

from lelab.scripts import lelab as cli

cli.BACKEND_PORT = int(os.environ.get("LELAB_ACCEPT_PORT", "8020"))
cli.HOST = "127.0.0.1"

if __name__ == "__main__":
    cli.main(["--no-open", "--host", "127.0.0.1"])
