"""Tests for the ChatGPT redirect-URI regex validator."""
from __future__ import annotations

import pytest

from app.oauth.redirect_uri import is_valid_chatgpt_redirect


@pytest.mark.parametrize("uri,expected", [
    # Valid ChatGPT callback shapes.
    ("https://chat.openai.com/aip/g-abcd1234/oauth/callback", True),
    ("https://chatgpt.com/aip/g-abcd1234/oauth/callback", True),
    ("https://chatgpt.com/aip/g-abc_DEF-123/oauth/callback", True),
    ("https://chat.openai.com/aip/g-A1B2C3D4E5F6/oauth/callback", True),

    # Invalid: wrong scheme.
    ("http://chat.openai.com/aip/g-abc/oauth/callback", False),

    # Invalid: wrong host.
    ("https://evil.com/aip/g-abc/oauth/callback", False),
    ("https://chat.openai.com.evil.com/aip/g-abc/oauth/callback", False),

    # Invalid: extra path segments / wrong path.
    ("https://chat.openai.com/aip/g-abc/oauth/callback/extra", False),
    ("https://chat.openai.com/aip/g-abc/auth/callback", False),

    # Invalid: open-redirect attempts.
    ("https://chat.openai.com@evil.com/aip/g-abc/oauth/callback", False),
    ("https://chatgpt.com/../aip/g-abc/oauth/callback", False),
    ("", False),
])
def test_is_valid_chatgpt_redirect(uri: str, expected: bool) -> None:
    assert is_valid_chatgpt_redirect(uri) is expected
