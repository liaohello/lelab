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
"""Deployment coordinates for the Jetson / Windows / train-server split.

Only non-secret values live in these profiles: hosts, ports, SSH users and
remote roots. Passwords and tokens never do - SSH auth is delegated to the
agent/key, and secrets stay in `.env` or `~/.ssh/`.

Precedence (highest first):

1. environment variables (LELAB_TRAIN_HOST ...), so a systemd unit or a
   one-off shell can always override the machine default;
2. the TOML file pointed to by LELAB_SERVERS_CONFIG;
3. `~/.config/lelab/servers.toml` if it exists;
4. `deploy/config/servers.toml` inside the checkout if it exists.

`deploy/config/servers.example.toml` is the credential-free template.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "LELAB_SERVERS_CONFIG"

# TOML section/key -> environment variable.
_SECTION_KEYS: dict[str, dict[str, str]] = {
    "train_server": {
        "host": "LELAB_TRAIN_HOST",
        "port": "LELAB_TRAIN_PORT",
        "user": "LELAB_TRAIN_USER",
        "root": "LELAB_TRAIN_ROOT",
        "ssh_key": "LELAB_TRAIN_SSH_KEY",
        "policy_host": "LELAB_TRAIN_POLICY_HOST",
        "policy_port": "LELAB_TRAIN_POLICY_PORT",
    },
    "jetson": {
        "host": "LELAB_JETSON_HOST",
        "port": "LELAB_JETSON_PORT",
        "user": "LELAB_JETSON_USER",
        "root": "LELAB_JETSON_ROOT",
    },
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def config_candidates(env: Mapping[str, str] | None = None) -> list[Path]:
    """Files that may provide deployment profiles, in precedence order."""
    values = os.environ if env is None else env
    candidates: list[Path] = []
    explicit = values.get(CONFIG_ENV_VAR, "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.home() / ".config" / "lelab" / "servers.toml")
    candidates.append(_project_root() / "deploy" / "config" / "servers.toml")
    return candidates


def config_path(env: Mapping[str, str] | None = None) -> Path | None:
    """First existing profile file, or None when only env vars are used."""
    for candidate in config_candidates(env):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def load_servers_toml(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Parse the active profile file; missing/invalid files yield an empty map."""
    path = config_path(env)
    if path is None:
        return {}
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Ignoring unreadable server profile %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def resolved_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment mapping with profile values as defaults (env still wins)."""
    values: dict[str, str] = dict(os.environ if env is None else env)
    profile = load_servers_toml(values)
    for section, keys in _SECTION_KEYS.items():
        section_values = profile.get(section)
        if not isinstance(section_values, dict):
            continue
        for key, env_name in keys.items():
            raw = section_values.get(key)
            if raw is None or env_name in values:
                continue
            values[env_name] = str(raw)
    return values


def train_policy_endpoint(env: Mapping[str, str] | None = None) -> tuple[str, int]:
    """Host/port of the train-side fallback PolicyServer.

    Always loopback: the gRPC PolicyServer is unencrypted and must only be
    reached through the SSH tunnel (Jetson local end: 127.0.0.1:28081).
    """
    values = resolved_env(env)
    host = values.get("LELAB_TRAIN_POLICY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_port = values.get("LELAB_TRAIN_POLICY_PORT", "28081").strip()
    try:
        port = int(raw_port)
    except ValueError:
        port = 28081
    return host, port
