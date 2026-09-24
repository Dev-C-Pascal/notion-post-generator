"""Pipeline одного прогону: select (топ дня Андрія) → draft (модель на RunPod) → write_notion + postgen.

Релевантність — векторний відбір Андрія (bge-m3 + pgvector): топ дня, а поки його немає — тематичний відбір.
Окремих кроків extraction / evaluate немає — оцінює людина в Notion (статус «Not started»).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from . import comms, db, notion, pg
from .llm import ModelClient, get_model_client
from .models import Article, DraftResult, RelevanceResult, RunSummary

log = logging.getLogger("pipeline")

STATUS = "Not started"  # новий драфт чекає на людину: approve / edit / reject


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def step_select(limit: int) -> list[tuple[Article, RelevanceResult]]:
    """Статті з відбору Андрія, на які ще немає драфту. Порожньо — прогін нічого не пише."""
    if not comms.enabled():
        log.warning("COMMS_DATABASE_URL не задано — статей немає")
        return []
    drafted = pg.drafted_article_ids() if pg.enabled() else set()
    return comms.fetch_picks(limit, drafted)


async def step_write_notion(run_id: str, database_id: str, r: DraftResult) -> str:
    """Upsert у Notion: якщо для (article_id, run_id) рядок уже є — оновлюємо, інакше створюємо."""
    assert r.draft
    props = notion.build_properties(
        draft=r.draft.headline,
        source=f"{r.article.title} — {r.article.url}",
        status=STATUS,
    )
    body = (
        f"run_id: {run_id} · model: {r.draft.model_version}\n\n"
        f"{r.draft.text}"
    )
    existing = db.find_draft_page(r.article.id, run_id)
    if existing:
        await notion.update_row(existing, props)
        return existing
    page = await notion.create_row(database_id, props, body=body)
    return page["id"]


async def run_pipeline(run_id: str, *, database_id: str, limit: int = 1, model: ModelClient | None = None) -> RunSummary:
    model = model or get_model_client()
    t0 = time.monotonic()
    db.create_run(run_id, model.version)
    picks: list[tuple[Article, RelevanceResult]] = []
    written = 0
    failure: str | None = None
    try:
        picks = step_select(limit)
        for a, rel in picks:
            res = DraftResult(article=a, relevance=rel)
            # модель на RunPod відповідає хвилинами — в окремому потоці, щоб не блокувати /health і вебхуки
            res.draft = await asyncio.to_thread(model.draft, a)
            res.notion_page_id = await step_write_notion(run_id, database_id, res)
            if pg.enabled():
                pg.save_draft(run_id, res, STATUS)
            written += 1
            db.upsert_draft(
                run_id=run_id, article_id=a.id, notion_page_id=res.notion_page_id,
                relevance=rel.relevant, reason=rel.reason, score=rel.score, extraction=None,
                headline=res.draft.headline, draft_text=res.draft.text, failure_type=None,
                model_version=model.version,
            )
        status = "ok"
    except Exception as e:
        log.exception("run %s failed", run_id)
        status, failure = "failed", type(e).__name__
    duration = int((time.monotonic() - t0) * 1000)
    db.finish_run(run_id, status=status, duration_ms=duration, articles_in=len(picks),
                  relevant_count=len(picks), drafts_written=written, failure_type=failure)
    return RunSummary(run_id=run_id, status=status, started_at="", duration_ms=duration, articles_in=len(picks),
                      relevant_count=len(picks), drafts_written=written, failure_type=failure, model_version=model.version)
