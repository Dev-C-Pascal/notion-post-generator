"""Читання статей із бази Андрія `comms` (стек /opt/comms на тому ж сервері, контейнер deploy-db-1).

Вмикається змінною COMMS_DATABASE_URL. Тільки SELECT — у його базу ми нічого не пишемо.
Без змінної (локально, у тестах) pipeline бере статті з SQLite, як раніше.
"""
from __future__ import annotations

import os

import psycopg

from .models import Article

LATEST_ARTICLE = """
    SELECT a.article_id::text, a.url_canonical, coalesce(a.title, ''), a.body_text,
           coalesce(s.name, s.domain, 'unknown'), coalesce(a.published_at::text, '')
    FROM core.article a
    LEFT JOIN core.source s USING (source_id)
    WHERE a.retrieval_status = 'full_text' AND a.body_text IS NOT NULL
    ORDER BY a.published_at DESC NULLS LAST
    LIMIT 1
"""


def enabled() -> bool:
    return bool(os.environ.get("COMMS_DATABASE_URL"))


def fetch_latest_article() -> Article | None:
    """Найсвіжіша (за published_at) стаття з повним текстом."""
    with psycopg.connect(os.environ["COMMS_DATABASE_URL"], connect_timeout=5) as c:
        r = c.execute(LATEST_ARTICLE).fetchone()
    if not r:
        return None
    return Article(id=r[0], url=r[1], title=r[2], text=r[3], source=r[4], published_at=r[5])
