"""Pipeline одного прогону: select (топ дня Андрія) → draft (модель на RunPod) → write_notion + postgen.

Релевантність — векторний відбір Андрія (bge-m3 + pgvector): топ дня, а поки його немає — тематичний відбір.
Окремих кроків extraction / evaluate немає — оцінює людина в Notion: статус «New draft», колонки рецензента,
Score — формула в самій таблиці.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import comms, db, notion, pg
from .llm import ModelClient, get_model_client
from .models import Article, DraftResult, RelevanceResult, RunSummary

log = logging.getLogger("pipeline")

STATUS = "New draft"  # новий драфт чекає на рецензента; далі статуси ставить людина
# канал кнопки → lang для моделі Артема: за мовою вона обирає і формат (uk — пост для FB, en — тред для X)
CHANNEL_LANG = {"fb": "uk", "x": "en"}
PICK_SPARE = 5  # скільки зайвих кандидатів брати на випадок, коли їх розбирають паралельні прогони
# Модель відповідає хвилинами (runsync + опитування з time.sleep). У спільному пулі asyncio.to_thread (6 потоків
# на 2 CPU) вона займала всі потоки, а DNS-запит до Notion чекав у тій самій черзі → ConnectTimeout уже після
# готової генерації. Тому окремий пул: не більше MAX_PARALLEL_DRAFTS генерацій разом, решта чекає своєї черги
# (таймаут моделі рахується з моменту, коли генерація справді почалась).
MAX_PARALLEL_DRAFTS = int(os.environ.get("MAX_PARALLEL_DRAFTS", "3"))
_MODEL_POOL = ThreadPoolExecutor(max_workers=MAX_PARALLEL_DRAFTS, thread_name_prefix="model")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def step_select(run_id: str, limit: int, channel: str | None = None) -> list[tuple[Article, RelevanceResult]]:
    """Статті з відбору Андрія, яких ще не брав жоден прогін. Кожну бронюємо в used_articles до генерації,
    тож кілька натискань поспіль отримують різні статті. Порожньо — прогін нічого не пише."""
    if not comms.enabled():
        log.warning("COMMS_DATABASE_URL не задано — статей немає")
        return []
    if not pg.enabled():  # локально: без бази драфтів бронювати нема де
        return comms.fetch_picks(limit, set())
    picks: list[tuple[Article, RelevanceResult]] = []
    tried: set[str] = set()
    while len(picks) < limit:
        # із запасом: верх списку могли щойно розібрати паралельні прогони
        batch = comms.fetch_picks(limit - len(picks) + PICK_SPARE, pg.used_article_ids() | tried)
        if not batch:
            break
        for a, rel in batch:
            tried.add(str(a.id))
            if pg.claim_article(a, run_id, channel):
                picks.append((a, rel))
                if len(picks) == limit:
                    break
    return picks


def release_unwritten(run_id: str, picks: list[tuple[Article, RelevanceResult]], written: set[str]) -> None:
    """Прогін упав: статті, чий драфт не дійшов до Notion, знову вільні."""
    for a, _ in picks:
        if str(a.id) not in written:
            try:
                pg.release_article(str(a.id), run_id)
            except Exception:
                log.exception("run %s: не вдалося зняти бронь зі статті %s", run_id, a.id)


async def step_write_notion(run_id: str, database_id: str, r: DraftResult, channel: str | None = None) -> str:
    """Upsert у Notion: якщо для (article_id, run_id) рядок уже є — оновлюємо, інакше створюємо."""
    assert r.draft
    props = notion.build_properties(
        draft=r.draft.headline,
        source=f"{r.article.title} — {r.article.url}",
        status=STATUS,
    )
    body = (
        f"run_id: {run_id} · model: {r.draft.model_version} · канал: {channel or 'auto'} · мова: {r.draft.lang}\n\n"
        f"{r.draft.text}"
    )
    existing = db.find_draft_page(r.article.id, run_id)
    if existing:
        await notion.update_row(existing, props)
        return existing
    page = await notion.create_row(database_id, props, body=body)
    return page["id"]


async def run_pipeline(run_id: str, *, database_id: str, limit: int = 1, channel: str | None = None,
                       model: ModelClient | None = None) -> RunSummary:
    """channel — fb | x | None (мова поста = мова статті)."""
    model = model or get_model_client()
    lang = CHANNEL_LANG[channel] if channel else None
    t0 = time.monotonic()
    db.create_run(run_id, model.version)
    picks: list[tuple[Article, RelevanceResult]] = []
    in_notion: set[str] = set()  # статті, чий драфт уже в Notion: бронь із них не знімаємо
    written = 0
    failure: str | None = None
    try:
        picks = step_select(run_id, limit, channel)
        for a, rel in picks:
            res = DraftResult(article=a, relevance=rel)
            # модель на RunPod відповідає хвилинами — у власному пулі, щоб не блокувати вебхуки й запити до Notion
            res.draft = await asyncio.get_running_loop().run_in_executor(_MODEL_POOL, model.draft, a, lang)
            res.notion_page_id = await step_write_notion(run_id, database_id, res, channel)
            in_notion.add(str(a.id))
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
        if pg.enabled():
            release_unwritten(run_id, picks, in_notion)
    duration = int((time.monotonic() - t0) * 1000)
    db.finish_run(run_id, status=status, duration_ms=duration, articles_in=len(picks),
                  relevant_count=len(picks), drafts_written=written, failure_type=failure)
    return RunSummary(run_id=run_id, status=status, started_at="", duration_ms=duration, articles_in=len(picks),
                      relevant_count=len(picks), drafts_written=written, failure_type=failure, model_version=model.version)
