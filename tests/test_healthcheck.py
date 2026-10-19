"""Health Check: еталонний набір і окремий прогін — без мережі й без Postgres (сховище підмінене)."""
import os

os.environ.setdefault("NOTION_TOKEN", "secret_xxx")
os.environ.setdefault("WEBHOOK_SECRET", "t")
os.environ.setdefault("NOTION_DATABASE_ID", "0" * 32)
os.environ.setdefault("RUNPOD_ENDPOINT_ID", "test")
os.environ.setdefault("RUNPOD_API_KEY", "test")
os.environ.setdefault("COMMS_DATABASE_URL", "")

import asyncio  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import healthcheck as hc  # noqa: E402
from app import notion, pg, pipeline, status  # noqa: E402
from app.llm import GroundingRefused, ModelError  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Article, Draft  # noqa: E402


def _art(aid: str) -> Article:
    return Article(id=aid, url=f"https://x/{aid}", title=f"T {aid}", text="текст", source="s", published_at="2026-10-01")


# ---------- еталонний набір ----------

def test_reference_set_takes_only_relevant_with_text_half_per_channel():
    cands = [(f"f{i}", "fb", f"pf-{i}") for i in range(6)] + [(f"x{i}", "x", f"px{i}") for i in range(6)]
    relevant = {"pf0", "pf1", "pf2", "pf3", "px0", "px1", "px2"}  # дефіси в id сторінки не важать
    texts = {a: _art(a) for a, _, _ in cands if a != "f3"}  # у f3 немає повного тексту
    chosen = hc.pick_reference(cands, relevant, texts, per_channel=2)
    assert [c[1] for c in chosen] == ["fb", "fb", "x", "x"]
    assert {c[0] for c in chosen} <= {"f0", "f1", "f2", "x0", "x1", "x2"}
    assert chosen == hc.pick_reference(list(reversed(cands)), relevant, texts, per_channel=2)  # стабільний вибір
    with pytest.raises(RuntimeError, match="канал fb: релевантних статей з текстом 3, а треба 4"):
        hc.pick_reference(cands, relevant, texts, per_channel=4)


# ---------- прогін ----------

class FakeModel:
    version = "runpod-ep:draft_grounded"

    def __init__(self, fail: dict | None = None):
        self.calls: list[tuple[str, str | None]] = []
        self.fail = fail or {}

    def draft(self, article: Article, lang: str | None = None) -> Draft:
        self.calls.append((str(article.id), lang))
        if exc := self.fail.get(str(article.id)):
            raise exc
        return Draft(article_id=article.id, headline=article.title, text="пост", model_version=self.version,
                     lang=lang or "en", needs_review=str(article.id) == "x1")


class FakeStore:
    def __init__(self, refs: list[hc.RefArticle], done=()):
        self.refs, self.done, self.saved, self.finished = refs, set(done), {}, []

    def install(self, monkeypatch):
        monkeypatch.setattr(hc, "reference_articles", lambda: self.refs)
        monkeypatch.setattr(hc, "done_articles", lambda run_id: set(self.done))
        monkeypatch.setattr(hc, "save_draft", self.save)
        monkeypatch.setattr(hc, "finish_run", lambda run_id, st, error=None: self.finished.append((run_id, st, error)))
        monkeypatch.setattr(hc, "create_run", lambda run_id, trigger, version: None)
        monkeypatch.setattr(hc, "WAIT_S", 0.01)

        def forbidden(*a, **k):
            raise AssertionError("Health Check не бронює статей, не пише в Notion і не чіпає рядок статусу")
        for mod, name in ((pg, "claim_article"), (pg, "save_draft"), (notion, "create_row"), (status, "report")):
            monkeypatch.setattr(mod, name, forbidden)
        monkeypatch.setattr(hc, "_current", None)

    def save(self, run_id, article_id, outcome, draft, error, duration_ms):
        self.saved[article_id] = (outcome, draft.lang if draft else None, error)


REFS = [hc.RefArticle(_art("f1"), "fb"), hc.RefArticle(_art("f2"), "fb"),
        hc.RefArticle(_art("x1"), "x"), hc.RefArticle(_art("x2"), "x")]


async def test_run_drafts_every_reference_article_without_touching_buttons(monkeypatch):
    store = FakeStore(REFS)
    store.install(monkeypatch)
    model = FakeModel(fail={"f2": GroundingRefused("job 1: no facts"), "x2": ModelError("job 2: timeout")})
    run_id, _ = hc.start("manual", model)
    await hc.run(run_id, model)
    assert sorted(model.calls) == [("f1", "uk"), ("f2", "uk"), ("x1", "en"), ("x2", "en")]  # мова — за каналом
    assert store.saved == {"f1": ("ok", "uk", None), "f2": ("refused", None, "job 1: no facts"),
                           "x1": ("needs_manual_review", "en", None), "x2": ("error", None, "ModelError: job 2: timeout")}
    assert store.finished == [(run_id, "done", None)]
    assert hc._current is None


async def test_resume_generates_only_missing(monkeypatch):
    store = FakeStore(REFS, done={"f1", "x1"})
    store.install(monkeypatch)
    monkeypatch.setattr(hc, "reopen_run", lambda run_id: ("schedule", "runpod-ep:draft_grounded"))
    model = FakeModel()
    run_id, _ = hc.start("manual", model, resume="hc-old")
    assert run_id == "hc-old"
    await hc.run(run_id, model)
    assert sorted(c[0] for c in model.calls) == ["f2", "x2"]


def test_only_one_run_at_a_time(monkeypatch):
    FakeStore(REFS).install(monkeypatch)
    hc.start("manual", FakeModel())
    with pytest.raises(hc.AlreadyRunning):
        hc.start("manual", FakeModel())


async def test_buttons_go_first(monkeypatch):
    store = FakeStore(REFS[:1])
    store.install(monkeypatch)
    monkeypatch.setattr(pipeline, "_in_pool", pipeline.MAX_PARALLEL_DRAFTS)  # кнопки зайняли весь свій пул
    model = FakeModel()
    run_id, _ = hc.start("manual", model)
    task = asyncio.create_task(hc.run(run_id, model))
    await asyncio.sleep(0.1)
    assert model.calls == []  # Health Check чекає
    monkeypatch.setattr(pipeline, "_in_pool", pipeline.MAX_PARALLEL_DRAFTS - 1)
    await asyncio.wait_for(task, 2)
    assert model.calls == [("f1", "uk")]


def test_schedule_due(monkeypatch):
    monkeypatch.setattr(hc, "EVERY_DAYS", 7)
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert hc.due(None, now) == "new"
    assert hc.due({"status": "done", "started_at": now - timedelta(days=7)}, now) == "new"
    assert hc.due({"status": "done", "started_at": now - timedelta(days=3)}, now) is None
    assert hc.due({"status": "interrupted", "started_at": now - timedelta(days=1)}, now) == "resume"


def test_api(monkeypatch):
    started = []

    def fake_start(trigger, model, resume=None):
        if resume == "busy":
            raise hc.AlreadyRunning("hc-1")
        started.append((trigger, model and model.version, resume))
        return "hc-new", model or FakeModel()

    async def fake_run(run_id, model):
        return None

    monkeypatch.setattr(hc, "start", fake_start)
    monkeypatch.setattr(hc, "run", fake_run)
    with TestClient(app) as c:
        assert c.post("/health-check").status_code == 401
        r = c.post("/health-check", json={"endpoint_id": "ep2", "mode": "draft"}, headers={"x-webhook-secret": "t"})
        assert (r.status_code, r.json()) == (202, {"run_id": "hc-new", "model_version": "runpod-ep2:draft"})
        assert c.post("/health-check", headers={"x-webhook-secret": "t"}).json()["model_version"] == \
            "runpod-test:draft_grounded"  # без параметрів — модель кнопок
        assert c.post("/health-check", json={"resume": "busy"}, headers={"x-webhook-secret": "t"}).status_code == 409
    assert started == [("manual", "runpod-ep2:draft", None), ("manual", "runpod-test:draft_grounded", None)]
