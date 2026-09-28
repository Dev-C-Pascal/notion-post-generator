"""Статті з бази Андрія `comms` (стек /opt/comms на тому ж сервері, контейнер deploy-db-1).

Стаття для драфту — з його векторного відбору (bge-m3 + pgvector), лише з повним текстом і лише ті,
на які ми ще не писали драфт:
  1. топ дня (rank_daily → ml.daily_pick): останній зріз, за рангом — лише якщо зріз свіжий (rank_daily щогодини;
     старий зріз означає, що відбір стоїть, і його «топ дня» — новини позавчора);
  2. якщо там порожньо — тематичний відбір (score_topics → marts.topic_queue): above_threshold, за балом,
     лише статті за останню добу (вікно самої черги — 3 дні, без фільтра брались новини дво-, триденної давності).
Вмикається змінною COMMS_DATABASE_URL. Тільки SELECT — у його базу ми нічого не пишемо.
"""
from __future__ import annotations

import os

import psycopg

from .models import Article, RelevanceResult

TOP_MAX_AGE_HOURS = 3  # зріз топу дня старший за це — rank_daily не працює, беремо тематичний відбір
FRESH_HOURS = 24  # стаття для тематичного відбору не старша за добу (як fresh_hours у ранкері Андрія)

TOP_PICKS = """
    SELECT a.article_id::text, a.url_canonical, coalesce(a.title, ''), a.body_text,
           coalesce(s.name, s.domain, 'unknown'), coalesce(a.published_at::text, ''),
           p.rank, p.score, coalesce(t.name_uk, p.topic_code, '—'), p.event_size
    FROM ml.daily_pick p
    JOIN ops.candidate_pool c USING (candidate_id)
    JOIN core.article a ON a.article_id = c.article_id
    LEFT JOIN core.source s ON s.source_id = a.source_id
    LEFT JOIN core.topic t ON t.topic_code = p.topic_code
    WHERE p.computed_at = (SELECT max(computed_at) FROM ml.daily_pick)
      AND p.computed_at > now() - make_interval(hours => %s)
      AND a.retrieval_status = 'full_text' AND coalesce(a.body_text, '') <> ''
      AND a.article_id::text <> ALL(%s::text[])
    ORDER BY p.rank
    LIMIT %s
"""

# topic_queue дає рядок на кожен кандидат пулу, тож одна стаття може трапитись кілька разів — DISTINCT ON
TOPIC_PICKS = """
    SELECT id, url, title, body, source, published, topic, score FROM (
        SELECT DISTINCT ON (q.article_id)
               a.article_id::text AS id, a.url_canonical AS url, coalesce(a.title, '') AS title, a.body_text AS body,
               coalesce(s.name, s.domain, 'unknown') AS source, coalesce(a.published_at::text, '') AS published,
               q.topic, q.score
        FROM marts.topic_queue q
        JOIN core.article a USING (article_id)
        LEFT JOIN core.source s ON s.source_id = a.source_id
        WHERE q.above_threshold AND a.retrieval_status = 'full_text' AND coalesce(a.body_text, '') <> ''
          AND coalesce(a.published_at, a.ingested_at) >= now() - make_interval(hours => %s)
          AND a.article_id::text <> ALL(%s::text[])
        ORDER BY q.article_id, q.score DESC
    ) x
    ORDER BY score DESC
    LIMIT %s
"""


def enabled() -> bool:
    return bool(os.environ.get("COMMS_DATABASE_URL"))


def _query(sql: str, params: tuple) -> list[tuple]:
    with psycopg.connect(os.environ["COMMS_DATABASE_URL"], connect_timeout=5) as c:
        return c.execute(sql, params).fetchall()


def fetch_picks(limit: int, exclude: set[str]) -> list[tuple[Article, RelevanceResult]]:
    """Статті для драфту, крім exclude (на них драфт уже є): спершу топ дня, решта — з тематичного відбору."""
    picks = []
    for aid, url, title, text, source, published, rank, score, topic, event_size in _query(
            TOP_PICKS, (TOP_MAX_AGE_HOURS, sorted(exclude), limit)):
        reason = f"топ дня №{rank}, тема «{topic}», бал {score:.3f}, видань про подію: {event_size}"
        picks.append(_pick(aid, url, title, text, source, published, topic, score, reason))
    if len(picks) < limit:
        taken = exclude | {str(a.id) for a, _ in picks}
        for aid, url, title, text, source, published, topic, score in _query(
                TOPIC_PICKS, (FRESH_HOURS, sorted(taken), limit - len(picks))):
            reason = f"тематичний відбір (у топі дня вільних статей немає): тема «{topic}», бал {score:.3f}"
            picks.append(_pick(aid, url, title, text, source, published, topic, score, reason))
    return picks


def _pick(aid: str, url: str, title: str, text: str, source: str, published: str, topic: str, score: float,
          reason: str) -> tuple[Article, RelevanceResult]:
    article = Article(id=aid, url=url, title=title, text=text, source=source, published_at=published, topic=topic)
    return article, RelevanceResult(article_id=aid, relevant=True, reason=reason, score=float(score))
