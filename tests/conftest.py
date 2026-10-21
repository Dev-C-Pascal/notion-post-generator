"""Спільне для тестів: платні й зовнішні речі вимкнені, справжній Postgres — лише коли заданий TEST_DATABASE_URL."""
import os
import uuid

import pytest


@pytest.fixture(autouse=True)
def _no_paid_judge(monkeypatch):
    """Оцінка Health Check платна: у тестах вимкнена, навіть якщо на машині розробника є ANTHROPIC_API_KEY."""
    monkeypatch.setenv("HEALTHCHECK_EVAL", "0")


@pytest.fixture
def pgdb(monkeypatch):
    """Порожня схема у справжньому Postgres (TEST_DATABASE_URL=postgresql://user@host:port/db), DRAFTS_DATABASE_URL
    вказує на неї. Без змінної тест пропускається: локально й у CI без бази лишаються тести з підмінами."""
    import psycopg

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL не задано — тест зі справжнім Postgres пропущено")
    schema = f"t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setenv("DRAFTS_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}options=-csearch_path%3D{schema}")
    yield
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


ARTICLES = [("a1", "fb"), ("a2", "fb"), ("x1", "x"), ("x2", "x")]


@pytest.fixture
def seed_run(pgdb):
    """Фабрика прогонів для тестів зі справжньою базою. Еталонні статті (a1, a2 — fb; x1, x2 — x) засіваються один раз;
    драфти прогону: a1 ok, a2 needs_manual_review, x1 refused, x2 error — оцінювати можна лише перші два."""
    import json

    from app import healthcheck, pg

    def seed(run_id="hc-1", *, started_at=None, status="done", texts=None, extraction=None,
             model="runpod-ep:draft_grounded"):
        texts = texts or {}
        healthcheck.init_db()
        with pg._connect() as c:
            for aid, ch in ARTICLES:
                c.execute("INSERT INTO hc_articles (article_id, channel, url, title, text, source, published_at) "
                          "VALUES (%s, %s, %s, %s, %s, 'Kyiv Post', '2026-10-01') ON CONFLICT DO NOTHING",
                          (aid, ch, f"https://x/{aid}", f"Title {aid}", f"Текст статті {aid}"))
            c.execute("INSERT INTO hc_runs (run_id, trigger, model_version, status, started_at) "
                      "VALUES (%s, 'manual', %s, %s, coalesce(%s::timestamptz, now()))", (run_id, model, status, started_at))
            for aid, outcome in (("a1", "ok"), ("a2", "needs_manual_review"), ("x1", "refused"), ("x2", "error")):
                has_text = outcome in ("ok", "needs_manual_review")
                c.execute("INSERT INTO hc_drafts (run_id, article_id, outcome, lang, text, extraction) "
                          "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                          (run_id, aid, outcome, "uk" if has_text else None,
                           texts.get(aid, f"Пост {aid}") if has_text else None,
                           json.dumps(extraction or {}) if has_text else None))

    return seed
