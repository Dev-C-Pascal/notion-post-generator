"""Pipeline одного прогону: fetch → relevance → extraction → draft → evaluate → write_notion.
Кожен крок — окрема функція з типізованим I/O (models.py). Модель — через ModelClient (llm.py).
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from . import db, notion, pg
from .llm import ModelClient, get_model_client
from .models import Article, DraftResult, RunSummary

log = logging.getLogger("pipeline")

PLACEHOLDER = Article(
    id=0, url="https://example.com/placeholder", title="Тестова стаття (база статей порожня)",
    text="База статей ще порожня, тому pipeline працює на тестовій статті. "
         "Коли Data Architect наповнить articles.db, цей рядок зникне.",
    source="stub", published_at="2026-09-17T00:00:00Z", topic="тест",
)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def step_fetch(limit: int) -> list[Article]:
    articles = db.fetch_articles(limit=limit)
    return articles or [PLACEHOLDER]


async def step_write_notion(run_id: str, database_id: str, r: DraftResult) -> str:
    """Upsert у Notion: якщо для (article_id, run_id) рядок уже є — оновлюємо, інакше створюємо."""
    assert r.draft and r.evaluation
    props = notion.build_properties(
        draft=r.draft.headline,
        source=f"{r.article.title} — {r.article.url}",
        status="Done" if not r.evaluation.failure_type else "In progress",
        score=r.evaluation.quality_score,
    )
    body = (
        f"run_id: {run_id} · model: {r.draft.model_version} · relevance: {r.relevance.score:.2f} ({r.relevance.reason})"
        f" · failure_type: {r.evaluation.failure_type or 'none'}\n\n"
        f"{r.draft.text}"
    )
    existing = db.find_draft_page(r.article.id, run_id)
    if existing:
        await notion.update_row(existing, props)
        return existing
    page = await notion.create_row(database_id, props, body=body)
    return page["id"]


async def run_pipeline(run_id: str, *, database_id: str, limit: int = 1, topic: str | None = None,
                       model: ModelClient | None = None) -> RunSummary:
    model = model or get_model_client()
    t0 = time.monotonic()
    db.create_run(run_id, model.version)
    articles: list[Article] = []
    relevant = written = 0
    failure: str | None = None
    try:
        articles = step_fetch(limit)
        for a in articles:
            rel = model.relevance(a, topic)
            res = DraftResult(article=a, relevance=rel)
            if rel.relevant:
                relevant += 1
                res.extraction = model.extraction(a)
                res.draft = model.draft(a, res.extraction, n=db.count_drafts() + 1)
                res.evaluation = model.evaluate(res.draft)
                res.notion_page_id = await step_write_notion(run_id, database_id, res)
                if pg.enabled():
                    pg.save_draft(run_id, res)
                written += 1
                if a.id:
                    db.mark_processed(a.id)
            db.upsert_draft(
                run_id=run_id, article_id=a.id, notion_page_id=res.notion_page_id,
                relevance=rel.relevant, reason=rel.reason, score=rel.score,
                extraction=res.extraction.model_dump_json() if res.extraction else None,
                headline=res.draft.headline if res.draft else None,
                draft_text=res.draft.text if res.draft else None,
                failure_type=res.evaluation.failure_type if res.evaluation else None,
                model_version=model.version,
            )
        status = "ok"
    except Exception as e:
        log.exception("run %s failed", run_id)
        status, failure = "failed", type(e).__name__
    duration = int((time.monotonic() - t0) * 1000)
    db.finish_run(run_id, status=status, duration_ms=duration, articles_in=len(articles),
                  relevant_count=relevant, drafts_written=written, failure_type=failure)
    return RunSummary(run_id=run_id, status=status, started_at="", duration_ms=duration, articles_in=len(articles),
                      relevant_count=relevant, drafts_written=written, failure_type=failure, model_version=model.version)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
