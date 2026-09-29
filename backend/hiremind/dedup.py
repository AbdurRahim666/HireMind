# HireMind Phase 2 — gated known-jobs registry + cross-board duplicate flagging.
#
# Every function here is a no-op when HireMind is disabled, so the core scrape
# pipeline is 100% unchanged with the flag off. When enabled, each scraped job
# is fingerprinted once and recorded in the `known_jobs` table; a posting whose
# fingerprint was already seen (e.g. the same ad on another board under a
# different URL) is tagged `_seen_before` — never dropped.

import db
from hiremind import normalizer


def _hiremind_enabled() -> bool:
    try:
        from hiremind.config import hiremind_enabled as _flag
        return bool(_flag())
    except Exception:
        return False


def _fingerprint(job: dict) -> str:
    return (job or {}).get("fingerprint") or normalizer.job_fingerprint(job)


def _source_of(job: dict, source: str = "") -> str:
    if source:
        return source
    return (job or {}).get("_cache_site") or (job or {}).get("job_board") or ""


def record_seen(job: dict, source: str = "") -> bool:
    """Record a single job's fingerprint in known_jobs. No-op when disabled.

    Returns True if the fingerprint was new, False if already recorded (or
    HireMind disabled)."""
    if not _hiremind_enabled():
        return False
    fp = _fingerprint(job)
    if not fp:
        return False
    return db.record_known_job(
        fingerprint=fp,
        title=(job or {}).get("title", ""),
        company=(job or {}).get("company", ""),
        url=(job or {}).get("url", ""),
        source=_source_of(job, source),
    )


def dedup_tag_jobs(jobs: list, source: str = "") -> int:
    """Enrich + tag + record a batch of scraped jobs.

    For each job: compute fingerprint + salary_norm, mark `_seen_before=True`
    when the fingerprint was already in known_jobs, then record it. Returns the
    number flagged as seen-before. No-op when HireMind is disabled.

    Within one batch the first occurrence is recorded and any later posting with
    the same fingerprint in the same batch is flagged too — otherwise the same
    ad listed twice on a single board would slip through as two new jobs.
    """
    if not _hiremind_enabled() or not jobs:
        return 0
    fps = []
    for j in jobs:
        try:
            normalizer.enrich_job(j)
        except Exception:
            j["fingerprint"] = normalizer.job_fingerprint(j)
        j.setdefault("_seen_before", False)
        fps.append((j, j.get("fingerprint") or normalizer.job_fingerprint(j)))

    existing = db.known_jobs_seen([fp for _, fp in fps]) if fps else set()
    seen_in_batch = set()
    flagged = 0
    rows = []
    for j, fp in fps:
        if fp and (fp in existing or fp in seen_in_batch):
            j["_seen_before"] = True
            flagged += 1
        if fp:
            seen_in_batch.add(fp)
        if fp:
            rows.append({
                "fingerprint": fp,
                "title": j.get("title", ""),
                "company": j.get("company", ""),
                "url": j.get("url", ""),
                "source": _source_of(j, source),
            })
    if rows:
        db.record_known_jobs_bulk(rows)
    return flagged


def known_jobs_stats() -> dict:
    """Summary of the known-jobs registry. Reports enabled=False when disabled."""
    if not _hiremind_enabled():
        return {"enabled": False, "count": 0, "sources": 0}
    stats = db.known_jobs_stats()
    return {"enabled": True, **stats}