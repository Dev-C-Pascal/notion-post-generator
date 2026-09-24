"""Статті з бази Андрія `comms` (стек /opt/comms на тому ж сервері, контейнер deploy-db-1).

Стаття для драфту — з топу дня його векторного відбору (bge-m3 + pgvector, задача rank_daily → ml.daily_pick):
останній зріз топу за рангом, лише з повним текстом і лише ті, на які ми ще не писали драфт.
Вмикається змінною COMMS_DATABASE_URL. Тільки SELECT — у його базу ми нічого не пишемо.
"""
from __future__ import annotations

import os

import psycopg

from .models import Article, RelevanceResult

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
      AND a.retrieval_status = 'full_text' AND a.body_text IS NOT NULL
      AND a.article_id::text <> ALL(%s::text[])
    ORDER BY p.rank
    LIMIT %s
"""


def enabled() -> bool:
    return bool(os.environ.get("COMMS_DATABASE_URL"))


def fetch_top_picks(limit: int, exclude: set[str]) -> list[tuple[Article, RelevanceResult]]:
    """Найвищі в топі дня статті з повним текстом, крім exclude (на них драфт уже є)."""
    with psycopg.connect(os.environ["COMMS_DATABASE_URL"], connect_timeout=5) as c:
        rows = c.execute(TOP_PICKS, (sorted(exclude), limit)).fetchall()
    picks = []
    for aid, url, title, text, source, published, rank, score, topic, event_size in rows:
        article = Article(id=aid, url=url, title=title, text=text, source=source, published_at=published, topic=topic)
        reason = f"топ дня №{rank}, тема «{topic}», бал {score:.3f}, видань про подію: {event_size}"
        picks.append((article, RelevanceResult(article_id=aid, relevant=True, reason=reason, score=float(score))))
    return picks
