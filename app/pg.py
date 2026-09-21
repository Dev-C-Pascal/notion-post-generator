"""Окрема Postgres-база драфтів (сервіс `db` у нашому docker-compose, база `postgen`).

Вмикається змінною DRAFTS_DATABASE_URL. Без неї (локально, у тестах) бекенд працює як раніше.
Кожен драфт, що пішов у Notion, додатково лягає сюди — upsert по (run_id, article_id).
"""
from __future__ import annotations

import os

import psycopg

from .models import DraftResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL,
    article_id      TEXT NOT NULL,
    article_url     TEXT NOT NULL,
    article_title   TEXT NOT NULL,
    relevant        BOOLEAN NOT NULL,
    relevance_score REAL,
    reason          TEXT,
    extraction      JSONB,
    headline        TEXT NOT NULL,
    text            TEXT NOT NULL,
    quality_score   REAL,
    failure_type    TEXT,
    model_version   TEXT NOT NULL,
    notion_page_id  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, article_id)
);
"""


def enabled() -> bool:
    return bool(os.environ.get("DRAFTS_DATABASE_URL"))


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DRAFTS_DATABASE_URL"], connect_timeout=5)


def init_db() -> None:
    with _connect() as c:
        c.execute(SCHEMA)


def save_draft(run_id: str, r: DraftResult) -> None:
    assert r.draft
    with _connect() as c:
        c.execute(
            """INSERT INTO drafts (run_id, article_id, article_url, article_title, relevant, relevance_score, reason,
                                   extraction, headline, text, quality_score, failure_type, model_version,
                                   notion_page_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, article_id) DO UPDATE SET
                 headline = excluded.headline, text = excluded.text, quality_score = excluded.quality_score,
                 failure_type = excluded.failure_type, notion_page_id = excluded.notion_page_id""",
            (run_id, str(r.article.id), r.article.url, r.article.title, r.relevance.relevant, r.relevance.score,
             r.relevance.reason, r.extraction.model_dump_json() if r.extraction else None,
             r.draft.headline, r.draft.text,
             r.evaluation.quality_score if r.evaluation else None,
             r.evaluation.failure_type if r.evaluation else None,
             r.draft.model_version, r.notion_page_id),
        )
