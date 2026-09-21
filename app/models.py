"""Типізовані I/O кожного кроку pipeline (pydantic)."""
from pydantic import BaseModel


class Article(BaseModel):
    id: int | str  # int — SQLite articles.db, str (uuid) — comms.core.article
    url: str
    title: str
    text: str
    source: str
    published_at: str
    processed_flag: bool = False
    topic: str | None = None


class RelevanceResult(BaseModel):
    article_id: int | str
    relevant: bool
    reason: str
    score: float  # 0..1


class Extraction(BaseModel):
    article_id: int | str
    main_claim: str
    facts: list[str]
    examples: list[str]
    angle: str


class Draft(BaseModel):
    article_id: int | str
    headline: str
    text: str
    model_version: str


class Evaluation(BaseModel):
    article_id: int | str
    quality_score: float  # 0..1
    failure_type: str | None = None  # None = ok; інакше: too_short | off_topic | hallucination | ...


class DraftResult(BaseModel):
    article: Article
    relevance: RelevanceResult
    extraction: Extraction | None = None
    draft: Draft | None = None
    evaluation: Evaluation | None = None
    notion_page_id: str | None = None


class RunSummary(BaseModel):
    run_id: str
    status: str  # running | ok | failed
    started_at: str
    duration_ms: int | None = None
    articles_in: int = 0
    relevant_count: int = 0
    drafts_written: int = 0
    failure_type: str | None = None
    model_version: str
