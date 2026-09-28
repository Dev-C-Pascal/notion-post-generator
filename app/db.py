"""Дві бази даних. Прототип — SQLite (файли в data/), прод — Postgres з тією ж схемою.

articles.db  — база статей (схема узгоджена з Андрієм). Порожня: наповнює Data Architect.
runs.db      — журнал прогонів (runs) і драфтів (drafts) з ключем article_id + run_id для upsert.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Article

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
ARTICLES_DB = DATA_DIR / "articles.db"
RUNS_DB = DATA_DIR / "runs.db"

ARTICLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    text           TEXT NOT NULL,
    source         TEXT NOT NULL,
    published_at   TEXT NOT NULL,            -- ISO-8601
    processed_flag INTEGER NOT NULL DEFAULT 0, -- 0/1: чи вже пройшла pipeline
    topic          TEXT                        -- необов'язково, підказка для relevance
);
"""

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    status         TEXT NOT NULL,             -- running | ok | failed
    started_at     TEXT NOT NULL,
    duration_ms    INTEGER,
    articles_in    INTEGER NOT NULL DEFAULT 0,
    relevant_count INTEGER NOT NULL DEFAULT 0,
    drafts_written INTEGER NOT NULL DEFAULT 0,
    failure_type   TEXT,
    model_version  TEXT NOT NULL,
    failure_step   TEXT,                      -- select | model | notion | store — де впав прогін
    failure_detail TEXT                       -- стаття і текст помилки (до 500 символів)
);
CREATE TABLE IF NOT EXISTS drafts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    article_id     INTEGER NOT NULL,          -- articles.id (SQLite) або uuid із comms.core.article
    notion_page_id TEXT,
    relevance      INTEGER NOT NULL,          -- 0/1
    reason         TEXT,
    score          REAL,
    extraction     TEXT,                      -- JSON
    headline       TEXT,
    draft_text     TEXT,
    failure_type   TEXT,
    model_version  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (article_id, run_id)               -- ключ upsert: повторний запуск не дублює
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _connect(ARTICLES_DB) as c:
        c.executescript(ARTICLES_SCHEMA)
    with _connect(RUNS_DB) as c:
        c.executescript(RUNS_SCHEMA)
        # колонки, додані 2026-09-28: у старій runs.db на сервері їх ще немає
        cols = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
        for col in ("failure_step", "failure_detail"):
            if col not in cols:
                c.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")


# ---------- articles ----------

def fetch_articles(limit: int = 10, only_unprocessed: bool = True) -> list[Article]:
    q = "SELECT * FROM articles" + (" WHERE processed_flag = 0" if only_unprocessed else "") + " ORDER BY id LIMIT ?"
    with _connect(ARTICLES_DB) as c:
        rows = c.execute(q, (limit,)).fetchall()
    return [Article(**{**dict(r), "processed_flag": bool(r["processed_flag"])}) for r in rows]


def count_articles() -> int:
    with _connect(ARTICLES_DB) as c:
        return c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]


def mark_processed(article_id: int) -> None:
    with _connect(ARTICLES_DB) as c:
        c.execute("UPDATE articles SET processed_flag = 1 WHERE id = ?", (article_id,))


# ---------- runs ----------

def create_run(run_id: str, model_version: str) -> None:
    with _connect(RUNS_DB) as c:
        c.execute("INSERT INTO runs (run_id, status, started_at, model_version) VALUES (?,?,?,?) "
                  "ON CONFLICT(run_id) DO UPDATE SET status='running', started_at=excluded.started_at",
                  (run_id, "running", _now(), model_version))


def finish_run(run_id: str, *, status: str, duration_ms: int, articles_in: int, relevant_count: int,
               drafts_written: int, failure_type: str | None, failure_step: str | None = None,
               failure_detail: str | None = None) -> None:
    with _connect(RUNS_DB) as c:
        c.execute(
            "UPDATE runs SET status=?, duration_ms=?, articles_in=?, relevant_count=?, drafts_written=?, failure_type=?, "
            "failure_step=?, failure_detail=? WHERE run_id=?",
            (status, duration_ms, articles_in, relevant_count, drafts_written, failure_type, failure_step,
             failure_detail, run_id),
        )


def get_run(run_id: str) -> dict | None:
    with _connect(RUNS_DB) as c:
        r = c.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not r:
            return None
        drafts = c.execute("SELECT * FROM drafts WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return {**dict(r), "drafts": [dict(d) for d in drafts]}


def list_runs(limit: int = 50) -> list[dict]:
    with _connect(RUNS_DB) as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))]


def count_drafts() -> int:
    with _connect(RUNS_DB) as c:
        return c.execute("SELECT COUNT(*) FROM drafts WHERE notion_page_id IS NOT NULL").fetchone()[0]


# ---------- drafts (upsert по article_id + run_id) ----------

def find_draft_page(article_id: int | str, run_id: str) -> str | None:
    with _connect(RUNS_DB) as c:
        r = c.execute("SELECT notion_page_id FROM drafts WHERE article_id=? AND run_id=?", (article_id, run_id)).fetchone()
    return r["notion_page_id"] if r else None


def upsert_draft(*, run_id: str, article_id: int | str, notion_page_id: str | None, relevance: bool, reason: str,
                 score: float | None, extraction: str | None, headline: str | None, draft_text: str | None,
                 failure_type: str | None, model_version: str) -> None:
    with _connect(RUNS_DB) as c:
        c.execute(
            """INSERT INTO drafts (run_id, article_id, notion_page_id, relevance, reason, score, extraction, headline,
                                   draft_text, failure_type, model_version, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(article_id, run_id) DO UPDATE SET
                 notion_page_id=excluded.notion_page_id, relevance=excluded.relevance, reason=excluded.reason,
                 score=excluded.score, extraction=excluded.extraction, headline=excluded.headline,
                 draft_text=excluded.draft_text, failure_type=excluded.failure_type,
                 model_version=excluded.model_version""",
            (run_id, article_id, notion_page_id, int(relevance), reason, score, extraction, headline, draft_text,
             failure_type, model_version, _now()),
        )
