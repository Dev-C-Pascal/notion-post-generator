"""Тести без мережі: RunPod-клієнт, вибір статті, e2e прогін з mock-Notion, API."""
import os

os.environ["NOTION_TOKEN"] = "secret_xxx"
os.environ["WEBHOOK_SECRET"] = "t"
os.environ["NOTION_DATABASE_ID"] = "0" * 32
# фіктивні: справжній RunPod у тестах не викликається (моделі підставляються, у test_api статей немає)
os.environ["RUNPOD_ENDPOINT_ID"] = "test"
os.environ["RUNPOD_API_KEY"] = "test"
os.environ["COMMS_DATABASE_URL"] = ""

import asyncio  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import comms, db, main, notion, pg, pipeline  # noqa: E402
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
    # зріз топу — лише свіжий; топу вистачило — тематичний відбір не чіпаємо
    assert calls == [("top", (comms.TOP_MAX_AGE_HOURS, ["old"], 1))]


def test_picks_fall_back_to_topic_queue(monkeypatch):
    calls = _fake_db(monkeypatch, [], [TOPIC_ROW])
    [(a, rel)] = comms.fetch_picks(1, {"old"})
    assert a.id == "a-topic" and "Війна в Україні" in rel.reason
    assert "у топі дня вільних статей немає" in rel.reason  # не «топ дня ще рахується»: він рахується щогодини
    # тематичний відбір — лише статті за останню добу
    assert calls == [("top", (comms.TOP_MAX_AGE_HOURS, ["old"], 1)), ("topic", (comms.FRESH_HOURS, ["old"], 1))]
    # топ дав менше, ніж треба: решта з тематичного, без статей, уже взятих із топу
    calls = _fake_db(monkeypatch, [TOP_ROW], [TOPIC_ROW])
    assert [a.id for a, _ in comms.fetch_picks(2, {"old"})] == ["a-top", "a-topic"]
    assert calls[1] == ("topic", (comms.FRESH_HOURS, ["a-top", "old"], 1))


def _art(aid: str) -> tuple[Article, RelevanceResult]:
    return ART.model_copy(update={"id": aid, "url": f"https://x/{aid}"}), REL.model_copy(update={"article_id": aid})


class FakeUsed:
    """used_articles без Postgres. taken — те, що вже забронював інший прогін, але чого ще не було у знімку."""

    def __init__(self, used=(), taken=()):
        self.used, self.taken, self.released = dict.fromkeys(used, "old"), set(taken), []

    def install(self, monkeypatch, feed: list[str]):
        monkeypatch.setattr(comms, "enabled", lambda: True)
        monkeypatch.setattr(pg, "enabled", lambda: True)
        monkeypatch.setattr(comms, "fetch_picks", lambda limit, exclude: [_art(a) for a in feed if a not in exclude][:limit])
        monkeypatch.setattr(pg, "used_article_ids", lambda: set(self.used))
        monkeypatch.setattr(pg, "claim_article", self.claim)
        monkeypatch.setattr(pg, "release_article", lambda aid, run_id: self.released.append((aid, run_id)))

    def claim(self, article, run_id, channel):
        aid = str(article.id)
        if aid in self.used or aid in self.taken:
            self.used.setdefault(aid, "other-run")
            return False
        self.used[aid] = run_id
        return True


def test_select_never_takes_used_article(monkeypatch):
    fake = FakeUsed(used={"a1"})
    fake.install(monkeypatch, ["a1", "a2", "a3"])
    assert [a.id for a, _ in pipeline.step_select("r1", limit=1)] == ["a2"]  # a1 уже має драфт
    assert [a.id for a, _ in pipeline.step_select("r2", limit=1)] == ["a3"]  # a2 забронював r1
    assert pipeline.step_select("r3", limit=1) == []  # статті скінчились — прогін нічого не пише
    assert fake.used == {"a1": "old", "a2": "r1", "a3": "r2"}


def test_parallel_run_gets_next_article(monkeypatch):
    # r1 ще генерує a2 (у знімку used його нема, але бронь уже стоїть) — r2 бере a3, а не дубль
    fake = FakeUsed(taken={"a2"})
    fake.install(monkeypatch, ["a2", "a3"])
    assert [a.id for a, _ in pipeline.step_select("r2", limit=1)] == ["a3"]
    # увесь запас розібрали паралельні прогони — повторна вибірка з новим знімком used
    feed = [f"b{i}" for i in range(pipeline.PICK_SPARE + 3)]
    fake = FakeUsed(taken=set(feed[:pipeline.PICK_SPARE + 1]))
    fake.install(monkeypatch, feed)
    assert [a.id for a, _ in pipeline.step_select("r3", limit=1)] == [feed[pipeline.PICK_SPARE + 1]]


def test_select_without_comms_is_empty(monkeypatch):
    monkeypatch.setattr(comms, "enabled", lambda: False)
    assert pipeline.step_select("r", limit=2) == []  # без бази Андрія статей немає — і заглушки теж


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
    monkeypatch.setattr(pipeline, "step_select", lambda run_id, limit, channel=None: picks)
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


@pytest.mark.asyncio
async def test_failed_run_releases_article(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "RUNS_DB", tmp_path / "runs.db")
    db.init_db()
    fake = FakeUsed()
    fake.install(monkeypatch, ["a1"])

    async def notion_down(database_id, properties, body=None):
        raise httpx.ConnectTimeout("notion")

    monkeypatch.setattr(notion, "create_row", notion_down)
    summary = await pipeline.run_pipeline("r1", database_id="db", model=FakeModel())
    assert (summary.status, summary.failure_type) == ("failed", "ConnectTimeout")
    assert fake.released == [("a1", "r1")]  # драфт не дійшов до Notion — стаття знову вільна
    run = db.get_run("r1")  # у журналі — крок, стаття і текст помилки, а не лише назва класу
    assert run and run["failure_step"] == "notion"
    assert run["failure_detail"] == "article a1 (https://x/a1): notion"


@pytest.mark.asyncio
async def test_notion_retries_only_when_request_did_not_happen(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setattr(notion, "BACKOFF_S", 0)
    replies: list = [httpx.ConnectTimeout("dns"), httpx.Response(429, headers={"Retry-After": "0"}),
                     httpx.Response(200, json={"id": "p1"})]
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    # не з'єднались, потім rate limit — запит точно не виконався, третя спроба створює рядок
    assert (await notion.create_row("db", {}, body="текст"))["id"] == "p1"
    assert sent == ["POST"] * 3
    # ReadTimeout: запит міг дійти до Notion — не повторюємо, щоб не створити дубль
    replies[:] = [httpx.ReadTimeout("slow"), httpx.Response(200, json={"id": "p2"})]
    sent.clear()
    with pytest.raises(httpx.ReadTimeout):
        await notion.create_row("db", {})
    assert sent == ["POST"]


class SlowModel(FakeModel):
    """Модель, що «генерує» 0,3 с і рахує, скільки генерацій ішло одночасно."""

    def __init__(self):
        super().__init__()
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def draft(self, article: Article, lang: str | None = None) -> Draft:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.3)
        with self.lock:
            self.active -= 1
        return super().draft(article, lang)


@pytest.mark.asyncio
async def test_parallel_clicks_do_not_starve_notion(monkeypatch, tmp_path):
    # 8 натискань разом: генерацій не більше MAX_PARALLEL_DRAFTS, а спільний пул потоків (DNS для Notion) вільний
    monkeypatch.setattr(db, "RUNS_DB", tmp_path / "runs.db")
    db.init_db()
    FakeUsed().install(monkeypatch, [f"a{i}" for i in range(8)])
    monkeypatch.setattr(pg, "save_draft", lambda run_id, r, status: None)
    created = []

    async def fake_create(database_id, properties, body=None):
        created.append(body)
        return {"id": f"page-{len(created)}"}

    monkeypatch.setattr(notion, "create_row", fake_create)
    model = SlowModel()
    runs = asyncio.gather(*(pipeline.run_pipeline(f"r{i}", database_id="db", model=model) for i in range(8)))
    await asyncio.sleep(0.1)  # генерації вже йдуть
    t0 = time.monotonic()
    await asyncio.to_thread(lambda: None)  # так само, як getaddrinfo перед з'єднанням з Notion
    assert time.monotonic() - t0 < 0.2
    summaries = await runs
    assert [s.status for s in summaries] == ["ok"] * 8 and len(created) == 8
    assert model.peak == pipeline.MAX_PARALLEL_DRAFTS


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
        assert c.post("/webhook/fb", json=hook, headers={"x-webhook-secret": "change-me"}).status_code == 401
        # тіло не JSON: автентифікація та сама, помилки 500 немає
        assert c.post("/webhook/fb", content=b"", headers={"x-webhook-secret": "t"}).status_code == 202
        assert c.get("/runs/nope").status_code == 404


@pytest.mark.parametrize("secret", ["", "change-me"])
def test_refuses_to_start_with_weak_secret(monkeypatch, secret):
    monkeypatch.setattr(main, "WEBHOOK_SECRET", secret)
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"), TestClient(app):
        pass
