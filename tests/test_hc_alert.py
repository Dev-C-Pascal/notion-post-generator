"""Health Check, пункт 6: повідомлення після кожного прогону — текст, режим «лише збої», одне на вердикт, збій самого Health
Check, супровід після перезапуску і наскрізний сценарій двох прогонів (модель → суддя → вердикт → Notion → повідомлення)."""
import os

os.environ.setdefault("NOTION_TOKEN", "secret_xxx")
os.environ.setdefault("WEBHOOK_SECRET", "t")
os.environ.setdefault("NOTION_DATABASE_ID", "0" * 32)
os.environ.setdefault("RUNPOD_ENDPOINT_ID", "test")
os.environ.setdefault("RUNPOD_API_KEY", "test")
os.environ.setdefault("COMMS_DATABASE_URL", "")

import json  # noqa: E402
import logging  # noqa: E402

import anthropic  # noqa: E402
import httpx2  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_hc_compare import _judge  # noqa: E402  (той самий модуль, що вже завантажив pytest)

from app import hc_alert, hc_eval, hc_flow, hc_report, hc_verdict, healthcheck, notify  # noqa: E402
from app import hc_compare as cmp  # noqa: E402
from app.hc_compare import Row  # noqa: E402
from app.hc_verdict import BASELINE, FAILED, PASSED, UNKNOWN, Verdict  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Draft  # noqa: E402

KEY = "claude-haiku-5-5/medium:test"
RUN = {"run_id": "hc-2", "trigger": "manual", "model_version": "runpod-new:draft_grounded", "note": None}
CHANNEL_VARS = ("HEALTHCHECK_ALERT_WEBHOOK", "HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", "HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID",
                "HEALTHCHECK_ALERT_TELEGRAM_THREAD_ID", "HEALTHCHECK_ALERT_MODE")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in CHANNEL_VARS + ("HEALTHCHECK_THRESHOLD_PP", "HEALTHCHECK_NOISE_PP", "NOTION_HEALTHCHECK_PAGE_ID",
                                "NOTION_HEALTHCHECK_DATABASE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hc_alert, "_recent", {})


def _webhook(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")


def failed() -> Verdict:
    return Verdict(FAILED, "Можна публікувати: 40% проти 50% в останньому успішному прогоні hc-1 (−10 п. п.) — впало більше "
                           "за поріг 5 п. п.", value=0.4, base_value=0.5, base_run="hc-1",
                   problems=["Суддя не зміг оцінити 2 драфтів"], observations=[f"спостереження {i}" for i in range(5)])


# ---------- текст ----------

def test_message_has_verdict_rate_model_reason_and_report():
    base = [Row(f"a{i}", "fb", draft="ok", eval="EVALUATED", fact_safety="ok", style=4) for i in range(4)]
    new = [Row("a0", "fb", draft="ok", eval="EVALUATED", fact_safety="not ok", tier_a=1, style=2), *base[1:]]
    c = cmp.compare("hc-1", base, "hc-2", new)
    text = hc_alert.message(failed(), {**RUN, "note": "змінили промпт"}, "https://notion.so/r", summary=c.new, base=c)
    assert text.splitlines() == [
        "❌ Health Check hc-2: модель не пройшла",
        "Можна публікувати: 40% проти 50% в останньому успішному прогоні hc-1 (−10 п. п.) — впало більше за поріг 5 п. п.",
        "Fact safety ok 75% · Style 4–5 75% · Style (сер.) 3,5 · драфтів 4 з 4",
        "Статті, що змінили «можна публікувати»: −1 / +0 (список у звіті)",
        "⚠️ Суддя не зміг оцінити 2 драфтів",
        "• спостереження 0", "• спостереження 1", "• спостереження 2", "• … і ще 2",
        "Модель: runpod-new:draft_grounded", "Запуск: змінили промпт", "Звіт: https://notion.so/r"]
    passed = hc_alert.message(Verdict(PASSED, "без змін"), {**RUN, "trigger": "schedule"}, None)
    assert passed.splitlines() == ["✅ Health Check hc-2: модель пройшла", "без змін", "Модель: runpod-new:draft_grounded",
                                   "Запуск: за розкладом", "Звіт: GET /health-check/runs/hc-2/report (з сервера)"]
    assert hc_alert.message(Verdict(BASELINE, "перший"), RUN, None).startswith(
        "🔵 Health Check hc-2: перший прогін, це база для порівнянь\n")


# ---------- доставка: одне повідомлення на вердикт ----------

async def test_every_run_is_reported_once_per_verdict(seed_run, monkeypatch, outbox):
    seed_run("hc-2")
    hc_report.save_report("hc-2", KEY, markdown="# r\n", summary={}, base_run="hc-1", baseline_run=None,
                          verdict=Verdict(UNKNOWN, "мало оцінок"))
    _webhook(monkeypatch)
    outbox["status"] = 503  # канал недоступний — стан не записуємо, щоб наступна спроба надіслала ще раз
    assert await hc_alert.deliver("hc-2", KEY, Verdict(UNKNOWN, "мало оцінок"), RUN, None) == {"webhook": False}
    assert hc_alert.sent_verdict("hc-2", KEY) is None
    outbox["status"] = 200
    assert await hc_alert.deliver("hc-2", KEY, Verdict(UNKNOWN, "мало оцінок"), RUN, None) == {"webhook": True}
    assert hc_alert.sent_verdict("hc-2", KEY) == UNKNOWN
    assert await hc_alert.deliver("hc-2", KEY, Verdict(UNKNOWN, "мало оцінок"), RUN, None) is None  # уже сповістили
    # дооцінили — вердикт змінився: ще одне повідомлення, і воно з новим вердиктом
    assert await hc_alert.deliver("hc-2", KEY, Verdict(PASSED, "ок"), RUN, "https://notion.so/r") == {"webhook": True}
    assert hc_alert.sent_verdict("hc-2", KEY) == PASSED
    assert await hc_alert.deliver("hc-2", KEY, Verdict(PASSED, "ок"), RUN, None) is None
    assert await hc_alert.deliver("hc-2", KEY, Verdict(PASSED, "ок"), RUN, None, force=True) == {"webhook": True}
    bodies = [b for _, b in outbox["requests"]]
    assert [b["verdict"] for b in bodies] == [UNKNOWN, UNKNOWN, PASSED, PASSED]
    assert bodies[2]["text"].startswith("✅ Health Check hc-2: модель пройшла\n") and bodies[2]["report_url"] == \
        "https://notion.so/r"


async def test_failed_mode_keeps_quiet_about_good_runs(seed_run, monkeypatch, outbox):
    seed_run("hc-2")
    hc_report.save_report("hc-2", KEY, markdown="# r\n", summary={}, base_run=None, baseline_run=None,
                          verdict=Verdict(PASSED, "ок"))
    _webhook(monkeypatch)
    monkeypatch.setenv("HEALTHCHECK_ALERT_MODE", "failed")
    assert await hc_alert.deliver("hc-2", KEY, Verdict(PASSED, "ок"), RUN, None) is None
    assert await hc_alert.deliver("hc-2", KEY, Verdict(BASELINE, "перший"), RUN, None) is None
    assert await hc_alert.deliver("hc-2", KEY, failed(), RUN, None) == {"webhook": True}
    assert len(outbox["requests"]) == 1


async def test_without_channels_it_only_logs(seed_run, outbox, caplog):
    seed_run("hc-2")
    hc_report.save_report("hc-2", KEY, markdown="# r\n", summary={}, base_run=None, baseline_run=None, verdict=failed())
    with caplog.at_level(logging.WARNING):
        sent = await hc_alert.deliver("hc-2", KEY, failed(), RUN, None)
    assert sent == {} and "модель не пройшла" in caplog.text and hc_alert.sent_verdict("hc-2", KEY) is None
    assert outbox["requests"] == []


async def test_a_failure_of_health_check_itself_is_reported_but_not_repeated(monkeypatch, outbox):
    _webhook(monkeypatch)
    first = await hc_alert.notify_problem("Health Check hc-2 не завершився", "RuntimeError: RunPod 500")
    assert first == {"webhook": True}
    assert outbox["requests"][0][1]["text"] == "🔴 Health Check hc-2 не завершився\nRuntimeError: RunPod 500"
    again = await hc_alert.notify_problem("Health Check hc-2 не завершився", "RuntimeError: RunPod 500")
    assert again == {}  # те саме повідомлення — не частіше, ніж раз на PROBLEM_REPEAT_S
    assert await hc_alert.notify_problem("Health Check hc-3 не завершився") == {"webhook": True}  # інше — одразу
    monkeypatch.setattr(hc_alert, "PROBLEM_REPEAT_S", 0)
    assert await hc_alert.notify_problem("Health Check hc-2 не завершився", "RuntimeError: RunPod 500") == {"webhook": True}


async def test_after_run_and_failed_run_report_the_problem(monkeypatch):
    from test_healthcheck import REFS, FakeModel, FakeStore

    problems = []

    async def fake_problem(subject, details=""):
        problems.append((subject, details))
        return {}

    monkeypatch.setattr(hc_alert, "notify_problem", fake_problem)

    async def broken_eval(run_id, judge=None, *, force=False):
        raise hc_eval.JudgeFatal("AuthenticationError: invalid x-api-key")

    monkeypatch.setattr(hc_eval, "evaluate_run", broken_eval)
    await hc_flow.after_run("hc-1")
    assert problems == [("Health Check hc-1: оцінка чи звіт не завершились",
                         "JudgeFatal: AuthenticationError: invalid x-api-key")]

    store = FakeStore(REFS[:1])
    store.install(monkeypatch)
    monkeypatch.setattr(healthcheck, "reference_articles", lambda: (_ for _ in ()).throw(RuntimeError("база недоступна")))
    run_id, model = healthcheck.start("manual", FakeModel())
    await healthcheck.run(run_id, model)
    assert store.finished == [(run_id, "failed", "RuntimeError: база недоступна")]
    assert problems[-1] == (f"Health Check {run_id} не завершився", "RuntimeError: база недоступна")


# ---------- супровід після перезапуску ----------

async def test_followups_finish_what_a_restart_interrupted(seed_run, monkeypatch):
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z")
    seed_run("hc-2", started_at="2026-10-08T10:00:00Z")
    seed_run("hc-3", started_at="2026-10-15T10:00:00Z", status="interrupted")
    ran = []

    async def fake_after(run_id, **kw):
        ran.append(run_id)

    monkeypatch.setattr(hc_flow, "after_run", fake_after)
    monkeypatch.setattr(hc_eval, "enabled", lambda: False)
    await hc_flow.resume_followups()
    assert ran == []  # оцінка вимкнена — нічого не робимо
    monkeypatch.setattr(hc_eval, "enabled", lambda: True)
    assert hc_flow.followup_pending("hc-2")
    await hc_flow.resume_followups()
    assert ran == ["hc-2"]  # останній ЗАВЕРШЕНИЙ прогін; обірваний hc-3 чекає свого resume
    _judge("hc-2", {"a1": ("ok", 4), "a2": ("ok", 4)})
    assert hc_flow.followup_pending("hc-2")  # оцінки є, звіту ще нема
    hc_report.save_report("hc-2", hc_eval.current_judge_key(), markdown="# r\n", summary={}, base_run=None, baseline_run=None,
                          verdict=Verdict(BASELINE, "перший"))
    assert not hc_flow.followup_pending("hc-2")
    await hc_flow.resume_followups()
    assert ran == ["hc-2"]


async def test_followups_do_not_pay_again_for_drafts_the_judge_already_failed_on(seed_run):
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z")
    key = hc_eval.current_judge_key()
    for case in hc_eval.pending_cases("hc-1", key):  # суддя збоїв на обох драфтах
        hc_eval.save_eval(hc_eval._row(case, key, outcome="ERROR", error="ParseError: немає блоку RESULT"))
    hc_report.save_report("hc-1", key, markdown="# r\n", summary={}, base_run=None, baseline_run=None,
                          verdict=Verdict(UNKNOWN, "нічого не оцінено"))
    assert not hc_flow.followup_pending("hc-1")  # звіт є, спроби були — щогодини той самий платний цикл не запускаємо
    assert cmp.pending_evals("hc-1", key) == 2  # а для порівняння вони й надалі недооцінені


def test_alert_test_endpoint_shows_why_a_channel_failed(monkeypatch):
    async def fake_send(text, **extra):
        return {"telegram": {"ok": False, "error": "Telegram: Bad Request: chat not found (400)"}}

    monkeypatch.setattr(notify, "send_detailed", fake_send)
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID", "-100")
    with TestClient(app) as c:
        assert c.post("/health-check/alert-test").status_code == 401
        r = c.post("/health-check/alert-test", headers={"x-webhook-secret": "t"})
    assert r.json() == {"channels": ["telegram"], "sent": {"telegram": False},
                        "errors": {"telegram": "Telegram: Bad Request: chat not found (400)"}}


# ---------- наскрізно: два прогони моделі → оцінка → вердикт → рядок у Notion → повідомлення ----------

class ScriptedModel:
    """«Модель», що віддає наперед заготовлені тексти: ними керуємо, що саме погіршилось між версіями."""

    def __init__(self, version: str, texts: dict[str, str]):
        self.version, self.texts = version, texts

    def draft(self, article, lang=None):
        return Draft(article_id=article.id, headline=article.title, text=self.texts[str(article.id)],
                     model_version=self.version, lang=lang or "en")


def _judge_result(bad: bool) -> str:
    fact, tier_a, style = ("not ok", 1, 2) if bad else ("ok", 0, 4)
    return (f"RESULT\noutcome: EVALUATED\nrelevance: yes\nfact_safety: {fact}\ntier_a: {tier_a}\ntier_b: 0\nunverifiable: 0\n"
            f"pipeline_flags: confirmed 0, false_alarm 0\nsource_captured: yes\nstyle: {style}\n"
            f"defects: major {int(bad)}, minor 0, recurring 0\nfailure_types: {'factual distortion' if bad else '-'}\n"
            f"status_written: {'Approved after edit' if bad else 'Approved as-is'}\n\nCLAIMS\n-\n\nDEFECTS\n-\n\n"
            "CORRECTED POST\n-\n\nCOMMENT\n-\n")


async def test_two_runs_end_to_end(seed_run, outbox, monkeypatch):
    """Справжні: Postgres, SDK Anthropic (підроблено лише мережу), увесь код Health Check. Підроблені: модель драфтів,
    відповіді судді, Notion і Telegram."""
    seed_run("hc-0", status="failed")  # лише щоб засіяти 4 еталонні статті; сам прогін не завершений і базою не стає
    from app import pg

    pg.init_db()  # sync_state: id таблиці прогонів у Notion
    for name, value in (("ANTHROPIC_API_KEY", "sk-test"), ("HEALTHCHECK_EVAL", "1"), ("NOTION_HEALTHCHECK_PAGE_ID", "p" * 32),
                        ("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", "123:secret"),
                        ("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID", "-100")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(healthcheck, "WAIT_S", 0.01)
    api_calls = []

    def anthropic_handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        user = body["messages"][0]["content"]
        api_calls.append(user.split("PAGE: ")[1].split("\n")[0])
        bad = "ПОГАНИЙ" in user.split("Тіло сторінки:")[1]  # лише в драфті, не в тексті статті
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "stop_reason": "end_turn",
            "stop_sequence": None, "content": [{"type": "text", "text": _judge_result(bad)}],
            "usage": {"input_tokens": 900, "output_tokens": 1500, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 20000}})

    sdk = anthropic.Anthropic(api_key="sk-test", http_client=anthropic.DefaultHttpxClient(
        transport=httpx2.MockTransport(anthropic_handler)))
    monkeypatch.setattr(hc_eval, "get_judge", lambda model=None: hc_eval.AnthropicJudge(client=sdk, model=model))
    tables, rows = [], []

    async def fake_create_database(parent, title, properties):
        tables.append(title)
        return {"id": "db-1"}

    async def fake_create_row(database_id, properties, body=None, extra_blocks=None):
        rows.append(properties)
        return {"url": f"https://www.notion.so/row-{len(rows)}"}

    monkeypatch.setattr(hc_report.notion, "create_database", fake_create_database)
    monkeypatch.setattr(hc_report.notion, "create_row", fake_create_row)

    good = {"a1": "Нормальний пост 1", "a2": "Нормальний пост 2", "x1": "Normal thread 1", "x2": "Normal thread 2"}
    first_model = ScriptedModel("runpod-ep:v1", good)
    first_id, _ = healthcheck.start("schedule", first_model)
    await healthcheck.run(first_id, first_model)

    first = hc_report.get_report(first_id)
    assert first and first["verdict"] == BASELINE and first["base_run"] is None  # перший прогін — це база
    assert len(api_calls) == 4 and tables == [hc_report.DB_TITLE] and len(rows) == 1
    assert rows[0]["Вердикт"] == {"select": {"name": "🔵 База"}} and rows[0]["Можна публікувати"] == {"number": 1.0}
    (url, body), = outbox["requests"]  # повідомлення приходить і про перший прогін
    assert url == "https://api.telegram.org/bot123:secret/sendMessage" and body["chat_id"] == "-100"
    assert body["text"].startswith(f"🔵 Health Check {first_id}: перший прогін, це база для порівнянь\n")
    assert "Запуск: за розкладом" in body["text"] and body["text"].endswith("Звіт: https://www.notion.so/row-1")

    worse = dict(good, a2="ПОГАНИЙ пост 2", x1="ПОГАНИЙ thread 1")  # нова версія моделі зіпсувала дві статті з чотирьох
    second_model = ScriptedModel("runpod-ep:v2", worse)
    second_id, _ = healthcheck.start("manual", second_model, note="змінили промпт")
    await healthcheck.run(second_id, second_model)

    assert len(api_calls) == 6  # дві незмінені статті взято з минулого прогону — за них не платимо вдруге
    assert sorted(c.split("/")[-1] for c in api_calls[4:]) == ["a2", "x1"]
    report = hc_report.get_report(second_id)
    assert report and report["verdict"] == FAILED and report["base_run"] == first_id and report["alert_verdict"] == FAILED
    md = report["markdown"]
    assert md.startswith(f"# Health Check · {second_id} · ") and "> ❌ **Модель не пройшла**" in md
    assert "(у базі було `runpod-ep:v1`: модель змінилась)" in md and "- Причина запуску: змінили промпт" in md
    assert "### Були «можна публікувати», стали «ні» (2)" in md and "Title a2" in md and "Title x1" in md
    assert tables == [hc_report.DB_TITLE] and len(rows) == 2  # таблиця та сама, рядок новий
    assert rows[1]["Вердикт"] == {"select": {"name": "❌ Не пройшла"}} and rows[1]["Різниця, п. п."] == {"number": -50.0}
    assert rows[1]["Версія моделі"]["rich_text"][0]["text"]["content"] == "runpod-ep:v2"
    assert len(outbox["requests"]) == 2
    text = outbox["requests"][1][1]["text"]
    assert text.startswith(f"❌ Health Check {second_id}: модель не пройшла\nМожна публікувати: 50% проти 100% в останньому "
                           f"успішному прогоні {first_id} (−50 п. п.) — впало більше за поріг 5 п. п.\n")
    assert "Запуск: змінили промпт" in text and text.endswith("Звіт: https://www.notion.so/row-2")
    assert "sk-test" not in text and "123:secret" not in text

    # ті самі дані вдруге: нічого не оцінюється заново, рядок у Notion і повідомлення не дублюються
    await hc_flow.after_run(second_id)
    assert len(api_calls) == 6 and len(rows) == 2 and len(outbox["requests"]) == 2

    with TestClient(app) as c:  # і все це видно через API
        assert c.get(f"/health-check/runs/{second_id}/report").text == md
        compare = c.get(f"/health-check/compare?run={second_id}").json()
        runs = c.get("/health-check/runs").json()
    assert compare["base_run"] == first_id
    assert [(r["run_id"], r["note"]) for r in runs[:2]] == [(second_id, "змінили промпт"), (first_id, None)]
    assert hc_verdict.RANK[report["verdict"]] == 2
