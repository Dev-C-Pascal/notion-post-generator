"""Дві бази даних (SQLite для прототипу; на проді — Postgres з тією ж схемою).

articles.db  — велика база статей (джерело).
posts.db     — журнал генерацій: який Notion-запис, яку статтю обрали, який пост написали.
Зв'язок між ними — posts.article_id -> articles.id.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
ARTICLES_DB = DATA_DIR / "articles.db"
POSTS_DB = DATA_DIR / "posts.db"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect(ARTICLES_DB) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL,
                url        TEXT,
                source     TEXT,
                topic      TEXT,
                body       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
    with _connect(POSTS_DB) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                notion_page_id TEXT NOT NULL,
                article_id     INTEGER NOT NULL,      -- FK -> articles.id (в іншій БД)
                topic          TEXT,
                post_text      TEXT NOT NULL,
                model          TEXT NOT NULL,
                status         TEXT NOT NULL,         -- ok | error
                error          TEXT,
                created_at     TEXT NOT NULL
            )
        """)


def seed_articles_if_empty() -> None:
    with _connect(ARTICLES_DB) as c:
        n = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        if n:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            ("КШЕ відкриває нову магістерську програму з AI", "https://kse.ua/ai-master", "kse.ua", "освіта",
             "Київська школа економіки запускає магістерську програму зі штучного інтелекту. Набір триває до жовтня."),
            ("Як українські стартапи залучили $1 млрд у 2025", "https://example.com/startups-2025", "AIN", "бізнес",
             "У 2025 році українські стартапи залучили понад мільярд доларів інвестицій, попри війну."),
            ("Відновлення енергетики: що зроблено за рік", "https://example.com/energy", "Економічна правда",
             "енергетика",
             "Україна відновила 60% пошкоджених потужностей і будує децентралізовану генерацію."),
            ("MLOps у продакшені: досвід банків", "https://example.com/mlops-banks", "DOU", "технології",
             "Українські банки впроваджують MLOps-пайплайни для скорингу та антифроду."),
        ]
        c.executemany(
            "INSERT INTO articles (title, url, source, topic, body, created_at) VALUES (?,?,?,?,?,?)",
            [(*r, now) for r in rows],
        )


def list_articles() -> list[sqlite3.Row]:
    with _connect(ARTICLES_DB) as c:
        return c.execute("SELECT * FROM articles ORDER BY id").fetchall()


def get_article(article_id: int) -> sqlite3.Row | None:
    with _connect(ARTICLES_DB) as c:
        return c.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()


def log_post(notion_page_id: str, article_id: int, topic: str | None, post_text: str,
             model: str, status: str = "ok", error: str | None = None) -> int:
    with _connect(POSTS_DB) as c:
        cur = c.execute(
            "INSERT INTO posts (notion_page_id, article_id, topic, post_text, model, status, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (notion_page_id, article_id, topic, post_text, model, status, error,
             datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def count_posts() -> int:
    with _connect(POSTS_DB) as c:
        return c.execute("SELECT COUNT(*) FROM posts WHERE status = 'ok'").fetchone()[0]


def list_posts(limit: int = 50) -> list[dict]:
    with _connect(POSTS_DB) as c:
        rows = c.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    # "поєднуємо" дві бази: підтягуємо назву статті з articles.db
    out = []
    for r in rows:
        d = dict(r)
        a = get_article(r["article_id"])
        d["article_title"] = a["title"] if a else None
        out.append(d)
    return out
