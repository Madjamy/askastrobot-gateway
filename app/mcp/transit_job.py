"""Fill the shared transit table: `python -m app.mcp.transit_job topup [--days-back N] [--months-ahead N]`.

Fetches only missing days (one /planets call per day, 00:00 UTC, geocentric) for
`days_back` days ago .. `months_ahead` months ahead. First run ~760 calls
(about 20 minutes at the job's pace); afterwards ~1 call a day. Patient retries:
15 s, then 60 s, then the day is skipped and retried on the next run.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone

from app.db import close_pool, init_pool
from app.logging_setup import configure_logging, get_logger
from app.mcp import astro_api, transit_table
from app.settings import get_settings

log = get_logger(__name__)
_PATIENT = (15, 60)


async def fetch_day(day: date) -> dict[str, tuple[float, bool]] | None:
    body = astro_api.birth_body(datetime(day.year, day.month, day.day, 0, 0), 0.0, 0.0, 0.0,
                                observation_point="geocentric")
    for attempt in range(len(_PATIENT) + 1):
        try:
            return await astro_api.planet_longitudes(body)
        except astro_api.AstroApiError as exc:
            if attempt == len(_PATIENT):
                log.error("transit_job.day_failed", day=day.isoformat(), error=str(exc)[:120])
                return None
            await asyncio.sleep(_PATIENT[attempt])
    return None


async def topup(days_back: int, months_ahead: int) -> tuple[int, int]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=int(months_ahead * 30.5) + 2)
    todo = await transit_table.missing_days(start, end)
    log.info("transit_job.start", missing=len(todo), start=start.isoformat(), end=end.isoformat())
    done = failed = 0
    for day in todo:
        positions = await fetch_day(day)
        if positions is None:
            failed += 1
            continue
        await transit_table.store_day(day, positions)
        done += 1
        if done % 50 == 0:
            log.info("transit_job.progress", done=done, failed=failed, remaining=len(todo) - done - failed)
    log.info("transit_job.finished", done=done, failed=failed)
    return done, failed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["topup"])
    parser.add_argument("--days-back", type=int, default=35)
    parser.add_argument("--months-ahead", type=int, default=24)
    args = parser.parse_args()
    configure_logging(get_settings().log_level)
    # The job may run alongside live traffic: be gentle with the plan's rate limit.
    get_settings().astro_api_min_interval = max(get_settings().astro_api_min_interval, 1.2)

    async def run() -> None:
        await init_pool()
        try:
            await topup(args.days_back, args.months_ahead)
        finally:
            await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    main()
