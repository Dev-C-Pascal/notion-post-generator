"""Тести без мережі: RunPod-клієнт, вибір статті, e2e прогін з mock-Notion, API."""
import os

os.environ["NOTION_TOKEN"] = "secret_xxx"
os.environ["WEBHOOK_SECRET"] = "t"
os.environ["NOTION_DATABASE_ID"] = "0" * 32
# фіктивні: справжній RunPod у тестах не викликається (моделі підставляються, у test_api статей немає)
os.environ["RUNPOD_ENDPOINT_ID"] = "test"
os.environ["RUNPOD_API_KEY"] = "test"
os.environ["COMMS_DATABASE_URL"] = ""

import json  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import comms, db, notion, pg, pipeline  # noqa: E402
from app.llm import ModelError, RunPodModelClient, detect_lang, get_model_client  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Article, Draft, RelevanceResult  # noqa: E402

ART = Article(id="0b7c-uuid", url="https://x/1", title="T", text="Перше речення. Друге речення. Третє.",
              source="s", published_at="2026-01-01", topic="економіка")
REL = RelevanceResult(article_id=ART.id, relevant=True, reason="топ дня №1", score=0.61)


class FakeModel:
    version = "fake"

    def draft(self, article: Article) -> Draft:
        return Draft(article_id=article.id, headline=article.title, text="пост 1/\n\nпост 2/", model_version=self.version)


def _runpod(responses: list[dict], sent: list) -> RunPodModelClient:
    """RunPod-клієнт, що замість мережі віддає відповіді по черзі."""
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json=responses.pop(0))
    return RunPodModelClient("ep1", "key", poll_s=0, transport=httpx.MockTransport(handler))


def test_runpod_draft_waits_for_cold_start():
    sent: list = []
    m = _runpod([{"id": "j1", "status": "IN_QUEUE"}, {"id": "j1", "status": "IN_PROGRESS"},
                 {"id": "j1", "status": "COMPLETED", "output": {"post": " Пост 1/\n\nПост 2/ "}}], sent)
    d = m.draft(ART)
    assert (d.headline, d.text, d.model_version) == (ART.title, "Пост 1/\n\nПост 2/", "runpod-ep1")
    assert sent[0] == ("POST", "/v2/ep1/runsync", {"input": {"article_text": ART.text, "lang": "uk"}})
    assert [s[1] for s in sent[1:]] == ["/v2/ep1/status/j1"] * 2


def test_runpod_failed_job_raises():
    m = _runpod([{"id": "j2", "status": "FAILED", "error": "CUDA OOM"}], [])
    with pytest.raises(ModelError, match="CUDA OOM"):
        m.draft(ART)


def test_model_client_requires_runpod(monkeypatch):
    assert get_model_client().version == "runpod-test"
    monkeypatch.delenv("RUNPOD_API_KEY")
    with pytest.raises(RuntimeError):
        get_model_client()
    assert (detect_lang("Нацбанк зберіг ставку"), detect_lang("NBU kept the rate")) == ("uk", "en")


def test_select_skips_already_drafted(monkeypatch):
    calls = []
    monkeypatch.setattr(comms, "enabled", lambda: True)
    monkeypatch.setattr(comms, "fetch_top_picks", lambda limit, exclude: calls.append((limit, exclude)) or [(ART, REL)])
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "drafted_article_ids", lambda: {"old-uuid"})
    assert pipeline.step_select(limit=2) == [(ART, REL)]
    assert calls == [(2, {"old-uuid"})]
    monkeypatch.setattr(comms, "enabled", lambda: False)
    assert pipeline.step_select(limit=2) == []  # без бази Андрія статей немає — і заглушки теж


@pytest.mark.asyncio
async def test_e2e_with_mock_notion(monkeypatch, tmp_path):
    # ізольовані БД, щоб не чіпати data/*.db
    monkeypatch.setattr(db, "ARTICLES_DB", tmp_path / "articles.db")
    monkeypatch.setattr(db, "RUNS_DB", tmp_path / "runs.db")
    created = []

    async def fake_create(database_id, properties, body=None):
        created.append((properties, body))
        return {"id": f"page-{len(created)}"}

    async def fake_update(page_id, properties):
        return {"id": page_id}

    monkeypatch.setattr(notion, "create_row", fake_create)
    monkeypatch.setattr(notion, "update_row", fake_update)
    picks = [(ART, REL)]
    monkeypatch.setattr(pipeline, "step_select", lambda limit: picks)
    saved = []
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "save_draft", lambda run_id, r, status: saved.append((run_id, r.notion_page_id, status)))
    db.init_db()
    summary = await pipeline.run_pipeline("testrun", database_id="db", model=FakeModel())
    assert summary.status == "ok" and summary.drafts_written == 1
    props, body = created[0]
    assert props["Draft"]["title"][0]["text"]["content"] == ART.title
    assert props["Status"]["select"]["name"] == "Not started"  # оцінює людина, не бекенд
    assert body.endswith("пост 1/\n\nпост 2/")
    assert saved == [("testrun", "page-1", "Not started")]  # драфт додатково пішов у Postgres
    # upsert: повторний прогін з тим самим run_id не створює новий рядок
    await pipeline.run_pipeline("testrun", database_id="db", model=FakeModel())
    assert len(created) == 1
    # топ вичерпано — прогін успішний, але нічого не пише
    picks.clear()
    empty = await pipeline.run_pipeline("run2", database_id="db", model=FakeModel())
    assert (empty.status, empty.drafts_written, len(created)) == ("ok", 0, 1)


def test_api():
    with TestClient(app) as c:
        assert c.get("/health").json()["model"] == "runpod-test"
        assert c.post("/webhook", json={"data": {"object": "page", "id": "x"}}).status_code == 401
        r = c.post("/run", headers={"x-webhook-secret": "t"})
        assert r.status_code == 202 and "run_id" in r.json()
        assert c.get("/runs/nope").status_code == 404
