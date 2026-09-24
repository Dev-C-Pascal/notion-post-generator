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

    def __init__(self):
        self.langs: list = []

    def draft(self, article: Article, lang: str | None = None) -> Draft:
        self.langs.append(lang)
        return Draft(article_id=article.id, headline=article.title, text="пост 1/\n\nпост 2/",
                     model_version=self.version, lang=lang or "en")


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
    assert (d.headline, d.text, d.model_version, d.lang) == (ART.title, "Пост 1/\n\nПост 2/", "runpod-ep1", "uk")
    assert sent[0] == ("POST", "/v2/ep1/runsync", {"input": {"article_text": ART.text, "lang": "uk"}})
    assert [s[1] for s in sent[1:]] == ["/v2/ep1/status/j1"] * 2


def test_runpod_lang_from_channel_overrides_article():
    sent: list = []
    m = _runpod([{"id": "j3", "status": "COMPLETED", "output": {"post": "Thread 1/"}}], sent)
    assert m.draft(ART, lang="en").lang == "en"  # стаття українська, але кнопка X просить англійський тред
    assert sent[0][2]["input"]["lang"] == "en"


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


TOP_ROW = ("a-top", "https://x/top", "Top", "текст", "Reuters", "2026-09-24", 1, 0.612, "Стан економіки РФ", 3)
TOPIC_ROW = ("a-topic", "https://x/topic", "Topic", "текст", "Kyiv Post", "2026-09-24", "Війна в Україні", 0.669)


def _fake_db(monkeypatch, top: list, topic: list) -> list:
    """comms._query без Postgres: TOP_PICKS → top, TOPIC_PICKS → topic. Повертає журнал запитів."""
    calls: list = []

    def query(sql, params):
        calls.append(("top" if sql == comms.TOP_PICKS else "topic", params))
        return top if sql == comms.TOP_PICKS else topic
    monkeypatch.setattr(comms, "_query", query)
    return calls


def test_picks_prefer_daily_top(monkeypatch):
    calls = _fake_db(monkeypatch, [TOP_ROW], [TOPIC_ROW])
    [(a, rel)] = comms.fetch_picks(1, {"old"})
    assert (a.id, a.topic) == ("a-top", "Стан економіки РФ")
    assert rel.reason.startswith("топ дня №1") and rel.score == 0.612
    assert calls == [("top", (["old"], 1))]  # топу вистачило — тематичний відбір не чіпаємо


def test_picks_fall_back_to_topic_queue(monkeypatch):
    calls = _fake_db(monkeypatch, [], [TOPIC_ROW])
    [(a, rel)] = comms.fetch_picks(1, {"old"})
    assert a.id == "a-topic" and "тематичний відбір" in rel.reason and "Війна в Україні" in rel.reason
    assert calls == [("top", (["old"], 1)), ("topic", (["old"], 1))]
    # топ дав менше, ніж треба: решта з тематичного, без статей, уже взятих із топу
    calls = _fake_db(monkeypatch, [TOP_ROW], [TOPIC_ROW])
    assert [a.id for a, _ in comms.fetch_picks(2, {"old"})] == ["a-top", "a-topic"]
    assert calls[1] == ("topic", (["a-top", "old"], 1))


def test_select_skips_already_drafted(monkeypatch):
    calls = []
    monkeypatch.setattr(comms, "enabled", lambda: True)
    monkeypatch.setattr(comms, "fetch_picks", lambda limit, exclude: calls.append((limit, exclude)) or [(ART, REL)])
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
    model = FakeModel()
    summary = await pipeline.run_pipeline("testrun", database_id="db", channel="fb", model=model)
    assert summary.status == "ok" and summary.drafts_written == 1
    assert model.langs == ["uk"]  # кнопка FB → український пост
    props, body = created[0]
    assert props["Draft"]["title"][0]["text"]["content"] == ART.title
    assert props["Status"]["select"]["name"] == "New draft"  # оцінює людина, не бекенд
    assert "канал: fb · мова: uk" in body.split("\n\n")[0]
    assert body.endswith("пост 1/\n\nпост 2/")
    assert saved == [("testrun", "page-1", "New draft")]  # драфт додатково пішов у Postgres
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
        hook = {"data": {"object": "page", "id": "x"}}
        for path, channel in (("/webhook/fb", "fb"), ("/webhook/x", "x"), ("/webhook", None), ("/run?channel=x", "x")):
            r = c.post(path, json=hook, headers={"x-webhook-secret": "t"})
            assert (r.status_code, r.json()["channel"]) == (202, channel), path
        assert c.post("/webhook/tiktok", json=hook, headers={"x-webhook-secret": "t"}).status_code == 404
        assert c.post("/webhook/fb", json=hook).status_code == 401
        assert c.get("/runs/nope").status_code == 404
