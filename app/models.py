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
    """Чому стаття обрана: місце в топі дня векторного відбору Андрія (ml.daily_pick)."""
    article_id: int | str
    relevant: bool
    reason: str
    score: float  # бал ранкера Андрія: ½ відповідність темі + ½ схожість на взірці


class Draft(BaseModel):
    article_id: int | str
    headline: str
    text: str
    model_version: str
    lang: str  # мова поста, яку попросили в моделі: uk | en
    # mode=draft_grounded: факти, з яких написано пост ({"claim", "verbatim_quote"}), і ті, що не пройшли перевірку
    facts_used: list[dict] = []
    facts_rejected: list[dict] = []


class DraftResult(BaseModel):
    article: Article
    relevance: RelevanceResult
    draft: Draft | None = None
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
