"""Validation of ChatGPT-issued redirect URIs.

ChatGPT Custom GPT Actions issue redirect URIs of the form:
    https://chat.openai.com/aip/g-XXXXXXXX/oauth/callback
    https://chatgpt.com/aip/g-XXXXXXXX/oauth/callback

Both hosts are valid; the GPT id is assigned by OpenAI on first save.
We accept any URI matching the regex below — no manual per-bot allowlist.
"""
from __future__ import annotations

import re

_CHATGPT_REDIRECT_RE = re.compile(
    r"^https://(chat\.openai\.com|chatgpt\.com)/aip/g-[A-Za-z0-9_-]+/oauth/callback$"
)


def is_valid_chatgpt_redirect(uri: str) -> bool:
    """Return True iff `uri` is a permissible ChatGPT Custom GPT redirect callback."""
    if not uri:
        return False
    return bool(_CHATGPT_REDIRECT_RE.match(uri))
