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

from app import comms, db, main, notion, pg, pipeline, status  # noqa: E402
from app.llm import GroundingRefused, ModelError, RunPodModelClient, detect_lang, get_model_client  # noqa: E402
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


FACT = {"claim": "Vyriy unveiled Slavic.", "verbatim_quote": "Vyriy Industries has unveiled Slavic"}


def test_runpod_draft_waits_for_cold_start():
    sent: list = []
    m = _runpod([{"id": "j1", "status": "IN_QUEUE"}, {"id": "j1", "status": "IN_PROGRESS"},
                 {"id": "j1", "status": "COMPLETED", "output": {
                     "status": "ok", "post": " Пост 1/\n\nПост 2/ ", "facts_used": [FACT], "facts_rejected": [],
                     "trusted_source": "s", "verification": {"overall": "PASS"}, "raw_extraction_output": "[…]"}}],
                sent)
    d = m.draft(ART)
    assert (d.headline, d.text, d.lang, d.needs_review) == (ART.title, "Пост 1/\n\nПост 2/", "uk", False)
    assert d.model_version == "runpod-ep1:draft_grounded"  # режим у версії: оцінки до і після не змішуються
    assert (d.facts_used, d.facts_rejected) == ([FACT], [])
    assert d.review == {"status": "ok", "trusted_source": "s", "verification": {"overall": "PASS"}}
    # справжня назва видання — лише її модель має право цитувати
    assert sent[0] == ("POST", "/v2/ep1/runsync", {"input": {
        "article_text": ART.text, "lang": "uk", "mode": "draft_grounded", "source_name": "s"}})
    assert [s[1] for s in sent[1:]] == ["/v2/ep1/status/j1"] * 2


REVIEW_OUT = {"status": "needs_manual_review", "post": None, "draft_for_review": " Чернетка з вигадкою. ",
              "verification": {"overall": "FAIL", "deterministic_failures": [
                  {"sentence": "Russia is trying to recruit him.", "reason": "not supported by any fact"}],
                  # так пункт судді виглядав на живому ендпоінті 29.09
                  "judge_failures": [{"matched_fact_indices": [5], "offending_span": "4%", "sentence_id": 1,
                                      "text": "росія витрачає 4% ВВП на обслуговування боргу.", "verdict": "DISTORTED",
                                      "reason": "Fact 5 says '4 trillion rubles this year'."},
                                     "number 12.4% not in article"]},
              "facts_used": [FACT], "facts_rejected": [], "trusted_source": None}


def test_runpod_needs_manual_review_is_never_the_post():
    sent: list = []
    d = _runpod([{"id": "j7", "status": "COMPLETED", "output": REVIEW_OUT}], sent).draft(
        ART.model_copy(update={"source": "unknown"}))
    assert "source_name" not in sent[0][2]["input"]  # видання невідоме — не вигадуємо
    assert (d.needs_review, d.text) == (True, "Чернетка з вигадкою.")  # post = null не чіпаємо
    assert d.review["verification"]["overall"] == "FAIL"
    with pytest.raises(ModelError, match="невідомий output.status"):
        _runpod([{"id": "j8", "status": "COMPLETED", "output": {"status": "weird", "post": "x"}}], []).draft(ART)
    with pytest.raises(ModelError, match="тексту немає"):
        _runpod([{"id": "j9", "status": "COMPLETED", "output": {"status": "needs_manual_review", "post": None}}],
                []).draft(ART)


@pytest.mark.parametrize("job, text", [
    # v15 (29.09, перевірено живим викликом): нормальна відмова — COMPLETED + output.error_message
    ({"id": "j10", "status": "COMPLETED", "output": {
        "error_message": "Extraction returned no parseable facts; refusing to draft.", "raw_extraction_output": "[]"}},
     "no parseable facts"),
    # погані вхідні дані — теж відмова від статті, не збій
    ({"id": "j11", "status": "COMPLETED", "output": {"error_message": "Input article_text is empty."}}, "is empty"),
    # до v15: FAILED + job.error (RunPod резервує "error") — лишаємо розпізнавання
    ({"id": "j4", "status": "FAILED", "error": "Extraction returned no parseable facts; refusing to draft.",
      "output": {"raw_extraction_output": "[]"}}, "refusing to draft"),
    ({"id": "j5", "status": "COMPLETED",
      "output": {"error": "No extracted facts are grounded in the article; refusing to draft."}}, "refusing to draft"),
])
def test_runpod_grounding_refusal(job, text):
    with pytest.raises(GroundingRefused, match=text):
        _runpod([job], []).draft(ART)


def test_failure_lines_flag_invalid_judge():
    lines = pipeline._failure_lines({"overall": "FAIL", "judge_output_invalid": True, "judge_failures": [],
                                     "judge_raw": "not json"})
    assert lines == ["Суддя повернув невалідну відповідь — його вердикту не довіряємо: перевірте весь текст"]


def test_runpod_lang_from_channel_overrides_article():
    sent: list = []
    m = _runpod([{"id": "j3", "status": "COMPLETED", "output": {"post": "Thread 1/"}}], sent)
    assert m.draft(ART, lang="en").lang == "en"  # стаття українська, але кнопка X просить англійський тред
    assert sent[0][2]["input"]["lang"] == "en"


def test_runpod_failed_job_raises():
    m = _runpod([{"id": "j2", "status": "FAILED", "error": "CUDA OOM"}], [])
    with pytest.raises(ModelError, match="CUDA OOM") as e:
        m.draft(ART)
    assert not isinstance(e.value, GroundingRefused)  # звичайний збій: статтю можна брати знову
    # output.error без відмови — теж збій, такий пост не постимо
    with pytest.raises(ModelError, match="boom"):
        _runpod([{"id": "j6", "status": "COMPLETED", "output": {"error": "boom", "post": "x"}}], []).draft(ART)


def test_model_client_requires_runpod(monkeypatch):
    assert get_model_client().version == "runpod-test:draft_grounded"
    monkeypatch.setenv("RUNPOD_MODE", "draft")  # повернути старий режим без зміни коду
    assert get_model_client().version == "runpod-test:draft"
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

    async def fake_create(database_id, properties, body=None, extra_blocks=None):
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

    async def notion_down(database_id, properties, body=None, extra_blocks=None):
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


class RefusingModel(FakeModel):
    """draft_grounded: відмовляється від статей зі списку, решту пише з фактами."""

    def __init__(self, refuse: set[str]):
        super().__init__()
        self.refuse, self.asked = refuse, []

    def draft(self, article: Article, lang: str | None = None) -> Draft:
        self.asked.append(article.id)
        if article.id in self.refuse:
            raise GroundingRefused("RunPod job j: Extraction returned no parseable facts; refusing to draft.")
        return super().draft(article, lang).model_copy(update={
            "facts_used": [FACT], "facts_rejected": [{"claim": "Slavic costs $1M.", "verbatim_quote": "costs $1M"}]})


@pytest.mark.asyncio
async def test_refused_article_is_skipped_not_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "RUNS_DB", tmp_path / "runs.db")
    db.init_db()
    fake = FakeUsed()
    fake.install(monkeypatch, ["a1", "a2"])
    saved: list = []
    monkeypatch.setattr(pg, "save_draft", lambda run_id, r, st: saved.append(r.draft))
    created: list = []

    async def fake_create(database_id, properties, body=None, extra_blocks=None):
        created.append((properties, extra_blocks))
        return {"id": "page-1"}

    monkeypatch.setattr(notion, "create_row", fake_create)
    model = RefusingModel({"a1"})
    summary = await pipeline.run_pipeline("r1", database_id="db", channel="fb", model=model)
    # a1 — відмова, у тому ж прогоні взято a2; a1 лишається «використаною» (бронь не знято) — вдруге її не візьмуть
    assert (summary.status, summary.drafts_written, model.asked) == ("ok", 1, ["a1", "a2"])
    assert fake.released == [] and fake.used["a1"] == "r1"
    # під постом — згорнуті блоки з підтвердженими й відкинутими фактами; факти пішли і в postgen
    toggles = created[0][1]
    assert [t["toggle"]["rich_text"][0]["text"]["content"][:25] for t in toggles] == [
        "Факти, з яких написано по", "Відкинуті факти (1) — цит"]
    item = toggles[0]["toggle"]["children"][0]["bulleted_list_item"]["rich_text"][0]["text"]["content"]
    assert item == "Vyriy unveiled Slavic. — «Vyriy Industries has unveiled Slavic»"
    assert saved[0].facts_used == [FACT]


@pytest.mark.asyncio
async def test_run_gives_up_after_max_refusals(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "RUNS_DB", tmp_path / "runs.db")
    db.init_db()
    fake = FakeUsed()
    feed = ["a1", "a2", "a3", "a4"]
    fake.install(monkeypatch, feed)
    model = RefusingModel(set(feed))
    summary = await pipeline.run_pipeline("r1", database_id="db", model=model)
    # 1 + MAX_REFUSALS спроб, далі — збій; жодну з відхилених статей не повертаємо в чергу
    assert model.asked == feed[:pipeline.MAX_REFUSALS + 1]
    assert (summary.status, summary.failure_type, fake.released) == ("failed", "GroundingRefused", [])
    run = db.get_run("r1")
    assert run and run["failure_step"] == "model"


class ReviewModel(FakeModel):
    """draft_grounded повернув needs_manual_review."""

    def draft(self, article: Article, lang: str | None = None) -> Draft:
        return super().draft(article, lang).model_copy(update={
            "text": "Чернетка з вигадкою.", "needs_review": True, "facts_used": [FACT],
            "review": {"status": "needs_manual_review", "verification": REVIEW_OUT["verification"]}})


@pytest.mark.asyncio
async def test_needs_manual_review_goes_to_table_as_needs_fact_check(monkeypatch, tmp_path):
    sent = _board(monkeypatch, tmp_path, ["a1"])
    saved: list = []
    monkeypatch.setattr(pg, "save_draft", lambda run_id, r, st: saved.append(st))
    created: list = []

    async def fake_create(database_id, properties, body=None, extra_blocks=None):
        created.append((properties, body, extra_blocks))
        return {"id": "page-1"}

    monkeypatch.setattr(notion, "create_row", fake_create)
    summary = await pipeline.run_pipeline("r1", database_id="db", channel="x", model=ReviewModel())
    assert (summary.status, summary.drafts_written) == ("ok", 1)
    props, body, blocks = created[0]
    assert props["Status"]["select"]["name"] == "Needs fact-check"  # наявна опція, таблицю не змінюємо
    assert props["Draft"]["title"][0]["text"]["content"] == "⚠ Не пройшов перевірку: T"
    assert "НЕ ПЕРЕВІРЕНО" in body and body.endswith("Чернетка з вигадкою.")
    failed = [i["bulleted_list_item"]["rich_text"][0]["text"]["content"] for i in blocks[0]["toggle"]["children"]]
    assert blocks[0]["toggle"]["rich_text"][0]["text"]["content"] == "Що не пройшло перевірку (3)"
    assert failed == ["«Russia is trying to recruit him.» — not supported by any fact",
                      "«росія витрачає 4% ВВП на обслуговування боргу.» → «4%» — DISTORTED: "
                      "Fact 5 says '4 trillion rubles this year'.",
                      "number 12.4% not in article"]
    assert saved == ["Needs fact-check"]
    await status.drain()
    [line] = _board_lines(sent[-1][1])
    assert "X — не пройшов перевірку фактів: «T» → у таблиці як Needs fact-check" in line


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

    async def fake_create(database_id, properties, body=None, extra_blocks=None):
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


def _board(monkeypatch, tmp_path, feed: list[str], tree: dict | None = None) -> list:
    """Рядок статусу ввімкнено, Notion-блоки підмінено: повертає список надісланих оновлень блоків.
    tree — id блоку → дочірні блоки, як їх бачить Notion (callout «blk» спершу порожній)."""
    monkeypatch.setenv("NOTION_STATUS_BLOCK_ID", "blk")
    monkeypatch.setattr(status, "_lines", {})
    monkeypatch.setattr(status, "_pending", False)
    monkeypatch.setattr(status, "_lock", None)  # замок прив'язується до event loop, а в кожного тесту свій
    monkeypatch.setattr(status, "_body_id", None)
    sent: list = []
    tree = {"blk": []} if tree is None else tree
    tree.setdefault("blk", [])
    ids = iter(range(1, 10_000))  # нові id не повторюють видалені (як у Notion)

    async def fake_update_block(block_id, payload):
        if block_id not in tree:
            raise RuntimeError("Notion 404: object_not_found")  # блок видалили руками
        sent.append((block_id, payload))
        return {}

    async def fake_list_children(block_id):
        return list(tree.get(block_id, []))

    async def fake_append_children(block_id, children):
        made = []
        for c in children:
            b = {"id": f"b{next(ids)}", "type": c["type"]}
            tree[b["id"]] = []
            tree[block_id].append(b)
            for g in c[c["type"]].get("children", []):
                gid = f"b{next(ids)}"
                tree[gid] = []
                tree[b["id"]].append({"id": gid, "type": g["type"]})
            made.append(b)
        return made

    monkeypatch.setattr(notion, "update_block", fake_update_block)
    monkeypatch.setattr(notion, "list_children", fake_list_children)
    monkeypatch.setattr(notion, "append_children", fake_append_children)
    monkeypatch.setattr(db, "RUNS_DB", tmp_path / "runs.db")
    db.init_db()
    FakeUsed().install(monkeypatch, feed)
    monkeypatch.setattr(pg, "save_draft", lambda run_id, r, st: None)
    return sent


def _board_lines(payload: dict) -> list[str]:
    """Рядки натискань з оновлення paragraph у toggle (підказка живе окремо, у самому callout)."""
    return "".join(s["text"]["content"] for s in payload["paragraph"]["rich_text"]).split("\n")


@pytest.mark.asyncio
async def test_status_board_follows_each_click(monkeypatch, tmp_path):
    sent = _board(monkeypatch, tmp_path, ["a1"])

    async def fake_create(database_id, properties, body=None, extra_blocks=None):
        return {"id": "page-1"}

    monkeypatch.setattr(notion, "create_row", fake_create)
    await pipeline.run_pipeline("r1", database_id="db", channel="fb", model=FakeModel())
    await pipeline.run_pipeline("r2", database_id="db", channel="x", model=FakeModel())  # статей більше немає
    await status.drain()
    block_id, payload = sent[-1]
    lines = _board_lines(payload)
    assert block_id != "blk" and len(lines) == 2  # рядки — не в callout, а в paragraph у toggle
    assert "X — немає свіжих статей" in lines[0]  # новіші зверху
    assert "FB — готово: «T» → у таблиці MVP" in lines[1]
    [link] = [s["text"]["link"]["url"] for s in payload["paragraph"]["rich_text"] if s["text"].get("link")]
    assert link == "https://www.notion.so/page1"  # назва — посилання на рядок драфту


@pytest.mark.asyncio
async def test_status_board_reports_failure_and_restart(monkeypatch, tmp_path):
    sent = _board(monkeypatch, tmp_path, ["a1"])

    async def notion_down(database_id, properties, body=None, extra_blocks=None):
        raise RuntimeError("Notion 400: validation_error")

    monkeypatch.setattr(notion, "create_row", notion_down)
    await pipeline.run_pipeline("r1", database_id="db", channel="fb", model=FakeModel())
    await status.drain()
    [line] = _board_lines(sent[-1][1])
    assert "FB — не вдалося (notion): «T» — стаття знову в черзі, натисніть ще раз" in line
    status.restarted()  # після перезапуску бекенду «генерується» не висить
    await status.drain()
    [line] = _board_lines(sent[-1][1])
    assert "бекенд запущено." in line and "обірвано" not in line


@pytest.mark.asyncio
async def test_status_board_coalesces_bursts(monkeypatch, tmp_path):
    sent = _board(monkeypatch, tmp_path, [])
    for i in range(20):
        status.report(f"r{i}", "fb", "12:00", f"подія {i}")
    await status.drain()
    # 20 подій поспіль — одне оновлення рядків, а не 20 (ліміт Notion ~3 запити/с)
    assert len([p for _, p in sent if "paragraph" in p]) == 1
    lines = _board_lines(sent[-1][1])
    assert len(lines) == status.MAX_LINES and "подія 19" in lines[0] and f"подія {20 - status.MAX_LINES}" in lines[-1]


@pytest.mark.asyncio
async def test_status_board_lives_in_collapsed_toggle(monkeypatch, tmp_path):
    tree: dict = {}
    sent = _board(monkeypatch, tmp_path, [], tree)
    status.report("r1", "fb", "12:00", "подія 1")
    await status.drain()
    status.report("r2", "x", "12:01", "подія 2")
    await status.drain()
    # у callout — лише коротка підказка, один раз; рядки — у paragraph всередині одного toggle
    assert [p for b, p in sent if b == "blk"] == [{"callout": {"rich_text": [status._seg(status.HEADER)]}}]
    [toggle] = tree["blk"]
    [body] = tree[toggle["id"]]
    assert (toggle["type"], body["type"]) == ("toggle", "paragraph")
    assert [b for b, p in sent if b != "blk"] == [body["id"]] * 2
    assert "подія 2" in _board_lines(sent[-1][1])[0]

    # toggle видалили руками — наступне оновлення створює його заново, а не мовчки падає
    tree.pop(tree.pop(toggle["id"])[0]["id"])
    tree["blk"].clear()
    status.report("r3", "fb", "12:02", "подія 3")
    await status.drain()
    [toggle2] = tree["blk"]
    assert toggle2["id"] != toggle["id"] and sent[-1][0] == tree[toggle2["id"]][0]["id"]
    assert "подія 3" in _board_lines(sent[-1][1])[0]


@pytest.mark.asyncio
async def test_status_board_shows_queue_position(monkeypatch, tmp_path):
    # натискань більше, ніж місць у пулі: ті, що чекають, бачать своє місце, і воно посувається
    n = pipeline.MAX_PARALLEL_DRAFTS + 2
    sent = _board(monkeypatch, tmp_path, [f"a{i}" for i in range(n)])
    monkeypatch.setattr(pipeline, "_waiting", [])
    monkeypatch.setattr(pipeline, "_waiting_info", {})

    async def fake_create(database_id, properties, body=None, extra_blocks=None):
        return {"id": "page-1"}

    monkeypatch.setattr(notion, "create_row", fake_create)
    states: dict[str, list[str]] = {}  # послідовність станів кожного натискання (рендер блоку зливає проміжні)
    report = status.report

    def spy(run_id, channel, started, state, **kw):
        states.setdefault(run_id, []).append(state)
        report(run_id, channel, started, state, **kw)

    monkeypatch.setattr(status, "report", spy)
    model = SlowModel()
    await asyncio.gather(*(pipeline.run_pipeline(f"r{i}", database_id="db", channel="fb", model=model)
                           for i in range(n)))
    await status.drain()
    def changes(run_id: str) -> list[str]:  # місце в черзі перепоказується при кожному старті — повтори прибираємо
        seq = [s for s in states[run_id] if s != "шукаю статтю…"]
        return [s for i, s in enumerate(seq) if i == 0 or s != seq[i - 1]]

    # останнє натискання: 2-ге в черзі → черга посунулась → генерується → готово
    assert changes(f"r{n - 1}") == ["у черзі: 2-й", "у черзі: 1-й", "генерується", "готово"]
    assert changes("r0") == ["генерується", "готово"]  # перші — без черги
    assert sum("готово" in line for line in _board_lines(sent[-1][1])) == n  # фінал у блоці — усі готові
    assert pipeline._waiting == []


def test_api():
    with TestClient(app) as c:
        assert c.get("/health").json()["model"] == "runpod-test:draft_grounded"
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
