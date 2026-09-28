-- articles.db

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

-- runs.db

CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    status         TEXT NOT NULL,             -- running | ok | failed
    started_at     TEXT NOT NULL,
    duration_ms    INTEGER,
    articles_in    INTEGER NOT NULL DEFAULT 0,
    relevant_count INTEGER NOT NULL DEFAULT 0,
    drafts_written INTEGER NOT NULL DEFAULT 0,
    failure_type   TEXT,
    model_version  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    article_id     INTEGER NOT NULL,          -- FK -> articles.id (в іншій БД)
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
