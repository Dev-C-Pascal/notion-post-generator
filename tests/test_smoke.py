"""Тести без мережі: unit на кроки моделі, e2e прогін з mock-Notion, API."""
import os

os.environ["NOTION_TOKEN"] = "secret_xxx"
os.environ["WEBHOOK_SECRET"] = "t"
os.environ["NOTION_DATABASE_ID"] = "0" * 32

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import comms, db, notion, pg, pipeline  # noqa: E402
from app.llm import StubModelClient  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Article  # noqa: E402

ART = Article(id=1, url="https://x/1", title="T", text="Перше речення. Друге речення. Третє.",
              source="s", published_at="2026-01-01", topic="економіка")


def test_model_steps():
    m = StubModelClient()
    rel = m.relevance(ART, "економіка")
    assert rel.relevant and rel.score == 0.9
    ex = m.extraction(ART)
    assert ex.main_claim == "Перше речення"
    d = m.draft(ART, ex, n=7)
    assert (d.headline, d.text) == (ART.title, ART.text)  # поки без моделі: пост = стаття
    assert m.evaluate(d).failure_type == "too_short"  # ART коротша за 80 символів


@pytest.mark.asyncio
async def test_e2e_with_mock_notion(monkeypatch, tmp_path):
    # ізольовані БД, щоб не чіпати data/*.db
    monkeypatch.setattr(db, "ARTICLES_DB", tmp_path / "articles.db")
    monkeypatch.setattr(db, "RUNS_DB", tmp_path / "runs.db")
    created = []

    async def fake_create(database_id, properties, body=None):
        created.append(properties)
        return {"id": f"page-{len(created)}"}

    async def fake_update(page_id, properties):
        return {"id": page_id}

    monkeypatch.setattr(notion, "create_row", fake_create)
    monkeypatch.setattr(notion, "update_row", fake_update)
    saved = []
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "save_draft", lambda run_id, r, status: saved.append((run_id, r.notion_page_id)))
    db.init_db()
    summary = await pipeline.run_pipeline("testrun", database_id="db", limit=1)
    assert summary.status == "ok" and summary.drafts_written == 1
    assert created[0]["Draft"]["title"][0]["text"]["content"] == pipeline.PLACEHOLDER.title
    assert saved == [("testrun", "page-1")]  # драфт додатково пішов у Postgres
    # upsert: повторний прогін з тим самим run_id не створює новий рядок
    await pipeline.run_pipeline("testrun", database_id="db", limit=1)
    assert len(created) == 1


def test_fetch_takes_latest_from_comms(monkeypatch):
    latest = ART.model_copy(update={"id": "0b7c-uuid"})
    monkeypatch.setattr(comms, "enabled", lambda: True)
    monkeypatch.setattr(comms, "fetch_latest_article", lambda: latest)
    assert pipeline.step_fetch(limit=5) == [latest]
    monkeypatch.setattr(comms, "fetch_latest_article", lambda: None)
    assert pipeline.step_fetch(limit=5) == [pipeline.PLACEHOLDER]


def test_api():
    with TestClient(app) as c:
        assert c.get("/health").json()["ok"] is True
        assert c.post("/webhook", json={"data": {"object": "page", "id": "x"}}).status_code == 401
        r = c.post("/run", headers={"x-webhook-secret": "t"})
        assert r.status_code == 202 and "run_id" in r.json()
        assert c.get("/runs/nope").status_code == 404
