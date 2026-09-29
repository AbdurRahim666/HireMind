# HireMind Phase 3a — read-only fit scoring for the jobs a user already has.
#
# Pure module: no DB, no LLM, no config, no network. It only reshapes the
# existing offline `hiremind.matcher` output into a compact map keyed by job
# URL so a client can join scores onto the job cards it is already rendering,
# without re-fetching jobs and without touching any stored score.
#
# Nothing here mutates its inputs or writes anywhere. Every existing JobAwn
# score (ai_score / keyword_score / total_score) is left exactly as it was.

from hiremind.matcher import score_job

# A session can hold hundreds of jobs; the endpoint caps the batch, but this is
# the last line of defence against an unexpectedly large session.
DEFAULT_LIMIT = 300

# Reasons are the heaviest field (long human strings). Three is enough for a
# "why did this match" tooltip without bloating the response.
_REASONS_LIMIT = 3


def _compact(match: dict) -> dict:
    """Trim a full matcher block down to what the UI actually renders.

    `breakdown` is intentionally dropped: it is useful for debugging/tests but
    adds five integers per job for no display value.
    """
    return {
        "score": match["score"],
        "verdict": match["verdict"],
        "matched_role": match.get("matched_role") or "",
        "matched_skills": list(match.get("matched_skills") or []),
        "reasons": list(match.get("reasons") or [])[:_REASONS_LIMIT],
    }


def score_for_urls(profile: dict, jobs: list, limit: int = DEFAULT_LIMIT) -> dict:
    """Score a session's jobs against a profile, keyed by job URL.

    Returns {"scores": {url: compact_match}, "count": n}. Jobs without a URL are
    skipped (there is nothing for the client to join on) and non-dict entries are
    ignored, so a malformed row can never take the whole batch down. Input jobs
    are never mutated.
    """
    scores: dict = {}
    rows = jobs if isinstance(jobs, list) else []
    for job in rows[:limit]:
        if not isinstance(job, dict):
            continue
        url = job.get("url")
        if not url or not isinstance(url, str):
            continue
        scores[url] = _compact(score_job(profile, job))
    return {"scores": scores, "count": len(scores)}
