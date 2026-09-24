"""Окрема Postgres-база драфтів (сервіс `db` у нашому docker-compose, база `postgen`).

Вмикається змінною DRAFTS_DATABASE_URL. Без неї (локально, у тестах) бекенд працює як раніше.
Кожен драфт, що пішов у Notion, додатково лягає сюди — upsert по (run_id, article_id).
draft_versions — історія драфту: v1 = те, що згенерував бекенд, далі кожна правка людини в Notion
(її підтягує sync.py). sync_state — водяний знак синку (last_edited_time останньої обробленої сторінки).
used_articles — використані статті: жодну не беремо двічі.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

import psycopg

from .models import Article, DraftResult

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
CREATE TABLE IF NOT EXISTS draft_versions (
    id               BIGSERIAL PRIMARY KEY,
    notion_page_id   TEXT NOT NULL,
    version          INTEGER NOT NULL,          -- 1 = згенерований бекендом, далі правки в Notion
    headline         TEXT NOT NULL,
    text             TEXT NOT NULL,
    status           TEXT,
    content_hash     TEXT NOT NULL,
    edited_by        TEXT,                      -- Notion user id (last_edited_by); NULL для v1
    notion_edited_at TIMESTAMPTZ,
    captured_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (notion_page_id, version)
);
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS used_articles (
    article_id    TEXT PRIMARY KEY,
    article_url   TEXT NOT NULL UNIQUE,
    article_title TEXT NOT NULL,
    run_id        TEXT NOT NULL,                -- прогін, що взяв статтю
    channel       TEXT,                         -- fb | x | NULL (кнопка без каналу)
    claimed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- статті, на які драфт уже є, теж використані (зокрема ті, що були до появи used_articles)
INSERT INTO used_articles (article_id, article_url, article_title, run_id, claimed_at)
SELECT DISTINCT ON (article_id) article_id, article_url, article_title, run_id, created_at
FROM drafts ORDER BY article_id, created_at
ON CONFLICT DO NOTHING;
"""


def enabled() -> bool:
    return bool(os.environ.get("DRAFTS_DATABASE_URL"))


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DRAFTS_DATABASE_URL"], connect_timeout=5)


def init_db() -> None:
    with _connect() as c:
        c.execute(SCHEMA)


def normalize(text: str) -> str:
    """Абзаци без зайвих пробілів і порожніх рядків — так текст виглядає після кола через Notion."""
    return "\n\n".join(p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip())


def content_hash(headline: str, text: str, status: str | None) -> str:
    payload = [headline.strip(), normalize(text), status]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def save_draft(run_id: str, r: DraftResult, status: str) -> None:
    """Драфт + його перша версія (те, що бекенд відправив у Notion).
    extraction / quality_score / failure_type лишаються NULL: цих кроків більше немає, оцінює людина."""
    assert r.draft
    with _connect() as c:
        c.execute(
            """INSERT INTO drafts (run_id, article_id, article_url, article_title, relevant, relevance_score, reason,
                                   headline, text, model_version, notion_page_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, article_id) DO UPDATE SET
                 headline = excluded.headline, text = excluded.text, notion_page_id = excluded.notion_page_id""",
            (run_id, str(r.article.id), r.article.url, r.article.title, r.relevance.relevant, r.relevance.score,
             r.relevance.reason, r.draft.headline, r.draft.text, r.draft.model_version, r.notion_page_id),
        )
        if r.notion_page_id:
            _add_version(c, r.notion_page_id, r.draft.headline, r.draft.text, status, None, None)


def _add_version(c: psycopg.Connection, page_id: str, headline: str, text: str, status: str | None,
                 edited_by: str | None, edited_at: datetime | None) -> bool:
    """Нова версія, якщо зміст відрізняється від останньої. Повертає True, якщо записали."""
    h = content_hash(headline, text, status)
    last = c.execute(
        "SELECT version, content_hash FROM draft_versions WHERE notion_page_id = %s ORDER BY version DESC LIMIT 1",
        (page_id,),
    ).fetchone()
    if last and last[1] == h:
        return False
    c.execute(
        """INSERT INTO draft_versions (notion_page_id, version, headline, text, status, content_hash, edited_by,
                                       notion_edited_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (page_id, (last[0] if last else 0) + 1, headline, text, status, h, edited_by, edited_at),
    )
    return True


def add_version(page_id: str, headline: str, text: str, status: str | None, edited_by: str | None,
                edited_at: datetime | None) -> bool:
    with _connect() as c:
        return _add_version(c, page_id, headline, text, status, edited_by, edited_at)


def used_article_ids() -> set[str]:
    """Статті, які вже взяв якийсь прогін, — на них драфт є або пишеться просто зараз."""
    with _connect() as c:
        return {r[0] for r in c.execute("SELECT article_id FROM used_articles")}


def claim_article(article: Article, run_id: str, channel: str | None) -> bool:
    """Забронювати статтю до генерації: модель пише хвилинами, а кнопку тиснуть кілька разів поспіль.
    Атомарно (unique на article_id і url): False — статтю вже взяв інший прогін."""
    with _connect() as c:
        row = c.execute(
            """INSERT INTO used_articles (article_id, article_url, article_title, run_id, channel)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING article_id""",
            (str(article.id), article.url, article.title, run_id, channel),
        ).fetchone()
    return row is not None


def release_article(article_id: str, run_id: str) -> None:
    """Драфт не дійшов до Notion (впала модель чи Notion) — стаття знову вільна для наступного прогону."""
    with _connect() as c:
        c.execute("DELETE FROM used_articles WHERE article_id = %s AND run_id = %s", (article_id, run_id))


def known_pages() -> set[str]:
    """Сторінки Notion, які створив бекенд (тільки їх і синхронізуємо)."""
    with _connect() as c:
        return {r[0] for r in c.execute("SELECT notion_page_id FROM drafts WHERE notion_page_id IS NOT NULL")}


def get_state(key: str) -> str | None:
    with _connect() as c:
        r = c.execute("SELECT value FROM sync_state WHERE key = %s", (key,)).fetchone()
    return r[0] if r else None


def set_state(key: str, value: str) -> None:
    with _connect() as c:
        c.execute("INSERT INTO sync_state (key, value) VALUES (%s, %s) "
                  "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
