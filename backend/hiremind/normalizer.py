# HireMind Phase 2 — offline job normalization & fingerprinting.
# Pure functions only: no DB, no LLM, no config. Deterministic and safe to run
# on every scraped job.

import hashlib
import json
import re

_CURRENCY_NAMES = {
    "AED": "AED", "SAR": "SAR", "KWD": "KWD", "OMR": "OMR", "BHD": "BHD",
    "QAR": "QAR", "USD": "USD", "EUR": "EUR", "GBP": "GBP", "INR": "INR",
    "EGP": "EGP", "PHP": "PHP", "ZAR": "ZAR", "PKR": "PKR", "CAD": "CAD",
    "AUD": "AUD", "MYR": "MYR", "IDR": "IDR", "TRY": "TRY", "CNY": "CNY",
    "JPY": "JPY", "NPR": "NPR", "LKR": "LKR", "BDT": "BDT",
}
_CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY",
    "د.إ": "AED",
}
_CURRENCIES = {**{k.lower(): v for k, v in _CURRENCY_NAMES.items()}, **_CURRENCY_SYMBOLS}

_PER_HOUR_RE = re.compile(r"per\s*hour|/hr|/hour|hourly", re.I)
_PER_DAY_RE = re.compile(r"per\s*day|/day|daily", re.I)
_PER_MONTH_RE = re.compile(r"per\s*month|per\s*mo\b|/month|/mo\b|monthly", re.I)
_PER_YEAR_RE = re.compile(
    r"per\s*annum|per\s*year|/annum|/yr\b|/year|\bannum\b|\byearly\b|\bannual\b"
    r"|\bp\.?\s*a\.?\b|\bpa\b(?!\.)",
    re.I,
)

_LOW_RE = re.compile(r"\b(from|starting at|min|minimum)\b", re.I)
_HIGH_RE = re.compile(r"\b(up to|max|maximum)\b", re.I)

# ── Cross-currency comparison (fingerprint keys only) ──
#
# Two boards advertising the same ad rarely use the same currency, so the raw
# salary string cannot go into a dedup key verbatim. We convert to a
# purchasing-power-adjusted USD figure using two static tables (no network, no
# config): an approximate market FX rate and a rough cost-of-living index.
# Both are deliberately coarse — precision comes from the bucketing below, not
# from these numbers.
_MARKET_USD = {
    "AED": 0.2723, "SAR": 0.2666, "KWD": 3.257, "OMR": 2.600, "BHD": 2.660,
    "QAR": 0.2747, "USD": 1.0, "EUR": 1.0850, "GBP": 1.2720, "INR": 0.0120,
    "EGP": 0.0208, "PHP": 0.0177, "ZAR": 0.0530, "PKR": 0.0036, "CAD": 0.7300,
    "AUD": 0.6600, "MYR": 0.2200, "IDR": 0.000062, "TRY": 0.0290, "CNY": 0.1390,
    "JPY": 0.0064, "NPR": 0.0075, "LKR": 0.0034, "BDT": 0.0092,
}
# Cost of living relative to the US (1.0). Used so an AED posting and a USD
# posting covering the same real-world job budget land on the same key.
_COL_INDEX = {
    "AED": 0.60, "SAR": 0.62, "KWD": 0.72, "OMR": 0.62, "BHD": 0.68,
    "QAR": 0.65, "USD": 1.0, "EUR": 0.85, "GBP": 0.87, "INR": 0.28,
    "EGP": 0.25, "PHP": 0.34, "ZAR": 0.42, "PKR": 0.25, "CAD": 0.90,
    "AUD": 0.92, "MYR": 0.35, "IDR": 0.35, "TRY": 0.32, "CNY": 0.45,
    "JPY": 0.65, "NPR": 0.25, "LKR": 0.25, "BDT": 0.28,
}
# How much a USD-equivalent buys in each currency (FX adjusted for cost of
# living). Only these currencies have a comparable key; anything else falls
# back to the raw-text key.
_PPP_USD = {
    cur: fx / col for cur, fx, col in
    ((c, _MARKET_USD[c], _COL_INDEX[c]) for c in _MARKET_USD if c in _COL_INDEX)
}

# Significant figures kept when bucketing an annual figure. Two sig figs makes
# the key tolerant of the rough conversion above while still separating
# materially different offers (AED 6k vs AED 6k+1k per month stay distinct).
_SIG_FIGS = 2


def _band(value) -> int:
    """Floor `value` to 2 significant figures (0 for anything non-positive)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    magnitude = len(str(int(abs(v)))) - _SIG_FIGS
    step = 10 ** magnitude if magnitude > 0 else 1
    return int(v // step) * step

_AMOUNT_RE = re.compile(
    r"(?:(?P<cur>\bAED\b|\bSAR\b|\bKWD\b|\bOMR\b|\bBHD\b|\bQAR\b|\bUSD\b"
    r"|\bEUR\b|\bGBP\b|\bINR\b|\bEGP\b|\bPHP\b|\bZAR\b|\bPKR\b|\bCAD\b"
    r"|\bAUD\b|\bMYR\b|\bIDR\b|\bTRY\b|\bCNY\b|\bJPY\b|[$€£₹¥]))?"
    r"\s*(?P<num>\d[\d,.]*)\s*(?P<mult>[kK]|thousand|\blakh\b|\blacs?|\bcrore\b|\bcr\b)?",
    re.I,
)

_MULTIPLIERS = {
    "k": 1000, "K": 1000, "thousand": 1000,
    "lakh": 100000, "lacs": 100000, "lac": 100000,
    "crore": 10000000, "cr": 10000000,
}


def _to_int(num_text: str) -> int:
    cleaned = num_text.replace(",", "").replace(" ", "")
    try:
        return int(round(float(cleaned)))
    except (ValueError, TypeError):
        return 0


def _currency_of(text: str) -> str | None:
    found = None
    for key in _CURRENCIES:
        if key in text.lower():
            found = _CURRENCIES[key]
            if len(key) >= 2:
                return found
    return found


def _period_of(text: str) -> str | None:
    if _PER_HOUR_RE.search(text):
        return "hour"
    if _PER_DAY_RE.search(text):
        return "day"
    if _PER_MONTH_RE.search(text):
        return "month"
    if _PER_YEAR_RE.search(text):
        return "year"
    return None


def normalize_salary(raw) -> dict | None:
    """Parse a raw salary string into a structured record.

    Returns None when nothing numeric can be extracted. Otherwise returns
    {min, max, currency, per, semantics, eligible, text} where eligible means
    at least one numeric bound is present. Hourly/monthly/annual values are
    kept as-stated; `yearly` gives an annualized estimate (per-period aware,
    raw currency) useful for Phase 3 filtering.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return {
            "min": value, "max": value, "currency": None, "per": None,
            "semantics": "exact", "eligible": True,
            "yearly": {"min": value, "max": value}, "usd_annual": None, "text": "",
        }
    text = " ".join(str(raw).split())
    if not text:
        return None

    matches = list(_AMOUNT_RE.finditer(text))
    values = []
    for m in matches:
        num = _to_int(m.group("num"))
        mult = m.group("mult")
        if m.group("mult") in _MULTIPLIERS:
            num *= _MULTIPLIERS[mult]
        values.append((int(m.start()), int(m.end()), num))
    if not values:
        return None

    currency = _currency_of(text)
    per = _period_of(text)

    low, high, semantics = None, None, "exact"
    if len(values) >= 2:
        gap = text[values[0][1]:values[1][0]].strip()
        if not gap or gap in {"-", "–", "to", "--", "—"}:
            low, high, semantics = values[0][2], values[1][2], "range"
    if semantics != "range":
        value = values[0][2]
        if _HIGH_RE.search(text):
            low, high = None, value
            semantics = "max"
        elif _LOW_RE.search(text):
            low, high = value, None
            semantics = "min"
        else:
            low, high = value, value
            semantics = "exact"

    if low is None and high is None:
        return None

    yearly = _annualized(low, high, per)
    return {
        "min": low, "max": high, "currency": currency, "per": per,
        "semantics": semantics, "eligible": True, "yearly": yearly,
        "usd_annual": usd_annual(yearly, currency), "text": text,
    }


def _annualized(low, high, per):
    scale = {"year": 1.0, "month": 12.0, "day": 260.0, "hour": 2080.0}.get(per)
    if scale is None:
        scale = 1.0
    lo = (low * scale) if low is not None else None
    hi = (high * scale) if high is not None else None
    return {"min": lo, "max": hi}


def usd_annual(yearly: dict, currency: str | None) -> dict | None:
    """Convert an annualized {min, max} into purchasing-power-adjusted USD.

    Returns None when the currency is unknown or absent, so callers can fall
    back to the raw-currency key. Open bounds (min or max None) stay None.
    """
    rate = _PPP_USD.get((currency or "").upper())
    if rate is None:
        return None
    out = {}
    for bound in ("min", "max"):
        value = (yearly or {}).get(bound)
        out[bound] = None if value is None else value * rate
    return out


_EXP_RANGE_RE = re.compile(
    r"(?P<min>\d{1,2})\s*(?:-|–|to)\s*(?P<max>\d{1,2})\s*(?:y(?:ear|r)s?|yrs?|years of experience)",
    re.I,
)
_EXP_MIN_RE = re.compile(
    r"(?:\b(?:min|minimum|at least|over|more than)\b\s*)?"
    r"(?P<n>\d{1,2})\s*\+?\s*(?:y(?:ear|r)s?|yrs?|years of experience|years)",
    re.I,
)
_EXP_EXACT_RE = re.compile(
    r"(?P<n>\d{1,2})\s*(?:y(?:ear|r)s?|yrs?)\s+of\s+experience",
    re.I,
)


def normalize_experience(raw) -> dict | None:
    """Parse an experience string into min/max years.

    Returns {min_years, max_years, semantics} or None when nothing matches.
    """
    if raw is None:
        return None
    s = " ".join(str(raw).split())
    if not s:
        return None
    m = _EXP_RANGE_RE.search(s)
    if m:
        return {"min_years": int(m.group("min")), "max_years": int(m.group("max")),
                "semantics": "range"}
    m = _EXP_EXACT_RE.search(s)
    if m:
        return {"min_years": int(m.group("n")), "max_years": int(m.group("n")),
                "semantics": "exact"}
    m = _EXP_MIN_RE.search(s)
    if m:
        return {"min_years": int(m.group("n")), "max_years": None, "semantics": "min"}
    return None


_REMOTE_WORDS = ("remote", "work from home", "work from anywhere", "telecommute")
_JOB_TYPE_WORDS = ("full time", "full-time", "part time", "part-time",
                   "permanent", "contract", "temporary", "freelance",
                   "hybrid", "remote", "onsite", "on-site", "in-office")

_COUNTRIES = {
    "uae": "UAE", "united arab emirates": "UAE", "emirates": "UAE",
    "saudi arabia": "Saudi Arabia", "ksa": "Saudi Arabia", "saudi": "Saudi Arabia",
    "qatar": "Qatar", "kuwait": "Kuwait", "bahrain": "Bahrain", "oman": "Oman",
    "usa": "USA", "us": "USA", "united states": "USA",
    "united states of america": "USA", "uk": "United Kingdom",
    "u.k.": "United Kingdom", "united kingdom": "United Kingdom",
    "england": "United Kingdom", "ireland": "Ireland", "egypt": "Egypt",
    "india": "India", "canada": "Canada", "australia": "Australia",
    "philippines": "Philippines", "pakistan": "Pakistan", "singapore": "Singapore",
    "germany": "Germany", "france": "France", "netherlands": "Netherlands",
    "china": "China", "japan": "Japan", "switzerland": "Switzerland",
    "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark",
}
_UAE_CITIES = {
    "dubai": "Dubai", "abu dhabi": "Abu Dhabi", "sharjah": "Sharjah",
    "ajman": "Ajman", "ras al khaimah": "Ras Al Khaimah",
    "umm al quwain": "Umm Al Quwain", "fujairah": "Fujairah",
}


def _clean_loc_seg(seg: str) -> str:
    seg = (seg or "").strip()
    for w in _JOB_TYPE_WORDS:
        seg = re.sub(rf"\b{re.escape(w)}\b", "", seg, flags=re.I)
    seg = re.sub(r"[(),]", " ", seg)
    seg = re.sub(r"\s+", " ", seg).strip()
    return seg


def _segment_parts(seg: str) -> list:
    """Split one bullet/decorator segment into geoish parts."""
    parts = []
    for p in re.split(r",", seg):
        for part in re.split(r"\s+[-–]\s+", p):
            part = _clean_loc_seg(part)
            if part:
                parts.append(part)
    return parts


def _parts_of(seg: str) -> list | None:
    """Return parsed parts of a segment, or None when the segment carries no
    recognizable geography (pure job-type labels like 'Hybrid'/'Full-time')."""
    parts = _segment_parts(seg)
    if not parts:
        return None
    for p in parts:
        if p.lower() in _COUNTRIES or p.lower() in _UAE_CITIES:
            return parts
    return None


def normalize_location(raw) -> dict:
    """Structure a location string into city/region/country + remote flag.

    Tolerates board decorations ("Hybrid • Dubai, UAE • Full-time",
    "Bengaluru, Karnataka, India", "Remote", "Dubai - United Arab Emirates").
    """
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return {"city": "", "region": "", "country": "", "is_remote": False}
    lower = text.lower()
    is_remote = any(tok in lower for tok in _REMOTE_WORDS)

    body = re.sub(r"\((?:remote|hybrid|on-site|onsite|office)\)", "", text, flags=re.I)
    segments = [seg.strip(" -–") for seg in re.split(r"[•|]", body) if seg.strip(" -–")]

    segs = []
    for seg in segments:
        parts = _parts_of(seg)
        if parts:
            segs = parts
            break
    if not segs and segments:
        segs = _segment_parts(segments[0]) or []

    country = ""
    for i in range(len(segs) - 1, -1, -1):
        token = segs[i].strip().lower()
        if token in _COUNTRIES:
            country = _COUNTRIES[token]
            del segs[i]
            break

    city = ""
    if segs:
        fl = segs[0].lower()
        if fl in _UAE_CITIES:
            city = _UAE_CITIES[fl]
            if not country:
                country = "UAE"
            segs = segs[1:]
    if not city:
        for i in range(len(segs) - 1, -1, -1):
            sl = segs[i].lower()
            if sl in _UAE_CITIES:
                city = _UAE_CITIES[sl]
                del segs[i]
                break

    remaining = [s for s in segs if s]
    if not city and remaining:
        city = remaining[0]
        remaining = remaining[1:]
    region = ""
    if remaining:
        region = remaining[0]

    return {"city": city, "region": region, "country": country, "is_remote": is_remote}


def _canon_title(title: str) -> str:
    t = re.sub(r"[^\w\s/-]", " ", (title or "").strip().lower())
    return re.sub(r"\s+", " ", t).strip()


_COMPANY_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|llc|inc|incorporated|corp|corporation|pvt|private limited"
    r"|co|company|sarl|llp|plc|ag|gmbh|pte|p\.?s\.?a\.?|sp\.? z\.? o\.?o\.?|co\.?\,? k\.?g\.?)\b\.?$",
    re.I,
)


def _canon_company(company: str) -> str:
    c = re.sub(r"[^\w\s]", " ", (company or "").strip().lower())
    c = re.sub(r"\s+", " ", c).strip()
    c = _COMPANY_SUFFIX_RE.sub("", c).strip()
    return c


def _salary_key(job: dict) -> str:
    """Salary component of the fingerprint key.

    Prefers a banded purchasing-power USD figure so the same ad posted as
    "AED 6,000/month" on one board and "$1,400/month" on another collapses to
    one key. Falls back to the raw currency/min/max/per when the posting
    carries no comparable salary, and to a separate 'raw' namespace when a
    currency was recognized but has no USD conversion.
    """
    s = normalize_salary(job.get("salary"))
    if not s or not s["eligible"]:
        return "none"
    usd = s.get("usd_annual")
    if usd:
        return "usd:{:07d}-{:07d}".format(_band(usd["min"]), _band(usd["max"]))
    return f"raw:{s['currency'] or ''}:{s['min'] or ''}-{s['max'] or ''}:{s['per'] or ''}"


def _experience_key(job: dict) -> str:
    """Experience component of the fingerprint key.

    Prefers the board's own experience_level tag, then falls back to a years
    requirement parsed out of the description/title. Unknown contributes an
    empty segment so it never collides with a stated requirement.
    """
    level = str(job.get("experience_level") or job.get("job_level") or "").strip().lower()
    if level:
        return f"lvl:{level}"
    exp = normalize_experience(job.get("experience")) or normalize_experience(
        f"{job.get('title') or ''} {job.get('description') or ''}"
    )
    if not exp:
        return ""
    return f"yrs:{exp['min_years'] if exp['min_years'] is not None else ''}"


def job_fingerprint(job: dict) -> str:
    """Deterministic SHA-256 fingerprint of a job posting.

    Built from normalized title/company/location/salary/experience so the same
    posting scraped by different boards (different URLs) produces the same key.
    """
    loc = normalize_location((job or {}).get("location"))
    parts = [
        _canon_title((job or {}).get("title")),
        _canon_company((job or {}).get("company")),
        (loc["city"] or "").lower(),
        (loc["region"] or "").lower(),
        (loc["country"] or "").lower(),
        "remote" if loc["is_remote"] else "",
        _salary_key(job),
        _experience_key(job),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def fingerprint_parts(job: dict) -> dict:
    """Human-readable breakdown of what went into a fingerprint (for tests/debug)."""
    loc = normalize_location((job or {}).get("location"))
    return {
        "title": _canon_title((job or {}).get("title")),
        "company": _canon_company((job or {}).get("company")),
        "city": (loc["city"] or "").lower(),
        "region": (loc["region"] or "").lower(),
        "country": (loc["country"] or "").lower(),
        "remote": loc["is_remote"],
        "salary": _salary_key(job),
        "experience": _experience_key(job),
    }


def enrich_job(job: dict) -> dict:
    """Add `fingerprint` and `salary_norm` keys to a job dict (in place)."""
    job["fingerprint"] = job_fingerprint(job)
    job["salary_norm"] = json.dumps(normalize_salary(job.get("salary")), ensure_ascii=False)
    return job