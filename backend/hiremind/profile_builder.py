# Builds a structured HireMind candidate profile from (PII-scrubbed) resume text.
# Reuses the existing LLM dispatch chain (LLMClient.keyword_chat), the shared
# json_parser, and utils.pii — no new providers or network paths.
from hiremind.prompts import PROFILE_EXTRACTION_PROMPT
from llm.llm_client import LLMClient
from utils.json_parser import extract_json
from utils.pii import sanitize_resume_text

PROFILE_VERSION = 1

_TARGET_ROLES_LIMIT = 10
_SKILLS_LIMIT = 40
_EXPERIENCE_LIMIT = 15
_EDUCATION_LIMIT = 15
_CERTS_LIMIT = 30
_LANGUAGES_LIMIT = 15
_KEYWORDS_LIMIT = 30


def _as_text(value, default=""):
    if isinstance(value, str):
        return value.strip()
    return default


def _as_number(value, default=None):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _as_bool(value, default=None):
    if isinstance(value, bool):
        return value
    return default


def _as_list(value, default):
    if isinstance(value, list):
        return value
    return default


def _clean_profile(raw: dict) -> dict:
    raw = raw or {}

    skills = []
    for s in _as_list(raw.get("skills"), [])[:_SKILLS_LIMIT]:
        if not isinstance(s, dict):
            continue
        name = _as_text(s.get("name"))
        if not name:
            continue
        skills.append({
            "name": name,
            "category": _as_text(s.get("category"), "skill"),
            "years": _as_number(s.get("years")),
        })

    experience = []
    for e in _as_list(raw.get("experience"), [])[:_EXPERIENCE_LIMIT]:
        if not isinstance(e, dict):
            continue
        role = _as_text(e.get("role"))
        if not role:
            continue
        experience.append({
            "role": role,
            "company": _as_text(e.get("company")),
            "years": _as_number(e.get("years")),
            "domain": _as_text(e.get("domain")),
            "highlights": _as_list(e.get("highlights"), [])[:10],
        })
        experience[-1]["highlights"] = [_as_text(h) for h in experience[-1]["highlights"] if _as_text(h)]

    education = []
    for e in _as_list(raw.get("education"), [])[:_EDUCATION_LIMIT]:
        if not isinstance(e, dict):
            continue
        degree = _as_text(e.get("degree"))
        if degree:
            education.append({
                "degree": degree,
                "school": _as_text(e.get("school")),
                "year": _as_text(e.get("year")),
            })

    remote_preferred = _as_bool(raw.get("remote_preferred"))
    return {
        "version": PROFILE_VERSION,
        "summary": _as_text(raw.get("summary")),
        "headline": _as_text(raw.get("headline")),
        "seniority": _as_text(raw.get("seniority"), "unknown"),
        "yoe": _as_number(raw.get("yoe")),
        "skills": skills,
        "experience": experience,
        "education": education,
        "certifications": [_as_text(c) for c in _as_list(raw.get("certifications"), [])[:_CERTS_LIMIT] if _as_text(c)],
        "languages": [_as_text(l) for l in _as_list(raw.get("languages"), [])[:_LANGUAGES_LIMIT] if _as_text(l)],
        "target_roles": [_as_text(r) for r in _as_list(raw.get("target_roles"), [])[:_TARGET_ROLES_LIMIT] if _as_text(r)],
        "remote_preferred": remote_preferred,
        "internship_mode": bool(_as_bool(raw.get("internship_mode"), False)),
        "keywords": [_as_text(k) for k in _as_list(raw.get("keywords"), [])[:_KEYWORDS_LIMIT] if _as_text(k)],
        "preferred": {
            "locations": [],
            "remote": bool(remote_preferred),
        },
    }


def build_profile(resume_text: str, email: str = "", user: dict = None) -> dict:
    """Build a structured candidate profile from resume text.

    PII is scrubbed BEFORE any text leaves the process for the LLM. The optional
    `user` row (from db.get_user) enriches the profile with the location the
    candidate already declared on their JobAwn profile.
    """
    from config import TARGET_ROLES

    sanitized = sanitize_resume_text(resume_text or "")
    # The prompt's JSON sample uses literal braces, so a plain .format() would
    # misread them as replacement fields — use .replace() instead.
    prompt = (
        PROFILE_EXTRACTION_PROMPT
        .replace("{target_roles}", ", ".join(TARGET_ROLES))
        .replace("{resume}", sanitized)
    )
    raw = {}
    for _ in range(2):
        try:
            response = LLMClient.keyword_chat(prompt, max_tokens=4000)
            if response:
                parsed = extract_json(response)
                if isinstance(parsed, dict):
                    raw = parsed
                    break
        except Exception:
            pass

    profile = _clean_profile(raw)
    profile["version"] = PROFILE_VERSION

    user = user or {}
    loc = {
        "city": user.get("city") or "",
        "state": user.get("state") or "",
        "country": user.get("country") or "",
    }
    if any(loc.values()):
        profile["preferred"]["locations"] = [loc]
    profile["source_resume"] = user.get("resume_filename") or ""
    profile["email"] = email
    return profile