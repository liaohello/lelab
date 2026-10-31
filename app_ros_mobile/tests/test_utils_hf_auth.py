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
"""Tests for lelab.utils.hf_auth — whoami caching."""

from __future__ import annotations

from unittest.mock import patch


def test_invalidate_whoami_cache_clears_cached_value() -> None:
    from lelab.utils import hf_auth

    # cached_whoami() delegates to _WHOAMI_API.whoami(cache=True), which stores
    # results in _WHOAMI_API._whoami_cache keyed by token. Patching the whole
    # whoami() method would bypass the cache logic, so we patch _inner_whoami
    # (the actual HTTP call) instead — the real caching code then runs around it.
    # get_token() must return a truthy value so cached_whoami() doesn't short-circuit.
    with (
        patch("lelab.utils.hf_auth.get_token", return_value="hf_fake_token"),
        patch.object(hf_auth._WHOAMI_API, "_inner_whoami", return_value={"name": "alice"}) as spy,
    ):
        # Clear any pre-existing cache entry.
        hf_auth.invalidate_whoami_cache()

        first = hf_auth.cached_whoami()
        second = hf_auth.cached_whoami()
        assert first == {"name": "alice"}
        assert second == {"name": "alice"}
        # Cached: only one upstream call.
        assert spy.call_count == 1

        hf_auth.invalidate_whoami_cache()

        third = hf_auth.cached_whoami()
        assert third == {"name": "alice"}
        # After invalidation, the next call hits whoami again.
        assert spy.call_count == 2


def test_handle_hf_auth_status_returns_dict() -> None:
    from lelab.utils import hf_auth

    # handle_hf_auth_status() reads the stored token first, then verifies it.
    with (
        patch("lelab.utils.hf_auth.get_token", return_value="hf_fake_token"),
        patch("lelab.utils.hf_auth.whoami", return_value={"name": "alice", "orgs": []}),
    ):
        hf_auth.invalidate_whoami_cache()
        result = hf_auth.handle_hf_auth_status()
        assert isinstance(result, dict)
        assert result["authenticated"] is True
        assert result["token_present"] is True
        assert result["verified"] is True


def test_status_without_token_reports_absent_token() -> None:
    from lelab.utils import hf_auth

    with patch("lelab.utils.hf_auth.get_token", return_value=None):
        result = hf_auth.handle_hf_auth_status()

    assert result["authenticated"] is False
    assert result["token_present"] is False
    assert result["error"] is None
    assert result["token_path"]


def test_status_with_network_failure_keeps_the_token() -> None:
    """A saved token must not look 'logged out' when the Hub is unreachable."""
    from lelab.utils import hf_auth

    class NetworkError(OSError):
        pass

    with (
        patch("lelab.utils.hf_auth.get_token", return_value="hf_fake_token"),
        patch("lelab.utils.hf_auth.whoami", side_effect=NetworkError("connection refused")),
    ):
        result = hf_auth.handle_hf_auth_status()

    assert result["authenticated"] is False
    assert result["token_present"] is True
    assert result["token_rejected"] is False
    assert "Could not reach huggingface.co" in result["error"]


def test_status_with_rejected_token_is_marked_rejected() -> None:
    from lelab.utils import hf_auth

    class _FakeResponse:
        status_code = 401
        headers: dict[str, str] = {}
        reason_phrase = "Unauthorized"

    class RejectedTokenError(OSError):
        """Stands in for HfHubHTTPError(401) without the httpx plumbing."""

        def __init__(self) -> None:
            super().__init__("401 Client Error: Invalid user token")
            self.response = _FakeResponse()

    with (
        patch("lelab.utils.hf_auth.get_token", return_value="hf_bad"),
        patch("lelab.utils.hf_auth.whoami", side_effect=RejectedTokenError()),
    ):
        result = hf_auth.handle_hf_auth_status()

    assert result["token_present"] is True
    assert result["token_rejected"] is True
    assert "rejected" in result["error"].lower()
