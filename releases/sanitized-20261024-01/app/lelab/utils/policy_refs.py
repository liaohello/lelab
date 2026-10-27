# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Single source of truth for the opaque policy refs the UI passes around.

Training-job checkpoints reach the inference handlers as strings:

* ``/abs/path/pretrained_model``            — a directory on the machine that
  runs the policy (Jetson for local inference, train server for cloud
  inference);
* ``user/repo@checkpoints/000050``          — one checkpoint inside a LeRobot
  model repo (the ``checkpoints/<step>/pretrained_model`` tree);
* ``user/repo@root``                        — the repo root *is* the
  ``pretrained_model`` directory.

Both Hub shapes are resolved by ``snapshot_download`` either locally
(``lelab.rollout``) or on the training server (``lelab.remote_inference``), so
the parsing lives here instead of in one of the call sites.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

HUB_TREE_RE = re.compile(r"^(?P<repo>[^@]+)@checkpoints/(?P<step_dir>\d+)$")
HUB_ROOT_RE = re.compile(r"^(?P<repo>[^@]+)@root$")

PolicyRefKind = Literal["local", "hub_tree", "hub_root"]


@dataclass(frozen=True, slots=True)
class PolicyRef:
    kind: PolicyRefKind
    #: Raw ref as received (echoed back in error messages).
    raw: str
    #: Local directory (``kind == "local"``).
    path: str | None = None
    #: Hub coordinates (``hub_tree`` / ``hub_root``).
    repo_id: str | None = None
    step_dir: str | None = None

    @property
    def allow_patterns(self) -> list[str] | None:
        """``snapshot_download`` filter that fetches only this checkpoint."""
        if self.kind != "hub_tree" or self.step_dir is None:
            return None
        return [f"checkpoints/{self.step_dir}/pretrained_model/*"]

    def local_subpath(self) -> str | None:
        """Path of the ``pretrained_model`` dir inside the downloaded repo."""
        if self.kind != "hub_tree" or self.step_dir is None:
            return None
        return f"checkpoints/{self.step_dir}/pretrained_model"


class PolicyRefError(ValueError):
    """Raised for a ref that is neither a path nor a supported Hub shape."""


def parse_policy_ref(ref: str) -> PolicyRef:
    """Classify a policy ref; raises :class:`PolicyRefError` when unparsable."""
    if not ref or not ref.strip():
        raise PolicyRefError("Policy ref must not be empty")
    ref = ref.strip()
    if ref.startswith("/") or ref.startswith("~") or ref.startswith("."):
        return PolicyRef(kind="local", raw=ref, path=ref)
    match = HUB_TREE_RE.match(ref)
    if match:
        return PolicyRef(
            kind="hub_tree",
            raw=ref,
            repo_id=match.group("repo"),
            step_dir=match.group("step_dir"),
        )
    match = HUB_ROOT_RE.match(ref)
    if match:
        return PolicyRef(kind="hub_root", raw=ref, repo_id=match.group("repo"))
    raise PolicyRefError(
        f"Unrecognised policy ref: {ref!r} (expected an absolute path, "
        "'user/repo@checkpoints/<step>' or 'user/repo@root')"
    )
