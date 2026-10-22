"""Health Check, пункт 4: звірка судді з людськими оцінками — метрики на синтетичних парах, збір пар з Notion і postgen,
оцінка суддею, звіт, повідомлення, якщо збіг нижче цілі, і ендпоінти. Без мережі: Notion, comms і суддя підмінені."""
import os

os.environ.setdefault("NOTION_TOKEN", "secret_xxx")
os.environ.setdefault("WEBHOOK_SECRET", "t")
os.environ.setdefault("NOTION_DATABASE_ID", "0" * 32)
os.environ.setdefault("RUNPOD_ENDPOINT_ID", "test")
os.environ.setdefault("RUNPOD_API_KEY", "test")
os.environ.setdefault("COMMS_DATABASE_URL", "")

import json  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import comms, hc_calibrate, hc_eval, notion, pg  # noqa: E402
from app.hc_calibrate import Pair  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Article  # noqa: E402

KEY = "claude-haiku-5-5/medium:test"


def P(i: int, human: str, judge: str | None, *, ch: str = "fb", hs: int = 4, js: int | None = 4,
      rel: str = "yes") -> Pair:
    return Pair(page=f"p{i}", channel=ch, title=f"T{i}", url=f"https://x/{i}", human_relevance=rel, human_fact=human,
                human_style=hs, judge_outcome="EVALUATED" if judge else "ERROR", judge_fact=judge,
                judge_style=js if judge else None)


# ---------- метрики (чисті функції) ----------

def test_fact_agreement_matrix_kappa_and_the_always_not_ok_baseline():
    # 10 пар: люди 4 ok / 6 not ok; суддя влучив 7, пропустив 1 not ok, був надто суворим на 2; ще одну не оцінив
    pairs = [P(0, "ok", "ok"), P(1, "ok", "ok"), P(2, "ok", "not ok"), P(3, "ok", "not ok"),
             P(4, "not ok", "not ok"), P(5, "not ok", "not ok"), P(6, "not ok", "not ok"), P(7, "not ok", "not ok"),
             P(8, "not ok", "not ok"), P(9, "not ok", "ok"), P(10, "not ok", None)]
    m = hc_calibrate.metrics(pairs, 0.8)
    f = m["fact"]
    assert (f["n"], f["decided"], f["agreement"]) == (11, 10, 0.7)
    assert f["matrix"] == {"ok": {"ok": 2, "not ok": 2}, "not ok": {"ok": 1, "not ok": 5}}
    assert (f["judge_too_lenient"], f["judge_too_strict"], f["always_not_ok"]) == (1, 2, 0.6)
    # p_o = 0,7; p_e = 0,4·0,3 + 0,6·0,7 = 0,54; kappa = 0,16 / 0,46
    assert f["kappa"] == pytest.approx(0.16 / 0.46)
    assert m["passed"] is False and m["judge_outcomes"] == {"EVALUATED": 10, "ERROR": 1}


def test_kappa_edges():
    assert hc_calibrate.kappa({"ok": {"ok": 0, "not ok": 0}, "not ok": {"ok": 0, "not ok": 0}}) is None
    assert hc_calibrate.kappa({"ok": {"ok": 3, "not ok": 0}, "not ok": {"ok": 0, "not ok": 7}}) == pytest.approx(1)
    # суддя завжди каже not ok: збіг 70%, але kappa 0 — нічого не розрізняє
    assert hc_calibrate.kappa({"ok": {"ok": 0, "not ok": 3}, "not ok": {"ok": 0, "not ok": 7}}) == pytest.approx(0)


def test_publishable_precision_recall_style_and_channels():
    pairs = [P(0, "ok", "ok", hs=4, js=4),  # обидва «можна публікувати»
             P(1, "ok", "ok", hs=5, js=3),  # люди «так», суддя «ні» (Style)
             P(2, "ok", "ok", hs=3, js=4, ch="x"),  # люди «ні» (Style 3), суддя «так»
             P(3, "not ok", "not ok", hs=2, js=2, ch="x"),
             P(4, "ok", "ok", hs=5, js=5, rel="no", ch="x")]  # люди: стаття нерелевантна — публікувати не можна
    m = hc_calibrate.metrics(pairs, 0.8)
    pub = m["publishable"]
    assert pub["pairs"] == 5 and pub["agreement"] == pytest.approx(2 / 5)
    # суддя «так»: p0, p2, p4; люди «так»: p0, p1
    assert pub["precision"] == pytest.approx(1 / 3) and pub["recall"] == pytest.approx(1 / 2)
    assert (pub["human_rate"], pub["judge_rate"]) == (pytest.approx(2 / 5), pytest.approx(3 / 5))
    st = m["style"]
    assert (st["exact"], st["within_1"]) == (pytest.approx(3 / 5), pytest.approx(4 / 5))
    assert (st["human_avg"], st["judge_avg"]) == (pytest.approx(19 / 5), pytest.approx(18 / 5))
    assert m["channels"]["fb"]["fact"]["n"] == 2 and m["channels"]["x"]["fact"]["agreement"] == 1.0
    assert m["passed"] is True


def test_report_text_and_disagreements_put_missed_fakes_first():
    pairs = [P(0, "ok", "not ok"), P(1, "not ok", "ok", ch="x"), P(2, "ok", "ok"), P(3, "not ok", "not ok")]
    assert [p.page for p in hc_calibrate.disagreements(pairs)] == ["p1", "p0"]  # пропущений фейк небезпечніший
    md = hc_calibrate.render(hc_calibrate.metrics(pairs, 0.8), KEY, pairs=pairs, spent=0.42,
                             when=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert md.startswith("# Health Check · звірка судді з людьми · 2026-10-22\n\n"
                         "> ❌ **Збіг по fact safety 50% (ціль ≥ 80%): суддя не досягає цілі**\n")
    assert "- Базова лінія «завжди not ok»: 50%" in md and f"- Суддя: `{KEY}`; вартість оцінки $0.42" in md
    assert "| Людина \\ суддя | ok | not ok |\n| --- | --- | --- |\n| ok | 1 | 1 |\n| not ok | 1 | 1 |" in md
    assert "## Де суддя розійшовся з людьми (fact safety)\n\n- X · [T1](https://x/1) — людина: not ok, суддя: ok\n" in md
    assert md.endswith("\n") and not md.endswith("\n\n")
    from app import hc_report

    kinds = {b["type"] for b in hc_report.to_notion_blocks(md)}  # звіт перетворюється на блоки Notion без сюрпризів
    assert kinds <= {"heading_1", "heading_2", "paragraph", "bulleted_list_item", "callout", "table"}


def test_human_labels_take_only_fully_rated_rows():
    def page(pid, rel, fact, style):
        props = {name: {"select": {"name": v} if v else None} for name, v in
                 (("Article relevance", rel), ("Fact safety", fact), ("Style score", style))}
        return {"id": pid, "properties": props}

    labels = hc_calibrate.human_labels([page("aa-bb", "yes", "not ok", "2"), page("cc", "no", "ok", "5"),
                                        page("dd", "yes", None, "3"), page("ee", "yes", "ok", None)])
    assert labels == {"aabb": {"relevance": "yes", "fact_safety": "not ok", "style": 2},
                      "cc": {"relevance": "no", "fact_safety": "ok", "style": 5}}


def test_target_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_CALIBRATION_TARGET", raising=False)
    assert hc_calibrate.target() == 0.8
    monkeypatch.setenv("HEALTHCHECK_CALIBRATION_TARGET", "0.85")
    assert hc_calibrate.target() == 0.85


# ---------- збір, оцінка, звіт (справжній Postgres) ----------

def _judge_reply(fact: str, style: int) -> str:
    tier_a = 1 if fact == "not ok" else 0
    return (f"RESULT\noutcome: EVALUATED\nrelevance: yes\nfact_safety: {fact}\ntier_a: {tier_a}\ntier_b: 0\n"
            f"style: {style}\nfailure_types: -\n\nCLAIMS\n-\n\nDEFECTS\n-\n\nCORRECTED POST\n-\n\nCOMMENT\n-\n")


class ScriptedJudge:
    key = KEY

    def __init__(self, verdicts: dict[str, tuple[str, int] | Exception]):
        self.verdicts, self.calls = verdicts, []

    def evaluate(self, case):
        self.calls.append((case.article_id, case.channel, case.lang, case.draft_outcome))
        v = self.verdicts[case.article_id]
        if isinstance(v, Exception):
            raise v
        return hc_eval.JudgeReply(_judge_reply(*v), input_tokens=1000, cache_read_tokens=20000, output_tokens=2000)


@pytest.fixture
def mvp(pgdb, monkeypatch):
    """postgen з чотирма драфтами в Notion (і одним без рядка), таблиця MVP з людськими оцінками, comms з текстами."""
    pg.init_db()
    with pg._connect() as c:
        for i, (aid, channel, text, extraction) in enumerate((
                ("art-1", "fb", "Пост один", {"status": "ok"}),
                ("art-2", "fb", "Пост два", {"status": "needs_manual_review"}),
                ("art-3", "x", "Thread three", None),
                ("art-4", None, "Тред без каналу", None),  # кнопка до 23.09: канал за мовою тексту
                ("art-5", "x", "Thread five", None))):  # немає тексту статті в comms
            page = f"1111{i}aaa-bbbb-cccc-dddd-eeeeeeeeeeee" if aid != "art-5" else "55555555-bbbb-cccc-dddd-eeeeeeeeeeee"
            c.execute("INSERT INTO used_articles (article_id, article_url, article_title, run_id, channel) "
                      "VALUES (%s, %s, %s, 'r1', %s)", (aid, f"https://x/{aid}", f"Title {aid}", channel))
            c.execute("INSERT INTO drafts (run_id, article_id, article_url, article_title, relevant, headline, text, "
                      "model_version, notion_page_id, extraction) VALUES ('r1', %s, %s, %s, true, 'h', %s, 'runpod-ep:v1', "
                      "%s, %s::jsonb)", (aid, f"https://x/{aid}", f"Title {aid}", text, page,
                                         json.dumps(extraction) if extraction else None))
    labels = {"11110aaabbbbccccddddeeeeeeeeeeee": ("yes", "ok", "4"), "11111aaabbbbccccddddeeeeeeeeeeee": ("yes", "not ok", "2"),
              "11112aaabbbbccccddddeeeeeeeeeeee": ("no", "not ok", "3"), "11113aaabbbbccccddddeeeeeeeeeeee": ("yes", "ok", "5"),
              "55555555bbbbccccddddeeeeeeeeeeee": ("yes", "ok", "4"), "99999999bbbbccccddddeeeeeeeeeeee": ("yes", "ok", "4")}
    state = {"labels": labels, "queries": []}

    async def fake_query(database_id, filter, sorts=None):
        state["queries"].append((database_id, filter))
        return [{"id": pid, "properties": {name: {"select": {"name": v}} for name, v in
                                           zip(("Article relevance", "Fact safety", "Style score"), vals, strict=True)}}
                for pid, vals in state["labels"].items()]

    def fake_fetch(ids):
        state["fetched"] = ids
        return {aid: Article(id=aid, url=f"https://x/{aid}", title=f"Title {aid}", text=f"Текст статті {aid}",
                             source="Kyiv Post", published_at="2026-09-20") for aid in ids if aid != "art-5"}

    monkeypatch.setattr(notion, "query_rows", fake_query)
    monkeypatch.setattr(comms, "fetch_by_ids", fake_fetch)
    return state


async def test_collect_copies_pairs_once_and_refreshes_human_labels(mvp):
    stats = await hc_calibrate.collect()
    assert stats == {"rated_in_notion": 6, "with_draft": 5, "already_collected": 0, "added": 4, "without_article_text": 1}
    _, filt = mvp["queries"][0]
    assert filt == {"and": [{"property": p, "select": {"is_not_empty": True}}
                            for p in ("Article relevance", "Fact safety", "Style score")]}
    with pg._connect() as c:
        rows = c.execute("SELECT article_id, channel, lang, draft_outcome, relevance, fact_safety, style, article_text "
                         "FROM hc_human ORDER BY article_id").fetchall()
    assert rows == [("art-1", "fb", "uk", "ok", "yes", "ok", 4, "Текст статті art-1"),
                    ("art-2", "fb", "uk", "needs_manual_review", "yes", "not ok", 2, "Текст статті art-2"),
                    ("art-3", "x", "en", "ok", "no", "not ok", 3, "Текст статті art-3"),
                    ("art-4", "fb", "uk", "ok", "yes", "ok", 5, "Текст статті art-4")]
    mvp["labels"]["11110aaabbbbccccddddeeeeeeeeeeee"] = ("yes", "not ok", "1")  # рецензент виправив оцінку
    again = await hc_calibrate.collect()
    assert again["added"] == 0 and again["already_collected"] == 4 and mvp["fetched"] == ["art-5"]  # comms — лише для нових
    with pg._connect() as c:
        assert c.execute("SELECT fact_safety, style FROM hc_human WHERE article_id = 'art-1'").fetchone() == ("not ok", 1)


async def test_evaluate_report_and_alert_when_below_target(mvp, monkeypatch, outbox):
    await hc_calibrate.collect()
    judge = ScriptedJudge({"art-1": ("ok", 4), "art-2": ("ok", 4),  # пропустив not ok
                           "art-3": ("not ok", 3), "art-4": hc_eval.ParseError("немає блоку RESULT")})
    summary = await hc_calibrate.evaluate(judge)
    assert summary == {"judge": KEY, "cases": 4, "outcomes": {"EVALUATED": 3, "ERROR": 1}}
    assert sorted(judge.calls) == [("art-1", "fb", "uk", "ok"), ("art-2", "fb", "uk", "needs_manual_review"),
                                   ("art-3", "x", "en", "ok"), ("art-4", "fb", "uk", "ok")]
    assert [c.article_id for _, c in hc_calibrate.pending(KEY)] == ["art-4"]  # збій оцінки повторюється наступного разу
    judge.verdicts["art-4"] = ("ok", 5)
    await hc_calibrate.evaluate(judge)
    assert len(judge.calls) == 5 and hc_calibrate.pending(KEY) == []  # уже оцінене вдруге не оплачуємо

    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")
    monkeypatch.setenv("HEALTHCHECK_CALIBRATION_MENTION", "@anton")
    monkeypatch.setenv("NOTION_HEALTHCHECK_PAGE_ID", "p" * 32)
    pages = []

    async def fake_create_page(parent, title, blocks):
        pages.append((parent, title))
        return {"url": "https://www.notion.so/calibration"}

    monkeypatch.setattr(notion, "create_page", fake_create_page)
    result = await hc_calibrate.report(KEY)
    assert (result["agreement"], result["decided"], result["passed"]) == (0.75, 4, False)
    assert pages == [("p" * 32, f"Health Check · звірка судді з людьми · {datetime.now(timezone.utc):%Y-%m-%d}")]
    (url, body), = outbox["requests"]
    assert url == "https://hooks.example/abc" and body["verdict"] == "calibration"
    assert body["text"].startswith("@anton ❌ Health Check: суддя збігається з людьми по fact safety лише на 75% (ціль ≥ 80%, "
                                   "4 драфтів, «завжди not ok» дало б 50%)")
    assert body["text"].endswith("Звіт: https://www.notion.so/calibration")
    latest = hc_calibrate.latest(KEY)
    assert latest and (latest["agreement"], latest["target"], latest["decided"]) == (0.75, 0.8, 4)
    assert latest["created_at"] and latest["notion_url"] == "https://www.notion.so/calibration"
    assert hc_calibrate.latest("інший суддя") is None
    assert "**Збіг по fact safety 75%" in latest["markdown"] and "вартість оцінки $" in latest["markdown"]


async def test_no_alert_when_the_judge_meets_the_target(mvp, monkeypatch, outbox):
    await hc_calibrate.collect()
    await hc_calibrate.evaluate(ScriptedJudge({"art-1": ("ok", 4), "art-2": ("not ok", 2), "art-3": ("not ok", 3),
                                               "art-4": ("ok", 5)}))
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")
    result = await hc_calibrate.report(KEY)
    assert result["passed"] and result["agreement"] == 1.0 and result["notion_url"] is None and outbox["requests"] == []


async def test_run_reports_its_own_failure_instead_of_raising(mvp, monkeypatch, outbox):
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")
    judge = ScriptedJudge({a: hc_eval.JudgeFatal("AuthenticationError: invalid x-api-key")
                           for a in ("art-1", "art-2", "art-3", "art-4")})
    assert await hc_calibrate.run(judge) is None and not hc_calibrate.is_busy()
    assert len(judge.calls) == 1  # мертвий ключ зупиняє всю звірку, а не 300 однакових збоїв
    (_, body), = outbox["requests"]
    assert body["text"].startswith("🔴 Health Check: звірка судді з людьми не завершилась\nJudgeFatal: AuthenticationError")


def test_latest_without_a_table_is_none(pgdb):
    assert hc_calibrate.latest(KEY) is None  # звіт прогону тоді скаже, що суддю ще не звірено


# ---------- ендпоінти ----------

def test_calibration_endpoints(monkeypatch):
    started = []

    async def fake_run(judge=None, *, limit=None, collect_first=True):
        started.append((judge.key, limit, collect_first))

    class J:
        key = KEY

    monkeypatch.setattr(hc_calibrate, "run", fake_run)
    monkeypatch.setattr(hc_eval, "get_judge", lambda model=None: J())
    found = {}
    monkeypatch.setattr(hc_calibrate, "latest", lambda key: found.get(key))
    monkeypatch.setattr(hc_eval, "current_judge_key", lambda model=None: KEY)
    secret = {"x-webhook-secret": "t"}
    with TestClient(app) as c:
        assert c.post("/health-check/calibration").status_code == 401
        r = c.post("/health-check/calibration", headers=secret, json={"limit": 20, "collect": False})
        assert (r.status_code, r.json()) == (202, {"judge": KEY, "limit": 20, "result": "/health-check/calibration"})
        assert c.post("/health-check/calibration", headers=secret, json={"limit": "all"}).status_code == 202
        monkeypatch.setattr(hc_calibrate, "is_busy", lambda: True)
        assert c.post("/health-check/calibration", headers=secret).status_code == 409
        assert c.get("/health-check/calibration").status_code == 404
        found[KEY] = {"agreement": 0.84, "target": 0.8, "decided": 280, "notion_url": None, "metrics": {"passed": True},
                      "markdown": "# звірка\n", "created_at": datetime(2026, 10, 22, tzinfo=timezone.utc)}
        body = c.get("/health-check/calibration").json()
        md = c.get("/health-check/calibration?format=markdown")
    assert started == [(KEY, 20, False), (KEY, None, True)]
    assert body == {"agreement": 0.84, "target": 0.8, "decided": 280, "notion_url": None, "metrics": {"passed": True},
                    "created_at": "2026-10-22T00:00:00+00:00", "running": True}
    assert (md.text, md.headers["content-type"]) == ("# звірка\n", "text/markdown; charset=utf-8")
