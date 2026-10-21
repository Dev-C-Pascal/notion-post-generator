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
