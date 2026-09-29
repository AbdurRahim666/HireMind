# HireMind extraction prompt for building a structured candidate profile from a resume.
# Mirrors the style/discipline of api/routes/resume.py's EXTRACT_PROMPT: literally
# ground every field in resume text, prefer empty/unknown over invented values, and
# return ONLY raw JSON (the shared utils.json_parser extracts and cleans it).

PROFILE_EXTRACTION_PROMPT = """You are a career coach building a structured candidate profile from a resume.
Analyze the resume and return a single JSON object describing the candidate as accurately as possible.
Only use information literally present in the resume. When something is unknown or absent, use null or
an empty array — never invent values or years.

First think step-by-step about what the titles and bullet points actually show for domain, then
return ONLY the raw JSON object below. Do NOT include reasoning tags or any additional text.

{
  "summary": "2-3 sentence professional summary grounded in the resume's own framing",
  "headline": "one-line headline capturing current role and domain",
  "seniority": "junior|mid|senior|lead|unknown",
  "yoe": <float or null>,
  "skills": [{"name": "Python", "category": "language|framework|tool|domain|soft", "years": <float or null>}],
  "experience": [{"role": "...", "company": "...", "years": <float or null>, "domain": "...", "highlights": ["..."]}],
  "education": [{"degree": "...", "school": "...", "year": "..."}],
  "certifications": ["..."],
  "languages": ["..."],
  "target_roles": ["3-5 roles that best match the resume's career track"],
  "remote_preferred": <boolean or null>,
  "internship_mode": <boolean>,
  "keywords": ["top skills/technologies explicitly mentioned"]
}

Available roles for target_roles: {target_roles}

Resume:
{resume}
"""