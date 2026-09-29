from pydantic import BaseModel, Field


class ProfileRefreshRequest(BaseModel):
    resume_text: str | None = Field(default=None, max_length=200000, description="Raw resume text. When omitted, the stored resume file is read instead.")


class MatchRequest(BaseModel):
    jobs: list[dict] = Field(default_factory=list, max_length=500, description="Scraped job dicts to score against the candidate profile.")
    min_score: float = Field(default=0.0, ge=0.0, le=100.0, description="Only return jobs at or above this score.")