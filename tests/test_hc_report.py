"""Health Check, пункт 5: звіт — вигляд (render), блоки Notion, збереження, публікація, збирання з бази й ендпоінти."""
import os

os.environ.setdefault("NOTION_TOKEN", "secret_xxx")
os.environ.setdefault("WEBHOOK_SECRET", "t")
os.environ.setdefault("NOTION_DATABASE_ID", "0" * 32)
os.environ.setdefault("RUNPOD_ENDPOINT_ID", "test")
os.environ.setdefault("RUNPOD_API_KEY", "test")
os.environ.setdefault("COMMS_DATABASE_URL", "")

import dataclasses  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_hc_compare import BASE, NEW, _judge  # noqa: E402  (той самий модуль, що вже завантажив pytest)

from app import hc_compare as cmp  # noqa: E402
from app import hc_flow, hc_report, notion  # noqa: E402
from app.main import app  # noqa: E402

RUN = {"run_id": "hc-2", "trigger": "schedule", "model_version": "runpod-new:draft_grounded", "status": "done",
       "started_at": datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc),
       "finished_at": datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)}
KEY = "claude-haiku-5-5/medium:test"
NEW_WITH_TYPES = [dataclasses.replace(r, failure_types=["factual distortion"] if r.article_id == "f1" else
                                      ["weak structure"] if r.article_id == "x2" else r.failure_types) for r in NEW]


def _comparisons():
    prev = cmp.compare("hc-1", BASE, "hc-2", NEW_WITH_TYPES)
    prev.meta = {"base": {"model_version": "runpod-old:draft_grounded"}, "new": RUN, "judge": KEY}
    baseline = cmp.compare("hc-0", BASE, "hc-2", NEW_WITH_TYPES)
    baseline.meta = {"base": {"model_version": "runpod-new:draft_grounded"}, "new": RUN, "judge": KEY}
    return prev, baseline


# ---------- вигляд ----------

def test_report_shows_verdict_metrics_changes_and_caveats():
    prev, baseline = _comparisons()
    prev.warnings.append("hc-2: 3 драфтів без оцінки цього судді — порівняння неповне")
    md = hc_report.render(RUN, prev.new, KEY, prev=prev, baseline=baseline, level="warning",
                          reasons=["Fact safety = ok впав на 12 п. п. (p = 0,03)"])
    assert md.startswith("# Health Check · hc-2 · 2026-10-08\n\n> ⚠️ **Є ознаки погіршення**\n\n- Fact safety = ok впав на 12")
    assert "\n\n## Прогін\n\n- Модель:" in md
    assert "- Модель: `runpod-new:draft_grounded` (у базі було `runpod-old:draft_grounded`: модель змінилась)" in md
    assert "- Запуск: schedule · 2026-10-08 10:00 UTC → 2026-10-08 12:30 UTC · статус done" in md
    assert "- Статей у наборі: 8 (FB 4, X 4); драфт отримали 7, відмов моделі 0, збоїв 1" in md
    assert "## Застереження\n\n- hc-2: 3 драфтів без оцінки" in md
    assert "| Метрика | Зараз | Попередній (hc-1) | Зміна | p | База (hc-0) | Зміна | Ціль |" in md
    assert "| Fact safety = ok | 57% | 50% | +7 п. п. ▲ | 1,00 | 50% | +7 п. п. ▲ |" in md
    assert "| Style 4–5 | 43% | 33% | +10 п. п. ▲ | 1,00 | 33% | +10 п. п. ▲ | ≥ 20% |" in md  # ціль із критеріїв
    assert "| Tier A на драфт | 0,43 | 0,67 | −0,24 ▲ | 1,00 | 0,67 | −0,24 ▲ |" in md  # менше — краще, тож ▲
    assert "| FB | 4 | 4 | 0 | 0 | 50% (+17 ▲) | 50% (+17 ▲) | 3,25 (+0,25 ▲) | 0,50 (−0,50 ▲) |" in md
    assert "| X | 4 | 3 | 0 | 1 | 67% (0 ≈) | 33% (0 ≈) | 3,00 (0 ≈) | 0,33 (0 ≈) |" in md  # нульову зміну не пишемо як +0
    assert "### Погіршилось (1)\n- **FB** [Title f1](https://x/f1) — з'явився фейк; Style 4 → 2 " \
           "(fact ok · Style 4 → fact not ok · Style 2)" in md
    assert "### Покращилось (2)\n- **FB** [Title f2](https://x/f2) — фейк зник; Style 2 → 4" in md
    assert "- **FB** [Title f4](https://x/f4) — драфт з'явився (відмова моделі → fact ok · Style 4)" in md
    assert "## Типи помилок\n\n| Тип помилки | Драфтів | Було |\n| --- | --- | --- |\n| factual distortion | 1 | 1 |" in md
    assert "Модель підняла прапорець «не пройшов перевірку» на 2 з 7 драфтів." in md
    assert "(точність)" in md and "прапорець мав 33%" in md
    assert f"- Суддя: `{KEY}`; оцінено 7 з 7 драфтів (100%)" in md and "p — точний знаковий тест" in md
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_times_are_shown_in_utc_whatever_the_database_session_zone_is():
    kyiv = timezone(timedelta(hours=3))
    run = {**RUN, "started_at": datetime(2026, 10, 8, 13, 0, tzinfo=kyiv),
           "finished_at": datetime(2026, 10, 8, 2, 30, tzinfo=kyiv)}
    md = hc_report.render(run, cmp.summarize_scopes(NEW), KEY)
    assert "2026-10-08 10:00 UTC → 2026-10-07 23:30 UTC" in md
    assert md.startswith("# Health Check · hc-2 · 2026-10-08")


def test_first_run_report_has_no_comparison_columns():
    summary = cmp.summarize_scopes(NEW)
    md = hc_report.render(RUN, summary, KEY, level="info")
    assert "> ℹ️ **Перший прогін: це база для наступних порівнянь**" in md
    assert "| Метрика | Зараз | Ціль |" in md and "| Style 4–5 | 43% | ≥ 20% |" in md
    assert "Погіршилось" not in md and "Попередній" not in md and "Модель: `runpod-new:draft_grounded`\n" in md
    plain = hc_report.render(RUN, summary, KEY)  # без рівня вердикту рядка-виноски нема
    assert "> " not in plain


def test_report_only_with_baseline_and_long_lists_are_cut():
    prev, baseline = _comparisons()
    many = [{"article_id": f"a{i}", "channel": "x", "title": f"Стаття [{i}] | з трубою", "url": f"https://x/{i}",
             "what": "з'явився фейк", "before": "fact ok", "after": "fact not ok"} for i in range(20)]
    prev.regressions = many
    md = hc_report.render(RUN, prev.new, KEY, prev=prev)
    assert "### Погіршилось (20)" in md and "- … і ще 5" in md and md.count("— з'явився фейк (fact ok → fact not ok)") == 15
    assert "[Стаття (3) / з трубою](https://x/3)" in md  # дужки й труба в назві не ламають markdown
    only_base = hc_report.render(RUN, baseline.new, KEY, baseline=baseline)
    assert "База (hc-0)" in only_base and "Попередній" not in only_base


# ---------- блоки Notion ----------

def test_markdown_becomes_notion_blocks():
    md = "# Заголовок\n\n> ✅ **Усе добре**\n\n- пункт з **жирним** і [посиланням](https://x/1) та `кодом`\n\n" \
         "| A | B |\n| --- | --- |\n| 1 | [t](https://x/2) |\n| 2 |  |\n\nзвичайний абзац\n"
    blocks = hc_report.to_notion_blocks(md)
    assert [b["type"] for b in blocks] == ["heading_1", "callout", "bulleted_list_item", "table", "paragraph"]
    assert blocks[1]["callout"]["icon"] == {"type": "emoji", "emoji": "✅"}
    assert blocks[1]["callout"]["rich_text"] == [{"type": "text", "text": {"content": "Усе добре"},
                                                  "annotations": {"bold": True, "code": False}}]
    bullet = blocks[2]["bulleted_list_item"]["rich_text"]
    assert [(t["text"]["content"], t["text"].get("link"), t.get("annotations")) for t in bullet] == [
        ("пункт з ", None, None), ("жирним", None, {"bold": True, "code": False}), (" і ", None, None),
        ("посиланням", {"url": "https://x/1"}, None), (" та ", None, None), ("кодом", None, {"bold": False, "code": True})]
    table = blocks[3]["table"]
    assert (table["table_width"], table["has_column_header"], len(table["children"])) == (2, True, 3)  # шапка + 2 рядки
    assert table["children"][1]["table_row"]["cells"][1][0]["text"]["link"] == {"url": "https://x/2"}
    assert table["children"][2]["table_row"]["cells"][1] == []  # порожня клітинка
    assert all(b["object"] == "block" for b in blocks)


def test_full_report_converts_to_valid_blocks():
    prev, baseline = _comparisons()
    blocks = hc_report.to_notion_blocks(hc_report.render(RUN, prev.new, KEY, prev=prev, baseline=baseline, level="ok"))
    kinds = {b["type"] for b in blocks}
    assert kinds <= {"heading_1", "heading_2", "heading_3", "paragraph", "bulleted_list_item", "callout", "table"}
    assert {"heading_1", "heading_2", "table", "callout"} <= kinds
    metrics = next(b["table"] for b in blocks if b["type"] == "table")
    assert metrics["table_width"] == 8 and len(metrics["children"]) == 1 + len(hc_report.METRICS)
    assert all(len(row["table_row"]["cells"]) == 8 for row in metrics["children"])
    long = hc_report.to_notion_blocks("x" * 4500)  # ліміт Notion: 2000 символів на фрагмент
    assert [len(t["text"]["content"]) for t in long[0]["paragraph"]["rich_text"]] == [2000, 2000, 500]


async def test_notion_page_is_only_created_in_chunks_of_100(monkeypatch):
    calls = []

    async def fake_send(client, method, url, **kwargs):
        calls.append((method, url.removeprefix(notion.NOTION_API), kwargs["json"]))
        return {"id": "page-1", "url": "https://www.notion.so/page-1"}

    monkeypatch.setattr(notion, "_send", fake_send)
    blocks = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}} for _ in range(230)]
    page = await notion.create_page("parent-1", "Звіт", blocks)
    assert page["url"] == "https://www.notion.so/page-1"
    assert [(m, u) for m, u, _ in calls] == [("POST", "/pages"), ("PATCH", "/blocks/page-1/children"),
                                             ("PATCH", "/blocks/page-1/children")]  # лише створення й дописування
    first = calls[0][2]
    assert first["parent"] == {"page_id": "parent-1"} and len(first["children"]) == 100
    assert first["properties"] == {"title": {"title": [{"text": {"content": "Звіт"}}]}}
    assert [len(c[2]["children"]) for c in calls[1:]] == [100, 30]


async def test_publish_needs_a_parent_page_and_names_the_page_after_the_report(monkeypatch):
    created = []

    async def fake_create(parent, title, blocks):
        created.append((parent, title, len(blocks)))
        return {"url": "https://www.notion.so/r"}

    monkeypatch.setattr(notion, "create_page", fake_create)
    monkeypatch.delenv("NOTION_HEALTHCHECK_PAGE_ID", raising=False)
    assert await hc_report.publish("hc-2", "# Health Check · hc-2 · 2026-10-08\n\nтекст\n", "ok") is None and created == []
    monkeypatch.setenv("NOTION_HEALTHCHECK_PAGE_ID", "3e9d1fbd-7688-81bc-8572-df2997fac612")
    url = await hc_report.publish("hc-2", "# Health Check · hc-2 · 2026-10-08\n\nтекст\n", "critical")
    assert url == "https://www.notion.so/r"
    assert created == [("3e9d1fbd768881bc8572df2997fac612", "🔴 Health Check · hc-2 · 2026-10-08", 2)]


# ---------- збереження й збирання (справжній Postgres) ----------

def test_report_roundtrip_keeps_the_alert_state(pgdb, seed_run):
    seed_run("hc-1")
    hc_report.save_report("hc-1", KEY, markdown="# v1\n", summary={"all": {"n": 8}}, base_run=None, baseline_run=None,
                          level="info", reasons=[], notion_url="https://notion/x")
    with hc_report.pg._connect() as c:
        c.execute("UPDATE hc_reports SET alert_level = 'warning', alert_at = now() WHERE run_id = 'hc-1'")
    hc_report.save_report("hc-1", KEY, markdown="# v2\n", summary={"all": {"n": 9}}, base_run="hc-0", baseline_run=None,
                          level="warning", reasons=["чому"], notion_url=None)  # перебудова звіту без публікації
    r = hc_report.get_report("hc-1", KEY)
    assert r and (r["markdown"], r["base_run"], r["level"], r["reasons"]) == ("# v2\n", "hc-0", "warning", ["чому"])
    assert r["notion_url"] == "https://notion/x" and r["alert_level"] == "warning" and r["alert_at"]  # не затерто
    assert hc_report.get_report("hc-1") and hc_report.get_report("hc-1", "чужий") is None and hc_report.get_report("нема") is None


async def test_make_report_compares_with_the_previous_run_and_stores_it(seed_run, monkeypatch):
    for name in ("HEALTHCHECK_ERROR_WARN", "HEALTHCHECK_ERROR_CRIT"):  # у засіяних даних 1 з 4 статей без відповіді моделі
        monkeypatch.setenv(name, "1")  # — це окрема тема (hc_alert); тут перевіряємо звіт
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z", model="runpod-old:draft_grounded")
    seed_run("hc-2", started_at="2026-10-08T10:00:00Z", model="runpod-new:draft_grounded")
    assert await hc_flow.make_report("нема") is None
    _judge("hc-1", {"a1": ("ok", 4), "a2": ("not ok", 2)})
    first = await hc_flow.make_report("hc-1")  # перший прогін: бази нема
    assert first and "Перший прогін" in first and "Попередній" not in first
    _judge("hc-2", {"a1": ("not ok", 2), "a2": ("not ok", 2)})
    published = []

    async def fake_publish(run_id, markdown, level):
        published.append((run_id, level))
        return "https://www.notion.so/r"

    monkeypatch.setattr(hc_report, "publish", fake_publish)
    md = await hc_flow.make_report("hc-2")
    assert md and "Попередній (hc-1)" in md and "База (" not in md  # база збігається з попереднім — другої колонки нема
    assert "модель змінилась" in md and "### Погіршилось (1)" in md and "Title a1" in md
    assert hc_report.get_report("hc-2") is None  # без save нічого не зберігається і не публікується
    await hc_flow.make_report("hc-2", save=True)
    saved = hc_report.get_report("hc-2")
    assert saved and saved["base_run"] == "hc-1" and saved["baseline_run"] is None and saved["markdown"] == md
    assert saved["notion_url"] == "https://www.notion.so/r" and published == [("hc-2", "ok")]
    await hc_flow.make_report("hc-2", save=True)  # перебудова звіту не плодить нових сторінок у Notion
    assert len(published) == 1
    await hc_flow.make_report("hc-2", save=True, republish=True)  # а навмисна — створює ще одну (стару не чіпає)
    assert len(published) == 2
    # Notion недоступний — звіт усе одно збережено
    async def broken(run_id, markdown, level):
        raise RuntimeError("Notion 404")
    monkeypatch.setattr(hc_report, "publish", broken)
    await hc_flow.make_report("hc-1", save=True)
    assert hc_report.get_report("hc-1")["notion_url"] is None


async def test_baseline_gets_its_own_column_when_it_is_not_the_previous_run(seed_run):
    for i, day in enumerate(("01", "08", "15"), 1):
        seed_run(f"hc-{i}", started_at=f"2026-10-{day}T10:00:00Z")
        _judge(f"hc-{i}", {"a1": ("ok", 4), "a2": ("ok", 4)})
    md = await hc_flow.make_report("hc-3")
    assert md and "Попередній (hc-2)" in md and "База (hc-1)" in md  # найстаріший прогін — еталон


# ---------- ендпоінти ----------

def test_report_endpoints(monkeypatch):
    stored = {"hc-2": {"markdown": "# збережений\n"}}
    built = []
    monkeypatch.setattr(cmp, "run_info", lambda rid: {"run_id": rid} if rid in ("hc-1", "hc-2") else None)
    monkeypatch.setattr(hc_report, "get_report", lambda rid, judge=None: stored.get(rid))

    async def fake_make(run_id, base_id=None, judge_key=None, **kw):
        built.append((run_id, base_id, judge_key))
        return f"# на льоту {run_id}\n"

    rebuilt = []

    async def fake_rebuild(run_id, republish=False):
        rebuilt.append((run_id, republish))

    monkeypatch.setattr(hc_flow, "make_report", fake_make)
    monkeypatch.setattr(hc_flow, "rebuild_report", fake_rebuild)
    secret = {"x-webhook-secret": "t"}
    with TestClient(app) as c:
        assert c.get("/health-check/runs/нема/report").status_code == 404
        r = c.get("/health-check/runs/hc-2/report")
        assert (r.status_code, r.text, r.headers["content-type"]) == (200, "# збережений\n", "text/markdown; charset=utf-8")
        assert c.get("/health-check/runs/hc-1/report").text == "# на льоту hc-1\n"  # збереженого нема — збирається на льоту
        assert c.get("/health-check/runs/hc-2/report?fresh=true").text == "# на льоту hc-2\n"
        assert c.get("/health-check/runs/hc-2/report?base=hc-1&judge=k").text == "# на льоту hc-2\n"
        assert built == [("hc-1", None, None), ("hc-2", None, None), ("hc-2", "hc-1", "k")]
        assert c.post("/health-check/runs/hc-2/report").status_code == 401
        assert c.post("/health-check/runs/нема/report", headers=secret).status_code == 404
        r = c.post("/health-check/runs/hc-2/report", headers=secret)
        assert (r.status_code, r.json()["report"]) == (202, "/health-check/runs/hc-2/report")
        assert c.post("/health-check/runs/hc-2/report?republish=true", headers=secret).status_code == 202
        monkeypatch.setattr(hc_flow, "is_busy", lambda rid: True)
        assert c.post("/health-check/runs/hc-2/report", headers=secret).status_code == 409
    assert rebuilt == [("hc-2", False), ("hc-2", True)]


@pytest.mark.parametrize("judge, evaluate_missing, key", [(None, True, None), ("свій", False, "свій")])
async def test_after_run_builds_the_report_once_the_evaluation_is_done(monkeypatch, judge, evaluate_missing, key):
    from app import hc_eval

    order = []

    async def fake_eval(run_id, j=None, *, force=False):
        order.append("evaluate")

    async def fake_report(run_id, base_id=None, judge_key=None, **kw):
        order.append(("report", judge_key, kw))

    monkeypatch.setattr(hc_eval, "evaluate_run", fake_eval)
    monkeypatch.setattr(hc_flow, "make_report", fake_report)
    fake_judge = type("J", (), {"key": "свій"})() if judge else None
    await hc_flow.after_run("hc-1", judge=fake_judge)
    assert order == ["evaluate", ("report", key, {"evaluate_missing": evaluate_missing, "save": True})]
