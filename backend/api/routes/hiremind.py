import os
from fastapi import APIRouter, Depends, HTTPException, Request

from api.deps import get_current_user
from db import get_candidate_profile, get_user, save_candidate_profile
from hiremind.config import hiremind_enabled
from hiremind.profile_builder import PROFILE_VERSION, build_profile
from hiremind.schemas import MatchRequest, ProfileRefreshRequest
from utils.client_ip import get_client_ip
from utils.rate_limiter import check_rate_limit

router = APIRouter(prefix="/api/hiremind", tags=["hiremind"])

# Profile extraction is an LLM call — cap per user so a refresh storm cannot
# drain the token bucket (mirrors api/routes/resume.py's keyword-extraction cap).
_PROFILE_RATE = 5
_PROFILE_WINDOW = 60

# Batch matching is pure offline scoring, but a client could still spam it —
# cap per user per minute.
_MATCH_RATE = 30
_MATCH_WINDOW = 60
_MATCH_LIMIT = 200

_RESUME_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "resumes")


def _enabled_or_404():
    if not hiremind_enabled():
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/profile")
async def hiremind_profile(user: dict = Depends(get_current_user)):
    """Return the candidate's structured HireMind profile (404 until it is built)."""
    _enabled_or_404()
    row = get_candidate_profile(user["email"])
    if not row:
        raise HTTPException(status_code=404, detail="No HireMind profile yet. Call POST /api/hiremind/profile/refresh.")
    return {"ok": True, "email": user["email"], "profile": row["profile"]}


@router.post("/profile/refresh")
async def hiremind_profile_refresh(req: ProfileRefreshRequest, request: Request = None,
                                   user: dict = Depends(get_current_user)):
    """Build (or rebuild) the candidate profile from resume text and persist it.

    `resume_text` is optional — when omitted, the user's stored resume file is
    read and extracted through the existing resume parsing pipeline.
    """
    _enabled_or_404()
    email = user["email"]
    client_ip = get_client_ip(request)
    if client_ip and not check_rate_limit(f"hiremind_profile:{email}", _PROFILE_RATE, _PROFILE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    user_row = get_user(email) or {}
    resume_text = (req.resume_text or "").strip()
    filename = user_row.get("resume_filename", "")
    if not resume_text:
        if not filename:
            raise HTTPException(status_code=400, detail="No resume on file. Upload one or pass resume_text.")
        filepath = os.path.join(_RESUME_DIR, filename)
        if not os.path.isfile(filepath):
            raise HTTPException(status_code=404, detail="Resume file not found")
        from api.routes.resume import _extract_text
        resume_text = _extract_text(filepath)

    profile = build_profile(resume_text, email=email, user=user_row)
    save_candidate_profile(
        email,
        resume_filename=filename,
        parsed_text=resume_text,
        profile_json=profile,
        version=profile.get("version", PROFILE_VERSION),
    )
    return {"ok": True, "email": email, "profile": profile}


@router.post("/match")
async def hiremind_match(req: MatchRequest, request: Request = None,
                         user: dict = Depends(get_current_user)):
    """Score scraped jobs 0–100 against the candidate's profile, with reasons.

    Deterministic, offline matching — no LLM calls, so it is safe to run over a
    whole session's jobs. Returns jobs sorted best-first; each job carries a
    `match` block (score, verdict, breakdown, matched role/skills, reasons).
    """
    _enabled_or_404()
    email = user["email"]
    client_ip = get_client_ip(request)
    if client_ip and not check_rate_limit(f"hiremind_match:{email}", _MATCH_RATE, _MATCH_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    row = get_candidate_profile(email)
    if not row:
        raise HTTPException(status_code=409, detail="No HireMind profile yet. Call POST /api/hiremind/profile/refresh first.")

    from hiremind.matcher import score_jobs
    jobs = score_jobs(row["profile"], req.jobs, min_score=req.min_score, limit=_MATCH_LIMIT)
    return {"ok": True, "email": email, "count": len(jobs), "jobs": jobs}