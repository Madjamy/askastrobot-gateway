"""Retry policy: time-outs / API error bodies / blank data are retried; 4xx and exhausted deadlines are not."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import app.mcp.astro_api as api
from app.settings import get_settings

_T = {"start_time": "2026-08-12 23:54:30", "end_time": "2027-03-14 01:29:51"}
GOOD = {"output": {"maha_dasa": {"Lord": "Mars", **_T}, "antar_dasa": {"Lord": "Moon", **_T},
                   "pratyantar_dasa": {"Lord": "Jupiter", **_T}}}


@pytest.fixture
def fake_http(monkeypatch):
    calls: list[int] = []
    queue: list[httpx.Response] = []
    real = httpx.AsyncClient

    def handler(request):
        calls.append(1)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(get_settings(), "freeastrology_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "astro_api_min_interval", 0.0)
    sleeps: list[float] = []

    async def fast_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(api.asyncio, "sleep", fast_sleep)
    return queue, calls, sleeps


def test_retries_error_body_then_blank_then_succeeds(fake_http) -> None:
    queue, calls, sleeps = fake_http
    queue += [httpx.Response(200, json={"errorType": "Sandbox.Timedout"}),
              httpx.Response(200, json={"output": {}}),
              httpx.Response(200, json=GOOD)]
    out = asyncio.run(api.dasha_now({}, api.datetime(2026, 10, 18, 12, 0), 0, 0, 0))
    assert out["maha_dasa"]["Lord"] == "Mars" and len(calls) == 3 and sleeps == [1.0, 3.0]


def test_gives_up_after_retries_on_timeouts(fake_http) -> None:
    queue, calls, sleeps = fake_http
    queue += [httpx.ReadTimeout("slow")] * 3
    with pytest.raises(api.AstroApiError, match="ReadTimeout"):
        asyncio.run(api.dasha_table({}))
    assert len(calls) == 3


def test_no_retry_on_client_error(fake_http) -> None:
    queue, calls, _ = fake_http
    queue += [httpx.Response(401, json={"message": "bad key"})]
    with pytest.raises(api.AstroApiError, match="HTTP 401"):
        asyncio.run(api.dasha_table({}))
    assert len(calls) == 1


def test_no_retry_when_deadline_is_too_close(fake_http) -> None:
    queue, calls, _ = fake_http
    queue += [httpx.Response(503, text="busy")]
    with pytest.raises(api.AstroApiError, match="HTTP 503"):
        asyncio.run(api.dasha_table({}, deadline=time.monotonic() + 3.0))
    assert len(calls) == 1                     # one try; no time for a retry


def test_no_call_started_without_time_left(fake_http) -> None:
    queue, calls, _ = fake_http
    with pytest.raises(api.AstroApiError, match="no time left"):
        asyncio.run(api.dasha_table({}, deadline=time.monotonic() + 0.5))
    assert calls == []


def test_dasha_without_dates_is_rejected(fake_http) -> None:
    queue, calls, _ = fake_http
    bad = {"output": {k: {"Lord": "Mars"} for k in ("maha_dasa", "antar_dasa", "pratyantar_dasa")}}
    queue += [httpx.Response(200, json=bad)] * 3
    with pytest.raises(api.AstroApiError, match="blank or incomplete"):
        asyncio.run(api.dasha_now({}, api.datetime(2026, 10, 18), 0, 0, 0))
    assert len(calls) == 3


def test_rate_limit_respects_retry_after(fake_http) -> None:
    queue, calls, sleeps = fake_http
    queue += [httpx.Response(429, headers={"retry-after": "5"}), httpx.Response(200, json=GOOD)]
    asyncio.run(api.dasha_now({}, api.datetime(2026, 10, 18), 0, 0, 0))
    assert sleeps == [5.0] and len(calls) == 2
