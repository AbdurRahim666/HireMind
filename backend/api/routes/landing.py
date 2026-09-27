"""Public endpoints for the landing page."""

import time

from fastapi import APIRouter

from db import _get_conn, country_code_for_name

router = APIRouter(prefix="/api", tags=["landing"])

_countries_cache: dict = {}
_COUNTRIES_TTL = 600  # seconds


@router.get("/countries-used")
def countries_used():
    """Countries people have used the app from, most-visits first.
    Results are cached in-memory to keep landing loads cheap."""
    global _countries_cache
    now = time.time()
    hit = _countries_cache.get("data")
    at = _countries_cache.get("at", 0)
    if hit is not None and now - at < _COUNTRIES_TTL:
        return {"countries": hit}

    with _get_conn() as (conn, cur):
        cur.execute(
            """SELECT country, MAX(country_code) AS country_code, COUNT(*) AS visits
               FROM visits
               WHERE country != '' AND country NOT IN ('Local')
               GROUP BY country
               ORDER BY visits DESC"""
        )
        rows = [dict(r) for r in cur.fetchall()]

    countries = []
    for r in rows:
        code = (r.get("country_code") or "").strip().lower()
        if not code:
            code = country_code_for_name(r["country"])
        countries.append({"country": r["country"], "code": code, "visits": r["visits"]})

    countries = [c for c in countries if c["country"].strip()]
    _countries_cache = {"data": countries, "at": now}
    return {"countries": countries}