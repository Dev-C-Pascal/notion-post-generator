"""Health Check, пункт 7: звіт — вигляд (render) з вердиктом, блоки Notion, рядок таблиці прогонів у Notion, збереження,
збирання з бази й ендпоінти."""
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
from app import hc_flow, hc_report, hc_verdict, notion  # noqa: E402
from app.main import app  # noqa: E402

RUN = {"run_id": "hc-2", "trigger": "schedule", "model_version": "runpod-new:draft_grounded", "status": "done",
       "started_at": datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc),
       "finished_at": datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc), "note": None}
KEY = "claude-haiku-5-5/medium:test"
NEW_WITH_TYPES = [dataclasses.replace(r, failure_types=["factual distortion"] if r.article_id == "f1" else
                                      ["weak structure"] if r.article_id == "x2" else r.failure_types) for r in NEW]


@pytest.fixture(autouse=True)
def _verdict_env(monkeypatch):
    for name in ("HEALTHCHECK_THRESHOLD_PP", "HEALTHCHECK_NOISE_PP", "HEALTHCHECK_WARN_PP", "HEALTHCHECK_ALPHA_WARN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hc_verdict, "ERROR_FAIL", 1.0)  # у синтетичних даних 1 з 8 статей без відповіді моделі — не про це


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
    v = hc_verdict.decide(prev.new, prev, baseline, run_id="hc-2")
    md = hc_report.render(RUN, prev.new, KEY, verdict=v, base=prev, baseline=baseline)
    # «можна публікувати» від усіх 8 статей: 2 → 3 (+12,5 п. п.) — пройшла; 1 з 8 без відповіді моделі — застереження
    assert md.startswith("# Health Check · hc-2 · 2026-10-08\n\n> ✅ **Модель пройшла**\n\n- Можна публікувати: 38% проти "
                         "25% в останньому успішному прогоні hc-1 (+12 п. п.) — не впало\n"
                         "- ⚠️ Модель не відповіла на 1 з 8 статей (збій чи таймаут RunPod)")
    assert "- Модель: `runpod-new:draft_grounded` (у базі було `runpod-old:draft_grounded`: модель змінилась)" in md
    assert "- Причина запуску: за розкладом" in md
    assert "- Запуск: schedule · 2026-10-08 10:00 UTC → 2026-10-08 12:30 UTC · статус done" in md
    assert "- Статей у наборі: 8 (FB 4, X 4); драфт отримали 7, відмов моделі 0, збоїв 1" in md
    assert "## Застереження\n\n- hc-2: 3 драфтів без оцінки" in md
    assert "| Метрика | Зараз | Останній успішний (hc-1) | Зміна | p | Еталон (hc-0) | Зміна | Ціль |" in md
    assert "| **Можна публікувати (fact ok + Style 4–5)** | 38% | 25% | +12 п. п. ▲ | 1,00 | 25% | +12 п. п. ▲ |  |" in md
    assert "| FB | 4 | 4 | 0 | 0 | 50% (+25 ▲) | 50% (+17 ▲) | 50% (+17 ▲) | 3,25 (+0,25 ▲) |" in md
    assert "| Fact safety = ok | 57% | 50% | +7 п. п. ▲ | 1,00 | 50% | +7 п. п. ▲ |" in md
    assert "| Style 4–5 | 43% | 33% | +10 п. п. ▲ | 1,00 | 33% | +10 п. п. ▲ | ≥ 20% |" in md  # ціль із критеріїв
    assert "| Tier A на драфт | 0,43 | 0,67 | −0,24 ▲ | 1,00 | 0,67 | −0,24 ▲ |" in md  # менше — краще, тож ▲
    assert "### Були «можна публікувати», стали «ні» (1)\n- **FB** [Title f1](https://x/f1)" in md
    assert "### Були «ні», стали «можна публікувати» (2)" in md
    assert "### Інші погіршення" not in md  # f1 уже показано вище — двічі не повторюємо
    assert "Які помилки стали частішими" not in md  # розбір причин — лише коли модель не пройшла
    assert "## Типи помилок\n\n| Тип помилки | Драфтів | Було |\n| --- | --- | --- |\n| factual distortion | 1 | 1 |" in md
    assert "Модель підняла прапорець «не пройшов перевірку» на 2 з 7 драфтів." in md
    assert f"- Суддя: `{KEY}`; оцінено 7 з 7 драфтів (100%)" in md and "p — точний знаковий тест" in md
    assert "Суддю ще не звірено з людськими оцінками" in md
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_a_failed_model_report_says_what_got_worse_and_where():
    worse = cmp.compare("hc-1", NEW_WITH_TYPES, "hc-2", BASE)  # навпаки: 50% → 25%
    worse.meta = {"base": {"model_version": "runpod-new:draft_grounded"}, "new": RUN, "judge": KEY}
    v = hc_verdict.decide(worse.new, worse, run_id="hc-2")
    assert v.status == hc_verdict.FAILED and v.delta_pp == pytest.approx(-12.5)
    md = hc_report.render({**RUN, "note": "змінили промпт"}, worse.new, KEY, verdict=v, base=worse,
                          calibration={"agreement": 0.84, "target": 0.8, "decided": 280,
                                       "created_at": datetime(2026, 10, 20, tzinfo=timezone.utc)})
    assert md.startswith("# Health Check · hc-2 · 2026-10-08\n\n> ❌ **Модель не пройшла**\n\n- Можна публікувати: 25% проти "
                         "38% в останньому успішному прогоні hc-1 (−12 п. п.) — впало більше за поріг 5 п. п.\n")
    assert "- Причина запуску: змінили промпт" in md and "— та сама, що в базі" in md
    assert "### Були «можна публікувати», стали «ні» (2)" in md and "[Title f2](https://x/f2)" in md
    assert "- **FB** [Title f4](https://x/f4) — відмова моделі (fact ok · Style 4 → відмова моделі)" in md
    assert "### Які помилки стали частішими" in md and "| weak structure | 1 | 3 | +2 |" in md
    assert "Інші погіршення" not in md and "Інші покращення" not in md
    assert "- Звірка судді з людськими оцінками (2026-10-20): збіг по fact safety 84% на 280 драфтах, ціль ≥ 80% — " \
           "досягнуто." in md


def test_times_are_shown_in_utc_whatever_the_database_session_zone_is():
    kyiv = timezone(timedelta(hours=3))
    run = {**RUN, "started_at": datetime(2026, 10, 8, 13, 0, tzinfo=kyiv),
           "finished_at": datetime(2026, 10, 8, 2, 30, tzinfo=kyiv)}
    summary = cmp.summarize_scopes(NEW)
    md = hc_report.render(run, summary, KEY, verdict=hc_verdict.decide(summary, None))
    assert "2026-10-08 10:00 UTC → 2026-10-07 23:30 UTC" in md
    assert md.startswith("# Health Check · hc-2 · 2026-10-08")


def test_first_run_report_is_the_baseline_without_comparison_columns():
    summary = cmp.summarize_scopes(NEW)
    md = hc_report.render(RUN, summary, KEY, verdict=hc_verdict.decide(summary, None))
    assert "> 🔵 **Перший прогін: це база для наступних порівнянь**" in md
    assert "- Можна публікувати: 38%. Це перший успішний прогін — з ним порівнюватимемо наступні." in md
    assert "| Метрика | Зараз | Ціль |" in md and "| Style 4–5 | 43% | ≥ 20% |" in md
    assert "Що змінилось по статтях" not in md and "Останній успішний" not in md


def test_long_lists_are_cut_and_titles_do_not_break_markdown():
    prev, baseline = _comparisons()
    many = [{"article_id": f"a{i}", "channel": "x", "title": f"Стаття [{i}] | з трубою", "url": f"https://x/{i}",
             "what": "з'явився фейк", "before": "fact ok", "after": "fact not ok"} for i in range(20)]
    prev.regressions = many
    md = hc_report.render(RUN, prev.new, KEY, verdict=hc_verdict.decide(prev.new, prev), base=prev)
    assert "### Інші погіршення (20)" in md and "- … і ще 10" in md
    assert md.count("— з'явився фейк (fact ok → fact not ok)") == hc_report.SHOWN_REGRESSIONS
    assert "[Стаття (3) / з трубою](https://x/3)" in md  # дужки й труба в назві не ламають markdown
    only_base = hc_report.render(RUN, baseline.new, KEY, verdict=hc_verdict.decide(baseline.new, None, baseline),
                                 baseline=baseline)
    assert "Еталон (hc-0)" in only_base and "Останній успішний" not in only_base


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
    v = hc_verdict.decide(prev.new, prev, baseline)
    blocks = hc_report.to_notion_blocks(hc_report.render(RUN, prev.new, KEY, verdict=v, base=prev, baseline=baseline))
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
    assert [len(c[2]["children"]) for c in calls[1:]] == [100, 30]


# ---------- Notion: таблиця прогонів ----------

def _failed_verdict() -> tuple[dict, hc_verdict.Verdict]:
    worse = cmp.compare("hc-1", NEW_WITH_TYPES, "hc-2", BASE)
    return worse.new, hc_verdict.decide(worse.new, worse)


def test_row_properties_carry_date_model_rate_delta_and_verdict():
    summary, v = _failed_verdict()
    props = hc_report.row_properties({**RUN, "note": "змінили промпт"}, summary, v)
    assert props[hc_report.TITLE_PROP] == {"title": notion.rich_text("2026-10-08 · hc-2")}
    assert props["Дата"] == {"date": {"start": "2026-10-08T10:00:00+00:00"}}
    assert props["Версія моделі"] == {"rich_text": notion.rich_text("runpod-new:draft_grounded")}
    assert props["Можна публікувати"] == {"number": 0.25} and props["Різниця, п. п."] == {"number": -12.5}
    assert props["Вердикт"] == {"select": {"name": "❌ Не пройшла"}} and props["Поріг, п. п."] == {"number": 5.0}
    assert props["База"] == {"rich_text": notion.rich_text("hc-1")}
    assert props["Причина запуску"] == {"rich_text": notion.rich_text("змінили промпт")}
    assert (props["Драфтів"], props["Відмов і збоїв"]) == ({"number": 6}, {"number": 2})
    schema = hc_report.database_schema()
    for name, prop in props.items():  # кожна властивість рядка є в схемі таблиці з тим самим типом
        assert next(iter(schema[name])) == next(iter(prop)), name
    first = hc_report.row_properties(RUN, cmp.summarize_scopes(NEW), hc_verdict.decide(cmp.summarize_scopes(NEW), None))
    assert "Різниця, п. п." not in first and "База" not in first  # першому прогону порівнювати нема з чим
    assert first["Вердикт"] == {"select": {"name": "🔵 База"}}


async def test_fit_properties_adapts_to_a_table_the_team_made_itself(monkeypatch):
    async def fake_db(database_id):
        return {"properties": {"Name": {"type": "title"}, "Вердикт": {"type": "select"}, "Дата": {"type": "date"},
                               "Можна публікувати": {"type": "rich_text"}}}  # тип інший — не пишемо

    monkeypatch.setattr(notion, "get_database", fake_db)
    summary, v = _failed_verdict()
    props = await hc_report.fit_properties("db", hc_report.row_properties(RUN, summary, v))
    assert sorted(props) == ["Name", "Вердикт", "Дата"] and props["Name"]["title"][0]["text"]["content"].endswith("hc-2")


async def test_publish_creates_the_table_once_and_adds_a_row_per_run(pgdb, monkeypatch):
    from app import pg

    pg.init_db()
    created, rows = [], []

    async def fake_create_db(parent, title, properties):
        created.append((parent, title, sorted(properties)))
        return {"id": "db-1111-2222"}

    async def fake_create_row(database_id, properties, body=None, extra_blocks=None):
        rows.append((database_id, properties["Вердикт"], extra_blocks[0]["type"]))
        return {"url": f"https://www.notion.so/row-{len(rows)}"}

    monkeypatch.setattr(notion, "create_database", fake_create_db)
    monkeypatch.setattr(notion, "create_row", fake_create_row)
    monkeypatch.delenv("NOTION_HEALTHCHECK_DATABASE_ID", raising=False)
    monkeypatch.delenv("NOTION_HEALTHCHECK_PAGE_ID", raising=False)
    summary, v = _failed_verdict()
    md = hc_report.render(RUN, summary, KEY, verdict=v)
    assert await hc_report.publish(RUN, md, v, summary) is None and created == []  # не налаштовано — нічого не робимо
    monkeypatch.setenv("NOTION_HEALTHCHECK_PAGE_ID", "3e9d1fbd-7688-81bc-8572-df2997fac612")
    assert await hc_report.publish(RUN, md, v, summary) == "https://www.notion.so/row-1"
    assert await hc_report.publish({**RUN, "run_id": "hc-3"}, md, v, summary) == "https://www.notion.so/row-2"
    assert created == [("3e9d1fbd768881bc8572df2997fac612", hc_report.DB_TITLE, sorted(hc_report.database_schema()))]
    assert pg.get_state(hc_report.DB_STATE_KEY) == "db11112222"  # таблицю запам'ятали — друга не створюється
    assert rows == [("db11112222", {"select": {"name": "❌ Не пройшла"}}, "callout")] * 2  # тіло рядка — звіт без заголовка


# ---------- збереження й збирання (справжній Postgres) ----------

def test_report_roundtrip_keeps_the_alert_state(seed_run):
    seed_run("hc-1")
    v = hc_verdict.Verdict(hc_verdict.BASELINE, "перший")
    hc_report.save_report("hc-1", KEY, markdown="# v1\n", summary={"all": {"n": 8}}, base_run=None, baseline_run=None,
                          verdict=v, notion_url="https://notion/x")
    with hc_report.pg._connect() as c:
        c.execute("UPDATE hc_reports SET alert_verdict = 'baseline', alert_at = now() WHERE run_id = 'hc-1'")
    failed = hc_verdict.Verdict(hc_verdict.FAILED, "чому", value=0.2, base_value=0.3)
    hc_report.save_report("hc-1", KEY, markdown="# v2\n", summary={"all": {"n": 9}}, base_run="hc-0", baseline_run=None,
                          verdict=failed, notion_url=None)  # перебудова звіту без публікації
    r = hc_report.get_report("hc-1", KEY)
    assert r and (r["markdown"], r["base_run"], r["verdict"]) == ("# v2\n", "hc-0", "failed")
    assert r["verdict_info"]["reason"] == "чому" and r["verdict_info"]["delta_pp"] == pytest.approx(-10)
    assert r["notion_url"] == "https://notion/x" and r["alert_verdict"] == "baseline" and r["alert_at"]  # не затерто
    assert hc_report.get_report("hc-1") and hc_report.get_report("hc-1", "чужий") is None and hc_report.get_report("нема") is None


async def test_make_report_compares_with_the_last_successful_run_and_stores_it(seed_run, monkeypatch):
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z", model="runpod-old:draft_grounded")
    seed_run("hc-2", started_at="2026-10-08T10:00:00Z", model="runpod-new:draft_grounded")
    assert await hc_flow.make_report("нема") is None
    published = []

    async def fake_publish(run, markdown, verdict, summary):
        published.append((run["run_id"], verdict.status))
        return "https://www.notion.so/r"

    monkeypatch.setattr(hc_report, "publish", fake_publish)
    _judge("hc-1", {"a1": ("ok", 4), "a2": ("not ok", 2)})
    first = await hc_flow.make_report("hc-1", save=True)  # перший прогін: бази нема, він сам стає базою
    assert first and "Перший прогін" in first and "Останній успішний" not in first
    _judge("hc-2", {"a1": ("not ok", 2), "a2": ("not ok", 2)})
    md = await hc_flow.make_report("hc-2")
    assert md and "Останній успішний (hc-1)" in md and "Еталон (" not in md  # еталон збігається з базою — колонки нема
    assert "модель змінилась" in md and "### Були «можна публікувати», стали «ні» (1)" in md and "Title a1" in md
    assert hc_report.get_report("hc-2") is None  # без save нічого не зберігається і не публікується
    await hc_flow.make_report("hc-2", save=True)
    saved = hc_report.get_report("hc-2")
    assert saved and saved["base_run"] == "hc-1" and saved["baseline_run"] is None and saved["markdown"] == md
    assert saved["verdict"] == "failed" and saved["notion_url"] == "https://www.notion.so/r"
    assert published == [("hc-1", "baseline"), ("hc-2", "failed")]
    await hc_flow.make_report("hc-2", save=True)  # перебудова звіту не плодить нових рядків у Notion
    assert len(published) == 2
    await hc_flow.make_report("hc-2", save=True, republish=True)  # а навмисна — створює ще один (старий не чіпає)
    assert len(published) == 3

    async def broken(run, markdown, verdict, summary):  # Notion недоступний — звіт усе одно збережено
        raise RuntimeError("Notion 404")

    monkeypatch.setattr(hc_report, "publish", broken)
    monkeypatch.setattr(hc_flow.hc_alert, "notify_problem", lambda *a, **k: _none())
    seed_run("hc-3", started_at="2026-10-15T10:00:00Z")
    await hc_flow.make_report("hc-3", save=True)
    assert hc_report.get_report("hc-3")["notion_url"] is None


async def _none() -> dict:
    return {}


async def test_baseline_gets_its_own_column_when_it_is_not_the_last_successful_run(seed_run):
    for i, day in enumerate(("01", "08", "15"), 1):
        seed_run(f"hc-{i}", started_at=f"2026-10-{day}T10:00:00Z")
        _judge(f"hc-{i}", {"a1": ("ok", 4), "a2": ("ok", 4)})
        await hc_flow.make_report(f"hc-{i}", save=True)
    md = hc_report.get_report("hc-3")["markdown"]
    assert "Останній успішний (hc-2)" in md and "Еталон (hc-1)" in md  # найстаріший прогін — еталон


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
