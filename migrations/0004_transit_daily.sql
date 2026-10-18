-- ====================================================================
-- AskAstroBot Gateway - migration 0004: shared transit (gochar) table.
-- One row per planet per day (00:00 UTC): sidereal Lahiri geocentric
-- longitude and retrograde flag. Same for every user; no personal data.
-- Filled by: python -m app.mcp.transit_job topup   (daily cron)
-- Idempotent.
-- ====================================================================
CREATE TABLE IF NOT EXISTS public.gw_transit_daily (
  day        DATE             NOT NULL,
  planet     TEXT             NOT NULL CHECK (planet IN ('Sun','Moon','Mars','Mercury','Jupiter','Venus','Saturn','Rahu','Ketu')),
  longitude  DOUBLE PRECISION NOT NULL CHECK (longitude >= 0 AND longitude < 360),
  retro      BOOLEAN          NOT NULL DEFAULT FALSE,
  fetched_at TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
  PRIMARY KEY (day, planet)
);
