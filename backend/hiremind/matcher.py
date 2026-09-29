# Offline, deterministic job-scoring used to rank scraped jobs against the
# candidate's HireMind profile. Pure regex/token matching — no LLM calls, so
# this path is cheap enough to run over the whole jobs listing in-process.
import json
import re

from hiremind.normalizer import normalize_location

# Component weights (sum to 100).
_WEIGHTS = {"role": 35, "skills": 30, "seniority": 15, "location": 10, "salary": 10}

# Shared YOE vocabulary from utils.experience_level — order matters for proximity.
_BUCKETS = ["0-2", "2-4", "4-7", "7-10", "10+"]

# Board-provided experience_level / job_level tags -> bucket index.
_LEVEL_TO_BUCKET_IDX = {
    "internship": 0, "student": 0, "entry level": 0, "entry_level": 0,
    "associate": 0, "trainee": 0, "junior": 0, "new grad": 0, "fresher": 0,
    "mid-level": 1, "mid level": 1, "mid-senior level": 2, "senior": 2,
    "lead": 3, "manager": 3, "staff": 3, "director": 4, "executive": 4,
}

_INTERN_WORDS = ("intern", "internship", "internship program", "summer intern", "trainee")


def _norm_text(value) -> str:
    """Lowercase, collapse non-word characters to single spaces."""
    return re.sub(r"[^a-z0-9+#.]+", " ", str(value or "").lower()).strip()


def role_fit(title, target_roles) -> tuple:
    """0..1 role compatibility plus the best-matched target role (or None).

    A target role appearing verbatim as a subtitle of the title scores 1.0;
    otherwise the Dice coefficient over title/target tokens is used. With no
    target roles we stay neutral at 0.6 instead of penalising the job.
    """
    t = _norm_text(title)
    if not t:
        return 0.0, None
    roles = [r for r in (target_roles or []) if isinstance(r, str) and r.strip()]
    if not roles:
        return 0.6, None
    best, best_role = 0.0, None
    t_toks = set(t.split())
    for role in roles:
        rn = _norm_text(role)
        if not rn:
            continue
        if rn in t:
            return 1.0, role
        r_toks = set(rn.split())
        if not r_toks or not t_toks:
            continue
        overlap = 2 * len(r_toks & t_toks) / (len(r_toks) + len(t_toks))
        if overlap > best:
            best, best_role = overlap, role
    return best, best_role


def _skill_sources(profile) -> tuple:
    profile = profile or {}
    names = [s.get("name") for s in profile.get("skills", [])
             if isinstance(s, dict) and str(s.get("name") or "").strip()]
    extras = [k for k in profile.get("keywords", []) if isinstance(k, str) and k.strip()]
    certs = [c for c in profile.get("certifications", []) if isinstance(c, str) and c.strip()]
    return names, extras, certs


def _matches_in(job_text: str, terms) -> list:
    """Return the terms whose words all appear in the job text (order-insensitive)."""
    text = " " + re.sub(r"[^a-z0-9+#.]+", " ", job_text.lower()) + " "
    found = []
    for term in terms:
        t = _norm_text(term)
        if t and f" {t} " in text:
            found.append(str(term).strip())
    return found


def _job_search_text(job) -> str:
    parts = [
        job.get("title"),
        " ".join(job.get("tags") or []),
        job.get("description"),
    ]
    text = " ".join(str(p) for p in parts if p)
    return text[:2500]


def skill_fit(profile, job_text) -> tuple:
    """0..1 skill overlap plus the list of matched skill names.

    Core skill names count first; keywords and certifications fill up to the
    expected denominator (at most 6). No discernible skill signal -> neutral.
    """
    names, extras, certs = _skill_sources(profile)
    expected_raw = names or extras or certs
    if not expected_raw:
        return 0.6, []
    matched_core = _matches_in(job_text, names)
    expected = min(6, max(1, len(expected_raw)))
    matched = list(matched_core)
    bonus = _matches_in(job_text, extras + certs)
    for b in bonus:
        if len(matched) >= expected:
            break
        if b not in matched:
            matched.append(b)
    return min(1.0, len(matched) / expected), matched_core


def _profile_bucket(profile) -> int | None:
    yoe = profile.get("yoe")
    if isinstance(yoe, (int, float)) and not isinstance(yoe, bool):
        if yoe <= 2:
            return 0
        if yoe <= 4:
            return 1
        if yoe <= 7:
            return 2
        if yoe <= 10:
            return 3
        return 4
    seniority = str(profile.get("seniority") or "").strip().lower()
    return {
        "intern": 0, "internship": 0, "entry": 0, "entry level": 0,
        "entry-level": 0, "junior": 0, "fresher": 0, "associate": 0,
        "trainee": 0, "new grad": 0, "graduate": 0,
        "mid": 1, "mid-level": 1, "mid level": 1, "intermediate": 1,
        "senior": 2, "snr": 2, "sr": 2,
        "lead": 3, "manager": 3, "staff": 3, "principal": 3, "architect": 3,
        "director": 4, "head": 4, "executive": 4, "vp": 4, "chief": 4,
    }.get(seniority)


def _job_bucket(job) -> int | None:
    yb = job.get("yoe_bucket")
    if yb is not None:
        try:
            return _BUCKETS.index(str(yb).strip().lower())
        except ValueError:
            pass
    level = str(job.get("experience_level") or job.get("job_level") or "").strip().lower()
    return _LEVEL_TO_BUCKET_IDX.get(level)


def _job_internship(job) -> bool:
    level = str(job.get("experience_level") or "").strip().lower()
    if level in ("internship", "student"):
        return True
    title = str(job.get("title") or "").lower()
    return any(w in title for w in _INTERN_WORDS)


def seniority_fit(profile, job) -> tuple:
    """0..1 seniority fit plus a human note."""
    if _job_internship(job):
        if profile.get("internship_mode"):
            return 1.0, "Internship — matches your internship mode"
        return 0.1, "Internship role (your profile is not in internship mode)"
    p_idx = _profile_bucket(profile)
    j_idx = _job_bucket(job)
    if j_idx is None:
        return 0.6, "Experience requirement not specified on the job"
    if p_idx is None:
        return 0.6, "Candidate experience level unknown"
    distance = abs(p_idx - j_idx)
    score = max(0.0, 1.0 - 0.25 * distance)
    note = f"Job asks ~{_BUCKETS[j_idx]} yrs, profile at ~{_BUCKETS[p_idx]} yrs"
    return score, note


def _job_remote(job) -> bool:
    if isinstance(job.get("is_remote"), bool):
        return job["is_remote"]
    loc = job.get("location")
    if loc and normalize_location(loc)["is_remote"]:
        return True
    if isinstance(job.get("tags"), list):
        tags = " ".join(str(t) for t in job["tags"]).lower()
        if re.search(r"\b(remote|work from home|wfh|100% remote)\b", tags):
            return True
    return False


def location_fit(profile, job) -> tuple:
    """0..1 location fit plus a human note."""
    loc = normalize_location(job.get("location"))
    remote = _job_remote(job)
    pref = (profile or {}).get("preferred") or {}
    pref_locs = pref.get("locations") or []
    rp = profile.get("remote_preferred")
    if rp is None and "remote" in pref:
        rp = bool(pref.get("remote"))

    if pref_locs:
        jc, jcity = (loc.get("country") or "").lower(), (loc.get("city") or "").lower()
        for pl in pref_locs:
            pc = str(pl.get("country") or "").lower()
            pci = str(pl.get("city") or "").lower()
            if pc and jc and (pc in jc or jc in pc):
                return 1.0, "Location matches your preferred region"
            if pci and jcity and pci == jcity:
                return 1.0, f"City matches your preference: {pl.get('city')}"
        if jc:
            return 0.3, "Location differs from your preferences"

    if rp is True:
        if remote:
            return 1.0, "Remote — matches your preference"
        return 0.6, "On-site role (you prefer remote)"
    if rp is False:
        if remote:
            return 0.4, "Remote role (you prefer on-site)"
        return 1.0, "Location fits your preference"
    if remote:
        return 0.7, "Remote role"
    if loc.get("country") or loc.get("city"):
        return 0.6, "Location not verified against your preferences"
    return 0.6, "Location not specified on the posting"


def salary_fit(profile, job) -> tuple:
    """0..1 salary component + a human note.

    The Phase-1 profile carries no salary expectation, so this stays neutral;
    the reason string still surfaces the posted compensation when available.
    """
    sal = job.get("salary_norm") or job.get("salary")
    if sal:
        text = sal
        if isinstance(sal, str):
            try:
                parsed = json.loads(sal)
                text = parsed.get("text") or text
            except Exception:
                pass
        return 0.6, f"Posted compensation: {text}"
    return 0.6, "Salary not factored (no expectation on your profile)"


def score_job(profile, job) -> dict:
    """Score one job 0..100 against a profile, with per-component breakdown."""
    profile = profile or {}
    job = job or {}
    title = str(job.get("title") or "Untitled")
    job_text = _job_search_text(job)

    role_score, matched_role = role_fit(title, profile.get("target_roles"))
    skill_score, matched_skills = skill_fit(profile, job_text)
    seniority_score, seniority_note = seniority_fit(profile, job)
    location_score, location_note = location_fit(profile, job)
    salary_score, salary_note = salary_fit(profile, job)

    totals = {
        "role": role_score * _WEIGHTS["role"],
        "skills": skill_score * _WEIGHTS["skills"],
        "seniority": seniority_score * _WEIGHTS["seniority"],
        "location": location_score * _WEIGHTS["location"],
        "salary": salary_score * _WEIGHTS["salary"],
    }
    score = int(round(sum(totals.values())))
    score = max(0, min(100, score))
    breakdown = {k: int(round(v)) for k, v in totals.items()}

    reasons = []
    if matched_role:
        reasons.append(f"Title matches your target role: {matched_role}")
    else:
        reasons.append("No target-role match found in the title")
    if matched_skills:
        reasons.append("Matched skills: " + ", ".join(matched_skills[:6]))
    else:
        reasons.append("No skill overlap detected")
    reasons.append(seniority_note)
    reasons.append(location_note)
    reasons.append(salary_note)
    if job.get("_seen_before"):
        reasons.append("You already saw this posting on another board")

    return {
        "score": score,
        "verdict": ("strong" if score >= 80 else
                    "solid" if score >= 60 else
                    "possible" if score >= 40 else "weak"),
        "breakdown": breakdown,
        "matched_role": matched_role,
        "matched_skills": matched_skills,
        "reasons": reasons,
    }


def score_jobs(profile, jobs, min_score=0.0, limit=200) -> list:
    """Score a batch of jobs, sorted best-first. Caller jobs are not mutated."""
    if not isinstance(jobs, list):
        return []
    results = []
    for j in jobs[:limit]:
        if not isinstance(j, dict):
            continue
        copy = dict(j)
        copy["match"] = score_job(profile, copy)
        if copy["match"]["score"] >= min_score:
            results.append(copy)
    results.sort(key=lambda x: x["match"]["score"], reverse=True)
    return results