"""Pipeline одного прогону: select (топ дня Андрія) → draft (модель на RunPod) → write_notion + postgen.

Релевантність — векторний відбір Андрія (bge-m3 + pgvector): топ дня, а поки його немає — тематичний відбір.
Окремих кроків extraction / evaluate немає — оцінює людина в Notion: статус «New draft», колонки рецензента,
Score — формула в самій таблиці.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from . import comms, db, notion, pg, status
from .llm import GroundingRefused, ModelClient, get_model_client
from .models import Article, Draft, DraftResult, RelevanceResult, RunSummary

log = logging.getLogger("pipeline")

STATUS = "New draft"  # новий драфт чекає на рецензента; далі статуси ставить людина
# draft_grounded повернув needs_manual_review: пост не пройшов перевірку фактів — у таблицю з наявним статусом
REVIEW_STATUS = "Needs fact-check"
REVIEW_TITLE = "⚠ Не пройшов перевірку: "
REVIEW_WARNING = ("⚠ НЕ ПЕРЕВІРЕНО. Модель не змогла підтвердити всі факти цього поста навіть після повторної спроби. "
                  "Не публікувати без фактчеку — речення, що не пройшли перевірку, і причини — у блоці нижче.")


def draft_status(d: Draft) -> str:
    return REVIEW_STATUS if d.needs_review else STATUS
# канал кнопки → lang для моделі Артема: за мовою вона обирає і формат (uk — пост для FB, en — тред для X)
CHANNEL_LANG = {"fb": "uk", "x": "en"}
PICK_SPARE = 5  # скільки зайвих кандидатів брати на випадок, коли їх розбирають паралельні прогони
# Модель відповідає хвилинами (runsync + опитування з time.sleep). У спільному пулі asyncio.to_thread (6 потоків
# на 2 CPU) вона займала всі потоки, а DNS-запит до Notion чекав у тій самій черзі → ConnectTimeout уже після
# готової генерації. Тому окремий пул: не більше MAX_PARALLEL_DRAFTS генерацій разом, решта чекає своєї черги
# (таймаут моделі рахується з моменту, коли генерація справді почалась).
MAX_PARALLEL_DRAFTS = int(os.environ.get("MAX_PARALLEL_DRAFTS", "3"))
_MODEL_POOL = ThreadPoolExecutor(max_workers=MAX_PARALLEL_DRAFTS, thread_name_prefix="model")
_in_pool = 0  # скільки прогонів зараз у пулі моделі (генерують або чекають) — для «у черзі» в рядку статусу
# mode=draft_grounded може відмовитись від статті (немає фактів, підтверджених цитатою) — тоді в тому ж прогоні
# беремо наступну, але не більше MAX_REFUSALS разів: кожна спроба — це виклик моделі
MAX_REFUSALS = 2


def _draft(model: ModelClient, article: Article, lang: str | None, on_start: Callable[[], object]) -> Draft:
    """У потоці пулу: повідомити, що генерація справді почалась (черга позаду), і генерувати."""
    on_start()
    return model.draft(article, lang)


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
    review = r.draft.needs_review
    props = notion.build_properties(
        draft=(REVIEW_TITLE if review else "") + r.draft.headline,
        source=f"{r.article.title} — {r.article.url}",
        status=draft_status(r.draft),
    )
    body = (
        f"run_id: {run_id} · model: {r.draft.model_version} · канал: {channel or 'auto'} · мова: {r.draft.lang}\n\n"
        + (f"{REVIEW_WARNING}\n\n" if review else "")
        + r.draft.text
    )
    existing = db.find_draft_page(r.article.id, run_id)
    if existing:
        await notion.update_row(existing, props)
        return existing
    # draft_grounded: під постом — факти з цитатами зі статті, щоб рецензент перевіряв Fact safety за хвилину
    facts = []
    if review:
        failed = _failure_lines(r.draft.review.get("verification"))
        facts.append(notion.toggle(f"Що не пройшло перевірку ({len(failed)})",
                                   failed or ["модель не вказала, які саме речення — див. draft_reasoning у postgen"]))
    if r.draft.facts_used:
        facts.append(notion.toggle(f"Факти, з яких написано пост ({len(r.draft.facts_used)}) — кожен підтверджено "
                                   "цитатою зі статті", [_fact_line(f) for f in r.draft.facts_used]))
    if r.draft.facts_rejected:
        facts.append(notion.toggle(f"Відкинуті факти ({len(r.draft.facts_rejected)}) — цитату в статті не знайдено, "
                                   "у пост не пішли", [_fact_line(f) for f in r.draft.facts_rejected]))
    page = await notion.create_row(database_id, props, body=body, extra_blocks=facts)
    return page["id"]


def _fact_line(f: dict) -> str:
    claim, quote = f.get("claim"), f.get("verbatim_quote")
    return f"{claim} — «{quote}»" if claim and quote else str(claim or quote or f)


def _failure_lines(verification: object) -> list[str]:
    """verification.deterministic_failures + judge_failures → «речення» → «фрагмент» — ВЕРДИКТ: причина.
    Пункт судді (перевірено 29.09): {"text", "offending_span", "verdict", "reason", "sentence_id",
    "matched_fact_indices"}; інші назви полів теж приймаємо, а незнайоме показуємо як JSON — рецензент побачить усе."""
    if not isinstance(verification, dict):
        return []
    lines = []
    if verification.get("judge_output_invalid"):  # суддя відповів непарсабельно або процитував те, чого нема в пості
        lines.append("Суддя повернув невалідну відповідь — його вердикту не довіряємо: перевірте весь текст")
    for key in ("deterministic_failures", "judge_failures"):
        for f in verification.get(key) or []:
            if not isinstance(f, dict):
                lines.append(str(f))
                continue
            text = next((f[k] for k in ("text", "sentence", "offending_text", "claim") if f.get(k)), "")
            reason = next((f[k] for k in ("reason", "why", "explanation", "issue") if f.get(k)), "")
            if not (text and reason):
                lines.append(json.dumps(f, ensure_ascii=False))
                continue
            span, verdict = f.get("offending_span"), f.get("verdict")
            head = f"«{text}»" + (f" → «{span}»" if span and span != text else "")
            lines.append(f"{head} — " + (f"{verdict}: " if verdict else "") + reason)
    return lines


async def run_pipeline(run_id: str, *, database_id: str, limit: int = 1, channel: str | None = None,
                       model: ModelClient | None = None) -> RunSummary:
    """channel — fb | x | None (мова поста = мова статті)."""
    global _in_pool
    model = model or get_model_client()
    lang = CHANNEL_LANG[channel] if channel else None
    t0 = time.monotonic()
    started = status.now()  # час натискання — ним рядок прогону підписаний у блоці статусу
    status.report(run_id, channel, started, "шукаю статтю…")
    db.create_run(run_id, model.version)
    picks: list[tuple[Article, RelevanceResult]] = []
    in_notion: set[str] = set()  # статті, чий драфт уже в Notion: бронь із них не знімаємо
    refused: set[str] = set()  # статті, від яких модель відмовилась: бронь теж лишаємо — вдруге брати марно
    written = 0
    failure: str | None = None
    step, current = "select", None  # для діагностики: на якому кроці і з якою статтею впав прогін
    failure_step: str | None = None
    failure_detail: str | None = None
    loop = asyncio.get_running_loop()
    try:
        picks = step_select(run_id, limit, channel)
        if not picks:
            status.report(run_id, channel, started, "немає свіжих статей — спробуйте пізніше")
        queue = list(picks)
        while queue:
            a, rel = queue.pop(0)
            current = a
            res = DraftResult(article=a, relevance=rel)
            step = "model"
            if _in_pool >= MAX_PARALLEL_DRAFTS:
                status.report(run_id, channel, started, "у черзі", title=a.title,
                              note=f"(вже йдуть {MAX_PARALLEL_DRAFTS} генерації)")
            on_start = functools.partial(loop.call_soon_threadsafe, functools.partial(
                status.report, run_id, channel, started, "генерується", title=a.title, note="(зазвичай 3–10 хв)"))
            # модель на RunPod відповідає хвилинами — у власному пулі, щоб не блокувати вебхуки й запити до Notion
            _in_pool += 1
            try:
                draft = await loop.run_in_executor(_MODEL_POOL, _draft, model, a, lang, on_start)
            except GroundingRefused as e:
                refused.add(str(a.id))
                log.warning("run %s: модель відмовилась від статті %s (%s): %s", run_id, a.id, a.url, e)
                if len(refused) > MAX_REFUSALS:
                    raise
                more = step_select(run_id, 1, channel)  # замість неї — наступна стаття в тому ж прогоні
                status.report(run_id, channel, started, "модель відмовилась", title=a.title,
                              note="(немає фактів, які вона змогла підтвердити) — "
                                   + ("беру наступну статтю" if more else "інших свіжих статей немає"))
                picks += more
                queue += more
                continue
            finally:
                _in_pool -= 1
            res.draft = draft
            step = "notion"
            res.notion_page_id = await step_write_notion(run_id, database_id, res, channel)
            in_notion.add(str(a.id))
            if draft.needs_review:
                status.report(run_id, channel, started, "не пройшов перевірку фактів", title=a.title,
                              url=status.page_url(res.notion_page_id), note=f"→ у таблиці як {REVIEW_STATUS}")
            else:
                status.report(run_id, channel, started, "готово", title=a.title,
                              url=status.page_url(res.notion_page_id), note="→ у таблиці MVP")
            step = "store"
            if pg.enabled():
                pg.save_draft(run_id, res, draft_status(draft))
            written += 1
            db.upsert_draft(
                run_id=run_id, article_id=a.id, notion_page_id=res.notion_page_id,
                relevance=rel.relevant, reason=rel.reason, score=rel.score, extraction=None,
                headline=draft.headline, draft_text=draft.text, failure_type=None,
                model_version=model.version,
            )
        if refused and not written:  # модель відмовилась від усіх статей, а нових немає — драфту не буде
            raise GroundingRefused(f"модель відмовилась від {len(refused)} статей, інших свіжих немає")
        outcome = "ok"
    except Exception as e:
        log.exception("run %s failed at %s", run_id, step)
        outcome, failure, failure_step = "failed", type(e).__name__, step
        # текст помилки (для моделі — з id задачі RunPod), а не лише назва класу
        failure_detail = (f"article {current.id} ({current.url}): " if current else "") + str(e)
        failure_detail = failure_detail[:500]
        if pg.enabled():
            release_unwritten(run_id, picks, in_notion | refused)
        if isinstance(e, GroundingRefused):
            status.report(run_id, channel, started, "модель відмовилась", title=current.title if current else None,
                          note=f"від {len(refused)} статей поспіль (немає фактів, які вона змогла підтвердити) — "
                               "натисніть ще раз пізніше")
        elif not (current and str(current.id) in in_notion):  # драфт уже в таблиці — лишаємо «готово»
            status.report(run_id, channel, started, f"не вдалося ({step})", title=current.title if current else None,
                          note="— стаття знову в черзі, натисніть ще раз" if current else "— натисніть ще раз пізніше")
    duration = int((time.monotonic() - t0) * 1000)
    try:
        db.finish_run(run_id, status=outcome, duration_ms=duration, articles_in=len(picks),
                      relevant_count=len(picks), drafts_written=written, failure_type=failure,
                      failure_step=failure_step, failure_detail=failure_detail)
    except Exception:  # журнал не записався — прогін лишиться «running» у runs.db, але причина буде в логах
        log.exception("run %s: не вдалося записати підсумок у runs.db (status=%s)", run_id, outcome)
    return RunSummary(run_id=run_id, status=outcome, started_at="", duration_ms=duration, articles_in=len(picks),
                      relevant_count=len(picks), drafts_written=written, failure_type=failure, model_version=model.version)
